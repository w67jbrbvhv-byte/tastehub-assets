"""End-to-end wiring of the daily cycle, with the broker and the model stubbed.

Proves the orchestration holds together: strategy is required, the halt file
wins, rejected orders are journalled, and a dry run sends nothing.
"""

from datetime import date

import pytest

from trader import session, strategy_doc
from trader.config import (
    Config,
    CrashLadderConfig,
    IbkrConfig,
    Instrument,
    LadderTranche,
    PathsConfig,
    RiskConfig,
)
from trader.journal import Journal
from trader.llm import Completion
from trader.marketdata import build_snapshot
from trader.models import PortfolioState, ProposedOrder, TradingDecision

STRATEGY_MD = """\
# Trading strategy — test

## Rules

### R1 — Buy the benchmark
**Condition.** Always.

**Action.** Buy 5% of NAV.
"""


@pytest.fixture
def config(tmp_path) -> Config:
    cfg = Config(
        benchmark="AAA",
        universe=[Instrument(symbol="AAA", name="Alpha"), Instrument(symbol="BBB", name="Beta")],
        ibkr=IbkrConfig(port=4002),
        risk=RiskConfig(min_order_value=100.0),
        crash_ladder=CrashLadderConfig(enabled=False, reserve_pct=0.0, tranches=[]),
        paths=PathsConfig(
            journal="journal.sqlite",
            strategy="STRATEGY.md",
            strategy_archive="versions",
            halt_file="HALT",
        ),
    )
    cfg.root = tmp_path
    return cfg


class FakeBroker:
    """Stands in for the IBKR connection. Records what would have been sent."""

    instance: "FakeBroker | None" = None

    def __init__(self, config):
        self.config = config
        self.account = "DU1234567"
        self.placed: list = []
        FakeBroker.instance = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def cancel_open_orders(self):
        return 0

    def portfolio_state(self, peak_nav=0.0):
        return PortfolioState(nav=100_000.0, cash=100_000.0, positions={}, peak_nav=max(peak_nav, 100_000.0))

    # Overridden per test to simulate a crash or bad data.
    closes = {"AAA": [100.0] * 300, "BBB": [50.0] * 300}

    def snapshots(self):
        today = date.today().strftime("%Y%m%d")
        snaps = {
            sym: build_snapshot(sym, sym, series, today)
            for sym, series in self.closes.items()
        }
        return snaps, dict(self.closes)

    def place_raw(self, symbol, action, quantity, limit_price, order_ref, wait_seconds=20.0):
        from trader.broker import PlacedOrder
        from trader.models import ProposedOrder
        from trader.models import RiskVerdict

        verdict = RiskVerdict(
            ProposedOrder(
                symbol=symbol, action=action, quantity=quantity,
                limit_price=limit_price, rule_id=order_ref, rationale="ladder",
            ),
            True, "ok", limit_price, quantity * limit_price,
        )
        self.placed.append(verdict)
        return PlacedOrder(
            symbol=symbol, action=action, quantity=quantity, limit_price=limit_price,
            ib_order_id=len(self.placed), status="Submitted", filled=0.0, avg_fill_price=0.0,
        )

    def place(self, verdict, wait_seconds=20.0):
        from trader.broker import PlacedOrder

        self.placed.append(verdict)
        return PlacedOrder(
            symbol=verdict.order.symbol, action=verdict.order.action,
            quantity=verdict.order.quantity, limit_price=verdict.limit_price,
            ib_order_id=len(self.placed), status="Submitted", filled=0.0, avg_fill_price=0.0,
        )


def fake_llm_returning(*orders):
    class FakeLlm:
        def __init__(self, config):
            pass

        def decide(self, **kwargs):
            decision = TradingDecision(
                market_assessment="Stub assessment.",
                orders=list(orders),
                no_trade_reason="" if orders else "no rule fired",
            )
            return Completion(decision, 100, 50, decision.model_dump())

    return FakeLlm


def install(monkeypatch, llm_cls):
    monkeypatch.setattr(session, "Broker", FakeBroker)
    monkeypatch.setattr(session, "Llm", llm_cls)


def order(symbol="AAA", action="BUY", qty=50, rule="R1", limit=0.0):
    return ProposedOrder(
        symbol=symbol, action=action, quantity=qty, limit_price=limit,
        rule_id=rule, rationale="stub",
    )


# --------------------------------------------------------------------------


def test_trade_refuses_without_a_strategy(config, monkeypatch):
    install(monkeypatch, fake_llm_returning())
    assert session.run_trade(config) == 2


def test_halt_file_stops_the_cycle_before_connecting(config, monkeypatch):
    config.strategy_path.parent.mkdir(parents=True, exist_ok=True)
    config.strategy_path.write_text(STRATEGY_MD)
    config.halt_path.write_text("halted")
    install(monkeypatch, fake_llm_returning(order()))
    assert session.run_trade(config) == 3
    assert FakeBroker.instance is None or not FakeBroker.instance.placed


