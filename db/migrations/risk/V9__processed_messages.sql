CREATE TABLE risk.processed_messages (   -- inbox de risk; igual que core.processed_messages
  consumer     TEXT NOT NULL,
  event_id     UUID NOT NULL,
  processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, event_id)
);
