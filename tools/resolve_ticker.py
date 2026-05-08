#!/usr/bin/env python3
"""Resolve ISIN + exchange → Yahoo Finance ticker.

Usage (called by skill):
    python3 tools/resolve_ticker.py <isin> <exchange>
        → prints: TICKER|CURRENCY|SOURCE
        → exit 0 on success

    python3 tools/resolve_ticker.py <isin>
        → exchange missing: exits 2, prints instructions for skill

Exit codes:
    0  resolved (from DB or Yahoo)
    1  ambiguous — printed numbered options, skill must re-call with chosen exchange
    2  missing exchange — skill must ask user which exchange before calling again
    3  unresolvable — Yahoo failed, no DB entry; skill must ask user directly
"""
import sqlite3, sys, os, datetime

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

MIC_ALIASES = {
    "LSE": "XLON",
    "PA": "XPAR", "XPAR": "XPAR",
    "NASDAQ": "XNAS",
    "NYSE": "XNYS",
    "XETRA": "XETR",
    "XLON": "XLON", "XNAS": "XNAS", "XNYS": "XNYS", "XETR": "XETR",
}


def normalize_exchange(raw: str) -> str:
    """Map broker abbreviation → ISO MIC. Unknown values are uppercased."""
    return MIC_ALIASES.get(raw.upper(), raw.upper())


def lookup_db(conn, isin: str, exchange: str):
    """Return ticker dict if (isin, exchange) exists in ticker_mappings, else None."""
    row = conn.execute(
        "SELECT ticker, currency, source FROM ticker_mappings WHERE isin = ? AND exchange = ?",
        (isin, exchange)
    ).fetchone()
    if row:
        return {"ticker": row[0], "currency": row[1], "source": row[2]}
    return None


def save_mapping(conn, isin: str, exchange: str, ticker: str, currency: str, source: str):
    """Upsert a row into ticker_mappings."""
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT OR REPLACE INTO ticker_mappings (isin, exchange, ticker, currency, source, verified_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (isin, exchange, ticker, currency, source, now)
    )
    conn.commit()


def yahoo_search(isin: str):
    """
    Attempt to find ticker(s) for an ISIN via Yahoo Finance unofficial search.
    Returns list of {ticker, currency, exchange_mic} dicts.
    Empty list if search fails or no results.

    NOTE: Uses undocumented Yahoo endpoint — may break. Failure is expected and handled.
    """
    try:
        import requests
        url = "https://query2.finance.yahoo.com/v1/finance/search"
        params = {"q": isin, "quotesCount": 10, "newsCount": 0}
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, params=params, headers=headers, timeout=8)
        r.raise_for_status()
        quotes = r.json().get("quotes", [])
        results = []
        for q in quotes:
            sym = q.get("symbol", "")
            ccy = q.get("currency", "USD")
            exch = q.get("exchange", "")
            mic = normalize_exchange(exch)
            results.append({"ticker": sym, "currency": ccy, "exchange_mic": mic, "exchange_raw": exch})
        return results
    except Exception:
        return []


def run(isin: str, exchange_raw):
    conn = sqlite3.connect(DB)

    if not exchange_raw:
        print("ERROR: exchange not provided.", file=sys.stderr)
        print("HINT: Ask the user which exchange this instrument trades on (e.g., LSE, NASDAQ, XETRA).", file=sys.stderr)
        conn.close()
        sys.exit(2)

    exchange = normalize_exchange(exchange_raw)

    # 1. DB cache hit
    cached = lookup_db(conn, isin, exchange)
    if cached:
        print(f"{cached['ticker']}|{cached['currency']}|{cached['source']}")
        conn.close()
        sys.exit(0)

    # 2. Yahoo search
    candidates = yahoo_search(isin)

    if not candidates:
        print(f"ERROR: Could not resolve {isin} via Yahoo Finance.", file=sys.stderr)
        print("HINT: Ask the user for the Yahoo Finance ticker directly.", file=sys.stderr)
        conn.close()
        sys.exit(3)

    # Filter by exchange if possible
    matching = [c for c in candidates if c["exchange_mic"] == exchange]

    if len(matching) == 1:
        c = matching[0]
        save_mapping(conn, isin, exchange, c["ticker"], c["currency"], "auto")
        print(f"{c['ticker']}|{c['currency']}|auto")
        conn.close()
        sys.exit(0)

    if len(matching) == 0:
        # No match for given exchange — show all candidates
        matching = candidates

    # Ambiguous: print options for skill to present to user
    print(f"AMBIGUOUS: {len(matching)} candidates for {isin}. User must choose:", file=sys.stderr)
    for i, c in enumerate(matching, 1):
        print(f"  {i}. {c['ticker']} ({c['exchange_raw']}/{c['exchange_mic']}) [{c['currency']}]", file=sys.stderr)
    print("HINT: Re-call with the MIC exchange the user selects.", file=sys.stderr)
    conn.close()
    sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: resolve_ticker.py <isin> [<exchange>]", file=sys.stderr)
        sys.exit(2)
    isin_arg = sys.argv[1]
    exch_arg = sys.argv[2] if len(sys.argv) > 2 else None
    run(isin_arg, exch_arg)
