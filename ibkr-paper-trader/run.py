#!/usr/bin/env python3
"""Entry point.

    python run.py check              connect, verify the paper account, print state
    python run.py strategy           weekly: write or revise the rules
    python run.py trade --dry-run    daily: decide, screen, but place nothing
    python run.py trade              daily: decide, screen, place
    python run.py report             performance against buy-and-hold
    python run.py halt / resume      the kill switch
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from trader.broker import NotAPaperAccount
from trader.config import load_config
from trader.session import run_check, run_report, run_strategy, run_trade

DEFAULT_CONFIG = Path(__file__).parent / "config.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IBKR paper trading agent")
    parser.add_argument(
        "command",
        choices=["check", "strategy", "trade", "report", "halt", "resume"],
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="trade: run the full cycle but send nothing to IBKR",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # ib_async is chatty about routine disconnects.
    logging.getLogger("ib_async").setLevel(logging.ERROR)

    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001 — config errors must be legible
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    if args.command == "halt":
        config.halt_path.write_text("halted by hand\n")
        print(f"Created {config.halt_path}. No orders will be placed until it is deleted.")
        return 0

    if args.command == "resume":
        if config.halt_path.exists():
            config.halt_path.unlink()
            print(f"Removed {config.halt_path}. Trading may resume.")
        else:
            print("Not halted.")
        return 0

    if args.command == "report":
        return run_report(config)

    try:
        if args.command == "check":
            return run_check(config)
        if args.command == "strategy":
            return run_strategy(config)
        if args.command == "trade":
            return run_trade(config, dry_run=args.dry_run)
    except NotAPaperAccount as exc:
        print(f"REFUSING TO RUN: {exc}", file=sys.stderr)
        return 4
    except ConnectionRefusedError:
        print(
            f"Could not reach IB Gateway at {config.ibkr.host}:{config.ibkr.port}.\n"
            "Is the Gateway running and logged in, and is the API enabled "
            "(Configure > Settings > API > Enable ActiveX and Socket Clients)?",
            file=sys.stderr,
        )
        return 5
    except TimeoutError:
        print(
            "Timed out talking to IB Gateway. It is probably running but not logged in, "
            "or another program is using the same client id.",
            file=sys.stderr,
        )
        return 5

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
