CREATE TABLE risk.aml_exempt_accounts (  -- cuentas que no generan alertas AML (p. ej. CLEARING)
  account_id UUID PRIMARY KEY,
  reason     TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
