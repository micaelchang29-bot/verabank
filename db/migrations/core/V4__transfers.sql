CREATE TABLE core.transfers (
  transfer_id        UUID PRIMARY KEY,
  client_id          TEXT NOT NULL,
  idempotency_key    UUID NOT NULL,
  request_hash       CHAR(64) NOT NULL,                -- SHA-256 del cuerpo canónico
  source_account_id  UUID NOT NULL REFERENCES core.accounts,
  dest_account_id    UUID REFERENCES core.accounts,
  dest_external_ref  TEXT,
  dest_country       CHAR(2) NOT NULL,
  rail               TEXT NOT NULL CHECK (rail IN ('INTERNAL','EXTERNAL')),
  transfer_type      TEXT NOT NULL CHECK (transfer_type IN ('TRANSFER','CASH_OUT','PAYMENT','DEBIT')),
  amount             NUMERIC(18,2) NOT NULL CHECK (amount > 0),
  currency           CHAR(3) NOT NULL,
  status             TEXT NOT NULL CHECK (status IN
                       ('PENDING','IN_REVIEW','PROCESSING','COMPLETED','REJECTED','FAILED')),
  fraud_score        NUMERIC(6,5),
  fraud_decision     TEXT CHECK (fraud_decision IN ('APPROVE','REVIEW','BLOCK')),
  decision_source    TEXT CHECK (decision_source IN ('MODEL','FALLBACK')),
  model_version      TEXT,
  scoring_latency_ms INT,
  failure_reason     TEXT,
  occurred_at        TIMESTAMPTZ NOT NULL,             -- tiempo de negocio
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at       TIMESTAMPTZ,
  CONSTRAINT uq_transfer_idem UNIQUE (client_id, idempotency_key),
  CONSTRAINT ck_dest CHECK ((rail = 'INTERNAL' AND dest_account_id IS NOT NULL)
                         OR (rail = 'EXTERNAL' AND dest_external_ref IS NOT NULL)),
  CONSTRAINT ck_distinct CHECK (dest_account_id IS DISTINCT FROM source_account_id)
);
CREATE INDEX ix_transfers_source_time ON core.transfers (source_account_id, occurred_at DESC);
CREATE INDEX ix_transfers_dest_time   ON core.transfers (dest_account_id, occurred_at DESC)
  WHERE dest_account_id IS NOT NULL;
CREATE INDEX ix_transfers_open ON core.transfers (status, created_at)
  WHERE status IN ('PENDING','IN_REVIEW','PROCESSING');
CREATE INDEX ix_transfers_time_brin ON core.transfers USING brin (occurred_at);
