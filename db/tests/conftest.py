"""PostgreSQL desechable para las pruebas de db/. Necesita Docker; si no está, las pruebas fallan (no se omiten).

Cada base se levanta con los mismos scripts de roles que Compose (db/init) y se prepara con lo mismo que hace
`make up-core`: Flyway de core y audit con verabank_transfer (como transfer-service), roles de ejecución,
Flyway de risk con verabank_risk y permisos. Los scripts de scripts/ se ejecutan tal cual, contra el contenedor
de la prueba (VERABANK_DB_CONTAINER).
"""

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
POSTGRES_IMAGE = "postgres:16.16"
FLYWAY_IMAGE = "flyway/flyway:11.7.2"  # la misma versión que transfer-service
DB = "verabank"
ADMIN = "verabank_admin"
PASSWORDS = {
    ADMIN: "admin-test-pw",
    "verabank_transfer": "transfer-test-pw",
    "verabank_risk": "risk-test-pw",
    "verabank_bi": "bi-test-pw",
    "risk_svc": "risk-svc-test-pw",
    "sim_loader": "sim-loader-test-pw",
}


def docker(*args, check=True, env=None, input=None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, env=env, input=input
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])} falló:\n{result.stdout}\n{result.stderr}")
    return result


class Database:
    """Un contenedor PostgreSQL desechable en su propia red (el host se llama `postgres`)."""

    def __init__(self, tmp: Path, runtime_roles_at_init: bool):
        self.suffix = uuid.uuid4().hex[:8]
        self.name = f"verabank-dbtest-{self.suffix}"
        self.network = f"verabank-dbtest-net-{self.suffix}"
        initdb = tmp / f"initdb-{self.suffix}"
        initdb.mkdir()
        scripts = ["10-roles.sh"] + (["20-roles-runtime.sh"] if runtime_roles_at_init else [])
        for script in scripts:
            shutil.copy(ROOT / "db/init/initdb.d" / script, initdb / script)
            (initdb / script).chmod(0o755)
        docker("network", "create", self.network)
        docker(
            "run", "-d", "--name", self.name, "--network", self.network,
            "--network-alias", "postgres", "-p", "127.0.0.1::5432",
            "-e", f"POSTGRES_DB={DB}", "-e", f"POSTGRES_USER={ADMIN}",
            "-e", f"POSTGRES_PASSWORD={PASSWORDS[ADMIN]}",
            "-e", f"VERABANK_TRANSFER_PASSWORD={PASSWORDS['verabank_transfer']}",
            "-e", f"VERABANK_RISK_PASSWORD={PASSWORDS['verabank_risk']}",
            "-e", f"VERABANK_BI_PASSWORD={PASSWORDS['verabank_bi']}",
            "-e", f"RISK_SVC_PASSWORD={PASSWORDS['risk_svc']}",
            "-e", f"SIM_LOADER_PASSWORD={PASSWORDS['sim_loader']}",
            "-v", f"{initdb}:/docker-entrypoint-initdb.d:ro",
            "-v", f"{ROOT / 'db/init/roles.sql'}:/sql/roles.sql:ro",
            "-v", f"{ROOT / 'db/init/roles_runtime.sql'}:/sql/roles_runtime.sql:ro",
            POSTGRES_IMAGE,
        )  # fmt: skip
        mapping = docker("port", self.name, "5432/tcp").stdout.split()[-1]
        self.port = int(mapping.rsplit(":", 1)[1])
        self._wait_ready()

    def _wait_ready(self):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                with self.connect(ADMIN) as conn:
                    conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'verabank_bi'").fetchone()
                return
            except psycopg.OperationalError:
                time.sleep(0.5)
        raise RuntimeError(f"PostgreSQL no arrancó:\n{docker('logs', self.name).stdout[-2000:]}")

    def connect(self, role: str) -> psycopg.Connection:
        return psycopg.connect(
            host="127.0.0.1", port=self.port, dbname=DB, user=role, password=PASSWORDS[role],
            autocommit=True, connect_timeout=5,
        )  # fmt: skip

    # --- lo que hace make up-core, paso a paso -------------------------------------------------

    def script_env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")}
        env.update(
            POSTGRES_DB=DB, POSTGRES_USER=ADMIN,
            RISK_SVC_PASSWORD=PASSWORDS["risk_svc"], SIM_LOADER_PASSWORD=PASSWORDS["sim_loader"],
            VERABANK_RISK_PASSWORD=PASSWORDS["verabank_risk"],
            VERABANK_DB_CONTAINER=self.name, VERABANK_DB_NETWORK=self.network, VERABANK_DB_HOST="postgres",
            FLYWAY_IMAGE=FLYWAY_IMAGE,
        )  # fmt: skip
        return env

    def script(self, name: str, *args: str, check: bool = True, **env) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [str(ROOT / "scripts" / name), *args], env={**self.script_env(), **env},
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        if check and result.returncode != 0:
            raise RuntimeError(f"{name} falló ({result.returncode}):\n{result.stdout}\n{result.stderr}")
        return result

    def flyway_transfer(self) -> subprocess.CompletedProcess:
        """Las migraciones de core y audit, como las ejecuta transfer-service (db/README.md)."""
        env = {**os.environ, "FLYWAY_PASSWORD": PASSWORDS["verabank_transfer"]}
        return docker(
            "run", "--rm", "--network", self.network, "-e", "FLYWAY_PASSWORD",
            "-v", f"{ROOT / 'db/migrations/core'}:/flyway/sql/core:ro",
            "-v", f"{ROOT / 'db/migrations/audit'}:/flyway/sql/audit:ro",
            FLYWAY_IMAGE, f"-url=jdbc:postgresql://postgres:5432/{DB}", "-user=verabank_transfer",
            "-schemas=flyway_transfer", "-defaultSchema=flyway_transfer",
            "-locations=filesystem:/flyway/sql/core,filesystem:/flyway/sql/audit",
            "-connectRetries=10", "migrate", env=env,
        )  # fmt: skip

    def create_analytics(self):
        """analytics lo crea el rol de arranque (db/README.md)."""
        sql = (ROOT / "db/migrations/analytics/V1__create_schema_analytics.sql").read_text()
        with self.connect(ADMIN) as conn:
            conn.execute(sql)
            conn.execute("CREATE TABLE analytics.probe (n INT)")
            conn.execute("INSERT INTO analytics.probe VALUES (1)")

    def provision(self, with_roles_script: bool = True):
        self.flyway_transfer()
        self.create_analytics()
        if with_roles_script:
            self.script("db-roles.sh")
        self.script("db-migrate-risk.sh")
        self.script("db-grants.sh")

    def teardown(self):
        docker("rm", "-f", "-v", self.name, check=False)
        docker("network", "rm", self.network, check=False)


