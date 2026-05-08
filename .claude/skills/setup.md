---
name: setup
description: Initialize portfolio.db for a new user, or migrate an existing DB to the current schema.
---

# Portfolio Setup

## Steps

1. Encuentra un Python 3.11+ disponible:

```bash
python3 --version && python3.13 --version 2>/dev/null; python3.12 --version 2>/dev/null; python3.11 --version 2>/dev/null
```

Elige el comando de mayor versión disponible (≥ 3.11). Puede ser `python3.13`, `python3.12`, `python3.11`, o `python3` si ya apunta a 3.11+. Guarda ese comando — úsalo en todos los pasos siguientes en lugar de `python3`.

Si ninguno es ≥ 3.11, indica al usuario que instale Python 3.11+ (ej. `brew install python@3.12`) y detente.

2. Check if `portfolio.db` exists:
```bash
ls portfolio.db 2>/dev/null && echo "EXISTS" || echo "NEW"
```

### If NEW — create DB from schema:
```bash
sqlite3 portfolio.db < schema.sql && echo "DB created OK"
```
Confirm: "portfolio.db created. Ready to use."

### If EXISTS — run migration:
```bash
<python> tools/migrate.py
```
(reemplaza `<python>` con el comando encontrado en el paso 1)

Show the full output to the user. If it exits with an error, show the error and stop.

3. Verify DB is usable:
```bash
<python> tools/snapshot.py 2>&1 | head -5
```
Expected: either a portfolio table or "no positions" message — no crash.

4. Confirm to the user: "Setup complete. Commands available: /ingest, /snapshot, /tax <year>"
