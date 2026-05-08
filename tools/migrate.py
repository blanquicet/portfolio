#!/usr/bin/env python3
"""
Migrate portfolio.db to the current schema.

Responsibilities:
  1. Apply DDL for tables that may not exist (ticker_mappings, lot_assignments)
  2. Verify ticker_mappings is populated (tickers resolved via /ingest going forward)
  3. Verify referential integrity post-migration
  4. Idempotent — safe to run multiple times

Usage:
    python3 tools/migrate.py
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
    ".L":  ("XLON", "USD"),   # LSE iShares ETFs: USD share class (Yahoo confirms USD)
    ".PA": ("XPAR", "EUR"),
}
_DEFAULT_MIC = ("XNAS", "USD")  # US tickers have no suffix → NASDAQ/NYSE default


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

    print("2. Verifying ticker_mappings…")
    total = conn.execute("SELECT COUNT(*) FROM ticker_mappings").fetchone()[0]
    if total == 0:
        print(
            "   ⚠  ticker_mappings is empty. Tickers will be resolved on first ingest via /ingest.\n"
            "      Run /ingest with a broker PDF to start populating ticker_mappings automatically."
        )
    else:
        print(f"   ✓ {total} ticker mappings present")

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
    print("\nNext steps:")
    print("  1. Review ticker_mappings: python3 tools/insert.py query 'SELECT * FROM ticker_mappings'")
    print("  2. Run /ingest with a broker PDF to start populating tickers automatically.")


if __name__ == "__main__":
    main()
