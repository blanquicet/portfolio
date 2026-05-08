-- Portfolio snapshot: current net positions per broker
-- Run: sqlite3 portfolio.db < queries/snapshot.sql
-- or:  sqlite3 portfolio.db "$(cat queries/snapshot.sql)"

SELECT
  s.name                                          AS security,
  s.currency                                      AS native_ccy,
  ROUND(SUM(
    CASE WHEN t.type IN ('buy','vesting','transfer_in') THEN  t.quantity
         WHEN t.type IN ('sell','sell_to_cover','transfer_out') THEN -t.quantity
         ELSE 0 END
  ), 4)                                            AS net_qty,
  -- Avg cost basis (buys + vestings only, in native currency)
  ROUND(
    SUM(CASE WHEN t.type IN ('buy','vesting') THEN t.total ELSE 0 END) /
    NULLIF(SUM(CASE WHEN t.type IN ('buy','vesting') THEN t.quantity ELSE 0 END), 0)
  , 4)                                             AS avg_cost,
  -- Where it sits today
  CASE
    WHEN MAX(CASE WHEN t.type='transfer_out' THEN t.date ELSE NULL END) >
         MAX(CASE WHEN t.type='transfer_in'  THEN t.date ELSE NULL END)
    THEN 'transferred_out'
    WHEN SUM(CASE WHEN t.broker='fidelity' AND t.type='transfer_out' THEN t.quantity ELSE 0 END) <
         SUM(CASE WHEN t.broker='fidelity' AND t.type='vesting' THEN t.quantity ELSE 0 END)
         AND s.name='Microsoft Corp'
    THEN 'split:fidelity+ibkr'
    ELSE MAX(t.broker)
  END                                              AS primary_broker
FROM transactions t
JOIN securities s ON s.id = t.security_id
WHERE t.date <= date('now')
GROUP BY s.id, s.name, s.currency
HAVING net_qty > 0.001
ORDER BY s.name;
