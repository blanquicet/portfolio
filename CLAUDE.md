# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A personal investment portfolio tracker backed by a single SQLite database (`portfolio.db`). All tools are standalone Python 3 scripts that read/write the DB directly. There is no web server, no ORM, and no build step.

## Common commands

```bash
# View current positions with live prices and unrealized P&L
python3 tools/snapshot.py              # all brokers
python3 tools/snapshot.py ibkr        # IBKR only
python3 tools/snapshot.py fidelity    # Fidelity only

# Tax report for Colombia renta declaration
python3 tools/tax_report.py           # fiscal year 2025
python3 tools/tax_report.py 2024      # another year
python3 tools/tax_report.py --detail  # show individual FIFO lots

# Insert data
python3 tools/insert.py security '<json>'
python3 tools/insert.py transaction '<json>'
python3 tools/insert.py query '<sql>'

# Load FX rates
python3 tools/load_trm.py trm_data.txt         # USD/COP from Banco de la República
python3 tools/load_eurusd.py --download         # EUR/USD from ECB API

# Raw SQL queries
sqlite3 portfolio.db < queries/snapshot.sql
sqlite3 portfolio.db "SELECT * FROM v_transactions LIMIT 10;"
```

## Architecture

### Database (`portfolio.db` / `schema.sql`)

Three core tables:
- **`securities`** — one row per instrument (ISIN, name, type, currency)
- **`transactions`** — every trade event (buy, sell, vesting, split, transfer_in/out, dividend, fee, sell_to_cover, interest)
- **`fx_rates`** — historical daily rates; stores EUR/USD (from ECB) and USD/COP (TRM from Banco de la República)

Two views: `v_transactions` (joined readable view) and `v_positions` (net shares per ISIN per broker).

### FIFO engine (`tools/fifo.py`)

Shared module imported by both `snapshot.py` and `tax_report.py`. Key rules:
- `buy` / `vesting` → add lot to queue
- `sell` → consume lots oldest-first (FIFO)
- `sell_to_cover` → does **not** consume FIFO lots; cost = sale price, gain = $0 (RSU vest value is already in employer cost basis)
- `transfer_in/out` → skipped by FIFO; FOP transfers preserve original cost basis

`build_queues()` returns a dict of `{isin: FifoQueue}` loaded from the DB.

### Snapshot (`tools/snapshot.py`)

Uses `yfinance` for live prices. ISIN → Yahoo ticker mapping is hardcoded in `TICKER_MAP`. Currency conversion: EUR prices × EURUSD rate; all market values summed in USD.

### Tax report (`tools/tax_report.py`)

Colombian tax rules applied on top of FIFO lots:
- Holding > 730 days → *Ganancia Ocasional* (15% flat, excluded from exógena)
- Holding ≤ 730 days → *Renta Ordinaria* (progressive rate, included in exógena)

FX chain: USD/COP via TRM; EUR/COP derived as EUR/USD × TRM.

### Skills (`.claude/skills/`)

Custom Claude Code skills for common workflows (e.g., `snapshot` skill runs `snapshot.py` and formats output). Skills are invoked via the `Skill` tool, not directly.

## Key conventions

- All currency stored per transaction as the **instrument's trading currency**, not the broker account currency (e.g., an EUR-quoted ETF stays EUR even when held at a USD-account broker).
- Dates are ISO 8601 strings (`YYYY-MM-DD`) throughout.
- `source_file` column on transactions records the original import filename for traceability.
- Foreign keys are enforced (`PRAGMA foreign_keys = ON` in every connection).
- The absolute DB path is resolved from `__file__` in each tool (`../portfolio.db`), so scripts work from any working directory.
