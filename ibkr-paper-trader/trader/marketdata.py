"""Turn daily bars into the compact numeric picture the models are given.

The models never see raw bars. They see returns, trend position, realised
volatility and distance from the 52-week high — computed here, deterministically,
so the same market produces the same numbers every run.
"""

from __future__ import annotations

import math
from datetime import date, datetime

from .models import InstrumentSnapshot

TRADING_DAYS = 252


def _pct_change(series: list[float], lookback: int) -> float:
    if len(series) <= lookback:
        return 0.0
    past = series[-1 - lookback]
    if past <= 0:
        return 0.0
    return (series[-1] / past - 1.0) * 100.0


def _sma(series: list[float], window: int) -> float:
    if len(series) < window or window <= 0:
        return 0.0
    return sum(series[-window:]) / window


def _annualised_vol(closes: list[float], window: int = 20) -> float:
    if len(closes) < window + 1:
        return 0.0
    rets = [
        closes[i] / closes[i - 1] - 1.0
        for i in range(len(closes) - window, len(closes))
        if closes[i - 1] > 0
    ]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    variance = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(variance) * math.sqrt(TRADING_DAYS) * 100.0


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(value[:10].replace("-", "") if fmt == "%Y%m%d" else value[:10], fmt).date()
            except ValueError:
                continue
    return None


def build_snapshot(
    symbol: str,
    name: str,
    closes: list[float],
    last_date: object = None,
) -> InstrumentSnapshot:
    """Compute the metric set from a list of daily closes, oldest first."""
    closes = [c for c in closes if c and c > 0]
    if not closes:
        return InstrumentSnapshot(
            symbol=symbol, name=name, last=0.0, as_of=None,
            ret_1d=0.0, ret_5d=0.0, ret_20d=0.0, ret_60d=0.0, ret_252d=0.0,
            sma_20=0.0, sma_50=0.0, sma_200=0.0,
            pct_vs_sma_50=0.0, pct_vs_sma_200=0.0, vol_20d_annual=0.0,
            high_252d=0.0, low_252d=0.0, pct_from_252d_high=0.0, bars_available=0,
        )

    last = closes[-1]
    window = closes[-TRADING_DAYS:] if len(closes) >= TRADING_DAYS else closes
    high_252 = max(window)
    low_252 = min(window)
    sma_50 = _sma(closes, 50)
    sma_200 = _sma(closes, 200)

    return InstrumentSnapshot(
        symbol=symbol,
        name=name,
        last=last,
        as_of=_as_date(last_date),
        ret_1d=_pct_change(closes, 1),
        ret_5d=_pct_change(closes, 5),
        ret_20d=_pct_change(closes, 20),
        ret_60d=_pct_change(closes, 60),
        ret_252d=_pct_change(closes, TRADING_DAYS),
        sma_20=_sma(closes, 20),
        sma_50=sma_50,
        sma_200=sma_200,
        pct_vs_sma_50=(last / sma_50 - 1.0) * 100.0 if sma_50 > 0 else 0.0,
        pct_vs_sma_200=(last / sma_200 - 1.0) * 100.0 if sma_200 > 0 else 0.0,
        vol_20d_annual=_annualised_vol(closes),
        high_252d=high_252,
        low_252d=low_252,
        pct_from_252d_high=(last / high_252 - 1.0) * 100.0 if high_252 > 0 else 0.0,
        bars_available=len(closes),
    )
