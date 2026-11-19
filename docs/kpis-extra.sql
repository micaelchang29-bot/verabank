-- EXTRA A: drift activo (alertas de PSI de las dos últimas ventanas, por PSI)
SELECT d.window_start, d.window_end, d.feature, d.psi, d.n_current, d.model_version
FROM analytics.drift_metrics d
WHERE d.alert AND d.reference = 'valid'
  AND d.window_start >= (SELECT max(window_start) FROM analytics.drift_metrics WHERE reference = 'valid') - interval '24 hours'
ORDER BY d.psi DESC, d.feature;

-- EXTRA B: cola de revisión con razones (REVIEW abiertas y BLOCK del último día, con sus tres razones)
SELECT o.transfer_id, o.occurred_at, o.segment, o.amount, o.fraud_decision, o.status, o.model_version,
       string_agg(r.feature || ' (' || to_char(r.contribution, 'FMS990.000') || ')', ', ' ORDER BY r.rank) AS razones
FROM analytics.v_transfer_outcomes o
LEFT JOIN analytics.fraud_reasons r ON r.transfer_id = o.transfer_id
WHERE o.decision_source = 'MODEL'
  AND ((o.fraud_decision = 'REVIEW' AND o.status = 'IN_REVIEW')
       OR (o.fraud_decision = 'BLOCK'
           AND o.occurred_at >= (SELECT max(occurred_at) FROM analytics.v_transfer_outcomes) - interval '24 hours'))
GROUP BY o.transfer_id, o.occurred_at, o.segment, o.amount, o.fraud_decision, o.status, o.model_version
ORDER BY (o.status = 'IN_REVIEW') DESC, o.occurred_at DESC
LIMIT 500;
