"""The risk engine.

This is the only thing standing between a language model and your account.
It is deliberately dumb, deterministic and unaware of any strategy: it takes
proposed orders and a portfolio state, and refuses anything that breaks a
limit from config.yaml. No prompt, no rule, no argument in a rationale can
route around it, because it never reads them.

Every check is evaluated against a *running simulated* state, so cumulative
limits (position count, cash floor, daily turnover) hold across a batch of
orders rather than only per order.
"""

from __future__ import annotations

from copy import deepcopy

from .config import Config
from .models import PortfolioState, Position, ProposedOrder, RiskVerdict


class HaltReason(str):
    pass


def _round_price(price: float, decimals: int) -> float:
    return round(price, decimals)


def default_limit_price(action: str, last: float, config: Config) -> float:
    """A limit price slightly through the last trade, so orders fill but never
    at an unbounded price. Never a market order — paper or not."""
    offset = config.risk.limit_offset_bps / 10_000.0
    raw = last * (1 + offset) if action == "BUY" else last * (1 - offset)
    return _round_price(raw, config.risk.price_decimals)


def check_halt(state: PortfolioState, config: Config) -> str:
    """Portfolio-level stop. Returns a reason string, or '' if trading may proceed."""
    if state.nav <= 0:
        return f"NAV is {state.nav:.2f}; refusing to trade"
    if state.peak_nav > 0 and state.drawdown_pct >= config.risk.drawdown_halt_pct:
        return (
            f"drawdown {state.drawdown_pct:.1f}% from peak NAV "
            f"{state.peak_nav:,.2f} has reached the {config.risk.drawdown_halt_pct:.1f}% halt limit"
        )
    return ""


def evaluate(
    orders: list[ProposedOrder],
    state: PortfolioState,
    prices: dict[str, float],
    config: Config,
    known_rule_ids: set[str],
) -> tuple[list[RiskVerdict], str]:
    """Screen a batch of proposed orders.

    Returns (verdicts in execution order, halt_reason). If halt_reason is
    non-empty every order is rejected and nothing should be placed.
    """
    halt = check_halt(state, config)
    if halt:
        return ([RiskVerdict(o, False, f"trading halted: {halt}") for o in orders], halt)

    risk = config.risk
    sim = deepcopy(state)
    verdicts: list[RiskVerdict] = []

    # Sells first: they free cash and position slots that buys may need.
    ordered = sorted(orders, key=lambda o: 0 if o.action == "SELL" else 1)

    for order in ordered:
        verdict = _evaluate_one(order, sim, prices, config, known_rule_ids)
        verdicts.append(verdict)
        if verdict.accepted:
            _apply(order, verdict, sim, prices)
            sim.orders_today += 1
            sim.turnover_today += verdict.notional

    _ = risk  # limits are read inside _evaluate_one; kept for readability
    return verdicts, ""


