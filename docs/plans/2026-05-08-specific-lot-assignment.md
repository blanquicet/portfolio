# Specific-Lot Assignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow individual sell transactions to be linked to specific buy/vesting lots, overriding FIFO order when the user explicitly chose which lot to sell.

**Architecture:** A new `lot_assignments` table stores (sell_id → buy_id, qty) links. `FifoQueue` gains a `consume_specific(assignments)` method. `build_queues()` loads assignments from the DB and uses them when present, falling back to FIFO otherwise. `tax_report.py` requires no changes — it inherits correct lot matching automatically.

**Tech Stack:** SQLite, Python 3, existing `tools/fifo.py` engine.

---

## File Map

| File | Change |
|------|--------|
| `schema.sql` | Add `lot_assignments` table definition |
| `portfolio.db` | Apply migration (CREATE TABLE) |
| `tools/fifo.py` | Add `FifoQueue.consume_specific()` + update `build_queues()` to load & apply assignments |
| `tools/assign_lot.py` | New CLI tool: insert/list/delete lot assignments |
| `tests/test_fifo.py` | New: unit tests for `consume_specific` and mixed FIFO+specific scenarios |

---

## Task 1: Add `lot_assignments` table to schema and DB

**Files:**
- Modify: `schema.sql`
- Modify: `portfolio.db` (run migration via sqlite3)

- [ ] **Step 1: Add table definition to schema.sql**

Append to the end of `schema.sql`:

```sql
-- Specific-lot assignments: override FIFO for a particular sell transaction.
-- When a sell_id has rows here, those exact buy lots are consumed (in row order)
-- instead of the oldest-first FIFO default.
-- Partial coverage is NOT supported: either all qty of the sell is covered by
-- assignments, or none are (falls back to FIFO).
CREATE TABLE IF NOT EXISTS lot_assignments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sell_id  INTEGER NOT NULL REFERENCES transactions(id),
    buy_id   INTEGER NOT NULL REFERENCES transactions(id),
    quantity REAL    NOT NULL CHECK(quantity > 0),
    UNIQUE(sell_id, buy_id)
);
```

- [ ] **Step 2: Apply migration to the live DB**

```bash
cd /Users/melendex/Documents/src/portfolio
sqlite3 portfolio.db "
CREATE TABLE IF NOT EXISTS lot_assignments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sell_id  INTEGER NOT NULL REFERENCES transactions(id),
    buy_id   INTEGER NOT NULL REFERENCES transactions(id),
    quantity REAL    NOT NULL CHECK(quantity > 0),
    UNIQUE(sell_id, buy_id)
);
"
```

Verify:
```bash
sqlite3 portfolio.db ".schema lot_assignments"
```
Expected output:
```
CREATE TABLE lot_assignments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sell_id  INTEGER NOT NULL REFERENCES transactions(id),
    buy_id   INTEGER NOT NULL REFERENCES transactions(id),
    quantity REAL    NOT NULL CHECK(quantity > 0),
    UNIQUE(sell_id, buy_id)
);
```

- [ ] **Step 3: Commit**

```bash
git add schema.sql
git commit -m "feat: add lot_assignments table for specific-lot sell tracking"
```

---

## Task 2: Add `FifoQueue.consume_specific()` method

**Files:**
- Modify: `tools/fifo.py` — `FifoQueue` class
- Create: `tests/test_fifo.py`

