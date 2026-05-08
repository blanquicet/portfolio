---
name: snapshot
description: Show live portfolio snapshot with market prices, unrealized P&L, and portfolio weights. Accepts optional broker filter (ibkr, fidelity).
---

# Portfolio Snapshot

Run the snapshot script and show the output to the user.

## Steps

1. Determine the broker filter:
   - If the user said "ibkr" or "IBKR" → pass `ibkr`
   - If the user said "fidelity" or "Fidelity" → pass `fidelity`
   - Otherwise → no argument (all brokers)

2. Run:
```bash
cd /Users/melendex/Documents/src/portfolio && python3 tools/snapshot.py [broker]
```

3. Show the full output to the user as-is. Do not summarize or truncate it.

4. After the table, offer one follow-up:
   > "Want me to dig into any position, or export this to CSV?"