def test_dry_run_places_nothing_but_journals_everything(config, monkeypatch):
    config.strategy_path.write_text(STRATEGY_MD)
    install(monkeypatch, fake_llm_returning(order()))

    assert session.run_trade(config, dry_run=True) == 0

    assert FakeBroker.instance.placed == []
    with Journal(config.journal_path) as journal:
        rows = journal.recent_orders()
        assert len(rows) == 1
        assert rows[0]["outcome"] == "dry_run"
        assert rows[0]["rule_id"] == "R1"
        # A dry run must not count against tomorrow's budget.
        assert journal.orders_today() == (0, 0.0)


def test_live_run_places_and_journals(config, monkeypatch):
    config.strategy_path.write_text(STRATEGY_MD)
    install(monkeypatch, fake_llm_returning(order()))

    assert session.run_trade(config) == 0

    assert len(FakeBroker.instance.placed) == 1
    with Journal(config.journal_path) as journal:
        rows = journal.recent_orders()
        assert rows[0]["outcome"] == "placed"
        assert rows[0]["ib_status"] == "Submitted"
        count, turnover = journal.orders_today()
        assert count == 1 and turnover > 0


def test_order_citing_an_unknown_rule_is_rejected_not_placed(config, monkeypatch):
    config.strategy_path.write_text(STRATEGY_MD)
    install(monkeypatch, fake_llm_returning(order(rule="R7")))

    assert session.run_trade(config) == 0

    assert FakeBroker.instance.placed == []
    with Journal(config.journal_path) as journal:
        row = journal.recent_orders()[0]
        assert row["outcome"] == "rejected"
        assert "R7" in row["reason"]


def test_order_outside_the_universe_is_rejected(config, monkeypatch):
    config.strategy_path.write_text(STRATEGY_MD)
    install(monkeypatch, fake_llm_returning(order(symbol="TSLA")))

    assert session.run_trade(config) == 0
    assert FakeBroker.instance.placed == []


def test_no_orders_is_a_clean_run(config, monkeypatch):
    config.strategy_path.write_text(STRATEGY_MD)
    install(monkeypatch, fake_llm_returning())

    assert session.run_trade(config) == 0
    with Journal(config.journal_path) as journal:
        assert journal.recent_orders() == []
        assert journal.recent_decisions()[0]["no_trade_reason"] == "no rule fired"


def test_strategy_markdown_without_rule_ids_is_refused(config, monkeypatch):
    config.strategy_path.write_text("# Strategy\n\nBuy things when they look good.\n")
    install(monkeypatch, fake_llm_returning(order()))
    assert session.run_trade(config) == 2


def test_rule_ids_survive_a_round_trip_through_the_document(config):
    ids = strategy_doc.rule_ids_from_markdown(STRATEGY_MD)
    assert ids == {"R1"}


# ==========================================================================
# Crash behaviour, end to end. These are the ones that matter: they assert
# that the pre-committed plan still executes when everything else has stopped.
# ==========================================================================


def ladder_config(config: Config) -> Config:
    config.crash_ladder = CrashLadderConfig(
        enabled=True,
        reserve_pct=25.0,
        target="AAA",
        rearm_within_pct=5.0,
        max_position_pct=60.0,
        limit_offset_bps=150.0,
        tranches=[
            LadderTranche(drawdown_pct=15.0, deploy_pct=20.0),
            LadderTranche(drawdown_pct=25.0, deploy_pct=25.0),
        ],
    )
    return config


def falling_series(depth_pct: float, over_days: int = 30, peak: float = 100.0) -> list[float]:
    """A peak followed by a glide down to `depth_pct` below it.

    Deliberately gradual: a fall delivered in a single bar trips the data guard,
    which is correct behaviour but makes for a fixture that tests nothing. Real
    drawdowns arrive over days.
    """
    floor = peak * (1 - depth_pct / 100.0)
    flat = [peak] * (300 - over_days)
    glide = [peak + (floor - peak) * (i + 1) / over_days for i in range(over_days)]
    return flat + glide


def crashed(depth_pct: float):
    """A FakeBroker whose benchmark sits `depth_pct` below its 252-day high."""

    class CrashedBroker(FakeBroker):
        closes = {"AAA": falling_series(depth_pct), "BBB": [50.0] * 300}

    return CrashedBroker


