CREATE TABLE risk.country_risk (         -- listas de países de riesgo (GAFI/FATF) con su vigencia
  country    CHAR(2) NOT NULL CHECK (country ~ '^[A-Z]{2}$'),   -- ISO 3166-1 alfa-2
  level      TEXT NOT NULL CHECK (level IN ('HIGH','WATCH')),
  source     TEXT NOT NULL,                                     -- URL de la lista
  list_date  DATE NOT NULL,                                     -- fecha de publicación de la lista
  valid_from DATE NOT NULL,
  valid_to   DATE,                                              -- NULL = vigente
  PRIMARY KEY (country, valid_from),
  CONSTRAINT ck_country_risk_period CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE INDEX ix_country_risk_current ON risk.country_risk (country) WHERE valid_to IS NULL;
