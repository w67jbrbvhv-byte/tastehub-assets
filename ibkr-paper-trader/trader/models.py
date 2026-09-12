"""Shared data shapes: what the models are allowed to return, and what the
rest of the program passes around.

The LLM-facing models deliberately avoid Optional fields — a sentinel default
(0.0 / "") keeps the JSON schema simple and the round-trip predictable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Strategist output (weekly)
# --------------------------------------------------------------------------


class StrategyRule(BaseModel):
    rule_id: str = Field(description="Short stable id, e.g. R1, R2. Referenced by orders.")
    name: str = Field(description="Four or five words naming the rule.")
    condition: str = Field(description="The observable, checkable condition that triggers this rule.")
    action: str = Field(description="Exactly what to do when the condition holds, including sizing.")


class Strategy(BaseModel):
    thesis: str = Field(description="The core view, in plain English. Why this should work.")
    universe_view: str = Field(description="How each instrument in the universe is meant to be used.")
    rules: list[StrategyRule] = Field(description="The complete, exhaustive rule set. 3-8 rules.")
    rebalance_policy: str = Field(description="When and how positions are resized.")
    exit_policy: str = Field(description="When positions are cut, including any stop discipline.")
    falsification: str = Field(
        description="What observation over the next quarter would mean this strategy is wrong."
    )
    changes_from_previous: str = Field(
        description="What changed versus the previous version and the evidence for the change. "
        "'Initial version' if there was none."
    )


# --------------------------------------------------------------------------
# Trader output (daily)
# --------------------------------------------------------------------------


class ProposedOrder(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(description="Whole shares, positive.")
    limit_price: float = Field(
        description="Limit price. Use 0 to let the system set it from the last price."
    )
    rule_id: str = Field(description="The rule_id from the strategy that authorises this order.")
    rationale: str = Field(description="One or two sentences. What in today's data triggers the rule.")


class TradingDecision(BaseModel):
    market_assessment: str = Field(description="Three or four sentences on today's data.")
    orders: list[ProposedOrder] = Field(description="Empty list if no rule fires today.")
    no_trade_reason: str = Field(description="If orders is empty, why. Otherwise empty string.")


# --------------------------------------------------------------------------
# Internal state
# --------------------------------------------------------------------------


@dataclass
class Position:
    symbol: str
    quantity: float
    market_price: float
    market_value: float
    average_cost: float
    unrealised_pnl: float

    @property
    def cost_basis(self) -> float:
        return self.average_cost * self.quantity


@dataclass
class PortfolioState:
    nav: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    peak_nav: float = 0.0
    orders_today: int = 0
    turnover_today: float = 0.0
    # Cash ring-fenced for the crash ladder. Invisible to the strategy.
    reserve: float = 0.0

    @property
    def drawdown_pct(self) -> float:
        if self.peak_nav <= 0:
            return 0.0
        return max(0.0, (self.peak_nav - self.nav) / self.peak_nav * 100.0)

    def quantity(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return pos.quantity if pos else 0.0

    def value(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return pos.market_value if pos else 0.0


@dataclass
class InstrumentSnapshot:
    """Precomputed daily metrics for one instrument. This is what the model
    sees — not raw bars."""

    symbol: str
    name: str
    last: float
    as_of: date | None
    ret_1d: float
    ret_5d: float
    ret_20d: float
    ret_60d: float
    ret_252d: float
    sma_20: float
    sma_50: float
    sma_200: float
    pct_vs_sma_50: float
    pct_vs_sma_200: float
    vol_20d_annual: float
    high_252d: float
    low_252d: float
    pct_from_252d_high: float
    bars_available: int

    def to_row(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "last": round(self.last, 4),
            "as_of": self.as_of.isoformat() if self.as_of else "",
            "ret_1d_pct": round(self.ret_1d, 2),
            "ret_5d_pct": round(self.ret_5d, 2),
            "ret_20d_pct": round(self.ret_20d, 2),
            "ret_60d_pct": round(self.ret_60d, 2),
            "ret_252d_pct": round(self.ret_252d, 2),
            "vs_sma50_pct": round(self.pct_vs_sma_50, 2),
            "vs_sma200_pct": round(self.pct_vs_sma_200, 2),
            "vol_20d_annual_pct": round(self.vol_20d_annual, 2),
            "pct_below_252d_high": round(self.pct_from_252d_high, 2),
            "bars": self.bars_available,
        }


@dataclass
class RiskVerdict:
    order: ProposedOrder
    accepted: bool
    reason: str
    limit_price: float = 0.0
    notional: float = 0.0
