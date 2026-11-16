-- Comprobaciones tras make chaos-rail. Una fila por comprobación: nombre | valor | ok (t/f).
-- scripts/chaos-checks.sh las ejecuta con psql, igual que make reconcile, y falla si alguna ok es f.
-- Alcance: toda la base. Las transferencias de la semilla (INTERNAL) no entran en las de rail EXTERNAL.

WITH ext AS (
  SELECT transfer_id, status, source_account_id
  FROM core.transfers
  WHERE rail = 'EXTERNAL'
),
completed_ok AS (                     -- COMPLETED externa: exactamente un D en origen y un C en CLEARING
  SELECT e.transfer_id
  FROM ext e
  WHERE e.status = 'COMPLETED'
    AND (SELECT count(*) FROM core.transactions x WHERE x.transfer_id = e.transfer_id) = 2
    AND (SELECT count(*) FROM core.transactions x
         WHERE x.transfer_id = e.transfer_id AND x.direction = 'D' AND x.account_id = e.source_account_id) = 1
    AND (SELECT count(*) FROM core.transactions x JOIN core.accounts a USING (account_id)
         WHERE x.transfer_id = e.transfer_id AND x.direction = 'C' AND a.account_type = 'CLEARING') = 1
),
comp AS (                             -- compensaciones por fallo del rail, en segundos
  SELECT extract(epoch FROM s.updated_at - (s.payload->>'compensation_started_at')::timestamptz) AS secs
  FROM core.saga_state s
  JOIN core.transfers t ON t.transfer_id = s.saga_id
  WHERE s.status = 'COMPENSATED'
    AND t.status = 'FAILED'
    AND s.payload ? 'compensation_started_at'
),
p99 AS (SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY secs) AS v, count(*) AS n FROM comp)
SELECT 'sagas_no_finales' AS comprobacion,
       (SELECT count(*) FROM core.saga_state WHERE status NOT IN ('COMPLETED', 'COMPENSATED'))::text AS valor,
       (SELECT count(*) FROM core.saga_state WHERE status NOT IN ('COMPLETED', 'COMPENSATED')) = 0 AS ok
UNION ALL
SELECT 'completed_externas_sin_par_D_C_exacto',
       (SELECT count(*) FROM ext WHERE status = 'COMPLETED' AND transfer_id NOT IN (SELECT transfer_id FROM completed_ok))::text,
       (SELECT count(*) FROM ext WHERE status = 'COMPLETED' AND transfer_id NOT IN (SELECT transfer_id FROM completed_ok)) = 0
UNION ALL
SELECT 'failed_con_asientos',
       (SELECT count(*) FROM core.transfers t WHERE t.status = 'FAILED'
          AND EXISTS (SELECT 1 FROM core.transactions x WHERE x.transfer_id = t.transfer_id))::text,
       (SELECT count(*) FROM core.transfers t WHERE t.status = 'FAILED'
          AND EXISTS (SELECT 1 FROM core.transactions x WHERE x.transfer_id = t.transfer_id)) = 0
UNION ALL
SELECT 'asientos_duplicados',
       (SELECT count(*) FROM (SELECT 1 FROM core.transactions
                              GROUP BY transfer_id, account_id, direction, entry_kind HAVING count(*) > 1) d)::text,
       (SELECT count(*) FROM (SELECT 1 FROM core.transactions
                              GROUP BY transfer_id, account_id, direction, entry_kind HAVING count(*) > 1) d) = 0
UNION ALL
SELECT 'p99_compensacion_s (' || p99.n || ' compensadas, máx 60)',
       COALESCE(round(p99.v::numeric, 3)::text, 'n/a'),
       COALESCE(p99.v <= 60, true)
FROM p99;
