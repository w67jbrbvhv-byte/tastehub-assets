"""Tests for the data sanity guard.

The guard exists because a bad print and a real collapse are indistinguishable
at the moment they arrive. Both must stop the run.
"""

from datetime import date, timedelta

import pytest

from trader.config import Config, CrashLadderConfig, DataGuardConfig, Instrument
from trader.dataguard import inspect
from trader.marketdata import build_snapshot

TODAY = date(2026, 9, 11)


def make_config(**guard) -> Config:
    return Config(
        benchmark="AAA",
        universe=[Instrument(symbol="AAA"), Instrument(symbol="BBB")],
        crash_ladder=CrashLadderConfig(enabled=False, reserve_pct=0.0, tranches=[]),
        data_guard=DataGuardConfig(**guard),
    )


def series(values, last_date=TODAY):
    return values, last_date.strftime("%Y%m%d")


def snaps(aaa_closes, bbb_closes=None, last_date=TODAY):
    # `is None`, not `or`: an empty list is a legitimate input here.
    bbb_closes = [50.0] * 300 if bbb_closes is None else bbb_closes
    return {
        "AAA": build_snapshot("AAA", "A", aaa_closes, last_date.strftime("%Y%m%d")),
        "BBB": build_snapshot("BBB", "B", bbb_closes, last_date.strftime("%Y%m%d")),
    }, {"AAA": aaa_closes, "BBB": bbb_closes}


def test_clean_data_passes():
    s, c = snaps([100.0] * 300)
    verdict = inspect(s, c, make_config(), TODAY)
    assert verdict.ok and not verdict.fatal and not verdict.warnings


def test_a_normal_bad_day_still_passes():
    """A 6% fall is a bad day, not a data error. The guard must not cry wolf."""
    s, c = snaps([100.0] * 299 + [94.0])
    assert inspect(s, c, make_config(), TODAY).ok


def test_an_implausible_one_day_move_stops_everything():
    s, c = snaps([100.0] * 299 + [40.0])
    verdict = inspect(s, c, make_config(), TODAY)
    assert not verdict.ok
    assert any("sanity limit" in m for m in verdict.fatal)


def test_a_price_far_from_the_recent_median_stops_everything():
    # Drifts up gradually then prints far away: the daily-move check alone
    # might miss a staircase, the median check catches it.
    closes = [100.0] * 296 + [100.0, 120.0, 145.0, 175.0]
    s, c = snaps(closes)
    verdict = inspect(s, c, make_config(max_daily_move_pct=60.0), TODAY)
    assert not verdict.ok


def test_stale_data_stops_the_run():
    s, c = snaps([100.0] * 300, last_date=TODAY - timedelta(days=12))
    verdict = inspect(s, c, make_config(), TODAY)
    assert not verdict.ok
    assert any("days old" in m for m in verdict.fatal)


def test_thin_history_on_the_benchmark_is_fatal():
    s, c = snaps([100.0] * 20)
    verdict = inspect(s, c, make_config(), TODAY)
    assert not verdict.ok
    assert any("bars" in m for m in verdict.fatal)


def test_thin_history_on_a_minor_name_only_removes_that_name():
    s, c = snaps([100.0] * 300, bbb_closes=[50.0] * 20)
    verdict = inspect(s, c, make_config(), TODAY)
    assert verdict.ok
    assert verdict.unusable == {"BBB"}
    assert verdict.warnings


def test_a_dead_instrument_is_reported_not_ignored():
    s, c = snaps([100.0] * 300, bbb_closes=[])
    verdict = inspect(s, c, make_config(), TODAY)
    assert verdict.ok  # BBB is not critical
    assert "BBB" in verdict.unusable


def test_a_dead_benchmark_stops_the_run():
    s, c = snaps([])
    verdict = inspect(s, c, make_config(), TODAY)
    assert not verdict.ok


def test_the_ladder_target_counts_as_critical():
    """Even if it is not the benchmark, the thing the ladder buys must be sound."""
    config = Config(
        benchmark="AAA",
        universe=[Instrument(symbol="AAA"), Instrument(symbol="BBB")],
        crash_ladder=CrashLadderConfig(target="BBB"),
    )
    s, c = snaps([100.0] * 300, bbb_closes=[50.0] * 10)
    verdict = inspect(s, c, config, TODAY)
    assert not verdict.ok
