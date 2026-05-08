"""Tests for resolve_ticker.py — DB cache, MIC normalization, missing exchange."""
import sqlite3, sys, os, json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# We import only the pure functions, not the CLI entry point
from tools.resolve_ticker import normalize_exchange, lookup_db, save_mapping

@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE ticker_mappings (
            isin TEXT NOT NULL,
            exchange TEXT NOT NULL,
            ticker TEXT NOT NULL,
            currency TEXT NOT NULL,
            source TEXT NOT NULL,
            verified_at TEXT,
            PRIMARY KEY (isin, exchange)
        )
    """)
    conn.commit()
    return conn

def test_normalize_exchange_lse():
    assert normalize_exchange("LSE") == "XLON"

def test_normalize_exchange_pa():
    assert normalize_exchange("PA") == "XPAR"

def test_normalize_exchange_already_mic():
    assert normalize_exchange("XNAS") == "XNAS"

def test_normalize_exchange_unknown_raises_systemexit():
    """Unknown exchanges must exit with code 2, not silently pass through."""
    import pytest
    with pytest.raises(SystemExit) as exc_info:
        normalize_exchange("BOGUS_EXCHANGE")
    assert exc_info.value.code == 2

def test_lookup_db_hit(db):
    db.execute(
        "INSERT INTO ticker_mappings VALUES (?,?,?,?,?,?)",
        ("IE00B4L5Y983", "XLON", "IWDA.L", "USD", "manual", "2024-01-01")
    )
    db.commit()
    result = lookup_db(db, "IE00B4L5Y983", "XLON")
    assert result == {"ticker": "IWDA.L", "currency": "USD", "source": "manual"}

def test_lookup_db_miss(db):
    result = lookup_db(db, "IE00B4L5Y983", "XLON")
    assert result is None

def test_lookup_db_wrong_exchange(db):
    db.execute(
        "INSERT INTO ticker_mappings VALUES (?,?,?,?,?,?)",
        ("IE00B4L5Y983", "XLON", "IWDA.L", "USD", "manual", "2024-01-01")
    )
    db.commit()
    result = lookup_db(db, "IE00B4L5Y983", "XPAR")
    assert result is None

def test_save_mapping(db):
    save_mapping(db, "IE00B4L5Y983", "XLON", "IWDA.L", "USD", "auto")
    result = lookup_db(db, "IE00B4L5Y983", "XLON")
    assert result is not None
    assert result["ticker"] == "IWDA.L"
    assert result["source"] == "auto"

def test_save_mapping_idempotent(db):
    save_mapping(db, "IE00B4L5Y983", "XLON", "IWDA.L", "USD", "auto")
    save_mapping(db, "IE00B4L5Y983", "XLON", "IWDA.L", "USD", "auto")  # no error
    rows = db.execute("SELECT COUNT(*) FROM ticker_mappings").fetchone()[0]
    assert rows == 1
