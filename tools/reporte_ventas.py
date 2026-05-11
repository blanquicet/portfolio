#!/usr/bin/env python3
"""
Reporte de ganancias/pérdidas por venta de inversiones.

Calcula por cada venta:
  - Lotes FIFO consumidos
  - Costo base en COP (TRM del día de compra)
  - Ingreso en COP (TRM del día de venta)
  - Días de tenencia → Largo plazo (> 730 días) vs Corto plazo (≤ 730 días)
  - Ganancia/pérdida en USD y COP

Fuentes FX:
  - USD/COP: TRM Banco de la República (tabla fx_rates)
  - EUR/USD: BCE (tabla fx_rates)
  - EUR/COP: EUR/USD × TRM (derivado)

Usage:
    python3 tools/reporte_ventas.py 2025              # tabla por lote FIFO (por defecto)
    python3 tools/reporte_ventas.py 2024              # otro año
    python3 tools/reporte_ventas.py 2025 --summary    # vista resumida por venta
    python3 tools/reporte_ventas.py 2025 --detail     # agrega detalle de lotes en modo summary
    python3 tools/reporte_ventas.py 2025 --largo      # solo ventas de largo plazo
    python3 tools/reporte_ventas.py 2025 --corto      # solo ventas de corto plazo
"""
import sqlite3, sys, os
from datetime import datetime
sys.path.insert(0, os.path.dirname(__file__))
from fifo import fx, to_usd, FifoQueue, build_queues

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

YEAR = None
DETAIL = False
TABLE = True
SHOW_STC = False
FILTER = None   # None = todo, "largo", "corto"
CSV_MODE = False
for arg in sys.argv[1:]:
    if arg == "--detail":
        DETAIL = True
    elif arg == "--csv":
        CSV_MODE = True
        TABLE = True   # CSV implica modo tabla
    elif arg == "--table":
        TABLE = True
    elif arg == "--summary":
        TABLE = False
    elif arg == "--show-stc":
        SHOW_STC = True
    elif arg in ("--largo", "--corto"):
        FILTER = arg[2:]   # "largo" | "corto"
    elif arg.isdigit() and len(arg) == 4:
        YEAR = int(arg)

if YEAR is None:
    print("Uso: python3 tools/reporte_ventas.py <año> [--summary|--table] [--detail] [--largo] [--corto] [--show-stc]")
    print("  Ej: python3 tools/reporte_ventas.py 2025")
    print("  Ej: python3 tools/reporte_ventas.py 2025 --summary")
    sys.exit(1)

DIAS_LARGO_PLAZO = 730  # umbral de temporalidad: > 730 días = largo plazo


# ──────────────────────────────────────────────────────────────────────────────
# Helpers locales
# ──────────────────────────────────────────────────────────────────────────────

