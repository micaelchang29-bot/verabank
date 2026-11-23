"""docs/kpis.sql frente a §5g, el parser de marcadores y la idempotencia de setup_metabase.py (API simulada)."""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

import setup_metabase as sm

BASE = "http://metabase.test"
ENV = {"METABASE_ADMIN_EMAIL": "admin@verabank.local", "METABASE_ADMIN_PASSWORD": "pw-admin-1234",
       "VERABANK_BI_PASSWORD": "pw-bi", "POSTGRES_DB": "verabank"}


def section_5g_sql() -> str:
    text = (sm.ROOT / "docs" / "diseno.md").read_text(encoding="utf-8")
    section = text[text.index("### 5g."):text.index("## 6. Pruebas")]
    return re.search(r"```sql\n(.*?)```", section, re.S).group(1)


def test_kpis_sql_is_section_5g_verbatim():
    assert (sm.ROOT / "docs" / "kpis.sql").read_text(encoding="utf-8") == section_5g_sql()


def test_exactly_five_kpi_markers_with_title_and_sql():
    queries = sm.parse_queries((sm.ROOT / "docs" / "kpis.sql").read_text(encoding="utf-8"))
    assert [q.marker for q in queries] == ["KPI 1", "KPI 2", "KPI 3", "KPI 4", "KPI 5"]
    assert queries[0].title == "tasa de detección de fraude por día (casos y monto)"
    assert queries[2].card_name == "KPI 3 — latencia de alertas AML y falsos positivos por regla"
    for q in queries:
        assert q.sql.startswith("SELECT") and q.sql.endswith(";") and "-- KPI" not in q.sql
    assert queries[4].sql.endswith("GROUP BY 1 ORDER BY 1 DESC LIMIT 24;")


def test_parser_associates_each_title_with_its_own_sql():
    text = "-- KPI 1: uno\nSELECT 1;\n\n-- EXTRA B: dos\nSELECT 2\nFROM x;\n"
    assert sm.parse_queries(text) == [sm.Query("KPI 1", "uno", "SELECT 1;"), sm.Query("EXTRA B", "dos", "SELECT 2\nFROM x;")]
    assert sm.parse_queries(text)[1].card_name == "Extra B — dos"


def test_load_queries_has_the_five_kpis_and_the_two_extras():
    queries = sm.load_queries()
    assert [q.marker for q in queries] == ["KPI 1", "KPI 2", "KPI 3", "KPI 4", "KPI 5", "EXTRA A", "EXTRA B"]
    assert "analytics.drift_metrics" in queries[5].sql and "analytics.fraud_reasons" in queries[6].sql


def test_load_queries_rejects_a_missing_kpi(tmp_path):
    bad = tmp_path / "kpis.sql"
    bad.write_text("-- KPI 1: a\nSELECT 1;\n-- KPI 3: c\nSELECT 3;\n")
    with pytest.raises(SystemExit):
        sm.load_queries((bad,))


class FakeMetabase:
    """Estado en memoria de una instancia de Metabase, servido por respx."""

    def __init__(self) -> None:
        self.setup_done = False
        self.objects: dict[str, dict[int, dict]] = {"database": {}, "collection": {1: {"id": "root", "name": "Our analytics"}},
                                                    "card": {}, "dashboard": {}}
        self.next_id = 100
        self.requests: list[tuple[str, str]] = []
        self.syncs = 0

    def new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        body = json.loads(request.content) if request.content else {}
        if path != "/api/session/properties" and path not in ("/api/setup", "/api/session"):
            assert request.headers.get("X-Metabase-Session") == "sess-1"
        if path == "/api/session/properties":
            return httpx.Response(200, json={"has-user-setup": self.setup_done, "setup-token": "tok"})
        if path == "/api/setup":
            assert not self.setup_done and body["token"] == "tok" and body["prefs"]["allow_tracking"] is False
            self.setup_done = True
            return httpx.Response(200, json={"id": "sess-1"})
        if path == "/api/session":
            assert body == {"username": ENV["METABASE_ADMIN_EMAIL"], "password": ENV["METABASE_ADMIN_PASSWORD"]}
            return httpx.Response(200, json={"id": "sess-1"})
        match = re.fullmatch(r"/api/(database|collection|card|dashboard)(?:/(\d+))?(/sync_schema)?", path)
        assert match, path
        kind, oid, sync = match.group(1), match.group(2), match.group(3)
        store = self.objects[kind]
        if sync:
            self.syncs += 1
            return httpx.Response(200, json={"status": "ok"})
        if method == "GET" and oid is None:
            items = [v for k, v in store.items() if kind != "collection" or k != 1] + ([store[1]] if kind == "collection" else [])
            return httpx.Response(200, json={"data": items, "total": len(items)} if kind == "database" else items)
        if method == "GET":
            return httpx.Response(200, json=store[int(oid)])
        if method == "POST":
            obj = {**body, "id": self.new_id(), "archived": False}
            if kind == "dashboard":
                obj["dashcards"] = []
            store[obj["id"]] = obj
            return httpx.Response(200, json=obj)
        if method == "PUT":
            obj = store[int(oid)]
            if kind == "dashboard":
                cards = []
                for dc in body["dashcards"]:
                    cards.append({**dc, "id": dc["id"] if dc["id"] > 0 else self.new_id()})
                obj["dashcards"] = cards
            else:
                obj.update(body)
            return httpx.Response(200, json=obj)
        raise AssertionError((method, path))


