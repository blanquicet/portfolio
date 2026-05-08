# Ticker como identificador de securities — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reemplazar ISIN como identificador de securities por ticker de Yahoo Finance, eliminando `ticker_mappings`, `resolve_ticker.py`, y la indirección ISIN→ticker del snapshot.

**Architecture:** `securities.ticker` (UNIQUE NOT NULL) reemplaza `securities.isin` como clave natural. El agente extrae el ticker directamente del PDF/broker o pregunta al usuario si no está en el documento. `snapshot.py` y `fifo.py` usan ticker directamente — sin tabla de mapeos. `migrate.py` lleva DBs existentes al nuevo schema usando `ticker_mappings` como fuente de los tickers durante la migración, luego la tabla se elimina.

**Tech Stack:** Python 3.11+, SQLite 3, yfinance, pytest

---

## Mapa de archivos

| Archivo | Cambio |
|---------|--------|
| `schema.sql` | `securities`: reemplazar `isin TEXT UNIQUE` por `ticker TEXT UNIQUE NOT NULL`; eliminar tabla `ticker_mappings` |
| `tools/insert.py` | `upsert_security` y `insert_transaction` usan `ticker` en vez de `isin` |
| `tools/snapshot.py` | Eliminar `load_ticker_map_from_db`; SQL usa `s.ticker` directamente |
| `tools/fifo.py` | SQL interno usa `s.ticker`; `FifoQueue` keyed by ticker |
| `tools/tax_report.py` | SQL interno usa `s.ticker`; output muestra ticker |
| `tools/migrate.py` | Migra `isin` → `ticker` usando `ticker_mappings`; después limpia `ticker_mappings` |
| `tools/resolve_ticker.py` | **Eliminar** |
| `tools/load_fx.py` | Sin cambios (no referencia isin ni ticker_mappings) |
| `.claude/skills/ingest/SKILL.md` | Eliminar Steps 4 (resolve tickers); schema extraction pide ticker en vez de isin |
| `queries/snapshot.sql` | Usar `s.ticker` |
| `queries/snapshot_ibkr.sql` | Usar `s.ticker` |
| `tests/test_resolve_ticker.py` | **Eliminar** |
| `tests/test_snapshot_db_tickers.py` | Reescribir: `load_ticker_map_from_db` ya no existe; probar que snapshot usa `s.ticker` directamente |
| `tests/test_insert_dedup.py` | Actualizar fixtures: usar `ticker` en vez de `isin` |
| `tests/test_migrate.py` | Reescribir para el nuevo flujo de migración |
| `tests/test_fifo.py` | Actualizar fixtures si usan `isin` |

---

## Task 1: Actualizar schema.sql

**Files:**
- Modify: `schema.sql`

- [ ] **Step 1: Leer el schema actual**

```bash
cat schema.sql
```

- [ ] **Step 2: Reemplazar `isin` por `ticker` en `securities` y eliminar `ticker_mappings`**

El archivo completo debe quedar así (reemplazar contenido):

```sql
CREATE TABLE IF NOT EXISTS securities (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker   TEXT UNIQUE NOT NULL,   -- Yahoo Finance ticker (e.g. IWDA.L, AAPL, MC.PA)
    name     TEXT NOT NULL,
    type     TEXT NOT NULL CHECK(type IN ('etf', 'stock', 'bond', 'cdt', 'crypto_etp', 'fund')),
    currency TEXT NOT NULL           -- instrument denomination currency
);

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    security_id INTEGER NOT NULL REFERENCES securities(id),
    date        TEXT NOT NULL,  -- ISO 8601 (YYYY-MM-DD)
    type        TEXT NOT NULL CHECK(type IN ('buy', 'sell', 'transfer_in', 'transfer_out', 'dividend', 'fee', 'vesting', 'sell_to_cover', 'split', 'interest')),
    broker      TEXT NOT NULL,
    quantity    REAL NOT NULL,
    price       REAL,           -- per unit in original currency
    currency    TEXT NOT NULL,
    total       REAL,           -- total amount in original currency
    fee         REAL DEFAULT 0,
    exchange    TEXT,
    notes       TEXT,
    source_file TEXT
);

CREATE TABLE IF NOT EXISTS fx_rates (
    date          TEXT NOT NULL,
    from_currency TEXT NOT NULL,
    to_currency   TEXT NOT NULL,
    rate          REAL NOT NULL,
    PRIMARY KEY (date, from_currency, to_currency)
);

-- Useful views

CREATE VIEW IF NOT EXISTS v_transactions AS
SELECT
    t.id,
    s.ticker,
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
    s.ticker,
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
GROUP BY s.ticker, t.broker
HAVING shares > 0.0001;

-- Specific-lot assignments: override FIFO for a particular sell transaction.
CREATE TABLE IF NOT EXISTS lot_assignments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sell_id  INTEGER NOT NULL REFERENCES transactions(id),
    buy_id   INTEGER NOT NULL REFERENCES transactions(id),
    quantity REAL    NOT NULL CHECK(quantity > 0),
    UNIQUE(sell_id, buy_id)
);
```

- [ ] **Step 3: Commit**

```bash
git add schema.sql
git commit -m "schema: replace isin with ticker as security identifier; drop ticker_mappings"
```

---

## Task 2: Actualizar insert.py

**Files:**
- Modify: `tools/insert.py`
- Test: `tests/test_insert_dedup.py`

- [ ] **Step 1: Escribir test que falla — `upsert_security` con ticker**

En `tests/test_insert_dedup.py`, reemplazar el fixture y tests:

