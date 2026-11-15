CREATE TABLE risk.fraud_labels (
  transfer_id  UUID PRIMARY KEY,
  is_fraud     BOOLEAN NOT NULL,
  label_source TEXT NOT NULL,
  labeled_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
