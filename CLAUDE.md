# CLAUDE.md — Portfolio Tracker

Este repo es un tracker de portafolio de inversiones personal para colombianos.
Self-hosted, operado vía agente de IA (Claude Code, GitHub Copilot, etc.).

## Qué hace

- Ingesta transacciones desde PDFs o screenshots de brokers
- Mantiene un portafolio en SQLite con costo base FIFO y asignación de lote específico
- Muestra posiciones con precios live de Yahoo Finance
- Genera reporte de renta para Colombia (Ganancia Ocasional / Renta Ordinaria)

## Skills disponibles

El agente detecta automáticamente qué skill usar según el contexto:

| Intención del usuario | Skill activada |
|-----------------------|---------------|
| "quiero agregar mis acciones", "tengo un PDF del broker" | `ingest` |
| "muéstrame mi portafolio", "cuánto llevo ganado" | `snapshot` |
| "reporte de impuestos", "declaración de renta 2024" | `tax` |
| "setup", "inicializar", problemas con la DB | `setup` |

No es necesario usar slash commands — basta con describir lo que quieres.

## Arquitectura

```
portfolio/
├── schema.sql          # Estado final del schema (fuente de verdad para nuevos usuarios)
├── portfolio.db        # DB local — en .gitignore, nunca se sube
├── tools/
│   ├── fifo.py         # Motor FIFO compartido
│   ├── snapshot.py     # Posiciones con precios live (lee tickers de ticker_mappings)
│   ├── tax_report.py   # Reporte renta Colombia (hardcodeado: TRM Banrep, UVT DIAN)
│   ├── insert.py       # Insertar securities/transacciones, detecta duplicados
│   ├── resolve_ticker.py # ISIN+exchange → Yahoo ticker, con cache en DB
│   ├── load_fx.py      # Auto-fetch EUR/USD (ECB), TRM (Banrep), con fallback manual
│   ├── migrate.py      # Migrar DB existente al schema actual
│   └── assign_lot.py   # Asignar lote específico a una venta
├── queries/            # SQL fijo por caso de uso
└── .claude/skills/     # Skills del agente (ingest, snapshot, tax, setup)
```

## Convenciones clave

- Fechas: ISO 8601 (`YYYY-MM-DD`) en toda la DB
- Moneda: la del instrumento en su plaza de cotización (no la cuenta del broker)
- `source_file`: nombre del PDF original — trazabilidad de cada transacción
- Foreign keys activas en todas las conexiones (`PRAGMA foreign_keys = ON`)
- DB path resuelto desde `__file__` en cada script — funciona desde cualquier directorio