```python
"""Tests for duplicate detection in insert.py."""
import sqlite3, sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.insert import find_duplicate, insert_transaction, upsert_security


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE securities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT UNIQUE NOT NULL,
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
        INSERT INTO securities (ticker, name, type, currency)
        VALUES ('MSFT', 'Microsoft', 'stock', 'USD');
    """)
    conn.commit()
    return conn


def _sec_id(db):
    return db.execute("SELECT id FROM securities WHERE ticker='MSFT'").fetchone()[0]


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


def test_upsert_security_creates_new(db):
    sid = upsert_security({"ticker": "AAPL", "name": "Apple Inc", "type": "stock", "currency": "USD"}, db)
    assert isinstance(sid, int)
    row = db.execute("SELECT ticker FROM securities WHERE id=?", (sid,)).fetchone()
    assert row[0] == "AAPL"


def test_upsert_security_updates_existing(db):
    sid1 = upsert_security({"ticker": "MSFT", "name": "Microsoft OLD", "type": "stock", "currency": "USD"}, db)
    sid2 = upsert_security({"ticker": "MSFT", "name": "Microsoft Corp", "type": "stock", "currency": "USD"}, db)
    assert sid1 == sid2
    row = db.execute("SELECT name FROM securities WHERE ticker='MSFT'").fetchone()
    assert row[0] == "Microsoft Corp"
```

- [ ] **Step 2: Correr tests para verificar que fallan**

```bash
cd /path/to/portfolio  # repo root
pytest tests/test_insert_dedup.py -v
```

Expected: varios FAIL (columna `isin` no existe, `upsert_security` tiene firma incorrecta)

- [ ] **Step 3: Actualizar `tools/insert.py`**

Reemplazar el contenido completo:

```python
#!/usr/bin/env python3
"""Insert transactions into portfolio.db.

Usage (called by agent via skill):
    python3 tools/insert.py security '<json>'
    python3 tools/insert.py transaction '<json>'
    python3 tools/insert.py query '<sql>'
    python3 tools/insert.py transaction '<json>' --force
"""
import json, sqlite3, sys, os

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

def get_db():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn

def find_duplicate(conn, data: dict):
    """
    Return the transaction ID if a probable duplicate exists, else None.
    Duplicate key: (security_id, date, type, broker, quantity, price).
    """
    row = conn.execute(
        "SELECT id FROM transactions "
        "WHERE security_id = ? AND date = ? AND type = ? AND broker = ? "
        "AND ABS(quantity - ?) < 0.0001 AND ABS(COALESCE(price,0) - ?) < 0.0001",
        (
            data["security_id"], data["date"], data["type"], data["broker"],
            data["quantity"], data.get("price", 0) or 0
        )
    ).fetchone()
    return row[0] if row else None

def upsert_security(data: dict, conn=None) -> int:
    """Insert or update a security. Returns the security id.
    
    data keys: ticker, name, type, currency
    conn: optional existing connection (for testing); if None, opens DB file.
    """
    close_after = conn is None
    if conn is None:
        conn = get_db()
    cur = conn.execute(
        "INSERT INTO securities (ticker, name, type, currency) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(ticker) DO UPDATE SET name=excluded.name, type=excluded.type, currency=excluded.currency "
        "RETURNING id",
        (data["ticker"], data["name"], data["type"], data["currency"])
    )
    row = cur.fetchone()
    conn.commit()
    if close_after:
        conn.close()
    return row[0]

def insert_transaction(data: dict, force: bool = False) -> int:
    conn = get_db()
    # resolve security_id from ticker
    row = conn.execute("SELECT id FROM securities WHERE ticker = ?", (data["ticker"],)).fetchone()
    if not row:
        print(f"ERROR: security '{data['ticker']}' not found. Insert it first.", file=sys.stderr)
        sys.exit(1)
    sec_id = row[0]

    # duplicate check
    if not force:
        check_data = {**data, "security_id": sec_id}
        dup_id = find_duplicate(conn, check_data)
        if dup_id is not None:
            print(f"  ⚠  Probable duplicate of transaction id={dup_id} "
                  f"({data['date']} {data['type']} {data['quantity']} @ {data.get('price')}). "
                  f"Skipping. Pass --force to insert anyway.", file=sys.stderr)
            conn.close()
            sys.exit(2)

    cur = conn.execute(
        "INSERT INTO transactions (security_id, date, type, broker, quantity, price, currency, total, fee, exchange, notes, source_file) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (sec_id, data["date"], data["type"], data["broker"], data["quantity"],
         data.get("price"), data["currency"], data.get("total"),
         data.get("fee", 0), data.get("exchange"), data.get("notes"), data.get("source_file"))
    )
    tid = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return tid

def run_query(sql: str):
    conn = get_db()
    cur = conn.execute(sql)
    rows = cur.fetchall()
    if rows:
        cols = [d[0] for d in cur.description]
        print("\t".join(cols))
        for r in rows:
            print("\t".join(str(v) for v in r))
    else:
        print("(no rows)")
    conn.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["security", "transaction", "query"])
    parser.add_argument("arg", nargs="?")
    parser.add_argument("--force", action="store_true", help="Insert even if duplicate detected")
    args = parser.parse_args()

    if args.cmd == "security":
        sid = upsert_security(json.loads(args.arg))
        print(f"security_id={sid}")
    elif args.cmd == "transaction":
        data = json.loads(args.arg)
        tid = insert_transaction(data, force=args.force)
        print(f"transaction_id={tid}")
    elif args.cmd == "query":
        run_query(args.arg)
```

- [ ] **Step 4: Correr tests**

```bash
pytest tests/test_insert_dedup.py -v
```

Expected: todos PASS

- [ ] **Step 5: Commit**

