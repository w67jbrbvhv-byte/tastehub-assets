"""The crash ladder: a pre-committed, mechanical response to a severe fall.

Two rules govern everything in this file.

**No model is consulted.** Not the strategist, not the trader, not at any point.
An LLM asked "is this the bottom?" in the middle of a crash will produce
articulate narrative and no information. The rungs, the sizes and the instrument
are decided in config.yaml in advance, and the only thing that happens during
the event is arithmetic. The value of the plan is precisely that it was made
when you were calm.

**It is not blocked by the drawdown halt.** The halt exists to stop
narrative-driven trading when something has gone wrong. The ladder is the
opposite of narrative-driven trading, and a 20% NAV drawdown is roughly when
the second rung fires — a halt that stopped it would disable the plan at the
exact moment it was written for.

The trigger is the *benchmark's* drawdown from its own trailing 252-day high,
not the portfolio's. You want to buy because the market fell, not because you
did badly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Config
from .models import InstrumentSnapshot

log = logging.getLogger(__name__)


@dataclass
class LadderState:
    """Persisted between runs. `fired` is the set of rung indices already used."""

    fired: set[int] = field(default_factory=set)
    reserve_base: float = 0.0          # NAV snapshot at the first trigger of this cycle
    carried_notional: float = 0.0      # unfilled remainder from earlier runs
    cycle_low_drawdown: float = 0.0    # worst benchmark drawdown seen this cycle

    @property
    def armed(self) -> bool:
        return bool(self.fired) or self.carried_notional > 0


@dataclass
class LadderAction:
    """What the ladder wants done this run. `notional` of 0 means nothing."""

    notional: float = 0.0
    symbol: str = ""
    drawdown_pct: float = 0.0
    rungs: list[int] = field(default_factory=list)
    note: str = ""
    new_state: LadderState = field(default_factory=LadderState)

    @property
    def acts(self) -> bool:
        return self.notional > 0


def reserve_required(nav: float, state: LadderState, config: Config) -> float:
    """Cash the strategy may not touch.

    Shrinks as rungs fire: once a tranche has been deployed, the cash it
    represented is in the market and no longer needs protecting.
    """
    ladder = config.crash_ladder
    if not ladder.enabled or ladder.reserve_pct <= 0:
        return 0.0
    deployed_share = sum(
        ladder.tranches[i].deploy_pct for i in state.fired if i < len(ladder.tranches)
    )
    remaining = max(0.0, 1.0 - deployed_share / 100.0)
    return nav * ladder.reserve_pct / 100.0 * remaining


def benchmark_drawdown_pct(snapshot: InstrumentSnapshot) -> float:
    """Positive number: how far below its 252-day high the benchmark sits."""
    return max(0.0, -snapshot.pct_from_252d_high)


def evaluate(
    state: LadderState,
    benchmark: InstrumentSnapshot,
    nav: float,
    cash: float,
    config: Config,
) -> LadderAction:
    """Decide this run's ladder deployment. Pure: no I/O, no model, no clock."""
    ladder = config.crash_ladder
    new_state = LadderState(
        fired=set(state.fired),
        reserve_base=state.reserve_base,
        carried_notional=state.carried_notional,
        cycle_low_drawdown=state.cycle_low_drawdown,
    )

    if not ladder.enabled or not ladder.tranches:
        return LadderAction(new_state=new_state, note="ladder disabled")

    drawdown = benchmark_drawdown_pct(benchmark)
    new_state.cycle_low_drawdown = max(state.cycle_low_drawdown, drawdown)

    # --- recovery: the cycle is over, re-arm every rung ---------------------
    if state.armed and drawdown <= ladder.rearm_within_pct:
        log.info("benchmark recovered to %.1f%% below its high; ladder re-armed", drawdown)
        return LadderAction(
            drawdown_pct=drawdown,
            note=(
                f"benchmark recovered to {drawdown:.1f}% below its high; "
                f"{len(state.fired)} rung(s) re-armed for the next cycle"
            ),
            new_state=LadderState(),
        )

    # --- which rungs are newly triggered? -----------------------------------
    triggered = [
        i
        for i, tranche in enumerate(ladder.tranches)
        if drawdown >= tranche.drawdown_pct and i not in state.fired
    ]

    # Snapshot the base at the first trigger of a cycle, so later rungs are
    # sized off the reserve as it stood before the fall, not off a shrunken NAV.
    if triggered and not state.armed:
        new_state.reserve_base = nav
    base = new_state.reserve_base or nav

    notional = state.carried_notional
    for i in triggered:
        tranche = ladder.tranches[i]
        notional += base * ladder.reserve_pct / 100.0 * tranche.deploy_pct / 100.0
        new_state.fired.add(i)

    if notional <= 0:
        return LadderAction(
            drawdown_pct=drawdown,
            new_state=new_state,
            note=f"benchmark {drawdown:.1f}% below its high; no rung triggered",
        )

    # --- never spend below the cash floor, crash or no crash ----------------
    floor = nav * config.risk.min_cash_pct / 100.0
    spendable = max(0.0, cash - floor)
    deployable = min(notional, spendable)
    unfilled = notional - deployable
    new_state.carried_notional = unfilled if ladder.carry_unfilled else 0.0

    note_parts = [f"benchmark {drawdown:.1f}% below its 252-day high"]
    if triggered:
        rungs = ", ".join(f"-{ladder.tranches[i].drawdown_pct:.0f}%" for i in triggered)
        note_parts.append(f"rung(s) {rungs} triggered")
    if state.carried_notional > 0:
        note_parts.append(f"{state.carried_notional:,.0f} carried from earlier runs")
    if unfilled > 0:
        note_parts.append(f"{unfilled:,.0f} held back by the cash floor, carried forward")

    return LadderAction(
        notional=deployable,
        symbol=config.ladder_target,
        drawdown_pct=drawdown,
        rungs=triggered,
        note="; ".join(note_parts),
        new_state=new_state,
    )


