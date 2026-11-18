-- Tres razones por caso REVIEW o BLOCK del modelo (§5e), escritas por el job por lotes (ml_job).
CREATE TABLE analytics.fraud_reasons (
  transfer_id   UUID NOT NULL,
  rank          SMALLINT NOT NULL CHECK (rank BETWEEN 1 AND 3),
  feature       TEXT NOT NULL,
  contribution  DOUBLE PRECISION NOT NULL,             -- con signo: > 0 empuja hacia fraude
  model_version TEXT NOT NULL,
  computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (transfer_id, rank)
);
