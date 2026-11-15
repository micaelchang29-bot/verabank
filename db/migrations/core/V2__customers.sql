CREATE TABLE core.customers (            -- sintético, sin datos personales
  customer_id UUID PRIMARY KEY,
  segment     TEXT NOT NULL CHECK (segment IN ('RETAIL','PREMIUM','PYME')),
  country     CHAR(2) NOT NULL DEFAULT 'PE',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
