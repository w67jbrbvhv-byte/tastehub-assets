"""The cycles: `check`, `strategy` and `trade`.

Order of operations for a trading day, and why:
  1. Halt file → stop. A file you can create by hand outranks everything.
  2. Connect, verify the account is a paper account.
  3. Cancel the agent's own working orders from yesterday.
  4. Read state and market data; record both before anything is decided, so
     the journal shows what the model actually saw.
  5. Portfolio-level halt check (drawdown).
  6. Ask the model.
  7. Screen every proposal through the risk engine.
  8. Place what survives; record everything, accepted and rejected alike.
"""

from __future__ import annotations

import logging
from datetime import date

from . import strategy_doc
from .broker import Broker
from .config import Config
from .journal import Journal
from .llm import Llm, ModelRefusal
from .models import PortfolioState
from .report import order_history_text, performance_text
from .risk import check_halt, evaluate

log = logging.getLogger(__name__)


def _halted_by_file(config: Config) -> bool:
    return config.halt_path.exists()


def _load_state(broker: Broker, journal: Journal, today: date) -> PortfolioState:
    state = broker.portfolio_state(peak_nav=journal.peak_nav())
    state.orders_today, state.turnover_today = journal.orders_today(today)
    return state


def _record_equity(journal: Journal, state: PortfolioState, benchmark_price: float | None, today: date) -> None:
    positions_value = sum(p.market_value for p in state.positions.values())
    journal.record_equity(state.nav, state.cash, positions_value, benchmark_price, today)


# --------------------------------------------------------------------------


def run_check(config: Config) -> int:
    """Connect, verify, print. Places nothing. Run this first."""
    today = date.today()
    with Journal(config.journal_path) as journal, Broker(config) as broker:
        state = _load_state(broker, journal, today)
        snapshots = broker.snapshots()
        bench = snapshots.get(config.benchmark)
        _record_equity(journal, state, bench.last if bench else None, today)

        run_id = journal.start_run("check", "", state.nav, state.cash, today)
        journal.record_snapshots(run_id, snapshots)
        journal.finish_run(run_id, notes="connectivity check")

        print(f"Account:        {broker.account} (paper)")
        print(f"NAV:            {state.nav:,.2f} {config.base_currency}")
        print(f"Cash:           {state.cash:,.2f} {config.base_currency}")
        print(f"Peak NAV:       {state.peak_nav:,.2f}  (drawdown {state.drawdown_pct:.2f}%)")
        print(f"Positions:      {len(state.positions)}")
        for pos in sorted(state.positions.values(), key=lambda p: -p.market_value):
            weight = pos.market_value / state.nav * 100 if state.nav else 0
            print(
                f"  {pos.symbol:<6} {pos.quantity:>8g} @ {pos.market_price:>9,.2f}"
                f"  = {pos.market_value:>12,.2f} ({weight:5.1f}%)"
            )
        print()
        print(f"Market data ({len(snapshots)} instruments):")
        for snap in snapshots.values():
            flag = "" if snap.bars_available >= 200 else "   << thin history"
            print(
                f"  {snap.symbol:<6} last {snap.last:>9,.2f}  20d {snap.ret_20d:>6.2f}%  "
                f"vs200d {snap.pct_vs_sma_200:>6.2f}%  bars {snap.bars_available}{flag}"
            )
        halt = check_halt(state, config)
        print()
        if _halted_by_file(config):
            print(f"HALTED: {config.halt_path} exists. No trading until it is removed.")
        elif halt:
            print(f"HALTED: {halt}")
        else:
            print("Risk state: OK, trading would be permitted.")
        strategy = strategy_doc.load(config.strategy_path)
        rules = strategy_doc.rule_ids_from_markdown(strategy)
        print(f"Strategy:       {'present, rules ' + ', '.join(sorted(rules)) if rules else 'NONE — run `strategy` first'}")
    return 0


def run_strategy(config: Config) -> int:
    """Weekly: let the strategist write or revise the rules. Places nothing."""
    today = date.today()
    with Journal(config.journal_path) as journal, Broker(config) as broker:
        state = _load_state(broker, journal, today)
        snapshots = broker.snapshots()
        bench = snapshots.get(config.benchmark)
        _record_equity(journal, state, bench.last if bench else None, today)

        run_id = journal.start_run(
            "strategy", config.llm.strategist_model, state.nav, state.cash, today
        )
        journal.record_snapshots(run_id, snapshots)

        llm = Llm(config)
        try:
            completion = llm.write_strategy(
                current_strategy_md=strategy_doc.load(config.strategy_path),
                snapshots=snapshots,
                state=state,
                performance=performance_text(journal),
                recent_orders=order_history_text(journal),
            )
        except ModelRefusal as exc:
            journal.finish_run(run_id, halted=True, halt_reason=str(exc))
            print(f"Strategist declined: {exc}")
            print("The existing strategy is unchanged.")
            return 2

        strategy = completion.parsed
        markdown = strategy_doc.save(
            strategy, config.strategy_path, config.strategy_archive_dir, today
        )
        journal.record_strategy(
            run_id, [r.rule_id for r in strategy.rules], markdown, completion.raw
        )
        journal.record_decision(
            run_id, strategy.thesis, "", completion.raw,
            completion.input_tokens, completion.output_tokens,
        )
        journal.finish_run(run_id, notes=f"{len(strategy.rules)} rules")

        print(markdown)
        print(f"\nWritten to {config.strategy_path}")
        print(f"Archived in {config.strategy_archive_dir}")
        print(f"Tokens: {completion.input_tokens:,} in / {completion.output_tokens:,} out")
    return 0


