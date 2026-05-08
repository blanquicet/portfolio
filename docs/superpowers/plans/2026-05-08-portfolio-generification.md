# Portfolio Tracker Generification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the portfolio repo so any Colombian investor can clone it and use it without modifying code — moving personal data out of source files and into the local database.

**Architecture:** WAT pattern throughout — skills coordinate, `tools/` scripts execute all logic, `queries/` holds SQL. New scripts `resolve_ticker.py`, `load_fx.py`, and `migrate.py` replace hardcoded data structures. `TICKER_MAP` moves from `snapshot.py` into the `ticker_mappings` DB table. Skills move into the portfolio repo itself with relative paths.

**Tech Stack:** Python 3.11+, SQLite (sqlite3 stdlib), yfinance, requests, Claude Code skills (.md), pytest for unit tests.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `schema.sql` | Modify | Add `ticker_mappings` table DDL |
| `tools/snapshot.py` | Modify | Remove `TICKER_MAP`; read tickers from `ticker_mappings` DB table |
| `tools/resolve_ticker.py` | Create | ISIN+exchange → ticker; DB cache → Yahoo auto → manual fallback |
| `tools/load_fx.py` | Create | Auto-fetch EUR/USD (ECB), GBP/USD (ECB), TRM (Banrep) with graceful degradation |
| `tools/migrate.py` | Create | Idempotent migration: DDL + backfill `ticker_mappings` from TICKER_MAP |
| `tools/insert.py` | Modify | Add duplicate detection before insert |
| `.claude/skills/setup.md` | Create | Verify Python + create/migrate DB |
| `.claude/skills/ingest.md` | Create | Ingest PDF/screenshot per normalized schema + ticker resolve + FX load |
| `.claude/skills/snapshot.md` | Modify | Remove absolute path; use relative path from repo root |
| `requirements.txt` | Create | Pinned Python dependencies |
| `README.md` | Create | Setup in 3 steps + command reference |
| `tests/test_resolve_ticker.py` | Create | Unit tests for resolve_ticker (DB cache hit, MIC normalization, missing exchange) |
| `tests/test_load_fx.py` | Create | Unit tests for load_fx (gap detection, fallback message) |
| `tests/test_migrate.py` | Create | Unit tests for migrate (idempotency, backfill) |
| `tests/test_insert_dedup.py` | Create | Unit tests for duplicate detection in insert.py |

---

## Task 1: Add `ticker_mappings` to schema.sql

**Files:**
- Modify: `schema.sql`

- [ ] **Step 1: Add the DDL block**

Open `schema.sql` and append after the `lot_assignments` table:

```sql
CREATE TABLE IF NOT EXISTS ticker_mappings (
    isin         TEXT NOT NULL,
    exchange     TEXT NOT NULL,   -- ISO MIC: XLON, XPAR, XNAS, XNYS, XETR, etc.
    ticker       TEXT NOT NULL,
    currency     TEXT NOT NULL,   -- USD, EUR, GBP, COP
    source       TEXT NOT NULL CHECK(source IN ('auto', 'manual')),
    verified_at  TEXT,            -- ISO 8601
    PRIMARY KEY (isin, exchange)
);
```

- [ ] **Step 2: Verify schema parses cleanly**

```bash
cd /Users/melendex/Documents/src/portfolio
sqlite3 /tmp/test_schema.db < schema.sql && echo "OK"
```

Expected: `OK` with no errors.

- [ ] **Step 3: Commit**

```bash
git add schema.sql
git commit -m "feat: add ticker_mappings table to schema"
```

---

## Task 2: Create `tools/resolve_ticker.py`

**Files:**
- Create: `tools/resolve_ticker.py`
- Create: `tests/test_resolve_ticker.py`

### MIC normalization table (used in both tests and implementation)

```python
MIC_ALIASES = {
    "LSE": "XLON",
    "PA": "XPAR", "XPAR": "XPAR",
    "NASDAQ": "XNAS",
    "NYSE": "XNYS",
    "XETRA": "XETR",
    "XLON": "XLON", "XNAS": "XNAS", "XNYS": "XNYS", "XETR": "XETR",
}
```

- [ ] **Step 1: Write the failing tests**

Create `tests/test_resolve_ticker.py`:

```python
"""Tests for resolve_ticker.py — DB cache, MIC normalization, missing exchange."""
import sqlite3, sys, os, json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# We import only the pure functions, not the CLI entry point
from tools.resolve_ticker import normalize_exchange, lookup_db, save_mapping

@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE ticker_mappings (
            isin TEXT NOT NULL,
            exchange TEXT NOT NULL,
            ticker TEXT NOT NULL,
            currency TEXT NOT NULL,
            source TEXT NOT NULL,
            verified_at TEXT,
            PRIMARY KEY (isin, exchange)
        )
    """)
    conn.commit()
    return conn

def test_normalize_exchange_lse():
    assert normalize_exchange("LSE") == "XLON"

def test_normalize_exchange_pa():
    assert normalize_exchange("PA") == "XPAR"

def test_normalize_exchange_already_mic():
    assert normalize_exchange("XNAS") == "XNAS"

def test_normalize_exchange_unknown_returns_upper():
    # Unknown aliases are uppercased and passed through
    assert normalize_exchange("unknown") == "UNKNOWN"

def test_lookup_db_hit(db):
    db.execute(
        "INSERT INTO ticker_mappings VALUES (?,?,?,?,?,?)",
        ("IE00B4L5Y983", "XLON", "IWDA.L", "USD", "manual", "2024-01-01")
    )
    db.commit()
    result = lookup_db(db, "IE00B4L5Y983", "XLON")
    assert result == {"ticker": "IWDA.L", "currency": "USD", "source": "manual"}

def test_lookup_db_miss(db):
    result = lookup_db(db, "IE00B4L5Y983", "XLON")
    assert result is None

def test_lookup_db_wrong_exchange(db):
    db.execute(
        "INSERT INTO ticker_mappings VALUES (?,?,?,?,?,?)",
        ("IE00B4L5Y983", "XLON", "IWDA.L", "USD", "manual", "2024-01-01")
    )
    db.commit()
    result = lookup_db(db, "IE00B4L5Y983", "XPAR")
    assert result is None

def test_save_mapping(db):
    save_mapping(db, "IE00B4L5Y983", "XLON", "IWDA.L", "USD", "auto")
    result = lookup_db(db, "IE00B4L5Y983", "XLON")
    assert result is not None
    assert result["ticker"] == "IWDA.L"
    assert result["source"] == "auto"

def test_save_mapping_idempotent(db):
    save_mapping(db, "IE00B4L5Y983", "XLON", "IWDA.L", "USD", "auto")
    save_mapping(db, "IE00B4L5Y983", "XLON", "IWDA.L", "USD", "auto")  # no error
    rows = db.execute("SELECT COUNT(*) FROM ticker_mappings").fetchone()[0]
    assert rows == 1
```

