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
        print(f"  ⚠ to_sec_ccy_price: moneda Yahoo '{yahoo_ccy}' no soportada — tratando como USD",
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
            result[t] = (result[t][0], "USD")  # fallback

    return result
