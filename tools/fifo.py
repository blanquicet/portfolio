"""
Shared FIFO engine and FX helpers for the portfolio tracker.

Used by snapshot.py (current positions cost basis) and
tax_report.py (realized gains per fiscal year).

FIFO rules:
  - buy / vesting      → add lot to queue
  - sell               → consume lots FIFO (oldest first)
  - sell_to_cover      → does NOT consume FIFO lots;
                         cost = sale price, gain = $0
                         (vest value already in employer CIR)
  - transfer_in/out    → do not touch the FIFO queue;
                         FOP transfers preserve original cost basis
"""
import sqlite3
from collections import defaultdict

# ──────────────────────────────────────────────────────────────────────────────
# FX helpers
# ──────────────────────────────────────────────────────────────────────────────

def fx(conn, from_ccy, to_ccy, dt):
    """
    Historical FX rate for date dt (or nearest prior business day).
    Returns None if not found.
    """
    row = conn.execute("""
        SELECT rate FROM fx_rates
        WHERE from_currency = ? AND to_currency = ? AND date <= ?
        ORDER BY date DESC LIMIT 1
    """, (from_ccy, to_ccy, dt)).fetchone()
    return row[0] if row else None


def to_usd(conn, amount, ccy, dt):
    """Convert amount in ccy → USD at historical rate on dt."""
    if ccy == "USD":
        return amount
    if ccy == "EUR":
        rate = fx(conn, "EUR", "USD", dt)
        return amount * rate if rate else None
    if ccy == "GBP":
        rate = fx(conn, "GBP", "USD", dt)
        return amount * rate if rate else None
    return None


# ──────────────────────────────────────────────────────────────────────────────
# FIFO queue
# ──────────────────────────────────────────────────────────────────────────────

class FifoQueue:
    """FIFO lot queue for one instrument."""

    def __init__(self):
        self.lots = []   # [qty_remaining, price_usd, date_str, source]

    def add(self, qty, price_usd, dt, source):
        self.lots.append([qty, price_usd, dt, source])

    def consume(self, qty_needed):
        """
        Consume qty_needed units FIFO.
        Returns list of (qty_consumed, price_usd, buy_date, source).
        Raises ValueError if queue is insufficient.
        """
        consumed = []
        remaining = qty_needed
        for lot in self.lots:
            if remaining <= 0:
                break
            lot_qty, price_usd, buy_date, source = lot
            if lot_qty <= 0:
                continue
            take = min(lot_qty, remaining)
            consumed.append((take, price_usd, buy_date, source))
            lot[0] -= take
            remaining -= take
        if remaining > 1e-6:
            raise ValueError(f"FIFO insufficient: missing {remaining:.4f} units")
        return consumed

    def remaining_lots(self):
        """Return lots with qty > 0 (i.e. not yet sold)."""
        return [(qty, price_usd, dt, src)
                for qty, price_usd, dt, src in self.lots
                if qty > 1e-6]

    def avg_cost_usd(self):
        """
        Weighted average cost in USD of remaining lots.
        Returns None if no lots remain.
        """
        lots = self.remaining_lots()
        if not lots:
            return None
        total_val = sum(qty * price for qty, price, _, _ in lots)
        total_qty = sum(qty for qty, _, _, _ in lots)
        return total_val / total_qty if total_qty else None

    def oldest_buy_date(self):
        """Date of the oldest remaining lot."""
        lots = self.remaining_lots()
        return min(dt for _, _, dt, _ in lots) if lots else None


# ──────────────────────────────────────────────────────────────────────────────
# Build queues from DB
# ──────────────────────────────────────────────────────────────────────────────

def build_queues(conn, as_of_date=None):
    """
    Read all buy/vesting/sell transactions from the DB and return
    a dict {isin: FifoQueue} with FIFO state reflecting all sales.

    as_of_date: ISO string 'YYYY-MM-DD'. If given, only transactions
                up to that date are included. Defaults to today.
    """
    date_filter = as_of_date or "date('now')"
    if as_of_date:
        date_clause = f"AND t.date <= '{as_of_date}'"
    else:
        date_clause = "AND t.date <= date('now')"

    rows = conn.execute(f"""
        SELECT
            s.isin, s.name, s.currency AS db_ccy,
            t.id, t.date, t.type, t.broker,
            t.quantity, t.currency AS t_ccy, t.total, t.fee
        FROM transactions t
        JOIN securities s ON s.id = t.security_id
        WHERE t.type IN ('buy','vesting','sell','sell_to_cover','transfer_in','transfer_out')
          {date_clause}
        ORDER BY t.date, t.id
    """).fetchall()

    queues = defaultdict(FifoQueue)
    errors = []

    for r in rows:
        isin  = r["isin"]
        qty   = r["quantity"]
        dt    = r["date"]
        typ   = r["type"]
        ccy   = r["t_ccy"]
        total = r["total"]
        src   = f"{r['broker']} {r['date']} id={r['id']}"

        if typ in ("buy", "vesting"):
            price_usd = to_usd(conn, total / qty if total and qty else 0, ccy, dt)
            queues[isin].add(qty, price_usd, dt, src)

        elif typ == "sell":
            try:
                queues[isin].consume(qty)
            except ValueError as e:
                errors.append(f"{r['name']} {dt}: {e}")

        # sell_to_cover: cost = sale price, no FIFO consumption
        # transfer_in / transfer_out: no queue change

    return queues, errors
