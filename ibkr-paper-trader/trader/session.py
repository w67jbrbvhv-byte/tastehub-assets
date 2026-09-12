"""The cycles: `check`, `strategy` and `trade`.

Order of operations for a trading day, and why:
  1. Halt file → stop. A file you can create by hand outranks everything.
  2. Connect, verify the account is a paper account.
  3. Cancel the agent's own working orders from yesterday.
  4. Read state and market data; record both before anything is decided, so
     the journal shows what the model actually saw.
  5. Screen the data. A suspect snapshot stops everything, ladder included —
     a bad print and a real collapse look identical at the moment they land.
  6. Run the crash ladder. Before the model, and *not* blocked by the drawdown
     halt: a 20% NAV drawdown is roughly when the second rung fires, so a halt
     that stopped it would disable the plan exactly when it was written for.
  7. Portfolio-level halt check (drawdown) — this gates the model, only.
  8. Ask the model.
  9. Screen every proposal through the risk engine, with the crash reserve
     subtracted from spendable cash so the strategy cannot raid it.
 10. Place what survives; record everything, accepted and rejected alike.
"""

from __future__ import annotations

import logging
from datetime import date

from . import dataguard, strategy_doc, tail
from .broker import Broker
from .config import Config
from .journal import Journal
from .llm import Llm, ModelRefusal
from .models import InstrumentSnapshot, PortfolioState
from .report import ladder_status_text, order_history_text, performance_text
from .risk import check_halt, evaluate
from .tail import LadderState

log = logging.getLogger(__name__)


def _halted_by_file(config: Config) -> bool:
    return config.halt_path.exists()


def _load_state(
    broker: Broker, journal: Journal, today: date, config: Config
) -> tuple[PortfolioState, LadderState]:
    state = broker.portfolio_state(peak_nav=journal.peak_nav())
    state.orders_today, state.turnover_today = journal.orders_today(today)
    row = journal.load_ladder_state()
    ladder_state = LadderState(
        fired=row.fired,
        reserve_base=row.reserve_base,
        carried_notional=row.carried_notional,
        cycle_low_drawdown=row.cycle_low_drawdown,
    )
    state.reserve = tail.reserve_required(state.nav, ladder_state, config)
    return state, ladder_state


def _save_ladder(journal: Journal, state: LadderState) -> None:
    journal.save_ladder_state(
        state.fired, state.reserve_base, state.carried_notional, state.cycle_low_drawdown
    )


def _run_ladder(
    config: Config,
    journal: Journal,
    broker: Broker,
    run_id: int,
    state: PortfolioState,
    ladder_state: LadderState,
    snapshots: dict[str, InstrumentSnapshot],
    today: date,
    dry_run: bool,
) -> LadderState:
    """The pre-committed crash response. No model is consulted anywhere in here."""
    benchmark = snapshots.get(config.benchmark)
    if benchmark is None:
        return ladder_state

    action = tail.evaluate(ladder_state, benchmark, state.nav, state.cash, config)

    if not action.acts:
        if action.note and action.new_state.fired != ladder_state.fired:
            # A re-arm happened: worth a line in the journal.
            print(f"Crash ladder: {action.note}")
            journal.record_ladder_event(
                run_id, action.drawdown_pct, [], 0.0, "rearmed", action.note, today
            )
        _save_ladder(journal, action.new_state)
        return action.new_state

    price = snapshots[action.symbol].last if action.symbol in snapshots else 0.0
    quantity, limit = tail.size_order(action, price, config)

    print()
    print("=== CRASH LADDER ===")
    print(f"  {action.note}")

    if quantity <= 0:
        reason = f"notional {action.notional:,.2f} too small to buy a share of {action.symbol}"
        print(f"  NOT DEPLOYED: {reason}")
        journal.record_ladder_event(
            run_id, action.drawdown_pct, action.rungs, action.notional, "skipped", reason, today
        )
        _save_ladder(journal, action.new_state)
        return action.new_state

    notional = quantity * limit
    cap_breach = tail.check_position_cap(
        action.symbol, notional, state.value(action.symbol), state.nav, config
    )
    if cap_breach:
        print(f"  NOT DEPLOYED: {cap_breach}")
        journal.record_ladder_event(
            run_id, action.drawdown_pct, action.rungs, notional, "rejected", cap_breach, today
        )
        _save_ladder(journal, action.new_state)
        return action.new_state

    rung_label = "+".join(str(r) for r in action.rungs) or "carry"
    if dry_run:
        print(f"  DRY RUN: BUY {quantity} {action.symbol} @ {limit:.2f} = {notional:,.2f}")
        journal.record_ladder_event(
            run_id, action.drawdown_pct, action.rungs, notional, "dry_run", action.note, today
        )
        _save_ladder(journal, ladder_state)  # a dry run must not consume a rung
        return ladder_state

    try:
        result = broker.place_raw(
            action.symbol, "BUY", quantity, limit, f"ladder:{rung_label}"
        )
    except Exception as exc:  # noqa: BLE001 — record and let the day continue
        log.exception("ladder order failed")
        print(f"  ERROR: {exc}")
        journal.record_ladder_event(
            run_id, action.drawdown_pct, action.rungs, notional, "error", str(exc), today
        )
        # The rung is not consumed: it will fire again on the next run.
        _save_ladder(journal, ladder_state)
        return ladder_state

    print(f"  PLACED: BUY {quantity} {action.symbol} @ {limit:.2f} = {notional:,.2f} "
          f"({result.status})")
    journal.record_order(
        run_id, action.symbol, "BUY", quantity, limit, notional, f"LADDER:{rung_label}",
        action.note, "placed", "", result.ib_order_id, result.status,
        result.filled, result.avg_fill_price, trade_day=today,
    )
    journal.record_ladder_event(
        run_id, action.drawdown_pct, action.rungs, notional, "placed", action.note, today
    )
    _save_ladder(journal, action.new_state)
    return action.new_state


