#!/usr/bin/env python3
"""
Portfolio snapshot — net positions with live prices, unrealized P&L, portfolio %.

Usage:
    python3 tools/snapshot.py                  # all brokers, values in USD
    python3 tools/snapshot.py ibkr             # IBKR only, USD
    python3 tools/snapshot.py trii             # Trii only, USD
    python3 tools/snapshot.py trii cop         # Trii only, values in COP

Price source: Yahoo Finance (yfinance).
  - USD prices → used directly.
  - EUR prices → × EURUSD.
  - COP prices → ÷ USDCOP.
  - All market values summed in the display currency (USD default, COP if requested).
"""
import sqlite3, sys, os, warnings
warnings.filterwarnings("ignore")
import yfinance as yf
sys.path.insert(0, os.path.dirname(__file__))
from fifo import build_queues

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")


def load_ticker_map_from_db(conn, preferred_exchanges: list = None) -> dict:
    """
    Load {isin: ticker} from ticker_mappings.
    If preferred_exchanges is given (e.g. ['XBOG']), those entries win over others.
    Within same priority, manual beats auto.
    """
    rows = conn.execute(
        "SELECT isin, exchange, ticker, source FROM ticker_mappings"
    ).fetchall()

    preferred = set(preferred_exchanges or [])

    def priority(row):
        is_pref   = 1 if row[1] in preferred else 0
        is_manual = 1 if row[3] == "manual"  else 0
        return (is_pref, is_manual)

    result = {}
    for row in sorted(rows, key=priority):
        result[row[0]] = row[2]
    return result


SQL = """
SELECT
  s.isin,
  s.name                                                            AS security,
  s.currency                                                        AS db_ccy,
  ROUND(SUM(
    CASE WHEN t.type IN ('buy','vesting','transfer_in') THEN  t.quantity
         WHEN t.type IN ('sell','sell_to_cover','transfer_out') THEN -t.quantity
         ELSE 0 END
  ), 4)                                                             AS net_qty
FROM transactions t
JOIN securities s ON s.id = t.security_id
WHERE t.date <= date('now')
  {broker_filter}
GROUP BY s.id, s.isin, s.name, s.currency
HAVING net_qty > 0.001
ORDER BY s.currency DESC, s.name;
"""


def _fetch_single(ticker: str) -> tuple:
    """Fetch price and currency for a single ticker via fast_info."""
    try:
        info = yf.Ticker(ticker).fast_info
        p   = getattr(info, "last_price", None)
        ccy = getattr(info, "currency", "USD") or "USD"
        if p and p > 0:
            return float(p), ccy
    except Exception:
        pass
    return None, "USD"


def fetch_prices(isins: list, ticker_map: dict) -> tuple:
    """
    Returns:
        prices  : {isin: price_in_usd}
        display : {isin: (price_native, yahoo_ccy)}
        fx      : {'EURUSD': float, 'USDCOP': float}
    """
    # ── FX rates
    fx = {"EURUSD": 1.12, "USDCOP": 4100.0}
    try:
        fx_raw = yf.download(["EURUSD=X", "COP=X"], period="2d",
                             progress=False, auto_adjust=True)
        if not fx_raw.empty:
            closes = fx_raw["Close"]
            try:
                fx["EURUSD"] = float(closes["EURUSD=X"].dropna().iloc[-1])
            except Exception:
                pass
            try:
                fx["USDCOP"] = float(closes["COP=X"].dropna().iloc[-1])
            except Exception:
                pass
    except Exception:
        pass

    isin_to_ticker = {i: ticker_map[i] for i in isins if i in ticker_map}
    all_tickers    = list(set(isin_to_ticker.values()))

    if not all_tickers:
        return {}, {}, fx

    # ── Batch download
    ticker_price: dict = {}
    ticker_ccy:   dict = {}

    try:
        raw = yf.download(all_tickers, period="2d", progress=False, auto_adjust=True)
        if not raw.empty:
            closes_raw = raw["Close"]
            for t in all_tickers:
                try:
                    col  = closes_raw[t] if len(all_tickers) > 1 else closes_raw
                    last = col.dropna()
                    if len(last) > 0:
                        ticker_price[t] = float(last.iloc[-1])
                except Exception:
                    pass
    except Exception:
        pass

    # ── Per-ticker fallback + currency lookup
    for t in all_tickers:
        if t not in ticker_price:
            p, ccy = _fetch_single(t)
            if p:
                ticker_price[t] = p
                ticker_ccy[t]   = ccy
                continue
        # currency lookup (always)
        if t not in ticker_ccy:
            try:
                info = yf.Ticker(t).fast_info
                ccy  = getattr(info, "currency", None) or "USD"
                ticker_ccy[t] = ccy
            except Exception:
                ticker_ccy[t] = "USD"

    # ── Build output dicts
    prices  = {}
    display = {}
    for isin, ticker in isin_to_ticker.items():
        raw_price = ticker_price.get(ticker)
        if raw_price is None:
            continue
        yahoo_ccy = ticker_ccy.get(ticker, "USD")
        if yahoo_ccy == "USD":
            price_usd = raw_price
        elif yahoo_ccy == "EUR":
            price_usd = raw_price * fx["EURUSD"]
        elif yahoo_ccy == "COP":
            price_usd = raw_price / fx["USDCOP"]
        else:
            price_usd = raw_price   # best effort
        prices[isin]  = price_usd
        display[isin] = (raw_price, yahoo_ccy)

    unmapped = [i for i in isins if i not in ticker_map]
    if unmapped:
        print(f"  ⚠  No ticker for {len(unmapped)} ISINs (positions likely closed or untickered).",
              file=sys.stderr)

    return prices, display, fx


