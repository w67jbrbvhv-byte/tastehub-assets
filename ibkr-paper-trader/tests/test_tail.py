"""Tests for the crash ladder.

This code runs perhaps once a decade, under conditions nobody will be calm
enough to debug in. That makes it exactly the code that must be tested now.
"""

import pytest

from trader.config import Config, CrashLadderConfig, Instrument, LadderTranche, RiskConfig
from trader.marketdata import build_snapshot
from trader.tail import (
    LadderState,
    benchmark_drawdown_pct,
    check_position_cap,
    evaluate,
    rebuild_advice,
    reserve_required,
    size_order,
)

TRANCHES = [
    LadderTranche(drawdown_pct=15.0, deploy_pct=20.0),
    LadderTranche(drawdown_pct=25.0, deploy_pct=25.0),
    LadderTranche(drawdown_pct=35.0, deploy_pct=30.0),
    LadderTranche(drawdown_pct=50.0, deploy_pct=25.0),
]


def make_config(**ladder_overrides) -> Config:
    ladder = dict(
        enabled=True,
        reserve_pct=25.0,
        target="AAA",
        rearm_within_pct=5.0,
        max_position_pct=60.0,
        limit_offset_bps=150.0,
        tranches=list(TRANCHES),
    )
    ladder.update(ladder_overrides)
    return Config(
        benchmark="AAA",
        universe=[Instrument(symbol="AAA"), Instrument(symbol="BBB")],
        risk=RiskConfig(min_cash_pct=5.0, min_order_value=250.0, price_decimals=2),
        crash_ladder=CrashLadderConfig(**ladder),
    )


def benchmark_at(drawdown_pct: float, peak: float = 100.0):
    """A 300-bar series whose last close sits `drawdown_pct` below its high."""
    last = peak * (1 - drawdown_pct / 100.0)
    return build_snapshot("AAA", "Alpha", [peak] * 299 + [last], "20260911")


# -- the trigger ------------------------------------------------------------


def test_drawdown_is_measured_from_the_benchmark_high():
    assert benchmark_drawdown_pct(benchmark_at(30.0)) == pytest.approx(30.0)
    assert benchmark_drawdown_pct(benchmark_at(0.0)) == pytest.approx(0.0)


def test_calm_market_deploys_nothing():
    action = evaluate(LadderState(), benchmark_at(3.0), 100_000, 100_000, make_config())
    assert not action.acts
    assert action.new_state.fired == set()


def test_shallow_dip_below_the_first_rung_deploys_nothing():
    action = evaluate(LadderState(), benchmark_at(14.9), 100_000, 100_000, make_config())
    assert not action.acts


def test_first_rung_fires_and_sizes_off_the_reserve():
    # Reserve is 25% of 100k = 25,000. Rung one deploys 20% of that = 5,000.
    action = evaluate(LadderState(), benchmark_at(16.0), 100_000, 100_000, make_config())
    assert action.acts
    assert action.notional == pytest.approx(5_000.0)
    assert action.new_state.fired == {0}
    assert action.symbol == "AAA"


def test_a_rung_fires_only_once():
    state = LadderState(fired={0}, reserve_base=100_000)
    action = evaluate(state, benchmark_at(18.0), 100_000, 100_000, make_config())
    assert not action.acts
    assert action.new_state.fired == {0}


def test_a_sudden_deep_fall_fires_every_rung_it_passes():
    # Straight to -40%: rungs at 15, 25 and 35 all trigger at once.
    action = evaluate(LadderState(), benchmark_at(40.0), 100_000, 100_000, make_config())
    assert action.new_state.fired == {0, 1, 2}
    # 20 + 25 + 30 = 75% of the 25,000 reserve
    assert action.notional == pytest.approx(18_750.0)


