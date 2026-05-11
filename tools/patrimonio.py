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