```bash
git add tools/insert.py tests/test_insert_dedup.py
git commit -m "feat: insert.py uses ticker as security identifier"
```

---

## Task 3: Actualizar fifo.py

**Files:**
- Modify: `tools/fifo.py`
- Test: `tests/test_fifo.py`

- [ ] **Step 1: Leer test_fifo.py y verificar qué fixtures usan isin**

```bash
cat tests/test_fifo.py
```

Buscar cualquier uso de `isin` en fixtures de DB o en assertions del tipo `queues["IE00..."]`.

- [ ] **Step 2: Actualizar fixtures en test_fifo.py**

En `tests/test_fifo.py`, en cualquier fixture que cree la tabla `securities`, cambiar:
- `isin TEXT UNIQUE NOT NULL` → `ticker TEXT UNIQUE NOT NULL`
- INSERT con `isin='...'` → INSERT con `ticker='IWDA.L'` (o el ticker correspondiente)
- `queues["IE00B4L5Y983"]` → `queues["IWDA.L"]` en cualquier assertion

- [ ] **Step 3: Correr tests para verificar que fallan**

```bash
pytest tests/test_fifo.py -v
```

Expected: FAIL (columna `isin` no existe en DB de test)

- [ ] **Step 4: Actualizar SQL en fifo.py**

En `tools/fifo.py`, en la función `build_queues`, el SELECT principal hace `JOIN securities s`. Cambiar:

```python
    rows = conn.execute(f"""
        SELECT
            s.ticker, s.name, s.currency AS db_ccy,
            t.id, t.date, t.type, t.broker,
            t.quantity, t.currency AS t_ccy, t.total, t.fee
        FROM transactions t
        JOIN securities s ON s.id = t.security_id
        WHERE t.type IN ('buy','vesting','sell','sell_to_cover','transfer_in','transfer_out')
          {date_clause}
        ORDER BY t.date, t.id
    """, params).fetchall()
```

Y en el loop de procesamiento, cambiar `isin = r["isin"]` → `ticker = r["ticker"]` (y `r["isin"]` → `r["ticker"]` en todos los accesos), y `queues[isin]` → `queues[ticker]`.

El return sigue siendo `queues, errors` pero ahora keyed by ticker.

El código completo del loop actualizado:

```python
    queues = defaultdict(FifoQueue)
    errors = []

    for r in rows:
        ticker = r["ticker"]
        qty    = r["quantity"]
        dt     = r["date"]
        typ    = r["type"]
        ccy    = r["t_ccy"]
        total  = r["total"]
        tid    = r["id"]
        src    = f"{r['broker']} {r['date']} id={tid}"

        if typ in ("buy", "vesting"):
            price_usd = to_usd(conn, total / qty if total and qty else 0, ccy, dt)
            queues[ticker].add(qty, price_usd, dt, src, buy_id=tid)

        elif typ == "sell":
            if tid in assignments_by_sell:
                assigned_total = sum(q for _, q in assignments_by_sell[tid])
                if abs(assigned_total - qty) > 1e-6:
                    errors.append(
                        f"{r['name']} {dt}: lot assignments sum {assigned_total:.4f} "
                        f"!= sell qty {qty:.4f} — using FIFO instead"
                    )
                    try:
                        queues[ticker].consume(qty)
                    except ValueError as e:
                        errors.append(f"{r['name']} {dt}: {e}")
                else:
                    try:
                        queues[ticker].consume_specific(assignments_by_sell[tid])
                    except ValueError as e:
                        errors.append(f"{r['name']} {dt} (specific lot): {e}")
            else:
                try:
                    queues[ticker].consume(qty)
                except ValueError as e:
                    errors.append(f"{r['name']} {dt}: {e}")

        elif typ == "sell_to_cover":
            try:
                queues[ticker].consume(qty)
            except ValueError as e:
                errors.append(f"{r['name']} {dt} STC: {e}")

        # transfer_in / transfer_out: no queue change (FOP — basis preserved)

    return queues, errors
```

- [ ] **Step 5: Correr tests**

```bash
pytest tests/test_fifo.py -v
```

Expected: todos PASS

- [ ] **Step 6: Commit**

```bash
git add tools/fifo.py tests/test_fifo.py
git commit -m "feat: fifo.py uses ticker as security key instead of isin"
```

---

## Task 4: Actualizar snapshot.py y queries/

**Files:**
- Modify: `tools/snapshot.py`
- Modify: `queries/snapshot.sql`
- Modify: `queries/snapshot_ibkr.sql`
- Modify: `tests/test_snapshot_db_tickers.py`

- [ ] **Step 1: Escribir test que falla**

Reemplazar `tests/test_snapshot_db_tickers.py` completo:

```python
"""Test that snapshot.py uses ticker from securities directly (no ticker_mappings)."""
import sqlite3, sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.snapshot import fetch_prices


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE securities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT UNIQUE NOT NULL,
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
        INSERT INTO securities (ticker, name, type, currency)
        VALUES ('AAPL', 'Apple Inc', 'stock', 'USD');
    """)
    conn.commit()
    return conn


def test_fetch_prices_accepts_ticker_list():
    """fetch_prices takes a list of tickers, not ISINs."""
    # We don't hit Yahoo in unit tests — just verify the function accepts the right signature
    prices, display, fx = fetch_prices([])
    assert prices == {}
    assert display == {}
    assert "EURUSD" in fx


def test_no_load_ticker_map_in_snapshot_module():
    """load_ticker_map_from_db must NOT exist — snapshot reads ticker directly."""
    import tools.snapshot as snap
    assert not hasattr(snap, "load_ticker_map_from_db"), \
        "load_ticker_map_from_db still present — remove it"
    assert not hasattr(snap, "ticker_mappings"), \
        "ticker_mappings reference still present in snapshot"
```

