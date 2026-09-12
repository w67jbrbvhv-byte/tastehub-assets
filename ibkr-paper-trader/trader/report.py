"""Performance reporting: the agent against simply holding the benchmark.

If the agent is not beating a buy-and-hold of the benchmark, on a risk-adjusted
basis, it is not adding anything — and that comparison should be visible every
time you look, not something you compute when you feel like it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config
from .journal import Journal
from .models import InstrumentSnapshot
from .tail import LadderState, benchmark_drawdown_pct

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


def estimated_friction(journal: Journal, config: Config) -> tuple[float, int]:
    """What the paper record did not charge: commission, half-spread, impact.

    Paper fills are free and instant. A record that ignores that is not evidence
    of anything, so every comparison below is shown after this haircut as well.
    """
    total = 0.0
    count = 0
    for row in journal.recent_orders(5000):
        if row["outcome"] != "placed":
            continue
        count += 1
        total += config.friction.cost_of(float(row["notional"] or 0.0))
    return total, count


def performance_text(journal: Journal, config: Config | None = None) -> str:
    agent, benchmark, days = performance(journal)
    if agent is None:
        return f"Only {days} equity point(s) recorded — not enough for a comparison yet."

    lines = [f"{days} trading days recorded.", "", agent.summary]

    net_return = agent.total_return_pct
    if config is not None:
        friction, n_orders = estimated_friction(journal, config)
        rows = journal.equity_curve()
        start_nav = float(rows[0]["nav"]) if rows else 0.0
        if friction > 0 and start_nav > 0:
            drag_pct = friction / start_nav * 100.0
            net_return = agent.total_return_pct - drag_pct
            lines.append(
                f"{'Agent, after friction':<22} {net_return:>8.2f}%  "
                f"({n_orders} orders, est. {friction:,.0f} "
                f"{config.base_currency} of spread and commission paper did not charge)"
            )

    if benchmark:
        lines.append(benchmark.summary)
        gap = net_return - benchmark.total_return_pct
        verdict = "ahead of" if gap > 0 else "behind"
        lines += [
            "",
            f"After friction the agent is {abs(gap):.2f} percentage points {verdict} "
            f"buy-and-hold.",
        ]
        if agent.max_drawdown_pct > benchmark.max_drawdown_pct:
            lines.append(
                f"It also took a deeper drawdown than the benchmark "
                f"({agent.max_drawdown_pct:.1f}% against {benchmark.max_drawdown_pct:.1f}%) — "
                f"so any excess return was bought with extra risk, not skill."
            )
        if days < 250:
            lines.append(
                f"At {days} days this is noise, not evidence. The graduation gate in "
                f"PLAYBOOK.md needs 250+."
            )
    else:
        lines.append("(No benchmark prices recorded yet.)")

    all_orders = journal.recent_orders(5000)
    placed = [o for o in all_orders if o["outcome"] == "placed"]
    rejected = [o for o in all_orders if o["outcome"] == "rejected"]
    lines += [
        "",
        f"Orders placed: {len(placed)}. Orders rejected by the risk engine: {len(rejected)}.",
    ]
    proposed = len(placed) + len(rejected)
    if proposed >= 10:
        rate = len(rejected) / proposed * 100.0
        if rate > 30:
            lines.append(
                f"A {rate:.0f}% rejection rate means the strategy and your limits "
                f"disagree. One of them is wrong — decide which before going live."
            )
    if rejected:
        lines.append("Most recent rejections:")
        for row in rejected[:5]:
            lines.append(
                f"  {row['trade_date']} {row['action']} {row['quantity']} {row['symbol']}"
                f" — {row['reason']}"
            )
    return "\n".join(lines)


def ladder_status_text(
    config: Config, state: LadderState, benchmark: InstrumentSnapshot | None
) -> str:
    ladder = config.crash_ladder
    if not ladder.enabled:
        return "Crash ladder: disabled."

    lines = [
        f"Crash ladder: target {config.ladder_target}, reserve "
        f"{ladder.reserve_pct:.0f}% of NAV."
    ]
    if benchmark is not None:
        drawdown = benchmark_drawdown_pct(benchmark)
        lines.append(
            f"  {config.benchmark} is {drawdown:.1f}% below its 252-day high."
        )
    else:
        drawdown = None

    for i, tranche in enumerate(ladder.tranches):
        if i in state.fired:
            mark, note = "FIRED  ", "deployed"
        elif drawdown is not None:
            mark = "armed  "
            note = f"{tranche.drawdown_pct - drawdown:.1f} points away"
        else:
            mark, note = "armed  ", "waiting"
        lines.append(
            f"  {mark} -{tranche.drawdown_pct:>4.0f}%  deploy {tranche.deploy_pct:>3.0f}% "
            f"of reserve   ({note})"
        )

    if state.carried_notional > 0:
        lines.append(
            f"  {state.carried_notional:,.2f} carried forward, unfilled by the cash floor."
        )
    if benchmark is not None:
        from .tail import rebuild_advice

        advice = rebuild_advice(state, benchmark, config)
        if advice:
            lines += ["", f"  {advice}"]
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