The existing `lots` list stores `[qty_remaining, price_usd, date_str, source]`. The `source` field already contains `"broker YYYY-MM-DD id=NNN"` — we need to match by `buy_id`. To enable this, `add()` must also store the transaction ID.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fifo.py`:

```python
"""Unit tests for FifoQueue specific-lot consumption."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))
from fifo import FifoQueue


def make_queue():
    """Three lots: ids 10, 11, 12 with 10 units each."""
    q = FifoQueue()
    q.add(qty=10, price_usd=100.0, dt="2024-01-01", source="broker 2024-01-01 id=10", buy_id=10)
    q.add(qty=10, price_usd=200.0, dt="2024-06-01", source="broker 2024-06-01 id=11", buy_id=11)
    q.add(qty=10, price_usd=300.0, dt="2025-01-01", source="broker 2025-01-01 id=12", buy_id=12)
    return q


def test_consume_specific_full_lot():
    """Consume an entire lot by buy_id, skipping older ones."""
    q = make_queue()
    # Sell 10 units from lot 12 (newest), skipping lots 10 and 11
    consumed = q.consume_specific([(12, 10.0)])
    assert len(consumed) == 1
    qty, price, buy_date, src = consumed[0]
    assert abs(qty - 10.0) < 1e-6
    assert abs(price - 300.0) < 1e-6
    assert buy_date == "2025-01-01"
    # Lots 10 and 11 must be untouched
    remaining = q.remaining_lots()
    assert len(remaining) == 2
    assert any(abs(p - 100.0) < 1e-6 for _, p, _, _ in remaining)
    assert any(abs(p - 200.0) < 1e-6 for _, p, _, _ in remaining)


def test_consume_specific_partial_lot():
    """Consume part of a lot by buy_id."""
    q = make_queue()
    consumed = q.consume_specific([(11, 4.0)])
    assert len(consumed) == 1
    qty, price, _, _ = consumed[0]
    assert abs(qty - 4.0) < 1e-6
    assert abs(price - 200.0) < 1e-6
    # Lot 11 should have 6 units remaining
    remaining = {src.split("id=")[1]: r_qty for r_qty, _, _, src in q.remaining_lots()}
    assert abs(float(remaining["11"]) - 6.0) < 1e-6


def test_consume_specific_multiple_lots():
    """Consume from multiple specific lots in one call."""
    q = make_queue()
    # Sell 5 from lot 10 and 5 from lot 12 (skipping lot 11)
    consumed = q.consume_specific([(10, 5.0), (12, 5.0)])
    assert len(consumed) == 2
    prices = {round(p) for _, p, _, _ in consumed}
    assert prices == {100, 300}
    # Lot 11 untouched (10 units), lots 10 and 12 have 5 each
    remaining = q.remaining_lots()
    assert len(remaining) == 3
    remaining_by_price = {round(p): q for q, p, _, _ in remaining}
    assert abs(remaining_by_price[100] - 5.0) < 1e-6
    assert abs(remaining_by_price[200] - 10.0) < 1e-6
    assert abs(remaining_by_price[300] - 5.0) < 1e-6


def test_consume_specific_unknown_buy_id_raises():
    """Raises ValueError if buy_id does not exist in queue."""
    q = make_queue()
    try:
        q.consume_specific([(999, 5.0)])
        assert False, "should have raised"
    except ValueError as e:
        assert "999" in str(e)


def test_consume_specific_exceeds_lot_qty_raises():
    """Raises ValueError if requested qty exceeds lot's remaining qty."""
    q = make_queue()
    try:
        q.consume_specific([(10, 15.0)])  # lot 10 only has 10
        assert False, "should have raised"
    except ValueError as e:
        assert "10" in str(e)


def test_fifo_unaffected_when_no_assignments():
    """Normal FIFO consume still works — no regression."""
    q = make_queue()
    consumed = q.consume(15.0)
    # Should consume all 10 from lot 10 and 5 from lot 11
    total_qty = sum(c[0] for c in consumed)
    assert abs(total_qty - 15.0) < 1e-6
    prices_consumed = [round(c[1]) for c in consumed]
    assert 100 in prices_consumed
    assert 200 in prices_consumed
    assert 300 not in prices_consumed
