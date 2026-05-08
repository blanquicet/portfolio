#!/usr/bin/env python3
"""Insert transactions into portfolio.db.

Usage (called by Claude via skill):
    python3 tools/insert.py security '<json>'
    python3 tools/insert.py transaction '<json>'
    python3 tools/insert.py query '<sql>'
"""
import json, sqlite3, sys, os

DB = os.path.join(os.path.dirname(__file__), "..", "portfolio.db")

def get_db():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn

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

def insert_transaction(data: dict) -> int:
    conn = get_db()
    # resolve security_id from isin
    row = conn.execute("SELECT id FROM securities WHERE isin = ?", (data["isin"],)).fetchone()
    if not row:
        print(f"ERROR: security {data['isin']} not found. Insert it first.", file=sys.stderr)
        sys.exit(1)
    sec_id = row[0]
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
    cmd = sys.argv[1]
    if cmd == "security":
        sid = upsert_security(json.loads(sys.argv[2]))
        print(f"security_id={sid}")
    elif cmd == "transaction":
        tid = insert_transaction(json.loads(sys.argv[2]))
        print(f"transaction_id={tid}")
    elif cmd == "query":
        run_query(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
