# Despliegue en kind (ampliación, ADR-13 D12)

Requisitos: `kind` v0.31.0, `kubectl` y Docker; Compose detenido (`make down`, conserva sus datos).

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
