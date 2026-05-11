# Patrimonio Histórico — Design Spec

**Goal:** Script `tools/patrimonio.py` que muestra el estado del portafolio al 31-dic de un año dado: lotes abiertos por broker/moneda, costo en moneda del activo, costo en COP, valor de mercado histórico al 31-dic, valor en COP.

**Architecture:** Script standalone que reutiliza `build_queues()` del motor FIFO con `as_of_date` y filtro por broker. Calcula costo desde `price_usd × qty_remaining` (ya en el lote — no usa `transactions.total`). Descarga precios históricos de Yahoo Finance con `auto_adjust=False`. Convierte a COP con tasas históricas de `fx_rates`. Requiere dos cambios mínimos a `fifo.py`.

**Tech Stack:** Python 3.11+, SQLite, yfinance, módulos existentes `fifo.py` (build_queues, fx).

---

## Uso

```bash
python3 tools/patrimonio.py 2025              # → snapshot al 2025-12-31
python3 tools/patrimonio.py 2024              # → snapshot al 2024-12-31
python3 tools/patrimonio.py --as-of 2025-06-30  # → snapshot a fecha arbitraria
```

Si se pasa `--as-of YYYY-MM-DD`, ignora el argumento de año y usa esa fecha exacta. Si se pasa solo el año, la fecha de corte es `YYYY-12-31`.

---

## Compatibilidad con plan ticker-as-identifier

Este script se implementa sobre la arquitectura actual: ISIN como identificador primario, tickers resueltos via `ticker_mappings`. El plan de migración ticker-as-identifier está escrito (`docs/superpowers/plans/2026-05-08-ticker-as-identifier.md`) pero no ejecutado. Si ese pivot se realiza en el futuro, `patrimonio.py` se actualiza entonces — no se diseña para ambas arquitecturas simultáneamente.

---

## Cambios a `fifo.py` (mínimos, no-breaking)

### 1. Parámetro `broker` en `build_queues()`

```python
def build_queues(conn, as_of_date=None, broker=None):
```

Si `broker` es distinto de `None`, agregar al WHERE:
```sql
AND t.broker = ?
```

Esto aísla los lotes y consumos por broker. Los callers existentes (`snapshot.py`, `tax_report.py`) no pasan `broker` → comportamiento idéntico al actual.

### 2. Nuevo método `remaining_lots_with_buy_id()` en `FifoQueue`

`remaining_lots()` **no se modifica** — `avg_cost_usd()` y `oldest_buy_date()` desempaquetan 4 campos y siguen funcionando sin cambios.

Se añade un método separado solo para `patrimonio.py`:

```python
def remaining_lots_with_buy_id(self):
    """Return lots with qty > 0, incluyendo buy_id. Solo para patrimonio.py."""
    return [(qty, price_usd, dt, src, bid)
            for qty, price_usd, dt, src, bid in self.lots
            if qty > 1e-6]
```

`patrimonio.py` llama `queue.remaining_lots_with_buy_id()` en vez de `remaining_lots()`.

---

## Flujo del script

1. Recibe año → `as_of = f"{year}-12-31"`
2. Consulta brokers distintos con transacciones hasta `as_of`:
   ```sql
   SELECT DISTINCT broker FROM transactions WHERE date <= ?
   ```
3. Por cada broker: llama `build_queues(conn, as_of_date=as_of, broker=b)`
4. Por cada lote en `queue.remaining_lots_with_buy_id()`: extrae `qty, price_usd, dt, src, buy_id`
5. Recupera `sec_ccy` y `name` del security via JOIN en DB (usando `buy_id` → `transactions.security_id` → `securities`)
6. Calcula costo en `sec_ccy` y costo COP (ver sección "Lógica de costo")
7. Agrupa lotes por `(broker, sec_ccy)`
8. Descarga precios históricos al `as_of` (ver sección "Precios históricos")
9. Calcula valor en `sec_ccy` y valor COP
10. Imprime tabla agrupada + subtotales + gran total COP

---

## Lógica de costo

El lote FIFO tiene `price_usd` (costo por unidad en USD, calculado en `build_queues` vía `to_usd` al momento de la compra) y `qty_remaining`. El costo es **siempre prorrateado** por la cantidad que queda:

