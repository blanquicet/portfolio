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


def test_build_queues_no_broker_returns_all_lots():
    """build_queues sin broker= devuelve lotes de todos los brokers."""
    conn = make_db_with_two_brokers()
    queues_all, _ = build_queues(conn)
    lots_all = queues_all['US0000000001'].remaining_lots()
    assert len(lots_all) == 2  # broker_a (10 units) + broker_b (5 units)


# ── Tests Task 2: calc_lot_costs ──────────────────────────────────────────────

def test_calc_lot_costs_usd_sec():
    """Lote USD: cost_sec = price_usd * qty; cost_cop = cost_sec * TRM."""
    from patrimonio import calc_lot_costs
    result = calc_lot_costs(
        qty=10.0, price_usd=100.0, sec_ccy="USD",
        trm_compra=4000.0, eur_usd_compra=1.10
    )
    assert abs(result["cost_sec"] - 1000.0) < 0.01    # 10 * 100
    assert abs(result["cost_cop"] - 4_000_000.0) < 1  # 1000 * 4000


def test_calc_lot_costs_eur_sec():
    """Lote EUR: cost_sec = cost_usd / EUR_USD; cost_cop = cost_usd * TRM."""
    from patrimonio import calc_lot_costs
    result = calc_lot_costs(
        qty=10.0, price_usd=110.0, sec_ccy="EUR",
        trm_compra=4000.0, eur_usd_compra=1.10
    )
    assert abs(result["cost_sec"] - 1000.0) < 0.01    # (10*110) / 1.10
    assert abs(result["cost_cop"] - 4_400_000.0) < 1  # (10*110) * 4000


def test_calc_lot_costs_cop_sec():
    """Lote COP: cost_sec y cost_cop son iguales (price_usd * qty * TRM)."""
    from patrimonio import calc_lot_costs
    result = calc_lot_costs(
        qty=100.0, price_usd=0.25, sec_ccy="COP",  # price_usd = 1000 COP / 4000 TRM
        trm_compra=4000.0, eur_usd_compra=1.10
    )
    # cost_usd = 100 * 0.25 = 25; cost_cop = 25 * 4000 = 100_000
    assert abs(result["cost_sec"] - 100_000.0) < 1
    assert abs(result["cost_cop"] - 100_000.0) < 1


def test_calc_lot_costs_partial_lot():
    """Venta parcial: usa qty_remaining, no qty original."""
    from patrimonio import calc_lot_costs
    # Compró 10, vendió 4 → qty_remaining = 6
    result = calc_lot_costs(
        qty=6.0, price_usd=100.0, sec_ccy="USD",
        trm_compra=4000.0, eur_usd_compra=1.10
    )
    assert abs(result["cost_sec"] - 600.0) < 0.01
    assert abs(result["cost_cop"] - 2_400_000.0) < 1


def test_calc_lot_costs_missing_trm():
    """TRM None → cost_cop es None (no aborta)."""
    from patrimonio import calc_lot_costs
    result = calc_lot_costs(
        qty=10.0, price_usd=100.0, sec_ccy="USD",
        trm_compra=None, eur_usd_compra=1.10
    )
    assert result["cost_sec"] is not None
    assert result["cost_cop"] is None


def test_calc_lot_costs_missing_eur_usd():
    """EUR/USD None para sec_ccy=EUR → cost_sec es None."""
    from patrimonio import calc_lot_costs
    result = calc_lot_costs(
        qty=10.0, price_usd=110.0, sec_ccy="EUR",
        trm_compra=4000.0, eur_usd_compra=None
    )
    assert result["cost_sec"] is None
    assert result["cost_cop"] is not None   # cost_cop = cost_usd * TRM, no depende de EUR/USD


def test_calc_lot_costs_none_price_usd():
    """price_usd=None (FX de compra faltante) → ambos campos None, sin crash."""
    from patrimonio import calc_lot_costs
    result = calc_lot_costs(
        qty=10.0, price_usd=None, sec_ccy="USD",
        trm_compra=4000.0, eur_usd_compra=1.10
    )
    assert result["cost_sec"] is None
    assert result["cost_cop"] is None


# ── Tests Task 3: to_sec_ccy_price ───────────────────────────────────────────

def test_to_sec_ccy_usd_yahoo_usd_sec():
    """Yahoo USD → sec USD: directo."""
    from patrimonio import to_sec_ccy_price
    price = to_sec_ccy_price(100.0, "USD", "USD", eur_usd=1.10, trm=4000.0, gbp_usd=1.25)
    assert abs(price - 100.0) < 0.01


