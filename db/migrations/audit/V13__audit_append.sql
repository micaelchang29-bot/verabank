-- Único punto de escritura en audit.audit_log para otros servicios (risk). Corre con los
-- privilegios de su dueño (verabank_transfer), así que el invocador no necesita ningún privilegio
-- sobre las tablas de audit: el trigger de cadena (V11) asigna audit_id, prev_hash y row_hash.
-- El GRANT EXECUTE a risk_svc no va aquí: esta migración también corre donde ese rol no existe
-- (pruebas de transfer-service); lo concede db/init/grants.sql, después de todas las migraciones.
CREATE FUNCTION audit.append(
  p_actor          TEXT,
  p_action         TEXT,
  p_entity_type    TEXT,
  p_entity_id      TEXT,
  p_correlation_id UUID,
  p_detail         JSONB
) RETURNS BIGINT
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
  INSERT INTO audit.audit_log (actor, action, entity_type, entity_id, correlation_id, detail)
  VALUES (p_actor, p_action, p_entity_type, p_entity_id, p_correlation_id, p_detail)
  RETURNING audit_id
$$;
REVOKE ALL ON FUNCTION audit.append(TEXT, TEXT, TEXT, TEXT, UUID, JSONB) FROM PUBLIC;
