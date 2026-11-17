-- Permisos sobre objetos de risk y audit. Idempotente: se ejecuta como superusuario DESPUÉS de las
-- migraciones de core, audit y risk (make db-grants). Los roles los crea db/init/roles_runtime.sql.
--
-- Va aparte de las migraciones porque las de audit también las ejecuta transfer-service, y sus
-- pruebas corren en bases donde risk_svc no existe.

-- risk_svc: lectura y escritura en risk, sin ningún acceso a core ni a audit directo.
GRANT USAGE ON SCHEMA risk TO risk_svc;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA risk TO risk_svc;
-- Tablas que cree verabank_risk en migraciones futuras.
ALTER DEFAULT PRIVILEGES FOR ROLE verabank_risk IN SCHEMA risk
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO risk_svc;

-- Respaldo del lado del servidor del límite de 100 ms por consulta que impone el cliente (risk-scoring). Es por
-- sentencia, no por transacción, y también lo usa el rol stream: sus sentencias (upserts de una alerta, consultas
-- de ventana por índice, API de alertas) son de milisegundos.
ALTER ROLE risk_svc SET statement_timeout = '250ms';

-- Auditoría: risk_svc solo puede llamar a audit.append (SECURITY DEFINER); el USAGE del esquema
-- no da acceso a sus tablas.
GRANT USAGE ON SCHEMA audit TO risk_svc;
GRANT EXECUTE ON FUNCTION audit.append(TEXT, TEXT, TEXT, TEXT, UUID, JSONB) TO risk_svc;

-- sim_loader: solo INSERT en risk.scoring_log (sin SELECT, UPDATE ni otras tablas).
GRANT USAGE ON SCHEMA risk TO sim_loader;
GRANT INSERT ON risk.scoring_log TO sim_loader;

-- BI: solo lectura. analytics ya le llega por los privilegios por defecto de db/init/roles.sql.
GRANT USAGE ON SCHEMA risk TO verabank_bi;
GRANT SELECT ON ALL TABLES IN SCHEMA risk TO verabank_bi;
ALTER DEFAULT PRIVILEGES FOR ROLE verabank_risk IN SCHEMA risk GRANT SELECT ON TABLES TO verabank_bi;