def make_database(tmp_path_factory, *, runtime_roles_at_init: bool) -> Database:
    return Database(tmp_path_factory.mktemp("db"), runtime_roles_at_init)


@pytest.fixture(scope="session")
def stage1_db(tmp_path_factory):
    """Un volumen existente: solo los roles de la fase 1 y solo core y audit migradas (sin risk_svc)."""
    db = make_database(tmp_path_factory, runtime_roles_at_init=False)
    db.flyway_transfer()
    yield db
    db.teardown()


@pytest.fixture(scope="session")
def provisioned_db(tmp_path_factory):
    """Un volumen existente al que make up-core añade roles, migraciones de risk y permisos."""
    db = make_database(tmp_path_factory, runtime_roles_at_init=False)
    db.provision()
    yield db
    db.teardown()


@pytest.fixture(scope="session")
def fresh_db(tmp_path_factory):
    """Un volumen nuevo: los roles de ejecución ya existen al inicializar (initdb.d/20-roles-runtime.sh)."""
    db = make_database(tmp_path_factory, runtime_roles_at_init=True)
    db.provision(with_roles_script=False)
    yield db
    db.teardown()


def risk_signature(conn: psycopg.Connection) -> dict:
    """Huella del esquema risk (columnas, índices, restricciones, disparadores y permisos)."""
    q = lambda sql: sorted(conn.execute(sql).fetchall())  # noqa: E731
    return {
        "columns": q("""SELECT table_name, column_name, data_type, is_nullable, column_default
                        FROM information_schema.columns WHERE table_schema = 'risk'"""),
        "indexes": q("SELECT indexdef FROM pg_indexes WHERE schemaname = 'risk'"),
        "constraints": q("""SELECT conrelid::regclass::text, pg_get_constraintdef(oid)
                            FROM pg_constraint WHERE connamespace = 'risk'::regnamespace"""),
        "triggers": q("""SELECT tgrelid::regclass::text, tgname FROM pg_trigger
                         WHERE NOT tgisinternal AND tgrelid::regclass::text LIKE 'risk.%'"""),
        "acl": q("""SELECT relname, relacl::text FROM pg_class
                    WHERE relnamespace = 'risk'::regnamespace AND relkind = 'r'"""),
        "default_acl": q("SELECT defaclrole::regrole::text, defaclobjtype, defaclacl::text FROM pg_default_acl"),
    }  # fmt: skip
