# Patrimonio Histórico Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Script `tools/patrimonio.py` que muestra el estado del portafolio al 31-dic de un año dado — lotes FIFO abiertos por broker/moneda, costo en moneda del activo, costo en COP, valor de mercado histórico, valor en COP.

**Architecture:** Dos cambios mínimos a `fifo.py` (parámetro `broker` en `build_queues`, nuevo método `remaining_lots_with_buy_id()`). Script nuevo `patrimonio.py` que itera brokers, llama `build_queues` por broker, descarga precios históricos con `yf.download(auto_adjust=False)`, convierte a COP con `fx_rates` históricas y genera tabla agrupada por broker → `securities.currency`.

**Tech Stack:** Python 3.11+, SQLite, yfinance, módulos existentes `tools/fifo.py`.

---

## File Map

| Acción | Archivo | Responsabilidad |
|---|---|---|
| Modificar | `tools/fifo.py` | Añadir `broker` param a `build_queues`; añadir `remaining_lots_with_buy_id()` a `FifoQueue` |
| Crear | `tools/patrimonio.py` | Script principal: carga lotes, precios, FX, imprime tabla |
| Crear | `tests/test_patrimonio.py` | 9 tests con DB en memoria |

---

## Contexto del codebase (leer antes de empezar)

**`tools/fifo.py`** — motor FIFO compartido. Las funciones relevantes:
- `build_queues(conn, as_of_date=None)` → `{isin: FifoQueue}` — construye colas FIFO desde DB
- `FifoQueue.remaining_lots()` → `[(qty, price_usd, dt, src)]` — lotes con qty > 0 (4 campos, NO modificar)
- `fx(conn, from_ccy, to_ccy, dt)` → `float | None` — tasa histórica de `fx_rates` (último valor ≤ dt)

**`tools/snapshot.py`** — usa `build_queues(conn)` sin `broker` y `avg_cost_usd()` / `oldest_buy_date()` que internamente llaman `remaining_lots()`. No tocar.

**`schema.sql`** — tablas relevantes:
- `securities(id, isin, name, type, currency)` — `currency` es `sec_ccy` (USD/EUR/COP)
- `transactions(id, security_id, date, type, broker, quantity, price, currency, total, fee, exchange, notes, source_file)`
- `ticker_mappings(isin, exchange, ticker, currency, source, verified_at)` — resuelve ISIN → ticker Yahoo
- `fx_rates(date, from_currency, to_currency, rate)` — tasas históricas

**Tests existentes** en `tests/` usan el patrón:
```python
import sys, os, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))
from fifo import FifoQueue, build_queues
```

---

## Task 1: Cambios mínimos a `fifo.py`

**Files:**
- Modify: `tools/fifo.py` (líneas 160-251 — función `build_queues` y clase `FifoQueue`)
- Test: `tests/test_patrimonio.py` (crear)

### Contexto
`build_queues` actualmente no filtra por broker — procesa todas las transacciones. Si el mismo ISIN existe en dos brokers, los lotes se mezclan. Necesitamos llamarlo una vez por broker. `remaining_lots()` descarta `buy_id` (guardado como `_bid`) — lo necesitamos para recuperar metadata del security. Creamos un método nuevo para no romper `avg_cost_usd()` y `oldest_buy_date()` que dependen del shape de 4 campos.

- [ ] **Step 1: Escribir tests que fallan**

Crear `tests/test_patrimonio.py` con estas dos pruebas iniciales:

```python
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
```

- [ ] **Step 2: Correr tests para verificar que fallan**

```bash
cd /Users/melendex/Documents/src/portfolio
python -m pytest tests/test_patrimonio.py -v
```

Esperado: FAIL — `build_queues() got an unexpected keyword argument 'broker'` y `FifoQueue has no attribute remaining_lots_with_buy_id`.

- [ ] **Step 3: Implementar cambios en `fifo.py`**

**Cambio 1 — parámetro `broker` en `build_queues`:**

