"""``aml``: envío de data/aml/transfers.jsonl por la API (sin servicios)."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

from verabank_sim.aml import run as aml
from verabank_sim.load.api import Reply

A, B, C = (f"{n}0000000-0000-4000-8000-000000000000" for n in "abc")


def transfer(tid: str, when: str, src: str, amount: str, *, dst: str | None = None, ext: str | None = None,
             country: str = "PE", ttype: str = "TRANSFER") -> dict:
    t = {"transfer_id": tid, "source_account_id": src, "dest_country": country, "rail": "INTERNAL" if dst else "EXTERNAL",
         "transfer_type": ttype, "amount": amount, "currency": "PEN", "occurred_at": when}
    t.update({"dest_account_id": dst} if dst else {"dest_external_ref": ext})
    return t


TRANSFERS = [
    transfer("t3", "2026-01-01T02:10:00Z", B, "20.00", ext="EXT-1", country="KP"),
    transfer("t1", "2026-01-01T01:05:00Z", A, "9500.00", dst=B),
    transfer("t2", "2026-01-01T01:40:00Z", A, "9600.00", dst=C),
    transfer("t4", "2026-01-01T01:40:00Z", C, "5.50", dst=A, ttype="PAYMENT"),
]


ORDERED = sorted(TRANSFERS, key=lambda t: (t["occurred_at"], t["transfer_id"]))


class Api:
    def __init__(self, in_review: set[str] = frozenset(), review_status: int = 200) -> None:
        self.in_review, self.review_status = in_review, review_status
        self.posts: list[tuple[str, dict]] = []
        self.reviews: list[tuple[str, str, str]] = []

    def post_transfer(self, key: str, body: dict) -> Reply:
        self.posts.append((key, body))
        sid = f"s{len(self.posts)}"
        if body["amount"] in self.in_review:
            return Reply(201, {}, {"transfer_id": sid, "status": "IN_REVIEW"}, 1.0)
        return Reply(201, {}, {"transfer_id": sid, "status": "COMPLETED"}, 1.0)

    def review(self, tid: str, decision: str, comment: str) -> Reply:
        self.reviews.append((tid, decision, comment))
        return Reply(self.review_status, {}, {"status": "COMPLETED"}, 1.0)


class State:
    def __init__(self, blocked_amount: str | None = None, stuck_amount: str | None = None) -> None:
        self.blocked, self.stuck = blocked_amount, stuck_amount
        self.statements: list[str] = []

    def run(self, stdin: str, *args: str, timeout_s: float = 0) -> str:
        self.statements.append(stdin)
        return "3|6|3|19100.00\n"

    def query(self, sql: str) -> list[list[str]]:
        keys = re.findall(r"'([0-9a-f-]{36})'", sql)
        by_key = {aml.idempotency_key(t): t for t in TRANSFERS}
        rows = []
        for i, k in enumerate(keys):
            amount = by_key[k]["amount"]
            status, reason = "COMPLETED", ""
            if amount == self.blocked:
                status, reason = "REJECTED", "RISK_BLOCK"
            elif amount == self.stuck:
                status = "PROCESSING"
            rows.append([k, f"{i:08d}-0000-4000-8000-000000000000", status, "APPROVE", "MODEL", "9", reason])
        return rows


def test_se_envian_por_hora_de_negocio_y_en_orden_con_el_cuerpo_del_contrato(tmp_path: Path) -> None:
    path = tmp_path / "transfers.jsonl"
    path.write_text("".join(json.dumps(t) + "\n" for t in TRANSFERS))
    groups = aml.build_groups(aml.read_transfers(path))   # el archivo se lee ya ordenado por (occurred_at, id)
    assert [g for g, _ in groups] == ["2026-01-01T01", "2026-01-01T02"]
    assert [j.transfer["transfer_id"] for _, g in groups for j in g] == ["t1", "t2", "t4", "t3"]
    first = groups[0][1][0].body
    assert first == {"source_account_id": A, "dest_country": "PE", "rail": "INTERNAL", "transfer_type": "TRANSFER",
                     "amount": "9500.00", "currency": "PEN", "occurred_at": "2026-01-01T01:05:00Z", "dest_account_id": B}
    external = groups[1][1][0].body
    assert external["rail"] == "EXTERNAL" and external["dest_external_ref"] == "EXT-1" and "dest_account_id" not in external
    assert isinstance(external["amount"], str)


def test_la_clave_de_idempotencia_es_determinista_por_transferencia() -> None:
    assert aml.idempotency_key(TRANSFERS[0]) == aml.idempotency_key(dict(TRANSFERS[0]))
    assert len({aml.idempotency_key(t) for t in TRANSFERS}) == 4


def test_se_siembran_todas_las_cuentas_con_lo_que_envian() -> None:
    accounts = {a.account_id: a.balance for a in aml.seed_accounts(TRANSFERS)}
    assert accounts == {A: Decimal("19100.00"), B: Decimal("20.00"), C: Decimal("5.50")}
    receiving_only = [transfer("x", "2026-01-01T00:00:00Z", A, "1.00", dst="d0000000-0000-4000-8000-000000000000")]
    assert {a.account_id: a.balance for a in aml.seed_accounts(receiving_only)}["d0000000-0000-4000-8000-000000000000"] == 0


def test_con_approve_reviews_aprueba_las_que_quedan_en_revision(tmp_path: Path) -> None:
    api, state = Api(in_review={"9600.00"}), State()
    out_map = tmp_path / "service_run.jsonl"
    summary = aml.run(ORDERED, aml.Options(tps=1000, approve_reviews=True, final_timeout_s=1), api, state, "web", out_map)
    assert api.reviews == [("s2", "APPROVE", "aml-eval")]
    assert summary["sent"] == 4 and summary["approved_in_review"] == 1 and summary["completed"] == 3
    assert summary["blocked"] == 0 and summary["not_completed"] == 0 and summary["seeded_accounts"] == 3
    lines = [json.loads(line) for line in out_map.read_text().splitlines()]
    assert [m["transfer_id"] for m in lines] == ["t1", "t2", "t4", "t3"]
    assert {m["transfer_id"]: m["outcome"] for m in lines}["t2"] == "APPROVED_IN_REVIEW"
    assert all(m["service_transfer_id"] for m in lines)


def test_sin_approve_reviews_no_se_aprueba_nada() -> None:
    api = Api(in_review={"9600.00"})
    aml.run(ORDERED, aml.Options(tps=1000, approve_reviews=False, seed=False, final_timeout_s=1), api, State(), "web", None)
    assert api.reviews == []


def test_block_se_informa_aparte_y_lo_no_terminado_tambien() -> None:
    api, state = Api(), State(blocked_amount="9500.00", stuck_amount="20.00")
    summary = aml.run(ORDERED, aml.Options(tps=1000, seed=False, final_timeout_s=0.01), api, state, "web", None)
    assert summary["blocked"] == 1 and summary["not_completed"] == 1 and summary["completed"] == 2
    assert "rechazadas por BLOCK" in aml.format_summary(summary)
