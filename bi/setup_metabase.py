"""Crea en Metabase, por su API, la conexión de solo lectura, la colección, las tarjetas de KPIs y el dashboard (D9).

Idempotente: busca cada objeto por nombre y lo actualiza; solo crea lo que falta. La edición open source no exporta
dashboards (manual 0.2), así que el dashboard es código. Versión fijada en docker-compose.yml (servicio ``metabase``).

Entorno (de .env, por scripts/bi-up.sh): METABASE_URL, METABASE_ADMIN_EMAIL, METABASE_ADMIN_PASSWORD,
VERABANK_BI_PASSWORD, POSTGRES_DB y, opcionales, METABASE_DB_HOST/PORT (cómo ve Metabase a PostgreSQL). No imprime
credenciales.
"""

from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
KPI_FILES = (ROOT / "docs" / "kpis.sql", ROOT / "docs" / "kpis-extra.sql")
DATABASE_NAME = "VeraBank"
COLLECTION_NAME = "VeraBank"
DASHBOARD_NAME = "VeraBank — Riesgo"
MARKER = re.compile(r"^-- (KPI \d+|EXTRA [A-Z]): (.+)$")
BI_ROLE = "verabank_bi"   # el bi_ro del manual (db/README.md)


@dataclass(frozen=True)
class Query:
    marker: str      # "KPI 1", "EXTRA A"
    title: str
    sql: str

    @property
    def card_name(self) -> str:
        label = self.marker if self.marker.startswith("KPI") else "Extra " + self.marker.split()[1]
        return f"{label} — {self.title}"


def parse_queries(text: str) -> list[Query]:
    """Divide un archivo .sql por sus marcadores ``-- KPI n: título`` o ``-- EXTRA X: título``."""
    out: list[Query] = []
    marker = title = None
    body: list[str] = []
    for line in text.splitlines():
        found = MARKER.match(line)
        if found:
            if marker:
                out.append(Query(marker, title, "\n".join(body).strip()))
            marker, title, body = found.group(1), found.group(2).strip(), []
        elif marker:
            body.append(line)
    if marker:
        out.append(Query(marker, title, "\n".join(body).strip()))
    return out


def load_queries(paths=KPI_FILES) -> list[Query]:
    queries = [q for p in paths for q in parse_queries(p.read_text(encoding="utf-8"))]
    kpis = [q for q in queries if q.marker.startswith("KPI")]
    if len(kpis) != 5 or [q.marker for q in kpis] != [f"KPI {i}" for i in range(1, 6)]:
        raise SystemExit(f"se esperaban KPI 1 a 5 en docs/kpis.sql; hay {[q.marker for q in kpis]}")
    return queries