Reemplazar la firma y la query en `build_queues` (línea ~160):

```python
def build_queues(conn, as_of_date=None, broker=None):
    """
    Read all buy/vesting/sell transactions from the DB and return
    a dict {isin: FifoQueue} with FIFO state reflecting all sales.

    If a sell transaction has rows in `lot_assignments`, those specific
    buy lots are consumed instead of the oldest-first FIFO default.

    as_of_date: ISO string 'YYYY-MM-DD'. If given, only transactions
                up to that date are included. Defaults to today.
    broker: if given, only transactions from this broker are included.
            Used by patrimonio.py to isolate lots per broker.
    """
    if as_of_date:
        date_clause = "AND t.date <= ?"
        params = [as_of_date]
    else:
        date_clause = "AND t.date <= date('now')"
        params = []

    if broker:
        broker_clause = "AND t.broker = ?"
        params.append(broker)
    else:
        broker_clause = ""

    rows = conn.execute(f"""
        SELECT
            s.isin, s.name, s.currency AS db_ccy,
            t.id, t.date, t.type, t.broker,
            t.quantity, t.currency AS t_ccy, t.total, t.fee
        FROM transactions t
        JOIN securities s ON s.id = t.security_id
        WHERE t.type IN ('buy','vesting','sell','sell_to_cover','transfer_in','transfer_out')
          {date_clause}
          {broker_clause}
        ORDER BY t.date, t.id
    """, params).fetchall()
```

**Cambio 2 — añadir `remaining_lots_with_buy_id()` a `FifoQueue`** (después de `remaining_lots()`, línea ~136):

```python
    def remaining_lots_with_buy_id(self):
        """Return lots with qty > 0, including buy_id. Used by patrimonio.py."""
        return [(qty, price_usd, dt, src, bid)
                for qty, price_usd, dt, src, bid in self.lots
                if qty > 1e-6]
```

- [ ] **Step 4: Correr tests para verificar que pasan**

```bash
python -m pytest tests/test_patrimonio.py -v
```

Esperado: PASS (2/2).

- [ ] **Step 5: Verificar que los tests existentes siguen pasando**

```bash
python -m pytest tests/ -v
```

Esperado: todos los tests existentes siguen en PASS. Si alguno falla, es señal de que el cambio a `build_queues` rompió algo — revisar.

- [ ] **Step 6: Commit**

```bash
git add tools/fifo.py tests/test_patrimonio.py
git commit -m "feat(fifo): add broker filter to build_queues; add remaining_lots_with_buy_id()"
```

---

## Task 2: Tests de `patrimonio.py` (TDD — escribir antes del script)

**Files:**
- Test: `tests/test_patrimonio.py` (extender)
- Read: `tools/tax_report.py` (referencia para el patrón de conversión FX)

### Contexto
`patrimonio.py` necesita una función de lógica pura que dado un lote y tasas FX calcule costo en `sec_ccy` y costo en COP. Testeamos esa función de forma aislada antes de escribir el script. La función se importará como `from patrimonio import calc_lot_costs`.

La lógica de costo (del spec):
- `cost_usd = price_usd × qty` (price_usd ya viene convertido a USD por build_queues)
- `sec_ccy=USD`: `cost_sec = cost_usd`; `cost_cop = cost_usd × TRM_compra`
- `sec_ccy=EUR`: `cost_sec = cost_usd / EUR_USD_compra`; `cost_cop = cost_usd × TRM_compra`
- `sec_ccy=COP`: `cost_sec = cost_usd × TRM_compra` (= cost_cop); no TRM adicional

- [ ] **Step 1: Añadir tests de `calc_lot_costs` a `test_patrimonio.py`**

Añadir al final de `tests/test_patrimonio.py`:

```python
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
```

- [ ] **Step 2: Correr para verificar que fallan**

```bash
python -m pytest tests/test_patrimonio.py::test_calc_lot_costs_usd_sec -v
```

