CREATE TABLE core.transactions (         -- asientos del libro mayor (doble partida)
  entry_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  transfer_id   UUID NOT NULL REFERENCES core.transfers,
  account_id    UUID NOT NULL REFERENCES core.accounts,
  direction     CHAR(1) NOT NULL CHECK (direction IN ('D','C')),
  entry_kind    TEXT NOT NULL CHECK (entry_kind IN ('TRANSFER','REVERSAL')),
  amount        NUMERIC(18,2) NOT NULL CHECK (amount > 0),
  balance_after NUMERIC(18,2) NOT NULL,
  posted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_entry UNIQUE (transfer_id, account_id, direction, entry_kind)
);
CREATE INDEX ix_tx_account_time ON core.transactions (account_id, posted_at DESC);
