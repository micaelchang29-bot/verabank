"""PostgreSQL desechable para las pruebas de los jobs de la fase 5 (PSI, razones, reentrenamiento). Necesita Docker.

Aplica las migraciones reales que los jobs leen o escriben (risk V1–V3: esquema, scoring_log, fraud_labels; analytics
V1, V3 y V4) y crea el rol ``ml_job`` con los permisos de ``db/init/grants.sql``. Un esquema ``core`` con una tabla
permite comprobar que ``ml_job`` no lo lee. Si Docker no está, la prueba falla (no se omite).
"""

from __future__ import annotations

import re
import subprocess
import time
import uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "postgres:16.16"
ADMIN_PW = "admin-ml-test"
ML_JOB_PW = "ml-job-test"
MIGRATIONS = ["risk/V1__create_schema_risk.sql", "risk/V2__scoring_log.sql", "risk/V3__fraud_labels.sql",
              "analytics/V1__create_schema_analytics.sql", "analytics/V3__drift_metrics.sql",
              "analytics/V4__fraud_reasons.sql"]


def ml_job_grants() -> str:
    """El bloque de ml_job de db/init/grants.sql (entre sus marcas >>> y <<<), tal cual."""
    text = (ROOT / "db/init/grants.sql").read_text()
    return re.search(r"^-- >>> ml_job.*?^-- <<< ml_job", text, re.S | re.M).group(0)


class Postgres:
    def __init__(self) -> None:
        self.name = f"verabank-mltest-{uuid.uuid4().hex[:8]}"
        subprocess.run(["docker", "run", "-d", "--name", self.name, "-p", "127.0.0.1::5432",
                        "-e", "POSTGRES_DB=verabank", "-e", "POSTGRES_USER=admin", "-e", f"POSTGRES_PASSWORD={ADMIN_PW}",
                        IMAGE], check=True, capture_output=True)
        port = subprocess.run(["docker", "port", self.name, "5432/tcp"], check=True, capture_output=True,
                              text=True).stdout.split()[-1]
        self.port = int(port.rsplit(":", 1)[1])
        # La imagen arranca un servidor temporal para initdb y lo reinicia: se exigen tres conexiones seguidas.
        deadline, ok = time.monotonic() + 90, 0
        while ok < 3:
            try:
                with self.connect("admin") as conn:
                    conn.execute("SELECT 1")
                ok += 1
            except psycopg.OperationalError:
                ok = 0
                if time.monotonic() > deadline:
                    raise
            time.sleep(0.5)
        with self.connect("admin") as conn:
            for m in MIGRATIONS:
                conn.execute((ROOT / "db/migrations" / m).read_text())
            conn.execute(f"CREATE ROLE ml_job LOGIN PASSWORD '{ML_JOB_PW}'")
            conn.execute(ml_job_grants())
            conn.execute("CREATE SCHEMA core; CREATE TABLE core.accounts (account_id uuid)")

    def dsn(self, role: str = "ml_job") -> str:
        pw = ADMIN_PW if role == "admin" else ML_JOB_PW
        return f"host=127.0.0.1 port={self.port} dbname=verabank user={role} password={pw} connect_timeout=5"

    def connect(self, role: str = "ml_job") -> psycopg.Connection:
        return psycopg.connect(self.dsn(role), autocommit=True)

    def reset(self) -> None:
        with self.connect("admin") as conn:
            conn.execute("TRUNCATE risk.scoring_log, risk.fraud_labels, analytics.drift_metrics, analytics.fraud_reasons")

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", "-v", self.name], check=False, capture_output=True)
