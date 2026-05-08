# PRD — Portfolio Tracker Genérico

**Fecha:** 2026-05-08
**Estado:** Aprobado v3
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
- Mover skills al repo (`ingest`, `snapshot`, `setup`) con rutas relativas — sin rutas absolutas hardcodeadas
- Wrappers delgados en `personal-assistant` que delegan a las skills del repo portfolio
- Resolución automática de tickers (ISIN → Yahoo ticker) con fallback a confirmación del usuario
- Carga automática de FX (EUR/USD desde BCE, TRM desde Banrep) con degradación elegante a manual
- Flujo de setup inicial mínimo: verificar Python + crear DB desde `schema.sql`
- Tax report hardcodeado para Colombia (Ganancia Ocasional / Renta Ordinaria)
- Soporte de asignación de lote específico en ventas (`lot_assignments`) — ya implementado
- Script de migración para bases de datos existentes (`tools/migrate.py`)

### OUT of scope

- UI web o mobile
- Soporte multi-país para tax report
- Notificaciones o alertas de precio
- Integración directa con APIs de brokers
- Archivo de mapeos de tickers conocidos mantenido en el repo

---

## 2. Arquitectura y Componentes

### Patrón WAT

El repo sigue el patrón WAT estrictamente:
- **Skills** — coordinan, no ejecutan lógica. Llaman scripts y presentan resultados.
- **Scripts (`tools/`)** — ejecutan toda la lógica. Una responsabilidad cada uno.
- **Queries (`queries/`)** — SQL fijo en archivos `.sql`, nunca inline.

Las skills usan rutas relativas desde el root del repo. Ninguna ruta absoluta en skills ni scripts.

### Estructura del repo

```
portfolio/
├── .claude/
│   └── skills/
│       ├── ingest.md          # ingestión desde PDF/screenshot (movida desde personal-assistant)
│       ├── snapshot.md        # snapshot de posiciones (movida desde personal-assistant)
│       └── setup.md           # NUEVO — setup mínimo: verificar Python + crear DB
├── schema.sql                 # estado final del schema — incluye ticker_mappings
├── tools/
│   ├── fifo.py                # sin cambios — motor FIFO compartido
│   ├── snapshot.py            # TICKER_MAP eliminado → lee ticker_mappings de DB
│   ├── tax_report.py          # sin cambios — Colombia hardcodeado, documentado
│   ├── insert.py              # sin cambios
│   ├── assign_lot.py          # sin cambios
│   ├── resolve_ticker.py      # NUEVO — ISIN + exchange → ticker, auto-resolve + fallback manual
│   ├── load_fx.py             # NUEVO — unifica load_eurusd + load_trm + auto-fetch con fallback
│   └── migrate.py             # NUEVO — migra DB existente al schema actual
├── queries/                   # sin cambios — SQL fijo por caso de uso
├── requirements.txt           # NUEVO — dependencias Python explícitas con versiones
├── README.md                  # NUEVO — setup completo para usuario nuevo
└── portfolio.db               # en .gitignore — contiene datos personales
```

### En `personal-assistant/.claude/skills/`

`portfolio-ingest.md` y `portfolio-snapshot.md` se convierten en wrappers delgados que delegan a las skills del repo `portfolio/`. El usuario del asistente personal no nota diferencia.

### Nuevos componentes

#### Tabla `ticker_mappings`

Reemplaza el `TICKER_MAP` hardcodeado en `snapshot.py`. Persiste en DB, fuera de git.
Un mismo ISIN puede tener múltiples entradas si cotiza en varias plazas (ej. LSE en USD y Euronext en EUR).
La PK es `(isin, exchange)`.

```sql
CREATE TABLE IF NOT EXISTS ticker_mappings (
    isin         TEXT NOT NULL,
    exchange     TEXT NOT NULL,   -- ISO MIC: XLON, XPAR, XNAS, XNYS, XETR, etc.
    ticker       TEXT NOT NULL,
    currency     TEXT NOT NULL,   -- USD, EUR, GBP, COP, etc.
    source       TEXT NOT NULL CHECK(source IN ('auto', 'manual')),
    verified_at  TEXT,            -- ISO 8601
    PRIMARY KEY (isin, exchange)
);
```

