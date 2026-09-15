"""The engine BUDGET — an engine's demands in the launcher's OWN words, read
from `agents/engine/<tag>/tag.budget` (TOML). No AI's key names appear here:
`standard` names one of the launcher's CAPABILITY STANDARDS, which every
`agents/ai/<key>/efforts.tiers` spells out in that AI's model + effort, and
the other keys are PURPOSES each harness's `knobs.mapping` translates into
its native settings (`tags/harness.py`, `Harness.render`). So an engine author never learns
an AI's vocabulary, and adding an AI never touches an engine (decision
2026-09-13, plans/adding_an_ai.md).

A standard is either end of the scale — `cheapest`, `best`: each AI's own
extremes — or a DATED one, `YYYYQn`: the capability the frontier reached in
that quarter, set by the model released then that raised the record
(operator, 2026-09-14). Standards only rise with time, so their order is in
the key itself and this module validates the FORMAT; which dated standards
exist is tree data (`agents/ai/capability.standards`, read by `tags/ai.py`),
and the registry checks every engine names one the AIs define.

Leaf within the tag package: imports `base` only, so `engine.py` (which owns
the file), `ai.py` (the tiers) and `harness.py` (which translates it) can import it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from .base import TagError

BUDGET_FILE = "tag.budget"

# The two named ends of the scale — each AI's own cheapest and strongest
# configuration — and the shape of a dated standard between them.
CHEAPEST = "cheapest"
BEST = "best"
_DATED = re.compile(r"^(\d{4})Q([1-4])$")
_BEST_RANK = 10**6          # above any quarter (9999Q4 ranks 39999)


def is_standard(key: object) -> bool:
    """Whether `key` is spelled like a standard: an end, or `YYYYQn`."""
    return isinstance(key, str) and (key in (CHEAPEST, BEST) or bool(_DATED.match(key)))


def rank_of(standard: str | None) -> int:
    """The standard's place on the scale, an int that rises with capability:
    cheapest 0, a quarter by its date (year × 4 + quarter, so 2025Q4 < 2026Q1),
    best above every quarter; -1 for none. Standards only rise with time
    (operator, 2026-09-14), which is what makes the date the order."""
    if standard == CHEAPEST:
        return 0
    if standard == BEST:
        return _BEST_RANK
    if isinstance(standard, str) and (m := _DATED.match(standard)):
        return int(m.group(1)) * 4 + int(m.group(2))
    return -1


def sorted_standards(keys: Iterable[str]) -> list[str]:
    """Standards weakest first — the order every efforts.tiers lists them."""
    return sorted(keys, key=rank_of)


# The purposes an engine may state beside its standard: switches (a boolean → the
# `<purpose>.on` / `.off` table of a harness's knobs.mapping) and amounts (a positive
# integer → the `<purpose>` table, `{value}` filled in).
SWITCHES = ("thinking", "memory", "background_agents", "telemetry", "tool_search")
AMOUNTS = ("max_output_tokens", "tool_output_tokens", "compact_at_percent")


@dataclass(frozen=True)
class Budget:
    """One engine's budget. Every field optional: an unset purpose renders
    nothing (the AI's defaults stand), and a nested engine's file overlays only
    what it sets (`overlay`). `standard` is required only once an engine is
    rendered — `default/tag.budget` sets it, and nesting inherits it."""
    standard: str | None = None
    thinking: bool | None = None
    memory: bool | None = None
    background_agents: bool | None = None
    telemetry: bool | None = None
    tool_search: bool | None = None
    max_output_tokens: int | None = None
    tool_output_tokens: int | None = None
    compact_at_percent: int | None = None

    def overlay(self, child: "Budget") -> "Budget":
        """This budget with every field the child SETS replacing this one's —
        the nesting rule (`engine/thinker/breakthrough/` = thinker's budget
        plus breakthrough's additions)."""
        return replace(self, **{f.name: v for f in fields(child)
                                if (v := getattr(child, f.name)) is not None})

    @property
    def rank(self) -> int:
        """The standard's place on the scale (`rank_of`); -1 for no standard."""
        return rank_of(self.standard)


def parse_budget(data: dict[str, Any], path: Path) -> Budget:
    """A `tag.budget` mapping → `Budget`, type-checked key by key: `standard`
    spelled like one (an end or a quarter — whether the AIs define it is the
    registry's check), switches booleans, amounts positive integers
    (`compact_at_percent` at most 100); an unknown key is a `TagError`, so a
    typo cannot silently render nothing."""
    known = {"standard", *SWITCHES, *AMOUNTS}
    for key in data:
        if key not in known:
            raise TagError(f"{path}: unknown budget key {key!r} — the words are: {', '.join(sorted(known))}")
    standard = data.get("standard")
    if standard is not None and not is_standard(standard):
        raise TagError(f"{path}: standard must be {CHEAPEST}, {BEST} or a quarter like 2025Q4, got {standard!r}")
    values: dict[str, Any] = {"standard": standard}
    for key in SWITCHES:
        if key in data and not isinstance(data[key], bool):
            raise TagError(f"{path}: {key} must be true or false, got {data[key]!r}")
        values[key] = data.get(key)
    for key in AMOUNTS:
        raw = data.get(key)
        if raw is not None and (isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0):
            raise TagError(f"{path}: {key} must be a positive integer, got {raw!r}")
        if key == "compact_at_percent" and raw is not None and raw > 100:
            raise TagError(f"{path}: compact_at_percent is a percentage, got {raw}")
        values[key] = raw
    return Budget(**values)
