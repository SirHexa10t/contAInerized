"""`.lego` — the per-agent build file (TOML syntax).

`agents/<agent>.lego` is the agent's *starting point*: which engine, and
which professions / specialties / policies are pre-picked (all un-pickable)
when its create-form opens. Every key is optional; a missing file (or key)
means an empty default for that axis. Reference validity is checked against
a `Registry` (see `registry.Registry.validate_build`), not here.

Kept separate from the tag tree: a `.lego` names tags, it isn't one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .base import TagError, read_toml

LEGO_KEYS = ("engine", "professions", "specialties", "policies")


@dataclass(frozen=True)
class AgentBuild:
    """A parsed `.lego`. `engine` is a single name (or None → fall back to
    `engine/<agent>/` then `engine/default/` at resolve time); the three
    axis lists are the pre-picked tag names."""
    engine: str | None = None
    professions: tuple[str, ...] = ()
    specialties: tuple[str, ...] = ()
    policies: tuple[str, ...] = ()

    def selected(self) -> set[str]:
        """Every tag name this build pre-picks (engine included), for
        one-shot reference validation."""
        names = {*self.professions, *self.specialties, *self.policies}
        if self.engine:
            names.add(self.engine)
        return names


def load_lego(path: Path) -> AgentBuild:
    """Parse an agent's `.lego`. Missing file → an all-empty `AgentBuild`
    (equivalent to an empty file — both legal). Type-checks each key: `engine`
    a string, the three axis keys lists of strings; anything else is a
    `TagError` naming the file and key."""
    if not path.is_file():
        return AgentBuild()
    data = read_toml(path)

    engine = data.get("engine")
    if engine is not None and not isinstance(engine, str):
        raise TagError(f"{path}: 'engine' must be a string, got {type(engine).__name__}")

    def string_list(key: str) -> tuple[str, ...]:
        raw = data.get(key, [])
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise TagError(f"{path}: '{key}' must be a list of strings")
        return tuple(raw)

    return AgentBuild(
        engine=engine,
        professions=string_list("professions"),
        specialties=string_list("specialties"),
        policies=string_list("policies"),
    )
