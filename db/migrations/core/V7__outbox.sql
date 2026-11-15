CREATE TABLE core.outbox (
  outbox_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id       UUID NOT NULL UNIQUE,
  aggregate_type TEXT NOT NULL,
  aggregate_id   UUID NOT NULL,
  topic          TEXT NOT NULL,
  message_key    TEXT NOT NULL,
  event_type     TEXT NOT NULL,
  payload        JSONB NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at   TIMESTAMPTZ
);
CREATE INDEX ix_outbox_pending ON core.outbox (outbox_id) WHERE published_at IS NULL;