**Canon de exchange — ISO MIC obligatorio:**
El campo `exchange` usa siempre códigos ISO 10383 (MIC). Los brokers suelen traer abreviaciones propias; `resolve_ticker.py` es responsable de normalizar antes de insertar:

| Abreviación broker | MIC canónico |
|--------------------|--------------|
| LSE                | XLON         |
| PA, XPAR           | XPAR         |
| NASDAQ             | XNAS         |
| NYSE               | XNYS         |
| XETRA              | XETR         |

Si el broker no trae exchange (campo nulo o vacío), `resolve_ticker.py` no puede construir la PK → cae directamente a resolución manual: la skill pregunta al usuario en qué bolsa opera el instrumento antes de intentar la búsqueda.

Cuando un ISIN tiene múltiples entradas, la skill de ingestión usa la plaza del broker de origen (ya normalizada a MIC) para seleccionar el ticker correcto.

#### `resolve_ticker.py`

Dado un ISIN y opcionalmente un exchange/currency hint:
1. Busca en `ticker_mappings` por `(isin, exchange)` — si existe, retorna inmediatamente
2. Si no existe, intenta inferir via Yahoo Finance (búsqueda por ISIN)
3. Si hay ambigüedad (mismo ISIN disponible en varias plazas/monedas), imprime las opciones numeradas y termina con exit code no-zero — la skill pregunta al usuario su elección y llama de nuevo con el exchange seleccionado
4. Guarda el resultado en `ticker_mappings` con `source='auto'` o `source='manual'`

**Nota:** Yahoo Finance no tiene API oficial de búsqueda por ISIN. El script usa endpoints no documentados que pueden cambiar. Si fallan, el flujo cae directamente a selección manual — el usuario nunca queda bloqueado.

#### `load_fx.py`

Unifica `load_eurusd.py` y `load_trm.py`. Durante ingestión, recibe una lista de fechas y pares de monedas necesarios, detecta los huecos en `fx_rates`, y los llena con degradación elegante:

| Par | Fuente primaria | Fallback manual |
|-----|----------------|-----------------|
| EUR/USD | API pública BCE | Instrucciones de descarga |
| USD/COP (TRM) | API Banrep | URL + pasos explícitos (ver abajo) |
| GBP/USD | API pública BCE o similar | Instrucciones de descarga |

**Fallback TRM:** Si la API de Banrep falla, el script imprime:
```
No se pudo obtener TRM automáticamente.
Descarga manual en: https://suameca.banrep.gov.co/estadisticas-economicas/informacionSerie/1/tasa_cambio_peso_colombiano_trm_dolar_usd
→ Cambiar a vista "Tabla" → Seleccionar fechas de interés → Descargar
→ Luego: python3 tools/load_trm.py <archivo.txt>
```

#### `tools/migrate.py`

Script de migración para usuarios con DB existente. Responsabilidades:
1. Aplica DDL de nuevas tablas si no existen (`ticker_mappings`, `lot_assignments`)
2. Backfill de `ticker_mappings` desde el `TICKER_MAP` hardcodeado en el código (se corre una sola vez antes de eliminar el hardcode)
3. Verifica integridad referencial básica post-migración
4. Idempotente — puede correrse múltiples veces sin efecto secundario

**Regla de schema:** `schema.sql` define siempre el estado final (para usuarios nuevos). `migrate.py` lleva bases existentes a ese estado. Nunca alterar `schema.sql` para compatibilidad hacia atrás.

**Estrategia de release — orden obligatorio para el mantenedor del repo:**
El `TICKER_MAP` en `snapshot.py` contiene ISINs del portafolio personal del mantenedor. Si se elimina el map del código antes de migrar la DB, la información nunca llega al repo. El flujo correcto es:

1. Correr `python3 tools/migrate.py` localmente → verifica que `ticker_mappings` quedó populada
2. Eliminar `TICKER_MAP` de `snapshot.py` (el código ya no lo referencia tras el refactor)
3. Verificar con `git diff` que ningún ISIN personal queda en el código trackeado
4. Solo entonces hacer push / publicar el repo

