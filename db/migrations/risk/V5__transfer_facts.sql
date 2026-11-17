CREATE TABLE risk.transfer_facts (       -- proyección de TransferCompleted para las ventanas AML
  transfer_id       UUID PRIMARY KEY,
  source_account_id UUID NOT NULL,
  dest_ref          TEXT NOT NULL,                     -- cuenta interna o referencia externa
  dest_country      CHAR(2) NOT NULL,
  transfer_type     TEXT NOT NULL,
  amount            NUMERIC(18,2) NOT NULL,
  occurred_at       TIMESTAMPTZ NOT NULL,              -- tiempo de negocio
  emitted_at        TIMESTAMPTZ NOT NULL,              -- emitted_at del evento
  completed_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX ix_facts_source ON risk.transfer_facts (source_account_id, occurred_at DESC) INCLUDE (amount);
CREATE INDEX ix_facts_dest   ON risk.transfer_facts (dest_ref, occurred_at DESC) INCLUDE (amount, source_account_id);  -- FAN_IN
