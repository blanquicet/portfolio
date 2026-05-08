# PRD — Portfolio Tracker Genérico

**Fecha:** 2026-05-08
**Estado:** Aprobado
**Autor:** Jose Blanquicet + Claude

---

## 1. Visión y Alcance

### Propuesta de valor

Un tracker de portafolio de inversiones personal, self-hosted, operado vía Claude Code con skills propias. El usuario le pasa a Claude un PDF o screenshot de su broker, y el sistema extrae, ingesta y mantiene su portafolio automáticamente — con snapshot de posiciones, costo de venta FIFO (o lote específico), y reporte de renta Colombia.

### Usuarios objetivo

- **v1:** Colombianos técnicos capaces de clonar un repo y configurar Claude Code
- **v2 (futuro, fuera de scope):** Colombianos no técnicos — requiere UI

### IN scope

- Generalizar el repo para que cualquier usuario colombiano lo clone y use sin modificar código
- Mover skills al repo (`ingest`, `snapshot`, `setup`) — wrappers delgados en `personal-assistant` apuntan a estas
- Resolución automática de tickers (ISIN → Yahoo ticker) con fallback a confirmación del usuario
- Carga automática de FX (EUR/USD desde BCE, TRM desde Banrep) durante ingestión
- Flujo de setup inicial guiado por Claude (`/setup`)
- Tax report hardcodeado para Colombia (Ganancia Ocasional / Renta Ordinaria)
- Soporte de asignación de lote específico en ventas (`lot_assignments`) — ya implementado

### OUT of scope

- UI web o mobile
- Soporte multi-país para tax report
- Notificaciones o alertas de precio
- Integración directa con APIs de brokers

---

## 2. Arquitectura y Componentes

### Patrón WAT

El repo sigue el patrón WAT estrictamente:
- **Skills** — coordinan, no ejecutan lógica. Llaman scripts y presentan resultados.
- **Scripts (`tools/`)** — ejecutan toda la lógica. Una responsabilidad cada uno.
- **Queries (`queries/`)** — SQL fijo en archivos `.sql`, nunca inline.

### Estructura del repo

```
portfolio/
├── .claude/
│   └── skills/
│       ├── ingest.md          # ingestión desde PDF/screenshot (movida desde personal-assistant)
│       ├── snapshot.md        # snapshot de posiciones (movida desde personal-assistant)
│       └── setup.md           # NUEVO — onboarding guiado para usuario nuevo
├── schema.sql                 # incluye nueva tabla ticker_mappings
├── tools/
│   ├── fifo.py                # sin cambios — motor FIFO compartido
│   ├── snapshot.py            # TICKER_MAP eliminado → lee ticker_mappings de DB
│   ├── tax_report.py          # sin cambios — Colombia hardcodeado, documentado
│   ├── insert.py              # sin cambios
│   ├── assign_lot.py          # sin cambios
│   ├── resolve_ticker.py      # NUEVO — ISIN → ticker con auto-resolve + fallback manual
│   └── load_fx.py             # NUEVO — unifica load_eurusd + load_trm + auto-fetch APIs
├── queries/                   # sin cambios — SQL fijo por caso de uso
├── README.md                  # NUEVO — setup completo para usuario nuevo
└── portfolio.db               # en .gitignore — contiene datos personales
```

### En `personal-assistant/.claude/skills/`

`portfolio-ingest.md` y `portfolio-snapshot.md` se convierten en wrappers delgados que delegan a las skills del repo `portfolio/`. El usuario del asistente personal no nota diferencia.

### Nuevos componentes

#### Tabla `ticker_mappings`

Reemplaza el `TICKER_MAP` hardcodeado en `snapshot.py`. Persiste en DB, fuera de git.

```sql
CREATE TABLE IF NOT EXISTS ticker_mappings (
    isin         TEXT PRIMARY KEY,
    ticker       TEXT NOT NULL,
    exchange     TEXT,          -- LSE, PA, NASDAQ, etc.
    currency     TEXT NOT NULL, -- USD, EUR, GBP, etc.
    source       TEXT NOT NULL CHECK(source IN ('auto', 'manual')),
    verified_at  TEXT           -- ISO 8601
);
```

#### `resolve_ticker.py`

Dado un ISIN:
1. Busca en `ticker_mappings` — si existe, retorna inmediatamente
2. Si no existe, intenta inferir via Yahoo Finance search API
3. Si hay ambigüedad (mismo ETF disponible en USD y GBP en LSE), imprime las opciones y termina con exit code no-zero — la skill le pregunta al usuario y llama de nuevo con la elección
4. Guarda el resultado en `ticker_mappings` con `source=auto` o `source=manual`

#### `load_fx.py`