- [ ] **Step 2: Correr tests para verificar que fallan**

```bash
pytest tests/test_snapshot_db_tickers.py -v
```

Expected: `test_no_load_ticker_map_in_snapshot_module` PASS (función ya existe pero el test verifica que NO exista — necesitamos eliminarla), `test_fetch_prices_accepts_ticker_list` probablemente FAIL por firma incorrecta.

- [ ] **Step 3: Reescribir snapshot.py**

Los cambios clave:
1. Eliminar `load_ticker_map_from_db` y toda referencia a `ticker_mappings`
2. El SQL principal usa `s.ticker` en vez de `s.isin`
3. `fetch_prices` acepta `tickers: list[str]` directamente (sin `ticker_map` dict)
4. Los dicts internos `prices`, `display_map` pasan a estar keyed by ticker (no isin)
5. `fifo_queues` ya viene keyed by ticker desde `build_queues`

Reemplazar el contenido completo de `tools/snapshot.py`:

```python
#!/usr/bin/env python3
"""
Portfolio snapshot — net positions with live prices, unrealized P&L, portfolio %.

Usage:
    python3 tools/snapshot.py            # all brokers combined
    python3 tools/snapshot.py ibkr       # IBKR only
    python3 tools/snapshot.py fidelity   # Fidelity only

Price source: Yahoo Finance (yfinance).
  - Currency per ticker is read from Yahoo's own fast_info.currency field.
  - USD prices → used directly.
  - EUR prices → × EURUSD.
  - All market values summed in USD for portfolio totals.
"""
import sqlite3, sys, os, warnings
warnings.filterwarnings("ignore")
import yfinance as yf
sys.path.insert(0, os.path.dirname(__file__))
from fifo import build_queues

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

SQL = """
SELECT
  s.ticker,
  s.name                                                            AS security,
  s.currency                                                        AS db_ccy,
  ROUND(SUM(
    CASE WHEN t.type IN ('buy','vesting','transfer_in') THEN  t.quantity
         WHEN t.type IN ('sell','sell_to_cover','transfer_out') THEN -t.quantity
         ELSE 0 END
  ), 4)                                                             AS net_qty
FROM transactions t
JOIN securities s ON s.id = t.security_id
WHERE t.date <= date('now')
  {broker_filter}
GROUP BY s.id, s.ticker, s.name, s.currency
HAVING net_qty > 0.001
ORDER BY s.currency DESC, s.name;
"""


def fetch_prices(tickers: list) -> tuple:
    """
    tickers: list of Yahoo Finance ticker strings (e.g. ['AAPL', 'IWDA.L', 'MC.PA'])
    Returns:
        prices  : {ticker: price_in_usd}
        display : {ticker: (price_native, yahoo_ccy)}
        fx      : {'EURUSD': float}
    """
    fx_data = yf.download(
        ["EURUSD=X"], period="2d", progress=False, auto_adjust=True
    )
    fx = {"EURUSD": 1.12}
    if not fx_data.empty:
        closes = fx_data["Close"]
        fx["EURUSD"] = float(closes["EURUSD=X"].dropna().iloc[-1])
    else:
        print("  ⚠  yfinance: could not fetch EUR/USD — using fallback 1.12 (may be stale)",
              file=sys.stderr)

    if not tickers:
        return {}, {}, fx

    raw = yf.download(tickers, period="2d", progress=False, auto_adjust=True)
    closes_raw = raw["Close"] if not raw.empty else None

    ticker_price = {}
    ticker_ccy   = {}
    for t in tickers:
        try:
            if closes_raw is not None:
                col = closes_raw[t] if hasattr(closes_raw, "__getitem__") else closes_raw
                last = col.dropna() if hasattr(col, "dropna") else col
                ticker_price[t] = float(last.iloc[-1])
        except Exception:
            pass
        try:
            info = yf.Ticker(t).fast_info
            ccy = getattr(info, "currency", None)
            if ccy is None:
                ccy = yf.Ticker(t).info.get("currency", "USD")
            ticker_ccy[t] = ccy
        except Exception:
            ticker_ccy[t] = "USD"

    prices  = {}
    display = {}
    for ticker in tickers:
        raw_price = ticker_price.get(ticker)
        if raw_price is None:
            continue
        yahoo_ccy = ticker_ccy.get(ticker, "USD")
        if yahoo_ccy == "USD":
            price_usd = raw_price
        elif yahoo_ccy == "EUR":
            price_usd = raw_price * fx["EURUSD"]
        else:
            print(f"  ⚠  {ticker}: unexpected currency '{yahoo_ccy}' — using raw price as USD (may be wrong)",
                  file=sys.stderr)
            price_usd = raw_price
        prices[ticker]  = price_usd
        display[ticker] = (raw_price, yahoo_ccy)

    return prices, display, fx


def run(broker=None):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    if broker:
        known = {r[0] for r in conn.execute("SELECT DISTINCT broker FROM transactions").fetchall()}
        if broker not in known:
            print(f"  ⚠  Unknown broker '{broker}'. Known: {sorted(known)}", file=sys.stderr)
            conn.close()
            sys.exit(1)

    if broker:
        rows = conn.execute(
            SQL.format(broker_filter="AND t.broker = ?"), (broker,)
        ).fetchall()
    else:
        rows = conn.execute(SQL.format(broker_filter="")).fetchall()

    fifo_queues, _ = build_queues(conn)
    conn.close()

    title = f"broker: {broker.upper()}" if broker else "all brokers"
    print(f"\n  Fetching live prices…", end=" ", flush=True)
    tickers = [r["ticker"] for r in rows]
    prices, display_map, fx = fetch_prices(tickers)
    eurusd = fx["EURUSD"]
    print(f"done  (EUR/USD {eurusd:.4f})")

    portfolio_usd = 0.0
    enriched = []
    for r in rows:
        ticker   = r["ticker"]
        qty      = r["net_qty"]
        db_ccy   = r["db_ccy"]

        price_usd     = prices.get(ticker)
        price_native, yahoo_ccy = display_map.get(ticker, (None, None))

        avg_cost_usd = fifo_queues[ticker].avg_cost_usd() if ticker in fifo_queues else None

        mkt_val_usd = qty * price_usd if price_usd is not None else None
        if mkt_val_usd:
            portfolio_usd += mkt_val_usd

        enriched.append({
            "name":         r["security"],
            "ticker":       ticker,
            "db_ccy":       db_ccy,
            "yahoo_ccy":    yahoo_ccy,
            "qty":          qty,
            "avg_cost_usd": avg_cost_usd,
            "price_native": price_native,
            "price_usd":    price_usd,
            "mkt_val_usd":  mkt_val_usd,
        })

    W = 110
    print(f"\n{'='*W}")
    print(f"  Portfolio snapshot — {title}")
    print(f"  {__import__('datetime').date.today()}   "
          f"Total market value: ${portfolio_usd:>12,.2f} USD")
    print(f"{'='*W}")
    print(f"\n  {'Security':<30} {'Ticker':<10} {'Ccy':>4}  {'Qty':>8}  "
          f"{'AvgCost':>9}  {'Price':>9}  "
          f"{'Mkt Val $':>12}  {'Unreal P&L $':>13}  {'P&L %':>7}  {'Port %':>7}")
    print(f"  {'-'*(W-2)}")

    cur_ccy = None
    for d in enriched:
        if d["db_ccy"] != cur_ccy:
            cur_ccy = d["db_ccy"]
            print(f"\n  ── {cur_ccy} instruments")

        qty       = d["qty"]
        price_nat = d["price_native"]
        price_usd = d["price_usd"]
        mv        = d["mkt_val_usd"]
        yahoo_ccy = d["yahoo_ccy"] or d["db_ccy"]

        if d["avg_cost_usd"] is not None and price_usd is not None:
            pnl_usd = (price_usd - d["avg_cost_usd"]) * qty
            pnl_pct = (price_usd - d["avg_cost_usd"]) / d["avg_cost_usd"] * 100
            pnl_str     = f"${pnl_usd:>+12,.0f}"
            pnl_pct_str = f"{pnl_pct:>+7.1f}%"
        else:
            pnl_str     = f"{'—':>13}"
            pnl_pct_str = f"{'—':>8}"

        port_pct  = f"{mv/portfolio_usd*100:>7.1f}%" if mv else f"{'—':>8}"
        mv_str    = f"${mv:>11,.2f}"  if mv is not None else f"{'—':>12}"
        avg_str   = f"${d['avg_cost_usd']:>8.2f}" if d["avg_cost_usd"] else f"{'—':>9}"
        if price_nat is not None:
            pr_str = f"{price_nat:>7.2f} {yahoo_ccy}"
        else:
            pr_str = f"{'—':>9}    "

        print(f"  {d['name']:<30} {d['ticker']:<10} {d['db_ccy']:>4}  {qty:>8.3f}  "
              f"{avg_str}  {pr_str}  "
              f"{mv_str}  {pnl_str}  {pnl_pct_str}  {port_pct}")

    print(f"\n  {'─'*(W-2)}")
    print(f"  {'TOTAL':<36}  {'':>4}  {'':>8}  {'':>9}  {'':>12}  "
          f"${portfolio_usd:>11,.2f}  {'':>13}  {'':>8}  {'100.0%':>7}")
    print(f"\n  Notes:")
    print(f"  • Avg cost in USD = FIFO weighted avg of remaining lots, converted at historical FX.")
    print(f"  • P&L computed in USD using historical cost basis — no live FX distortion.")
    print(f"  • Positions with no buy/vesting (transfer-in only) show '—' avg cost.\n")


if __name__ == "__main__":
    broker = sys.argv[1].lower() if len(sys.argv) > 1 else None
    run(broker)
```