def _record_equity(journal: Journal, state: PortfolioState, benchmark_price: float | None, today: date) -> None:
    positions_value = sum(p.market_value for p in state.positions.values())
    journal.record_equity(state.nav, state.cash, positions_value, benchmark_price, today)


# --------------------------------------------------------------------------


def run_check(config: Config) -> int:
    """Connect, verify, print. Places nothing. Run this first."""
    today = date.today()
    with Journal(config.journal_path) as journal, Broker(config) as broker:
        state, ladder_state = _load_state(broker, journal, today, config)
        snapshots, closes = broker.snapshots()
        bench = snapshots.get(config.benchmark)
        _record_equity(journal, state, bench.last if bench else None, today)

        run_id = journal.start_run("check", "", state.nav, state.cash, today)
        journal.record_snapshots(run_id, snapshots)
        journal.finish_run(run_id, notes="connectivity check")

        print(f"Account:        {broker.account} (paper)")
        print(f"NAV:            {state.nav:,.2f} {config.base_currency}")
        print(f"Cash:           {state.cash:,.2f} {config.base_currency}")
        print(f"  crash reserve {state.reserve:,.2f} (ring-fenced from the strategy)")
        print(f"  strategy may spend {max(0.0, state.cash - state.reserve):,.2f}")
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
        verdict = dataguard.inspect(snapshots, closes, config, today)
        if verdict.summary:
            print()
            print(verdict.summary)

        print()
        print(ladder_status_text(config, ladder_state, bench))

        halt = check_halt(state, config)
        print()
        if not verdict.ok:
            print("HALTED: the market data failed its sanity checks (see above).")
        elif _halted_by_file(config):
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
        state, ladder_state = _load_state(broker, journal, today, config)
        snapshots, closes = broker.snapshots()
        bench = snapshots.get(config.benchmark)
        _record_equity(journal, state, bench.last if bench else None, today)

        verdict = dataguard.inspect(snapshots, closes, config, today)
        if not verdict.ok:
            print(verdict.summary)
            print("\nRefusing to write a strategy on data that failed its sanity checks.")
            return 3

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
                performance=performance_text(journal, config),
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

        state, ladder_state = _load_state(broker, journal, today, config)
        snapshots, closes = broker.snapshots()
        prices = {s.symbol: s.last for s in snapshots.values()}
        bench = snapshots.get(config.benchmark)
        _record_equity(journal, state, bench.last if bench else None, today)

        run_id = journal.start_run("trade", config.llm.trader_model, state.nav, state.cash, today)
        journal.record_snapshots(run_id, snapshots)

        # --- data first. Nothing acts on numbers that failed their checks. ---
        data = dataguard.inspect(snapshots, closes, config, today)
        if data.warnings:
            print(data.summary)
        if not data.ok:
            print(data.summary)
            print("\nNo orders will be placed, and the crash ladder is held too. "
                  "Check the prices by hand before the next run.")
            journal.finish_run(run_id, halted=True, halt_reason="market data failed sanity checks")
            journal.record_ladder_event(
                run_id, 0.0, [], 0.0, "held", "suspect market data", today
            )
            return 3

        # --- the crash ladder. Mechanical, and deliberately not gated by the
        #     drawdown halt below: the halt stops narrative, not the plan. ----
        ladder_state = _run_ladder(
            config, journal, broker, run_id, state, ladder_state, snapshots, today, dry_run
        )
        # Deploying consumes reserve, so the strategy's spendable cash moves.
        state.reserve = tail.reserve_required(state.nav, ladder_state, config)

        halt = check_halt(state, config)
        if halt:
            journal.finish_run(run_id, halted=True, halt_reason=halt)
            print()
            print(f"STRATEGY HALTED: {halt}")
            print("The crash ladder above still runs — it is a pre-committed plan, not a view.")
            print("Strategy trading stays halted until you intervene.")
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
        print(f"NAV {state.nav:,.2f} {config.base_currency}, cash {state.cash:,.2f} "
              f"(reserve {state.reserve:,.2f}), {len(state.positions)} positions, "
              f"drawdown {state.drawdown_pct:.2f}%")
        print()
        print("Assessment:")
        print(f"  {decision.market_assessment}")
        print()

        if not decision.orders:
            reason = decision.no_trade_reason or "no rule fired"
            print(f"No orders today: {reason}")
            journal.finish_run(run_id, notes=f"no orders: {reason}")
            return 0

        verdicts, halt_reason = evaluate(
            decision.orders, state, prices, config, known_rules, reserve=state.reserve
        )

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
        print(performance_text(journal, config))
        print()
        row = journal.load_ladder_state()
        ladder_state = LadderState(
            fired=row.fired,
            reserve_base=row.reserve_base,
            carried_notional=row.carried_notional,
            cycle_low_drawdown=row.cycle_low_drawdown,
        )
        print(ladder_status_text(config, ladder_state, None))
        events = journal.ladder_events(10)
        if events:
            print()
            print("Crash ladder history:")
            for e in reversed(events):
                print(
                    f"  {e['trade_date']} drawdown {e['drawdown_pct']:.1f}% "
                    f"{e['outcome']}: {e['note']}"
                )
        print()
        history = order_history_text(journal, 40)
        if history:
            print("Recent orders:")
            print(history)
    return 0