def test_to_sec_ccy_eur_yahoo_eur_sec():
    """Yahoo EUR → sec EUR: directo."""
    from patrimonio import to_sec_ccy_price
    price = to_sec_ccy_price(90.0, "EUR", "EUR", eur_usd=1.10, trm=4000.0, gbp_usd=1.25)
    assert abs(price - 90.0) < 0.01


def test_to_sec_ccy_gbp_yahoo_usd_sec():
    """Yahoo GBP → USD: × gbp_usd."""
    from patrimonio import to_sec_ccy_price
    price = to_sec_ccy_price(80.0, "GBP", "USD", eur_usd=1.10, trm=4000.0, gbp_usd=1.25)
    assert abs(price - 100.0) < 0.01   # 80 * 1.25


def test_to_sec_ccy_gbp_pence_usd_sec():
    """Yahoo GBp (peniques) → USD: ÷100 × gbp_usd."""
    from patrimonio import to_sec_ccy_price
    price = to_sec_ccy_price(8000.0, "GBp", "USD", eur_usd=1.10, trm=4000.0, gbp_usd=1.25)
    assert abs(price - 100.0) < 0.01   # 8000/100 * 1.25


def test_to_sec_ccy_usd_yahoo_cop_sec():
    """Yahoo USD → COP: × TRM."""
    from patrimonio import to_sec_ccy_price
    price = to_sec_ccy_price(100.0, "USD", "COP", eur_usd=1.10, trm=4000.0, gbp_usd=1.25)
    assert abs(price - 400_000.0) < 1


def test_to_sec_ccy_missing_gbp_usd():
    """gbp_usd=None para ticker GBP → devuelve None."""
    from patrimonio import to_sec_ccy_price
    price = to_sec_ccy_price(80.0, "GBP", "USD", eur_usd=1.10, trm=4000.0, gbp_usd=None)
    assert price is None


def test_to_sec_ccy_unknown_yahoo_ccy_treated_as_usd(capsys):
    """Unknown yahoo_ccy: warning to stderr, treated as USD."""
    from patrimonio import to_sec_ccy_price
    price = to_sec_ccy_price(100.0, "CHF", "USD", eur_usd=1.10, trm=4000.0, gbp_usd=1.25)
    assert abs(price - 100.0) < 0.01  # treated as USD
    captured = capsys.readouterr()
    assert "CHF" in captured.err or "no soportada" in captured.err


def test_to_sec_ccy_cop_sec_missing_trm():
    """sec_ccy=COP, trm=None → returns None."""
    from patrimonio import to_sec_ccy_price
    price = to_sec_ccy_price(100.0, "USD", "COP", eur_usd=1.10, trm=None, gbp_usd=1.25)
    assert price is None


def test_to_sec_ccy_eur_sec_missing_eur_usd():
    """Step 2 EUR→USD None: eur_usd=None in step 2 → None."""
    from patrimonio import to_sec_ccy_price
    # Yahoo price is USD, sec_ccy is EUR, but eur_usd is None → step 2 returns None
    price = to_sec_ccy_price(100.0, "USD", "EUR", eur_usd=None, trm=4000.0, gbp_usd=1.25)
    assert price is None


def test_to_sec_ccy_cop_yahoo_cop_sec():
    """Yahoo COP → sec COP: price in COP stays in COP (divide by TRM, then multiply by TRM = identity)."""
    from patrimonio import to_sec_ccy_price
    # Yahoo price 5000 COP, sec_ccy=COP, TRM=4000
    # Step 1: 5000 / 4000 = 1.25 USD
    # Step 2: 1.25 * 4000 = 5000 COP
    price = to_sec_ccy_price(5000.0, "COP", "COP", eur_usd=1.10, trm=4000.0, gbp_usd=1.25)
    assert abs(price - 5000.0) < 0.01


def test_to_sec_ccy_cop_yahoo_missing_trm():
    """Yahoo COP with trm=None → returns None."""
    from patrimonio import to_sec_ccy_price
    price = to_sec_ccy_price(5000.0, "COP", "COP", eur_usd=1.10, trm=None, gbp_usd=1.25)
    assert price is None


# ── Tests Task 4: collect_lots + run integration ──────────────────────────────

