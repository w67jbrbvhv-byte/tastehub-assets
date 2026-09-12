"""Tests for the risk engine.

These are the tests that matter. Everything else in this project can be wrong
and you lose some paper money; if these are wrong, the guarantees the README
makes are not true.
"""

import pytest

from trader.config import Config, Instrument, RiskConfig
from trader.models import PortfolioState, Position, ProposedOrder
from trader.risk import check_halt, default_limit_price, evaluate

RULES = {"R1", "R2"}


BASE_RISK = dict(
    max_position_pct=20.0,
    max_positions=3,
    min_cash_pct=5.0,
    max_trade_pct=10.0,
    max_orders_per_day=4,
    max_daily_turnover_pct=25.0,
    drawdown_halt_pct=20.0,
    min_order_value=250.0,
    limit_offset_bps=25.0,
    max_limit_deviation_pct=2.0,
)


def make_config(**risk_overrides) -> Config:
    return Config(
        benchmark="AAA",
        universe=[
            Instrument(symbol="AAA", name="Alpha"),
            Instrument(symbol="BBB", name="Beta"),
        ],
        risk=RiskConfig(**{**BASE_RISK, **risk_overrides}),
    )


def make_state(nav=100_000.0, cash=100_000.0, positions=None, peak=100_000.0, orders=0, turnover=0.0):
    return PortfolioState(
        nav=nav,
        cash=cash,
        positions=positions or {},
        peak_nav=peak,
        orders_today=orders,
        turnover_today=turnover,
    )


def position(symbol, qty, price):
    return Position(
        symbol=symbol, quantity=qty, market_price=price, market_value=qty * price,
        average_cost=price, unrealised_pnl=0.0,
    )


def order(symbol="AAA", action="BUY", qty=10, limit=0.0, rule="R1"):
    return ProposedOrder(
        symbol=symbol, action=action, quantity=qty, limit_price=limit,
        rule_id=rule, rationale="test",
    )


PRICES = {"AAA": 100.0, "BBB": 50.0}


def only(verdicts):
    assert len(verdicts) == 1
    return verdicts[0]


# -- the happy path ---------------------------------------------------------


def test_valid_buy_is_accepted():
    verdicts, halt = evaluate([order(qty=50)], make_state(), PRICES, make_config(), RULES)
    v = only(verdicts)
    assert halt == ""
    assert v.accepted, v.reason
    assert v.limit_price == pytest.approx(100.25)  # 25bps through last
    assert v.notional == pytest.approx(5012.5)


def test_default_limit_offsets_in_the_right_direction():
    config = make_config()
    assert default_limit_price("BUY", 100.0, config) > 100.0
    assert default_limit_price("SELL", 100.0, config) < 100.0


# -- universe, shape, rule citation ----------------------------------------


def test_symbol_outside_universe_is_rejected():
    v = only(evaluate([order(symbol="ZZZ")], make_state(), PRICES, make_config(), RULES)[0])
    assert not v.accepted and "universe" in v.reason


def test_unknown_rule_id_is_rejected():
    v = only(evaluate([order(rule="R99")], make_state(), PRICES, make_config(), RULES)[0])
    assert not v.accepted and "R99" in v.reason


def test_zero_and_negative_quantities_are_rejected():
    for qty in (0, -5):
        v = only(evaluate([order(qty=qty)], make_state(), PRICES, make_config(), RULES)[0])
        assert not v.accepted and "positive" in v.reason


def test_missing_price_is_rejected():
    v = only(evaluate([order()], make_state(), {"AAA": 0.0}, make_config(), RULES)[0])
    assert not v.accepted and "price" in v.reason


# -- long only --------------------------------------------------------------


def test_selling_more_than_held_is_rejected():
    state = make_state(positions={"AAA": position("AAA", 10, 100.0)})
    v = only(evaluate([order(action="SELL", qty=25)], state, PRICES, make_config(), RULES)[0])
    assert not v.accepted and "shorting is disabled" in v.reason


def test_selling_what_is_held_is_accepted():
    state = make_state(positions={"AAA": position("AAA", 30, 100.0)})
    v = only(evaluate([order(action="SELL", qty=30)], state, PRICES, make_config(), RULES)[0])
    assert v.accepted, v.reason


def test_sell_with_no_position_is_rejected():
    v = only(evaluate([order(action="SELL", qty=5)], make_state(), PRICES, make_config(), RULES)[0])
    assert not v.accepted


# -- sizing limits ----------------------------------------------------------


def test_order_above_single_trade_cap_is_rejected():
    # 10% of 100k = 10,000; 150 shares at ~100 = ~15,000
    v = only(evaluate([order(qty=150)], make_state(), PRICES, make_config(), RULES)[0])
    assert not v.accepted and "single-order cap" in v.reason


def test_dust_order_is_rejected():
    v = only(evaluate([order(qty=1)], make_state(), PRICES, make_config(), RULES)[0])
    assert not v.accepted and "minimum" in v.reason


def test_position_cap_counts_existing_holding():
    # Cap is 20% = 20,000. Already holding 18,000, buying another ~5,000.
    state = make_state(positions={"AAA": position("AAA", 180, 100.0)}, cash=82_000.0)
    v = only(evaluate([order(qty=50)], state, PRICES, make_config(), RULES)[0])
    assert not v.accepted and "per-position cap" in v.reason