Esperado: FAIL — `ModuleNotFoundError: No module named 'patrimonio'`.

- [ ] **Step 3: Crear `tools/patrimonio.py` con solo `calc_lot_costs`**

```python
#!/usr/bin/env python3
"""
Patrimonio histórico al 31-dic de un año dado.

Usage:
    python3 tools/patrimonio.py 2025              # → snapshot al 2025-12-31
    python3 tools/patrimonio.py 2024              # → snapshot al 2024-12-31
    python3 tools/patrimonio.py --as-of 2025-06-30  # → snapshot a fecha arbitraria
"""
import sqlite3, sys, os, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(__file__))
from fifo import build_queues, fx

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")


def calc_lot_costs(qty, price_usd, sec_ccy, trm_compra, eur_usd_compra):
    """
    Calcula costo en sec_ccy y costo en COP para un lote FIFO.

    Args:
        qty:            cantidad restante del lote (ya prorrateada)
        price_usd:      precio por unidad en USD (calculado por build_queues)
        sec_ccy:        moneda del security ('USD', 'EUR', 'COP')
        trm_compra:     TRM (USD/COP) en la fecha de compra, o None
        eur_usd_compra: EUR/USD en la fecha de compra, o None

    Returns:
        dict con 'cost_sec' (en sec_ccy) y 'cost_cop', ambos pueden ser None.
    """
    cost_usd = price_usd * qty

    if sec_ccy == "USD":
        cost_sec = cost_usd
        cost_cop = cost_usd * trm_compra if trm_compra is not None else None

    elif sec_ccy == "EUR":
        cost_sec = cost_usd / eur_usd_compra if eur_usd_compra else None
        cost_cop = cost_usd * trm_compra if trm_compra is not None else None

    elif sec_ccy == "COP":
        cost_sec = cost_usd * trm_compra if trm_compra is not None else None
        cost_cop = cost_sec

    else:
        print(f"  ⚠ calc_lot_costs: sec_ccy '{sec_ccy}' no soportado — tratando como USD",
              file=sys.stderr)
        cost_sec = cost_usd
        cost_cop = cost_usd * trm_compra if trm_compra is not None else None

    return {"cost_sec": cost_sec, "cost_cop": cost_cop}
```

- [ ] **Step 4: Correr tests para verificar que pasan**

```bash
python -m pytest tests/test_patrimonio.py -v
```

Esperado: PASS (8/8 — los 2 de Task 1 + los 6 de Task 2).

- [ ] **Step 5: Commit**

```bash
git add tools/patrimonio.py tests/test_patrimonio.py
git commit -m "feat(patrimonio): add calc_lot_costs with full test coverage"
```

---

## Task 3: Lógica de precios históricos y conversión FX

**Files:**
- Modify: `tools/patrimonio.py`
- Test: `tests/test_patrimonio.py` (extender)

### Contexto
Necesitamos dos funciones puras más:

1. `to_sec_ccy_price(yahoo_price, yahoo_ccy, sec_ccy, eur_usd, trm, gbp_usd)` — convierte precio Yahoo a precio en `sec_ccy`. Dos pasos: Yahoo→USD, luego USD→sec_ccy.
2. `fetch_historical_prices(tickers, as_of)` — descarga precios con `yf.download(auto_adjust=False)`, fallback ±7 días, devuelve `{ticker: (price, yahoo_ccy)}`.

La conversión Yahoo→USD:
- `USD` → directo
- `EUR` → `× eur_usd`
- `GBP` → `× gbp_usd`
- `GBp` (peniques) → `÷ 100 × gbp_usd`
- otro → warning stderr, tratar como USD

USD→sec_ccy:
- `USD` → directo
- `EUR` → `÷ eur_usd`
- `COP` → `× trm`

- [ ] **Step 1: Añadir tests de `to_sec_ccy_price` a `test_patrimonio.py`**

```python
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
```

