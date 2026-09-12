"""IB Gateway / TWS connection, wrapped so that it cannot reach a live account.

Three independent guards, any one of which aborts the run:

  1. config.mode must be "paper" (there is no other accepted value).
  2. The port must be an IBKR paper port (validated in config.py).
  3. The account IBKR reports must be a DU... paper account. Live accounts
     are U..., so a misconfigured Gateway is caught after connecting, before
     any order is sent.

Guard 3 is the one that actually matters: ports and config can be edited, but
the account identifier comes from IBKR itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ib_async import IB, Contract, LimitOrder, Stock

from .config import Config, Instrument
from .marketdata import build_snapshot
from .models import InstrumentSnapshot, PortfolioState, Position, RiskVerdict

log = logging.getLogger(__name__)

PAPER_ACCOUNT_PREFIX = "DU"


class NotAPaperAccount(RuntimeError):
    pass


@dataclass
class PlacedOrder:
    symbol: str
    action: str
    quantity: int
    limit_price: float
    ib_order_id: int
    status: str
    filled: float
    avg_fill_price: float


class Broker:
    def __init__(self, config: Config) -> None:
        if config.mode != "paper":
            raise NotAPaperAccount(f"mode is {config.mode!r}; this program is paper-only")
        self.config = config
        self.ib = IB()
        self.account: str = ""
        self._contracts: dict[str, Contract] = {}

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "Broker":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()

    def connect(self) -> None:
        cfg = self.config.ibkr
        log.info("connecting to IBKR at %s:%s (client %s)", cfg.host, cfg.port, cfg.client_id)
        self.ib.connect(
            host=cfg.host,
            port=cfg.port,
            clientId=cfg.client_id,
            timeout=cfg.connect_timeout,
            account=cfg.account,
        )
        self.ib.reqMarketDataType(cfg.market_data_type)
        self.account = self._resolve_account()
        log.info("connected to paper account %s", self.account)

    def _resolve_account(self) -> str:
        managed = [a for a in self.ib.managedAccounts() if a]
        if not managed:
            raise NotAPaperAccount("IBKR reported no managed accounts")

        wanted = self.config.ibkr.account
        if wanted:
            if wanted not in managed:
                raise NotAPaperAccount(
                    f"account {wanted!r} not among the accounts this login manages: {managed}"
                )
            account = wanted
        elif len(managed) == 1:
            account = managed[0]
        else:
            paper = [a for a in managed if a.upper().startswith(PAPER_ACCOUNT_PREFIX)]
            if len(paper) != 1:
                raise NotAPaperAccount(
                    f"this login manages several accounts ({managed}); set ibkr.account "
                    f"in config.yaml to the DU... paper account you want to use"
                )
            account = paper[0]

        if not account.upper().startswith(PAPER_ACCOUNT_PREFIX):
            raise NotAPaperAccount(
                f"account {account!r} is not an IBKR paper account (paper accounts start "
                f"with {PAPER_ACCOUNT_PREFIX!r}). Refusing to continue."
            )
        return account

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    # -- contracts ---------------------------------------------------------
    def contract(self, instrument: Instrument) -> Contract:
        if instrument.symbol in self._contracts:
            return self._contracts[instrument.symbol]
        stock = Stock(
            symbol=instrument.symbol,
            exchange=instrument.exchange,
            currency=instrument.currency,
        )
        if instrument.primary_exchange:
            stock.primaryExchange = instrument.primary_exchange
        qualified = self.ib.qualifyContracts(stock)
        if not qualified:
            raise RuntimeError(
                f"IBKR could not resolve {instrument.symbol} "
                f"({instrument.exchange}/{instrument.currency}). Check the symbol, "
                f"exchange and currency in config.yaml."
            )
        self._contracts[instrument.symbol] = qualified[0]
        return qualified[0]

    # -- account state -----------------------------------------------------
    def portfolio_state(self, peak_nav: float = 0.0) -> PortfolioState:
        values = {
            v.tag: v.value
            for v in self.ib.accountValues(self.account)
            if v.currency in (self.config.base_currency, "BASE", "")
        }

        def as_float(tag: str) -> float:
            try:
                return float(values.get(tag, 0.0))
            except (TypeError, ValueError):
                return 0.0

        nav = as_float("NetLiquidation")
        cash = as_float("TotalCashValue")

        positions: dict[str, Position] = {}
        for item in self.ib.portfolio(self.account):
            symbol = item.contract.symbol
            if item.position == 0:
                continue
            positions[symbol] = Position(
                symbol=symbol,
                quantity=item.position,
                market_price=item.marketPrice,
                market_value=item.marketValue,
                average_cost=item.averageCost,
                unrealised_pnl=item.unrealizedPNL,
            )

        return PortfolioState(
            nav=nav,
            cash=cash,
            positions=positions,
            peak_nav=max(peak_nav, nav),
        )

    # -- market data -------------------------------------------------------
    def snapshots(self) -> dict[str, InstrumentSnapshot]:
        out: dict[str, InstrumentSnapshot] = {}
        duration = f"{self.config.ibkr.history_days} D"
        for instrument in self.config.universe:
            contract = self.contract(instrument)
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
            )
            if not bars:
                log.warning("no historical bars returned for %s", instrument.symbol)
            closes = [b.close for b in bars]
            last_date = bars[-1].date if bars else None
            out[instrument.symbol] = build_snapshot(
                instrument.symbol, instrument.name, closes, last_date
            )
        return out

    # -- execution ---------------------------------------------------------
    def place(self, verdict: RiskVerdict, wait_seconds: float = 20.0) -> PlacedOrder:
        """Place one accepted order as a day limit order and wait briefly for
        an initial status. Limit orders only — never market."""
        instrument = self.config.instrument(verdict.order.symbol)
        if instrument is None:
            raise RuntimeError(f"{verdict.order.symbol} left the universe mid-run")

        contract = self.contract(instrument)
        order = LimitOrder(
            action=verdict.order.action,
            totalQuantity=verdict.order.quantity,
            lmtPrice=verdict.limit_price,
        )
        order.tif = "DAY"
        order.account = self.account
        order.orderRef = f"agent:{verdict.order.rule_id}"

        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(1.0)
        waited = 1.0
        while waited < wait_seconds and trade.orderStatus.status in (
            "PendingSubmit",
            "PreSubmitted",
            "ApiPending",
        ):
            self.ib.sleep(1.0)
            waited += 1.0

        return PlacedOrder(
            symbol=verdict.order.symbol,
            action=verdict.order.action,
            quantity=verdict.order.quantity,
            limit_price=verdict.limit_price,
            ib_order_id=trade.order.orderId,
            status=trade.orderStatus.status,
            filled=trade.orderStatus.filled,
            avg_fill_price=trade.orderStatus.avgFillPrice,
        )

    def cancel_open_orders(self) -> int:
        """Cancel anything this agent left working. Called at the start of each
        cycle so yesterday's unfilled limits do not fill on stale logic."""
        cancelled = 0
        for trade in self.ib.openTrades():
            if trade.order.account and trade.order.account != self.account:
                continue
            if not (trade.order.orderRef or "").startswith("agent:"):
                continue
            self.ib.cancelOrder(trade.order)
            cancelled += 1
        if cancelled:
            self.ib.sleep(1.0)
        return cancelled