```
cost_usd = price_usd × qty_remaining
```

Conversión a `sec_ccy`:

| `sec_ccy` | Costo en `sec_ccy` |
|---|---|
| USD | `cost_usd` |
| EUR | `cost_usd / EUR_USD(dt_compra)` |
| COP | `cost_usd × TRM(dt_compra)` |

Conversión a COP (para subtotales y gran total):

| `sec_ccy` | Costo COP |
|---|---|
| USD | `cost_usd × TRM(dt_compra)` |
| EUR | `cost_usd × TRM(dt_compra)` (intermedio USD ya aplicado) |
| COP | `cost_sec_ccy` directo |

> `TRM(dt)` y `EUR_USD(dt)` se obtienen via `fx(conn, from, to, dt)` de `fifo.py`, que usa el último valor disponible en `fx_rates` anterior o igual a `dt`.

---

## Precios históricos al 31-dic

```python
yf.download(tickers, start=as_of, end=as_of + timedelta(days=1),
            auto_adjust=False, progress=False)["Close"]
```

`auto_adjust=False` para evitar sesgo por dividendos y splits en valoración histórica de patrimonio.

**Fallback**: si Yahoo no devuelve precio en `as_of` exacto (fin de semana, feriado), ampliar ventana:
```python
yf.download(tickers, start=as_of - timedelta(days=7), end=as_of + timedelta(days=1), ...)
```
Tomar el último `Close` disponible ≤ `as_of`.

Si aún no hay precio: mostrar `—` en precio, valor moneda y valor COP para ese lote. El script no aborta.

**Caso IWDA.L, CSPX.L y otros `.L` USD-class:** Yahoo Finance devuelve el precio en USD para estos tickers, aunque cotizan en LSE. `yahoo_ccy = "USD"` — no hay conversión GBp→GBP necesaria. Esto es consistente con la decisión existente de tratar LSE=USD para ETFs de clase USD.

---

## Conversión Yahoo → sec_ccy (función `to_sec_ccy`)

Dos pasos encadenados:

**Paso 1 — Yahoo_ccy → USD**

| Yahoo currency | Conversión |
|---|---|
| `USD` | directo |
| `EUR` | `× EUR/USD(as_of)` |
| `GBP` | `× GBP/USD(as_of)` |
| `GBp` (peniques) | `÷ 100 × GBP/USD(as_of)` |
| otro | warning en stderr, tratar como USD (best effort) |

**Paso 2 — USD → sec_ccy**

| sec_ccy | Conversión |
|---|---|
| `USD` | directo |
| `EUR` | `÷ EUR/USD(as_of)` |
| `COP` | `× TRM(as_of)` |

**USD → COP para valor total:**

| sec_ccy | Valor COP |
|---|---|
| `USD` | `valor_usd × TRM(as_of)` |
| `EUR` | `valor_eur × EUR/USD(as_of) × TRM(as_of)` |
| `COP` | `valor_cop` directo |

Las tasas FX de `as_of` se cargan una sola vez al inicio del script.

---

## FX rates requeridas en DB

- `USD/COP` al 31-dic del año pedido
- `EUR/USD` al 31-dic del año pedido (si hay activos EUR)
- `GBP/USD` al 31-dic del año pedido (si hay activos con tickers GBP/GBp)
- `USD/COP` y `EUR/USD` en fechas de compra de cada lote abierto

Si alguna tasa falta: warning en stderr + `—` en campos afectados. No aborta.

---

## Output — formato

