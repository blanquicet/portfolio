#!/usr/bin/env python3
"""
Patrimonio histórico al 31-dic de un año dado.

Usage:
    python3 tools/patrimonio.py 2025              # → snapshot al 2025-12-31
    python3 tools/patrimonio.py 2024              # → snapshot al 2024-12-31
    python3 tools/patrimonio.py --as-of 2025-06-30  # → snapshot a fecha arbitraria
"""
import sqlite3, sys, os, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(__file__))
from fifo import build_queues, fx

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")


def calc_lot_costs(qty, price_usd, sec_ccy, trm_compra, eur_usd_compra):
    """
    Calcula costo en sec_ccy y costo en COP para un lote FIFO.

    Args:
        qty:            cantidad restante del lote (ya prorrateada)
        price_usd:      precio por unidad en USD (calculado por build_queues)
        sec_ccy:        moneda del security ('USD', 'EUR', 'COP')
        trm_compra:     TRM (USD/COP) en la fecha de compra, o None
        eur_usd_compra: EUR/USD en la fecha de compra, o None

    Returns:
        dict con 'cost_sec' (en sec_ccy) y 'cost_cop', ambos pueden ser None.
        Si price_usd es None (FX de compra faltante), retorna {'cost_sec': None, 'cost_cop': None}.
    """
    if price_usd is None:
        return {"cost_sec": None, "cost_cop": None}

    cost_usd = price_usd * qty

    if sec_ccy == "USD":
        cost_sec = cost_usd
        cost_cop = cost_usd * trm_compra if trm_compra is not None else None

    elif sec_ccy == "EUR":
        cost_sec = cost_usd / eur_usd_compra if eur_usd_compra is not None else None
        cost_cop = cost_usd * trm_compra if trm_compra is not None else None

    elif sec_ccy == "COP":
        cost_sec = cost_usd * trm_compra if trm_compra is not None else None
        cost_cop = cost_sec  # COP security: native cost equals COP cost

    else:
        print(f"  ⚠ calc_lot_costs: sec_ccy '{sec_ccy}' no soportado — tratando como USD",
              file=sys.stderr)
        cost_sec = cost_usd
        cost_cop = cost_usd * trm_compra if trm_compra is not None else None

    return {"cost_sec": cost_sec, "cost_cop": cost_cop}


def to_sec_ccy_price(yahoo_price, yahoo_ccy, sec_ccy, eur_usd, trm, gbp_usd):
    """
    Convierte precio Yahoo → precio en sec_ccy.

    Paso 1: yahoo_ccy → USD
    Paso 2: USD → sec_ccy

    Devuelve None si falta una tasa necesaria.
    """
    # Paso 1: yahoo → USD
    if yahoo_ccy is None:
        return None
    yc = yahoo_ccy.strip()
    if yc == "USD":
        price_usd = yahoo_price
    elif yc == "EUR":
        if eur_usd is None:
            return None
        price_usd = yahoo_price * eur_usd
    elif yc == "GBP":
        if gbp_usd is None:
            return None
        price_usd = yahoo_price * gbp_usd
    elif yc == "GBp":
        if gbp_usd is None:
            return None
        price_usd = yahoo_price / 100 * gbp_usd
    else:
        print(f"  ⚠ to_sec_ccy_price: moneda Yahoo '{yc}' no soportada — tratando como USD",
              file=sys.stderr)
        price_usd = yahoo_price

    # Paso 2: USD → sec_ccy
    if sec_ccy == "USD":
        return price_usd
    elif sec_ccy == "EUR":
        if eur_usd is None:
            return None
        return price_usd / eur_usd
    elif sec_ccy == "COP":
        if trm is None:
            return None
        return price_usd * trm
    else:
        print(f"  ⚠ to_sec_ccy_price: sec_ccy '{sec_ccy}' no soportado", file=sys.stderr)
        return price_usd


