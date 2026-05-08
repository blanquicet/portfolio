---
name: ingest
description: "Ingestar transacciones de un broker — úsame cuando el usuario quiera agregar acciones, importar un extracto, o subir un PDF/screenshot de su broker."
---

# Portfolio Ingest

> **Python:** Usa el binario más reciente disponible: `python3.13`, `python3.12`, `python3.11`, o `python3` si ya es ≥3.11. Substitúyelo donde veas `python3` en los comandos.

The user has provided a PDF or screenshot from their broker.

## Precondition — verificar DB

Antes de extraer transacciones, verifica que la DB esté lista:

```bash
ls portfolio.db 2>/dev/null && echo "EXISTS" || echo "NEW"
```

- Si dice `NEW`: crea la DB primero ejecutando estos comandos, luego continúa con Step 1:
  ```bash
  <python> -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')"
  sqlite3 portfolio.db < schema.sql && echo "DB created OK"
  ```
  (donde `<python>` es el binario python3.11+ disponible — ver abajo)
- Si dice `EXISTS`: continúa directamente con Step 1.

## Step 1 — Extract transactions

Read the document carefully. Extract ALL transactions using this exact schema:

**Per security (insert once per unique ISIN):**
- `isin` — ISIN code (e.g., IE00B4L5Y983)
- `name` — full security name
- `type` — one of: `etf`, `stock`, `bond`, `cdt`, `crypto_etp`, `fund`
- `security_currency` — currency the instrument is denominated in (e.g., USD for IWDA.L even if bought via EUR account)

**Per transaction:**
- `date` — ISO 8601 (YYYY-MM-DD)
- `tx_type` — one of: `buy`, `sell`, `dividend`, `fee`, `transfer_in`, `transfer_out`, `vesting`, `sell_to_cover`, `split`, `interest`
- `broker` — broker name (e.g., `ibkr`, `scalable`, `fidelity`)
- `quantity` — number of shares/units (always positive)
- `price` — price per unit in `tx_currency`
- `tx_currency` — currency of the transaction (may differ from `security_currency`)
- `total` — total transaction value in `tx_currency`
- `fee` — commission/fee in `tx_currency` (0 if none)
- `exchange` — exchange where traded (e.g., LSE, NASDAQ, XETRA) — use broker's label
- `notes` — any relevant note (optional)
- `source_file` — filename of the document provided

Present the extracted data as a structured list for user review before inserting.

## Step 2 — User confirms extraction

Show the extracted transactions. Ask: "Does this look right? I'll proceed to insert."

## Step 3 — Insert securities

For each unique ISIN:
```bash
python3 tools/insert.py security '{"isin":"<isin>","name":"<name>","type":"<type>","currency":"<security_currency>"}'
```

## Step 4 — Resolve tickers

For each unique ISIN, call:
```bash
python3 tools/resolve_ticker.py <isin> <exchange>
```

**Interpret exit codes:**
- Exit 0: prints `TICKER|CURRENCY|SOURCE` → ticker resolved, continue
- Exit 1: ambiguous → the script printed numbered options on stderr → ask the user which exchange to use → re-call with that exchange as second argument
- Exit 2: exchange missing → ask the user which exchange the instrument trades on → re-call with that exchange
- Exit 3: Yahoo failed → ask the user for the Yahoo Finance ticker and currency directly, then save it manually:
  ```bash
  python3 tools/insert.py query "INSERT OR REPLACE INTO ticker_mappings (isin, exchange, ticker, currency, source, verified_at) VALUES ('<isin>', '<exchange>', '<ticker>', '<currency>', 'manual', datetime('now'))"
  ```

## Step 5 — Load FX rates

Collect all unique transaction dates and currency pairs needed (any non-USD currency involved):
```bash
python3 tools/load_fx.py --dates <date1>,<date2>,...  --pairs EUR/USD,USD/COP,GBP/USD
```
Only include pairs actually needed for the transactions being ingested.

If the script prints a manual fallback message (TRM or ECB), show it to the user and wait for them to perform the manual step before continuing.

## Step 6 — Insert transactions

For each transaction:
```bash
python3 tools/insert.py transaction '{"isin":"<isin>","date":"<date>","type":"<tx_type>","broker":"<broker>","quantity":<qty>,"price":<price>,"currency":"<tx_currency>","total":<total>,"fee":<fee>,"exchange":"<exchange>","notes":"<notes>","source_file":"<source_file>"}'
```

**If exit code 2 (duplicate detected):** Show the user the duplicate warning from stderr. Ask: "This looks like a duplicate — insert anyway? (yes/no)". If yes, re-run the same command with `--force` appended. If no, skip.

## Step 7 — Summary

Report: "X transactions inserted, Y tickers resolved (Z new), W already existed (duplicates skipped)."
