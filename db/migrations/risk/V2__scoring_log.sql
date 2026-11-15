CREATE TABLE risk.scoring_log (          -- bitácora de scoring y fuente de features online
  transfer_id   UUID PRIMARY KEY,
  source_ref    TEXT NOT NULL,
  dest_ref      TEXT NOT NULL,
  amount        NUMERIC(18,2) NOT NULL,
  occurred_at   TIMESTAMPTZ NOT NULL,
  features      JSONB NOT NULL,
  score         NUMERIC(6,5) NOT NULL,
  shadow_score  NUMERIC(6,5),
  decision      TEXT NOT NULL,
  model_version TEXT NOT NULL,
  scored_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_slog_source ON risk.scoring_log (source_ref, occurred_at DESC) INCLUDE (amount);
CREATE INDEX ix_slog_dest   ON risk.scoring_log (dest_ref, occurred_at DESC) INCLUDE (amount, source_ref);
