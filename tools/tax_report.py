#!/usr/bin/env python3
"""
Reporte de ganancias/pérdidas para declaración de renta Colombia 2025.

Calcula per venta:
  - Lotes FIFO que se consumieron
  - Costo fiscal en COP (TRM del día de compra)
  - Ingreso en COP (TRM del día de venta)
  - Días de tenencia → Ganancia Ocasional (> 730 días) vs Renta Ordinaria (≤ 730)
  - Ganancia/pérdida en USD y COP

Reglas fiscales:
  - > 730 días → Ganancia Ocasional → 15% flat → NO entra en exógena
  - ≤ 730 días → Renta Ordinaria → tarifa progresiva → SÍ entra en exógena

Fuentes FX:
  - USD/COP: TRM Banco de la República (tabla fx_rates)
  - EUR/USD: BCE (tabla fx_rates)
  - EUR/COP: EUR/USD × TRM (derivado)

Usage:
    python3 tools/tax_report.py           # año fiscal por defecto: 2025
    python3 tools/tax_report.py 2024      # otro año
    python3 tools/tax_report.py --detail  # muestra lotes FIFO individuales
"""
import sqlite3, sys, os
from collections import defaultdict
from datetime import date, datetime

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

YEAR = None
DETAIL = False
TABLE = False
SHOW_STC = False
FILTER = None   # None = todo, "ocasional", "ordinaria"
for arg in sys.argv[1:]:
    if arg == "--detail":
        DETAIL = True
    elif arg == "--table":
        TABLE = True
    elif arg == "--show-stc":
        SHOW_STC = True
    elif arg in ("--ocasional", "--ordinaria"):
        FILTER = arg[2:]   # "ocasional" | "ordinaria"
    elif arg.isdigit() and len(arg) == 4:
        YEAR = int(arg)

if YEAR is None:
    print("Uso: python3 tools/tax_report.py <año> [--table] [--detail] [--ocasional] [--ordinaria] [--show-stc]")
    print("  Ej: python3 tools/tax_report.py 2025 --table")
    sys.exit(1)

DIAS_LARGO_PLAZO = 730  # > 730 días = Ganancia Ocasional Colombia
UVT = {2024: 47065, 2025: 49799}
UVT_VAL = UVT.get(YEAR, 49799)
TOPE_EXOGENA_UVT  = 2400   # rentas de capital/no laborales
TOPE_EXOGENA_COP  = TOPE_EXOGENA_UVT * UVT_VAL


# ──────────────────────────────────────────────────────────────────────────────
# FX helpers
# ──────────────────────────────────────────────────────────────────────────────

def fx(conn, from_ccy, to_ccy, dt):
    """
    Busca tasa en fx_rates para la fecha exacta.
    Si no hay (fin de semana / festivo), busca el día hábil anterior más cercano.
    """
    row = conn.execute("""
        SELECT rate FROM fx_rates
        WHERE from_currency = ? AND to_currency = ? AND date <= ?
        ORDER BY date DESC LIMIT 1
    """, (from_ccy, to_ccy, dt)).fetchone()
    return row[0] if row else None


def to_usd(conn, amount, ccy, dt):
    """Convierte amount en ccy → USD usando tasa histórica."""
    if ccy == "USD":
        return amount
    if ccy == "EUR":
        rate = fx(conn, "EUR", "USD", dt)
        return amount * rate if rate else None
    if ccy == "GBP":
        rate = fx(conn, "GBP", "USD", dt)
        return amount * rate if rate else None
    return None


def to_cop(conn, amount_usd, dt):
    """Convierte USD → COP usando TRM del día."""
    trm = fx(conn, "USD", "COP", dt)
    return amount_usd * trm if trm else None


# ──────────────────────────────────────────────────────────────────────────────
# FIFO engine
# ──────────────────────────────────────────────────────────────────────────────

