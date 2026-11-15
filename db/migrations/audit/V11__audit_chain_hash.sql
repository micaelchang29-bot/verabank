CREATE FUNCTION audit.chain_hash() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE last_id BIGINT; last_hash BYTEA;
BEGIN
  PERFORM pg_advisory_xact_lock(815001);               -- serializa la cadena
  SELECT audit_id, row_hash INTO last_id, last_hash
    FROM audit.audit_log ORDER BY audit_id DESC LIMIT 1;
  NEW.audit_id  := coalesce(last_id, 0) + 1;
  NEW.prev_hash := last_hash;
  NEW.row_hash  := sha256(coalesce(last_hash, ''::bytea) || convert_to(concat_ws('|',
      to_char(NEW.occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
      NEW.actor, NEW.action, NEW.entity_type, NEW.entity_id, NEW.detail::text), 'UTF8'));
  RETURN NEW;
END $$;
CREATE TRIGGER trg_audit_chain BEFORE INSERT ON audit.audit_log
  FOR EACH ROW EXECUTE FUNCTION audit.chain_hash();