- [ ] **Step 2: Run tests — expect failures**

```bash
cd /Users/melendex/Documents/src/portfolio
python -m pytest tests/test_resolve_ticker.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError` or `ImportError` — `resolve_ticker` doesn't exist yet.

- [ ] **Step 3: Implement `tools/resolve_ticker.py`**

```python
#!/usr/bin/env python3
"""Resolve ISIN + exchange → Yahoo Finance ticker.

Usage (called by skill):
    python3 tools/resolve_ticker.py <isin> <exchange>
        → prints: TICKER|CURRENCY|SOURCE
        → exit 0 on success

    python3 tools/resolve_ticker.py <isin>
        → exchange missing: exits 2, prints instructions for skill

Exit codes:
    0  resolved (from DB or Yahoo)
    1  ambiguous — printed numbered options, skill must re-call with chosen exchange
    2  missing exchange — skill must ask user which exchange before calling again
    3  unresolvable — Yahoo failed, no DB entry; skill must ask user directly
"""
import sqlite3, sys, os, datetime

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

MIC_ALIASES = {
    "LSE": "XLON",
    "PA": "XPAR", "XPAR": "XPAR",
    "NASDAQ": "XNAS",
    "NYSE": "XNYS",
    "XETRA": "XETR",
    "XLON": "XLON", "XNAS": "XNAS", "XNYS": "XNYS", "XETR": "XETR",
}


def normalize_exchange(raw: str) -> str:
    """Map broker abbreviation → ISO MIC. Unknown values are uppercased."""
    return MIC_ALIASES.get(raw.upper(), raw.upper())


def lookup_db(conn, isin: str, exchange: str) -> dict | None:
    """Return ticker dict if (isin, exchange) exists in ticker_mappings, else None."""
    row = conn.execute(
        "SELECT ticker, currency, source FROM ticker_mappings WHERE isin = ? AND exchange = ?",
        (isin, exchange)
    ).fetchone()
    if row:
        return {"ticker": row[0], "currency": row[1], "source": row[2]}
    return None


def save_mapping(conn, isin: str, exchange: str, ticker: str, currency: str, source: str):
    """Upsert a row into ticker_mappings."""
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT OR REPLACE INTO ticker_mappings (isin, exchange, ticker, currency, source, verified_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (isin, exchange, ticker, currency, source, now)
    )
    conn.commit()


def yahoo_search(isin: str) -> list[dict]:
    """
    Attempt to find ticker(s) for an ISIN via Yahoo Finance unofficial search.
    Returns list of {ticker, currency, exchange_mic} dicts.
    Empty list if search fails or no results.

    NOTE: Uses undocumented Yahoo endpoint — may break. Failure is expected and handled.
    """
    try:
        import requests
        url = "https://query2.finance.yahoo.com/v1/finance/search"
        params = {"q": isin, "quotesCount": 10, "newsCount": 0}
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, params=params, headers=headers, timeout=8)
        r.raise_for_status()
        quotes = r.json().get("quotes", [])
        results = []
        for q in quotes:
            sym = q.get("symbol", "")
            ccy = q.get("currency", "USD")
            exch = q.get("exchange", "")
            mic = normalize_exchange(exch)
            results.append({"ticker": sym, "currency": ccy, "exchange_mic": mic, "exchange_raw": exch})
        return results
    except Exception:
        return []


def run(isin: str, exchange_raw: str | None):
    conn = sqlite3.connect(DB)

    if not exchange_raw:
        print("ERROR: exchange not provided.", file=sys.stderr)
        print("HINT: Ask the user which exchange this instrument trades on (e.g., LSE, NASDAQ, XETRA).", file=sys.stderr)
        conn.close()
        sys.exit(2)

    exchange = normalize_exchange(exchange_raw)

    # 1. DB cache hit
    cached = lookup_db(conn, isin, exchange)
    if cached:
        print(f"{cached['ticker']}|{cached['currency']}|{cached['source']}")
        conn.close()
        sys.exit(0)

    # 2. Yahoo search
    candidates = yahoo_search(isin)

    if not candidates:
        print(f"ERROR: Could not resolve {isin} via Yahoo Finance.", file=sys.stderr)
        print("HINT: Ask the user for the Yahoo Finance ticker directly.", file=sys.stderr)
        conn.close()
        sys.exit(3)

    # Filter by exchange if possible
    matching = [c for c in candidates if c["exchange_mic"] == exchange]

    if len(matching) == 1:
        c = matching[0]
        save_mapping(conn, isin, exchange, c["ticker"], c["currency"], "auto")
        print(f"{c['ticker']}|{c['currency']}|auto")
        conn.close()
        sys.exit(0)

    if len(matching) == 0:
        # No match for given exchange — show all candidates
        matching = candidates

    # Ambiguous: print options for skill to present to user
    print(f"AMBIGUOUS: {len(matching)} candidates for {isin}. User must choose:", file=sys.stderr)
    for i, c in enumerate(matching, 1):
        print(f"  {i}. {c['ticker']} ({c['exchange_raw']}/{c['exchange_mic']}) [{c['currency']}]", file=sys.stderr)
    print("HINT: Re-call with the MIC exchange the user selects.", file=sys.stderr)
    conn.close()
    sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: resolve_ticker.py <isin> [<exchange>]", file=sys.stderr)
        sys.exit(2)
    isin_arg = sys.argv[1]
    exch_arg = sys.argv[2] if len(sys.argv) > 2 else None
    run(isin_arg, exch_arg)
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd /Users/melendex/Documents/src/portfolio
python -m pytest tests/test_resolve_ticker.py -v
```

Expected: all 8 tests PASS.

- [ ] **Step 5: Smoke test CLI**

```bash
cd /Users/melendex/Documents/src/portfolio
python3 tools/resolve_ticker.py IE00B4L5Y983 LSE
```

Expected: either `IWDA.L|USD|auto` (if Yahoo responds) or exit 3 with hint message. No crash.

- [ ] **Step 6: Commit**

```bash
git add tools/resolve_ticker.py tests/test_resolve_ticker.py
git commit -m "feat: add resolve_ticker.py — ISIN+exchange → ticker with DB cache and Yahoo fallback"
```

---

## Task 3: Create `tools/load_fx.py`

**Files:**
- Create: `tools/load_fx.py`
- Create: `tests/test_load_fx.py`

