from __future__ import annotations

import pytest

from verabank_sim import cli
from verabank_sim.rail.cli import default_bootstrap, parse_delay


def test_parse_delay():
    assert parse_delay("20:200") == (20, 200)
    for bad in ("200:20", "20", "a:b", "20:200:1"):
        with pytest.raises(Exception):
            parse_delay(bad)


def test_el_despachador_conoce_rail_y_chaos_y_aplica_defaults():
    args = cli.build_parser().parse_args(["rail"])
    assert (args.reject_rate, args.delay_ms, args.dup_rate, args.dup_mode) == (0.0, (20, 200), 0.0, "same-id")
    assert (args.silent_rate, args.lost_rate, args.seed) == (0.0, 0.0, 42)
    assert str(args.state_db).endswith("simulator/.state/rail.db")
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["replay"])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


@pytest.mark.parametrize("flags", [["--reject-rate", "1.5"], ["--delay-ms", "9:1"], ["--dup-mode", "x"]])
def test_banderas_invalidas(flags):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["rail", *flags])


def test_variables_de_entorno_como_defaults(monkeypatch):
    monkeypatch.setenv("RAIL_REJECT_RATE", "0.25")
    monkeypatch.setenv("RAIL_DELAY_MS", "5:10")
    monkeypatch.setenv("RAIL_SEED", "9")
    args = cli.build_parser().parse_args(["rail"])
    assert (args.reject_rate, args.delay_ms, args.seed) == (0.25, (5, 10), 9)


def test_bootstrap_desde_entorno(monkeypatch):
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    monkeypatch.setenv("KAFKA_EXTERNAL_PORT", "31000")
    assert default_bootstrap() == "localhost:31000"
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker:1")
    assert default_bootstrap() == "broker:1"