def size_order(action: LadderAction, price: float, config: Config) -> tuple[int, float]:
    """Turn a notional into (whole shares, limit price). (0, 0.0) if not viable."""
    if not action.acts or price <= 0:
        return 0, 0.0
    limit = round(
        price * (1 + config.crash_ladder.limit_offset_bps / 10_000.0),
        config.risk.price_decimals,
    )
    quantity = int(action.notional // limit)
    if quantity <= 0 or quantity * limit < config.risk.min_order_value:
        return 0, 0.0
    return quantity, limit


def check_position_cap(
    symbol: str, notional: float, position_value: float, nav: float, config: Config
) -> str:
    """The one limit the ladder still respects. Returns '' if within it."""
    cap = nav * config.crash_ladder.max_position_pct / 100.0
    if position_value + notional > cap:
        return (
            f"would take {symbol} to {position_value + notional:,.2f}, above the ladder's "
            f"{cap:,.2f} cap ({config.crash_ladder.max_position_pct:.0f}% of NAV)"
        )
    return ""


def rebuild_advice(state: LadderState, benchmark: InstrumentSnapshot, config: Config) -> str:
    """Reserves do not rebuild themselves.

    Selling back into a recovery has tax and timing consequences that belong to
    a person, not to a cron job. So this reports, and you decide.
    """
    if not state.fired:
        return ""
    drawdown = benchmark_drawdown_pct(benchmark)
    deployed = sum(
        config.crash_ladder.tranches[i].deploy_pct
        for i in state.fired
        if i < len(config.crash_ladder.tranches)
    )
    if drawdown > config.crash_ladder.rearm_within_pct:
        return (
            f"{deployed:.0f}% of the reserve is deployed and the benchmark is still "
            f"{drawdown:.1f}% below its high. Hold. Do not rebuild into a falling market."
        )
    return (
        f"{deployed:.0f}% of the reserve is deployed and the benchmark has recovered to "
        f"{drawdown:.1f}% below its high. The ladder re-arms on the next run, but the "
        f"reserve does not refill itself — decide whether to sell back toward the "
        f"{config.crash_ladder.reserve_pct:.0f}% target, and do it deliberately."
    )
