"""Engine kind — `( )` — "how hard the agent THINKS".

An engine is a BUDGET in the launcher's own words (`tags/budget.py`): a
capability standard plus switches and amounts, read from
`agents/engine/<name>/tag.budget`. It names no AI's model or setting — each
AI's `agents/ai/<key>/efforts.tiers` and `knobs.mapping` translate the budget
(`Ai.render`), so the same engine runs on any AI. Until 2026-09-13 an engine
carried one `<ai>.conf` per AI (`claude.conf` …) with that AI's env vars; the
general budget replaced them.

The rewrite gave engines their own shelf (`agents/engine/<name>/`) and
folder-nesting **budget inheritance**:

    engine/thinker/              → thinker's tag.budget
    engine/thinker/breakthrough/ → thinker's budget, overlaid with breakthrough's

so "an engine exactly like another, with additions" is a nested folder that
holds only the additions. The effective budget is computed at scan time
(parents before children in the tree walk) and stored merged.

Engines are single-select (radio) in the form, so nesting contributes NO
`requires` gating — the inheritance shows up entirely in the merged budget.
The picker's engine ORDER is the budget's standard rank (strongest first),
then the output budget, then the name — AI-neutral by construction.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .base import Tag, common_fields, read_toml, walk_tag_tree
from .budget import BUDGET_FILE, Budget, parse_budget


@dataclass(frozen=True)
class Engine(Tag):
    """A discovered engine. `budget` is the EFFECTIVE (inheritance-merged)
    budget; `Ai.render(engine.budget)` turns it into one AI's settings."""
    parentheses: ClassVar[tuple[str, str]] = ("(", ")")
    root: ClassVar[str] = "engine"
    nutshell: ClassVar[str] = "how hard the agent THINKS"

    budget: Budget = Budget()

    @classmethod
    def scan(cls, agents_dir: Path) -> list["Engine"]:
        """Discover every engine under `agents/engine/`, computing each one's
        inheritance-merged budget. The tree walk yields parents before
        children, so a child's effective budget = its parent's, overlaid with
        the child's own file; a dir without the file inherits everything."""
        effective: dict[str, Budget] = {}
        out: list[Engine] = []
        for tag_dir, ancestors in walk_tag_tree(agents_dir / cls.root):
            own = parse_budget(read_toml(tag_dir / BUDGET_FILE), tag_dir / BUDGET_FILE) \
                if (tag_dir / BUDGET_FILE).is_file() else Budget()
            parent = effective[ancestors[-1]] if ancestors else Budget()
            merged = parent.overlay(own)
            effective[tag_dir.name] = merged
            fields = common_fields(tag_dir)
            fields.pop("_info")
            out.append(cls(**fields, budget=merged))
        return out


def standard_rank(engine: Engine | None) -> int:
    """The engine's place on the capability scale (`budget.rank_of`: cheapest
    0, quarters by date, best on top); -1 for no engine or no standard. The
    picker's sorts and the form's radio order rank by it — the same number
    whatever AI runs."""
    return engine.budget.rank if engine is not None else -1


def sorted_engines(engines: Iterable[Engine]) -> list[Engine]:
    """Engines ordered strongest first — by standard rank, then output budget
    descending (a bigger `max_output_tokens` ranks higher among engines on the
    same standard), then name as a stable final tiebreak. The form's radio
    group and the F8 legend both display in this order."""
    return sorted(engines, key=lambda e: (-standard_rank(e), -(e.budget.max_output_tokens or 0), e.name))
