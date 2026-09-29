"""`.lego` — the per-agent build file (TOML syntax).

`agents/<agent>.lego` is the agent's *starting point*: which engine, and
which professions / specialties / policies are pre-picked (all un-pickable)
when its create-form opens. Every key is optional; a missing file (or key)
means an empty default for that axis. Reference validity is checked against
a `Registry` (see `registry.Registry.validate_build`), not here.

Two fields of the build are never a `.lego`'s: `model` and `effort`, an
INSTANCE's picks (`tags/models.py`). A shipped pin would turn a vendor's
routine retirement into a failing tree for every clone, and an agent names
the capability it needs through its engine, whose tiers the tree keeps rated
and current — so the picks live only in `instances.toml` and `cluster.toml`.
`load_lego` refuses every key outside `LEGO_KEYS` (operator, 2026-09-29): a
typo'd key would otherwise do nothing, silently.

Kept separate from the tag tree: a `.lego` names tags, it isn't one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .base import TagError, read_toml

LEGO_KEYS = ("ai", "harness", "engine", "professions", "specialties", "policies")


@dataclass(frozen=True)
class AgentBuild:
    """A parsed `.lego`. `engine` is a single name (or None → fall back to
    `engine/<agent>/` then `engine/default/` at resolve time); the three
    axis lists are the pre-picked tag names."""
    ai: str | None = None              # the AI to run on (None → the tree's default member)
    harness: str | None = None         # the agent CLI to run it in (None → the AI's default harness)
    engine: str | None = None
    model: str | None = None           # an instance's model, one of its AI's models.list (None → follow the engine's tier model); never a .lego's
    effort: str | None = None          # an instance's effort level, one its model takes (None → the model's highest); never a .lego's
    professions: tuple[str, ...] = ()
    specialties: tuple[str, ...] = ()
    policies: tuple[str, ...] = ()

    def selected(self) -> set[str]:
        """Every tag name this build pre-picks (engine included), for
        one-shot reference validation."""
        names = {*self.professions, *self.specialties, *self.policies}
        if self.engine:
            names.add(self.engine)
        if self.ai:
            names.add(self.ai)
        if self.harness:
            names.add(self.harness)
        return names


def load_lego(path: Path) -> AgentBuild:
    """Parse an agent's `.lego`. Missing file → an all-empty `AgentBuild`
    (equivalent to an empty file — both legal). Type-checks each key: `ai`,
    `harness` and `engine` strings, the three axis keys lists of strings; anything else is a
    `TagError` naming the file and key — as is any key outside `LEGO_KEYS`,
    `model` and `effort` with their own reason (see the module doc: they
    are an instance's picks)."""
    if not path.is_file():
        return AgentBuild()
    data = read_toml(path)
    for pick in ("model", "effort"):
        if pick in data:
            raise TagError(f"{path}: a .lego cannot pin a {pick} — the {pick} is an instance's pick (the tag form, "
                           f"under the AI); name the capability the agent needs through its engine")
    unknown = sorted(set(data) - set(LEGO_KEYS))
    if unknown:
        raise TagError(f"{path}: unknown key(s) {', '.join(unknown)} — a .lego takes only {', '.join(LEGO_KEYS)}")

    engine = data.get("engine")
    if engine is not None and not isinstance(engine, str):
        raise TagError(f"{path}: 'engine' must be a string, got {type(engine).__name__}")
    ai = data.get("ai")
    if ai is not None and not isinstance(ai, str):
        raise TagError(f"{path}: 'ai' must be a string, got {type(ai).__name__}")
    harness = data.get("harness")
    if harness is not None and not isinstance(harness, str):
        raise TagError(f"{path}: 'harness' must be a string, got {type(harness).__name__}")

    def string_list(key: str) -> tuple[str, ...]:
        raw = data.get(key, [])
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise TagError(f"{path}: '{key}' must be a list of strings")
        return tuple(raw)

    return AgentBuild(
        ai=ai,
        harness=harness,
        engine=engine,
        professions=string_list("professions"),
        specialties=string_list("specialties"),
        policies=string_list("policies"),
    )
