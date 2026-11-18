-- Segunda referencia del PSI (ADR-13, D1): 'train' (§5f, por defecto) o 'valid' (pasos 425–550). Las filas existentes
-- son de la referencia de entrenamiento. La alerta de dos ventanas contiguas se evalúa dentro de cada referencia.
ALTER TABLE analytics.drift_metrics
  ADD COLUMN reference TEXT NOT NULL DEFAULT 'train',
  ADD CONSTRAINT ck_drift_reference CHECK (reference IN ('train', 'valid'));
ALTER TABLE analytics.drift_metrics DROP CONSTRAINT drift_metrics_pkey;
ALTER TABLE analytics.drift_metrics ADD PRIMARY KEY (window_start, feature, reference);