class FifoQueue:
    """Cola FIFO de lotes de compra para un instrumento."""

    def __init__(self):
        self.lots = []  # list of [qty_remaining, price_usd, date_str, source]

    def add(self, qty, price_usd, dt, source):
        self.lots.append([qty, price_usd, dt, source])

    def consume(self, qty_needed):
        """
        Consume qty_needed unidades en orden FIFO.
        Retorna lista de (qty_consumed, price_usd, buy_date, source).
        """
        consumed = []
        remaining = qty_needed
        for lot in self.lots:
            if remaining <= 0:
                break
            lot_qty, price_usd, buy_date, source = lot
            if lot_qty <= 0:
                continue
            take = min(lot_qty, remaining)
            consumed.append((take, price_usd, buy_date, source))
            lot[0] -= take
            remaining -= take
        if remaining > 1e-6:
            raise ValueError(f"FIFO insuficiente: faltan {remaining:.4f} unidades")
        return consumed


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def run():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 1. Cargar todos los movimientos relevantes ordenados cronológicamente
    rows = conn.execute("""
        SELECT
            s.isin, s.name, s.currency AS db_ccy,
            t.id, t.date, t.type, t.broker,
            t.quantity, t.price, t.currency AS t_ccy, t.total, t.fee
        FROM transactions t
        JOIN securities s ON s.id = t.security_id
        WHERE t.type IN ('buy','vesting','sell','sell_to_cover','transfer_in','transfer_out')
        ORDER BY t.date, t.id
    """).fetchall()

    # 2. Construir colas FIFO por ISIN
    #    — transfer_in/out NO crean ni consumen lotes propios:
    #      el transfer_in hereda los lotes originales (el costo no cambia).
    #      Solo buy/vesting agregan lotes; sell consume lotes.
    #      sell_to_cover NO consume lotes FIFO: costo = precio de vest (ganancia = $0).
    queues = defaultdict(FifoQueue)

    for r in rows:
        isin  = r["isin"]
        qty   = r["quantity"]
        dt    = r["date"]
        typ   = r["type"]
        ccy   = r["t_ccy"]
        total = r["total"]
        src   = f"{r['broker']} {r['date']} id={r['id']}"

        if typ in ("buy", "vesting"):
            price_usd = to_usd(conn, total / qty if total and qty else 0, ccy, dt)
            queues[isin].add(qty, price_usd, dt, src)

        # sell_to_cover, transfer_in, transfer_out: no tocan la cola FIFO

    # 3. Procesar ventas del año fiscal
    results = []
    fifo_errors = []

    for r in rows:
        if r["type"] not in ("sell", "sell_to_cover"):
            continue
        if not r["date"].startswith(str(YEAR)):
            continue

        isin       = r["isin"]
        name       = r["name"]
        sell_qty   = r["quantity"]
        sell_date  = r["date"]
        sell_total = r["total"]       # en moneda original de la venta
        sell_ccy   = r["t_ccy"]
        fee        = r["fee"] or 0
        typ        = r["type"]

        # Ingreso neto en USD
        ingreso_usd = to_usd(conn, sell_total, sell_ccy, sell_date)
        if ingreso_usd is None:
            fifo_errors.append(f"Sin FX para ingreso: {name} {sell_date} {sell_ccy}")
            continue
        ingreso_usd -= to_usd(conn, fee, sell_ccy, sell_date) or 0

        # TRM de la venta
        trm_venta = fx(conn, "USD", "COP", sell_date)
        ingreso_cop = ingreso_usd * trm_venta if trm_venta else None

        sell_dt = datetime.strptime(sell_date, "%Y-%m-%d").date()

        # ── STC: costo = precio de venta (ganancia = $0).
        #    Las acciones se adquirieron y vendieron el mismo día del vesting;
        #    la ganancia laboral ya fue declarada por el empleador en el CIR.
        #    Aplicar FIFO aquí implicaría doble tributación.
        if typ == "sell_to_cover":
            trm_compra = fx(conn, "USD", "COP", sell_date)
            lot_detail = [{
                "buy_date":  sell_date,           # misma fecha: vest = venta
                "qty":       sell_qty,
                "price_usd": ingreso_usd / sell_qty,
                "dias":      0,
                "largo":     False,
                "costo_usd": ingreso_usd,         # costo = ingreso → ganancia = 0
                "trm_c":     trm_compra,
                "costo_cop": ingreso_cop,
                "src":       "STC — costo=precio_vest",
            }]
            costo_usd_total = ingreso_usd
            costo_cop_total = ingreso_cop or 0.0
            ganancia_usd    = 0.0
            ganancia_cop    = 0.0
            clasificacion   = "STC"

        else:
            # FIFO: consumir lotes para ventas manuales
            try:
                lots_consumed = queues[isin].consume(sell_qty)
            except ValueError as e:
                fifo_errors.append(f"{name} {sell_date}: {e}")
                continue

            costo_usd_total = 0.0
            costo_cop_total = 0.0
            lot_detail = []

            for (lot_qty, lot_price_usd, buy_date, lot_src) in lots_consumed:
                buy_dt     = datetime.strptime(buy_date, "%Y-%m-%d").date()
                dias       = (sell_dt - buy_dt).days
                largo      = dias > DIAS_LARGO_PLAZO
                costo_lote_usd = lot_qty * lot_price_usd
                trm_compra     = fx(conn, "USD", "COP", buy_date)
                costo_lote_cop = costo_lote_usd * trm_compra if trm_compra else None

                costo_usd_total += costo_lote_usd
                if costo_lote_cop:
                    costo_cop_total += costo_lote_cop

                lot_detail.append({
                    "buy_date":  buy_date,
                    "qty":       lot_qty,
                    "price_usd": lot_price_usd,
                    "dias":      dias,
                    "largo":     largo,
                    "costo_usd": costo_lote_usd,
                    "trm_c":     trm_compra,
                    "costo_cop": costo_lote_cop,
                    "src":       lot_src,
                })

            ganancia_usd = ingreso_usd - costo_usd_total
            ganancia_cop = (ingreso_cop - costo_cop_total) if (ingreso_cop and costo_cop_total) else None

            all_corto = all(not d["largo"] for d in lot_detail)
            all_largo = all(d["largo"] for d in lot_detail)
            if all_largo:
                clasificacion = "OCASIONAL"
            elif all_corto:
                clasificacion = "ORDINARIA"
            else:
                clasificacion = "MIXTO"

        results.append({
            "isin":         isin,
            "name":         name,
            "type":         typ,
            "sell_date":    sell_date,
            "qty":          sell_qty,
            "ingreso_usd":  ingreso_usd,
            "trm_venta":    trm_venta,
            "ingreso_cop":  ingreso_cop,
            "costo_usd":    costo_usd_total,
            "costo_cop":    costo_cop_total,
            "ganancia_usd": ganancia_usd,
            "ganancia_cop": ganancia_cop,
            "clasificacion":clasificacion,
            "lots":         lot_detail,
        })

    conn.close()

    # ── Modo tabla (formato exógena — una fila por lote FIFO)
    if TABLE:
        filtro_label = f" — solo {FILTER.upper()}" if FILTER else ""
        HDR = (f"{'Instrumento':<36}  {'F.Venta':>10}  {'F.Compra':>10}  "
               f"{'Cant':>8}  {'Costo USD':>11}  {'Venta USD':>11}  {'Gan USD':>10}  "
               f"{'TRM Compra':>10}  {'Costo COP':>14}  {'TRM Venta':>10}  "
               f"{'Venta COP':>14}  {'Gan COP':>14}  {'Plazo':<10}")
        print(f"\n{'='*len(HDR)}")
        print(f"  Tabla exógena — una fila por lote FIFO — Año fiscal {YEAR}{filtro_label}")
        print(f"{'='*len(HDR)}\n")
        print(f"  {HDR}")
        print(f"  {'─'*len(HDR)}")

        totals_t = {"OCASIONAL": [0.0, 0.0, 0.0, 0.0],   # gan_usd, gan_cop, venta_usd, venta_cop
                    "ORDINARIA": [0.0, 0.0, 0.0, 0.0],
                    "STC":       [0.0, 0.0, 0.0, 0.0]}

        for r in results:
            clsf = r["clasificacion"]
            if clsf == "STC" and not SHOW_STC:
                continue
            if FILTER and clsf.lower() != FILTER and clsf != "STC":
                continue
            sell_date   = r["sell_date"]
            name        = r["name"][:36]
            trm_v       = r["trm_venta"]
            ingreso_usd = r["ingreso_usd"]
            ingreso_cop = r["ingreso_cop"]
            total_qty   = r["qty"]

            for d in r["lots"]:
                lot_qty   = d["qty"]
                frac      = lot_qty / total_qty
                venta_usd = ingreso_usd * frac
                venta_cop = (ingreso_cop or 0) * frac
                costo_usd = d["costo_usd"]
                costo_cop = d["costo_cop"] or 0.0
                gan_usd   = venta_usd - costo_usd
                gan_cop   = venta_cop - costo_cop
                trm_c     = d["trm_c"] or 0.0
                plazo     = "STC" if clsf == "STC" else ("OCASIONAL" if d["largo"] else "ORDINARIA")

                print(f"  {name:<36}  {sell_date:>10}  {d['buy_date']:>10}  "
                      f"{lot_qty:>8.4f}  {costo_usd:>11,.2f}  {venta_usd:>11,.2f}  {gan_usd:>+10,.2f}  "
                      f"{trm_c:>10,.2f}  {costo_cop:>14,.0f}  {trm_v:>10,.2f}  "
                      f"{venta_cop:>14,.0f}  {gan_cop:>+14,.0f}  {plazo:<10}")

                totals_t[plazo][0] += gan_usd
                totals_t[plazo][1] += gan_cop
                totals_t[plazo][2] += venta_usd
                totals_t[plazo][3] += venta_cop

        print(f"  {'─'*len(HDR)}")
        # Totales por categoría
        for plazo, (gu, gc, vu, vc) in totals_t.items():
            if plazo == "STC" and not SHOW_STC:
                continue
            if FILTER and plazo.lower() != FILTER and plazo != "STC":
                continue
            print(f"  {'TOTAL '+plazo:<36}  {'':>10}  {'':>10}  "
                  f"{'':>8}  {'':>11}  {vu:>11,.2f}  {gu:>+10,.2f}  "
                  f"{'':>10}  {'':>14}  {'':>10}  "
                  f"{vc:>14,.0f}  {gc:>+14,.0f}  {plazo:<10}")

        # Total general
        total_gan_u = sum(v[0] for v in totals_t.values())
        total_gan_c = sum(v[1] for v in totals_t.values())
        total_ven_u = sum(v[2] for v in totals_t.values())
        total_ven_c = sum(v[3] for v in totals_t.values())
        print(f"  {'─'*len(HDR)}")
        print(f"  {'TOTAL VENTAS':<36}  {'':>10}  {'':>10}  "
              f"{'':>8}  {'':>11}  {total_ven_u:>11,.2f}  {total_gan_u:>+10,.2f}  "
              f"{'':>10}  {'':>14}  {'':>10}  "
              f"{total_ven_c:>14,.0f}  {total_gan_c:>+14,.0f}")

        # ── Verificación tope exógena (solo rentas de capital = ORDINARIA)
        ven_ord_cop = totals_t["ORDINARIA"][3]
        tope_cop    = TOPE_EXOGENA_COP
        print(f"\n  ── Verificación exógena (Art. resolución DIAN — rentas de capital/no laborales)")
        print(f"  Ingresos brutos ORDINARIA:  ${ven_ord_cop:>16,.0f} COP")
        print(f"  Tope {TOPE_EXOGENA_UVT:,} UVT × ${UVT_VAL:,}:  ${tope_cop:>16,.0f} COP")
        if ven_ord_cop > tope_cop:
            print(f"  ✅ SUPERA el tope — OBLIGADO a reportar exógena por rentas de capital")
        else:
            print(f"  ℹ️  No supera el tope — verificar otros ingresos no laborales")

        print(f"\n  Notas:")
        print(f"  • UVT {YEAR}: ${UVT_VAL:,} COP")
        print(f"  • Una fila por lote FIFO. Venta USD/COP prorrateada por cantidad.")
        print(f"  • Costo COP = costo USD × TRM del día de COMPRA (Banco de la República)")
        print(f"  • Venta COP = venta USD × TRM del día de VENTA")
        print(f"  • Ganancia OCASIONAL (>730 días) NO cuenta para tope exógena\n")
        return

    # ── Imprimir
    W = 130
    print(f"\n{'='*W}")
    print(f"  Reporte de ganancias/pérdidas — Año fiscal {YEAR}")
    print(f"  Colombia: > {DIAS_LARGO_PLAZO} días = Ganancia Ocasional (15%) | ≤ {DIAS_LARGO_PLAZO} días = Renta Ordinaria")
    print(f"{'='*W}\n")

    totals = {"OCASIONAL": [0.0, 0.0], "ORDINARIA": [0.0, 0.0], "STC": [0.0, 0.0], "MIXTO": [0.0, 0.0]}

    for r in results:
        if r["clasificacion"] == "STC" and not SHOW_STC:
            continue
        if FILTER and r["clasificacion"].lower() != FILTER and r["clasificacion"] != "STC":
            continue
        clsf  = r["clasificacion"]
        gn_u  = r["ganancia_usd"]
        gn_c  = r["ganancia_cop"] or 0.0

        sign  = "✅" if gn_u >= 0 else "🔴"
        tag   = {"OCASIONAL": "🟡 OCASIONAL", "ORDINARIA": "🔵 ORDINARIA",
                  "STC":      "⚪ STC",        "MIXTO":     "⚠️  MIXTO"}[clsf]

        print(f"  {sign} {tag}  │  {r['name'][:40]:<40}  │  {r['sell_date']}  │  {r['qty']:.4f} uds")
        print(f"     Ingreso :  ${r['ingreso_usd']:>12,.2f} USD  │  TRM venta {r['trm_venta']:>9,.2f}  │  ${r['ingreso_cop']:>16,.0f} COP")
        print(f"     Costo   :  ${r['costo_usd']:>12,.2f} USD  │                    │  ${r['costo_cop']:>16,.0f} COP")
        print(f"     Ganancia:  ${gn_u:>+12,.2f} USD  │                    │  ${gn_c:>+16,.0f} COP")

        if DETAIL:
            print(f"     {'─'*100}")
            print(f"     {'Lote compra':<12} {'Qty':>8} {'P.costo USD':>12} {'Días':>6} {'Plazo':<12} {'Costo USD':>12} {'TRM compra':>12} {'Costo COP':>16}")
            for d in r["lots"]:
                plazo = "OCASIONAL" if d["largo"] else "ORDINARIA"
                cop_s = f"${d['costo_cop']:>14,.0f}" if d["costo_cop"] else "     sin TRM"
                print(f"     {d['buy_date']:<12} {d['qty']:>8.4f} ${d['price_usd']:>11,.2f} "
                      f"{d['dias']:>6} {plazo:<12} ${d['costo_usd']:>11,.2f} "
                      f"{d['trm_c']:>12,.2f} {cop_s}")
        print()

        if clsf in totals:
            totals[clsf][0] += gn_u
            totals[clsf][1] += gn_c

    # ── Resumen
    print(f"  {'─'*W}")
    print(f"\n  RESUMEN {YEAR}\n")
    print(f"  {'Categoría':<16} {'Ganancia USD':>15} {'Ganancia COP':>20}   Tratamiento fiscal")
    print(f"  {'─'*80}")
    for clsf, (gu, gc) in totals.items():
        tag = {"OCASIONAL": "15% flat — NO exógena",
               "ORDINARIA": "Progresiva — SÍ exógena",
               "STC":       "Ganancia = $0 — ya en CIR MSFT",
               "MIXTO":     "Ver detalle por lote"}[clsf]
        print(f"  {clsf:<16} ${gu:>+14,.2f} ${gc:>+19,.0f}   {tag}")

    total_u = sum(v[0] for v in totals.values())
    total_c = sum(v[1] for v in totals.values())
    print(f"  {'─'*80}")
    print(f"  {'TOTAL':<16} ${total_u:>+14,.2f} ${total_c:>+19,.0f}")

    if fifo_errors:
        print(f"\n  ⚠  ADVERTENCIAS ({len(fifo_errors)}):")
        for e in fifo_errors:
            print(f"     • {e}")

    print(f"\n  Notas:")
    print(f"  • Costo en COP = precio USD × TRM del día de COMPRA (Banco de la República)")
    print(f"  • Ingreso en COP = ingreso USD × TRM del día de VENTA")
    print(f"  • FIFO puro: el lote más antiguo se vende primero")
    print(f"  • transfer_in/out no crean lote nuevo (hereda costo original)")
    print(f"  • Usar --detail para ver lotes FIFO individuales por venta\n")


if __name__ == "__main__":
    run()
