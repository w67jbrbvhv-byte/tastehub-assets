"""Metric computation — these numbers are the model's entire view of the market."""

import pytest

from trader.marketdata import build_snapshot


def flat(n=300, value=100.0):
    return [value] * n


def test_flat_series_has_no_returns_and_no_vol():
    snap = build_snapshot("AAA", "Alpha", flat())
    assert snap.last == 100.0
    assert snap.ret_20d == pytest.approx(0.0)
    assert snap.vol_20d_annual == pytest.approx(0.0)
    assert snap.pct_vs_sma_200 == pytest.approx(0.0)


def test_returns_use_the_right_lookback():
    closes = list(range(1, 301))  # 1..300
    snap = build_snapshot("AAA", "Alpha", [float(c) for c in closes])
    assert snap.last == 300.0
    assert snap.ret_1d == pytest.approx((300 / 299 - 1) * 100)
    assert snap.ret_20d == pytest.approx((300 / 280 - 1) * 100)


def test_rising_series_sits_above_its_moving_averages():
    snap = build_snapshot("AAA", "Alpha", [100.0 * 1.001**i for i in range(300)])
    assert snap.pct_vs_sma_50 > 0
    assert snap.pct_vs_sma_200 > 0
    assert snap.pct_from_252d_high == pytest.approx(0.0, abs=1e-9)


def test_short_history_does_not_fabricate_long_averages():
    snap = build_snapshot("AAA", "Alpha", flat(30))
    assert snap.bars_available == 30
    assert snap.sma_200 == 0.0
    assert snap.pct_vs_sma_200 == 0.0
    assert snap.ret_252d == 0.0


def test_empty_history_is_survivable():
    snap = build_snapshot("AAA", "Alpha", [])
    assert snap.last == 0.0 and snap.bars_available == 0


def test_zero_and_none_closes_are_dropped():
    snap = build_snapshot("AAA", "Alpha", [0.0, None, 100.0, 101.0])
    assert snap.bars_available == 2
    assert snap.last == 101.0


def test_drawdown_from_the_52_week_high_is_negative():
    closes = [100.0] * 200 + [80.0]
    snap = build_snapshot("AAA", "Alpha", closes)
    assert snap.pct_from_252d_high == pytest.approx(-20.0)
