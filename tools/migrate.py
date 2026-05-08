#!/usr/bin/env python3
"""
Migrate portfolio.db to the current schema.

Responsibilities:
  1. Apply DDL for tables that may not exist (ticker_mappings, lot_assignments)
  2. Backfill ticker_mappings from TICKER_MAP in snapshot.py (run BEFORE deleting TICKER_MAP)
  3. Verify referential integrity post-migration
  4. Idempotent — safe to run multiple times

Usage:
    python3 tools/migrate.py

Release order (mantenedor del repo):
  1. python3 tools/migrate.py        ← run this first
  2. Verify ticker_mappings is populated
  3. Delete TICKER_MAP from snapshot.py
  4. git diff — confirm no ISINs remain in tracked code
  5. Push
"""
import sqlite3, sys, os, datetime

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

DDL_TICKER_MAPPINGS = """
CREATE TABLE IF NOT EXISTS ticker_mappings (
    isin         TEXT NOT NULL,
    exchange     TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    currency     TEXT NOT NULL,
    source       TEXT NOT NULL CHECK(source IN ('auto', 'manual')),
    verified_at  TEXT,
    PRIMARY KEY (isin, exchange)
);
"""

DDL_LOT_ASSIGNMENTS = """
CREATE TABLE IF NOT EXISTS lot_assignments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sell_id  INTEGER NOT NULL REFERENCES transactions(id),
    buy_id   INTEGER NOT NULL REFERENCES transactions(id),
    quantity REAL    NOT NULL CHECK(quantity > 0),
    UNIQUE(sell_id, buy_id)
);
"""

# MIC mapping for backfill — inferred from TICKER_MAP suffixes
_SUFFIX_TO_MIC = {
    ".L":  ("XLON", "GBP"),   # LSE tickers quote in GBp/GBP (e.g. IWDA.L)
    ".PA": ("XPAR", "EUR"),
}
_DEFAULT_MIC = ("XNAS", "USD")  # US tickers have no suffix → NASDAQ/NYSE default

# Historical TICKER_MAP — embedded here so migrate.py works independently of snapshot.py.
# This is the complete mapping at the time of the v1→v2 schema migration.
# New tickers added after migration will be resolved via resolve_ticker.py.
_HISTORICAL_TICKER_MAP = {
    # ── US stocks (USD) — XNAS default
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
    # ── US ETFs (USD) — XNAS default
    "US4642898427": "EPU",
    # ── LSE ETFs — USD share class (iShares USD-denominated on London Stock Exchange)
    "IE00B4L5Y983": "IWDA.L",
    "IE00B5BMR087": "CSPX.L",
    "IE00BKM4GZ66": "EIMI.L",
    "IE00B579F325": "SGLD.L",
    "IE00BGYWCB81": "VDEA.L",
    "IE00BF16M727": "CIBR.L",
    "IE00BYWZ0440": "IHYA.L",
    "IE00B43QJJ40": "GLAG.L",
    "LU0292109344": "XMBD.L",
    "LU1681045297": "ALAU.L",
    # ── Euronext Paris — EUR-quoted
    "FR0000121014": "MC.PA",
    "LU1563454310": "CLIM.PA",
    "LU1650489385": "MTE.PA",
    # ── WisdomTree Bitcoin EUR — Euronext Paris
    "GB00BJYDH287": "WBTC.PA",
}


def _infer_exchange_currency(ticker: str) -> tuple:
    """Infer MIC exchange and currency from Yahoo ticker suffix."""
    for suffix, (mic, ccy) in _SUFFIX_TO_MIC.items():
        if ticker.endswith(suffix):
            return mic, ccy
    return _DEFAULT_MIC


def apply_ddl(conn):
    """Apply missing DDL. Safe to call multiple times (IF NOT EXISTS)."""
    conn.executescript(DDL_TICKER_MAPPINGS + DDL_LOT_ASSIGNMENTS)
    conn.commit()


def backfill_ticker_mappings(conn, ticker_map: dict) -> int:
    """
    Insert entries from ticker_map into ticker_mappings if not already present.
    ticker_map format: {isin: (ticker, exchange_mic, currency)} OR {isin: ticker_str}
    Returns count of newly inserted rows.
    """
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    count = 0
    for isin, value in ticker_map.items():
        if isinstance(value, tuple):
            ticker, exchange, currency = value
        else:
            # Old format: value is just the ticker string
            ticker = value
            exchange, currency = _infer_exchange_currency(ticker)

        existing = conn.execute(
            "SELECT 1 FROM ticker_mappings WHERE isin = ? AND exchange = ?",
            (isin, exchange)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO ticker_mappings (isin, exchange, ticker, currency, source, verified_at) "
                "VALUES (?, ?, ?, ?, 'manual', ?)",
                (isin, exchange, ticker, currency, now)
            )
            count += 1

    conn.commit()
    return count


def verify_integrity(conn):
    """
    Basic post-migration checks. Raises AssertionError on failure.
    """
    orphans = conn.execute("""
        SELECT COUNT(*) FROM transactions t
        WHERE NOT EXISTS (SELECT 1 FROM securities s WHERE s.id = t.security_id)
    """).fetchone()[0]
    assert orphans == 0, f"Found {orphans} transactions with no matching security"


def main():
    if not os.path.exists(DB):
        print("ERROR: portfolio.db not found. Run /setup first.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB)

    print("1. Applying DDL…")
    apply_ddl(conn)
    print("   ✓ ticker_mappings and lot_assignments tables ensured")

    print("2. Backfilling ticker_mappings from historical map…")
    inserted = backfill_ticker_mappings(conn, _HISTORICAL_TICKER_MAP)
    total = conn.execute("SELECT COUNT(*) FROM ticker_mappings").fetchone()[0]
    if inserted > 0:
        print(f"   ✓ Inserted {inserted} new entries ({total} total in ticker_mappings)")
    else:
        print(f"   ✓ All {total} entries already present — skipping (idempotent)")

    print("3. Verifying integrity…")
    try:
        verify_integrity(conn)
        print("   ✓ Integrity checks passed")
    except AssertionError as e:
        print(f"   ✗ Integrity error: {e}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    conn.close()
    print("\n✅ Migration complete.")
    print("\nNext steps (if publishing the repo):")
    print("  1. Review ticker_mappings: python3 tools/insert.py query 'SELECT * FROM ticker_mappings'")
    print("  2. Delete TICKER_MAP from tools/snapshot.py")
    print("  3. Run: git diff — confirm no ISINs in tracked files")
    print("  4. Push")


if __name__ == "__main__":
    main()
