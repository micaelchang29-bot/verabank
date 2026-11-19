"""Acceso a PostgreSQL en el clúster kind (VERABANK_KIND=1): kubectl exec y sin contraseñas en la línea de comandos."""

from __future__ import annotations

import subprocess

from verabank_sim.load import db


def test_kind_mode_uses_kubectl_exec_on_the_postgres_statefulset(monkeypatch) -> None:
    monkeypatch.setenv("VERABANK_KIND", "1")
    monkeypatch.setenv("POSTGRES_DB", "verabank")
    monkeypatch.setenv("VERABANK_TRANSFER_PASSWORD", "s3cr3t-no-debe-aparecer")
    monkeypatch.delenv("VERABANK_DB_CONTAINER", raising=False)
    cmd = db.transfer_service_psql().command("-tA")
    assert cmd[:7] == ["kubectl", "--context", "kind-verabank", "exec", "-i", "statefulset/postgres", "--"]
    assert cmd[-4:] == ["verabank_transfer", "verabank", "VERABANK_TRANSFER_PASSWORD", "-tA"]
    assert not any("s3cr3t" in part for part in cmd)


def test_the_kind_script_reads_the_password_from_the_pod_environment() -> None:
    probe = db._KUBE_SCRIPT.replace("exec psql", "printf '%s|' \"$PGPASSWORD\" \"$u\" \"$d\" \"$@\"; :")
    out = subprocess.run(["sh", "-c", probe, "sh", "sim_loader", "vb", "SIM_LOADER_PASSWORD", "-c", "SELECT 1"],
                         env={"SIM_LOADER_PASSWORD": 'p"w$x y', "PATH": "/usr/bin:/bin"}, capture_output=True,
                         text=True, check=True)
    assert out.stdout.split("|")[:5] == ['p"w$x y', "sim_loader", "vb", "-c", "SELECT 1"]


def test_without_kind_compose_is_still_the_default(monkeypatch) -> None:
    monkeypatch.delenv("VERABANK_KIND", raising=False)
    monkeypatch.delenv("VERABANK_DB_CONTAINER", raising=False)
    assert db.read_only_psql().command()[:2] == ["docker", "compose"]
