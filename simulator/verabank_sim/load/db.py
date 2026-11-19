"""Acceso a PostgreSQL por ``psql`` dentro del contenedor, como ``scripts/db-lib.sh`` (sin dependencias nuevas).

La contraseña viaja por entorno (``-e PGPW`` sin valor), nunca por la línea de comandos. Con
``VERABANK_DB_CONTAINER=<nombre>`` usa ``docker exec`` sobre ese contenedor; con ``VERABANK_KIND=1``, ``kubectl exec`` sobre
el StatefulSet ``postgres`` del clúster kind, cuyo pod ya tiene las contraseñas en su entorno (la contraseña no viaja);
si no, el servicio ``postgres`` de Compose.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = ('u=$1; d=$2; shift 2; PGPASSWORD="$PGPW" exec psql -X -q -h 127.0.0.1 -U "$u" -d "$d" '
           '-v ON_ERROR_STOP=1 "$@"')
# kind: la contraseña se lee de la variable del mismo nombre en el entorno del pod (Secret verabank-env).
_KUBE_SCRIPT = ('u=$1; d=$2; v=$3; shift 3; eval "PGPASSWORD=\\"\\${$v}\\""; export PGPASSWORD; '
                'exec psql -X -q -h 127.0.0.1 -U "$u" -d "$d" -v ON_ERROR_STOP=1 "$@"')


class PsqlError(RuntimeError):
    pass


@dataclass(frozen=True)
class DockerPsql:
    role: str
    password_var: str
    database: str = ""
    container: str = ""

    def _database(self) -> str:
        return self.database or os.environ.get("POSTGRES_DB", "verabank")

    def command(self, *psql_args: str) -> list[str]:
        if os.environ.get("VERABANK_KIND") == "1":
            context = ["--context", f"kind-{os.environ.get('KIND_CLUSTER', 'verabank')}"]
            return ["kubectl", *context, "exec", "-i", "statefulset/postgres", "--", "sh", "-c", _KUBE_SCRIPT, "sh",
                    self.role, self._database(), self.password_var, *psql_args]
        if self.container or os.environ.get("VERABANK_DB_CONTAINER"):
            target = ["docker", "exec", "-i", "-e", "PGPW", self.container or os.environ["VERABANK_DB_CONTAINER"]]
        else:
            target = ["docker", "compose", "--project-directory", str(REPO_ROOT), "--profile", "core",
                      "exec", "-T", "-e", "PGPW", "postgres"]
        return [*target, "sh", "-c", _SCRIPT, "sh", self.role, self._database(), *psql_args]

    def run(self, stdin: str, *psql_args: str, timeout_s: float = 900.0) -> str:
        """Ejecuta psql con ``stdin`` (SQL o datos de COPY) y devuelve su salida estándar."""
        password = os.environ.get(self.password_var, "")
        if not password:
            raise SystemExit(f"falta {self.password_var} en el entorno (.env)")
        done = subprocess.run(self.command(*psql_args), input=stdin, capture_output=True, text=True, check=False,
                              timeout=timeout_s, env={**os.environ, "PGPW": password})
        if done.returncode != 0:
            raise PsqlError(done.stderr.strip()[:600] or f"psql terminó con {done.returncode}")
        return done.stdout

    def query(self, sql: str) -> list[list[str]]:
        """Filas de una consulta, columnas separadas por ``|`` (sin cabecera)."""
        out = self.run(sql, "-At", "-F", "|", "-f", "-")
        return [line.split("|") for line in out.splitlines() if line]


def transfer_service_psql() -> DockerPsql:
    """El rol de transfer-service: dueño de ``core``; siembra cuentas y lee el estado final de las transferencias."""
    return DockerPsql("verabank_transfer", "VERABANK_TRANSFER_PASSWORD")


def sim_loader_psql() -> DockerPsql:
    """Solo INSERT en ``risk.scoring_log`` (calentamiento del replay, D1)."""
    return DockerPsql("sim_loader", "SIM_LOADER_PASSWORD")


def read_only_psql() -> DockerPsql:
    """El rol de solo lectura de BI: lee ``risk`` y ``analytics``."""
    return DockerPsql("verabank_bi", "VERABANK_BI_PASSWORD")