- [ ] **Step 2: Correr para verificar que fallan**

```bash
python -m pytest tests/test_patrimonio.py::test_to_sec_ccy_usd_yahoo_usd_sec -v
```

Esperado: FAIL — `ImportError: cannot import name 'to_sec_ccy_price'`.

- [ ] **Step 3: Añadir `to_sec_ccy_price` y `fetch_historical_prices` a `patrimonio.py`**

Añadir después de `calc_lot_costs`:

```python
def to_sec_ccy_price(yahoo_price, yahoo_ccy, sec_ccy, eur_usd, trm, gbp_usd):
    """
    Convierte precio Yahoo → precio en sec_ccy.

    Paso 1: yahoo_ccy → USD
    Paso 2: USD → sec_ccy

    Devuelve None si falta una tasa necesaria.
    """
    # Paso 1: yahoo → USD
    yc = yahoo_ccy.strip()
    if yc == "USD":
        price_usd = yahoo_price
    elif yc == "EUR":
        if eur_usd is None:
            return None
        price_usd = yahoo_price * eur_usd
    elif yc == "GBP":
        if gbp_usd is None:
            return None
        price_usd = yahoo_price * gbp_usd
    elif yc == "GBp":
        if gbp_usd is None:
            return None
        price_usd = yahoo_price / 100 * gbp_usd
    else:
        print(f"  ⚠ to_sec_ccy_price: moneda Yahoo '{yahoo_ccy}' no soportada — tratando como USD",
              file=sys.stderr)
        price_usd = yahoo_price

    # Paso 2: USD → sec_ccy
    if sec_ccy == "USD":
        return price_usd
    elif sec_ccy == "EUR":
        if eur_usd is None:
            return None
        return price_usd / eur_usd
    elif sec_ccy == "COP":
        if trm is None:
            return None
        return price_usd * trm
    else:
        print(f"  ⚠ to_sec_ccy_price: sec_ccy '{sec_ccy}' no soportado", file=sys.stderr)
        return price_usd


def fetch_historical_prices(tickers, as_of):
    """
    Descarga precios de cierre históricos para una lista de tickers.

    Args:
        tickers: lista de strings (Yahoo Finance tickers)
        as_of:   datetime.date — fecha de corte

    Returns:
        dict {ticker: (price_float, yahoo_ccy_str)}
        Si no hay precio: el ticker no está en el dict.
    """
    import yfinance as yf
    from datetime import timedelta

    if not tickers:
        return {}

    result = {}

    # Intentar ventana amplia: 7 días antes hasta as_of+1
    start = as_of - timedelta(days=7)
    end   = as_of + timedelta(days=1)

    try:
        raw = yf.download(
            tickers if len(tickers) > 1 else tickers[0],
            start=str(start), end=str(end),
            auto_adjust=False, progress=False
        )
        if not raw.empty:
            closes = raw["Close"]
            for t in tickers:
                try:
                    col  = closes[t] if len(tickers) > 1 else closes
                    last = col.dropna()
                    if not last.empty:
                        result[t] = (float(last.iloc[-1]), None)  # currency llenada abajo
                except Exception:
                    pass
    except Exception as e:
        print(f"  ⚠ fetch_historical_prices: error descargando precios: {e}", file=sys.stderr)

    # Obtener moneda de Yahoo para cada ticker con precio
    for t in list(result.keys()):
        try:
            info = yf.Ticker(t).fast_info
            ccy  = getattr(info, "currency", None) or "USD"
            result[t] = (result[t][0], ccy)
        except Exception:
            result[t] = (result[t][0], "USD")  # fallback

    return result
```

- [ ] **Step 4: Correr tests**

```bash
python -m pytest tests/test_patrimonio.py -v
```

Esperado: PASS (14/14).

- [ ] **Step 5: Commit**

```bash
git add tools/patrimonio.py tests/test_patrimonio.py
git commit -m "feat(patrimonio): add to_sec_ccy_price and fetch_historical_prices"
```