- [ ] **Step 4: Actualizar queries/snapshot.sql y queries/snapshot_ibkr.sql**

`queries/snapshot.sql` — reemplazar `s.isin` por `s.ticker`:

```sql
-- Portfolio snapshot: current net positions per broker
-- Run: python3 tools/snapshot.py
-- or:  sqlite3 portfolio.db "$(cat queries/snapshot.sql)"

SELECT
  s.ticker,
  s.name                                          AS security,
  s.currency                                      AS native_ccy,
  ROUND(SUM(
    CASE WHEN t.type IN ('buy','vesting','transfer_in') THEN  t.quantity
         WHEN t.type IN ('sell','sell_to_cover','transfer_out') THEN -t.quantity
         ELSE 0 END
  ), 4)                                            AS net_qty,
  ROUND(
    SUM(CASE WHEN t.type IN ('buy','vesting') THEN t.total ELSE 0 END) /
    NULLIF(SUM(CASE WHEN t.type IN ('buy','vesting') THEN t.quantity ELSE 0 END), 0)
  , 4)                                             AS avg_cost,
  MAX(t.broker)                                    AS primary_broker
FROM transactions t
JOIN securities s ON s.id = t.security_id
WHERE t.date <= date('now')
GROUP BY s.id, s.ticker, s.name, s.currency
HAVING net_qty > 0.001
ORDER BY s.name;
```