def fetch_historical_prices(tickers, as_of):
    """
    Descarga precios de cierre históricos para una lista de tickers.

    Args:
        tickers: lista de strings (Yahoo Finance tickers)
        as_of:   datetime.date — fecha de corte

    Returns:
        dict {ticker: (price_float, yahoo_ccy_str)}
        Si no hay precio: el ticker no está en el dict.
    """
    import yfinance as yf
    from datetime import timedelta

    if not tickers:
        return {}

    result = {}

    # Ventana amplia: 7 días antes hasta as_of+1 (maneja fines de semana y feriados)
    start = as_of - timedelta(days=7)
    end   = as_of + timedelta(days=1)

    try:
        raw = yf.download(
            tickers if len(tickers) > 1 else tickers[0],
            start=str(start), end=str(end),
            auto_adjust=False, progress=False
        )
        if not raw.empty:
            closes = raw["Close"]
            for t in tickers:
                try:
                    # yf.download(str) → Close is a Series; yf.download(list) → Close is a DataFrame
                    col  = closes[t] if len(tickers) > 1 else closes
                    last = col.dropna()
                    if not last.empty:
                        result[t] = (float(last.iloc[-1]), None)  # currency llenada abajo
                except Exception:
                    pass
    except Exception as e:
        print(f"  ⚠ fetch_historical_prices: error descargando precios: {e}", file=sys.stderr)

    # Obtener moneda de Yahoo para cada ticker con precio
    for t in list(result.keys()):
        try:
            info = yf.Ticker(t).fast_info
            ccy  = getattr(info, "currency", None) or "USD"
            result[t] = (result[t][0], ccy)
        except Exception:
            print(f"  ⚠ fetch_historical_prices: no se pudo obtener moneda para {t}, asumiendo USD",
                  file=sys.stderr)
            result[t] = (result[t][0], "USD")

    return result


def collect_lots(conn, as_of):
    """
    Construye grupos {(broker, sec_ccy): [lot_dict]} para la fecha as_of.

    lot_dict contiene:
        name, isin, ticker, qty, price_usd, buy_date,
        cost_sec, cost_cop, sec_ccy, broker,
        price_asof (None — relleno por run()), val_sec (None), val_cop (None)
    """
    as_of_str = str(as_of)

    brokers = [r[0] for r in conn.execute(
        "SELECT DISTINCT broker FROM transactions WHERE date <= ?", (as_of_str,)
    ).fetchall()]

    # Cargar ticker_mappings {isin: ticker} — manual gana sobre auto
    ticker_map = {}
    for row in conn.execute("SELECT isin, exchange, ticker, source FROM ticker_mappings"):
        isin, exch, ticker, source = row
        if isin not in ticker_map or source == "manual":
            ticker_map[isin] = ticker

    groups = {}

    for broker in brokers:
        queues, errors = build_queues(conn, as_of_date=as_of_str, broker=broker)
        for err in errors:
            print(f"  ⚠ FIFO: {err}", file=sys.stderr)

        for isin, queue in queues.items():
            for qty, price_usd, buy_date, src, buy_id in queue.remaining_lots_with_buy_id():
                row = conn.execute("""
                    SELECT s.name, s.currency
                    FROM transactions t
                    JOIN securities s ON s.id = t.security_id
                    WHERE t.id = ?
                """, (buy_id,)).fetchone()

                if row is None:
                    print(f"  ⚠ buy_id {buy_id} no encontrado en DB", file=sys.stderr)
                    continue

                name, sec_ccy = row[0], row[1]

                trm_compra     = fx(conn, "USD", "COP", buy_date)
                eur_usd_compra = fx(conn, "EUR", "USD", buy_date)

                if trm_compra is None:
                    print(f"  ⚠ TRM no disponible para {buy_date} — costo COP será None",
                          file=sys.stderr)

                costs = calc_lot_costs(qty, price_usd, sec_ccy, trm_compra, eur_usd_compra)

                lot = {
                    "name":       name,
                    "isin":       isin,
                    "ticker":     ticker_map.get(isin),
                    "qty":        qty,
                    "price_usd":  price_usd,
                    "buy_date":   buy_date,
                    "sec_ccy":    sec_ccy,
                    "broker":     broker,
                    "cost_sec":   costs["cost_sec"],
                    "cost_cop":   costs["cost_cop"],
                    "price_asof": None,
                    "val_sec":    None,
                    "val_cop":    None,
                }
                groups.setdefault((broker, sec_ccy), []).append(lot)

    return groups


