#!/usr/bin/env python3
"""
Manage specific-lot assignments for sell transactions.

Usage:
    python3 tools/assign_lot.py list [sell_id]
        Show all assignments, or only those for a given sell_id.

    python3 tools/assign_lot.py add <sell_id> <buy_id> <qty>
        Assign qty units of buy_id to sell_id.
        Validates that both transaction IDs exist and have compatible ISINs.

    python3 tools/assign_lot.py delete <sell_id> [buy_id]
        Delete all assignments for sell_id, or only the sell_id+buy_id pair.

    python3 tools/assign_lot.py show <sell_id>
        Show the sell transaction and its available lots for the same ISIN.
"""
import sqlite3, sys, os

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")


def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_list(conn, sell_id=None):
    if sell_id is not None:
        rows = conn.execute("""
            SELECT
                la.id, la.sell_id, la.buy_id, la.quantity,
                s_sell.date AS sell_date, sec_sell.name AS sell_name,
                s_buy.date  AS buy_date,  sec_buy.name  AS buy_name
            FROM lot_assignments la
            JOIN transactions s_sell ON s_sell.id = la.sell_id
            JOIN securities sec_sell ON sec_sell.id = s_sell.security_id
            JOIN transactions s_buy  ON s_buy.id  = la.buy_id
            JOIN securities sec_buy  ON sec_buy.id = s_buy.security_id
            WHERE la.sell_id = ?
            ORDER BY la.sell_id, la.id
        """, (sell_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT
                la.id, la.sell_id, la.buy_id, la.quantity,
                s_sell.date AS sell_date, sec_sell.name AS sell_name,
                s_buy.date  AS buy_date,  sec_buy.name  AS buy_name
            FROM lot_assignments la
            JOIN transactions s_sell ON s_sell.id = la.sell_id
            JOIN securities sec_sell ON sec_sell.id = s_sell.security_id
            JOIN transactions s_buy  ON s_buy.id  = la.buy_id
            JOIN securities sec_buy  ON sec_buy.id = s_buy.security_id
            ORDER BY la.sell_id, la.id
        """).fetchall()

    if not rows:
        print("No lot assignments found.")
        return

    print(f"\n  {'ID':>4}  {'sell_id':>7}  {'sell_date':<12}  {'buy_id':>6}  {'buy_date':<12}  {'qty':>8}  security")
    print(f"  {'-'*80}")
    for r in rows:
        print(f"  {r['id']:>4}  {r['sell_id']:>7}  {r['sell_date']:<12}  "
              f"{r['buy_id']:>6}  {r['buy_date']:<12}  {r['quantity']:>8.4f}  {r['sell_name']}")
    print()


def cmd_show(conn, sell_id):
    """Show the sell transaction and available buy lots for the same security."""
    sell = conn.execute("""
        SELECT t.*, s.isin, s.name FROM transactions t
        JOIN securities s ON s.id = t.security_id
        WHERE t.id = ? AND t.type = 'sell'
    """, (sell_id,)).fetchone()

    if not sell:
        print(f"Error: transaction id={sell_id} not found or is not a sell.")
        print(f"  (sell_to_cover cannot have lot assignments — gain=$0 regardless of lot)")
        sys.exit(1)

    print(f"\n  Sell: id={sell_id}  {sell['date']}  {sell['name']}  "
          f"qty={sell['quantity']}  broker={sell['broker']}")

    buys = conn.execute("""
        SELECT t.id, t.date, t.quantity, t.total, t.currency, t.broker
        FROM transactions t
        JOIN securities s ON s.id = t.security_id
        WHERE s.isin = ? AND t.type IN ('buy', 'vesting')
          AND t.date <= ?
        ORDER BY t.date
    """, (sell['isin'], sell['date'])).fetchall()

    if not buys:
        print("  No buy lots found for this security before the sell date.")
        return

    print(f"\n  Available buy lots (same ISIN, on or before sell date):")
    print(f"  {'buy_id':>6}  {'date':<12}  {'qty':>8}  {'total':>10}  broker")
    print(f"  {'-'*55}")
    for b in buys:
        print(f"  {b['id']:>6}  {b['date']:<12}  {b['quantity']:>8.4f}  "
              f"{b['total']:>10.2f}  {b['broker']}")
    print()


def cmd_add(conn, sell_id, buy_id, qty):
    sell = conn.execute(
        "SELECT t.*, s.isin FROM transactions t JOIN securities s ON s.id=t.security_id WHERE t.id=?",
        (sell_id,)
    ).fetchone()
    if not sell or sell["type"] not in ("sell",):
        print(f"Error: id={sell_id} is not a sell transaction.")
        print(f"  (sell_to_cover uses FIFO always — gain=$0 regardless of lot choice)")
        sys.exit(1)

    buy = conn.execute(
        "SELECT t.*, s.isin FROM transactions t JOIN securities s ON s.id=t.security_id WHERE t.id=?",
        (buy_id,)
    ).fetchone()
    if not buy or buy["type"] not in ("buy", "vesting"):
        print(f"Error: id={buy_id} is not a buy/vesting transaction.")
        sys.exit(1)

    if sell["isin"] != buy["isin"]:
        print(f"Error: ISIN mismatch — sell is {sell['isin']}, buy is {buy['isin']}.")
        sys.exit(1)

    if buy["date"] > sell["date"]:
        print(f"Error: buy date {buy['date']} is after sell date {sell['date']}.")
        sys.exit(1)

    # Validate cumulative assignment does not exceed sell quantity
    existing = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM lot_assignments WHERE sell_id=?",
        (sell_id,)
    ).fetchone()[0]
    if existing + qty > sell["quantity"] + 1e-6:
        print(f"Error: assignment would exceed sell quantity.")
        print(f"  Sell qty:     {sell['quantity']:.4f}")
        print(f"  Already assigned: {existing:.4f}")
        print(f"  Requested now:    {qty:.4f}")
        print(f"  Total would be:   {existing + qty:.4f}")
        sys.exit(1)

    conn.execute(
        "INSERT INTO lot_assignments (sell_id, buy_id, quantity) VALUES (?, ?, ?)",
        (sell_id, buy_id, qty)
    )
    conn.commit()
    print(f"✓ Assigned {qty} units of buy id={buy_id} ({buy['date']}) "
          f"to sell id={sell_id} ({sell['date']}).")
    remaining = sell["quantity"] - existing - qty
    if remaining > 1e-6:
        print(f"  ℹ️  {remaining:.4f} units still unassigned — will use FIFO for those.")


def cmd_delete(conn, sell_id, buy_id=None):
    if buy_id is not None:
        conn.execute(
            "DELETE FROM lot_assignments WHERE sell_id=? AND buy_id=?",
            (sell_id, buy_id)
        )
    else:
        conn.execute("DELETE FROM lot_assignments WHERE sell_id=?", (sell_id,))
    conn.commit()
    print(f"✓ Deleted assignment(s) for sell_id={sell_id}"
          + (f" buy_id={buy_id}" if buy_id is not None else "") + ".")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    conn = get_conn()
    cmd = sys.argv[1]

    if cmd == "list":
        sell_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
        cmd_list(conn, sell_id)

    elif cmd == "show":
        if len(sys.argv) < 3:
            print("Usage: assign_lot.py show <sell_id>")
            sys.exit(1)
        cmd_show(conn, int(sys.argv[2]))

    elif cmd == "add":
        if len(sys.argv) < 5:
            print("Usage: assign_lot.py add <sell_id> <buy_id> <qty>")
            sys.exit(1)
        cmd_add(conn, int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]))

    elif cmd == "delete":
        if len(sys.argv) < 3:
            print("Usage: assign_lot.py delete <sell_id> [buy_id]")
            sys.exit(1)
        buy_id = int(sys.argv[3]) if len(sys.argv) > 3 else None
        cmd_delete(conn, int(sys.argv[2]), buy_id)

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()
