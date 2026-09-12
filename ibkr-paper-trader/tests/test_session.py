"""End-to-end wiring of the daily cycle, with the broker and the model stubbed.

Proves the orchestration holds together: strategy is required, the halt file
wins, rejected orders are journalled, and a dry run sends nothing.
"""

import pytest

from trader import session, strategy_doc
from trader.config import Config, IbkrConfig, Instrument, PathsConfig, RiskConfig
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

    def snapshots(self):
        return {
            "AAA": build_snapshot("AAA", "Alpha", [100.0] * 300, "20260911"),
            "BBB": build_snapshot("BBB", "Beta", [50.0] * 300, "20260911"),
        }

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