```

- [ ] **Step 2: Run tests — expect failures**

```bash
cd /Users/melendex/Documents/src/portfolio
python3 -m pytest tests/test_fifo.py -v 2>&1 | head -40
```

Expected: multiple FAILs — `add()` doesn't accept `buy_id`, `consume_specific` doesn't exist yet.

- [ ] **Step 3: Update `FifoQueue.add()` to accept `buy_id` and store it**

In `tools/fifo.py`, update `FifoQueue`:

```python
class FifoQueue:
    """FIFO lot queue for one instrument."""

    def __init__(self):
        # Each lot: [qty_remaining, price_usd, date_str, source, buy_id]
        self.lots = []

    def add(self, qty, price_usd, dt, source, buy_id=None):
        self.lots.append([qty, price_usd, dt, source, buy_id])

    def consume(self, qty_needed):
        """
        Consume qty_needed units FIFO (oldest first).
        Returns list of (qty_consumed, price_usd, buy_date, source).
        Raises ValueError if queue is insufficient.
        """
        consumed = []
        remaining = qty_needed
        for lot in self.lots:
            if remaining <= 0:
                break
            lot_qty, price_usd, buy_date, source, _buy_id = lot
            if lot_qty <= 0:
                continue
            take = min(lot_qty, remaining)
            consumed.append((take, price_usd, buy_date, source))
            lot[0] -= take
            remaining -= take
        if remaining > 1e-6:
            raise ValueError(f"FIFO insufficient: missing {remaining:.4f} units")
        return consumed

    def consume_specific(self, assignments):
        """
        Consume specific lots by buy_id.

        assignments: list of (buy_id, qty) tuples — consumed in order given.

        Returns list of (qty_consumed, price_usd, buy_date, source) — same
        format as consume(), so callers are interchangeable.

        Raises ValueError if:
          - a buy_id is not found in this queue
          - requested qty exceeds the lot's remaining qty
        """
        # Index lots by buy_id for O(1) lookup
        lot_index = {}
        for lot in self.lots:
            bid = lot[4]  # buy_id
            if bid is not None:
                lot_index[bid] = lot

        consumed = []
        for buy_id, qty_needed in assignments:
            if buy_id not in lot_index:
                raise ValueError(
                    f"buy_id {buy_id} not found in queue "
                    f"(available: {sorted(lot_index.keys())})"
                )
            lot = lot_index[buy_id]
            lot_qty, price_usd, buy_date, source, _bid = lot
            if qty_needed > lot_qty + 1e-6:
                raise ValueError(
                    f"buy_id {buy_id}: requested {qty_needed:.4f} "
                    f"but only {lot_qty:.4f} remaining"
                )
            take = min(qty_needed, lot_qty)
            consumed.append((take, price_usd, buy_date, source))
            lot[0] -= take
        return consumed

    def remaining_lots(self):
        """Return lots with qty > 0 (i.e. not yet sold)."""
        return [(qty, price_usd, dt, src)
                for qty, price_usd, dt, src, _bid in self.lots
                if qty > 1e-6]

    def avg_cost_usd(self):
        """Weighted average cost in USD of remaining lots."""
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
```

- [ ] **Step 4: Run tests — expect all pass**

```bash
cd /Users/melendex/Documents/src/portfolio
python3 -m pytest tests/test_fifo.py -v
```

Expected: 6 PASSED.

- [ ] **Step 5: Verify snapshot still works end-to-end**

```bash
python3 tools/snapshot.py 2>/dev/null | tail -5
```

Expected: TOTAL line prints without error.

- [ ] **Step 6: Commit**

```bash
git add tools/fifo.py tests/test_fifo.py
git commit -m "feat: FifoQueue.consume_specific() — sell against explicit lot by buy_id"
```

---

## Task 3: Load assignments in `build_queues()` and apply them

**Files:**
- Modify: `tools/fifo.py` — `build_queues()` function only

`build_queues()` currently calls `queues[isin].consume(qty)` for every sell. We need to:
1. Pass `buy_id` to `add()` (already done in Task 2).
2. Load `lot_assignments` for the date window.
3. When processing a sell, if it has assignments → `consume_specific()`, else → `consume()`.

- [ ] **Step 1: Add integration test**

Append to `tests/test_fifo.py`:

```python
import sqlite3

def make_test_db():
    """In-memory DB with two buys and one specific-lot sell."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE securities (
            id INTEGER PRIMARY KEY, isin TEXT, name TEXT, currency TEXT
        );
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, security_id INTEGER, date TEXT,
            type TEXT, broker TEXT, quantity REAL, currency TEXT,
            total REAL, fee REAL DEFAULT 0
        );
        CREATE TABLE fx_rates (
            date TEXT, from_currency TEXT, to_currency TEXT, rate REAL,
            PRIMARY KEY(date, from_currency, to_currency)
        );
        CREATE TABLE lot_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sell_id INTEGER, buy_id INTEGER, quantity REAL,
            UNIQUE(sell_id, buy_id)
        );
        INSERT INTO securities VALUES (1, 'US0000000001', 'Test Corp', 'USD');
        -- Buy 10 @ $100 (id=1), Buy 10 @ $200 (id=2), Sell 10 (id=3)
        INSERT INTO transactions VALUES
            (1, 1, '2024-01-01', 'buy',  'ibkr', 10, 'USD', 1000, 0),
            (2, 1, '2024-06-01', 'buy',  'ibkr', 10, 'USD', 2000, 0),
            (3, 1, '2025-01-01', 'sell', 'ibkr', 10, 'USD', 1800, 0);
        -- Assign the sell (id=3) to the SECOND buy (id=2), not the first
        INSERT INTO lot_assignments VALUES (1, 3, 2, 10.0);
    """)
    return conn


