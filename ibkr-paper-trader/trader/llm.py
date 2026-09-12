"""The two model calls: the strategist (weekly) and the trader (daily).

They are deliberately separated. If one call both forms the view and places
the trades, it will rewrite yesterday's thesis to justify whatever it feels
like doing today — the single most common failure mode of an LLM trader.
Here the strategist sets numbered rules once a week, and the daily trader may
only apply them, citing a rule id per order. An order citing a rule that does
not exist is rejected by the risk engine, not by a prompt.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from .config import Config
from .models import InstrumentSnapshot, PortfolioState, Strategy, TradingDecision

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class ModelRefusal(RuntimeError):
    """The model declined to answer. Treated as a hard stop: no orders."""


@dataclass
class Completion:
    parsed: Any
    input_tokens: int
    output_tokens: int
    raw: dict[str, Any]


STRATEGIST_SYSTEM = """\
You are the strategist for a small, long-only, unlevered paper trading account.
You do not place trades. You write the rule set that a separate, dumber process
applies mechanically each day.

What you are working with:
- A fixed universe of liquid ETFs. You cannot trade anything else.
- Daily price-derived metrics only: returns over several horizons, position
  relative to moving averages, 20-day realised volatility, distance from the
  52-week high. You have no news, no fundamentals, no earnings, no flows, and
  no information after the data given to you.
- Hard risk limits enforced in code. Rules that breach them are silently
  rejected, which wastes the rule. Write within the limits.

What a good rule set looks like:
- Between three and eight rules, each with an id (R1, R2, ...).
- Each condition must be checkable from the metrics provided, with explicit
  numeric thresholds. "If momentum looks strong" is not a rule. "If 60-day
  return exceeds +5% and price is above the 200-day average" is.
- Each action must state size in percent of NAV, not vague direction.
- The rule set must cover both entry and exit. A strategy that only buys is
  not a strategy.

Be honest. You are a language model reasoning over price history; you have no
established edge. The benchmark is simply holding the benchmark instrument, and
most active rule sets lose to it after costs. If the evidence from the journal
says the current rules are not working, say so plainly in changes_from_previous
and simplify — converging toward a passive allocation is a legitimate and often
correct outcome, not a failure to be avoided. Do not add complexity to look busy.
"""

TRADER_SYSTEM = """\
You apply an existing strategy. You do not form your own view and you do not
revise the strategy — a separate weekly process owns that.

Your job each day: read the strategy rules, read today's metrics and the current
portfolio, and decide whether any rule fires. If one does, return the orders it
requires, each citing the rule_id that authorises it. If none fires, return an
empty order list and say so.

Doing nothing is the normal outcome. Most days, no rule fires. You are not
measured on activity, and an order you cannot tie to a specific numbered rule
will be rejected before it reaches the broker. Do not trade because the day feels
eventful, because the portfolio looks unbalanced in a way the rules do not
address, or because you have not traded recently.

Constraints you must respect:
- Long only. You may sell only what is currently held, never more.
- Whole share quantities.
- Only symbols in the universe shown.
- The hard risk limits are listed. Orders that breach them are rejected in code.

