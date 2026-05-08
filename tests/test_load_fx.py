"""Tests for load_fx.py — gap detection and fallback messaging."""
import sqlite3, sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.load_fx import find_gaps, format_trm_fallback_message


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE fx_rates (
            date TEXT NOT NULL,
            from_currency TEXT NOT NULL,
            to_currency TEXT NOT NULL,
            rate REAL NOT NULL,
            PRIMARY KEY (date, from_currency, to_currency)
        )
    """)
    conn.commit()
    return conn


def test_find_gaps_all_missing(db):
    gaps = find_gaps(db, ["2024-01-02", "2024-01-03"], "EUR", "USD")
    assert gaps == ["2024-01-02", "2024-01-03"]


def test_find_gaps_none_missing(db):
    db.execute("INSERT INTO fx_rates VALUES ('2024-01-02','EUR','USD',1.10)")
    db.execute("INSERT INTO fx_rates VALUES ('2024-01-03','EUR','USD',1.11)")
    db.commit()
    gaps = find_gaps(db, ["2024-01-02", "2024-01-03"], "EUR", "USD")
    assert gaps == []


def test_find_gaps_partial(db):
    db.execute("INSERT INTO fx_rates VALUES ('2024-01-02','EUR','USD',1.10)")
    db.commit()
    gaps = find_gaps(db, ["2024-01-02", "2024-01-03"], "EUR", "USD")
    assert gaps == ["2024-01-03"]


def test_find_gaps_empty_dates(db):
    gaps = find_gaps(db, [], "EUR", "USD")
    assert gaps == []


def test_trm_fallback_message_contains_url():
    msg = format_trm_fallback_message(["2024-01-02", "2024-01-03"])
    assert "suameca.banrep.gov.co" in msg
    assert "load_trm.py" in msg
    assert "2024-01-02" in msg