`queries/snapshot_ibkr.sql` — mismo cambio, con filtro de broker.

- [ ] **Step 5: Correr tests**

```bash
pytest tests/test_snapshot_db_tickers.py -v
```

Expected: todos PASS

- [ ] **Step 6: Commit**

```bash
git add tools/snapshot.py queries/snapshot.sql queries/snapshot_ibkr.sql tests/test_snapshot_db_tickers.py
git commit -m "feat: snapshot.py uses ticker from securities directly — no ticker_mappings"
```

---

## Task 5: Actualizar tax_report.py

**Files:**
- Modify: `tools/tax_report.py`

No hay tests de tax_report; verificar manualmente con `--help` o con DB vacía.

- [ ] **Step 1: Actualizar SQL interno en tax_report.py**

En `tools/tax_report.py`, el SELECT principal dentro de `run()` hace JOIN con `securities`. Cambiar:
- `s.isin` → `s.ticker` en el SELECT
- `isin = r["isin"]` → `ticker = r["ticker"]` en el loop
- `queues[isin]` → `queues[ticker]` en todos los accesos
- Los dicts de resultados con clave `"isin"` → `"ticker"`
- Output lines que imprimen `isin` → imprimir `ticker`

Leer el archivo completo primero con `cat tools/tax_report.py` para ver todas las ocurrencias, luego hacer los reemplazos. Las líneas clave son:

```python
# En el SELECT (~línea 89):
            s.ticker, s.name, s.currency AS db_ccy,

# En el loop (~línea 104):
        ticker = r["ticker"]

# Todos los queues[isin] → queues[ticker]
# Todos los "isin": isin → "ticker": ticker en dicts de resultados
```

- [ ] **Step 2: Verificar que el script arranca sin crash**

```bash
python3 tools/tax_report.py 2024
```

Expected: imprime "No hay ventas..." o tabla de resultados. No debe crashear con `KeyError` o `OperationalError`.

- [ ] **Step 3: Commit**

```bash
git add tools/tax_report.py
git commit -m "feat: tax_report.py uses ticker as security key"
```

---

## Task 6: Actualizar migrate.py y sus tests

**Files:**
- Modify: `tools/migrate.py`
- Modify: `tests/test_migrate.py`

La migración de una DB existente (que tiene columna `isin`) debe:
1. Añadir columna `ticker` a `securities` (si no existe)
2. Intentar poblar `ticker` desde `ticker_mappings` (si la tabla existe)
3. Para rows donde `ticker` sigue vacío: imprimir advertencia (el usuario deberá actualizar manualmente o re-ingestar)
4. Hacer el columna `isin` opcional (no podemos hacer DROP COLUMN en SQLite sin recrear la tabla — por ahora dejamos `isin` como columna extra, no la borramos)
5. Limpiar la tabla `ticker_mappings` (ya no se necesita)
6. Verificar integridad

**Nota sobre SQLite:** SQLite no soporta `DROP COLUMN` antes de la versión 3.35.0. Para no complicar la migración, dejamos la columna `isin` en la tabla pero hacemos `ticker` el identificador real. En una futura limpieza se puede recrear la tabla.

- [ ] **Step 1: Escribir test que falla**

Reemplazar `tests/test_migrate.py` completo:

```python
"""Tests for migrate.py — idempotency, DDL, ticker migration from ticker_mappings."""
import sqlite3, sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.migrate import apply_ddl, verify_integrity


@pytest.fixture
def old_db():
    """Simulate a DB that predates the ticker refactor — has isin, no ticker column."""
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
        CREATE TABLE ticker_mappings (
            isin TEXT NOT NULL,
            exchange TEXT NOT NULL,
            ticker TEXT NOT NULL,
            currency TEXT NOT NULL,
            source TEXT NOT NULL,
            verified_at TEXT,
            PRIMARY KEY (isin, exchange)
        );
        INSERT INTO securities (isin, name, type, currency)
        VALUES ('IE00B4L5Y983', 'iShares MSCI World', 'etf', 'USD');
        INSERT INTO ticker_mappings (isin, exchange, ticker, currency, source)
        VALUES ('IE00B4L5Y983', 'XLON', 'IWDA.L', 'USD', 'manual');
    """)
    conn.commit()
    return conn


@pytest.fixture
def new_db():
    """Simulate a fresh DB already on the new schema (ticker column, no ticker_mappings)."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE securities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT UNIQUE NOT NULL,
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
        CREATE TABLE lot_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sell_id INTEGER NOT NULL,
            buy_id INTEGER NOT NULL,
            quantity REAL NOT NULL CHECK(quantity > 0),
            UNIQUE(sell_id, buy_id)
        );
    """)
    conn.commit()
    return conn


def test_apply_ddl_adds_ticker_column_to_old_db(old_db):
    apply_ddl(old_db)
    cols = [row[1] for row in old_db.execute("PRAGMA table_info(securities)").fetchall()]
    assert "ticker" in cols


def test_apply_ddl_populates_ticker_from_ticker_mappings(old_db):
    apply_ddl(old_db)
    row = old_db.execute("SELECT ticker FROM securities WHERE isin='IE00B4L5Y983'").fetchone()
    assert row is not None
    assert row[0] == "IWDA.L"


def test_apply_ddl_is_idempotent_on_new_db(new_db):
    apply_ddl(new_db)  # should not raise
    cols = [row[1] for row in new_db.execute("PRAGMA table_info(securities)").fetchall()]
    assert "ticker" in cols


def test_apply_ddl_creates_lot_assignments_if_missing(old_db):
    apply_ddl(old_db)
    row = old_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='lot_assignments'"
    ).fetchone()
    assert row is not None


def test_verify_integrity_passes(old_db):
    apply_ddl(old_db)
    verify_integrity(old_db)  # should not raise
```