def run(as_of):
    """Imprime el snapshot de patrimonio al as_of."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    as_of_str    = str(as_of)
    trm_asof     = fx(conn, "USD", "COP", as_of_str)
    eur_usd_asof = fx(conn, "EUR", "USD", as_of_str)
    gbp_usd_asof = fx(conn, "GBP", "USD", as_of_str)

    if trm_asof is None:
        print(f"  ⚠ TRM no disponible para {as_of_str} — valores COP serán None",
              file=sys.stderr)

    groups = collect_lots(conn, as_of)
    conn.close()

    all_tickers = list({
        lot["ticker"]
        for lots in groups.values()
        for lot in lots
        if lot["ticker"] is not None
    })

    print(f"\n  Descargando precios históricos al {as_of_str}…", end=" ", flush=True)
    price_map = fetch_historical_prices(all_tickers, as_of)
    print("listo.")

    for (broker, sec_ccy), lots in groups.items():
        for lot in lots:
            ticker = lot["ticker"]
            if ticker is None or ticker not in price_map:
                continue
            yahoo_price, yahoo_ccy = price_map[ticker]
            if yahoo_ccy is None:
                yahoo_ccy = "USD"
            price_sec = to_sec_ccy_price(
                yahoo_price, yahoo_ccy, sec_ccy,
                eur_usd=eur_usd_asof, trm=trm_asof, gbp_usd=gbp_usd_asof
            )
            if price_sec is not None:
                lot["price_asof"] = price_sec
                lot["val_sec"]    = price_sec * lot["qty"]
                if sec_ccy == "USD":
                    lot["val_cop"] = lot["val_sec"] * trm_asof if trm_asof else None
                elif sec_ccy == "EUR":
                    lot["val_cop"] = (lot["val_sec"] * eur_usd_asof * trm_asof
                                      if (eur_usd_asof and trm_asof) else None)
                elif sec_ccy == "COP":
                    lot["val_cop"] = lot["val_sec"]

    _print_report(groups, as_of_str, trm_asof, eur_usd_asof)


def _fmt(v, decimals=2, width=12):
    """Formatea número con separadores de miles. None → '—' alineado."""
    if v is None:
        return f"{'—':>{width}}"
    if decimals == 0:
        return f"{v:>{width},.0f}"
    return f"{v:>{width},.{decimals}f}"


def _print_report(groups, as_of_str, trm_asof, eur_usd_asof):
    W = 120
    print(f"\n{'═'*W}")
    print(f"  Patrimonio al {as_of_str}")
    if trm_asof:
        print(f"  TRM: {trm_asof:,.0f}   EUR/USD: {eur_usd_asof:.4f}" if eur_usd_asof
              else f"  TRM: {trm_asof:,.0f}")
    print(f"{'═'*W}")

    total_cost_cop      = 0.0
    total_val_cop       = 0.0
    missing_price_count = 0
    broker_totals       = {}

    for (broker, sec_ccy), lots in sorted(groups.items()):
        if not lots:
            continue

        is_cop    = sec_ccy == "COP"
        ccy_label = sec_ccy

        print(f"\n{broker.upper()} — {ccy_label}")

        if is_cop:
            print(f"  {'Instrumento':<36} {'Fecha cmp':>10}  {'Qty':>8}  "
                  f"{'Costo COP':>15}  {'Precio 31d':>12}  {'Valor COP':>15}")
        else:
            print(f"  {'Instrumento':<36} {'Fecha cmp':>10}  {'Qty':>8}  "
                  f"{'Costo '+ccy_label:>12}  {'TRM cmp':>8}  {'Costo COP':>15}  "
                  f"{'Precio 31d':>12}  {'Valor '+ccy_label:>12}  {'Valor COP':>15}")
        print(f"  {'─'*(W-2)}")

        sub_cost_sec = 0.0
        sub_cost_cop = 0.0
        sub_val_sec  = 0.0
        sub_val_cop  = 0.0

        for lot in sorted(lots, key=lambda x: x["buy_date"]):
            trm_cmp = None
            if not is_cop:
                cost_usd = lot["price_usd"] * lot["qty"] if lot["price_usd"] else None
                if lot["cost_cop"] is not None and cost_usd and cost_usd > 0:
                    trm_cmp = lot["cost_cop"] / cost_usd

            if lot["cost_sec"] is not None:
                sub_cost_sec += lot["cost_sec"]
            if lot["cost_cop"] is not None:
                sub_cost_cop += lot["cost_cop"]
            if lot["val_sec"] is not None:
                sub_val_sec += lot["val_sec"]
            if lot["val_cop"] is not None:
                sub_val_cop += lot["val_cop"]
            if lot["price_asof"] is None:
                missing_price_count += 1

            if is_cop:
                print(f"  {lot['name']:<36} {lot['buy_date']:>10}  {lot['qty']:>8.3f}  "
                      f"{_fmt(lot['cost_sec'], 0, 15)}  "
                      f"{_fmt(lot['price_asof'], 0, 12)}  "
                      f"{_fmt(lot['val_cop'], 0, 15)}")
            else:
                print(f"  {lot['name']:<36} {lot['buy_date']:>10}  {lot['qty']:>8.3f}  "
                      f"{_fmt(lot['cost_sec'], 2, 12)}  "
                      f"{_fmt(trm_cmp, 0, 8)}  "
                      f"{_fmt(lot['cost_cop'], 0, 15)}  "
                      f"{_fmt(lot['price_asof'], 2, 12)}  "
                      f"{_fmt(lot['val_sec'], 2, 12)}  "
                      f"{_fmt(lot['val_cop'], 0, 15)}")

        print(f"  {'─'*(W-2)}")
        if is_cop:
            print(f"  {'Subtotal':<36} {'':>10}  {'':>8}  "
                  f"{_fmt(sub_cost_sec, 0, 15)}  {'':>12}  {_fmt(sub_val_cop, 0, 15)}")
        else:
            print(f"  {'Subtotal':<36} {'':>10}  {'':>8}  "
                  f"{_fmt(sub_cost_sec, 2, 12)}  {'':>8}  "
                  f"{_fmt(sub_cost_cop, 0, 15)}  {'':>12}  "
                  f"{_fmt(sub_val_sec, 2, 12)}  {_fmt(sub_val_cop, 0, 15)}")

        total_cost_cop += sub_cost_cop
        total_val_cop  += sub_val_cop
        bt = broker_totals.setdefault(broker, {"cost_cop": 0.0, "val_cop": 0.0})
        bt["cost_cop"] += sub_cost_cop
        bt["val_cop"]  += sub_val_cop

    print(f"\n{'═'*W}")
    missing_note = f"  (* excluye {missing_price_count} lote(s) sin precio)" if missing_price_count else ""
    print(f"  TOTAL COP     Costo: {total_cost_cop:>15,.0f}     Valor: {total_val_cop:>15,.0f}{missing_note}")

    print(f"\n  Resumen por broker")
    print(f"  {'─'*60}")
    for broker, bt in sorted(broker_totals.items()):
        print(f"  {broker.upper():<12}  Costo COP: {bt['cost_cop']:>15,.0f}   Valor COP: {bt['val_cop']:>15,.0f}")
    print(f"  {'─'*60}")
    print(f"  {'TOTAL':<12}  Costo COP: {total_cost_cop:>15,.0f}   Valor COP: {total_val_cop:>15,.0f}")
    print()


if __name__ == "__main__":
    from datetime import date

    args  = sys.argv[1:]
    as_of = None

    if "--as-of" in args:
        idx   = args.index("--as-of")
        as_of = date.fromisoformat(args[idx + 1])
    else:
        year_arg = next((a for a in args if a.isdigit() and len(a) == 4), None)
        if year_arg is None:
            print("Uso: python3 tools/patrimonio.py <año>  |  --as-of YYYY-MM-DD")
            print("  Ej: python3 tools/patrimonio.py 2025")
            sys.exit(1)
        as_of = date(int(year_arg), 12, 31)

    run(as_of)
