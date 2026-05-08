#!/usr/bin/env python3
"""Insert transactions into portfolio.db.

Usage (called by Claude via skill):
    python3 tools/insert.py security '<json>'
    python3 tools/insert.py transaction '<json>'
    python3 tools/insert.py query '<sql>'
    python3 tools/insert.py transaction '<json>' --force
"""
import json, sqlite3, sys, os

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

def get_db():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn

def find_duplicate(conn, data: dict):
    """
    Return the transaction ID if a probable duplicate exists, else None.
    Duplicate key: (security_id, date, type, broker, quantity, price).
    """
    row = conn.execute(
        "SELECT id FROM transactions "
        "WHERE security_id = ? AND date = ? AND type = ? AND broker = ? "
        "AND ABS(quantity - ?) < 0.0001 AND ABS(COALESCE(price,0) - ?) < 0.0001",
        (
            data["security_id"], data["date"], data["type"], data["broker"],
            data["quantity"], data.get("price", 0) or 0
        )
    ).fetchone()
    return row[0] if row else None

def upsert_security(data: dict) -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO securities (isin, name, type, currency) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(isin) DO UPDATE SET name=excluded.name, type=excluded.type, currency=excluded.currency "
        "RETURNING id",
        (data["isin"], data["name"], data["type"], data["currency"])
    )
    row = cur.fetchone()
    conn.commit()
    conn.close()
    return row[0]

def insert_transaction(data: dict, force: bool = False) -> int:
    conn = get_db()
    # resolve security_id from isin
    row = conn.execute("SELECT id FROM securities WHERE isin = ?", (data["isin"],)).fetchone()
    if not row:
        print(f"ERROR: security {data['isin']} not found. Insert it first.", file=sys.stderr)
        sys.exit(1)
    sec_id = row[0]

    # duplicate check
    if not force:
        check_data = {**data, "security_id": sec_id}
        dup_id = find_duplicate(conn, check_data)
        if dup_id is not None:
            print(f"  ⚠  Probable duplicate of transaction id={dup_id} "
                  f"({data['date']} {data['type']} {data['quantity']} @ {data.get('price')}). "
                  f"Skipping. Pass --force to insert anyway.", file=sys.stderr)
            conn.close()
            sys.exit(2)

    cur = conn.execute(
        "INSERT INTO transactions (security_id, date, type, broker, quantity, price, currency, total, fee, exchange, notes, source_file) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (sec_id, data["date"], data["type"], data["broker"], data["quantity"],
         data.get("price"), data["currency"], data.get("total"),
         data.get("fee", 0), data.get("exchange"), data.get("notes"), data.get("source_file"))
    )
    tid = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return tid

def run_query(sql: str):
    conn = get_db()
    cur = conn.execute(sql)
    rows = cur.fetchall()
    if rows:
        cols = [d[0] for d in cur.description]
        print("\t".join(cols))
        for r in rows:
            print("\t".join(str(v) for v in r))
    else:
        print("(no rows)")
    conn.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["security", "transaction", "query"])
    parser.add_argument("arg", nargs="?")
    parser.add_argument("--force", action="store_true", help="Insert even if duplicate detected")
    args = parser.parse_args()

    if args.cmd == "security":
        sid = upsert_security(json.loads(args.arg))
        print(f"security_id={sid}")
    elif args.cmd == "transaction":
        data = json.loads(args.arg)
        tid = insert_transaction(data, force=args.force)
        print(f"transaction_id={tid}")
    elif args.cmd == "query":
        run_query(args.arg)