- [ ] **Step 2: Correr tests para verificar que fallan**

```bash
pytest tests/test_migrate.py -v
```

Expected: FAIL (`apply_ddl` no tiene lógica de ticker column todavía)

- [ ] **Step 3: Reescribir tools/migrate.py**

```python
#!/usr/bin/env python3
"""
Migrate portfolio.db to the current schema.

Responsibilities:
  1. Añadir columna ticker a securities (si no existe)
  2. Poblar ticker desde ticker_mappings (si la tabla existe y tiene datos)
  3. Crear lot_assignments si no existe
  4. Verificar integridad post-migración
  5. Idempotente — seguro correr múltiples veces

Usage:
    python3 tools/migrate.py
"""
import sqlite3, sys, os

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

DDL_LOT_ASSIGNMENTS = """
CREATE TABLE IF NOT EXISTS lot_assignments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sell_id  INTEGER NOT NULL REFERENCES transactions(id),
    buy_id   INTEGER NOT NULL REFERENCES transactions(id),
    quantity REAL    NOT NULL CHECK(quantity > 0),
    UNIQUE(sell_id, buy_id)
);
"""


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _column_exists(conn, table: str, column: str) -> bool:
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def apply_ddl(conn):
    """Apply all schema migrations. Idempotent."""
    # 1. Add ticker column to securities if missing
    if not _column_exists(conn, "securities", "ticker"):
        print("   → Adding ticker column to securities…")
        conn.execute("ALTER TABLE securities ADD COLUMN ticker TEXT")
        conn.commit()

    # 2. Populate ticker from ticker_mappings (prefer manual over auto)
    if _table_exists(conn, "ticker_mappings"):
        print("   → Populating ticker from ticker_mappings…")
        # For each security with isin, find the best ticker mapping
        conn.execute("""
            UPDATE securities
            SET ticker = (
                SELECT tm.ticker
                FROM ticker_mappings tm
                WHERE tm.isin = securities.isin
                ORDER BY CASE tm.source WHEN 'manual' THEN 0 ELSE 1 END
                LIMIT 1
            )
            WHERE ticker IS NULL AND isin IS NOT NULL
        """)
        conn.commit()

    # 3. Report securities still without ticker
    if _column_exists(conn, "securities", "isin"):
        missing = conn.execute(
            "SELECT isin, name FROM securities WHERE ticker IS NULL OR ticker = ''"
        ).fetchall()
        if missing:
            print(f"   ⚠  {len(missing)} securities without ticker — re-ingest to resolve:")
            for row in missing:
                print(f"      {row[0]}  {row[1]}")
        else:
            count = conn.execute("SELECT COUNT(*) FROM securities").fetchone()[0]
            if count > 0:
                print(f"   ✓ All {count} securities have a ticker")

    # 4. Create lot_assignments if missing
    conn.execute(DDL_LOT_ASSIGNMENTS)
    conn.commit()
    print("   ✓ lot_assignments table OK")


def verify_integrity(conn):
    """Basic referential integrity check. Raises AssertionError on failure."""
    orphans = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE security_id NOT IN (SELECT id FROM securities)"
    ).fetchone()[0]
    assert orphans == 0, f"Found {orphans} transactions with no matching security"
    print("   ✓ Referential integrity OK")


def main():
    if not os.path.exists(DB):
        print("portfolio.db not found — nothing to migrate. Run /ingest to create it.")
        sys.exit(0)

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    print("\n── Portfolio DB Migration ──")
    print("Step 1: Applying DDL…")
    apply_ddl(conn)
    print("Step 2: Verifying integrity…")
    verify_integrity(conn)
    conn.close()
    print("\n✓ Migration complete.\n")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Correr tests**

```bash
pytest tests/test_migrate.py -v
```

Expected: todos PASS

- [ ] **Step 5: Commit**

```bash
git add tools/migrate.py tests/test_migrate.py
git commit -m "feat: migrate.py adds ticker column and populates from ticker_mappings"
```

---

## Task 7: Eliminar resolve_ticker.py y actualizar skill de ingest

**Files:**
- Delete: `tools/resolve_ticker.py`
- Delete: `tests/test_resolve_ticker.py`
- Modify: `.claude/skills/ingest/SKILL.md`

- [ ] **Step 1: Eliminar archivos**

```bash
git rm tools/resolve_ticker.py tests/test_resolve_ticker.py
```

- [ ] **Step 2: Verificar que los tests restantes pasan**

```bash
pytest tests/ -v --ignore=tests/test_resolve_ticker.py
```

Expected: todos PASS (test_resolve_ticker ya no existe)

- [ ] **Step 3: Reescribir `.claude/skills/ingest/SKILL.md`**

Cambios clave:
- En Step 1 (Extract), el schema pide `ticker` en vez de `isin` — y agrega nota de que el ticker es el de Yahoo Finance
- Eliminar Step 4 (Resolve tickers) completamente
- En Step 3 (Insert securities), usar `ticker` en el JSON
- En Step 6 (Insert transactions), usar `ticker` en el JSON
- Renumerar pasos (quedan 6 en vez de 7)

```markdown
---
name: ingest
description: "Ingestar transacciones de un broker — úsame cuando el usuario quiera agregar acciones, importar un extracto, o subir un PDF/screenshot de su broker."
---

# Portfolio Ingest

> **Python:** Usa el binario más reciente disponible: `python3.13`, `python3.12`, `python3.11`, o `python3` si ya es ≥3.11. Substitúyelo donde veas `python3` en los comandos.

The user has provided a PDF, screenshot, or described transactions from their broker.

