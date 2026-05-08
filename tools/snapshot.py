#!/usr/bin/env python3
"""
Portfolio snapshot — net positions with live prices, unrealized P&L, portfolio %.

Usage:
    python3 tools/snapshot.py            # all brokers combined
    python3 tools/snapshot.py ibkr       # IBKR only
    python3 tools/snapshot.py fidelity   # Fidelity only

Price source: Yahoo Finance (yfinance).
  - Currency per ticker is read from Yahoo's own `info['currency']` field.
  - USD prices → used directly.
  - EUR prices → × EURUSD  (e.g. MC.PA, WBTC.PA).
  - All market values summed in USD for portfolio totals.

EUR/USD transfer note:
  The `currency` in the DB is the instrument's TRADING currency, not the
  broker account currency. A transfer (FOP) doesn't change that.
  e.g. BTCWEUR stays EUR-denominated at both Scalable and IBKR.
  IWDA.L is the USD share class on LSE — stays USD regardless of broker.
"""
import sqlite3, sys, os, warnings
warnings.filterwarnings("ignore")
import yfinance as yf
sys.path.insert(0, os.path.dirname(__file__))
from fifo import build_queues

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

# ISIN → Yahoo Finance ticker
# Verified: Yahoo returns the correct currency via info['currency'] per ticker.
# LSE ETFs suffixed .L — all are USD share classes (Yahoo confirms USD).
# Euronext Paris *.PA — all EUR-quoted.
TICKER_MAP = {
    # ── US stocks (USD)
    "US00724F1012": "ADBE",
    "US0494681010": "TEAM",
    "US15118V2079": "CELH",
    "US1696561059": "CMG",
    "US25754A2015": "DPZ",
    "US26603R1068": "DUOL",
    "US45841N1072": "IBKR",
    "US4612021034": "INTU",
    "US5007673065": "KWEB",
    "US58733R1023": "MELI",
    "US30303M1027": "META",
    "US5949181045": "MSFT",
    "IL0011762130": "MNDY",
    "US6541061031": "NKE",
    "US02156V1098": "OKLO",
    "CH1134540470": "ONON",
    "KYG687071012": "PAGS",
    "US70450Y1038": "PYPL",
    "US79466L3024": "CRM",
    "US81762P1021": "NOW",
    "LU1778762911": "SPOT",
    "US9224751084": "VEEV",
    "US98138H1014": "WDAY",
    # ── US ETFs (USD)
    "US4642898427": "EPU",
    # ── LSE ETFs — Yahoo confirms USD-quoted (USD share class, not GBp)
    "IE00B4L5Y983": "IWDA.L",   # iShares Core MSCI World       → USD
    "IE00B5BMR087": "CSPX.L",   # iShares Core S&P 500          → USD
    "IE00BKM4GZ66": "EIMI.L",   # iShares Core EM IMI           → USD
    "IE00B579F325": "SGLD.L",   # Invesco Physical Gold ETC     → USD
    "IE00BGYWCB81": "VDEA.L",   # Vanguard USD EM Govt Bond     → USD
    "IE00BF16M727": "CIBR.L",   # First Trust Cybersecurity     → USD
    "IE00BYWZ0440": "IHYA.L",   # iShares Global HY Corp        → USD (sold)
    "IE00B43QJJ40": "GLAG.L",   # SPDR BBG Global Agg           → USD (sold)
    "LU0292109344": "XMBD.L",   # Xtrackers MSCI Brazil         → USD
    "LU1681045297": "ALAU.L",   # Amundi MSCI EM LatAm          → USD
    # ── Euronext Paris (SBF) — EUR-quoted. Rule: everything *.PA is EUR.
    "FR0000121014": "MC.PA",    # LVMH                          → EUR
    "LU1563454310": "CLIM.PA",  # Amundi Global Agg Green Bond  → EUR (sold)
    "LU1650489385": "MTE.PA",   # Amundi Euro Gov Bond 10-15Y   → EUR (sold)
    # ── WisdomTree Bitcoin EUR — Euronext Paris, EUR-quoted
    "GB00BJYDH287": "WBTC.PA",  # WisdomTree Physical Bitcoin → EUR
}

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


def fetch_prices(isins: list) -> tuple:
    """
    Returns:
        prices  : {isin: price_in_usd}   — all values normalised to USD
        display : {isin: (price_native, yahoo_ccy)}  — for showing native price
        fx      : {'EURUSD': float}
    """
    # FX rates — only EUR/USD needed (no GBP positions)
    fx_data = yf.download(
        ["EURUSD=X"], period="2d", progress=False, auto_adjust=True
    )
    fx = {"EURUSD": 1.12}
    if not fx_data.empty:
        closes = fx_data["Close"]
        fx["EURUSD"] = float(closes["EURUSD=X"].dropna().iloc[-1])

    # Build ticker list
    isin_to_ticker = {i: TICKER_MAP[i] for i in isins if i in TICKER_MAP}
    all_tickers = list(set(isin_to_ticker.values()))

    if not all_tickers:
        return {}, {}, fx

    # Batch download
    raw = yf.download(all_tickers, period="2d", progress=False, auto_adjust=True)
    closes_raw = raw["Close"] if not raw.empty else None

    # Per-ticker: get last price and Yahoo currency
    ticker_price = {}
    ticker_ccy   = {}
    for t in all_tickers:
        # Price from batch download
        try:
            if closes_raw is not None:
                col = closes_raw[t] if hasattr(closes_raw, "__getitem__") else closes_raw
                last = col.dropna() if hasattr(col, "dropna") else col
                ticker_price[t] = float(last.iloc[-1])
        except Exception:
            pass
        # Currency from info (single call per ticker — only for ones not cached)
        try:
            info = yf.Ticker(t).fast_info
            # fast_info has 'currency' in newer yfinance
            ccy = getattr(info, "currency", None)
            if ccy is None:
                ccy = yf.Ticker(t).info.get("currency", "USD")
            ticker_ccy[t] = ccy
        except Exception:
            ticker_ccy[t] = "USD"

    # Map back isin → USD price + display info
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
        else:
            price_usd = raw_price  # fallback
        prices[isin]  = price_usd
        display[isin] = (raw_price, yahoo_ccy)

    unmapped = [i for i in isins if i not in TICKER_MAP]
    if unmapped:
        print(f"  ⚠  No ticker for {len(unmapped)} ISINs (positions likely closed).",
              file=sys.stderr)

    return prices, display, fx