The existing `load_eurusd.py` and `load_trm.py` stay — they are still used for manual loading. `load_fx.py` is a new orchestrator that auto-fetches gaps and prints fallback instructions if an API fails.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_load_fx.py`:

```python
"""Tests for load_fx.py — gap detection and fallback messaging."""
import sqlite3, sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.load_fx import find_gaps, format_trm_fallback_message


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE fx_rates (
            date TEXT NOT NULL,
            from_currency TEXT NOT NULL,
            to_currency TEXT NOT NULL,
            rate REAL NOT NULL,
            PRIMARY KEY (date, from_currency, to_currency)
        )
    """)
    conn.commit()
    return conn


def test_find_gaps_all_missing(db):
    gaps = find_gaps(db, ["2024-01-02", "2024-01-03"], "EUR", "USD")
    assert gaps == ["2024-01-02", "2024-01-03"]


def test_find_gaps_none_missing(db):
    db.execute("INSERT INTO fx_rates VALUES ('2024-01-02','EUR','USD',1.10)")
    db.execute("INSERT INTO fx_rates VALUES ('2024-01-03','EUR','USD',1.11)")
    db.commit()
    gaps = find_gaps(db, ["2024-01-02", "2024-01-03"], "EUR", "USD")
    assert gaps == []


def test_find_gaps_partial(db):
    db.execute("INSERT INTO fx_rates VALUES ('2024-01-02','EUR','USD',1.10)")
    db.commit()
    gaps = find_gaps(db, ["2024-01-02", "2024-01-03"], "EUR", "USD")
    assert gaps == ["2024-01-03"]


def test_trm_fallback_message_contains_url():
    msg = format_trm_fallback_message(["2024-01-02", "2024-01-03"])
    assert "suameca.banrep.gov.co" in msg
    assert "load_trm.py" in msg
    assert "2024-01-02" in msg
```

- [ ] **Step 2: Run tests — expect failures**

```bash
cd /Users/melendex/Documents/src/portfolio
python -m pytest tests/test_load_fx.py -v 2>&1 | head -20
```

Expected: `ImportError` — `load_fx` doesn't exist yet.

- [ ] **Step 3: Implement `tools/load_fx.py`**

```python
#!/usr/bin/env python3
"""Auto-fetch FX gaps for a list of dates and currency pairs.

Usage (called by ingest skill):
    python3 tools/load_fx.py --dates 2024-01-02,2024-01-03 --pairs EUR/USD,USD/COP

Fetches missing rates from:
  EUR/USD, GBP/USD  →  ECB public API
  USD/COP (TRM)     →  Banrep API (unreliable — graceful degradation)

Prints what was loaded and what needs manual action. Always exits 0.
The skill is responsible for reading the output and asking the user to
perform any manual steps.
"""
import sqlite3, sys, os, datetime

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

ECB_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/"
    "D.{quote}.{base}.SP00.A"
    "?startPeriod={start}&endPeriod={end}&format=csvdata"
)
BANREP_URL = (
    "https://www.banrep.gov.co/es/estadisticas/tasas-de-cambio-peso-colombiano-frente-al-dolar"
)
TRM_FALLBACK_DOWNLOAD_URL = (
    "https://suameca.banrep.gov.co/estadisticas-economicas/informacionSerie/1/"
    "tasa_cambio_peso_colombiano_trm_dolar_usd"
)


# ── Pure functions (testable without DB) ──────────────────────────────────────

def find_gaps(conn, dates: list[str], from_ccy: str, to_ccy: str) -> list[str]:
    """Return dates that are missing from fx_rates for the given pair."""
    existing = set(
        row[0] for row in conn.execute(
            "SELECT date FROM fx_rates WHERE from_currency = ? AND to_currency = ? AND date IN ({})".format(
                ",".join("?" * len(dates))
            ),
            [from_ccy, to_ccy] + dates
        ).fetchall()
    )
    return [d for d in dates if d not in existing]


def format_trm_fallback_message(missing_dates: list[str]) -> str:
    """Return a human-readable fallback message for TRM manual download."""
    date_range = f"{missing_dates[0]} a {missing_dates[-1]}" if len(missing_dates) > 1 else missing_dates[0]
    return (
        f"\n⚠  No se pudo obtener TRM automáticamente para: {date_range}\n"
        f"   Descarga manual en:\n"
        f"   {TRM_FALLBACK_DOWNLOAD_URL}\n"
        f"   → Cambiar a vista 'Tabla' → Seleccionar fechas de interés → Descargar\n"
        f"   → Luego correr: python3 tools/load_trm.py <archivo.txt>\n"
    )


# ── Network fetchers ───────────────────────────────────────────────────────────

def fetch_ecb(from_ccy: str, to_ccy: str, dates: list[str]) -> list[tuple]:
    """Fetch rates from ECB API. Returns list of (date, from, to, rate) tuples."""
    try:
        import csv, io, requests
        start = min(dates)
        end = max(dates)
        # ECB uses quote/base notation: D.USD.EUR = USD per EUR (i.e., EUR→USD)
        url = ECB_URL.format(quote=to_ccy, base=from_ccy, start=start, end=end)
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        result = []
        for row in reader:
            try:
                result.append((row["TIME_PERIOD"], from_ccy, to_ccy, float(row["OBS_VALUE"])))
            except (KeyError, ValueError):
                continue
        return result
    except Exception:
        return []


def fetch_banrep_trm(dates: list[str]) -> list[tuple]:
    """
    Attempt to fetch TRM from Banrep. Unreliable — returns empty list on any failure.
    Returns list of (date, 'USD', 'COP', rate) tuples.
    """
    try:
        import requests
        # Banrep has a JSON endpoint (may change — failure is expected)
        start = min(dates).replace("-", "%2F").replace("-", "/")
        end = max(dates)
        url = (
            f"https://www.banrep.gov.co/es/trm?op=ajax"
            f"&fecha_inicio={min(dates)}&fecha_fin={end}"
        )
        headers = {"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"}
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        # Banrep returns [{fecha: "DD/MM/YYYY", valor: "3,456.78"}, ...]
        result = []
        for item in data:
            try:
                raw_date = item.get("fecha", "")
                parts = raw_date.split("/")
                if len(parts) == 3:
                    iso_date = f"{parts[2]}-{parts[1]}-{parts[0]}"
                    rate_str = item.get("valor", "").replace(",", "").replace(".", "")
                    # Banrep may use period as thousands sep and comma as decimal
                    rate_str_clean = item.get("valor", "").replace(".", "").replace(",", ".")
                    rate = float(rate_str_clean)
                    result.append((iso_date, "USD", "COP", rate))
            except (ValueError, AttributeError):
                continue
        return result
    except Exception:
        return []


# ── Orchestrator ──────────────────────────────────────────────────────────────

def insert_rates(conn, rows: list[tuple]):
    conn.executemany(
        "INSERT OR REPLACE INTO fx_rates (date, from_currency, to_currency, rate) VALUES (?, ?, ?, ?)",
        rows
    )
    conn.commit()


def run(dates: list[str], pairs: list[tuple[str, str]]):
    """
    For each (from_ccy, to_ccy) pair, find gaps in fx_rates and fill them.
    Prints a summary. Prints fallback instructions for anything it couldn't fill.
    """
    conn = sqlite3.connect(DB)
    loaded_total = 0
    manual_needed = []

    for from_ccy, to_ccy in pairs:
        gaps = find_gaps(conn, dates, from_ccy, to_ccy)
        if not gaps:
            print(f"  ✓ {from_ccy}/{to_ccy}: all {len(dates)} rates already in DB")
            continue

        print(f"  → {from_ccy}/{to_ccy}: {len(gaps)} gaps — fetching…", end=" ", flush=True)

        if (from_ccy, to_ccy) == ("USD", "COP"):
            rows = fetch_banrep_trm(gaps)
        else:
            rows = fetch_ecb(from_ccy, to_ccy, gaps)

        if rows:
            # Filter to only the dates we need
            needed_set = set(gaps)
            filtered = [r for r in rows if r[0] in needed_set]
            insert_rates(conn, filtered)
            loaded_total += len(filtered)
            remaining = find_gaps(conn, gaps, from_ccy, to_ccy)
            print(f"loaded {len(filtered)}" + (f", {len(remaining)} still missing" if remaining else ""))
            if remaining:
                manual_needed.append((from_ccy, to_ccy, remaining))
        else:
            print("failed")
            manual_needed.append((from_ccy, to_ccy, gaps))

    if manual_needed:
        print()
        for from_ccy, to_ccy, missing in manual_needed:
            if (from_ccy, to_ccy) == ("USD", "COP"):
                print(format_trm_fallback_message(missing))
            else:
                print(
                    f"⚠  {from_ccy}/{to_ccy}: {len(missing)} rates missing. "
                    f"Download from ECB and run: python3 tools/load_eurusd.py <file>"
                )

    conn.close()
    print(f"\n  FX summary: {loaded_total} rates loaded, {len(manual_needed)} pairs need manual action.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dates", required=True, help="Comma-separated ISO dates")
    parser.add_argument("--pairs", required=True, help="Comma-separated pairs like EUR/USD,USD/COP")
    args = parser.parse_args()

    date_list = [d.strip() for d in args.dates.split(",")]
    pair_list = [tuple(p.strip().split("/")) for p in args.pairs.split(",")]
    run(date_list, pair_list)
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd /Users/melendex/Documents/src/portfolio
python -m pytest tests/test_load_fx.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 5: Smoke test CLI**

```bash
cd /Users/melendex/Documents/src/portfolio
python3 tools/load_fx.py --dates 2024-01-02,2024-01-03 --pairs EUR/USD
```

Expected: either loads rates or prints a clear fallback message. No crash.

- [ ] **Step 6: Commit**

```bash
git add tools/load_fx.py tests/test_load_fx.py
git commit -m "feat: add load_fx.py — auto-fetch FX gaps with graceful degradation"
```

---

## Task 4: Create `tools/migrate.py`

**Files:**
- Create: `tools/migrate.py`
- Create: `tests/test_migrate.py`

`migrate.py` reads the `TICKER_MAP` from `snapshot.py` at runtime (before it's deleted) and backfills `ticker_mappings`. It must be idempotent.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_migrate.py`:

```python
"""Tests for migrate.py — idempotency, DDL application, backfill."""
import sqlite3, sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.migrate import apply_ddl, backfill_ticker_mappings, verify_integrity


@pytest.fixture
def old_db():
    """Simulate a DB that predates ticker_mappings."""
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
    """)
    conn.commit()
    return conn