def to_cop(conn, amount_usd, dt):
    """Convierte USD → COP usando TRM del día."""
    trm = fx(conn, "USD", "COP", dt)
    return amount_usd * trm if trm else None


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def run():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 1. Construir colas FIFO hasta el 31-dic del año anterior (estado justo
    #    antes del año fiscal). Las ventas del año se procesan a continuación
    #    sobre estas colas para calcular ganancias correctamente.
    prior_year_end = f"{YEAR - 1}-12-31"
    queues, fifo_errors = build_queues(conn, as_of_date=prior_year_end)

    # 2. Cargar movimientos del año fiscal en orden cronológico
    year_rows = conn.execute("""
        SELECT
            s.isin, s.name, s.currency AS db_ccy,
            t.id, t.date, t.type, t.broker,
            t.quantity, t.price, t.currency AS t_ccy, t.total, t.fee
        FROM transactions t
        JOIN securities s ON s.id = t.security_id
        WHERE t.type IN ('buy','vesting','sell','sell_to_cover')
          AND t.date BETWEEN ? AND ?
        ORDER BY t.date, t.id
    """, (f"{YEAR}-01-01", f"{YEAR}-12-31")).fetchall()

    # 3. Procesar año fiscal en orden: agregar buys a cola, reportar ventas
    results = []

    for r in year_rows:
        isin = r["isin"]
        qty  = r["quantity"]
        dt   = r["date"]
        typ  = r["type"]
        ccy  = r["t_ccy"]
        total = r["total"]
        src  = f"{r['broker']} {dt} id={r['id']}"

        # Buys/vestings dentro del año: agregar a la cola antes de procesar ventas
        if typ in ("buy", "vesting"):
            price_usd = to_usd(conn, total / qty if total and qty else 0, ccy, dt)
            queues[isin].add(qty, price_usd, dt, src)
            continue

        # sell_to_cover and sell: reportar
        name       = r["name"]
        sell_qty   = r["quantity"]
        sell_date  = r["date"]
        sell_total = r["total"]
        sell_ccy   = r["t_ccy"]
        fee        = r["fee"] or 0
        broker     = r["broker"]

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
        if typ == "sell_to_cover":
            trm_compra = fx(conn, "USD", "COP", sell_date)
            lot_detail = [{
                "buy_date":  sell_date,
                "qty":       sell_qty,
                "price_usd": ingreso_usd / sell_qty,
                "dias":      0,
                "largo":     False,
                "costo_usd": ingreso_usd,
                "trm_c":     trm_compra,
                "costo_cop": ingreso_cop,
                "src":       "STC",
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
            costo_cop_incompleto = False
            lot_detail = []

            for (lot_qty, lot_price_usd, buy_date, lot_src) in lots_consumed:
                buy_dt     = datetime.strptime(buy_date, "%Y-%m-%d").date()
                dias       = (sell_dt - buy_dt).days
                largo      = dias > DIAS_LARGO_PLAZO
                costo_lote_usd = lot_qty * lot_price_usd
                trm_compra     = fx(conn, "USD", "COP", buy_date)
                costo_lote_cop = costo_lote_usd * trm_compra if trm_compra else None

                costo_usd_total += costo_lote_usd
                if costo_lote_cop is not None:
                    costo_cop_total += costo_lote_cop
                else:
                    costo_cop_incompleto = True

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
            if costo_cop_incompleto:
                ganancia_cop = None
                fifo_errors.append(
                    f"⚠ Costo COP incompleto: {name} {sell_date} — "
                    f"falta TRM de al menos un lote de compra. Ganancia COP no calculada."
                )
            else:
                ganancia_cop = (ingreso_cop - costo_cop_total) if ingreso_cop is not None else None

            all_corto = all(not d["largo"] for d in lot_detail)
            all_largo = all(d["largo"] for d in lot_detail)

            if all_largo:
                results.append({
                    "isin": isin, "name": name, "type": typ,
                    "sell_date": sell_date, "qty": sell_qty,
                    "ingreso_usd": ingreso_usd, "trm_venta": trm_venta,
                    "ingreso_cop": ingreso_cop, "costo_usd": costo_usd_total,
                    "costo_cop": costo_cop_total, "ganancia_usd": ganancia_usd,
                    "ganancia_cop": ganancia_cop, "clasificacion": "LARGO",
                    "broker": broker, "lots": lot_detail,
                })
            elif all_corto:
                results.append({
                    "isin": isin, "name": name, "type": typ,
                    "sell_date": sell_date, "qty": sell_qty,
                    "ingreso_usd": ingreso_usd, "trm_venta": trm_venta,
                    "ingreso_cop": ingreso_cop, "costo_usd": costo_usd_total,
                    "costo_cop": costo_cop_total, "ganancia_usd": ganancia_usd,
                    "ganancia_cop": ganancia_cop, "clasificacion": "CORTO",
                    "broker": broker, "lots": lot_detail,
                })
            else:
                # MIXTO: dividir en dos entradas, una por plazo
                for plazo_label, largo_flag in [("LARGO", True), ("CORTO", False)]:
                    sub_lots = [d for d in lot_detail if d["largo"] == largo_flag]
                    if not sub_lots:
                        continue
                    sub_qty       = sum(d["qty"] for d in sub_lots)
                    frac          = sub_qty / sell_qty
                    sub_ing_usd   = ingreso_usd * frac
                    sub_ing_cop   = (ingreso_cop * frac) if ingreso_cop is not None else None
                    sub_costo_usd = sum(d["costo_usd"] for d in sub_lots)
                    sub_costo_cop = sum(d["costo_cop"] or 0 for d in sub_lots)
                    sub_cop_incompleto = any(d["costo_cop"] is None for d in sub_lots)
                    sub_gan_usd   = sub_ing_usd - sub_costo_usd
                    if sub_cop_incompleto:
                        sub_gan_cop = None
                    else:
                        sub_gan_cop = (sub_ing_cop - sub_costo_cop) if sub_ing_cop is not None else None
                    results.append({
                        "isin": isin, "name": name, "type": typ,
                        "sell_date": sell_date, "qty": sub_qty,
                        "ingreso_usd": sub_ing_usd, "trm_venta": trm_venta,
                        "ingreso_cop": sub_ing_cop, "costo_usd": sub_costo_usd,
                        "costo_cop": sub_costo_cop, "ganancia_usd": sub_gan_usd,
                        "ganancia_cop": sub_gan_cop, "clasificacion": plazo_label,
                        "broker": broker, "lots": sub_lots,
                    })
            continue

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
            "broker":       broker,
            "lots":         lot_detail,
        })

    conn.close()

    # ── Modo tabla (una fila por lote FIFO)
    if TABLE:
        filtro_label = f" — solo {FILTER.upper()} PLAZO" if FILTER else ""

        if CSV_MODE:
            import csv as _csv
            writer = _csv.writer(sys.stdout)
            writer.writerow([
                "Broker", "Instrumento", "F. Venta", "F. Compra", "Días",
                "Cant", "Costo USD", "Venta USD", "Gan USD",
                "TRM Compra", "Costo COP", "TRM Venta", "Venta COP", "Gan COP", "Plazo",
            ])

            totals_t = {"LARGO": [0.0]*4, "CORTO": [0.0]*4, "STC": [0.0]*4}
            for r in results:
                clsf = r["clasificacion"]
                if clsf == "STC" and not SHOW_STC:
                    continue
                if FILTER and clsf.lower() != FILTER and clsf != "STC":
                    continue
                sell_date   = r["sell_date"]
                name        = r["name"]
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
                    dias      = d["dias"]
                    plazo     = "STC" if clsf == "STC" else ("LARGO" if d["largo"] else "CORTO")

                    writer.writerow([
                        r["broker"].upper(), name, sell_date, d["buy_date"], dias,
                        round(lot_qty, 4),
                        round(costo_usd, 2), round(venta_usd, 2), round(gan_usd, 2),
                        round(trm_c, 2), round(costo_cop, 0),
                        round(trm_v, 2), round(venta_cop, 0), round(gan_cop, 0),
                        plazo,
                    ])
                    totals_t[plazo][0] += gan_usd
                    totals_t[plazo][1] += gan_cop
                    totals_t[plazo][2] += venta_usd
                    totals_t[plazo][3] += venta_cop

            writer.writerow([])
            for plazo, (gu, gc, vu, vc) in totals_t.items():
                if plazo == "STC" and not SHOW_STC:
                    continue
                if FILTER and plazo.lower() != FILTER and plazo != "STC":
                    continue
                writer.writerow([f"TOTAL {plazo}", "", "", "", "", "", "", round(vu, 2), round(gu, 2),
                                  "", "", "", round(vc, 0), round(gc, 0), plazo])
            total_gu = sum(v[0] for v in totals_t.values())
            total_gc = sum(v[1] for v in totals_t.values())
            total_vu = sum(v[2] for v in totals_t.values())
            total_vc = sum(v[3] for v in totals_t.values())
            writer.writerow(["TOTAL VENTAS", "", "", "", "", "", "", round(total_vu, 2), round(total_gu, 2),
                              "", "", "", round(total_vc, 0), round(total_gc, 0), ""])
            return
        HDR = (f"{'Instrumento':<36}  {'F.Venta':>10}  {'F.Compra':>10}  {'Días':>5}  "
               f"{'Cant':>8}  {'Costo USD':>11}  {'Venta USD':>11}  {'Gan USD':>10}  "
               f"{'TRM Compra':>10}  {'Costo COP':>14}  {'TRM Venta':>10}  "
               f"{'Venta COP':>14}  {'Gan COP':>14}  {'Plazo':<10}")
        print(f"\n{'='*len(HDR)}")
        print(f"  Reporte de ganancias/pérdidas — una fila por lote FIFO — Año {YEAR}{filtro_label}")
        print(f"{'='*len(HDR)}\n")
        print(f"  {HDR}")
        print(f"  {'─'*len(HDR)}")

        totals_t = {"LARGO": [0.0, 0.0, 0.0, 0.0],   # gan_usd, gan_cop, venta_usd, venta_cop
                    "CORTO": [0.0, 0.0, 0.0, 0.0],
                    "STC":   [0.0, 0.0, 0.0, 0.0]}

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
                dias      = d["dias"]
                plazo     = "STC" if clsf == "STC" else ("LARGO" if d["largo"] else "CORTO")

                print(f"  {name:<36}  {sell_date:>10}  {d['buy_date']:>10}  {dias:>5}  "
                      f"{lot_qty:>8.4f}  {costo_usd:>11,.2f}  {venta_usd:>11,.2f}  {gan_usd:>+10,.2f}  "
                      f"{trm_c:>10,.2f}  {costo_cop:>14,.0f}  {trm_v:>10,.2f}  "
                      f"{venta_cop:>14,.0f}  {gan_cop:>+14,.0f}  {plazo:<10}")

                totals_t[plazo][0] += gan_usd
                totals_t[plazo][1] += gan_cop
                totals_t[plazo][2] += venta_usd
                totals_t[plazo][3] += venta_cop

        print(f"  {'─'*len(HDR)}")
        for plazo, (gu, gc, vu, vc) in totals_t.items():
            if plazo == "STC" and not SHOW_STC:
                continue
            if FILTER and plazo.lower() != FILTER and plazo != "STC":
                continue
            label = {"LARGO": f"LARGO (>{DIAS_LARGO_PLAZO}d)", "CORTO": f"CORTO (≤{DIAS_LARGO_PLAZO}d)", "STC": "STC"}[plazo]
            print(f"  {'TOTAL '+label:<36}  {'':>10}  {'':>10}  {'':>5}  "
                  f"{'':>8}  {'':>11}  {vu:>11,.2f}  {gu:>+10,.2f}  "
                  f"{'':>10}  {'':>14}  {'':>10}  "
                  f"{vc:>14,.0f}  {gc:>+14,.0f}  {plazo:<10}")

        total_gan_u = sum(v[0] for v in totals_t.values())
        total_gan_c = sum(v[1] for v in totals_t.values())
        total_ven_u = sum(v[2] for v in totals_t.values())
        total_ven_c = sum(v[3] for v in totals_t.values())
        print(f"  {'─'*len(HDR)}")
        print(f"  {'TOTAL VENTAS':<36}  {'':>10}  {'':>10}  {'':>5}  "
              f"{'':>8}  {'':>11}  {total_ven_u:>11,.2f}  {total_gan_u:>+10,.2f}  "
              f"{'':>10}  {'':>14}  {'':>10}  "
              f"{total_ven_c:>14,.0f}  {total_gan_c:>+14,.0f}")

        print(f"\n  Notas:")
        print(f"  • Largo plazo: tenencia > {DIAS_LARGO_PLAZO} días  |  Corto plazo: ≤ {DIAS_LARGO_PLAZO} días")
        print(f"  • Una fila por lote FIFO. Venta USD/COP prorrateada por cantidad.")
        print(f"  • Costo COP = costo USD × TRM del día de COMPRA (Banco de la República)")
        print(f"  • Venta COP = venta USD × TRM del día de VENTA\n")
        return

    # ── Modo summary
    W = 120
    print(f"\n{'='*W}")
    print(f"  Reporte de ganancias/pérdidas — Año {YEAR}")
    print(f"  Temporalidad: largo plazo > {DIAS_LARGO_PLAZO} días  |  corto plazo ≤ {DIAS_LARGO_PLAZO} días")
    print(f"{'='*W}\n")

    totals = {"LARGO": [0.0, 0.0], "CORTO": [0.0, 0.0], "STC": [0.0, 0.0]}

    for r in results:
        if r["clasificacion"] == "STC" and not SHOW_STC:
            continue
        if FILTER and r["clasificacion"].lower() != FILTER and r["clasificacion"] != "STC":
            continue
        clsf  = r["clasificacion"]
        gn_u  = r["ganancia_usd"]
        gn_c  = r["ganancia_cop"] or 0.0

        sign  = "✅" if gn_u >= 0 else "🔴"
        tag   = {"LARGO": "🟡 LARGO PLAZO", "CORTO": "🔵 CORTO PLAZO",
                  "STC":  "⚪ STC"}[clsf]

        print(f"  {sign} {tag}  │  {r['name'][:40]:<40}  │  {r['sell_date']}  │  {r['qty']:.4f} uds")
        trm_v_str = f"{r['trm_venta']:>9,.2f}" if r['trm_venta'] else f"{'sin TRM':>9}"
        print(f"     Ingreso :  ${r['ingreso_usd']:>12,.2f} USD  │  TRM venta {trm_v_str}  │  ${r['ingreso_cop'] or 0:>16,.0f} COP")
        print(f"     Costo   :  ${r['costo_usd']:>12,.2f} USD  │                    │  ${r['costo_cop'] or 0:>16,.0f} COP")
        print(f"     Ganancia:  ${gn_u:>+12,.2f} USD  │                    │  ${gn_c:>+16,.0f} COP")

        if DETAIL:
            print(f"     {'─'*100}")
            print(f"     {'Lote compra':<12} {'Qty':>8} {'P.costo USD':>12} {'Días':>6} {'Plazo':<12} {'Costo USD':>12} {'TRM compra':>12} {'Costo COP':>16}")
            for d in r["lots"]:
                plazo = f"LARGO (>{DIAS_LARGO_PLAZO}d)" if d["largo"] else f"CORTO (≤{DIAS_LARGO_PLAZO}d)"
                cop_s = f"${d['costo_cop']:>14,.0f}" if d["costo_cop"] else "     sin TRM"
                print(f"     {d['buy_date']:<12} {d['qty']:>8.4f} ${d['price_usd']:>11,.2f} "
                      f"{d['dias']:>6} {plazo:<14} ${d['costo_usd']:>11,.2f} "
                      f"{d['trm_c']:>12,.2f} {cop_s}")
        print()

        if clsf in totals:
            totals[clsf][0] += gn_u
            totals[clsf][1] += gn_c

    # ── Resumen
    print(f"  {'─'*W}")
    print(f"\n  RESUMEN {YEAR}\n")
    print(f"  {'Temporalidad':<16} {'Días tenencia':>15} {'Ganancia USD':>15} {'Ganancia COP':>20}")
    print(f"  {'─'*70}")
    labels = {
        "LARGO": f"> {DIAS_LARGO_PLAZO} días",
        "CORTO": f"≤ {DIAS_LARGO_PLAZO} días",
        "STC":   "0 días (STC)",
    }
    for clsf, (gu, gc) in totals.items():
        if clsf == "STC" and not SHOW_STC:
            continue
        print(f"  {clsf:<16} {labels[clsf]:>15} ${gu:>+14,.2f} ${gc:>+19,.0f}")

    total_u = sum(v[0] for v in totals.values())
    total_c = sum(v[1] for v in totals.values())
    print(f"  {'─'*70}")
    print(f"  {'TOTAL':<16} {'':>15} ${total_u:>+14,.2f} ${total_c:>+19,.0f}")

    if fifo_errors:
        print(f"\n  ⚠  ADVERTENCIAS ({len(fifo_errors)}):")
        for e in fifo_errors:
            print(f"     • {e}")

    print(f"\n  Notas:")
    print(f"  • Largo plazo: tenencia > {DIAS_LARGO_PLAZO} días  |  Corto plazo: ≤ {DIAS_LARGO_PLAZO} días")
    print(f"  • Costo en COP = precio USD × TRM del día de COMPRA (Banco de la República)")
    print(f"  • Ingreso en COP = ingreso USD × TRM del día de VENTA")
    print(f"  • FIFO puro: el lote más antiguo se vende primero")
    print(f"  • Usar --detail para ver lotes FIFO individuales por venta\n")


if __name__ == "__main__":
    run()
