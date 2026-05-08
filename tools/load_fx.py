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
TRM_FALLBACK_DOWNLOAD_URL = (
    "https://suameca.banrep.gov.co/estadisticas-economicas/informacionSerie/1/"
    "tasa_cambio_peso_colombiano_trm_dolar_usd"
)


# ── Pure functions (testable without DB) ──────────────────────────────────────

def find_gaps(conn, dates: list, from_ccy: str, to_ccy: str) -> list:
    """Return dates that are missing from fx_rates for the given pair."""
    if not dates:
        return []
    existing = set(
        row[0] for row in conn.execute(
            "SELECT date FROM fx_rates WHERE from_currency = ? AND to_currency = ? AND date IN ({})".format(
                ",".join("?" * len(dates))
            ),
            [from_ccy, to_ccy] + dates
        ).fetchall()
    )
    return [d for d in dates if d not in existing]


def format_trm_fallback_message(missing_dates: list) -> str:
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

def fetch_ecb(from_ccy: str, to_ccy: str, dates: list) -> list:
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


def fetch_banrep_trm(dates: list) -> list:
    """
    Attempt to fetch TRM from Banrep. Unreliable — returns empty list on any failure.
    Returns list of (date, 'USD', 'COP', rate) tuples.
    """
    try:
        import requests
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
                    rate_str_clean = item.get("valor", "").replace(".", "").replace(",", ".")
                    rate = float(rate_str_clean)
                    result.append((iso_date, "USD", "COP", rate))
            except (ValueError, AttributeError):
                continue
        return result
    except Exception:
        return []


# ── Orchestrator ──────────────────────────────────────────────────────────────

def insert_rates(conn, rows: list):
    conn.executemany(
        "INSERT OR REPLACE INTO fx_rates (date, from_currency, to_currency, rate) VALUES (?, ?, ?, ?)",
        rows
    )
    conn.commit()


def run(dates: list, pairs: list):
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