def test_apply_ddl_creates_ticker_mappings(old_db):
    apply_ddl(old_db)
    # Table should now exist
    row = old_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ticker_mappings'"
    ).fetchone()
    assert row is not None


def test_apply_ddl_is_idempotent(old_db):
    apply_ddl(old_db)
    apply_ddl(old_db)  # second call should not raise
    row = old_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ticker_mappings'"
    ).fetchone()
    assert row is not None


SAMPLE_TICKER_MAP = {
    "IE00B4L5Y983": ("IWDA.L", "XLON", "USD"),
    "FR0000121014": ("MC.PA", "XPAR", "EUR"),
}


def test_backfill_inserts_entries(old_db):
    apply_ddl(old_db)
    backfill_ticker_mappings(old_db, SAMPLE_TICKER_MAP)
    count = old_db.execute("SELECT COUNT(*) FROM ticker_mappings").fetchone()[0]
    assert count == 2


def test_backfill_is_idempotent(old_db):
    apply_ddl(old_db)
    backfill_ticker_mappings(old_db, SAMPLE_TICKER_MAP)
    backfill_ticker_mappings(old_db, SAMPLE_TICKER_MAP)  # second run
    count = old_db.execute("SELECT COUNT(*) FROM ticker_mappings").fetchone()[0]
    assert count == 2  # no duplicates


def test_verify_integrity_passes_on_clean_db(old_db):
    apply_ddl(old_db)
    # No errors expected on an empty DB
    verify_integrity(old_db)  # should not raise
```

- [ ] **Step 2: Run tests — expect failures**

```bash
cd /Users/melendex/Documents/src/portfolio
python -m pytest tests/test_migrate.py -v 2>&1 | head -20
```

Expected: `ImportError` — `migrate` doesn't exist yet.

- [ ] **Step 3: Implement `tools/migrate.py`**

```python
#!/usr/bin/env python3
"""
Migrate portfolio.db to the current schema.

Responsibilities:
  1. Apply DDL for tables that may not exist (ticker_mappings, lot_assignments)
  2. Backfill ticker_mappings from TICKER_MAP in snapshot.py (run BEFORE deleting TICKER_MAP)
  3. Verify referential integrity post-migration
  4. Idempotent — safe to run multiple times

Usage:
    python3 tools/migrate.py

Release order (mantenedor del repo):
  1. python3 tools/migrate.py        ← run this first
  2. Verify ticker_mappings is populated
  3. Delete TICKER_MAP from snapshot.py
  4. git diff — confirm no ISINs remain in tracked code
  5. Push
"""
import sqlite3, sys, os, datetime

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

DDL_TICKER_MAPPINGS = """
CREATE TABLE IF NOT EXISTS ticker_mappings (
    isin         TEXT NOT NULL,
    exchange     TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    currency     TEXT NOT NULL,
    source       TEXT NOT NULL CHECK(source IN ('auto', 'manual')),
    verified_at  TEXT,
    PRIMARY KEY (isin, exchange)
);
"""

