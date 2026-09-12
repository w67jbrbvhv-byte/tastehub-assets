"""Rendering and reading the strategy document.

The strategy lives as markdown on disk so you can read it, and as structured
rows in the journal so the program can check that an order cites a real rule.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .models import Strategy

RULE_ID_RE = re.compile(r"^###\s+(\S+)\s*[—:-]", re.MULTILINE)


def render(strategy: Strategy, written_on: date | None = None) -> str:
    day = (written_on or date.today()).isoformat()
    parts = [
        f"# Trading strategy — {day}",
        "",
        "## Thesis",
        strategy.thesis.strip(),
        "",
        "## How the universe is used",
        strategy.universe_view.strip(),
        "",
        "## Rules",
        "",
    ]
    for rule in strategy.rules:
        parts += [
            f"### {rule.rule_id} — {rule.name.strip()}",
            f"**Condition.** {rule.condition.strip()}",
            "",
            f"**Action.** {rule.action.strip()}",
            "",
        ]
    parts += [
        "## Rebalancing",
        strategy.rebalance_policy.strip(),
        "",
        "## Exits",
        strategy.exit_policy.strip(),
        "",
        "## What would prove this wrong",
        strategy.falsification.strip(),
        "",
        "## Changes from the previous version",
        strategy.changes_from_previous.strip(),
        "",
    ]
    return "\n".join(parts)


def save(strategy: Strategy, live_path: Path, archive_dir: Path, written_on: date | None = None) -> str:
    markdown = render(strategy, written_on)
    live_path.parent.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    live_path.write_text(markdown)
    day = (written_on or date.today()).isoformat()
    archive = archive_dir / f"STRATEGY-{day}.md"
    suffix = 1
    while archive.exists():
        suffix += 1
        archive = archive_dir / f"STRATEGY-{day}-{suffix}.md"
    archive.write_text(markdown)
    return markdown


def load(live_path: Path) -> str:
    return live_path.read_text() if live_path.exists() else ""


def rule_ids_from_markdown(markdown: str) -> set[str]:
    """Recover rule ids from the document, so a hand-edited STRATEGY.md still
    validates orders correctly."""
    return set(RULE_ID_RE.findall(markdown))
