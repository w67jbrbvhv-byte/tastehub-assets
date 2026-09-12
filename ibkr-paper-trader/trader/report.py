"""Performance reporting: the agent against simply holding the benchmark.

If the agent is not beating a buy-and-hold of the benchmark, on a risk-adjusted
basis, it is not adding anything — and that comparison should be visible every
time you look, not something you compute when you feel like it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .journal import Journal

TRADING_DAYS = 252


@dataclass
class Series:
    label: str
    start: float
    end: float
    total_return_pct: float
    max_drawdown_pct: float
    annual_vol_pct: float

    @property
    def summary(self) -> str:
        return (
            f"{self.label:<22} {self.total_return_pct:>8.2f}%  "
            f"max DD {self.max_drawdown_pct:>6.2f}%  vol {self.annual_vol_pct:>6.2f}%"
        )


def _max_drawdown_pct(values: list[float]) -> float:
    peak = float("-inf")
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak * 100.0)
    return worst


def _annual_vol_pct(values: list[float]) -> float:
    rets = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] > 0]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS) * 100.0


def _series(label: str, values: list[float]) -> Series | None:
    values = [v for v in values if v and v > 0]
    if len(values) < 2:
        return None
    return Series(
        label=label,
        start=values[0],
        end=values[-1],
        total_return_pct=(values[-1] / values[0] - 1.0) * 100.0,
        max_drawdown_pct=_max_drawdown_pct(values),
        annual_vol_pct=_annual_vol_pct(values),
    )


def performance(journal: Journal) -> tuple[Series | None, Series | None, int]:
    """Returns (agent, benchmark-held-instead, number of days recorded)."""
    rows = journal.equity_curve()
    if len(rows) < 2:
        return None, None, len(rows)

    navs = [float(r["nav"]) for r in rows]
    agent = _series("Agent", navs)

    bench_prices = [r["benchmark_price"] for r in rows]
    if all(p for p in bench_prices):
        start_nav = navs[0]
        start_price = float(bench_prices[0])
        bench_navs = [start_nav * float(p) / start_price for p in bench_prices]
        benchmark = _series("Benchmark buy & hold", bench_navs)
    else:
        benchmark = None

    return agent, benchmark, len(rows)


def performance_text(journal: Journal) -> str:
    agent, benchmark, days = performance(journal)
    if agent is None:
        return f"Only {days} equity point(s) recorded — not enough for a comparison yet."

    lines = [f"{days} trading days recorded.", "", agent.summary]
    if benchmark:
        lines.append(benchmark.summary)
        gap = agent.total_return_pct - benchmark.total_return_pct
        verdict = "ahead of" if gap > 0 else "behind"
        lines += [
            "",
            f"The agent is {abs(gap):.2f} percentage points {verdict} buy-and-hold.",
        ]
        if days < 120:
            lines.append(
                f"At {days} days this is noise, not evidence. Do not act on it."
            )
    else:
        lines.append("(No benchmark prices recorded yet.)")

    placed = [o for o in journal.recent_orders(500) if o["outcome"] == "placed"]
    rejected = [o for o in journal.recent_orders(500) if o["outcome"] == "rejected"]
    lines += [
        "",
        f"Orders placed: {len(placed)}. Orders rejected by the risk engine: {len(rejected)}.",
    ]
    if rejected:
        lines.append("Most recent rejections:")
        for row in rejected[:5]:
            lines.append(
                f"  {row['trade_date']} {row['action']} {row['quantity']} {row['symbol']}"
                f" — {row['reason']}"
            )
    return "\n".join(lines)


def order_history_text(journal: Journal, limit: int = 30) -> str:
    rows = journal.recent_orders(limit)
    if not rows:
        return ""
    lines = []
    for r in reversed(rows):
        status = r["outcome"]
        if status == "placed":
            detail = f"{r['ib_status']}, filled {r['filled']:g} @ {r['avg_fill_price']:.2f}"
        else:
            detail = r["reason"]
        lines.append(
            f"{r['trade_date']} {r['action']:<4} {r['quantity']:>5} {r['symbol']:<6} "
            f"@ {r['limit_price']:.2f} [{r['rule_id']}] {status}: {detail}"
        )
    return "\n".join(lines)
