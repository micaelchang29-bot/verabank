CREATE TABLE core.saga_state (
  saga_id      UUID PRIMARY KEY REFERENCES core.transfers,   -- igual a transfer_id
  current_step TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('RUNNING','WAITING_REPLY','IN_REVIEW',
                 'COMPLETED','COMPENSATING','COMPENSATED','COMPENSATION_FAILED')),
  attempt      INT NOT NULL DEFAULT 0,
  wake_at      TIMESTAMPTZ,              -- próximo reintento o vencimiento de espera
  last_error   TEXT,
  payload      JSONB NOT NULL DEFAULT '{}',
  version      BIGINT NOT NULL DEFAULT 0,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_saga_wake ON core.saga_state (wake_at)
  WHERE status IN ('RUNNING','WAITING_REPLY','COMPENSATING');
