-- make reconcile-full (T-17): las dos consultas de §7 más tres comprobaciones; todas deben dar 0.
-- Una fila por comprobación: nombre|valor. La (c) compara con k6 y la calcula scripts/reconcile-full.sh.
SELECT 'cuentas_descuadradas', count(*) FROM core.accounts a
WHERE a.balance <> COALESCE((SELECT sum(CASE direction WHEN 'C' THEN amount ELSE -amount END)
                             FROM core.transactions x WHERE x.account_id = a.account_id), 0);
SELECT 'retenciones_huerfanas', count(*) FROM core.accounts a
WHERE a.held <> 0 AND NOT EXISTS (
  SELECT 1 FROM core.transfers t
  WHERE t.source_account_id = a.account_id AND t.status IN ('PENDING','IN_REVIEW','PROCESSING'));
-- (a) asientos por transferencia con Σ D <> Σ C
SELECT 'transferencias_con_debe_distinto_de_haber', count(*) FROM (
  SELECT transfer_id FROM core.transactions GROUP BY transfer_id
  HAVING sum(amount) FILTER (WHERE direction = 'D') IS DISTINCT FROM sum(amount) FILTER (WHERE direction = 'C')) d;
-- (b) transferencias abiertas tras el settle
SELECT 'transferencias_abiertas', count(*) FROM core.transfers WHERE status IN ('PENDING','PROCESSING','IN_REVIEW');
-- (c) filas del canal loadtest que crearon una transferencia (para comparar con k6)
SELECT 'filas_loadtest', count(*) FROM core.transfers WHERE client_id = 'loadtest';
-- informativo: compensación de las FAILED (completed_at − created_at), en segundos
SELECT 'failed_compensacion_p50_p95_p99_s', coalesce(
  round(percentile_cont(0.50) WITHIN GROUP (ORDER BY extract(epoch FROM completed_at - created_at))::numeric, 3) || ' / ' ||
  round(percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM completed_at - created_at))::numeric, 3) || ' / ' ||
  round(percentile_cont(0.99) WITHIN GROUP (ORDER BY extract(epoch FROM completed_at - created_at))::numeric, 3) ||
  ' (n=' || count(*) || ')', 'sin FAILED')
FROM core.transfers WHERE status = 'FAILED' AND completed_at IS NOT NULL AND client_id = 'loadtest';