def run(broker=None, display_ccy="USD"):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    if broker:
        known = {r[0] for r in conn.execute("SELECT DISTINCT broker FROM transactions").fetchall()}
        if broker not in known:
            print(f"  ⚠  Unknown broker '{broker}'. Known: {sorted(known)}", file=sys.stderr)
            conn.close()
            sys.exit(1)

    if broker:
        rows = conn.execute(SQL.format(broker_filter="AND t.broker = ?"), (broker,)).fetchall()
    else:
        rows = conn.execute(SQL.format(broker_filter="")).fetchall()

    fifo_queues, _ = build_queues(conn)
    BROKER_EXCHANGE = {"trii": ["XBOG"]}
    preferred_ex    = BROKER_EXCHANGE.get(broker, []) if broker else []
    ticker_map      = load_ticker_map_from_db(conn, preferred_exchanges=preferred_ex)
    conn.close()

    title = f"broker: {broker.upper()}" if broker else "all brokers"
    print(f"\n  Fetching live prices…", end=" ", flush=True)
    isins = [r["isin"] for r in rows]
    prices, display_map, fx = fetch_prices(isins, ticker_map)
    eurusd  = fx["EURUSD"]
    usdcop  = fx["USDCOP"]
    print(f"done  (EUR/USD {eurusd:.4f}  |  USD/COP {usdcop:,.0f})")

    # ── Conversion helper: price_usd → display currency
    def to_display(usd_val):
        if usd_val is None:
            return None
        return usd_val * usdcop if display_ccy == "COP" else usd_val

    ccy_sym  = "COP" if display_ccy == "COP" else "USD"
    ccy_mark = "$" if display_ccy == "USD" else ""

    # ── First pass: totals
    portfolio_disp = 0.0
    enriched = []
    for r in rows:
        isin   = r["isin"]
        qty    = r["net_qty"]
        db_ccy = r["db_ccy"]

        price_usd            = prices.get(isin)
        price_native, y_ccy  = display_map.get(isin, (None, None))
        avg_cost_usd         = fifo_queues[isin].avg_cost_usd() if isin in fifo_queues else None
        mkt_val_usd          = qty * price_usd if price_usd is not None else None
        mkt_val_disp         = to_display(mkt_val_usd)

        if mkt_val_disp is not None:
            portfolio_disp += mkt_val_disp

        enriched.append({
            "name":          r["security"],
            "db_ccy":        db_ccy,
            "yahoo_ccy":     y_ccy,
            "qty":           qty,
            "avg_cost_usd":  avg_cost_usd,
            "avg_cost_disp": to_display(avg_cost_usd),
            "price_native":  price_native,
            "price_usd":     price_usd,
            "mkt_val_disp":  mkt_val_disp,
        })

    # ── Header
    W = 112
    print(f"\n{'='*W}")
    print(f"  Portfolio snapshot — {title}  [{ccy_sym}]")
    if display_ccy == "COP":
        print(f"  {__import__('datetime').date.today()}   "
              f"Total market value: {portfolio_disp:>15,.0f} COP")
    else:
        print(f"  {__import__('datetime').date.today()}   "
              f"Total market value: ${portfolio_disp:>12,.2f} USD")
    print(f"{'='*W}")

    if display_ccy == "COP":
        print(f"\n  {'Acción':<36} {'Qty':>8}  "
              f"{'Costo Avg':>13}  {'Precio':>13}  "
              f"{'Valor Mkt':>15}  {'P&L':>13}  {'P&L %':>7}  {'Port %':>7}")
    else:
        print(f"\n  {'Security':<36} {'Ccy':>4}  {'Qty':>8}  "
              f"{'AvgCost':>9}  {'Price':>9}  "
              f"{'Mkt Val $':>12}  {'Unreal P&L $':>13}  {'P&L %':>7}  {'Port %':>7}")
    print(f"  {'-'*(W-2)}")

    cur_ccy = None
    for d in enriched:
        if display_ccy != "COP" and d["db_ccy"] != cur_ccy:
            cur_ccy = d["db_ccy"]
            print(f"\n  ── {cur_ccy} instruments")

        qty       = d["qty"]
        price_nat = d["price_native"]
        price_usd = d["price_usd"]
        mv        = d["mkt_val_disp"]
        y_ccy     = d["yahoo_ccy"] or d["db_ccy"]
        ac_disp   = d["avg_cost_disp"]
        ac_usd    = d["avg_cost_usd"]

        # P&L always in display currency
        if ac_usd is not None and price_usd is not None:
            pnl_usd = (price_usd - ac_usd) * qty
            pnl_pct = (price_usd - ac_usd) / ac_usd * 100
            pnl_disp = pnl_usd * usdcop if display_ccy == "COP" else pnl_usd
            pnl_str     = f"{pnl_disp:>+13,.0f}" if display_ccy == "COP" else f"${pnl_usd:>+12,.0f}"
            pnl_pct_str = f"{pnl_pct:>+7.1f}%"
        else:
            pnl_str     = f"{'—':>13}"
            pnl_pct_str = f"{'—':>8}"

        port_pct = f"{mv/portfolio_disp*100:>7.1f}%" if mv and portfolio_disp else f"{'—':>8}"

        if display_ccy == "COP":
            mv_str  = f"{mv:>15,.0f}" if mv is not None else f"{'—':>15}"
            ac_str  = f"{ac_disp:>13,.0f}" if ac_disp else f"{'—':>13}"
            pr_str  = f"{price_nat:>13,.0f}" if price_nat is not None else f"{'—':>13}"
            print(f"  {d['name']:<36} {qty:>8.0f}  "
                  f"{ac_str}  {pr_str}  "
                  f"{mv_str}  {pnl_str}  {pnl_pct_str}  {port_pct}")
        else:
            mv_str  = f"${mv:>11,.2f}"  if mv  is not None else f"{'—':>12}"
            ac_str  = f"${ac_usd:>8.2f}" if ac_usd else f"{'—':>9}"
            pr_str  = f"{price_nat:>7.2f} {y_ccy}" if price_nat is not None else f"{'—':>9}    "
            print(f"  {d['name']:<36} {d['db_ccy']:>4}  {qty:>8.3f}  "
                  f"{ac_str}  {pr_str}  "
                  f"{mv_str}  {pnl_str}  {pnl_pct_str}  {port_pct}")

    # ── Footer
    print(f"\n  {'─'*(W-2)}")
    if display_ccy == "COP":
        print(f"  {'TOTAL':<36} {'':>8}  {'':>13}  {'':>13}  "
              f"{portfolio_disp:>15,.0f}  {'':>13}  {'':>8}  {'100.0%':>7}")
        print(f"\n  Notas:")
        print(f"  • Costo promedio = promedio ponderado FIFO de lotes vigentes, convertido a COP al TRM histórico.")
        print(f"  • P&L calculado en COP usando costo histórico — sin distorsión de TRM actual.")
    else:
        print(f"  {'TOTAL':<36}  {'':>4}  {'':>8}  {'':>9}  {'':>12}  "
              f"${portfolio_disp:>11,.2f}  {'':>13}  {'':>8}  {'100.0%':>7}")
        print(f"\n  Notes:")
        print(f"  • Avg cost in USD = FIFO weighted avg of remaining (unsold) lots, converted at historical FX.")
        print(f"  • P&L computed in USD using historical cost basis — no live FX distortion.")
    print()


if __name__ == "__main__":
    args       = [a.lower() for a in sys.argv[1:]]
    broker     = next((a for a in args if a not in ("cop", "usd")), None)
    display_ccy = "COP" if "cop" in args else "USD"
    run(broker, display_ccy)
