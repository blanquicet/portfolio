-- IBKR-only snapshot: positions that are currently at IBKR
-- Matches what you see in the IBKR app today
-- EUR positions: native currency shown; USD for US stocks/ETFs

SELECT
  s.name                                          AS security,
  s.currency                                      AS ccy,
  ROUND(SUM(
    CASE WHEN t.type IN ('buy','vesting','transfer_in') THEN  t.quantity
         WHEN t.type IN ('sell','sell_to_cover','transfer_out') THEN -t.quantity
         ELSE 0 END
  ), 4)                                            AS net_qty,
  ROUND(
    SUM(CASE WHEN t.type IN ('buy','vesting') THEN t.total ELSE 0 END) /
    NULLIF(SUM(CASE WHEN t.type IN ('buy','vesting') THEN t.quantity ELSE 0 END), 0)
  , 2)                                             AS avg_cost_native
FROM transactions t
JOIN securities s ON s.id = t.security_id
WHERE t.broker = 'ibkr'
  AND t.date <= date('now')
GROUP BY s.id, s.name, s.currency
HAVING net_qty > 0.001
ORDER BY s.name;