DDL_LOT_ASSIGNMENTS = """
CREATE TABLE IF NOT EXISTS lot_assignments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sell_id  INTEGER NOT NULL REFERENCES transactions(id),
    buy_id   INTEGER NOT NULL REFERENCES transactions(id),
    quantity REAL    NOT NULL CHECK(quantity > 0),
    UNIQUE(sell_id, buy_id)
);
"""

# MIC mapping for backfill — inferred from TICKER_MAP suffixes
_SUFFIX_TO_MIC = {
    ".L":  ("XLON", "USD"),
    ".PA": ("XPAR", "EUR"),
}
_DEFAULT_MIC = ("XNAS", "USD")  # US tickers have no suffix → NASDAQ/NYSE default


def _infer_exchange_currency(ticker: str) -> tuple[str, str]:
    """Infer MIC exchange and currency from Yahoo ticker suffix."""
    for suffix, (mic, ccy) in _SUFFIX_TO_MIC.items():
        if ticker.endswith(suffix):
            return mic, ccy
    return _DEFAULT_MIC


def apply_ddl(conn):
    """Apply missing DDL. Safe to call multiple times (IF NOT EXISTS)."""
    conn.executescript(DDL_TICKER_MAPPINGS + DDL_LOT_ASSIGNMENTS)
    conn.commit()


def backfill_ticker_mappings(conn, ticker_map: dict):
    """
    Insert entries from ticker_map into ticker_mappings if not already present.
    ticker_map format: {isin: (ticker, exchange_mic, currency)} OR {isin: ticker_str}
    Supports both the old TICKER_MAP format (isin→ticker_str) and the new tuple format.
    """
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    count = 0
    for isin, value in ticker_map.items():
        if isinstance(value, tuple):
            ticker, exchange, currency = value
        else:
            # Old format: value is just the ticker string
            ticker = value
            exchange, currency = _infer_exchange_currency(ticker)

        existing = conn.execute(
            "SELECT 1 FROM ticker_mappings WHERE isin = ? AND exchange = ?",
            (isin, exchange)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO ticker_mappings (isin, exchange, ticker, currency, source, verified_at) "
                "VALUES (?, ?, ?, ?, 'manual', ?)",
                (isin, exchange, ticker, currency, now)
            )
            count += 1

    conn.commit()
    return count


def verify_integrity(conn):
    """
    Basic post-migration checks. Raises AssertionError on failure.
    """
    # All transactions must reference a valid security
    orphans = conn.execute("""
        SELECT COUNT(*) FROM transactions t
        WHERE NOT EXISTS (SELECT 1 FROM securities s WHERE s.id = t.security_id)
    """).fetchone()[0]
    assert orphans == 0, f"Found {orphans} transactions with no matching security"


