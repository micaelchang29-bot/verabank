"""Comprobación estática de los manifiestos de kind (ADR-13, D12), sin clúster.

Uso: kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/kind/overlays/kind | python3 deploy/kind/check_manifests.py

Exige: requests y limits de CPU y memoria en todo contenedor; readiness, liveness y preStop en los de larga vida;
réplicas (transfer-service 2, risk-scoring 2, risk-stream 1) y el rol de cada risk-service; PodDisruptionBudget del
scoring con minAvailable 1; total de límites de memoria (réplicas × límites, más el mayor pod de un Job) ≤ 10 GiB
(§1.2); ninguna imagen sin etiqueta o con latest y las propias con imagePullPolicy Never; ninguna credencial fuera de
secretKeyRef ni ningún Secret en los manifiestos; las migraciones de db/migrations completas en sus ConfigMaps; y los
NodePort del overlay iguales a los extraPortMappings de cluster.yaml, con la imagen del nodo fijada por digest. Sale con 1 y lista los problemas.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
LIMIT_GIB = 10
REPLICAS = {"transfer-service": 2, "risk-scoring": 2, "risk-stream": 1}
ROLES = {"risk-scoring": "scoring", "risk-stream": "stream"}
SENSITIVE = re.compile(r"PASSWORD|SECRET|TOKEN|API_KEYS?$|CLUSTER_ID")
NODE_PORTS = {"transfer-service": 30080, "metabase": 30300}
UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "K": 10**3, "M": 10**6, "G": 10**9}


def memory_bytes(text: str) -> int:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMG]i?)?", str(text))
    if not m:
        raise ValueError(f"memoria no reconocida: {text}")
    return int(float(m.group(1)) * UNITS.get(m.group(2) or "", 1))


def pod_spec(obj: dict) -> dict | None:
    if obj["kind"] in ("Deployment", "StatefulSet", "Job"):
        return obj["spec"]["template"]["spec"]
    return None


def check(docs: list[dict]) -> tuple[list[str], dict]:
    problems: list[str] = []
    by_name = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    configmaps = {d["metadata"]["name"].rsplit("-", 1)[0]: d for d in docs if d["kind"] == "ConfigMap"}
    total = 0
    biggest_job = 0
    for d in docs:
        kind, name = d["kind"], d["metadata"]["name"]
        if kind == "Secret":
            problems.append(f"Secret {name} en los manifiestos: los secretos los crea make kind-secrets desde .env")
        if kind == "ConfigMap":
            for key, value in (d.get("data") or {}).items():
                if SENSITIVE.search(key):
                    problems.append(f"ConfigMap {name}: clave sensible {key} fuera de un Secret")
                if re.search(r"(?i)password\s*=\s*\S", str(value)) and not key.endswith((".sql", ".sh")):
                    problems.append(f"ConfigMap {name}/{key}: parece una credencial")
        spec = pod_spec(d)
        if spec is None:
            continue
        long_lived = kind != "Job"
        replicas = d["spec"].get("replicas", 1) if long_lived else 1
        if name in REPLICAS and replicas != REPLICAS[name]:
            problems.append(f"{kind} {name}: {replicas} réplicas, se esperaban {REPLICAS[name]}")
        pod_limit, init_limit = 0, 0
        for section in ("initContainers", "containers"):
            for c in spec.get(section) or []:
                where = f"{kind} {name}/{c['name']}"
                image = c["image"]
                if ":" not in image.rsplit("/", 1)[-1] or image.endswith(":latest"):
                    problems.append(f"{where}: imagen sin etiqueta fija ({image})")
                if image.startswith("verabank/") and c.get("imagePullPolicy") != "Never":
                    problems.append(f"{where}: imagen local sin imagePullPolicy Never")
                res = c.get("resources") or {}
                for part in ("requests", "limits"):
                    for r in ("cpu", "memory"):
                        if r not in (res.get(part) or {}):
                            problems.append(f"{where}: falta resources.{part}.{r}")
                mem = memory_bytes(res.get("limits", {}).get("memory", "0"))
                if section == "containers":
                    pod_limit += mem
                else:
                    init_limit = max(init_limit, mem)
                for env in c.get("env") or []:
                    if SENSITIVE.search(env["name"]) and "secretKeyRef" not in (env.get("valueFrom") or {}):
                        problems.append(f"{where}: {env['name']} no viene de un secretKeyRef")
                if long_lived and section == "containers":
                    for probe in ("readinessProbe", "livenessProbe"):
                        if probe not in c:
                            problems.append(f"{where}: falta {probe}")
                    if "preStop" not in (c.get("lifecycle") or {}):
                        problems.append(f"{where}: falta lifecycle.preStop")
        pod = max(pod_limit, init_limit)
        if long_lived:
            total += replicas * pod
        else:
            biggest_job = max(biggest_job, pod)
    for name, role in ROLES.items():
        cm = configmaps.get(f"{name}-config")
        if cm is None or (cm.get("data") or {}).get("VERABANK_ROLE") != role:
            problems.append(f"{name}: VERABANK_ROLE debe ser {role}")
    pdb = by_name.get(("PodDisruptionBudget", "risk-scoring"))
    if pdb is None or pdb["spec"].get("minAvailable") != 1 or pdb["spec"]["selector"]["matchLabels"] != {"app": "risk-scoring"}:
        problems.append("falta el PodDisruptionBudget de risk-scoring con minAvailable: 1")
    for d in ("core", "audit", "risk", "analytics"):
        expected = sorted(p.name for p in (ROOT / "db" / "migrations" / d).glob("*.sql"))
        cm = configmaps.get(f"migrations-{d}")
        got = sorted((cm or {}).get("data", {}))
        if got != expected:
            problems.append(f"ConfigMap migrations-{d}: {got} no coincide con db/migrations/{d} {expected}")
    cluster = yaml.safe_load((ROOT / "deploy" / "kind" / "cluster.yaml").read_text())
    for node in cluster["nodes"]:
        if not re.fullmatch(r"kindest/node:v[\d.]+@sha256:[0-9a-f]{64}", node.get("image", "")):
            problems.append(f"cluster.yaml: la imagen del nodo {node.get('role')} debe ir fijada por digest")
    mapped = {m["containerPort"] for n in cluster["nodes"] for m in n.get("extraPortMappings", [])}
    for svc, port in NODE_PORTS.items():
        s = by_name.get(("Service", svc))
        ports = [p.get("nodePort") for p in (s or {}).get("spec", {}).get("ports", [])]
        if not s or s["spec"].get("type") != "NodePort" or port not in ports or port not in mapped:
            problems.append(f"Service {svc}: NodePort {port} ausente o sin extraPortMapping en cluster.yaml")
    grand = total + biggest_job
    if grand > LIMIT_GIB * 2**30:
        problems.append(f"límites de memoria {grand / 2**30:.2f} GiB > {LIMIT_GIB} GiB")
    return problems, {"long_lived_gib": total / 2**30, "biggest_job_gib": biggest_job / 2**30, "total_gib": grand / 2**30}


def main() -> int:
    docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
    problems, totals = check(docs)
    print(f"kind: {len(docs)} objetos; límites de memoria: {totals['long_lived_gib']:.2f} GiB de larga vida + "
          f"{totals['biggest_job_gib']:.2f} GiB del mayor Job = {totals['total_gib']:.2f} GiB (máximo {LIMIT_GIB})")
    for p in problems:
        print(f"PROBLEMA: {p}")
    print("OK" if not problems else f"{len(problems)} problemas")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
