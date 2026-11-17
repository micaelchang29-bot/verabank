"""Generación determinista de transferencias legítimas y de las cuatro tipologías AML.

Toda decisión sale de ``random.Random`` con semillas de texto ``amlgen:<seed>:<flujo>``, sin depender del
orden de iteración de conjuntos (la salida es la misma byte a byte con la misma semilla).
"""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from typing import Any

NS = uuid.UUID("6f1d6a52-1c5e-5b0e-9a57-0a3c1e2d7b11")  # espacio de nombres de uuid5 de amlgen
CENT = Decimal("0.01")
TYPOLOGIES = ("STRUCTURING", "COUNTRY_RISK", "FAN_IN", "FAST_PASS")


def money(value: float | Decimal) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_EVEN)


@dataclass
class Generated:
    transfers: list[dict[str, Any]]
    truth: list[dict[str, Any]]
    near_threshold_legit: int = 0
    accounts: list[str] = field(default_factory=list)


class _Builder:
    def __init__(self, cfg: dict[str, Any], seed: int) -> None:
        self.cfg = cfg
        self.seed = seed
        self.epoch = datetime.fromisoformat(cfg["sim_epoch"]).astimezone(UTC)
        self.horizon = int(cfg["days"]) * 86400
        self.accounts = [str(uuid.uuid5(NS, f"account:{i}")) for i in range(int(cfg["accounts"]))]
        self.transfers: list[dict[str, Any]] = []

    def rng(self, stream: str) -> random.Random:
        return random.Random(f"amlgen:{self.seed}:{stream}")

    def tid(self, key: str) -> str:
        """Identificador provisional de generación; `finalize` lo sustituye por uno opaco."""
        return key

    def at(self, offset_s: int) -> str:
        return (self.epoch + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def add(self, key: str, *, src: str, dst: str | None, ext: str | None, country: str, amount: Decimal,
            when: int, ttype: str = "TRANSFER") -> str:
        t: dict[str, Any] = {
            "transfer_id": self.tid(key),
            "source_account_id": src,
            "dest_country": country,
            "rail": "INTERNAL" if dst else "EXTERNAL",
            "transfer_type": ttype,
            "amount": f"{amount:.2f}",
            "currency": self.cfg["currency"],
            "occurred_at": self.at(when),
        }
        if dst:
            t["dest_account_id"] = dst
        else:
            t["dest_external_ref"] = ext
        self.transfers.append(t)
        return t["transfer_id"]

    def other(self, rng: random.Random, *exclude: str) -> str:
        while True:
            a = self.accounts[rng.randrange(len(self.accounts))]
            if a not in exclude:
                return a

    def ext_ref(self, rng: random.Random) -> str:
        return f"EXT-{rng.randrange(10**9):09d}"


def _log_uniform(rng: random.Random, lo: float, hi: float) -> float:
    return math.exp(rng.uniform(math.log(lo), math.log(hi)))


def _truth(typology: str, n: int, seed: int, accounts: list[str], tids: list[str]) -> dict[str, Any]:
    return {
        "typology": typology,
        "instance_id": str(uuid.uuid5(NS, f"{seed}:{typology}:{n}")),
        "accounts": list(dict.fromkeys(accounts)),
        "transfer_ids": tids,
    }


def _background(b: _Builder) -> int:
    c = b.cfg["background"]
    rng = b.rng("background")
    n = round(len(b.accounts) * int(b.cfg["days"]) * float(c["per_account_per_day"]))
    lo, hi = c["near_threshold_pct"]
    threshold = Decimal(str(b.cfg["threshold"]))
    types = list(c["transfer_types"])
    weights = [c["transfer_types"][t] for t in types]
    near = 0
    for i in range(n):
        when = rng.randrange(b.horizon)
        src = b.accounts[rng.randrange(len(b.accounts))]
        if rng.random() < float(c["near_threshold_share"]):
            amount = money(threshold * Decimal(str(round(rng.uniform(lo, hi), 4))))
            near += 1
        else:
            amount = max(money(rng.lognormvariate(c["amount_mu"], c["amount_sigma"])), money(c["amount_min"]))
        ttype = rng.choices(types, weights)[0]
        if rng.random() < float(c["external_share"]):
            country = c["external_countries"][rng.randrange(len(c["external_countries"]))]
            b.add(f"legit:{i}", src=src, dst=None, ext=b.ext_ref(rng), country=country, amount=amount,
                  when=when, ttype=ttype)
        else:
            b.add(f"legit:{i}", src=src, dst=b.other(rng, src), ext=None, country=c["home_country"],
                  amount=amount, when=when, ttype=ttype)
    return near


def _structuring(b: _Builder) -> list[dict[str, Any]]:
    p = b.cfg["typologies"]["STRUCTURING"]
    rng = b.rng("STRUCTURING")
    threshold = Decimal(str(b.cfg["threshold"]))
    out = []
    for n in range(int(p["instances"])):
        src = b.other(rng)
        dst = b.other(rng, src)
        count = rng.randint(*p["transfers"])
        span = rng.randint(*p["span_days"]) * 86400
        start = rng.randrange(b.horizon - span)
        # primera y última fijan el tramo; el resto, al azar dentro de él
        offs = sorted([0, span] + [rng.randint(0, span) for _ in range(count - 2)])
        tids = []
        for k, off in enumerate(offs):
            pct = Decimal(str(round(rng.uniform(*p["amount_pct"]), 4)))
            tids.append(b.add(f"STRUCTURING:{n}:{k}", src=src, dst=dst, ext=None,
                              country=b.cfg["background"]["home_country"], amount=money(threshold * pct),
                              when=start + off))
        out.append(_truth("STRUCTURING", n, b.seed, [src, dst], tids))
    return out


def _country_risk(b: _Builder) -> list[dict[str, Any]]:
    p = b.cfg["typologies"]["COUNTRY_RISK"]
    rng = b.rng("COUNTRY_RISK")
    countries = list(b.cfg["risk_countries"]["high"]) + list(b.cfg["risk_countries"]["watch"])
    out = []
    for n in range(int(p["instances"])):
        src = b.other(rng)
        country = countries[rng.randrange(len(countries))]
        ext = b.ext_ref(rng)
        count = rng.randint(*p["transfers"])
        span = int(rng.uniform(*p["span_days"]) * 86400)
        start = rng.randrange(b.horizon - span)
        offs = sorted(rng.randint(0, span) for _ in range(count))
        tids = [b.add(f"COUNTRY_RISK:{n}:{k}", src=src, dst=None, ext=ext, country=country,
                      amount=money(_log_uniform(rng, *p["amount_range"])), when=start + off)
                for k, off in enumerate(offs)]
        out.append(_truth("COUNTRY_RISK", n, b.seed, [src], tids))
    return out


def _fan_in(b: _Builder) -> list[dict[str, Any]]:
    p = b.cfg["typologies"]["FAN_IN"]
    rng = b.rng("FAN_IN")
    window = int(p["window_hours"]) * 3600
    out = []
    for n in range(int(p["instances"])):
        dst = b.other(rng)
        n_senders = rng.randint(*p["senders"])
        senders: list[str] = []
        while len(senders) < n_senders:
            s = b.other(rng, dst, *senders)
            senders.append(s)
        start = rng.randrange(b.horizon - window)
        offs = sorted(rng.randrange(window) for _ in senders)  # todos dentro de la ventana (< 24 h)
        tids = [b.add(f"FAN_IN:{n}:{k}", src=s, dst=dst, ext=None, country=b.cfg["background"]["home_country"],
                      amount=money(rng.uniform(*p["amount_range"])), when=start + off)
                for k, (s, off) in enumerate(zip(senders, offs, strict=True))]
        out.append(_truth("FAN_IN", n, b.seed, [dst, *senders], tids))
    return out


def _fast_pass(b: _Builder) -> list[dict[str, Any]]:
    p = b.cfg["typologies"]["FAST_PASS"]
    rng = b.rng("FAST_PASS")
    countries = b.cfg["background"]["external_countries"]
    out = []
    for n in range(int(p["instances"])):
        sender = b.other(rng)
        mid = b.other(rng, sender)
        inbound = money(_log_uniform(rng, *p["amount_range"]))
        pct = Decimal(str(round(rng.uniform(*p["forward_pct"]), 4)))
        outbound = (inbound * pct).quantize(CENT, rounding=ROUND_CEILING)  # nunca por debajo del porcentaje
        delay = int(rng.uniform(*p["forward_delay_minutes"]) * 60)
        start = rng.randrange(b.horizon - delay)
        t_in = b.add(f"FAST_PASS:{n}:0", src=sender, dst=mid, ext=None,
                     country=b.cfg["background"]["home_country"], amount=inbound, when=start)
        t_out = b.add(f"FAST_PASS:{n}:1", src=mid, dst=None, ext=b.ext_ref(rng),
                      country=countries[rng.randrange(len(countries))], amount=outbound, when=start + delay)
        out.append(_truth("FAST_PASS", n, b.seed, [sender, mid], [t_in, t_out]))
    return out


def opaque_ids(keys: list[str], seed: int) -> dict[str, str]:
    """Clave provisional -> `transfer_id`. Se barajan con la semilla y el id sale de `f"{seed}:t:{i}"`.

    El índice `i` no guarda relación con la tipología, la instancia ni el orden de generación; el id no se
    puede reconstruir a partir de una clave que contenga el nombre de una tipología.
    """
    order = list(keys)
    random.Random(f"amlgen:{seed}:ids").shuffle(order)
    return {key: str(uuid.uuid5(NS, f"{seed}:t:{i}")) for i, key in enumerate(order)}


def generate(cfg: dict[str, Any], seed: int) -> Generated:
    b = _Builder(cfg, seed)
    near = _background(b)
    truth = [*_structuring(b), *_country_risk(b), *_fan_in(b), *_fast_pass(b)]
    ids = opaque_ids([t["transfer_id"] for t in b.transfers], seed)
    for t in b.transfers:
        t["transfer_id"] = ids[t["transfer_id"]]
    for g in truth:
        g["transfer_ids"] = [ids[k] for k in g["transfer_ids"]]
    b.transfers.sort(key=lambda t: (t["occurred_at"], t["transfer_id"]))
    truth.sort(key=lambda g: (TYPOLOGIES.index(g["typology"]), g["instance_id"]))
    return Generated(b.transfers, truth, near, b.accounts)
