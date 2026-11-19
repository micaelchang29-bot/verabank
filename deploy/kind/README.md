# Despliegue en kind (ampliación, ADR-13 D12)

Requisitos: `kind` v0.33.0, `kubectl` y Docker; Compose detenido (`make down`, conserva sus datos).

**Versión y hash, de la fuente oficial** (`gh api repos/kubernetes-sigs/kind/releases/latest`, consultado el
2026-11-19; release publicado el 2026-08-26): `v0.33.0`; binario
`https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64` con SHA-256
`aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d` (el `digest` del asset y el archivo
`kind-linux-amd64.sha256sum` del release coinciden). `make kind-up`, `kind-deploy` y `kind-down` comprueban ese hash
**antes** de ejecutar el binario (`scripts/kind-lib.sh`, `verified_kind`) y abortan si no coincide; `kind.yml` lo
comprueba antes de darle permiso de ejecución. Imagen del nodo fijada por digest, como piden las notas del release:
`kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5`. Al cambiar de versión
se actualizan juntos `scripts/kind-lib.sh`, `.github/workflows/kind.yml`, `deploy/kind/cluster.yaml` y este archivo.

```bash
make kind-check                 # sin clúster: límites, probes, réplicas, PDB, ≤ 10 GiB, sin credenciales
make kind-up                    # clúster «verabank»: transfer-service en localhost:18080, Metabase en 13000
make kind-deploy                # imágenes con kind load, Secret desde .env, manifiestos y espera a Ready
make kind-smoke                 # = make smoke KIND=1
make kind-chaos                 # k6 a 20 TPS y borrado forzado de un pod de risk-scoring; necesita el canal loadtest
make kind-down CONFIRM=yes      # borra el clúster y sus volúmenes
```

| Archivo | Contenido |
|---|---|
| `cluster.yaml` | Un nodo; `extraPortMappings` 30080 → 18080 y 30300 → 13000 (solo `127.0.0.1`). |
| `base/` | PostgreSQL y Kafka (StatefulSet con PVC), transfer-service ×2, risk-scoring ×2 (con PDB), risk-stream ×1, rail simulado, Metabase y los Jobs `db-migrate` y `bootstrap`. ConfigMaps generados desde `db/` y `deploy/kafka/`. |
| `overlays/kind/` | Services de transfer-service y Metabase como NodePort. |
| `check_manifests.py` | Comprobación estática (`make kind-check`; también en la CI). |

Se construye con `kubectl kustomize --load-restrictor LoadRestrictionsNone deploy/kind/overlays/kind`: los ConfigMaps
leen los archivos del repositorio, fuera de este directorio. Una migración nueva se añade a `base/kustomization.yaml`.
