-- Conciliación (docs/diseno.md §7). Ambas consultas deben devolver 0.
SELECT count(*) AS cuentas_descuadradas FROM core.accounts a
WHERE a.balance <> COALESCE((SELECT sum(CASE direction WHEN 'C' THEN amount ELSE -amount END)
                             FROM core.transactions x WHERE x.account_id = a.account_id), 0);
SELECT count(*) AS retenciones_huerfanas FROM core.accounts a
WHERE a.held <> 0 AND NOT EXISTS (
  SELECT 1 FROM core.transfers t
  WHERE t.source_account_id = a.account_id AND t.status IN ('PENDING','IN_REVIEW','PROCESSING'));
