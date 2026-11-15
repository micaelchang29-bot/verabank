CREATE TABLE audit.audit_log (
  audit_id       BIGINT PRIMARY KEY,                   -- lo asigna el trigger, sin huecos
  occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor          TEXT NOT NULL,
  action         TEXT NOT NULL,
  entity_type    TEXT NOT NULL,
  entity_id      TEXT NOT NULL,
  correlation_id UUID,
  detail         JSONB NOT NULL,
  prev_hash      BYTEA,
  row_hash       BYTEA NOT NULL
);
CREATE INDEX ix_audit_entity ON audit.audit_log (entity_type, entity_id, audit_id);