def test_the_ladder_fires_in_a_crash(config, monkeypatch):
    config = ladder_config(config)
    config.strategy_path.write_text(STRATEGY_MD)
    monkeypatch.setattr(session, "Broker", crashed(20.0))
    monkeypatch.setattr(session, "Llm", fake_llm_returning())

    assert session.run_trade(config) == 0

    placed = FakeBroker.instance.placed
    assert len(placed) == 1
    assert placed[0].order.symbol == "AAA"
    assert placed[0].order.rule_id.startswith("ladder:")

    with Journal(config.journal_path) as journal:
        assert journal.load_ladder_state().fired == {0}
        assert journal.ladder_events()[0]["outcome"] == "placed"


def test_the_ladder_still_fires_when_the_drawdown_halt_has_stopped_the_strategy(
    config, monkeypatch
):
    """The whole point. A 20% NAV drawdown halts the model; it must not halt
    the plan that was written for exactly this moment."""
    config = ladder_config(config)
    config.strategy_path.write_text(STRATEGY_MD)

    Crashed = crashed(30.0)

    class HalvedAccount(Crashed):
        def portfolio_state(self, peak_nav=0.0):
            # NAV 70k against a recorded peak of 100k: a 30% drawdown, well past
            # the 20% halt.
            return PortfolioState(
                nav=70_000.0, cash=70_000.0, positions={}, peak_nav=100_000.0
            )

    monkeypatch.setattr(session, "Broker", HalvedAccount)
    monkeypatch.setattr(session, "Llm", fake_llm_returning(order()))

    assert session.run_trade(config) == 3  # strategy halted

    placed = FakeBroker.instance.placed
    assert len(placed) == 1, "the ladder should have deployed despite the halt"
    assert placed[0].order.rule_id.startswith("ladder:")

    with Journal(config.journal_path) as journal:
        # Both rungs cleared by a 30% fall.
        assert journal.load_ladder_state().fired == {0, 1}
        # And the model's own order never reached the broker.
        assert [r["outcome"] for r in journal.recent_orders()] == ["placed"]
        assert journal.recent_orders()[0]["rule_id"].startswith("LADDER:")


def test_suspect_data_stops_the_ladder_as_well_as_the_strategy(config, monkeypatch):
    config = ladder_config(config)
    config.strategy_path.write_text(STRATEGY_MD)

    class BadPrint(FakeBroker):
        # A 60% one-day collapse: either a bad tick or the end of the world.
        # Either way a person should look before anything is bought.
        closes = {"AAA": [100.0] * 299 + [40.0], "BBB": [50.0] * 300}

    monkeypatch.setattr(session, "Broker", BadPrint)
    monkeypatch.setattr(session, "Llm", fake_llm_returning(order()))

    assert session.run_trade(config) == 3
    assert FakeBroker.instance.placed == []

    with Journal(config.journal_path) as journal:
        assert journal.load_ladder_state().fired == set()
        assert journal.ladder_events()[0]["outcome"] == "held"


def test_the_strategy_cannot_spend_the_crash_reserve(config, monkeypatch):
    config = ladder_config(config)
    config.strategy_path.write_text(STRATEGY_MD)
    # Cash is 100k on a 100k NAV; the reserve ring-fences 25k. A 78k buy leaves
    # 22k, under the reserve, so it must be refused.
    monkeypatch.setattr(session, "Broker", FakeBroker)
    monkeypatch.setattr(
        session, "Llm", fake_llm_returning(order(qty=780))
    )
    config.risk.max_trade_pct = 100.0
    config.risk.max_position_pct = 100.0
    config.risk.max_daily_turnover_pct = 200.0

    assert session.run_trade(config) == 0
    assert FakeBroker.instance.placed == []

    with Journal(config.journal_path) as journal:
        row = journal.recent_orders()[0]
        assert row["outcome"] == "rejected"
        assert "ring-fenced" in row["reason"]


def test_a_ladder_dry_run_consumes_no_rung(config, monkeypatch):
    config = ladder_config(config)
    config.strategy_path.write_text(STRATEGY_MD)
    monkeypatch.setattr(session, "Broker", crashed(20.0))
    monkeypatch.setattr(session, "Llm", fake_llm_returning())

    assert session.run_trade(config, dry_run=True) == 0
    assert FakeBroker.instance.placed == []

    with Journal(config.journal_path) as journal:
        assert journal.load_ladder_state().fired == set(), (
            "a dry run must leave the ladder armed, or a rehearsal would spend "
            "a rung that then never fires for real"
        )


def test_a_calm_market_leaves_the_ladder_alone(config, monkeypatch):
    config = ladder_config(config)
    config.strategy_path.write_text(STRATEGY_MD)
    monkeypatch.setattr(session, "Broker", crashed(4.0))
    monkeypatch.setattr(session, "Llm", fake_llm_returning())

    assert session.run_trade(config) == 0
    assert FakeBroker.instance.placed == []
    with Journal(config.journal_path) as journal:
        assert journal.ladder_events() == []