@pytest.fixture
def fake():
    server = FakeMetabase()
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.route().mock(side_effect=lambda request: server.handler(request))   # se busca en cada solicitud
        yield server


def run_once(fake: FakeMetabase) -> dict:
    mb = sm.Metabase(BASE)
    try:
        return sm.setup(mb, ENV, sm.load_queries())
    finally:
        mb.close()


def test_first_run_creates_everything_with_the_read_only_role(fake):
    result = run_once(fake)
    assert result["ops"] == {"create:card": 7, "create:collection": 1, "create:dashboard": 1, "create:database": 1,
                             "setup": 1, "update:dashboard": 1}
    db = next(iter(fake.objects["database"].values()))
    assert db["details"]["user"] == "verabank_bi" and db["details"]["dbname"] == "verabank" and db["engine"] == "postgres"
    assert fake.syncs == 1
    cards = list(fake.objects["card"].values())
    assert sorted(c["name"] for c in cards if c["name"].startswith("KPI")) == [
        f"KPI {i} — " + q.title for i, q in enumerate(sm.load_queries()[:5], start=1)]
    assert all(c["dataset_query"]["type"] == "native" and c["dataset_query"]["database"] == db["id"] for c in cards)
    dash = next(iter(fake.objects["dashboard"].values()))
    names = {c["id"]: c["name"] for c in cards}
    order = [names[dc["card_id"]] for dc in sorted(dash["dashcards"], key=lambda d: (d["row"], d["col"]))]
    assert order[0].startswith("Extra A") and order[1:6] == [f"KPI {i} — " + sm.load_queries()[i - 1].title
                                                             for i in range(1, 6)] and order[6].startswith("Extra B")


def test_second_run_only_updates_and_creates_nothing(fake):
    first = run_once(fake)
    snapshot = {k: sorted(v) for k, v in fake.objects.items()}
    dashcard_ids = sorted(dc["id"] for dc in next(iter(fake.objects["dashboard"].values()))["dashcards"])
    fake.requests.clear()
    second = run_once(fake)
    assert not any(k.startswith("create") or k == "setup" for k in second["ops"])
    assert second["ops"] == {"update:card": 7, "update:collection": 1, "update:dashboard": 1, "update:database": 1}
    assert not any(m == "POST" and p in ("/api/card", "/api/collection", "/api/dashboard", "/api/database", "/api/setup")
                   for m, p in fake.requests)
    assert {k: sorted(v) for k, v in fake.objects.items()} == snapshot
    assert sorted(dc["id"] for dc in next(iter(fake.objects["dashboard"].values()))["dashcards"]) == dashcard_ids
    assert second["cards"] == first["cards"] and second["dashboard_id"] == first["dashboard_id"]


def test_a_changed_query_is_updated_in_place(fake):
    run_once(fake)
    card = next(c for c in fake.objects["card"].values() if c["name"].startswith("KPI 1"))
    card["dataset_query"]["native"]["query"] = "SELECT 'editada a mano'"
    run_once(fake)
    assert card["dataset_query"]["native"]["query"] == sm.load_queries()[0].sql
    assert len(fake.objects["card"]) == 7


def test_login_retries_when_metabase_lost_its_database(fake, monkeypatch):
    """Primer bi-up: al recrearse PostgreSQL, Metabase respondió 401 al login por su conexión cortada."""
    fake.setup_done = True
    original, calls = fake.handler, []

    def flaky(request):
        if request.url.path == "/api/session":
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(401, text="FATAL: terminating connection due to administrator command")
        return original(request)

    monkeypatch.setattr(fake, "handler", flaky)
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    mb = sm.Metabase(BASE)
    try:
        assert mb.login(ENV["METABASE_ADMIN_EMAIL"], ENV["METABASE_ADMIN_PASSWORD"]) == "sess-1"
    finally:
        mb.close()
    assert len(calls) == 2


def test_login_does_not_retry_a_wrong_password(fake, monkeypatch):
    fake.setup_done = True
    original, calls = fake.handler, []

    def wrong(request):
        if request.url.path == "/api/session":
            calls.append(1)
            return httpx.Response(401, json={"errors": {"password": "did not match stored password"}})
        return original(request)

    monkeypatch.setattr(fake, "handler", wrong)
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)
    mb = sm.Metabase(BASE)
    try:
        with pytest.raises(sm.ApiError, match="HTTP 401"):
            mb.login(ENV["METABASE_ADMIN_EMAIL"], "otra")
    finally:
        mb.close()
    assert len(calls) == 1
