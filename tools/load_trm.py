#!/usr/bin/env python3
"""Load TRM (USD/COP) rates from a text file or stdin into portfolio.db.

Usage:
    python3 tools/load_trm.py < trm_data.txt
    python3 tools/load_trm.py trm_data.txt

Input format (pipe-separated, from Banco de la República):
    | 2021/01/01 | 3432.50 |
"""
# DEPRECATED: Use tools/load_fx.py for new ingestions.
# This script is kept for manual TRM loading when the Banrep API fails.
# See the fallback instructions printed by load_fx.py for usage.
import re, sqlite3, sys, os

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

def parse_trm_lines(lines):
    rows = []
    for line in lines:
        m = re.match(r'\|\s*(\d{4}/\d{2}/\d{2})\s*\|\s*([\d.]+)\s*\|', line.strip())
        if m:
            date = m.group(1).replace('/', '-')
            rate = float(m.group(2))
            rows.append((date, 'USD', 'COP', rate))
    return rows

def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            lines = f.readlines()
    else:
        lines = sys.stdin.readlines()

    rows = parse_trm_lines(lines)
    if not rows:
        print("No TRM data found", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB)
    conn.executemany(
        "INSERT OR REPLACE INTO fx_rates (date, from_currency, to_currency, rate) VALUES (?, ?, ?, ?)",
        rows
    )
    conn.commit()
    print(f"Loaded {len(rows)} TRM rates ({rows[0][0]} to {rows[-1][0]})")
    conn.close()

if __name__ == "__main__":
    main()