---

## Task 4: Script principal y output

**Files:**
- Modify: `tools/patrimonio.py`
- Test: `tests/test_patrimonio.py` (extender con test de integración)

### Contexto
La función `run(as_of)` orquesta todo:
1. Obtiene lista de brokers con transacciones hasta `as_of`
2. Por cada broker: `build_queues(conn, as_of_date=str(as_of), broker=b)`
3. Por cada lote en `queue.remaining_lots_with_buy_id()`: recupera `sec_ccy` y `name` via `buy_id → transactions.security_id → securities`
4. Agrupa en `{(broker, sec_ccy): [lote_dict, ...]}`
5. Carga FX del `as_of` desde DB (TRM, EUR/USD, GBP/USD)
6. Descarga precios históricos para todos los tickers únicos
7. Imprime tabla agrupada

- [ ] **Step 1: Añadir test de integración a `test_patrimonio.py`**

```python
# ── Test Task 4: integración run() ───────────────────────────────────────────

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

        -- Securities
        INSERT INTO securities VALUES (1, 'US0000000001', 'MSFT Test',   'stock', 'USD');
        INSERT INTO securities VALUES (2, 'IE00000000EU', 'LVMH Test',   'stock', 'EUR');
        INSERT INTO securities VALUES (3, 'COB00000001',  'BanCo Test',  'stock', 'COP');

        -- Broker fidelity: MSFT USD, compra 10 unidades, vende 4
        INSERT INTO transactions VALUES (1, 1, '2024-01-15', 'buy',  'fidelity', 10, 300.0, 'USD', 3000.0, 0, 'XNAS', NULL, NULL);
        INSERT INTO transactions VALUES (2, 1, '2024-06-01', 'sell', 'fidelity',  4, 380.0, 'USD', 1520.0, 0, 'XNAS', NULL, NULL);

        -- Broker scalable: LVMH EUR
        INSERT INTO transactions VALUES (3, 2, '2024-02-01', 'buy', 'scalable', 5, 800.0, 'EUR', 4000.0, 0, 'XPAR', NULL, NULL);

        -- Broker trii: BanCo COP
        INSERT INTO transactions VALUES (4, 3, '2024-03-01', 'buy', 'trii', 100, 5000.0, 'COP', 500000.0, 0, 'XBOG', NULL, NULL);

        -- Ticker mappings
        INSERT INTO ticker_mappings VALUES ('US0000000001', 'XNAS', 'MSFT', 'USD', 'manual', '2024-01-01');
        INSERT INTO ticker_mappings VALUES ('IE00000000EU', 'XPAR', 'MC.PA', 'EUR', 'manual', '2024-01-01');

        -- FX rates
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
    """
    run() agrupa lotes correctamente por broker → sec_ccy.
    Mockeamos fetch_historical_prices para no llamar a Yahoo.
    """
    import patrimonio
    from datetime import date

    # Mock: devuelve precio fijo para todos los tickers
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
```

- [ ] **Step 2: Correr para verificar que fallan**

```bash
python -m pytest tests/test_patrimonio.py::test_collect_lots_by_broker_and_secccy -v
```

Esperado: FAIL — `cannot import name 'collect_lots' from 'patrimonio'`.

- [ ] **Step 3: Implementar `collect_lots` y `run` en `patrimonio.py`**

Añadir al final de `patrimonio.py`:

```python
def collect_lots(conn, as_of):
    """
    Construye grupos {(broker, sec_ccy): [lot_dict]} para la fecha as_of.

    lot_dict contiene:
        name, isin, ticker, qty, price_usd, buy_date,
        cost_sec, cost_cop, sec_ccy, broker
    """
    from datetime import date as date_type
    as_of_str = str(as_of)

    # Cargar todos los brokers con transacciones hasta as_of
    brokers = [r[0] for r in conn.execute(
        "SELECT DISTINCT broker FROM transactions WHERE date <= ?", (as_of_str,)
    ).fetchall()]

    # Cargar ticker_mappings {isin: ticker}
    ticker_map = {}
    for row in conn.execute("SELECT isin, exchange, ticker, source FROM ticker_mappings"):
        isin, exch, ticker, source = row
        # manual wins over auto (mismo patrón que snapshot.py)
        if isin not in ticker_map or source == "manual":
            ticker_map[isin] = ticker

    groups = {}

    for broker in brokers:
        queues, errors = build_queues(conn, as_of_date=as_of_str, broker=broker)
        for err in errors:
            print(f"  ⚠ FIFO: {err}", file=sys.stderr)

        for isin, queue in queues.items():
            for qty, price_usd, buy_date, src, buy_id in queue.remaining_lots_with_buy_id():
                # Recuperar sec_ccy y name
                row = conn.execute("""
                    SELECT s.name, s.currency
                    FROM transactions t
                    JOIN securities s ON s.id = t.security_id
                    WHERE t.id = ?
                """, (buy_id,)).fetchone()

                if row is None:
                    print(f"  ⚠ buy_id {buy_id} no encontrado en DB", file=sys.stderr)
                    continue

                name, sec_ccy = row[0], row[1]

                # FX en fecha de compra
                trm_compra     = fx(conn, "USD", "COP", buy_date)
                eur_usd_compra = fx(conn, "EUR", "USD", buy_date)

                if trm_compra is None:
                    print(f"  ⚠ TRM no disponible para {buy_date} — costo COP sera None",
                          file=sys.stderr)

                costs = calc_lot_costs(qty, price_usd, sec_ccy, trm_compra, eur_usd_compra)

                lot = {
                    "name":     name,
                    "isin":     isin,
                    "ticker":   ticker_map.get(isin),
                    "qty":      qty,
                    "price_usd": price_usd,
                    "buy_date": buy_date,
                    "sec_ccy":  sec_ccy,
                    "broker":   broker,
                    "cost_sec": costs["cost_sec"],
                    "cost_cop": costs["cost_cop"],
                    # valor al as_of — se llena después
                    "price_asof":  None,
                    "val_sec":     None,
                    "val_cop":     None,
                }
                key = (broker, sec_ccy)
                groups.setdefault(key, []).append(lot)

    return groups


def run(as_of):
    """
    Imprime el snapshot de patrimonio al as_of.
    """
    from datetime import timedelta

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # FX al as_of (para valoración al corte)
    as_of_str  = str(as_of)
    trm_asof     = fx(conn, "USD", "COP", as_of_str)
    eur_usd_asof = fx(conn, "EUR", "USD", as_of_str)
    gbp_usd_asof = fx(conn, "GBP", "USD", as_of_str)

    if trm_asof is None:
        print(f"  ⚠ TRM no disponible para {as_of_str} — valores COP serán None", file=sys.stderr)

    groups = collect_lots(conn, as_of)
    conn.close()

    # Recopilar tickers únicos con precio pendiente
    all_tickers = list({
        lot["ticker"]
        for lots in groups.values()
        for lot in lots
        if lot["ticker"] is not None
    })

    print(f"\n  Descargando precios históricos al {as_of_str}…", end=" ", flush=True)
    price_map = fetch_historical_prices(all_tickers, as_of)
    print("listo.")

    # Enriquecer lotes con valor al as_of
    for (broker, sec_ccy), lots in groups.items():
        for lot in lots:
            ticker = lot["ticker"]
            if ticker is None or ticker not in price_map:
                continue
            yahoo_price, yahoo_ccy = price_map[ticker]
            if yahoo_ccy is None:
                yahoo_ccy = "USD"
            price_sec = to_sec_ccy_price(
                yahoo_price, yahoo_ccy, sec_ccy,
                eur_usd=eur_usd_asof, trm=trm_asof, gbp_usd=gbp_usd_asof
            )
            if price_sec is not None:
                lot["price_asof"] = price_sec
                lot["val_sec"]    = price_sec * lot["qty"]
                if sec_ccy == "USD":
                    lot["val_cop"] = lot["val_sec"] * trm_asof if trm_asof else None
                elif sec_ccy == "EUR":
                    lot["val_cop"] = lot["val_sec"] * eur_usd_asof * trm_asof if (eur_usd_asof and trm_asof) else None
                elif sec_ccy == "COP":
                    lot["val_cop"] = lot["val_sec"]

    _print_report(groups, as_of_str, trm_asof, eur_usd_asof)


def _fmt(v, decimals=2, width=12):
    """Formatea número con separadores de miles. None → '—' alineado."""
    if v is None:
        return f"{'—':>{width}}"
    if decimals == 0:
        return f"{v:>{width},.0f}"
    return f"{v:>{width},.{decimals}f}"


def _print_report(groups, as_of_str, trm_asof, eur_usd_asof):
    W = 120
    print(f"\n{'═'*W}")
    print(f"  Patrimonio al {as_of_str}")
    if trm_asof:
        print(f"  TRM: {trm_asof:,.0f}   EUR/USD: {eur_usd_asof:.4f}" if eur_usd_asof
              else f"  TRM: {trm_asof:,.0f}")
    print(f"{'═'*W}")

    total_cost_cop = 0.0
    total_val_cop  = 0.0
    missing_price_count = 0
    broker_totals = {}  # {broker: {cost_cop, val_cop}}

    for (broker, sec_ccy), lots in sorted(groups.items()):
        if not lots:
            continue

        is_cop = sec_ccy == "COP"
        ccy_label = sec_ccy

        print(f"\n{broker.upper()} — {ccy_label}")

        if is_cop:
            print(f"  {'Instrumento':<36} {'Fecha cmp':>10}  {'Qty':>8}  "
                  f"{'Costo COP':>15}  {'Precio 31d':>12}  {'Valor COP':>15}")
        else:
            print(f"  {'Instrumento':<36} {'Fecha cmp':>10}  {'Qty':>8}  "
                  f"{'Costo '+ccy_label:>12}  {'TRM cmp':>8}  {'Costo COP':>15}  "
                  f"{'Precio 31d':>12}  {'Valor '+ccy_label:>12}  {'Valor COP':>15}")
        print(f"  {'─'*(W-2)}")

        sub_cost_sec = 0.0
        sub_cost_cop = 0.0
        sub_val_sec  = 0.0
        sub_val_cop  = 0.0

        for lot in sorted(lots, key=lambda x: x["buy_date"]):
            import sqlite3 as _sq  # already imported but keep reference clear
            trm_cmp = None
            if not is_cop:
                # Re-derive TRM de compra del lot para mostrar en tabla
                # Nota: está embebido en el cálculo de cost_cop
                # Derivamos: trm_cmp = cost_cop / cost_usd si disponible
                cost_usd = lot["price_usd"] * lot["qty"]
                if lot["cost_cop"] is not None and cost_usd > 0:
                    trm_cmp = lot["cost_cop"] / cost_usd

            if lot["cost_sec"] is not None:
                sub_cost_sec += lot["cost_sec"]
            if lot["cost_cop"] is not None:
                sub_cost_cop += lot["cost_cop"]
            if lot["val_sec"] is not None:
                sub_val_sec += lot["val_sec"]
            if lot["val_cop"] is not None:
                sub_val_cop += lot["val_cop"]
            if lot["price_asof"] is None:
                missing_price_count += 1

            if is_cop:
                print(f"  {lot['name']:<36} {lot['buy_date']:>10}  {lot['qty']:>8.3f}  "
                      f"{_fmt(lot['cost_sec'], 0, 15)}  "
                      f"{_fmt(lot['price_asof'], 0, 12)}  "
                      f"{_fmt(lot['val_cop'], 0, 15)}")
            else:
                print(f"  {lot['name']:<36} {lot['buy_date']:>10}  {lot['qty']:>8.3f}  "
                      f"{_fmt(lot['cost_sec'], 2, 12)}  "
                      f"{_fmt(trm_cmp, 0, 8)}  "
                      f"{_fmt(lot['cost_cop'], 0, 15)}  "
                      f"{_fmt(lot['price_asof'], 2, 12)}  "
                      f"{_fmt(lot['val_sec'], 2, 12)}  "
                      f"{_fmt(lot['val_cop'], 0, 15)}")

        print(f"  {'─'*(W-2)}")
        if is_cop:
            print(f"  {'Subtotal':<36} {'':>10}  {'':>8}  "
                  f"{_fmt(sub_cost_sec, 0, 15)}  {'':>12}  {_fmt(sub_val_cop, 0, 15)}")
        else:
            print(f"  {'Subtotal':<36} {'':>10}  {'':>8}  "
                  f"{_fmt(sub_cost_sec, 2, 12)}  {'':>8}  "
                  f"{_fmt(sub_cost_cop, 0, 15)}  {'':>12}  "
                  f"{_fmt(sub_val_sec, 2, 12)}  {_fmt(sub_val_cop, 0, 15)}")

        total_cost_cop += sub_cost_cop
        total_val_cop  += sub_val_cop
        bt = broker_totals.setdefault(broker, {"cost_cop": 0.0, "val_cop": 0.0})
        bt["cost_cop"] += sub_cost_cop
        bt["val_cop"]  += sub_val_cop

    # Gran total
    print(f"\n{'═'*W}")
    missing_note = f"  (* excluye {missing_price_count} lote(s) sin precio)" if missing_price_count else ""
    print(f"  TOTAL COP     Costo: {total_cost_cop:>15,.0f}     Valor: {total_val_cop:>15,.0f}{missing_note}")

    # Resumen por broker
    print(f"\n  Resumen por broker")
    print(f"  {'─'*60}")
    for broker, bt in sorted(broker_totals.items()):
        print(f"  {broker.upper():<12}  Costo COP: {bt['cost_cop']:>15,.0f}   Valor COP: {bt['val_cop']:>15,.0f}")
    print(f"  {'─'*60}")
    print(f"  {'TOTAL':<12}  Costo COP: {total_cost_cop:>15,.0f}   Valor COP: {total_val_cop:>15,.0f}")
    print()


if __name__ == "__main__":
    from datetime import date, timedelta

    args   = sys.argv[1:]
    as_of  = None

    if "--as-of" in args:
        idx   = args.index("--as-of")
        as_of = date.fromisoformat(args[idx + 1])
    else:
        year_arg = next((a for a in args if a.isdigit() and len(a) == 4), None)
        if year_arg is None:
            print("Uso: python3 tools/patrimonio.py <año>  |  --as-of YYYY-MM-DD")
            print("  Ej: python3 tools/patrimonio.py 2025")
            sys.exit(1)
        as_of = date(int(year_arg), 12, 31)

    run(as_of)
```

- [ ] **Step 4: Correr todos los tests**

```bash
python -m pytest tests/test_patrimonio.py -v
```

Esperado: PASS (16/16).

- [ ] **Step 5: Correr el script contra la DB real**

```bash
cd /Users/melendex/Documents/src/portfolio
python3 tools/patrimonio.py 2024
```

Verificar:
- Se imprime la tabla agrupada por broker/moneda
- Los subtotales suman correctamente
- El resumen por broker aparece al final
- Warnings (si los hay) van a stderr, no al output principal

- [ ] **Step 6: Correr suite completa de tests**

```bash
python -m pytest tests/ -v
```

Esperado: todos los tests previos siguen en PASS.

- [ ] **Step 7: Commit final**

```bash
git add tools/patrimonio.py tests/test_patrimonio.py
git commit -m "feat(patrimonio): script completo — snapshot historico por broker/moneda con lotes FIFO"
```