class Metabase:
    """Cliente mínimo de la API. Cuenta las creaciones y actualizaciones (la prueba de idempotencia las mira)."""

    def __init__(self, base_url: str, *, transport: httpx.BaseTransport | None = None, timeout: float = 60.0):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)
        self.ops: Counter[str] = Counter()

    def call(self, method: str, path: str, **kw) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path}: HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def close(self) -> None:
        self.http.close()

    # --- sesión ---
    def login(self, email: str, password: str) -> str:
        props = self.call("GET", "/api/session/properties")
        if not props.get("has-user-setup"):
            body = {"token": props["setup-token"],
                    "user": {"email": email, "password": password, "first_name": "Admin", "last_name": "VeraBank",
                             "site_name": "VeraBank"},
                    "prefs": {"site_name": "VeraBank", "site_locale": "es", "allow_tracking": False}}
            session = self.call("POST", "/api/setup", json=body)["id"]
            self.ops["setup"] += 1
        else:
            session = self.call("POST", "/api/session", json={"username": email, "password": password})["id"]
        self.http.headers["X-Metabase-Session"] = session
        return session

    # --- búsqueda por nombre ---
    @staticmethod
    def _items(payload: Any) -> list[dict]:
        return payload["data"] if isinstance(payload, dict) and "data" in payload else payload

    def _upsert(self, kind: str, existing: dict | None, body: dict, path: str) -> dict:
        if existing is None:
            self.ops[f"create:{kind}"] += 1
            return self.call("POST", path, json=body)
        self.ops[f"update:{kind}"] += 1
        return self.call("PUT", f"{path}/{existing['id']}", json=body)

    def database(self, details: dict) -> int:
        found = next((d for d in self._items(self.call("GET", "/api/database")) if d["name"] == DATABASE_NAME), None)
        body = {"engine": "postgres", "name": DATABASE_NAME, "details": details, "is_full_sync": True}
        db = self._upsert("database", found, body, "/api/database")
        self.call("POST", f"/api/database/{db['id']}/sync_schema")
        return db["id"]

    def collection(self) -> int:
        found = next((c for c in self._items(self.call("GET", "/api/collection"))
                      if c.get("name") == COLLECTION_NAME and not c.get("archived") and isinstance(c.get("id"), int)),
                     None)
        return self._upsert("collection", found, {"name": COLLECTION_NAME}, "/api/collection")["id"]

    def card(self, q: Query, database_id: int, collection_id: int, existing: list[dict]) -> int:
        found = next((c for c in existing if c["name"] == q.card_name and c.get("collection_id") == collection_id
                      and not c.get("archived")), None)
        body = {"name": q.card_name, "display": "table", "visualization_settings": {},
                "collection_id": collection_id, "description": f"docs/{'kpis' if q.marker.startswith('KPI') else 'kpis-extra'}.sql, {q.marker}",
                "dataset_query": {"type": "native", "database": database_id, "native": {"query": q.sql}}}
        return self._upsert("card", found, body, "/api/card")["id"]

    def dashboard(self, collection_id: int, card_ids: list[int]) -> int:
        found = next((d for d in self._items(self.call("GET", "/api/dashboard"))
                      if d["name"] == DASHBOARD_NAME and d.get("collection_id") == collection_id
                      and not d.get("archived")), None)
        if found is None:
            self.ops["create:dashboard"] += 1
            found = self.call("POST", "/api/dashboard", json={"name": DASHBOARD_NAME, "collection_id": collection_id})
            current = {}
        else:
            current = {dc["card_id"]: dc["id"] for dc in self.call("GET", f"/api/dashboard/{found['id']}").get("dashcards", [])}
        dashcards = []
        for i, card_id in enumerate(card_ids):   # dos columnas de 12; la tarjeta de drift arriba del todo
            dashcards.append({"id": current.get(card_id, -(i + 1)), "card_id": card_id, "row": (i // 2) * 6,
                              "col": (i % 2) * 12, "size_x": 12, "size_y": 6, "parameter_mappings": [],
                              "visualization_settings": {}})
        self.ops["update:dashboard"] += 1
        self.call("PUT", f"/api/dashboard/{found['id']}", json={"dashcards": dashcards})
        return found["id"]


def wait_healthy(mb: Metabase, timeout_s: float = 300) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if mb.http.get("/api/health").json().get("status") == "ok":
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(3)
    raise SystemExit("Metabase no respondió /api/health a tiempo")


def setup(mb: Metabase, env: dict[str, str], queries: list[Query]) -> dict[str, Any]:
    mb.login(env["METABASE_ADMIN_EMAIL"], env["METABASE_ADMIN_PASSWORD"])
    details = {"host": env.get("METABASE_DB_HOST", "postgres"), "port": int(env.get("METABASE_DB_PORT", "5432")),
               "dbname": env.get("POSTGRES_DB", "verabank"), "user": BI_ROLE, "password": env["VERABANK_BI_PASSWORD"],
               "ssl": False, "schema-filters-type": "inclusion", "schema-filters-patterns": "analytics,risk"}
    database_id = mb.database(details)
    collection_id = mb.collection()
    existing = mb._items(mb.call("GET", "/api/card", params={"f": "all"}))
    # La tarjeta de drift primero (aviso de drift, D9), después los cinco KPIs y la cola de revisión.
    position = {"EXTRA A": 0, "EXTRA B": 6}
    ordered = sorted(queries, key=lambda q: position[q.marker] if q.marker in position else int(q.marker.split()[1]))
    cards = {q.card_name: mb.card(q, database_id, collection_id, existing) for q in ordered}
    dashboard_id = mb.dashboard(collection_id, list(cards.values()))
    return {"database_id": database_id, "collection_id": collection_id, "cards": cards, "dashboard_id": dashboard_id,
            "ops": dict(sorted(mb.ops.items()))}


def main() -> int:
    env = dict(os.environ)
    missing = [k for k in ("METABASE_ADMIN_EMAIL", "METABASE_ADMIN_PASSWORD", "VERABANK_BI_PASSWORD") if not env.get(k)]
    if missing:
        print(f"faltan en el entorno (.env): {', '.join(missing)}", file=sys.stderr)
        return 2
    url = env.get("METABASE_URL", f"http://localhost:{env.get('METABASE_PORT', '3000')}")
    mb = Metabase(url)
    try:
        wait_healthy(mb)
        result = setup(mb, env, load_queries())
    finally:
        mb.close()
    print(f"operaciones: {result['ops']}")
    for name, card_id in result["cards"].items():
        print(f"  tarjeta {card_id}: {name}")
    dashboard_url = f"{url}/dashboard/{result['dashboard_id']}"
    out = ROOT / "data" / "bi" / "dashboard_url.txt"   # make bi-url lo lee (data/ no se versiona)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dashboard_url + "\n", encoding="utf-8")
    print(f"dashboard: {dashboard_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
