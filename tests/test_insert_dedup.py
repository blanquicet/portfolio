"""Tests for duplicate detection in insert.py."""
import sqlite3, sys, os, json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.insert import find_duplicate, insert_transaction


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
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
        INSERT INTO securities (isin, name, type, currency)
        VALUES ('US5949181045', 'Microsoft', 'stock', 'USD');
    """)
    conn.commit()
    return conn


def _sec_id(db):
    return db.execute("SELECT id FROM securities WHERE isin='US5949181045'").fetchone()[0]


def test_find_duplicate_returns_none_when_empty(db):
    data = {
        "security_id": _sec_id(db), "date": "2024-01-02", "type": "buy",
        "broker": "IBKR", "quantity": 10.0, "price": 400.0
    }
    assert find_duplicate(db, data) is None


def test_find_duplicate_returns_id_on_match(db):
    sec_id = _sec_id(db)
    db.execute(
        "INSERT INTO transactions (security_id,date,type,broker,quantity,price,currency) "
        "VALUES (?,?,?,?,?,?,?)",
        (sec_id, "2024-01-02", "buy", "IBKR", 10.0, 400.0, "USD")
    )
    db.commit()
    data = {"security_id": sec_id, "date": "2024-01-02", "type": "buy",
            "broker": "IBKR", "quantity": 10.0, "price": 400.0}
    result = find_duplicate(db, data)
    assert result is not None


def test_find_duplicate_different_quantity_no_match(db):
    sec_id = _sec_id(db)
    db.execute(
        "INSERT INTO transactions (security_id,date,type,broker,quantity,price,currency) "
        "VALUES (?,?,?,?,?,?,?)",
        (sec_id, "2024-01-02", "buy", "IBKR", 10.0, 400.0, "USD")
    )
    db.commit()
    data = {"security_id": sec_id, "date": "2024-01-02", "type": "buy",
            "broker": "IBKR", "quantity": 5.0, "price": 400.0}
    assert find_duplicate(db, data) is None
