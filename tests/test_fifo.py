"""Unit tests for FifoQueue specific-lot consumption."""
import sys, os, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))
from fifo import FifoQueue


def make_queue():
    """Three lots: ids 10, 11, 12 with 10 units each."""
    q = FifoQueue()
    q.add(qty=10, price_usd=100.0, dt="2024-01-01", source="broker 2024-01-01 id=10", buy_id=10)
    q.add(qty=10, price_usd=200.0, dt="2024-06-01", source="broker 2024-06-01 id=11", buy_id=11)
    q.add(qty=10, price_usd=300.0, dt="2025-01-01", source="broker 2025-01-01 id=12", buy_id=12)
    return q


def test_consume_specific_full_lot():
    """Consume an entire lot by buy_id, skipping older ones."""
    q = make_queue()
    consumed = q.consume_specific([(12, 10.0)])
    assert len(consumed) == 1
    qty, price, buy_date, src = consumed[0]
    assert abs(qty - 10.0) < 1e-6
    assert abs(price - 300.0) < 1e-6
    assert buy_date == "2025-01-01"
    remaining = q.remaining_lots()
    assert len(remaining) == 2
    assert any(abs(p - 100.0) < 1e-6 for _, p, _, _ in remaining)
    assert any(abs(p - 200.0) < 1e-6 for _, p, _, _ in remaining)


def test_consume_specific_partial_lot():
    """Consume part of a lot by buy_id."""
    q = make_queue()
    consumed = q.consume_specific([(11, 4.0)])
    assert len(consumed) == 1
    qty, price, _, _ = consumed[0]
    assert abs(qty - 4.0) < 1e-6
    assert abs(price - 200.0) < 1e-6
    remaining = {src.split("id=")[1]: r_qty for r_qty, _, _, src in q.remaining_lots()}
    assert abs(float(remaining["11"]) - 6.0) < 1e-6


def test_consume_specific_multiple_lots():
    """Consume from multiple specific lots in one call."""
    q = make_queue()
    consumed = q.consume_specific([(10, 5.0), (12, 5.0)])
    assert len(consumed) == 2
    prices = {round(p) for _, p, _, _ in consumed}
    assert prices == {100, 300}
    remaining = q.remaining_lots()
    assert len(remaining) == 3
    remaining_by_price = {round(p): r_qty for r_qty, p, _, _ in remaining}
    assert abs(remaining_by_price[100] - 5.0) < 1e-6
    assert abs(remaining_by_price[200] - 10.0) < 1e-6
    assert abs(remaining_by_price[300] - 5.0) < 1e-6


def test_consume_specific_unknown_buy_id_raises():
    """Raises ValueError if buy_id does not exist in queue."""
    q = make_queue()
    try:
        q.consume_specific([(999, 5.0)])
        assert False, "should have raised"
    except ValueError as e:
        assert "999" in str(e)


def test_consume_specific_exceeds_lot_qty_raises():
    """Raises ValueError if requested qty exceeds lot's remaining qty."""
    q = make_queue()
    try:
        q.consume_specific([(10, 15.0)])
        assert False, "should have raised"
    except ValueError as e:
        assert "10" in str(e)


def test_fifo_unaffected_when_no_assignments():
    """Normal FIFO consume still works — no regression."""
    q = make_queue()
    consumed = q.consume(15.0)
    total_qty = sum(c[0] for c in consumed)
    assert abs(total_qty - 15.0) < 1e-6
    prices_consumed = [round(c[1]) for c in consumed]
    assert 100 in prices_consumed
    assert 200 in prices_consumed
    assert 300 not in prices_consumed


# ── Integration tests (build_queues with in-memory DB)

def make_test_db():
    """In-memory DB with two buys and one specific-lot sell."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE securities (
            id INTEGER PRIMARY KEY, isin TEXT, name TEXT, currency TEXT
        );
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, security_id INTEGER, date TEXT,
            type TEXT, broker TEXT, quantity REAL, currency TEXT,
            total REAL, fee REAL DEFAULT 0
        );
        CREATE TABLE fx_rates (
            date TEXT, from_currency TEXT, to_currency TEXT, rate REAL,
            PRIMARY KEY(date, from_currency, to_currency)
        );
        CREATE TABLE lot_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sell_id INTEGER, buy_id INTEGER, quantity REAL,
            UNIQUE(sell_id, buy_id)
        );
        INSERT INTO securities VALUES (1, 'US0000000001', 'Test Corp', 'USD');
        INSERT INTO transactions VALUES
            (1, 1, '2024-01-01', 'buy',  'ibkr', 10, 'USD', 1000, 0),
            (2, 1, '2024-06-01', 'buy',  'ibkr', 10, 'USD', 2000, 0),
            (3, 1, '2025-01-01', 'sell', 'ibkr', 10, 'USD', 1800, 0);
        INSERT INTO lot_assignments VALUES (1, 3, 2, 10.0);
    """)
    return conn


def test_build_queues_respects_specific_assignment():
    """build_queues uses consume_specific when lot_assignments exist."""
    from fifo import build_queues
    conn = make_test_db()
    queues, errors = build_queues(conn)
    conn.close()

    assert not errors
    lots = queues['US0000000001'].remaining_lots()
    assert len(lots) == 1
    qty, price, dt, _ = lots[0]
    assert abs(price - 100.0) < 1e-6, f"Expected $100 lot to remain, got {price}"
    assert dt == "2024-01-01"


def test_build_queues_fifo_when_no_assignment():
    """build_queues uses FIFO when no lot_assignments exist."""
    from fifo import build_queues
    conn = make_test_db()
    conn.execute("DELETE FROM lot_assignments")
    queues, errors = build_queues(conn)
    conn.close()

    assert not errors
    lots = queues['US0000000001'].remaining_lots()
    assert len(lots) == 1
    qty, price, dt, _ = lots[0]
    assert abs(price - 200.0) < 1e-6, f"Expected $200 lot to remain (FIFO), got {price}"