def main():
    if not os.path.exists(DB):
        print("ERROR: portfolio.db not found. Run /setup first.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB)

    print("1. Applying DDL…")
    apply_ddl(conn)
    print("   ✓ ticker_mappings and lot_assignments tables ensured")

    print("2. Backfilling ticker_mappings from snapshot.py TICKER_MAP…")
    try:
        # Import TICKER_MAP from snapshot at runtime (before it's deleted)
        sys.path.insert(0, os.path.dirname(__file__))
        from snapshot import TICKER_MAP
        inserted = backfill_ticker_mappings(conn, TICKER_MAP)
        total = conn.execute("SELECT COUNT(*) FROM ticker_mappings").fetchone()[0]
        print(f"   ✓ Inserted {inserted} new entries ({total} total in ticker_mappings)")
    except ImportError:
        print("   ⚠  TICKER_MAP not found in snapshot.py — skipping backfill (already migrated?)")

    print("3. Verifying integrity…")
    try:
        verify_integrity(conn)
        print("   ✓ Integrity checks passed")
    except AssertionError as e:
        print(f"   ✗ Integrity error: {e}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    conn.close()
    print("\n✅ Migration complete.")
    print("\nNext steps (if publishing the repo):")
    print("  1. Review ticker_mappings: python3 tools/insert.py query 'SELECT * FROM ticker_mappings'")
    print("  2. Delete TICKER_MAP from tools/snapshot.py")
    print("  3. Run: git diff — confirm no ISINs in tracked files")
    print("  4. Push")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd /Users/melendex/Documents/src/portfolio
python -m pytest tests/test_migrate.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/migrate.py tests/test_migrate.py
git commit -m "feat: add migrate.py — idempotent DDL + ticker_mappings backfill"
```

---

## Task 5: Refactor `tools/snapshot.py` — remove TICKER_MAP

**Files:**
- Modify: `tools/snapshot.py`
- Create: `tests/test_snapshot_db_tickers.py`

`snapshot.py` must read tickers from `ticker_mappings` in the DB instead of the hardcoded `TICKER_MAP`. The `fetch_prices` function signature does not change — only how `isin_to_ticker` is built.

- [ ] **Step 1: Write the failing test**

Create `tests/test_snapshot_db_tickers.py`:

```python
"""Test that snapshot.py reads tickers from DB, not from TICKER_MAP."""
import sqlite3, sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.snapshot import load_ticker_map_from_db


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE ticker_mappings (
            isin TEXT NOT NULL,
            exchange TEXT NOT NULL,
            ticker TEXT NOT NULL,
            currency TEXT NOT NULL,
            source TEXT NOT NULL,
            verified_at TEXT,
            PRIMARY KEY (isin, exchange)
        );
    """)
    conn.commit()
    return conn


def test_load_ticker_map_returns_isin_to_ticker(db):
    db.execute("INSERT INTO ticker_mappings VALUES ('IE00B4L5Y983','XLON','IWDA.L','USD','manual','2024-01-01')")
    db.execute("INSERT INTO ticker_mappings VALUES ('FR0000121014','XPAR','MC.PA','EUR','auto','2024-01-01')")
    db.commit()
    result = load_ticker_map_from_db(db)
    assert result["IE00B4L5Y983"] == "IWDA.L"
    assert result["FR0000121014"] == "MC.PA"


def test_load_ticker_map_empty_db(db):
    result = load_ticker_map_from_db(db)
    assert result == {}


def test_no_ticker_map_in_snapshot_module():
    """TICKER_MAP must not exist in snapshot.py after refactor."""
    import importlib
    import tools.snapshot as snap_module
    assert not hasattr(snap_module, "TICKER_MAP"), \
        "TICKER_MAP still present in snapshot.py — delete it after running migrate.py"
```

- [ ] **Step 2: Run tests — expect failure on the TICKER_MAP test**

```bash
cd /Users/melendex/Documents/src/portfolio
python -m pytest tests/test_snapshot_db_tickers.py -v 2>&1 | head -30
```

Expected: `test_no_ticker_map_in_snapshot_module` FAILS (TICKER_MAP still there), `load_ticker_map_from_db` tests FAIL (function doesn't exist yet).

- [ ] **Step 3: Add `load_ticker_map_from_db` to snapshot.py and remove TICKER_MAP**

In `tools/snapshot.py`:

a. **Add** this function after the imports section (before `SQL = ...`):

```python
def load_ticker_map_from_db(conn) -> dict:
    """Load {isin: ticker} from ticker_mappings table."""
    rows = conn.execute("SELECT isin, ticker FROM ticker_mappings").fetchall()
    return {row[0]: row[1] for row in rows}
```

b. **Delete** the entire `TICKER_MAP = { ... }` block (lines 34–78 in the current file).

c. **Update** `fetch_prices` to accept `ticker_map` as a parameter instead of using the global:

Change the function signature from:
```python
def fetch_prices(isins: list) -> tuple:
```
to:
```python
def fetch_prices(isins: list, ticker_map: dict) -> tuple:
```

And inside `fetch_prices`, change:
```python
    isin_to_ticker = {i: TICKER_MAP[i] for i in isins if i in TICKER_MAP}
```
to:
```python
    isin_to_ticker = {i: ticker_map[i] for i in isins if i in ticker_map}
```

And change the unmapped warning:
```python
    unmapped = [i for i in isins if i not in TICKER_MAP]
```
to:
```python
    unmapped = [i for i in isins if i not in ticker_map]
```

d. **Update** `run()` to load the ticker map from DB and pass it to `fetch_prices`:

In `run()`, after `fifo_queues, _ = build_queues(conn)` and before `conn.close()`, add:
```python
    ticker_map = load_ticker_map_from_db(conn)
```

Then change the `fetch_prices` call:
```python
    prices, display_map, fx = fetch_prices(isins, ticker_map)
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd /Users/melendex/Documents/src/portfolio
python -m pytest tests/test_snapshot_db_tickers.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Run migrate.py to populate DB before smoke testing**

```bash
cd /Users/melendex/Documents/src/portfolio
python3 tools/migrate.py
```

Expected output:
```
1. Applying DDL…
   ✓ ticker_mappings and lot_assignments tables ensured
2. Backfilling ticker_mappings from snapshot.py TICKER_MAP…
```

Wait — at this point `TICKER_MAP` has already been deleted in Step 3. **The correct order is:**

> **IMPORTANT:** `migrate.py` must be run against the real `portfolio.db` BEFORE deleting `TICKER_MAP`. In this plan we're refactoring the code — the actual migration of the real DB should be done as a one-time manual step:
>
> ```bash
> # Run this BEFORE the code changes in Step 3 go live on the real DB:
> python3 tools/migrate.py
> ```
>
> For testing purposes in this step, verify snapshot still works with a test DB that has ticker_mappings populated.

- [ ] **Step 6: Smoke test snapshot with real DB (if ticker_mappings already populated)**

```bash
cd /Users/melendex/Documents/src/portfolio
python3 tools/snapshot.py 2>&1 | head -20
```

Expected: same output as before refactor (positions, prices, P&L). If `ticker_mappings` is empty you'll see "0 tickers fetched" — that's expected until migrate.py has been run.

- [ ] **Step 7: Commit**

```bash
git add tools/snapshot.py tests/test_snapshot_db_tickers.py
git commit -m "refactor: snapshot.py reads tickers from DB — remove hardcoded TICKER_MAP"
```

---

## Task 6: Add duplicate detection to `tools/insert.py`

**Files:**
- Modify: `tools/insert.py`
- Create: `tests/test_insert_dedup.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_insert_dedup.py`:

```python
"""Tests for duplicate detection in insert.py."""
import sqlite3, sys, os, json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools.insert import find_duplicate, insert_transaction


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
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
        INSERT INTO securities (isin, name, type, currency)
        VALUES ('US5949181045', 'Microsoft', 'stock', 'USD');
    """)
    conn.commit()
    return conn


def _sec_id(db):
    return db.execute("SELECT id FROM securities WHERE isin='US5949181045'").fetchone()[0]


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
```

- [ ] **Step 2: Run tests — expect failures**

```bash
cd /Users/melendex/Documents/src/portfolio
python -m pytest tests/test_insert_dedup.py -v 2>&1 | head -20
```

Expected: `ImportError: cannot import name 'find_duplicate'`.

- [ ] **Step 3: Add `find_duplicate` to `tools/insert.py`**

Add this function after `get_db()` and before `upsert_security()`:

```python
def find_duplicate(conn, data: dict) -> int | None:
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
```

Then update `insert_transaction()` to use it. Replace the function body with:

```python
def insert_transaction(data: dict) -> int:
    conn = get_db()
    # resolve security_id from isin
    row = conn.execute("SELECT id FROM securities WHERE isin = ?", (data["isin"],)).fetchone()
    if not row:
        print(f"ERROR: security {data['isin']} not found. Insert it first.", file=sys.stderr)
        sys.exit(1)
    sec_id = row[0]

    # duplicate check
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
```

Also add `--force` flag support in the `__main__` block:

```python
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
        if args.force:
            # Bypass duplicate check: call raw insert
            conn = get_db()
            row = conn.execute("SELECT id FROM securities WHERE isin = ?", (data["isin"],)).fetchone()
            if not row:
                print(f"ERROR: security {data['isin']} not found.", file=sys.stderr)
                sys.exit(1)
            cur = conn.execute(
                "INSERT INTO transactions (security_id, date, type, broker, quantity, price, currency, total, fee, exchange, notes, source_file) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (row[0], data["date"], data["type"], data["broker"], data["quantity"],
                 data.get("price"), data["currency"], data.get("total"),
                 data.get("fee", 0), data.get("exchange"), data.get("notes"), data.get("source_file"))
            )
            tid = cur.fetchone()[0]
            conn.commit()
            conn.close()
            print(f"transaction_id={tid}")
        else:
            tid = insert_transaction(data)
            print(f"transaction_id={tid}")
    elif args.cmd == "query":
        run_query(args.arg)
```

- [ ] **Step 4: Run tests — expect pass**

```bash
cd /Users/melendex/Documents/src/portfolio
python -m pytest tests/test_insert_dedup.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/insert.py tests/test_insert_dedup.py
git commit -m "feat: insert.py — duplicate detection before transaction insert"
```

---

## Task 7: Create skills in `.claude/skills/`

**Files:**
- Create: `.claude/skills/setup.md`
- Create: `.claude/skills/ingest.md`
- Modify: `.claude/skills/snapshot.md`

All paths in skills are relative from the repo root. No absolute paths.

- [ ] **Step 1: Create `.claude/skills/setup.md`**

```markdown
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
```

- [ ] **Step 2: Create `.claude/skills/ingest.md`**

```markdown
---
name: ingest
description: Ingest transactions from a PDF or screenshot. Extracts data, resolves tickers, loads FX rates, inserts into DB.
---