def test_build_queues_respects_specific_assignment():
    """build_queues uses consume_specific when lot_assignments exist."""
    from fifo import build_queues
    conn = make_test_db()
    queues, errors = build_queues(conn)
    conn.close()

    assert not errors
    lots = queues['US0000000001'].remaining_lots()
    # Only the FIRST buy (@ $100) should remain — the second was consumed
    assert len(lots) == 1
    qty, price, dt, _ = lots[0]
    assert abs(price - 100.0) < 1e-6, f"Expected $100 lot to remain, got {price}"
    assert dt == "2024-01-01"


def test_build_queues_fifo_when_no_assignment():
    """build_queues uses FIFO when no lot_assignments exist."""
    from fifo import build_queues
    conn = make_test_db()
    # Remove the assignment → should fall back to FIFO (consume first buy)
    conn.execute("DELETE FROM lot_assignments")
    queues, errors = build_queues(conn)
    conn.close()

    assert not errors
    lots = queues['US0000000001'].remaining_lots()
    # FIFO: first buy (@ $100) consumed, second (@ $200) remains
    assert len(lots) == 1
    qty, price, dt, _ = lots[0]
    assert abs(price - 200.0) < 1e-6, f"Expected $200 lot to remain (FIFO), got {price}"
```

- [ ] **Step 2: Run new tests — expect 2 failures**

```bash
python3 -m pytest tests/test_fifo.py::test_build_queues_respects_specific_assignment \
                  tests/test_fifo.py::test_build_queues_fifo_when_no_assignment -v
```

Expected: 2 FAILED (`lot_assignments` not loaded yet).

- [ ] **Step 3: Update `build_queues()` in `tools/fifo.py`**

Replace the entire `build_queues` function:

```python
def build_queues(conn, as_of_date=None):
    """
    Read all buy/vesting/sell transactions from the DB and return
    a dict {isin: FifoQueue} with FIFO state reflecting all sales.

    If a sell transaction has rows in `lot_assignments`, those specific
    buy lots are consumed instead of the oldest-first FIFO default.

    as_of_date: ISO string 'YYYY-MM-DD'. If given, only transactions
                up to that date are included. Defaults to today.
    """
    if as_of_date:
        date_clause = "AND t.date <= ?"
        params = [as_of_date]
    else:
        date_clause = "AND t.date <= date('now')"
        params = []

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
    """, params).fetchall()

    # Load all lot_assignments (keyed by sell_id)
    # Each entry: sell_id → [(buy_id, quantity), ...]
    assignments_by_sell = defaultdict(list)
    try:
        for row in conn.execute(
            "SELECT sell_id, buy_id, quantity FROM lot_assignments ORDER BY id"
        ):
            assignments_by_sell[row[0]].append((row[1], row[2]))
    except Exception:
        pass  # table may not exist in test DBs created before migration

    queues = defaultdict(FifoQueue)
    errors = []

    for r in rows:
        isin  = r["isin"]
        qty   = r["quantity"]
        dt    = r["date"]
        typ   = r["type"]
        ccy   = r["t_ccy"]
        total = r["total"]
        tid   = r["id"]
        src   = f"{r['broker']} {r['date']} id={tid}"

        if typ in ("buy", "vesting"):
            price_usd = to_usd(conn, total / qty if total and qty else 0, ccy, dt)
            queues[isin].add(qty, price_usd, dt, src, buy_id=tid)

        elif typ == "sell":
            if tid in assignments_by_sell:
                try:
                    queues[isin].consume_specific(assignments_by_sell[tid])
                except ValueError as e:
                    errors.append(f"{r['name']} {dt} (specific lot): {e}")
            else:
                try:
                    queues[isin].consume(qty)
                except ValueError as e:
                    errors.append(f"{r['name']} {dt}: {e}")

        elif typ == "sell_to_cover":
            # STC physically removes shares — consume FIFO lots so queue stays
            # accurate. We do NOT report capital gain (income already declared
            # as labor income in employer CIR / Microsoft casilla 46).
            try:
                queues[isin].consume(qty)
            except ValueError as e:
                errors.append(f"{r['name']} {dt} STC: {e}")

        # transfer_in / transfer_out: no queue change (FOP — basis preserved)

    return queues, errors
```

- [ ] **Step 4: Run all tests**

```bash
python3 -m pytest tests/test_fifo.py -v
```

Expected: 8 PASSED, 0 failed.

- [ ] **Step 5: Verify snapshot and tax report end-to-end**

```bash
python3 tools/snapshot.py 2>/dev/null | tail -5
python3 tools/tax_report.py 2025 2>/dev/null | tail -20
```

Expected: both run without errors, totals unchanged (no lot_assignments in live DB yet).

- [ ] **Step 6: Commit**

```bash
git add tools/fifo.py tests/test_fifo.py
git commit -m "feat: build_queues uses consume_specific when lot_assignments exist"
```

---

## Task 4: CLI tool to manage lot assignments

**Files:**
- Create: `tools/assign_lot.py`

This tool lets you insert, list, and delete assignments without opening SQLite manually.

- [ ] **Step 1: Create `tools/assign_lot.py`**

```python
#!/usr/bin/env python3
"""
Manage specific-lot assignments for sell transactions.