Quantities: size in whole shares using the last price shown, so the resulting
order value matches the percentage of NAV the rule calls for.
"""


class Llm:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = anthropic.Anthropic()

    def _call(self, model: str, system: str, prompt: str, output_format: type[T]) -> Completion:
        response = self.client.messages.parse(
            model=model,
            max_tokens=self.config.llm.max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": self.config.llm.effort},
            output_format=output_format,
            messages=[{"role": "user", "content": prompt}],
        )

        if response.stop_reason == "refusal":
            detail = getattr(response.stop_details, "explanation", "") if response.stop_details else ""
            raise ModelRefusal(f"model declined to respond: {detail or 'no explanation given'}")
        if response.stop_reason == "max_tokens":
            raise RuntimeError(
                "response hit max_tokens before completing; raise llm.max_tokens in config.yaml"
            )
        if response.parsed_output is None:
            raise RuntimeError("model returned no parseable structured output")

        return Completion(
            parsed=response.parsed_output,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            raw=response.parsed_output.model_dump(),
        )

    # -- strategist --------------------------------------------------------
    def write_strategy(
        self,
        current_strategy_md: str,
        snapshots: dict[str, InstrumentSnapshot],
        state: PortfolioState,
        performance: str,
        recent_orders: str,
    ) -> Completion:
        prompt = "\n\n".join(
            [
                _universe_block(self.config),
                _limits_block(self.config),
                _market_block(snapshots),
                _portfolio_block(state, self.config),
                "## Performance so far\n" + (performance or "No history yet — this is the first run."),
                "## Recent order history\n" + (recent_orders or "None."),
                "## Current strategy\n"
                + (current_strategy_md.strip() or "None yet. Write the first version."),
                "## Your task\n"
                "Produce the strategy that will govern trading until the next review. "
                "If a current strategy exists, revise it in light of what the journal "
                "shows, and account for the change in changes_from_previous. Keep rule "
                "ids stable where a rule is unchanged.",
            ]
        )
        log.info("calling strategist (%s)", self.config.llm.strategist_model)
        return self._call(self.config.llm.strategist_model, STRATEGIST_SYSTEM, prompt, Strategy)

    # -- trader ------------------------------------------------------------
    def decide(
        self,
        strategy_md: str,
        snapshots: dict[str, InstrumentSnapshot],
        state: PortfolioState,
        recent_orders: str,
    ) -> Completion:
        prompt = "\n\n".join(
            [
                "## The strategy you must apply\n" + strategy_md.strip(),
                _universe_block(self.config),
                _limits_block(self.config),
                _market_block(snapshots),
                _portfolio_block(state, self.config),
                "## Recent order history\n" + (recent_orders or "None."),
                "## Your task\n"
                "Decide today's orders. Cite the authorising rule_id on each. "
                "If no rule fires, return an empty orders list and give the reason.",
            ]
        )
        log.info("calling trader (%s)", self.config.llm.trader_model)
        return self._call(self.config.llm.trader_model, TRADER_SYSTEM, prompt, TradingDecision)


# --------------------------------------------------------------------------
# Prompt blocks — plain text, stable ordering, no timestamps. Keeping these
# deterministic is what lets prompt caching work across runs.
# --------------------------------------------------------------------------


def _universe_block(config: Config) -> str:
    lines = [f"- {i.symbol} ({i.currency}) — {i.name or 'no description'}" for i in config.universe]
    return (
        f"## Universe (the only tradable instruments)\n"
        + "\n".join(lines)
        + f"\n\nBenchmark: {config.benchmark}. Everything is judged against holding it instead."
    )


def _limits_block(config: Config) -> str:
    r = config.risk
    return (
        "## Hard risk limits (enforced in code)\n"
        f"- Max {r.max_position_pct:.0f}% of NAV in any one instrument\n"
        f"- Max {r.max_positions} simultaneous positions\n"
        f"- Cash must stay above {r.min_cash_pct:.0f}% of NAV\n"
        f"- Max {r.max_trade_pct:.0f}% of NAV in a single order\n"
        f"- Max {r.max_orders_per_day} orders per day, max "
        f"{r.max_daily_turnover_pct:.0f}% of NAV traded per day\n"
        f"- Minimum order value {r.min_order_value:,.0f} {config.base_currency}\n"
        f"- Long only, no leverage, no shorting, no derivatives\n"
        f"- All trading halts at a {r.drawdown_halt_pct:.0f}% drawdown from peak NAV"
    )


def _market_block(snapshots: dict[str, InstrumentSnapshot]) -> str:
    rows = [s.to_row() for s in snapshots.values()]
    return (
        "## Today's market data\n"
        "One row per instrument. Percentages unless noted.\n```json\n"
        + json.dumps(rows, indent=2)
        + "\n```"
    )


def _portfolio_block(state: PortfolioState, config: Config) -> str:
    cur = config.base_currency
    lines = [
        f"NAV: {state.nav:,.2f} {cur}",
        f"Cash: {state.cash:,.2f} {cur} ({state.cash / state.nav * 100 if state.nav else 0:.1f}% of NAV)",
        f"Peak NAV to date: {state.peak_nav:,.2f} {cur} (drawdown {state.drawdown_pct:.1f}%)",
        f"Orders already placed today: {state.orders_today}",
        "",
        "Positions:",
    ]
    if not state.positions:
        lines.append("  (none — the account is entirely in cash)")
    for pos in sorted(state.positions.values(), key=lambda p: -p.market_value):
        weight = pos.market_value / state.nav * 100 if state.nav else 0.0
        lines.append(
            f"  {pos.symbol}: {pos.quantity:g} shares @ {pos.market_price:,.2f}, "
            f"value {pos.market_value:,.2f} ({weight:.1f}% of NAV), "
            f"avg cost {pos.average_cost:,.2f}, unrealised {pos.unrealised_pnl:,.2f}"
        )
    return "## Portfolio\n" + "\n".join(lines)
