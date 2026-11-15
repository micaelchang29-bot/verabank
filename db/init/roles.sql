-- Roles y privilegios de base de datos (ADR-09). Lo ejecuta db/init/initdb.d/10-roles.sh
-- una sola vez, al crear el volumen, con psql -v db=... -v admin=... -v *_pw=...
--
-- Propiedad de los esquemas: cada migración empieza con CREATE SCHEMA, de modo que el
-- propietario es el rol que la ejecuta (de ahí el CREATE sobre la base):
--   verabank_transfer -> core, audit   (transfer-service)
--   verabank_risk     -> risk          (risk-service)
--   analytics         -> lo crea el job de la fase 5 con el rol de arranque (:admin);
--                        verabank_bi lo lee y no puede escribir.
-- Ningún rol recibe privilegios sobre el esquema de otro.

CREATE ROLE verabank_transfer LOGIN PASSWORD :'transfer_pw';
CREATE ROLE verabank_risk     LOGIN PASSWORD :'risk_pw';
CREATE ROLE verabank_bi       LOGIN PASSWORD :'bi_pw';

REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO verabank_transfer, verabank_risk, verabank_bi;
GRANT CREATE  ON DATABASE :"db" TO verabank_transfer, verabank_risk;

-- Nadie crea objetos en public.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- BI: solo lectura sobre lo que cree el rol de arranque (el esquema analytics aún no
-- existe, así que los privilegios por defecto no pueden limitarse con IN SCHEMA).
-- Supuesto: el rol de arranque solo crea analytics; los demás esquemas los crean
-- verabank_transfer y verabank_risk.
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin" GRANT USAGE  ON SCHEMAS TO verabank_bi;
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin" GRANT SELECT ON TABLES  TO verabank_bi;
