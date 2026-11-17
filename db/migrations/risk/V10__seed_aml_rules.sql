-- Versión 1 de las reglas AML (docs/diseno.md §5a; valores iniciales supuestos, manual 0.1).
-- Sin FAN_IN: queda desactivada hasta que se decida (0.1). Un cambio de parámetros es una versión
-- nueva (INSERT con version + 1 y la anterior en active = false), nunca un UPDATE.
INSERT INTO risk.aml_rules (rule_code, version, params, active, valid_from) VALUES
  ('THRESHOLD', 1, '{"threshold": "10000.00"}', true, timestamptz '2026-01-01 00:00:00+00'),
  -- El ratio máximo es exclusivo: min_ratio <= monto / threshold < max_ratio.
  ('STRUCTURING', 1, '{"threshold": "10000.00", "min_ratio": 0.70, "max_ratio": 1.00, "min_count": 3, "window_hours": 24, "min_total": "10000.00"}', true, timestamptz '2026-01-01 00:00:00+00'),
  ('COUNTRY_RISK', 1, '{"high_min_amount": "1000.00", "watch_min_amount": "5000.00"}', true, timestamptz '2026-01-01 00:00:00+00');
