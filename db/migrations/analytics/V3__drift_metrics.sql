-- PSI por ventana de 24 pasos y por feature (más una fila para `score`), escrito por el job de
-- monitoreo (ml_job). Sin DDL en §4: columnas de la fase 5 (docs/adr/ADR-13.md, D1 y D2).
CREATE TABLE analytics.drift_metrics (
  window_start   TIMESTAMPTZ NOT NULL,
  window_end     TIMESTAMPTZ NOT NULL,
  feature        TEXT NOT NULL,                        -- nombre de la feature o 'score'
  psi            NUMERIC(10,6) NOT NULL,
  n_current      INT NOT NULL,
  n_bins         SMALLINT NOT NULL,
  degenerate     BOOLEAN NOT NULL,
  insufficient   BOOLEAN NOT NULL,
  is_top_feature BOOLEAN NOT NULL,
  alert          BOOLEAN NOT NULL DEFAULT false,
  model_version  TEXT NOT NULL,
  computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (window_start, feature),
  CONSTRAINT ck_drift_window CHECK (window_end > window_start)
);
CREATE INDEX ix_drift_alert ON analytics.drift_metrics (window_start DESC) WHERE alert;