```
Patrimonio al 2025-12-31
═══════════════════════════════════════════════════════════════════════════════════════

FIDELITY — USD
  Instrumento          Fecha cmp    Qty      Costo USD    TRM cmp     Costo COP      Precio 31d   Valor USD    Valor COP
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  MSFT                 2022-03-15   10.000    3,200.00     4,250      13,600,000       421.00       4,210.00    18,439,800
  MSFT                 2023-11-01    5.000    1,825.00     4,100       7,482,500       421.00       2,105.00     9,219,900
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Subtotal                                    5,025.00                21,082,500                    6,315.00    27,659,700

IBKR — USD
  Instrumento          Fecha cmp    Qty      Costo USD    TRM cmp     Costo COP      Precio 31d   Valor USD    Valor COP
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  IWDA.L               2024-01-10   50.000    4,560.00     3,980      18,148,800       103.50       5,175.00    22,666,500
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Subtotal                                    4,560.00                18,148,800                    5,175.00    22,666,500

SCALABLE — EUR
  Instrumento          Fecha cmp    Qty      Costo EUR    TRM cmp     Costo COP      Precio 31d   Valor EUR    Valor COP
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  LVMH                 2023-05-10   10.000      800.00     4,600       3,723,648       110.20       1,102.00     5,139,014
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Subtotal                                      800.00                 3,723,648                    1,102.00     5,139,014

TRII — COP
  Instrumento          Fecha cmp    Qty      Costo COP                              Precio 31d   Valor COP
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────
  NVDACO               2024-06-01  100.000   2,150,000                               2,890       289,000,000
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────
  Subtotal                                  2,150,000                                          289,000,000

═══════════════════════════════════════════════════════════════════════════════════════════════════════════
TOTAL COP     Costo: 45,104,948     Valor: 344,465,214
TRM al 2025-12-31: 4,380   |   EUR/USD: 1.0812
```

Notas de formato:
- Sección COP omite columnas TRM y Costo/Valor en moneda extranjera
- Qty con 3 decimales
- Si precio no disponible: `—` en Precio, Valor moneda y Valor COP del lote; ese lote contribuye al Costo COP del subtotal pero no al Valor COP
- TRM cmp = USD/COP del día de compra del lote (siempre USD/COP, independiente de la moneda del activo). Cada lote puede tener TRM distinta.
- Warnings (FX faltante, precio ausente) van a stderr — nunca interrumpen el output tabular
- Si algún lote no tiene precio, el Gran Total Valor incluye nota: `(* excluye N lote(s) sin precio)`

**Resumen por broker al final:**
```
Resumen por broker
  FIDELITY    Costo COP:  21,082,500   Valor COP:  27,659,700
  IBKR        Costo COP:  18,148,800   Valor COP:  22,666,500
  SCALABLE    Costo COP:   3,723,648   Valor COP:   5,139,014
  TRII        Costo COP:   2,150,000   Valor COP: 289,000,000
  ────────────────────────────────────────────────────────────
  TOTAL       Costo COP:  45,104,948   Valor COP: 344,465,214
```

---

## Archivos

| Acción | Archivo | Cambio |
|---|---|---|
| Modificar | `tools/fifo.py` | Dos cambios mínimos: parámetro `broker` en `build_queues`, nuevo método `remaining_lots_with_buy_id()` en `FifoQueue` |
| Crear | `tools/patrimonio.py` | Script nuevo |
| Crear | `tests/test_patrimonio.py` | Tests |

---

## Tests

`tests/test_patrimonio.py` — DB en memoria:

1. **Lote único USD**: compra USD, verifica `cost_usd = price_usd × qty`, costo COP, valor al corte
2. **Lote EUR nativo** (`sec_ccy=EUR`, `tx_ccy=EUR`): verifica `cost_eur`, conversión EUR→COP
3. **Lote USD comprado en EUR** (`sec_ccy=USD`, `tx_ccy=EUR`): verifica que `cost_usd = price_usd × qty_remaining` (price_usd ya calculado por `build_queues` vía `to_usd`) y que `cost_cop = cost_usd × TRM(dt_compra)`
4. **Lote COP**: verifica que no aplica TRM ni conversión adicional
5. **Venta parcial**: lote con 10 unidades, venta de 4 → `qty_remaining=6`, costo = `price_usd × 6`
6. **Broker isolation**: mismo ISIN en dos brokers, verifica que los lotes no se mezclan
7. **Precio no disponible**: lote sin ticker en ticker_mappings → muestra `—` sin excepción
8. **Conversión GBp**: Yahoo devuelve precio en GBp → divide entre 100 antes de convertir
9. **Subtotales y gran total COP**: verifica sumas aritméticas con al menos 2 brokers y 2 monedas
