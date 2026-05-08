#!/usr/bin/env python3
"""Load EUR/USD rates from ECB CSV into portfolio.db as EUR→COP derived rates.

Downloads from ECB API or reads a local CSV. Combines with existing TRM (USD/COP)
to produce EUR/COP rates.

Usage:
    python3 tools/load_eurusd.py /tmp/ecb_eurusd.csv
    python3 tools/load_eurusd.py --download
"""
# DEPRECATED: Use tools/load_fx.py for new ingestions.
# This script is kept for manual EUR/USD loading when the ECB API fails.
# See the fallback instructions printed by load_fx.py for usage.
import csv, sqlite3, sys, os, subprocess

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")
ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A?startPeriod=2021-01-01&endPeriod=2026-12-31&format=csvdata"

def load_from_csv(filepath):
    """Parse ECB CSV → list of (date, eur_usd_rate)."""
    rows = []
    with open(filepath) as f:
        reader = csv.DictReader(f)
        for r in reader:
            date = r["TIME_PERIOD"]
            rate = float(r["OBS_VALUE"])
            rows.append((date, rate))
    return rows

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--download":
        filepath = "/tmp/ecb_eurusd.csv"
        subprocess.run(["curl", "-s", ECB_URL, "-o", filepath], check=True)
    elif len(sys.argv) > 1:
        filepath = sys.argv[1]
    else:
        print("Usage: load_eurusd.py <csv_file> | --download", file=sys.stderr)
        sys.exit(1)

    ecb_rows = load_from_csv(filepath)
    conn = sqlite3.connect(DB)

    # Insert raw EUR/USD rates
    eurusd_data = [(d, "EUR", "USD", r) for d, r in ecb_rows]
    conn.executemany(
        "INSERT OR REPLACE INTO fx_rates (date, from_currency, to_currency, rate) VALUES (?, ?, ?, ?)",
        eurusd_data
    )

    # Derive EUR/COP = EUR/USD × USD/COP (TRM)
    derived = 0
    for date, eurusd in ecb_rows:
        row = conn.execute(
            "SELECT rate FROM fx_rates WHERE date = ? AND from_currency = 'USD' AND to_currency = 'COP'",
            (date,)
        ).fetchone()
        if row:
            eur_cop = eurusd * row[0]
            conn.execute(
                "INSERT OR REPLACE INTO fx_rates (date, from_currency, to_currency, rate) VALUES (?, ?, ?, ?)",
                (date, "EUR", "COP", eur_cop)
            )
            derived += 1

    conn.commit()
    print(f"Loaded {len(ecb_rows)} EUR/USD rates")
    print(f"Derived {derived} EUR/COP rates (EUR/USD × TRM)")
    conn.close()

if __name__ == "__main__":
    main()