def run(broker=None):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    bf = f"AND t.broker = '{broker}'" if broker else ""
    rows = conn.execute(SQL.format(broker_filter=bf)).fetchall()

    # Build FIFO queues before closing connection
    fifo_queues, _ = build_queues(conn)
    conn.close()

    title = f"broker: {broker.upper()}" if broker else "all brokers"
    print(f"\n  Fetching live prices…", end=" ", flush=True)
    isins = [r["isin"] for r in rows]
    prices, display_map, fx = fetch_prices(isins)
    eurusd = fx["EURUSD"]
    print(f"done  (EUR/USD {eurusd:.4f})")

    # ── First pass: compute market values for portfolio total
    portfolio_usd = 0.0
    enriched = []
    for r in rows:
        isin     = r["isin"]
        qty      = r["net_qty"]
        db_ccy   = r["db_ccy"]

        price_usd     = prices.get(isin)
        price_native, yahoo_ccy = display_map.get(isin, (None, None))

        # FIFO avg cost for remaining lots (already in USD, historical FX)
        avg_cost_usd = fifo_queues[isin].avg_cost_usd() if isin in fifo_queues else None

        # Market value in USD
        mkt_val_usd = qty * price_usd if price_usd is not None else None
        if mkt_val_usd:
            portfolio_usd += mkt_val_usd

        enriched.append({
            "name":         r["security"],
            "db_ccy":       db_ccy,
            "yahoo_ccy":    yahoo_ccy,
            "qty":          qty,
            "avg_cost_usd": avg_cost_usd,  # USD, FIFO cost of remaining lots
            "price_native": price_native,
            "price_usd":    price_usd,
            "mkt_val_usd":  mkt_val_usd,
        })

    # ── Print
    W = 110
    print(f"\n{'='*W}")
    print(f"  Portfolio snapshot — {title}")
    print(f"  {__import__('datetime').date.today()}   "
          f"Total market value: ${portfolio_usd:>12,.2f} USD")
    print(f"{'='*W}")
    print(f"\n  {'Security':<36} {'Ccy':>4}  {'Qty':>8}  "
          f"{'AvgCost':>9}  {'Price':>9}  "
          f"{'Mkt Val $':>12}  {'Unreal P&L $':>13}  {'P&L %':>7}  {'Port %':>7}")
    print(f"  {'-'*(W-2)}")

    cur_ccy = None
    for d in enriched:
        if d["db_ccy"] != cur_ccy:
            cur_ccy = d["db_ccy"]
            label = f"{cur_ccy} instruments"
            print(f"\n  ── {label}")

        qty       = d["qty"]
        price_nat = d["price_native"]
        price_usd = d["price_usd"]
        mv        = d["mkt_val_usd"]
        yahoo_ccy = d["yahoo_ccy"] or d["db_ccy"]

        # Unrealized P&L — compute in USD so it's apples-to-apples
        if d["avg_cost_usd"] is not None and price_usd is not None:
            pnl_usd = (price_usd - d["avg_cost_usd"]) * qty
            pnl_pct = (price_usd - d["avg_cost_usd"]) / d["avg_cost_usd"] * 100
            pnl_str     = f"${pnl_usd:>+12,.0f}"
            pnl_pct_str = f"{pnl_pct:>+7.1f}%"
        else:
            pnl_str     = f"{'—':>13}"
            pnl_pct_str = f"{'—':>8}"

        port_pct  = f"{mv/portfolio_usd*100:>7.1f}%" if mv else f"{'—':>8}"
        mv_str    = f"${mv:>11,.2f}"  if mv is not None else f"{'—':>12}"
        avg_str   = f"${d['avg_cost_usd']:>8.2f}" if d["avg_cost_usd"] else f"{'—':>9}"
        # Show price in its Yahoo currency with label
        if price_nat is not None:
            pr_str = f"{price_nat:>7.2f} {yahoo_ccy}"
        else:
            pr_str = f"{'—':>9}    "

        print(f"  {d['name']:<36} {d['db_ccy']:>4}  {qty:>8.3f}  "
              f"{avg_str}  {pr_str}  "
              f"{mv_str}  {pnl_str}  {pnl_pct_str}  {port_pct}")

    print(f"\n  {'─'*(W-2)}")
    print(f"  {'TOTAL':<36}  {'':>4}  {'':>8}  {'':>9}  {'':>12}  "
          f"${portfolio_usd:>11,.2f}  {'':>13}  {'':>8}  {'100.0%':>7}")
    print(f"\n  Notes:")
    print(f"  • Avg cost in USD = FIFO weighted avg of remaining (unsold) lots, converted at historical FX (fx_rates table).")
    print(f"  • P&L computed in USD using historical cost basis — no live FX distortion.")
    print(f"  • Positions with no buy/vesting (transfer-in only) show '—' avg cost.\n")


if __name__ == "__main__":
    broker = sys.argv[1].lower() if len(sys.argv) > 1 else None
    run(broker)
