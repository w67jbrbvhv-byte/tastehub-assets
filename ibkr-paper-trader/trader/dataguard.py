"""Refusing to act on data you do not trust.

During a fast market, a bad print and a real collapse look identical at the
moment they arrive. The right response to both is the same: stop, and fetch a
human. Losing one day costs almost nothing — the ladder deploys over weeks, and
no daily strategy is worth a day. Trading on a corrupt price can cost everything.

So a suspect snapshot stops *everything* for that run, the crash ladder included.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import Config
from .models import InstrumentSnapshot


@dataclass
class DataVerdict:
    ok: bool
    fatal: list[str] = field(default_factory=list)      # stop the whole run
    warnings: list[str] = field(default_factory=list)   # note it, carry on
    unusable: set[str] = field(default_factory=set)     # symbols to leave alone

    @property
    def summary(self) -> str:
        lines = [f"DATA REFUSED: {m}" for m in self.fatal]
        lines += [f"data warning: {m}" for m in self.warnings]
        return "\n".join(lines)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def inspect(
    snapshots: dict[str, InstrumentSnapshot],
    recent_closes: dict[str, list[float]],
    config: Config,
    today: date | None = None,
) -> DataVerdict:
    """Screen a set of snapshots before anything is decided on them."""
    guard = config.data_guard
    today = today or date.today()
    verdict = DataVerdict(ok=True)
    critical_symbols = {config.benchmark, config.ladder_target}

    for symbol, snap in snapshots.items():
        is_critical = symbol in critical_symbols

        def fail(message: str, stop_the_run: bool = is_critical) -> None:
            if stop_the_run:
                verdict.fatal.append(message)
            else:
                verdict.warnings.append(message)
                verdict.unusable.add(symbol)

        if snap.last <= 0 or snap.bars_available == 0:
            fail(f"{symbol}: no usable price data")
            continue

        if snap.bars_available < guard.min_bars:
            fail(
                f"{symbol}: only {snap.bars_available} bars, below the "
                f"{guard.min_bars} minimum"
            )

        if snap.as_of is None:
            fail(f"{symbol}: the last bar carries no date")
        else:
            stale_days = (today - snap.as_of).days
            if stale_days > guard.max_stale_days:
                fail(
                    f"{symbol}: last bar is {stale_days} days old "
                    f"({snap.as_of.isoformat()}), above the {guard.max_stale_days}-day limit"
                )

        # A move this size is either a once-in-a-century event or a bad print.
        # Both want a human, so both stop the run.
        if abs(snap.ret_1d) > guard.max_daily_move_pct:
            verdict.fatal.append(
                f"{symbol}: one-day move of {snap.ret_1d:+.1f}% exceeds the "
                f"{guard.max_daily_move_pct:.0f}% sanity limit — either the data is "
                f"wrong or something extraordinary happened. Check by hand before trading."
            )

        closes = recent_closes.get(symbol) or []
        if len(closes) >= 5:
            median = _median(closes[-5:])
            if median > 0:
                deviation = abs(snap.last / median - 1.0) * 100.0
                if deviation > guard.max_median_deviation_pct:
                    verdict.fatal.append(
                        f"{symbol}: last close {snap.last:,.2f} is {deviation:.1f}% from the "
                        f"5-day median {median:,.2f}, above the "
                        f"{guard.max_median_deviation_pct:.0f}% limit"
                    )

    verdict.ok = not verdict.fatal
    return verdict
