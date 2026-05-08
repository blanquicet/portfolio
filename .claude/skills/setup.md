---
name: setup
description: Initialize portfolio.db for a new user, or migrate an existing DB to the current schema.
---

# Portfolio Setup

## Steps

1. Check Python version:
```bash
python3 --version
```
If below 3.11, stop and tell the user to upgrade.

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
python3 tools/migrate.py
```
Show the full output to the user. If it exits with an error, show the error and stop.

3. Verify DB is usable:
```bash
python3 tools/snapshot.py 2>&1 | head -5
```
Expected: either a portfolio table or "no positions" message — no crash.

4. Confirm to the user: "Setup complete. Commands available: /ingest, /snapshot, /tax <year>"
