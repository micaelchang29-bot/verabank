-- KPI 1: tasa de detección de fraude por día (casos y monto)
SELECT date_trunc('day', occurred_at) AS dia,
       count(*) FILTER (WHERE is_fraud) AS fraudes,
       round(100.0 * count(*) FILTER (WHERE is_fraud AND fraud_decision <> 'APPROVE')
             / NULLIF(count(*) FILTER (WHERE is_fraud), 0), 2) AS deteccion_pct,
       round(100.0 * COALESCE(sum(amount) FILTER (WHERE is_fraud AND fraud_decision <> 'APPROVE'), 0)
             / NULLIF(sum(amount) FILTER (WHERE is_fraud), 0), 2) AS monto_detectado_pct
FROM analytics.v_transfer_outcomes
WHERE label_mature AND fraud_decision IS NOT NULL
GROUP BY 1 ORDER BY 1;

-- KPI 2: falsos positivos y precisión por versión de modelo
SELECT model_version,
       round(100.0 * count(*) FILTER (WHERE NOT is_fraud AND fraud_decision <> 'APPROVE')
             / NULLIF(count(*) FILTER (WHERE NOT is_fraud), 0), 3) AS fpr_pct,
       round(100.0 * count(*) FILTER (WHERE is_fraud AND fraud_decision <> 'APPROVE')
             / NULLIF(count(*) FILTER (WHERE fraud_decision <> 'APPROVE'), 0), 2) AS precision_pct,
       count(*) FILTER (WHERE NOT is_fraud AND fraud_decision = 'BLOCK') AS legitimas_bloqueadas
FROM analytics.v_transfer_outcomes
WHERE label_mature AND fraud_decision IS NOT NULL
GROUP BY 1;

-- KPI 3: latencia de alertas AML y falsos positivos por regla
SELECT rule_code, count(*) AS alertas,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY extract(epoch FROM created_at - trigger_emitted_at)) AS p50_s,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM created_at - trigger_emitted_at)) AS p95_s,
       round(100.0 * count(*) FILTER (WHERE status = 'CLOSED_FALSE_POSITIVE')
             / NULLIF(count(*) FILTER (WHERE status LIKE 'CLOSED%'), 0), 1) AS falsos_positivos_pct
FROM risk.aml_alerts
GROUP BY rule_code ORDER BY alertas DESC;

-- KPI 4: tasa de aprobación por segmento
SELECT segment, count(*) AS solicitudes,
       round(100.0 * count(*) FILTER (WHERE fraud_decision = 'APPROVE') / count(*), 2) AS aprobacion_automatica_pct,
       round(100.0 * count(*) FILTER (WHERE fraud_decision = 'REVIEW')  / count(*), 2) AS revision_pct,
       round(100.0 * count(*) FILTER (WHERE status = 'COMPLETED')       / count(*), 2) AS completadas_pct
FROM analytics.v_transfer_outcomes
WHERE fraud_decision IS NOT NULL
GROUP BY segment ORDER BY segment;

-- KPI 5: latencia de scoring y uso de fallback por hora
SELECT date_trunc('hour', created_at) AS hora, count(*) AS puntuadas,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY scoring_latency_ms) AS p95_ms,
       round(100.0 * count(*) FILTER (WHERE decision_source = 'FALLBACK') / count(*), 2) AS fallback_pct
FROM analytics.v_transfer_outcomes
WHERE scoring_latency_ms IS NOT NULL
GROUP BY 1 ORDER BY 1 DESC LIMIT 24;
