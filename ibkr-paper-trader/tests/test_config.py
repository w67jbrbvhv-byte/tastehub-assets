"""Config validation — chiefly the guards that keep this away from a live account."""

import pytest
from pydantic import ValidationError

from trader.config import Config, IbkrConfig, Instrument


def base(**kwargs):
    defaults = dict(benchmark="AAA", universe=[Instrument(symbol="AAA")])
    return Config(**{**defaults, **kwargs})


@pytest.mark.parametrize("port", [4001, 7496])
def test_live_ports_are_refused(port):
    with pytest.raises(ValidationError, match="LIVE"):
        IbkrConfig(port=port)


@pytest.mark.parametrize("port", [4002, 7497])
def test_paper_ports_are_accepted(port):
    assert IbkrConfig(port=port).port == port


def test_unknown_port_is_refused():
    with pytest.raises(ValidationError, match="not a known IBKR paper port"):
        IbkrConfig(port=8080)


def test_mode_must_be_paper():
    with pytest.raises(ValidationError):
        base(mode="live")


def test_benchmark_must_be_in_the_universe():
    with pytest.raises(ValidationError, match="not in the universe"):
        Config(benchmark="ZZZ", universe=[Instrument(symbol="AAA")])


def test_empty_universe_is_refused():
    with pytest.raises(ValidationError, match="universe is empty"):
        Config(benchmark="AAA", universe=[])


def test_duplicate_symbols_are_refused():
    with pytest.raises(ValidationError, match="duplicate symbols"):
        Config(
            benchmark="AAA",
            universe=[Instrument(symbol="AAA"), Instrument(symbol="AAA")],
        )


def test_example_config_is_valid():
    from pathlib import Path

    from trader.config import load_config

    config = load_config(Path(__file__).resolve().parents[1] / "config.example.yaml")
    assert config.mode == "paper"
    assert config.benchmark in config.symbols
    assert config.ibkr.port == 4002