def test_rungs_fire_progressively_as_the_fall_deepens():
    config = make_config()
    state = LadderState()
    deployed = []
    for drawdown in (16.0, 26.0, 36.0, 51.0):
        action = evaluate(state, benchmark_at(drawdown), 100_000, 100_000, config)
        deployed.append(action.notional)
        state = action.new_state
    assert deployed == pytest.approx([5_000.0, 6_250.0, 7_500.0, 6_250.0])
    assert sum(deployed) == pytest.approx(25_000.0)  # the whole reserve
    assert state.fired == {0, 1, 2, 3}


# -- the sizing base --------------------------------------------------------


def test_later_rungs_size_off_the_pre_crash_nav_not_the_shrunken_one():
    """Otherwise each rung buys less than the last, which is backwards: deeper
    falls should buy more, not less."""
    config = make_config()
    first = evaluate(LadderState(), benchmark_at(16.0), 100_000, 100_000, config)
    assert first.new_state.reserve_base == 100_000

    # NAV has since fallen to 70k. The second rung must still size off 100k.
    second = evaluate(first.new_state, benchmark_at(26.0), 70_000, 60_000, config)
    assert second.notional == pytest.approx(6_250.0)  # 25% of 100k x 25%


# -- the cash floor is absolute --------------------------------------------


def test_the_cash_floor_holds_even_in_a_crash():
    # Cash 8,000, NAV 100,000, floor 5% = 5,000. At most 3,000 is spendable.
    action = evaluate(LadderState(), benchmark_at(40.0), 100_000, 8_000, make_config())
    assert action.notional == pytest.approx(3_000.0)
    assert action.new_state.carried_notional == pytest.approx(15_750.0)


def test_the_unfilled_remainder_carries_to_the_next_run():
    config = make_config()
    first = evaluate(LadderState(), benchmark_at(40.0), 100_000, 8_000, config)
    carried = first.new_state.carried_notional
    assert carried > 0

    # Cash has since arrived. No new rung fires, but the carry still deploys.
    second = evaluate(first.new_state, benchmark_at(40.0), 100_000, 100_000, config)
    assert second.acts
    assert second.notional == pytest.approx(carried)
    assert second.new_state.carried_notional == pytest.approx(0.0)


def test_no_cash_deploys_nothing_and_carries_everything():
    action = evaluate(LadderState(), benchmark_at(40.0), 100_000, 5_000, make_config())
    assert not action.acts
    assert action.new_state.carried_notional == pytest.approx(18_750.0)


# -- re-arming --------------------------------------------------------------


def test_recovery_rearms_every_rung():
    state = LadderState(fired={0, 1}, reserve_base=100_000, cycle_low_drawdown=28.0)
    action = evaluate(state, benchmark_at(3.0), 120_000, 40_000, make_config())
    assert not action.acts
    assert action.new_state.fired == set()
    assert action.new_state.reserve_base == 0.0
    assert "re-armed" in action.note


def test_a_choppy_market_does_not_rearm_rungs():
    """Bouncing from -30% to -12% and back must not re-fire rung one."""
    config = make_config()
    state = LadderState(fired={0, 1}, reserve_base=100_000)

    bounce = evaluate(state, benchmark_at(12.0), 100_000, 50_000, config)
    assert not bounce.acts
    assert bounce.new_state.fired == {0, 1}  # still spent

    back_down = evaluate(bounce.new_state, benchmark_at(28.0), 100_000, 50_000, config)
    assert not back_down.acts  # rungs 0 and 1 are used; rung 2 is at -35%


def test_after_rearming_the_ladder_fires_again_in_the_next_cycle():
    config = make_config()
    spent = LadderState(fired={0, 1, 2, 3}, reserve_base=100_000)
    rearmed = evaluate(spent, benchmark_at(2.0), 150_000, 40_000, config).new_state
    assert rearmed.fired == set()

    action = evaluate(rearmed, benchmark_at(16.0), 150_000, 60_000, config)
    assert action.acts
    assert action.notional == pytest.approx(150_000 * 0.25 * 0.20)


# -- the reserve ------------------------------------------------------------