## Precondition — verificar DB

Antes de extraer transacciones, verifica que la DB esté lista:

```bash
ls portfolio.db 2>/dev/null && echo "EXISTS" || echo "NEW"
```

- Si dice `NEW`: crea la DB primero, luego continúa con Step 1:
  ```bash
  sqlite3 portfolio.db < schema.sql && echo "DB created OK"
  ```
- Si dice `EXISTS`: continúa directamente con Step 1.

## Step 1 — Extract transactions

Read the document carefully. Extract ALL transactions using this exact schema:

**Per security (insert once per unique ticker):**
- `ticker` — Yahoo Finance ticker (e.g. AAPL, IWDA.L, MC.PA). If not obvious from the document, ask the user: "¿Cuál es el ticker de Yahoo Finance para [nombre del instrumento]?" They can look it up at finance.yahoo.com.
- `name` — full security name
- `type` — one of: `etf`, `stock`, `bond`, `cdt`, `crypto_etp`, `fund`
- `security_currency` — currency the instrument is denominated in (e.g. USD for IWDA.L even if bought via EUR account)

**Per transaction:**
- `date` — ISO 8601 (YYYY-MM-DD)
- `tx_type` — one of: `buy`, `sell`, `dividend`, `fee`, `transfer_in`, `transfer_out`, `vesting`, `sell_to_cover`, `split`, `interest`
- `broker` — broker name (e.g. `ibkr`, `scalable`, `fidelity`)
- `quantity` — number of shares/units (always positive)
- `price` — price per unit in `tx_currency`
- `tx_currency` — currency of the transaction (may differ from `security_currency`)
- `total` — total transaction value in `tx_currency`
- `fee` — commission/fee in `tx_currency` (0 if none)
- `exchange` — exchange where traded (e.g. LSE, NASDAQ, XETRA) — use broker's label
- `notes` — any relevant note (optional)
- `source_file` — filename of the document provided

Present the extracted data as a structured list for user review before inserting.

## Step 2 — User confirms extraction

Show the extracted transactions. Ask: "Does this look right? I'll proceed to insert."

## Step 3 — Insert securities

For each unique ticker:
```bash
python3 tools/insert.py security '{"ticker":"<ticker>","name":"<name>","type":"<type>","currency":"<security_currency>"}'
```

## Step 4 — Load FX rates

Collect all unique transaction dates and currency pairs needed (any non-USD currency involved):
```bash
python3 tools/load_fx.py --dates <date1>,<date2>,... --pairs EUR/USD,USD/COP,GBP/USD
```
Only include pairs actually needed for the transactions being ingested.

If the script prints a manual fallback message, show it to the user and wait for them to perform the manual step before continuing.

## Step 5 — Insert transactions

For each transaction:
```bash
python3 tools/insert.py transaction '{"ticker":"<ticker>","date":"<date>","type":"<tx_type>","broker":"<broker>","quantity":<qty>,"price":<price>,"currency":"<tx_currency>","total":<total>,"fee":<fee>,"exchange":"<exchange>","notes":"<notes>","source_file":"<source_file>"}'
```

**If exit code 2 (duplicate detected):** Show the user the duplicate warning from stderr. Ask: "This looks like a duplicate — insert anyway? (yes/no)". If yes, re-run the same command with `--force` appended. If no, skip.

## Step 6 — Summary

Report: "X transactions inserted, Y securities created/updated, W already existed (duplicates skipped)."
```

- [ ] **Step 4: Correr todos los tests**

```bash
pytest tests/ -v
```

Expected: todos PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: remove resolve_ticker.py — ticker is now the primary security identifier"
```

---

## Task 8: Migrar la DB personal y hacer push

Este task lo ejecuta el mantenedor del repo (Jose), no un subagente.

- [ ] **Step 1: Correr la migración en la DB real**

```bash
python3 tools/migrate.py
```

Verificar que todas las securities tienen ticker asignado. Si alguna muestra `⚠ sin ticker`, actualizar manualmente:
```bash
python3 tools/insert.py query "UPDATE securities SET ticker='<ticker>' WHERE isin='<isin>'"
```

- [ ] **Step 2: Verificar que snapshot funciona**

```bash
python3 tools/snapshot.py
```

Expected: mismas posiciones que antes, con columna `Ticker` visible.

- [ ] **Step 3: Correr todos los tests**

```bash
pytest tests/ -v
```

Expected: todos PASS

- [ ] **Step 4: Push**

```bash
git push
```

---

## Self-Review

### Spec coverage

| Requisito | Task |
|-----------|------|
| `ticker` como PK natural en `securities` | Task 1 (schema) |
| `insert.py` usa ticker | Task 2 |
| `fifo.py` keyed by ticker | Task 3 |
| `snapshot.py` sin ticker_mappings | Task 4 |
| `tax_report.py` usa ticker | Task 5 |
| `migrate.py` migra isin→ticker | Task 6 |
| `resolve_ticker.py` eliminado | Task 7 |
| `ingest` skill sin Step 4 | Task 7 |
| DB real migrada y probada | Task 8 |

### Notas de diseño

- `isin` queda como columna extra en `securities` post-migración (SQLite no soporta DROP COLUMN < 3.35). No rompe nada — se puede limpiar en el futuro.
- `ticker_mappings` queda en la DB tras `migrate.py` — el mantenedor puede borrarla manualmente con `DROP TABLE ticker_mappings` una vez verificada la migración. No se borra automáticamente para evitar pérdida accidental de datos.
- El agente pregunta el ticker al usuario si no lo puede inferir del PDF — un paso manual simple reemplaza toda la complejidad de `resolve_ticker.py`.
