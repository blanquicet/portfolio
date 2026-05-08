"""Tests for migrate.py — idempotency, DDL application, backfill."""
import sqlite3, sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.migrate import apply_ddl, backfill_ticker_mappings, verify_integrity


@pytest.fixture
def old_db():
    """Simulate a DB that predates ticker_mappings."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE securities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            isin TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            currency TEXT NOT NULL
        );
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            security_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            type TEXT NOT NULL,
            broker TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL,
            currency TEXT NOT NULL,
            total REAL,
            fee REAL DEFAULT 0,
            exchange TEXT,
            notes TEXT,
            source_file TEXT
        );
        CREATE TABLE fx_rates (
            date TEXT NOT NULL,
            from_currency TEXT NOT NULL,
            to_currency TEXT NOT NULL,
            rate REAL NOT NULL,
            PRIMARY KEY (date, from_currency, to_currency)
        );
    """)
    conn.commit()
    return conn


def test_apply_ddl_creates_ticker_mappings(old_db):
    apply_ddl(old_db)
    row = old_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ticker_mappings'"
    ).fetchone()
    assert row is not None


def test_apply_ddl_is_idempotent(old_db):
    apply_ddl(old_db)
    apply_ddl(old_db)  # second call should not raise
    row = old_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ticker_mappings'"
    ).fetchone()
    assert row is not None


SAMPLE_TICKER_MAP = {
    "IE00B4L5Y983": ("IWDA.L", "XLON", "USD"),
    "FR0000121014": ("MC.PA", "XPAR", "EUR"),
}


def test_backfill_inserts_entries(old_db):
    apply_ddl(old_db)
    backfill_ticker_mappings(old_db, SAMPLE_TICKER_MAP)
    count = old_db.execute("SELECT COUNT(*) FROM ticker_mappings").fetchone()[0]
    assert count == 2


def test_backfill_is_idempotent(old_db):
    apply_ddl(old_db)
    backfill_ticker_mappings(old_db, SAMPLE_TICKER_MAP)
    backfill_ticker_mappings(old_db, SAMPLE_TICKER_MAP)  # second run
    count = old_db.execute("SELECT COUNT(*) FROM ticker_mappings").fetchone()[0]
    assert count == 2  # no duplicates


def test_verify_integrity_passes_on_clean_db(old_db):
    apply_ddl(old_db)
    verify_integrity(old_db)  # should not raise
