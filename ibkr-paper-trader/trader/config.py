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