Usage:
    python3 tools/assign_lot.py list [sell_id]
        Show all assignments, or only those for a given sell_id.

    python3 tools/assign_lot.py add <sell_id> <buy_id> <qty>
        Assign qty units of buy_id to sell_id.
        Validates that both transaction IDs exist and have compatible ISINs.

    python3 tools/assign_lot.py delete <sell_id> [buy_id]
        Delete all assignments for sell_id, or only the sell_id+buy_id pair.

    python3 tools/assign_lot.py show <sell_id>
        Show the sell transaction and its available lots for the same ISIN.
"""
import sqlite3, sys, os

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")


def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_list(conn, sell_id=None):
    where = f"WHERE la.sell_id = {sell_id}" if sell_id else ""
    rows = conn.execute(f"""
        SELECT
            la.id, la.sell_id, la.buy_id, la.quantity,
            s_sell.date AS sell_date, sec_sell.name AS sell_name,
            s_buy.date  AS buy_date,  sec_buy.name  AS buy_name
        FROM lot_assignments la
        JOIN transactions s_sell ON s_sell.id = la.sell_id
        JOIN securities sec_sell ON sec_sell.id = s_sell.security_id
        JOIN transactions s_buy  ON s_buy.id  = la.buy_id
        JOIN securities sec_buy  ON sec_buy.id = s_buy.security_id
        {where}
        ORDER BY la.sell_id, la.id
    """).fetchall()

    if not rows:
        print("No lot assignments found.")
        return

    print(f"\n  {'ID':>4}  {'sell_id':>7}  {'sell_date':<12}  {'buy_id':>6}  {'buy_date':<12}  {'qty':>8}  security")
    print(f"  {'-'*80}")
    for r in rows:
        print(f"  {r['id']:>4}  {r['sell_id']:>7}  {r['sell_date']:<12}  "
              f"{r['buy_id']:>6}  {r['buy_date']:<12}  {r['quantity']:>8.4f}  {r['sell_name']}")
    print()


def cmd_show(conn, sell_id):
    """Show the sell transaction and available buy lots for the same security."""
    sell = conn.execute("""
        SELECT t.*, s.isin, s.name FROM transactions t
        JOIN securities s ON s.id = t.security_id
        WHERE t.id = ? AND t.type IN ('sell', 'sell_to_cover')
    """, (sell_id,)).fetchone()

    if not sell:
        print(f"Error: transaction id={sell_id} not found or is not a sell.")
        sys.exit(1)

    print(f"\n  Sell: id={sell_id}  {sell['date']}  {sell['name']}  "
          f"qty={sell['quantity']}  broker={sell['broker']}")

    buys = conn.execute("""
        SELECT t.id, t.date, t.quantity, t.total, t.currency, t.broker
        FROM transactions t
        JOIN securities s ON s.id = t.security_id
        WHERE s.isin = ? AND t.type IN ('buy', 'vesting')
          AND t.date <= ?
        ORDER BY t.date
    """, (sell['isin'], sell['date'])).fetchall()

    if not buys:
        print("  No buy lots found for this security before the sell date.")
        return

    print(f"\n  Available buy lots (same ISIN, on or before sell date):")
    print(f"  {'buy_id':>6}  {'date':<12}  {'qty':>8}  {'total':>10}  broker")
    print(f"  {'-'*55}")
    for b in buys:
        print(f"  {b['id']:>6}  {b['date']:<12}  {b['quantity']:>8.4f}  "
              f"{b['total']:>10.2f}  {b['broker']}")
    print()


def cmd_add(conn, sell_id, buy_id, qty):
    # Validate sell
    sell = conn.execute(
        "SELECT t.*, s.isin FROM transactions t JOIN securities s ON s.id=t.security_id WHERE t.id=?",
        (sell_id,)
    ).fetchone()
    if not sell or sell["type"] not in ("sell", "sell_to_cover"):
        print(f"Error: id={sell_id} is not a sell/sell_to_cover transaction.")
        sys.exit(1)

    # Validate buy
    buy = conn.execute(
        "SELECT t.*, s.isin FROM transactions t JOIN securities s ON s.id=t.security_id WHERE t.id=?",
        (buy_id,)
    ).fetchone()
    if not buy or buy["type"] not in ("buy", "vesting"):
        print(f"Error: id={buy_id} is not a buy/vesting transaction.")
        sys.exit(1)

    # Same ISIN
    if sell["isin"] != buy["isin"]:
        print(f"Error: ISIN mismatch — sell is {sell['isin']}, buy is {buy['isin']}.")
        sys.exit(1)

    # Buy must be before sell
    if buy["date"] > sell["date"]:
        print(f"Error: buy date {buy['date']} is after sell date {sell['date']}.")
        sys.exit(1)

    conn.execute(
        "INSERT INTO lot_assignments (sell_id, buy_id, quantity) VALUES (?, ?, ?)",
        (sell_id, buy_id, qty)
    )
    conn.commit()
    print(f"✓ Assigned {qty} units of buy id={buy_id} ({buy['date']}) "
          f"to sell id={sell_id} ({sell['date']}).")


def cmd_delete(conn, sell_id, buy_id=None):
    if buy_id:
        conn.execute(
            "DELETE FROM lot_assignments WHERE sell_id=? AND buy_id=?",
            (sell_id, buy_id)
        )
    else:
        conn.execute("DELETE FROM lot_assignments WHERE sell_id=?", (sell_id,))
    conn.commit()
    print(f"✓ Deleted assignment(s) for sell_id={sell_id}"
          + (f" buy_id={buy_id}" if buy_id else "") + ".")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    conn = get_conn()
    cmd = sys.argv[1]

    if cmd == "list":
        sell_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
        cmd_list(conn, sell_id)

    elif cmd == "show":
        if len(sys.argv) < 3:
            print("Usage: assign_lot.py show <sell_id>")
            sys.exit(1)
        cmd_show(conn, int(sys.argv[2]))

    elif cmd == "add":
        if len(sys.argv) < 5:
            print("Usage: assign_lot.py add <sell_id> <buy_id> <qty>")
            sys.exit(1)
        cmd_add(conn, int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]))

    elif cmd == "delete":
        if len(sys.argv) < 3:
            print("Usage: assign_lot.py delete <sell_id> [buy_id]")
            sys.exit(1)
        buy_id = int(sys.argv[3]) if len(sys.argv) > 3 else None
        cmd_delete(conn, int(sys.argv[2]), buy_id)

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test the CLI**

```bash
cd /Users/melendex/Documents/src/portfolio
python3 tools/assign_lot.py list
```
Expected: `No lot assignments found.`

```bash
python3 tools/assign_lot.py show <sell_id_of_atlassian_sale>
```
(Replace `<sell_id_of_atlassian_sale>` with the actual transaction ID. Find it with:
`sqlite3 portfolio.db "SELECT t.id, t.date, t.quantity, s.name FROM transactions t JOIN securities s ON s.id=t.security_id WHERE t.type='sell' AND s.isin='US0494681010';"`)

Expected: shows the sell and the available buy lots for Atlassian.

- [ ] **Step 3: Commit**

```bash
git add tools/assign_lot.py
git commit -m "feat: assign_lot.py CLI — manage specific-lot sell assignments"
```

---

## Self-Review

**Spec coverage:**
- ✅ `lot_assignments` table — Task 1
- ✅ `consume_specific()` method — Task 2
- ✅ `build_queues()` applies specific lots, falls back to FIFO — Task 3
- ✅ CLI tool to insert/list/delete assignments — Task 4
- ✅ `tax_report.py` inherits correct behavior — no changes needed (covered in Task 3 e2e test)
- ✅ Retrocompatible — existing DB with no `lot_assignments` rows behaves identically

**Placeholder scan:** None found — all steps have exact code, commands, and expected output.

**Type consistency:**
- `lots` list element: `[qty, price_usd, dt, source, buy_id]` — used consistently in `add()`, `consume()`, `consume_specific()`, `remaining_lots()`.
- `consume_specific(assignments)` where `assignments = [(buy_id, qty), ...]` — consistent between `FifoQueue` method signature, `build_queues()` call, and test fixtures.