def _evaluate_one(
    order: ProposedOrder,
    sim: PortfolioState,
    prices: dict[str, float],
    config: Config,
    known_rule_ids: set[str],
) -> RiskVerdict:
    risk = config.risk
    reject = lambda reason: RiskVerdict(order, False, reason)  # noqa: E731

    # --- identity and shape -------------------------------------------------
    if config.instrument(order.symbol) is None:
        return reject(f"{order.symbol} is not in the configured universe")

    if order.action not in ("BUY", "SELL"):
        return reject(f"unsupported action {order.action!r}")

    if order.quantity <= 0:
        return reject(f"quantity must be positive, got {order.quantity}")

    if known_rule_ids and order.rule_id not in known_rule_ids:
        return reject(
            f"cites rule {order.rule_id!r}, which is not in the current strategy "
            f"({sorted(known_rule_ids)})"
        )

    last = prices.get(order.symbol, 0.0)
    if last <= 0:
        return reject(f"no usable price for {order.symbol}")

    # --- limit price --------------------------------------------------------
    if order.limit_price and order.limit_price > 0:
        deviation = abs(order.limit_price - last) / last * 100.0
        if deviation > risk.max_limit_deviation_pct:
            return reject(
                f"limit {order.limit_price:.4f} is {deviation:.1f}% from last "
                f"{last:.4f}, above the {risk.max_limit_deviation_pct:.1f}% cap"
            )
        limit = _round_price(order.limit_price, risk.price_decimals)
    else:
        limit = default_limit_price(order.action, last, config)

    notional = limit * order.quantity

    # --- per-order limits ---------------------------------------------------
    if notional < risk.min_order_value:
        return reject(
            f"order value {notional:,.2f} is below the {risk.min_order_value:,.2f} minimum"
        )

    max_trade_value = sim.nav * risk.max_trade_pct / 100.0
    if notional > max_trade_value:
        return reject(
            f"order value {notional:,.2f} exceeds the single-order cap "
            f"{max_trade_value:,.2f} ({risk.max_trade_pct:.1f}% of NAV)"
        )

    # --- daily budget -------------------------------------------------------
    if sim.orders_today >= risk.max_orders_per_day:
        return reject(
            f"daily order limit reached ({risk.max_orders_per_day} orders)"
        )

    turnover_cap = sim.nav * risk.max_daily_turnover_pct / 100.0
    if sim.turnover_today + notional > turnover_cap:
        return reject(
            f"would take today's turnover to {sim.turnover_today + notional:,.2f}, "
            f"above the {turnover_cap:,.2f} cap ({risk.max_daily_turnover_pct:.1f}% of NAV)"
        )

    held = sim.quantity(order.symbol)

    if order.action == "SELL":
        # Long-only: you may sell what you hold and not a share more.
        if order.quantity > held:
            return reject(
                f"would sell {order.quantity} but only {held:g} held; shorting is disabled"
            )
        return RiskVerdict(order, True, "ok", limit, notional)

    # --- BUY ----------------------------------------------------------------
    cash_after = sim.cash - notional
    cash_floor = sim.nav * risk.min_cash_pct / 100.0
    if cash_after < cash_floor:
        return reject(
            f"would leave cash {cash_after:,.2f} below the floor {cash_floor:,.2f} "
            f"({risk.min_cash_pct:.1f}% of NAV)"
        )
    if cash_after < 0:
        return reject("insufficient cash; borrowing is disabled")

    position_value_after = sim.value(order.symbol) + notional
    position_cap = sim.nav * risk.max_position_pct / 100.0
    if position_value_after > position_cap:
        return reject(
            f"would take {order.symbol} to {position_value_after:,.2f}, above the "
            f"{position_cap:,.2f} per-position cap ({risk.max_position_pct:.1f}% of NAV)"
        )

    if held == 0 and len(sim.positions) >= risk.max_positions:
        return reject(
            f"already holding {len(sim.positions)} positions, the configured maximum "
            f"({risk.max_positions})"
        )

    return RiskVerdict(order, True, "ok", limit, notional)


def _apply(
    order: ProposedOrder, verdict: RiskVerdict, sim: PortfolioState, prices: dict[str, float]
) -> None:
    """Fold an accepted order into the simulated state, assuming it fills at
    the limit. Optimistic on purpose: the simulation must not be *looser* than
    reality, and a fill at the limit is the worst price we would accept."""
    symbol = order.symbol
    price = verdict.limit_price
    existing = sim.positions.get(symbol)

    if order.action == "BUY":
        sim.cash -= verdict.notional
        if existing:
            total_cost = existing.average_cost * existing.quantity + verdict.notional
            existing.quantity += order.quantity
            existing.average_cost = total_cost / existing.quantity
            existing.market_value = existing.quantity * prices.get(symbol, price)
        else:
            sim.positions[symbol] = Position(
                symbol=symbol,
                quantity=order.quantity,
                market_price=prices.get(symbol, price),
                market_value=order.quantity * prices.get(symbol, price),
                average_cost=price,
                unrealised_pnl=0.0,
            )
    else:
        sim.cash += verdict.notional
        if existing:
            existing.quantity -= order.quantity
            existing.market_value = existing.quantity * prices.get(symbol, price)
            if existing.quantity <= 0:
                del sim.positions[symbol]
