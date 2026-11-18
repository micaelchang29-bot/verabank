-- Verificador de la cadena de auditoría (T-16, HU-07). Solo lectura: no modifica tablas ni triggers de audit.
-- Recalcula row_hash con la MISMA fórmula de audit.chain_hash() (audit/V11), encadenando con el row_hash de la
-- fila anterior, y comprueba prev_hash y que audit_id no tenga huecos.
--
-- Va en las migraciones que ejecuta el rol de arranque (no en las de audit, que ejecuta transfer-service) para
-- que su dueño no sea un rol de servicio: el dueño de una función siempre puede ejecutarla. EXECUTE se revoca a
-- PUBLIC y no se concede a nadie; solo el rol administrador (superusuario) la ejecuta.
--
-- Orden de las comprobaciones en cada fila: GAP (audit_id no es el anterior + 1), PREV_MISMATCH (prev_hash no es
-- el row_hash anterior), HASH_MISMATCH (row_hash no es el recalculado). Devuelve la primera fila con problema;
-- en un GAP, first_bad_id es el primer audit_id que falta.
-- Límite: sin un ancla externa no detecta que se trunque el final de la cadena ni que se reescriba entera.
CREATE FUNCTION audit.verify_chain(p_from BIGINT DEFAULT 1)
RETURNS TABLE (checked BIGINT, first_bad_id BIGINT, problem TEXT)
LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp
AS $$
  WITH ordered AS (
    SELECT l.audit_id, l.prev_hash, l.row_hash,
           lag(l.audit_id) OVER w AS before_id,
           lag(l.row_hash) OVER w AS before_hash,
           sha256(coalesce(lag(l.row_hash) OVER w, ''::bytea) || convert_to(concat_ws('|',
               to_char(l.occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
               l.actor, l.action, l.entity_type, l.entity_id, l.detail::text), 'UTF8')) AS expected_hash
    FROM audit.audit_log l
    WHERE l.audit_id >= greatest(p_from, 1) - 1             -- la fila anterior ancla la primera revisada
    WINDOW w AS (ORDER BY l.audit_id)
  ), checks AS (
    SELECT audit_id,
           CASE
             WHEN audit_id <> coalesce(before_id, greatest(p_from, 1) - 1) + 1 THEN 'GAP'
             WHEN prev_hash IS DISTINCT FROM before_hash THEN 'PREV_MISMATCH'
             WHEN row_hash IS DISTINCT FROM expected_hash THEN 'HASH_MISMATCH'
           END AS problem,
           coalesce(before_id, greatest(p_from, 1) - 1) + 1 AS expected_id
    FROM ordered
    WHERE audit_id >= greatest(p_from, 1)
  ), first_bad AS (
    SELECT CASE problem WHEN 'GAP' THEN expected_id ELSE audit_id END AS first_bad_id, problem
    FROM checks WHERE problem IS NOT NULL
    ORDER BY audit_id LIMIT 1
  )
  SELECT (SELECT count(*) FROM checks), (SELECT first_bad_id FROM first_bad), (SELECT problem FROM first_bad)
$$;
REVOKE ALL ON FUNCTION audit.verify_chain(BIGINT) FROM PUBLIC;
