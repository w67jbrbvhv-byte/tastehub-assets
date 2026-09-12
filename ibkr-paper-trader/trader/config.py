"""Configuration loading and validation.

Everything the program is allowed to do is declared here. If a limit is not in
this file, it is not enforced anywhere else either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# IB Gateway / TWS paper trading ports. Anything else is refused.
PAPER_PORTS = {4002, 7497}
LIVE_PORTS = {4001, 7496}


class Instrument(BaseModel):
    symbol: str
    exchange: str = "SMART"
    primary_exchange: str = ""
    currency: str = "GBP"
    name: str = ""


class IbkrConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 17
    account: str = ""
    market_data_type: int = Field(default=3, ge=1, le=4)
    connect_timeout: float = 20.0
    history_days: int = Field(default=400, ge=120, le=2000)

    @field_validator("port")
    @classmethod
    def _paper_port_only(cls, port: int) -> int:
        if port in LIVE_PORTS:
            raise ValueError(
                f"Port {port} is an IBKR LIVE trading port. This program only "
                f"connects to paper ports {sorted(PAPER_PORTS)}."
            )
        if port not in PAPER_PORTS:
            raise ValueError(
                f"Port {port} is not a known IBKR paper port. Use 4002 "
                f"(IB Gateway paper) or 7497 (TWS paper)."
            )
        return port


class LlmConfig(BaseModel):
    strategist_model: str = "claude-opus-5"
    trader_model: str = "claude-opus-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_tokens: int = Field(default=16000, ge=2000, le=64000)


class RiskConfig(BaseModel):
    max_position_pct: float = Field(default=20.0, gt=0, le=100)
    max_positions: int = Field(default=8, ge=1)
    min_cash_pct: float = Field(default=5.0, ge=0, lt=100)
    max_trade_pct: float = Field(default=10.0, gt=0, le=100)
    max_orders_per_day: int = Field(default=4, ge=0)
    max_daily_turnover_pct: float = Field(default=25.0, gt=0)
    drawdown_halt_pct: float = Field(default=20.0, gt=0, le=100)
    min_order_value: float = Field(default=250.0, ge=0)
    limit_offset_bps: float = Field(default=25.0, ge=0, le=500)
    max_limit_deviation_pct: float = Field(default=2.0, gt=0, le=20)
    price_decimals: int = Field(default=2, ge=0, le=6)


class LadderTranche(BaseModel):
    """One rung: when the benchmark is this far below its 52-week high, deploy
    this share of the reserve."""

    drawdown_pct: float = Field(gt=0, le=95, description="Benchmark drawdown that arms this rung")
    deploy_pct: float = Field(gt=0, le=100, description="Share of the reserve to deploy, %")


# Deeper falls buy more, because they are rarer and the expected return from
# deploying into them is higher. Nothing here is optimised — it is a shape you
# can defend in advance, which is the only property that matters.
DEFAULT_TRANCHES = [
    LadderTranche(drawdown_pct=15.0, deploy_pct=20.0),
    LadderTranche(drawdown_pct=25.0, deploy_pct=25.0),
    LadderTranche(drawdown_pct=35.0, deploy_pct=30.0),
    LadderTranche(drawdown_pct=50.0, deploy_pct=25.0),
]


class CrashLadderConfig(BaseModel):
    """The pre-committed response to a severe market fall.

    No model is consulted. The rungs, the sizes and the instrument are decided
    here, in advance, in the cold light of day — which is the entire point.
    """

    enabled: bool = True
    reserve_pct: float = Field(default=25.0, ge=0, le=80)
    target: str = Field(default="", description="What to buy on the way down. Blank = benchmark.")
    # Rungs fire once each. They re-arm only after the benchmark recovers to
    # within this distance of its high, so a choppy market cannot re-trigger them.
    rearm_within_pct: float = Field(default=5.0, gt=0, le=50)
    # A crash is exactly when normal position and turnover caps must not apply —
    # concentrating into the broad market is the intent, not an accident.
    max_position_pct: float = Field(default=60.0, gt=0, le=100)
    # Spreads blow out in a fall. A 25bps limit will not fill.
    limit_offset_bps: float = Field(default=150.0, ge=0, le=1000)
    # An unfilled tranche carries to the next run rather than being abandoned.
    carry_unfilled: bool = True
    tranches: list[LadderTranche] = Field(default_factory=lambda: list(DEFAULT_TRANCHES))

    @field_validator("tranches")
    @classmethod
    def _rungs_ascend(cls, tranches: list[LadderTranche]) -> list[LadderTranche]:
        if not tranches:
            return tranches
        depths = [t.drawdown_pct for t in tranches]
        if depths != sorted(depths):
            raise ValueError("crash_ladder.tranches must be ordered by increasing drawdown_pct")
        if len(set(depths)) != len(depths):
            raise ValueError("crash_ladder.tranches contains duplicate drawdown_pct values")
        total = sum(t.deploy_pct for t in tranches)
        if total > 100.0 + 1e-9:
            raise ValueError(
                f"crash_ladder.tranches deploy {total:.1f}% of the reserve in total; "
                f"the maximum is 100%"
            )
        return tranches

    @property
    def total_deploy_pct(self) -> float:
        return sum(t.deploy_pct for t in self.tranches)


class DataGuardConfig(BaseModel):
    """Refusing to trade on data you do not trust.

    A bad print during a fast market is indistinguishable from a real collapse
    at the moment it arrives. The correct response to both is to stop and fetch
    a human, not to guess.
    """

    min_bars: int = Field(default=60, ge=1, description="Below this, an instrument is unusable")
    max_stale_days: int = Field(default=5, ge=1, description="Calendar days since the last bar")
    max_daily_move_pct: float = Field(
        default=25.0, gt=0, description="A one-day move beyond this marks the data suspect"
    )
    max_median_deviation_pct: float = Field(
        default=35.0, gt=0, description="Deviation of the last close from the 5-day median"
    )


class FrictionConfig(BaseModel):
    """What a paper fill does not charge you.

    Used only in reporting, to haircut the paper record before it is compared
    with buy-and-hold. Paper fills have no spread, no queue and no impact; a
    record that ignores that is not evidence of anything.
    """

    commission_per_order: float = Field(default=3.0, ge=0)
    half_spread_bps: float = Field(default=6.0, ge=0)
    impact_bps: float = Field(default=2.0, ge=0)

    def cost_of(self, notional: float) -> float:
        return self.commission_per_order + notional * (
            self.half_spread_bps + self.impact_bps
        ) / 10_000.0


class PathsConfig(BaseModel):
    journal: str = "data/journal.sqlite"
    strategy: str = "strategy/STRATEGY.md"
    strategy_archive: str = "strategy/versions"
    halt_file: str = "HALT"


class Config(BaseModel):
    mode: Literal["paper"] = "paper"
    base_currency: str = "GBP"
    benchmark: str
    ibkr: IbkrConfig = Field(default_factory=IbkrConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    crash_ladder: CrashLadderConfig = Field(default_factory=CrashLadderConfig)
    data_guard: DataGuardConfig = Field(default_factory=DataGuardConfig)
    friction: FrictionConfig = Field(default_factory=FrictionConfig)
    universe: list[Instrument]
    paths: PathsConfig = Field(default_factory=PathsConfig)

    # Absolute root the relative paths above resolve against. Set on load.
    root: Path = Field(default_factory=Path.cwd, exclude=True)

    @field_validator("universe")
    @classmethod
    def _universe_sane(cls, universe: list[Instrument]) -> list[Instrument]:
        if not universe:
            raise ValueError("universe is empty: the agent would have nothing to trade")
        symbols = [i.symbol for i in universe]
        dupes = {s for s in symbols if symbols.count(s) > 1}
        if dupes:
            raise ValueError(f"duplicate symbols in universe: {sorted(dupes)}")
        return universe

    @model_validator(mode="after")
    def _benchmark_in_universe(self) -> "Config":
        if self.benchmark not in {i.symbol for i in self.universe}:
            raise ValueError(
                f"benchmark {self.benchmark!r} is not in the universe; it must be, "
                f"so its price history can be fetched"
            )
        return self

    @model_validator(mode="after")
    def _ladder_target_in_universe(self) -> "Config":
        ladder = self.crash_ladder
        if not ladder.enabled:
            return self
        if ladder.reserve_pct > 0 and not ladder.tranches:
            raise ValueError(
                "crash_ladder holds a reserve but defines no tranches: the cash would "
                "be locked away with nothing able to deploy it"
            )
        target = ladder.target or self.benchmark
        if target not in {i.symbol for i in self.universe}:
            raise ValueError(f"crash_ladder.target {target!r} is not in the universe")
        return self

    @property
    def ladder_target(self) -> str:
        return self.crash_ladder.target or self.benchmark

    # -- resolved paths ----------------------------------------------------
    @property
    def journal_path(self) -> Path:
        return self.root / self.paths.journal

    @property
    def strategy_path(self) -> Path:
        return self.root / self.paths.strategy

    @property
    def strategy_archive_dir(self) -> Path:
        return self.root / self.paths.strategy_archive

    @property
    def halt_path(self) -> Path:
        return self.root / self.paths.halt_file

    @property
    def symbols(self) -> list[str]:
        return [i.symbol for i in self.universe]

    def instrument(self, symbol: str) -> Instrument | None:
        return next((i for i in self.universe if i.symbol == symbol), None)


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.yaml to config.yaml and edit it."
        )
    data = yaml.safe_load(path.read_text()) or {}
    config = Config.model_validate(data)
    config.root = path.parent
    return config