Unifica `load_eurusd.py` y `load_trm.py`. Durante ingestión, recibe una lista de fechas y monedas necesarias, detecta los huecos en `fx_rates`, y los llena automáticamente:
- **EUR/USD:** API pública del BCE
- **USD/COP (TRM):** API pública del Banco de la República

No requiere intervención del usuario. La skill lo llama antes de insertar transacciones.

---

## 3. Flujos de Usuario

### Flujo 1 — Setup inicial (usuario nuevo)

```
1. Clona el repo
2. pip install -r requirements.txt
3. Abre Claude Code en portfolio/
4. /setup → Claude crea portfolio.db desde schema.sql,
   verifica dependencias Python, confirma que está listo para ingestar
```

### Flujo 2 — Ingestión de transacciones

```
1. Usuario le pasa a Claude un PDF o screenshot del broker
2. /ingest → Claude extrae las transacciones del documento
3. Para cada security nuevo:
   a. Llama resolve_ticker.py — intenta resolver ISIN → ticker automáticamente
   b. Si hay ambigüedad (ej. mismo ETF en USD y GBP en LSE),
      Claude le pregunta al usuario y llama resolve_ticker.py con la elección
4. Llama load_fx.py con las fechas/monedas necesarias — llena fx_rates automáticamente
5. Llama insert.py para cada security y transacción
6. Confirma resumen: "X transacciones insertadas, Y tickers resueltos"
```

### Flujo 3 — Snapshot de posiciones

```
/snapshot → Claude llama python3 tools/snapshot.py
  → lee ticker_mappings de DB (no TICKER_MAP hardcodeado)
  → fetcha precios live de Yahoo Finance
  → retorna tabla: posición, precio, valor USD, costo FIFO/lote, P&L %
Claude presenta el resultado o filtra por lo que el usuario pidió
```

### Flujo 4 — Reporte de renta Colombia

```
/tax <año> → Claude llama python3 tools/tax_report.py <año>
  → calcula P&L por venta (FIFO o lote específico via lot_assignments)
  → clasifica: Ganancia Ocasional (>730 días) vs Renta Ordinaria
  → retorna resumen en COP y USD
Claude presenta el resultado
```

### Flujo 5 — Consulta específica

```
Usuario: "¿cuánto llevo ganado en MSFT?"
→ Claude llama el script relevante (snapshot.py u otro)
→ Lee el output completo
→ Filtra y responde con la info solicitada
El script es siempre la fuente de verdad
```

---

## 4. Privacidad y Datos

### Qué va en git y qué no

| Artefacto | En git | Razón |
|-----------|--------|-------|
| `schema.sql` | ✅ | Estructura genérica, sin datos |
| `tools/*.py` | ✅ | Código sin ISINs ni cantidades hardcodeadas |
| `.claude/skills/*.md` | ✅ | Instrucciones genéricas |
| `queries/*.sql` | ✅ | Queries genéricas |
| `README.md` | ✅ | Documentación pública |
| `portfolio.db` | ❌ | Contiene transacciones reales — `.gitignore` |
| `*.db-shm`, `*.db-wal` | ❌ | Ya en `.gitignore` |

### Cambios de privacidad al código existente

- `TICKER_MAP` en `snapshot.py` — eliminado, se mueve a `ticker_mappings` en DB
- Referencias a brokers específicos en queries — generalizadas
- `tax_report.py` — se documenta explícitamente como Colombia-specific, sin cambios de lógica

### README — contenido mínimo

1. Requisitos: Python 3.11+, Claude Code, Anthropic API key
2. Setup en 3 pasos: clonar → `pip install -r requirements.txt` → `/setup`
3. Cómo ingestar: pasar PDF/screenshot a Claude y correr `/ingest`
4. Comandos disponibles: `/snapshot`, `/tax <año>`
5. Nota explícita: tax report hardcodeado para Colombia (Ganancia Ocasional / Renta Ordinaria, TRM Banrep, UVT DIAN)
6. Cómo reportar un ticker que no se resuelve automáticamente

---

## 5. Decisiones de Diseño

| Decisión | Alternativas consideradas | Razón |
|----------|--------------------------|-------|
| `ticker_mappings` en DB | Config YAML, hardcoded | Portable, persiste sin tocar código, compatible con v2 |
| Auto-resolve con fallback manual | Solo manual, solo auto | Reduce fricción sin sacrificar precisión en casos ambiguos |
| FX auto-fetch en ingestión | Manual siempre, script separado | El usuario no debería pensar en FX — es infraestructura |
| Tax report Colombia-only, hardcoded | Pluggable multi-país | YAGNI — audiencia es colombiana, over-engineering innecesario |
| WAT pattern estricto | Lógica en skills | Debuggeable, testeable, reutilizable sin Claude |
| Skills en repo portfolio | Solo en personal-assistant | Repo autónomo — cualquier usuario lo clona y funciona |