def make_full_db():
    """DB en memoria con 2 brokers, 2 monedas, un lote parcialmente vendido."""
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
        CREATE TABLE ticker_mappings (
            isin TEXT NOT NULL,
            exchange TEXT NOT NULL,
            ticker TEXT NOT NULL,
            currency TEXT NOT NULL,
            source TEXT NOT NULL,
            verified_at TEXT,
            PRIMARY KEY (isin, exchange)
        );
        CREATE TABLE fx_rates (
            date TEXT NOT NULL,
            from_currency TEXT NOT NULL,
            to_currency TEXT NOT NULL,
            rate REAL NOT NULL,
            PRIMARY KEY (date, from_currency, to_currency)
        );

        INSERT INTO securities VALUES (1, 'US0000000001', 'MSFT Test',   'stock', 'USD');
        INSERT INTO securities VALUES (2, 'IE00000000EU', 'LVMH Test',   'stock', 'EUR');
        INSERT INTO securities VALUES (3, 'COB00000001',  'BanCo Test',  'stock', 'COP');

        -- fidelity: MSFT USD — buy 10, sell 4 → 6 remaining
        INSERT INTO transactions VALUES (1, 1, '2024-01-15', 'buy',  'fidelity', 10, 300.0, 'USD', 3000.0, 0, 'XNAS', NULL, NULL);
        INSERT INTO transactions VALUES (2, 1, '2024-06-01', 'sell', 'fidelity',  4, 380.0, 'USD', 1520.0, 0, 'XNAS', NULL, NULL);

        -- scalable: LVMH EUR — buy 5 units
        INSERT INTO transactions VALUES (3, 2, '2024-02-01', 'buy', 'scalable', 5, 800.0, 'EUR', 4000.0, 0, 'XPAR', NULL, NULL);

        -- trii: BanCo COP — buy 100 units
        INSERT INTO transactions VALUES (4, 3, '2024-03-01', 'buy', 'trii', 100, 5000.0, 'COP', 500000.0, 0, 'XBOG', NULL, NULL);

        INSERT INTO ticker_mappings VALUES ('US0000000001', 'XNAS', 'MSFT', 'USD', 'manual', '2024-01-01');
        INSERT INTO ticker_mappings VALUES ('IE00000000EU', 'XPAR', 'MC.PA', 'EUR', 'manual', '2024-01-01');

        INSERT INTO fx_rates VALUES ('2024-01-15', 'USD', 'COP', 3900.0);
        INSERT INTO fx_rates VALUES ('2024-01-15', 'EUR', 'USD', 1.08);
        INSERT INTO fx_rates VALUES ('2024-02-01', 'USD', 'COP', 3950.0);
        INSERT INTO fx_rates VALUES ('2024-02-01', 'EUR', 'USD', 1.09);
        INSERT INTO fx_rates VALUES ('2024-03-01', 'USD', 'COP', 4000.0);
        INSERT INTO fx_rates VALUES ('2024-12-31', 'USD', 'COP', 4380.0);
        INSERT INTO fx_rates VALUES ('2024-12-31', 'EUR', 'USD', 1.10);
    """)
    return conn


def test_collect_lots_by_broker_and_secccy(monkeypatch):
    """run() agrupa lotes correctamente por broker → sec_ccy."""
    import patrimonio
    from datetime import date

    monkeypatch.setattr(
        patrimonio, "fetch_historical_prices",
        lambda tickers, as_of: {t: (400.0, "USD") for t in tickers}
    )

    conn = make_full_db()
    as_of = date(2024, 12, 31)

    groups = patrimonio.collect_lots(conn, as_of)

    # fidelity/USD: 1 lote (10 compradas, 4 vendidas → 6 restantes)
    assert ("fidelity", "USD") in groups
    fid_lots = groups[("fidelity", "USD")]
    assert len(fid_lots) == 1
    assert abs(fid_lots[0]["qty"] - 6.0) < 1e-6

    # scalable/EUR: 1 lote
    assert ("scalable", "EUR") in groups
    assert len(groups[("scalable", "EUR")]) == 1

    # trii/COP: 1 lote
    assert ("trii", "COP") in groups
    assert len(groups[("trii", "COP")]) == 1


def test_cost_usd_prorrateado(monkeypatch):
    """Lote parcial: costo = price_usd * qty_remaining (no total original)."""
    import patrimonio
    from datetime import date

    monkeypatch.setattr(
        patrimonio, "fetch_historical_prices",
        lambda tickers, as_of: {t: (400.0, "USD") for t in tickers}
    )

    conn = make_full_db()
    groups = patrimonio.collect_lots(conn, date(2024, 12, 31))

    lot = groups[("fidelity", "USD")][0]
    # price_usd = 3000/10 = 300; qty_remaining = 6; cost_usd = 300*6 = 1800
    assert abs(lot["cost_sec"] - 1800.0) < 0.01
    assert abs(lot["cost_cop"] - 1800.0 * 3900.0) < 1  # TRM del 2024-01-15
