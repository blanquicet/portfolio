#!/usr/bin/env python3
"""
Reporte de dividendos recibidos en un año fiscal.

Uso:
    python3 tools/reporte_dividendos.py 2025
    python3 tools/reporte_dividendos.py 2025 --csv > dividendos_2025.csv
"""
import sqlite3, sys, os

sys.path.insert(0, os.path.dirname(__file__))
from fifo import fx

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

# ── Args ──────────────────────────────────────────────────────────────────────

YEAR     = None
CSV_MODE = False

for arg in sys.argv[1:]:
    if arg == "--csv":
        CSV_MODE = True
    elif arg.isdigit() and len(arg) == 4:
        YEAR = int(arg)

if YEAR is None:
    print("Uso: python3 tools/reporte_dividendos.py <año> [--csv]")
    print("  Ej: python3 tools/reporte_dividendos.py 2025")
    sys.exit(1)

# ── Data ──────────────────────────────────────────────────────────────────────

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT t.date, s.name, t.broker, t.total, t.currency, t.notes
    FROM transactions t
    JOIN securities s ON s.id = t.security_id
    WHERE t.type = 'dividend'
      AND t.date BETWEEN ? AND ?
    ORDER BY t.date, t.broker, s.name
""", (f"{YEAR}-01-01", f"{YEAR}-12-31")).fetchall()


def trm_on(dt):
    return fx(conn, "USD", "COP", dt)

def eur_usd_on(dt):
    return fx(conn, "EUR", "USD", dt)

def to_cop(total, ccy, dt):
    if ccy == "COP":
        return total
    if ccy == "USD":
        t = trm_on(dt)
        return total * t if t else None
    if ccy == "EUR":
        e = eur_usd_on(dt)
        t = trm_on(dt)
        return total * e * t if (e and t) else None
    return None


# ── CSV ───────────────────────────────────────────────────────────────────────

if CSV_MODE:
    import csv as _csv
    writer = _csv.writer(sys.stdout)
    writer.writerow(["Fecha", "Instrumento", "Broker", "Valor (Moneda)", "Moneda", "TRM", "Valor COP", "Notas"])

    total_cop = 0.0
    for r in rows:
        dt, name, broker, total, ccy, notes = (
            r["date"], r["name"], r["broker"], r["total"], r["currency"], r["notes"]
        )
        trm  = trm_on(dt) if ccy != "COP" else None
        vcop = to_cop(total, ccy, dt)
        if vcop:
            total_cop += vcop
        writer.writerow([
            dt, name, broker.upper(),
            round(total, 2), ccy,
            round(trm, 2) if trm else "",
            round(vcop, 0) if vcop is not None else "",
            notes,
        ])

    writer.writerow([])
    writer.writerow(["", "TOTAL", "", "", "", "", round(total_cop, 0), ""])
    conn.close()
    sys.exit(0)


# ── Tabla ─────────────────────────────────────────────────────────────────────

W = 110
print(f"\n{'═'*W}")
print(f"  Dividendos recibidos — Año {YEAR}")
print(f"{'═'*W}\n")

HDR = (f"  {'Fecha':>10}  {'Instrumento':<38}  {'Broker':<10}  "
       f"{'Valor (Mon)':>12}  {'Mon':>4}  {'TRM':>8}  {'Valor COP':>16}  Notas")
print(HDR)
print(f"  {'─'*(W-2)}")

total_cop   = 0.0
missing     = 0
broker_tots = {}

for r in rows:
    dt, name, broker, total, ccy, notes = (
        r["date"], r["name"], r["broker"], r["total"], r["currency"], r["notes"]
    )
    trm  = trm_on(dt) if ccy != "COP" else None
    vcop = to_cop(total, ccy, dt)

    if vcop is None:
        missing += 1
        vcop_str = f"{'—':>16}"
        trm_str  = f"{'—':>8}"
    else:
        total_cop += vcop
        vcop_str  = f"{vcop:>16,.0f}"
        trm_str   = f"{trm:>8,.2f}" if trm else f"{'—':>8}"

    notes_short = (notes or "")[:40]
    print(f"  {dt:>10}  {name:<38}  {broker.upper():<10}  "
          f"{total:>12,.2f}  {ccy:>4}  {trm_str}  {vcop_str}  {notes_short}")

    bt = broker_tots.setdefault(broker.upper(), 0.0)
    broker_tots[broker.upper()] = bt + (vcop or 0.0)

print(f"  {'─'*(W-2)}")
missing_note = f"  (* excluye {missing} fila(s) sin TRM)" if missing else ""
print(f"\n  TOTAL COP {YEAR}:  {total_cop:>16,.0f}{missing_note}")

print(f"\n  Resumen por broker")
print(f"  {'─'*50}")
for b, v in sorted(broker_tots.items()):
    print(f"  {b:<12}  {v:>16,.0f} COP")
print(f"  {'─'*50}")
print(f"  {'TOTAL':<12}  {total_cop:>16,.0f} COP\n")

conn.close()
