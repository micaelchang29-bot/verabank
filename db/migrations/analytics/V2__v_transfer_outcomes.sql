CREATE VIEW analytics.v_transfer_outcomes AS
SELECT t.transfer_id, t.occurred_at, t.created_at, c.segment, t.transfer_type, t.rail,
       t.amount, t.status, t.fraud_decision, t.decision_source, t.model_version,
       t.scoring_latency_ms,
       COALESCE(l.is_fraud, false) AS is_fraud,
       t.occurred_at < (SELECT max(occurred_at) FROM core.transfers) - interval '48 hours'
         AS label_mature
FROM core.transfers t
JOIN core.accounts  a ON a.account_id  = t.source_account_id
JOIN core.customers c ON c.customer_id = a.customer_id
LEFT JOIN risk.fraud_labels l ON l.transfer_id = t.transfer_id;
