CREATE TABLE risk.aml_rules (            -- parámetros versionados de las reglas AML
  rule_code  TEXT NOT NULL,
  version    INT NOT NULL CHECK (version > 0),
  params     JSONB NOT NULL CHECK (jsonb_typeof(params) = 'object'),
  active     BOOLEAN NOT NULL,
  valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (rule_code, version)
);
-- Una sola versión activa por regla.
CREATE UNIQUE INDEX ux_aml_rules_active ON risk.aml_rules (rule_code) WHERE active;

-- Un cambio de parámetros es una versión nueva, nunca un UPDATE: solo `active` puede cambiar
-- (para desactivar la versión anterior al activar la nueva).
CREATE FUNCTION risk.aml_rules_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.rule_code IS DISTINCT FROM OLD.rule_code OR NEW.version IS DISTINCT FROM OLD.version
     OR NEW.params IS DISTINCT FROM OLD.params OR NEW.valid_from IS DISTINCT FROM OLD.valid_from THEN
    RAISE EXCEPTION 'aml_rules: los parámetros no se modifican; inserta una versión nueva';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_aml_rules_guard BEFORE UPDATE ON risk.aml_rules
  FOR EACH ROW EXECUTE FUNCTION risk.aml_rules_guard();
