"""Tests para patrimonio.py — Task 1: cambios a fifo.py."""
import sys, os, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))
from fifo import FifoQueue, build_queues


def make_db_with_two_brokers():
    """DB en memoria con el mismo ISIN en dos brokers distintos."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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
        CREATE TABLE lot_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sell_id INTEGER NOT NULL,
            buy_id INTEGER NOT NULL,
            quantity REAL NOT NULL
        );
        CREATE TABLE fx_rates (
            date TEXT NOT NULL,
            from_currency TEXT NOT NULL,
            to_currency TEXT NOT NULL,
            rate REAL NOT NULL,
            PRIMARY KEY (date, from_currency, to_currency)
        );
        INSERT INTO securities VALUES (1, 'US0000000001', 'Accion Test', 'stock', 'USD');
        -- broker_a compra 10 unidades
        INSERT INTO transactions VALUES (1, 1, '2024-01-01', 'buy', 'broker_a', 10, 100.0, 'USD', 1000.0, 0, 'XNAS', NULL, NULL);
        -- broker_b compra 5 unidades del mismo ISIN
        INSERT INTO transactions VALUES (2, 1, '2024-03-01', 'buy', 'broker_b', 5, 120.0, 'USD', 600.0, 0, 'XNAS', NULL, NULL);
        -- fx para to_usd
        INSERT INTO fx_rates VALUES ('2024-01-01', 'USD', 'COP', 3900.0);
        INSERT INTO fx_rates VALUES ('2024-03-01', 'USD', 'COP', 4000.0);
    """)
    return conn


def test_build_queues_broker_filter_isolates_lots():
    """build_queues con broker= solo incluye lotes de ese broker."""
    conn = make_db_with_two_brokers()
    queues_a, _ = build_queues(conn, broker='broker_a')
    queues_b, _ = build_queues(conn, broker='broker_b')

    lots_a = queues_a['US0000000001'].remaining_lots()
    lots_b = queues_b['US0000000001'].remaining_lots()

    assert len(lots_a) == 1
    assert abs(lots_a[0][0] - 10.0) < 1e-6   # qty
    assert abs(lots_a[0][1] - 100.0) < 1e-6  # price_usd

    assert len(lots_b) == 1
    assert abs(lots_b[0][0] - 5.0) < 1e-6
    assert abs(lots_b[0][1] - 120.0) < 1e-6


def test_remaining_lots_with_buy_id_returns_five_fields():
    """remaining_lots_with_buy_id() devuelve (qty, price_usd, dt, src, buy_id)."""
    q = FifoQueue()
    q.add(qty=10, price_usd=100.0, dt="2024-01-01", source="test", buy_id=42)
    q.add(qty=5,  price_usd=200.0, dt="2024-06-01", source="test2", buy_id=99)

    lots = q.remaining_lots_with_buy_id()
    assert len(lots) == 2
    qty, price, dt, src, bid = lots[0]
    assert abs(qty - 10.0) < 1e-6
    assert abs(price - 100.0) < 1e-6
    assert dt == "2024-01-01"
    assert bid == 42

    # remaining_lots() original sigue devolviendo 4 campos
    lots_old = q.remaining_lots()
    assert len(lots_old[0]) == 4
