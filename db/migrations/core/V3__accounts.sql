CREATE TABLE core.accounts (
  account_id   UUID PRIMARY KEY,
  customer_id  UUID REFERENCES core.customers,        -- NULL en la cuenta de compensación
  account_type TEXT NOT NULL DEFAULT 'CUSTOMER' CHECK (account_type IN ('CUSTOMER','CLEARING')),
  currency     CHAR(3) NOT NULL DEFAULT 'PEN',
  status       TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','BLOCKED','CLOSED')),
  balance      NUMERIC(18,2) NOT NULL DEFAULT 0,
  held         NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK (held >= 0),
  version      BIGINT NOT NULL DEFAULT 0,
  opened_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_no_overdraft CHECK (account_type = 'CLEARING' OR balance - held >= 0)
);
CREATE INDEX ix_accounts_customer ON core.accounts (customer_id);