def run_trade(config: Config, dry_run: bool = False) -> int:
    """The daily cycle."""
    today = date.today()

    strategy_md = strategy_doc.load(config.strategy_path)
    if not strategy_md.strip():
        print("No strategy found. Run `python run.py strategy` first.")
        return 2
    known_rules = strategy_doc.rule_ids_from_markdown(strategy_md)
    if not known_rules:
        print(f"{config.strategy_path} contains no parseable rule ids (### R1 — ...).")
        return 2

    if _halted_by_file(config):
        print(f"HALTED: {config.halt_path} exists. Delete it to resume trading.")
        return 3

    with Journal(config.journal_path) as journal, Broker(config) as broker:
        cancelled = broker.cancel_open_orders()
        if cancelled:
            log.info("cancelled %d stale working order(s)", cancelled)

        state = _load_state(broker, journal, today)
        snapshots = broker.snapshots()
        prices = {s.symbol: s.last for s in snapshots.values()}
        bench = snapshots.get(config.benchmark)
        _record_equity(journal, state, bench.last if bench else None, today)

        run_id = journal.start_run("trade", config.llm.trader_model, state.nav, state.cash, today)
        journal.record_snapshots(run_id, snapshots)

        halt = check_halt(state, config)
        if halt:
            journal.finish_run(run_id, halted=True, halt_reason=halt)
            print(f"HALTED: {halt}")
            print("No orders will be placed. Investigate before resuming.")
            return 3

        llm = Llm(config)
        try:
            completion = llm.decide(
                strategy_md=strategy_md,
                snapshots=snapshots,
                state=state,
                recent_orders=order_history_text(journal),
            )
        except ModelRefusal as exc:
            journal.record_decision(run_id, "", str(exc), {"refusal": str(exc)})
            journal.finish_run(run_id, halted=True, halt_reason=str(exc))
            print(f"Model declined: {exc}. No orders placed.")
            return 2

        decision = completion.parsed
        journal.record_decision(
            run_id,
            decision.market_assessment,
            decision.no_trade_reason,
            completion.raw,
            completion.input_tokens,
            completion.output_tokens,
        )

        print(f"=== {today.isoformat()} — {broker.account} ===")
        print(f"NAV {state.nav:,.2f} {config.base_currency}, cash {state.cash:,.2f}, "
              f"{len(state.positions)} positions, drawdown {state.drawdown_pct:.2f}%")
        print()
        print("Assessment:")
        print(f"  {decision.market_assessment}")
        print()

        if not decision.orders:
            reason = decision.no_trade_reason or "no rule fired"
            print(f"No orders today: {reason}")
            journal.finish_run(run_id, notes=f"no orders: {reason}")
            return 0

        verdicts, halt_reason = evaluate(decision.orders, state, prices, config, known_rules)

        placed = 0
        for verdict in verdicts:
            order = verdict.order
            head = f"{order.action} {order.quantity} {order.symbol} [{order.rule_id}]"

            if not verdict.accepted:
                print(f"  REJECTED  {head} — {verdict.reason}")
                journal.record_order(
                    run_id, order.symbol, order.action, order.quantity,
                    verdict.limit_price, verdict.notional, order.rule_id, order.rationale,
                    "rejected", verdict.reason, trade_day=today,
                )
                continue

            if dry_run:
                print(f"  DRY RUN   {head} @ {verdict.limit_price:.2f} "
                      f"= {verdict.notional:,.2f} — {order.rationale}")
                journal.record_order(
                    run_id, order.symbol, order.action, order.quantity,
                    verdict.limit_price, verdict.notional, order.rule_id, order.rationale,
                    "dry_run", "dry run: not sent", trade_day=today,
                )
                continue

            try:
                result = broker.place(verdict)
            except Exception as exc:  # noqa: BLE001 — record and carry on
                log.exception("order placement failed")
                print(f"  ERROR     {head} — {exc}")
                journal.record_order(
                    run_id, order.symbol, order.action, order.quantity,
                    verdict.limit_price, verdict.notional, order.rule_id, order.rationale,
                    "error", str(exc), trade_day=today,
                )
                continue

            placed += 1
            print(f"  PLACED    {head} @ {verdict.limit_price:.2f} "
                  f"= {verdict.notional:,.2f} — {result.status}")
            print(f"            {order.rationale}")
            journal.record_order(
                run_id, order.symbol, order.action, order.quantity,
                verdict.limit_price, verdict.notional, order.rule_id, order.rationale,
                "placed", "", result.ib_order_id, result.status,
                result.filled, result.avg_fill_price, trade_day=today,
            )

        journal.finish_run(
            run_id,
            halted=bool(halt_reason),
            halt_reason=halt_reason,
            notes=f"{placed} placed of {len(verdicts)} proposed",
        )
        print()
        print(f"{placed} order(s) placed, {len(verdicts) - placed} not.")
        print(f"Tokens: {completion.input_tokens:,} in / {completion.output_tokens:,} out")
    return 0


def run_report(config: Config) -> int:
    with Journal(config.journal_path) as journal:
        print(performance_text(journal))
        print()
        history = order_history_text(journal, 40)
        if history:
            print("Recent orders:")
            print(history)
    return 0