def test_the_full_reserve_is_ringfenced_before_any_rung_fires():
    assert reserve_required(100_000, LadderState(), make_config()) == pytest.approx(25_000)


def test_the_reserve_shrinks_as_rungs_are_spent():
    # Rungs 0 and 1 spent = 45% deployed, so 55% of the reserve is still held.
    state = LadderState(fired={0, 1})
    assert reserve_required(100_000, state, make_config()) == pytest.approx(13_750.0)


def test_a_fully_deployed_ladder_ringfences_nothing():
    state = LadderState(fired={0, 1, 2, 3})
    assert reserve_required(100_000, state, make_config()) == pytest.approx(0.0)


def test_a_disabled_ladder_ringfences_nothing():
    config = make_config(enabled=False)
    assert reserve_required(100_000, LadderState(), config) == 0.0


# -- order construction -----------------------------------------------------


def test_the_limit_is_set_well_through_the_last_price():
    """A 25bps limit does not fill when the spread is 200bps wide."""
    config = make_config()
    action = evaluate(LadderState(), benchmark_at(16.0), 100_000, 100_000, config)
    quantity, limit = size_order(action, 100.0, config)
    assert limit == pytest.approx(101.50)  # 150bps through
    assert quantity == 49                   # 5,000 // 101.50


def test_a_notional_too_small_to_buy_a_share_is_skipped():
    config = make_config()
    action = evaluate(LadderState(), benchmark_at(16.0), 100_000, 100_000, config)
    assert size_order(action, 1_000_000.0, config) == (0, 0.0)


def test_the_ladder_position_cap_is_wider_than_the_strategy_cap():
    config = make_config()
    # 60% of 100k = 60,000. Holding 50,000, adding 5,000 is fine.
    assert check_position_cap("AAA", 5_000, 50_000, 100_000, config) == ""
    # Adding 15,000 is not.
    assert "cap" in check_position_cap("AAA", 15_000, 50_000, 100_000, config)


# -- rebuild advice ---------------------------------------------------------


def test_no_rebuild_advice_before_anything_has_fired():
    assert rebuild_advice(LadderState(), benchmark_at(3.0), make_config()) == ""


def test_rebuild_advice_says_hold_while_the_market_is_still_down():
    advice = rebuild_advice(LadderState(fired={0, 1}), benchmark_at(30.0), make_config())
    assert "Hold" in advice and "falling market" in advice


def test_rebuild_advice_prompts_a_deliberate_decision_after_recovery():
    advice = rebuild_advice(LadderState(fired={0, 1}), benchmark_at(2.0), make_config())
    assert "deliberately" in advice


# -- config guards ----------------------------------------------------------


def test_tranches_must_ascend():
    with pytest.raises(Exception, match="increasing drawdown_pct"):
        CrashLadderConfig(
            tranches=[
                LadderTranche(drawdown_pct=30.0, deploy_pct=50.0),
                LadderTranche(drawdown_pct=15.0, deploy_pct=50.0),
            ]
        )


def test_tranches_cannot_deploy_more_than_the_reserve():
    with pytest.raises(Exception, match="maximum is 100"):
        CrashLadderConfig(
            tranches=[
                LadderTranche(drawdown_pct=15.0, deploy_pct=70.0),
                LadderTranche(drawdown_pct=30.0, deploy_pct=70.0),
            ]
        )


def test_a_reserve_with_no_tranches_is_refused():
    with pytest.raises(Exception, match="locked away"):
        Config(
            benchmark="AAA",
            universe=[Instrument(symbol="AAA")],
            crash_ladder=CrashLadderConfig(reserve_pct=25.0, tranches=[]),
        )


def test_the_ladder_target_must_be_tradable():
    with pytest.raises(Exception, match="not in the universe"):
        Config(
            benchmark="AAA",
            universe=[Instrument(symbol="AAA")],
            crash_ladder=CrashLadderConfig(target="ZZZ"),
        )