# Portfolio Ingest

The user has provided a PDF or screenshot from their broker.

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
- Exit 3: Yahoo failed → ask the user for the Yahoo Finance ticker directly → call:
  ```bash
  python3 tools/resolve_ticker.py <isin> <exchange> --manual <ticker> <currency>
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

**If exit code 2 (duplicate detected):** tell the user which transaction was skipped and why.

## Step 7 — Summary

Report: "X transactions inserted, Y tickers resolved (Z new), W already existed (duplicates skipped)."
```

- [ ] **Step 3: Update `.claude/skills/snapshot.md`** — remove absolute path

Replace the entire content of `.claude/skills/snapshot.md` with:

```markdown
---
name: snapshot
description: Show live portfolio snapshot with market prices, unrealized P&L, and portfolio weights. Accepts optional broker filter (ibkr, fidelity, scalable, etc.).
---

# Portfolio Snapshot

Run the snapshot script and show the output to the user.

## Steps

1. Determine the broker filter:
   - If the user named a broker → pass it as the argument (lowercase)
   - Otherwise → no argument (all brokers)

2. Run from the portfolio repo root:
```bash
python3 tools/snapshot.py [broker]
```

3. Show the full output to the user as-is. Do not summarize or truncate it.

4. After the table, offer one follow-up:
   > "Want me to dig into any position, or export this to CSV?"
```

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/setup.md .claude/skills/ingest.md .claude/skills/snapshot.md
git commit -m "feat: add setup and ingest skills; update snapshot skill to use relative path"
```

---

## Task 8: Create `requirements.txt`

**Files:**
- Create: `requirements.txt`

- [ ] **Step 1: Check current installed versions**

```bash
pip show yfinance requests | grep -E "^(Name|Version)"
```

- [ ] **Step 2: Create `requirements.txt`**

```
yfinance>=0.2.40
requests>=2.31.0
```

(`sqlite3` and `csv` are stdlib — no entry needed.)

- [ ] **Step 3: Verify install works cleanly**

```bash
pip install -r requirements.txt --dry-run 2>&1 | tail -5
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "chore: add requirements.txt with pinned dependencies"
```

---

## Task 9: Create `README.md`

**Files:**
- Create: `README.md`

- [ ] **Step 1: Create README.md**

```markdown
# Portfolio Tracker

A personal investment portfolio tracker for Colombian investors. Self-hosted, operated via Claude Code.

Pass a PDF or screenshot from your broker to Claude — it extracts, ingests, and maintains your portfolio automatically, with FIFO cost basis, specific-lot assignment, and a Colombia tax report.

## Requirements

