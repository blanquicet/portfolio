"""Test that snapshot.py reads tickers from DB, not from TICKER_MAP."""
import sqlite3, sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.snapshot import load_ticker_map_from_db


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE ticker_mappings (
            isin TEXT NOT NULL,
            exchange TEXT NOT NULL,
            ticker TEXT NOT NULL,
            currency TEXT NOT NULL,
            source TEXT NOT NULL,
            verified_at TEXT,
            PRIMARY KEY (isin, exchange)
        );
    """)
    conn.commit()
    return conn


def test_load_ticker_map_returns_isin_to_ticker(db):
    db.execute("INSERT INTO ticker_mappings VALUES ('IE00B4L5Y983','XLON','IWDA.L','USD','manual','2024-01-01')")
    db.execute("INSERT INTO ticker_mappings VALUES ('FR0000121014','XPAR','MC.PA','EUR','auto','2024-01-01')")
    db.commit()
    result = load_ticker_map_from_db(db)
    assert result["IE00B4L5Y983"] == "IWDA.L"
    assert result["FR0000121014"] == "MC.PA"


def test_load_ticker_map_empty_db(db):
    result = load_ticker_map_from_db(db)
    assert result == {}


def test_load_ticker_map_prefers_manual_over_auto(db):
    db.execute("INSERT INTO ticker_mappings VALUES ('IE00B4L5Y983','XNAS','IWDA_AUTO','USD','auto','2024-01-01')")
    db.execute("INSERT INTO ticker_mappings VALUES ('IE00B4L5Y983','XLON','IWDA.L','USD','manual','2024-01-01')")
    db.commit()
    result = load_ticker_map_from_db(db)
    # manual entry should win
    assert result["IE00B4L5Y983"] == "IWDA.L"


def test_no_ticker_map_in_snapshot_module():
    """TICKER_MAP must not exist in snapshot.py after refactor."""
    import importlib
    import tools.snapshot as snap_module
    assert not hasattr(snap_module, "TICKER_MAP"), \
        "TICKER_MAP still present in snapshot.py — delete it after running migrate.py"
