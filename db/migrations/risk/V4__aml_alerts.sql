CREATE TABLE risk.aml_alerts (
  alert_id            UUID PRIMARY KEY,
  dedupe_key          TEXT NOT NULL UNIQUE,            -- regla | cuenta | inicio de ventana
  rule_code           TEXT NOT NULL,
  rule_version        INT NOT NULL,
  rule_params         JSONB NOT NULL,                  -- parámetros vigentes al disparar
  subject_account_id  UUID NOT NULL,
  severity            TEXT NOT NULL CHECK (severity IN ('LOW','MEDIUM','HIGH')),
  status              TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN
                        ('OPEN','IN_REVIEW','ESCALATED','CLOSED_FALSE_POSITIVE','CLOSED_CONFIRMED')),
  window_start        TIMESTAMPTZ NOT NULL,
  window_end          TIMESTAMPTZ NOT NULL,
  total_amount        NUMERIC(18,2) NOT NULL,
  transfer_ids        UUID[] NOT NULL,
  trigger_emitted_at  TIMESTAMPTZ NOT NULL,            -- emitted_at del evento disparador
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at           TIMESTAMPTZ,
  closed_by           TEXT
);
CREATE INDEX ix_alerts_queue    ON risk.aml_alerts (status, severity, created_at);
CREATE INDEX ix_alerts_subject  ON risk.aml_alerts (subject_account_id, created_at DESC);
CREATE INDEX ix_alerts_transfer ON risk.aml_alerts USING gin (transfer_ids);