def test_cash_floor_is_respected():
    # Cash 6,000 on 100k NAV; floor is 5% = 5,000. A 5,000 buy breaches it.
    state = make_state(cash=6_000.0, positions={"BBB": position("BBB", 1880, 50.0)})
    v = only(evaluate([order(qty=50)], state, PRICES, make_config(), RULES)[0])
    assert not v.accepted and "floor" in v.reason


def test_max_positions_blocks_a_new_name_but_not_a_top_up():
    positions = {
        "AAA": position("AAA", 30, 100.0),
        "BBB": position("BBB", 60, 50.0),
    }
    config = make_config(max_positions=2)
    state = make_state(positions=positions, cash=50_000.0)

    new_name = only(evaluate([order(symbol="CCC", qty=30)], state, {**PRICES, "CCC": 100.0}, config, RULES)[0])
    assert not new_name.accepted  # CCC is not in the universe either

    config.universe.append(Instrument(symbol="CCC", name="Gamma"))
    new_name = only(evaluate([order(symbol="CCC", qty=30)], state, {**PRICES, "CCC": 100.0}, config, RULES)[0])
    assert not new_name.accepted and "maximum" in new_name.reason

    top_up = only(evaluate([order(symbol="AAA", qty=30)], state, PRICES, config, RULES)[0])
    assert top_up.accepted, top_up.reason


# -- limit price sanity -----------------------------------------------------


def test_limit_far_from_last_is_rejected():
    v = only(evaluate([order(qty=50, limit=130.0)], make_state(), PRICES, make_config(), RULES)[0])
    assert not v.accepted and "from last" in v.reason


def test_limit_close_to_last_is_honoured():
    v = only(evaluate([order(qty=50, limit=101.0)], make_state(), PRICES, make_config(), RULES)[0])
    assert v.accepted and v.limit_price == pytest.approx(101.0)


# -- cumulative limits across a batch --------------------------------------


def test_daily_order_count_is_cumulative_within_a_batch():
    config = make_config(max_orders_per_day=2)
    orders = [order(qty=50), order(qty=50), order(qty=50)]
    verdicts, _ = evaluate(orders, make_state(), PRICES, config, RULES)
    assert [v.accepted for v in verdicts] == [True, True, False]
    assert "daily order limit" in verdicts[2].reason


def test_orders_already_placed_today_count_against_the_limit():
    config = make_config(max_orders_per_day=2)
    state = make_state(orders=2)
    v = only(evaluate([order(qty=50)], state, PRICES, config, RULES)[0])
    assert not v.accepted and "daily order limit" in v.reason


def test_turnover_cap_is_cumulative():
    # 25% of 100k = 25,000. Four ~5,000 buys fit; the fifth would not, but the
    # order-count cap bites first, so raise it to isolate turnover.
    config = make_config(max_orders_per_day=20, max_daily_turnover_pct=12.0)
    orders = [order(qty=50) for _ in range(3)]
    verdicts, _ = evaluate(orders, make_state(), PRICES, config, RULES)
    assert [v.accepted for v in verdicts] == [True, True, False]
    assert "turnover" in verdicts[2].reason


def test_cumulative_buys_respect_the_cash_floor():
    config = make_config(max_orders_per_day=20, max_daily_turnover_pct=100.0, max_position_pct=100.0)
    state = make_state(nav=100_000.0, cash=20_000.0, positions={"BBB": position("BBB", 1600, 50.0)})
    orders = [order(qty=50) for _ in range(4)]  # ~5,012 each = ~20,050 total
    verdicts, _ = evaluate(orders, state, PRICES, config, RULES)
    assert verdicts[-1].accepted is False
    assert "floor" in verdicts[-1].reason


def test_sells_are_screened_before_buys():
    """A buy that only fits because a sell frees cash must still pass."""
    config = make_config(max_position_pct=100.0)
    state = make_state(
        nav=100_000.0,
        cash=5_000.0,
        positions={"BBB": position("BBB", 1900, 50.0)},
    )
    buy = order(symbol="AAA", action="BUY", qty=50)      # needs ~5,012
    sell = order(symbol="BBB", action="SELL", qty=150)   # frees ~7,498

    # On its own the buy breaches the cash floor.
    alone = evaluate([buy], state, PRICES, config, RULES)[0]
    assert not alone[0].accepted and "floor" in alone[0].reason

    # Presented together, the sell is screened first and the buy then fits.
    verdicts, _ = evaluate([buy, sell], state, PRICES, config, RULES)
    by_symbol = {v.order.symbol: v for v in verdicts}
    assert by_symbol["BBB"].accepted, by_symbol["BBB"].reason
    assert by_symbol["AAA"].accepted, by_symbol["AAA"].reason
    assert verdicts[0].order.action == "SELL"  # execution order, not input order


# -- the portfolio-level halt ----------------------------------------------


def test_drawdown_halt_stops_everything():
    state = make_state(nav=75_000.0, peak=100_000.0, cash=75_000.0)
    verdicts, halt = evaluate([order(qty=50), order(qty=10)], state, PRICES, make_config(), RULES)
    assert halt
    assert all(not v.accepted for v in verdicts)
    assert "drawdown" in halt


def test_drawdown_just_inside_the_limit_still_trades():
    state = make_state(nav=81_000.0, peak=100_000.0, cash=81_000.0)
    assert check_halt(state, make_config()) == ""


def test_zero_nav_halts():
    assert "NAV" in check_halt(make_state(nav=0.0, cash=0.0), make_config())


def test_empty_order_list_is_fine():
    verdicts, halt = evaluate([], make_state(), PRICES, make_config(), RULES)
    assert verdicts == [] and halt == ""
