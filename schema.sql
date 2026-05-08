CREATE TABLE IF NOT EXISTS securities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    isin TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('etf', 'stock', 'bond', 'cdt', 'crypto_etp', 'fund')),
    currency TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    security_id INTEGER NOT NULL REFERENCES securities(id),
    date TEXT NOT NULL,  -- ISO 8601 (YYYY-MM-DD)
    type TEXT NOT NULL CHECK(type IN ('buy', 'sell', 'transfer_in', 'transfer_out', 'dividend', 'fee', 'vesting', 'sell_to_cover', 'split', 'interest')),
    broker TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL,           -- per unit in original currency
    currency TEXT NOT NULL,
    total REAL,           -- total amount in original currency
    fee REAL DEFAULT 0,
    exchange TEXT,
    notes TEXT,
    source_file TEXT
);

CREATE TABLE IF NOT EXISTS fx_rates (
    date TEXT NOT NULL,
    from_currency TEXT NOT NULL,
    to_currency TEXT NOT NULL,
    rate REAL NOT NULL,
    PRIMARY KEY (date, from_currency, to_currency)
);

-- Useful views

CREATE VIEW IF NOT EXISTS v_transactions AS
SELECT
    t.id,
    s.isin,
    s.name AS security,
    t.date,
    t.type,
    t.broker,
    t.quantity,
    t.price,
    t.currency,
    t.total,
    t.fee,
    t.exchange,
    t.notes,
    t.source_file
FROM transactions t
JOIN securities s ON s.id = t.security_id
ORDER BY t.date;

CREATE VIEW IF NOT EXISTS v_positions AS
SELECT
    s.isin,
    s.name AS security,
    s.type,
    SUM(CASE
        WHEN t.type IN ('buy', 'transfer_in', 'vesting', 'split') THEN t.quantity
        WHEN t.type IN ('sell', 'sell_to_cover', 'transfer_out') THEN -t.quantity
        ELSE 0
    END) AS shares,
    t.broker
FROM transactions t
JOIN securities s ON s.id = t.security_id
GROUP BY s.isin, t.broker
HAVING shares > 0.0001;

-- Specific-lot assignments: override FIFO for a particular sell transaction.
-- When a sell_id has rows here, those exact buy lots are consumed (in row order)
-- instead of the oldest-first FIFO default.
-- Partial coverage is NOT supported: either all qty of the sell is covered by
-- assignments, or none are (falls back to FIFO).
CREATE TABLE IF NOT EXISTS lot_assignments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sell_id  INTEGER NOT NULL REFERENCES transactions(id),
    buy_id   INTEGER NOT NULL REFERENCES transactions(id),
    quantity REAL    NOT NULL CHECK(quantity > 0),
    UNIQUE(sell_id, buy_id)
);

CREATE TABLE IF NOT EXISTS ticker_mappings (
    -- No FK on isin: ticker_mappings may exist before a security is inserted (pre-resolution)
    isin         TEXT NOT NULL,
    exchange     TEXT NOT NULL,   -- ISO MIC: XLON, XPAR, XNAS, XNYS, XETR, etc.
    ticker       TEXT NOT NULL,
    currency     TEXT NOT NULL,   -- trading currency for this listing (may differ from securities.currency)
    source       TEXT NOT NULL CHECK(source IN ('auto', 'manual')),
    verified_at  TEXT,            -- ISO 8601
    PRIMARY KEY (isin, exchange)
);
