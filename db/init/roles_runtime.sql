-- Roles de ejecución de risk (ADR-09). Idempotente: se puede ejecutar cuantas veces haga falta,
-- como superusuario, con psql -v db=... -v risk_svc_pw=... -v sim_loader_pw=... -v ml_job_pw=...
--   verabank_risk  -> dueño del esquema risk y de sus migraciones (db/init/roles.sql)
--   risk_svc       -> lo que usa risk-service en ejecución: lee y escribe en risk, no es dueño de nada
--   sim_loader     -> calentamiento del replay (D1): solo INSERT en risk.scoring_log
--   ml_job         -> jobs de ML de la fase 5 (PSI, razones, reentrenamiento): lee scoring_log y fraud_labels,
--                     escribe drift_metrics y fraud_reasons
-- Los permisos sobre los objetos (db/init/grants.sql) se conceden después de las migraciones.

SELECT format('CREATE ROLE %I LOGIN', r)
FROM (VALUES ('risk_svc'), ('sim_loader'), ('ml_job')) AS v(r)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r)
\gexec

ALTER ROLE risk_svc   PASSWORD :'risk_svc_pw';
ALTER ROLE sim_loader PASSWORD :'sim_loader_pw';
ALTER ROLE ml_job     PASSWORD :'ml_job_pw';

GRANT CONNECT ON DATABASE :"db" TO risk_svc, sim_loader, ml_job;