Este orden garantiza que el historial de git nunca contiene el mapa personal (asumiendo que `TICKER_MAP` se introdujo en un commit privado o se squashea antes de publicar).

---

## 3. Formatos de Ingestión y Contrato de Datos

### Formatos soportados

| Formato | Brokers conocidos |
|---------|------------------|
| PDF estado de cuenta | IBKR, Scalable Capital, Fidelity |
| Screenshot | Cualquier broker — Claude extrae visualmente |

La skill de ingestión no hace parsing directo — Claude lee el documento y estructura los datos. La skill define el esquema de salida normalizado que Claude debe producir.

### Esquema normalizado de salida (por transacción)

```
isin, name, type (etf/stock/bond/...), security_currency,  ← security
date, tx_type (buy/sell/dividend/...), broker,
quantity, price, tx_currency, total, fee, exchange,
notes, source_file
```

Este esquema es el contrato entre Claude (extracción) y `insert.py` (persistencia). La skill lo define explícitamente — Claude no improvisa campos. `security_currency` es la moneda de denominación del instrumento (ej. USD para IWDA.L); `tx_currency` es la moneda de la transacción específica (puede diferir si el broker liquida en otra moneda).

### Estrategia anti-duplicados

`insert.py` detecta duplicados por `(security_id, date, type, broker, quantity, price)` antes de insertar. Si detecta un probable duplicado, informa al usuario y pide confirmación antes de proceder. No hay inserción silenciosa.

---

## 4. Flujos de Usuario

### Flujo 1 — Setup inicial (usuario nuevo)

```
1. Clonar el repo
2. pip install -r requirements.txt
3. Abrir Claude Code en portfolio/
4. /setup → Claude verifica Python 3.11+, dependencias,
   crea portfolio.db desde schema.sql, confirma listo
```

Para usuarios con DB existente: `/setup` detecta la DB y corre `tools/migrate.py` automáticamente.

### Flujo 2 — Ingestión de transacciones

```
1. Usuario le pasa a Claude un PDF o screenshot del broker
2. /ingest → Claude extrae transacciones según esquema normalizado
3. Para cada security nuevo:
   a. Llama resolve_ticker.py con isin + exchange del broker
   b. Si auto-resolve tiene ambigüedad → Claude pregunta al usuario → llama de nuevo con exchange elegido
   c. Si Yahoo falla → Claude pregunta al usuario directamente por el ticker
4. Llama load_fx.py con fechas y pares de monedas necesarios
   → Si falla una API → imprime instrucciones de descarga manual
5. Llama insert.py — verifica duplicados antes de insertar
6. Confirma resumen: "X transacciones insertadas, Y tickers resueltos, Z ya existían"
```

### Flujo 3 — Snapshot de posiciones