- Python 3.11+
- [Claude Code](https://claude.ai/code) with an Anthropic API key

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/your-username/portfolio.git
cd portfolio

# 2. Install dependencies
pip install -r requirements.txt

# 3. Initialize the database
# Open Claude Code in this directory, then run:
/setup
```

## Commands

| Command | What it does |
|---------|-------------|
| `/setup` | Create or migrate the database |
| `/ingest` | Ingest transactions from a PDF or screenshot |
| `/snapshot` | Show current positions with live prices and P&L |
| `/snapshot ibkr` | Snapshot filtered by broker |
| `/tax 2024` | Colombia tax report for fiscal year 2024 |

## Tax Report

The tax report is **hardcoded for Colombia**:
- **Ganancia Ocasional** (occasional gain): assets held >730 days → flat 15%
- **Renta Ordinaria** (ordinary income): assets held ≤730 days → progressive rate
- TRM (exchange rate) from Banco de la República
- UVT from DIAN (updated annually in `tools/tax_report.py`)

## Ticker Resolution

When a new security is ingested, the system tries to resolve its Yahoo Finance ticker automatically via ISIN. If it can't (ambiguous or Yahoo search fails), Claude will ask you:

1. Which exchange the instrument trades on (e.g., LSE, NASDAQ, XETRA)
2. Or the Yahoo Finance ticker directly (search at finance.yahoo.com)

Resolved tickers are saved in the local database — you won't be asked again.

## TRM Manual Fallback

If the Banco de la República API is unavailable, Claude will ask you to download TRM rates manually:

1. Go to: https://suameca.banrep.gov.co/estadisticas-economicas/informacionSerie/1/tasa_cambio_peso_colombiano_trm_dolar_usd
2. Switch to "Tabla" view
3. Select the dates of interest and download
4. Run: `python3 tools/load_trm.py <downloaded_file.txt>`

## Data Privacy

Your portfolio data stays local. `portfolio.db` is in `.gitignore` and never committed. Only generic code and SQL schemas are tracked in git.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add README with setup, commands, and privacy note"
```

---

## Task 10: Wrapper skills in `personal-assistant`

**Files:**
- Modify: `/Users/melendex/Documents/src/personal-assistant/.claude/skills/portfolio-snapshot.md` (if exists)
- Create: `/Users/melendex/Documents/src/personal-assistant/.claude/skills/portfolio-snapshot.md`
- Create: `/Users/melendex/Documents/src/personal-assistant/.claude/skills/portfolio-ingest.md`

These are thin wrappers that delegate to the portfolio repo's own skills.

- [ ] **Step 1: Check what exists**

```bash
ls /Users/melendex/Documents/src/personal-assistant/.claude/skills/ | grep portfolio
```

- [ ] **Step 2: Create/update `portfolio-snapshot.md` in personal-assistant**

```markdown
---
name: portfolio-snapshot
description: Show live portfolio snapshot. Delegates to the portfolio repo skill.
---

# Portfolio Snapshot (wrapper)

This skill delegates to the portfolio repo. Run from the portfolio directory:

```bash
cd /Users/melendex/Documents/src/portfolio
```

Then follow the `snapshot` skill instructions in `.claude/skills/snapshot.md` in that repo.
```

- [ ] **Step 3: Create `portfolio-ingest.md` in personal-assistant**

```markdown
---
name: portfolio-ingest
description: Ingest portfolio transactions from PDF or screenshot. Delegates to the portfolio repo skill.
---

# Portfolio Ingest (wrapper)

This skill delegates to the portfolio repo. Run from the portfolio directory:

```bash
cd /Users/melendex/Documents/src/portfolio
```

Then follow the `ingest` skill instructions in `.claude/skills/ingest.md` in that repo.
```

- [ ] **Step 4: Commit in personal-assistant**

```bash
cd /Users/melendex/Documents/src/personal-assistant
git add .claude/skills/portfolio-snapshot.md .claude/skills/portfolio-ingest.md
git commit -m "feat: portfolio skill wrappers delegate to portfolio repo"
```

---

## Task 11: Run full migration on the real DB + verify DoD

This is the manual validation task — run after all code tasks are complete.

- [ ] **Step 1: Run migrate.py on the real DB**

```bash
cd /Users/melendex/Documents/src/portfolio
python3 tools/migrate.py
```

Expected: no errors, `ticker_mappings` populated.

- [ ] **Step 2: Verify ticker count**

```bash
python3 tools/insert.py query 'SELECT COUNT(*) as total, source FROM ticker_mappings GROUP BY source'
```

Expected: at least 1 row, all `source='manual'` (from backfill).

- [ ] **Step 3: Confirm no TICKER_MAP in tracked code**

```bash
git grep "TICKER_MAP" -- '*.py' '*.md'
```

Expected: no output (TICKER_MAP has been deleted and committed in Task 5).

- [ ] **Step 4: Snapshot smoke test**

```bash
python3 tools/snapshot.py 2>&1 | head -30
```

Expected: same positions as before refactor. Values may differ ±1% due to live prices.

- [ ] **Step 5: Setup smoke test (new DB)**

```bash
sqlite3 /tmp/test_new_portfolio.db < schema.sql && echo "OK"
python3 tools/snapshot.py 2>&1 | head -5
```

Expected: no crash, "no positions" or empty table.

- [ ] **Step 6: Run full test suite**

```bash
cd /Users/melendex/Documents/src/portfolio
python -m pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 7: Final commit**

```bash
git add -A
git commit -m "chore: post-migration verification complete — portfolio repo generic and publishable"
```

---

## Self-Review

**Spec coverage check:**

| PRD requirement | Task |
|----------------|------|
| `ticker_mappings` table in schema | Task 1 |
| `resolve_ticker.py` with DB cache + Yahoo + manual fallback | Task 2 |
| `load_fx.py` with ECB + Banrep + graceful degradation | Task 3 |
| `migrate.py` idempotent with backfill + release order | Task 4 |
| `snapshot.py` removes TICKER_MAP | Task 5 |
| Anti-duplicate in `insert.py` | Task 6 |
| `setup.md`, `ingest.md` skills with relative paths | Task 7 |
| `snapshot.md` updated (no absolute path) | Task 7 |
| `requirements.txt` | Task 8 |
| `README.md` with all 7 required sections | Task 9 |
| Personal-assistant wrapper skills | Task 10 |
| DoD validation (all 8 criteria) | Task 11 |
| MIC normalization in resolve_ticker | Task 2 (normalize_exchange) |
| Missing exchange → manual fallback | Task 2 (exit code 2) |
| TRM fallback with exact URL | Task 3 (format_trm_fallback_message) |
| security_currency / tx_currency distinction in ingest skill | Task 7 (ingest.md) |
| Snapshot DoD: structural equivalence ±1% | Task 11, Step 4 |
| Release order: migrate → delete TICKER_MAP → git diff → push | Task 4 (documented in migrate.py) + Task 5 note |

**No placeholders found.** All steps contain exact code, commands, and expected output.

**Type consistency:** `load_ticker_map_from_db(conn)` defined in Task 5 and used in Task 5 only. `find_duplicate(conn, data)` defined and used in Task 6. `normalize_exchange`, `lookup_db`, `save_mapping` defined and tested in Task 2. All consistent.

---

## Post-Implementation Decisions

Decisions made during implementation that deviate from or clarify the original plan.

### D1: Multi-exchange snapshot simplified to `{isin: ticker}` map

**Original plan (Task 5):** `load_ticker_map_from_db` returns `{isin: ticker}` — simple, one ticker per ISIN.

**Mid-implementation detour:** After review, snapshot.py was temporarily changed to use `{(isin, exchange): ticker}` with GROUP BY `(security_id, exchange)` to support the same ISIN on two active exchanges. This added subqueries, double `{broker_filter}` placeholders, and `(isin, exchange)` tuple keys throughout `fetch_prices` and `run()`.

**Decision (reverted):** The multi-exchange snapshot complexity was removed. Reasons:
- The case of a single investor holding the same ISIN simultaneously on two different exchanges is practically non-existent for the Colombian retail audience.
- Transfers between brokers change the broker, not the exchange — so the "same ISIN, two exchanges" scenario doesn't arise from transfers.
- The added complexity (subqueries, tuple keys, NULL exchange edge cases) outweighed the theoretical benefit.

**Final state:** `load_ticker_map_from_db` returns `{isin: ticker}`, preferring `source='manual'` over `'auto'` when multiple entries exist for the same ISIN (ORDER BY determinism). The `ticker_mappings` PK remains `(isin, exchange)` — this is still correct for the resolver — but snapshot reads tickers by ISIN only.

### D2: `migrate.py` backfill uses no embedded TICKER_MAP

**Original plan (Task 4):** `migrate.py` imports `TICKER_MAP` from `snapshot.py` at runtime before it's deleted.

**Issue:** After Task 5 deleted `TICKER_MAP` from `snapshot.py`, the import fails silently and backfill is skipped.

**First fix (reverted):** Embedded `_HISTORICAL_TICKER_MAP` as a 44-entry static constant in `migrate.py`. This reintroduced a personal ticker seed file in tracked code — exactly what the PRD says is OUT of scope.

**Final state:** `migrate.py` has no embedded ticker map. The backfill step in `main()` is replaced by a verification step: it checks if `ticker_mappings` is populated and prints a hint if empty. New users populate `ticker_mappings` organically via `/ingest` + `resolve_ticker.py`. Existing users (like the repo owner) ran `migrate.py` before deleting `TICKER_MAP`, so their DB is already populated.

### D3: `resolve_ticker.py` enforces MIC — unknown exchange exits 2

**Original plan:** `normalize_exchange` returned `raw.upper()` for unknown exchanges with a warning.

**Final state:** Unknown exchange codes call `sys.exit(2)` — same exit code as "missing exchange" — with a message listing known MIC codes. This forces the skill to ask the user for a valid exchange before saving bad data to the DB. Test updated from `test_normalize_exchange_unknown_returns_upper` → `test_normalize_exchange_unknown_raises_systemexit`.

### D4: `load_fx.py` adds `--file` flag for local CSV ingestion

**Not in original plan.** Added to close the loop on the ECB fallback: the fallback message tells the user to download a CSV, and `--file` + `--pairs` provides the command to ingest it. Without this, the fallback was incomplete (download instructions with no way to load the file).

### D5: `migrate.py` LSE currency = USD (not GBP)

The `_SUFFIX_TO_MIC` inference for `.L` tickers was briefly changed to GBP, then reverted to USD. iShares ETFs on LSE (IWDA.L, CSPX.L, etc.) are USD share classes — Yahoo Finance `fast_info.currency` returns `"USD"` for them. The `currency` field in `ticker_mappings` reflects what Yahoo reports, not the exchange's quoting currency (GBp).