```
/snapshot → Claude llama python3 tools/snapshot.py
  → lee ticker_mappings de DB
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

## 5. Definition of Done

| Flujo | Criterio de aceptación |
|-------|----------------------|
| **Setup** | Usuario nuevo clona repo → `/setup` → DB creada sin errores → `/snapshot` retorna "no hay posiciones" sin crashear |
| **Setup (DB existente)** | `migrate.py` corre sin errores → `ticker_mappings` populada desde TICKER_MAP anterior → `/snapshot` produce mismo resultado que antes de migrar |
| **Ingest — happy path** | PDF de broker conocido → todas las transacciones insertadas → tickers resueltos automáticamente → FX cargado → sin duplicados |
| **Ingest — ambigüedad de ticker** | ISIN con múltiples plazas → Claude presenta opciones → usuario elige → ticker guardado como `manual` → ingestión completa |
| **Ingest — fallo TRM** | API Banrep falla → mensaje con URL + pasos manuales → usuario descarga → `load_trm.py` → ingestión completa |
| **Ingest — reingestión** | Mismo PDF corrido dos veces → segunda vez: "X ya existían, 0 insertadas" sin duplicados |
| **Snapshot** | Ninguna referencia a `TICKER_MAP` en código → todos los tickers vienen de DB → mismas posiciones (ISINs, cantidades, costos FIFO) que pre-refactor; valores de mercado con tolerancia ±1% por precios live |
| **Tax** | Tax report con lote específico asignado produce resultado correcto → validado contra cálculo manual |

---

## 6. Privacidad y Datos

### Qué va en git y qué no

| Artefacto | En git | Razón |
|-----------|--------|-------|
| `schema.sql` | ✅ | Estructura genérica, sin datos |
| `tools/*.py` | ✅ | Código sin ISINs ni cantidades hardcodeadas |
| `.claude/skills/*.md` | ✅ | Instrucciones genéricas, rutas relativas |
| `queries/*.sql` | ✅ | Queries genéricas |
| `requirements.txt` | ✅ | Dependencias sin info personal |
| `README.md` | ✅ | Documentación pública |
| `portfolio.db` | ❌ | Contiene transacciones reales — `.gitignore` |
| `*.db-shm`, `*.db-wal` | ❌ | Ya en `.gitignore` |

### Cambios de privacidad al código existente

- `TICKER_MAP` en `snapshot.py` — eliminado después de correr `migrate.py` (backfill a DB)
- Referencias a brokers específicos en nombres de queries — generalizadas
- `tax_report.py` — se documenta explícitamente como Colombia-specific, sin cambios de lógica
- Skills: rutas absolutas eliminadas, reemplazadas por rutas relativas desde repo root

### README — contenido mínimo

1. Requisitos: Python 3.11+, Claude Code, Anthropic API key
2. Setup en 3 pasos: clonar → `pip install -r requirements.txt` → `/setup`
3. Cómo ingestar: pasar PDF/screenshot a Claude y correr `/ingest`
4. Comandos disponibles: `/snapshot`, `/tax <año>`
5. Nota explícita: tax report hardcodeado para Colombia (Ganancia Ocasional / Renta Ordinaria, TRM Banrep, UVT DIAN)
6. Cómo proceder si un ticker no se resuelve automáticamente
7. Fallback manual para TRM con URL de Banrep

---

## 7. Decisiones de Diseño

| Decisión | Alternativas consideradas | Razón |
|----------|--------------------------|-------|
| PK `ticker_mappings` = `(isin, exchange)` | Solo isin, (isin, currency) | Un ISIN puede cotizar en varias plazas con diferente moneda; exchange es el discriminador natural |
| Exchange en MIC (ISO 10383) | Abreviaciones libres del broker | Canon único evita colisiones LSE/XLON; resolve_ticker normaliza antes de insertar; si broker no trae exchange → resolución manual |
| Orden de release: migrate → borrar TICKER_MAP → push | Eliminar TICKER_MAP antes de migrar | Garantiza que ISINs personales no llegan al historial de git del repo público |
| Auto-resolve Yahoo → fallback manual | Seed file en repo, solo manual | Sin seed file que mantener; Yahoo cubre la mayoría; manual como red de seguridad |
| FX auto-fetch con degradación elegante | Solo manual, solo auto | Automatiza el happy path sin bloquear al usuario si la API falla |
| TRM: URL Banrep explícita en fallback | Instrucción genérica | El usuario sabe exactamente qué hacer — reduce fricción del fallback manual |
| `migrate.py` separado de `schema.sql` | ALTER TABLE en schema.sql | schema.sql = estado final siempre; migrate.py = transición. Usuarios nuevos y existentes tienen rutas limpias |
| Anti-duplicados en insert.py | En skill, en DB constraint | El script es la fuente de verdad — la lógica vive donde se ejecuta |
| Setup mínimo (no wizard) | Wizard interactivo guiado | Usuario técnico puede editar archivos — no necesita wizard; simplicidad > UX elaborado |
| WAT pattern estricto | Lógica en skills | Debuggeable, testeable, reutilizable sin Claude |
| Skills en repo portfolio | Solo en personal-assistant | Repo autónomo — cualquier usuario lo clona y funciona |
| Tax report Colombia-only hardcodeado | Pluggable multi-país | YAGNI — audiencia es colombiana, over-engineering innecesario |
