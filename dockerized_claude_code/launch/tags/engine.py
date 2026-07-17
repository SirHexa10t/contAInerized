"""Engine kind — `( )` — "how hard the agent THINKS".

An engine is a token/effort/model budget: the existing `engine.conf` env-var
format (ANTHROPIC_MODEL, CLAUDE_CODE_EFFORT_LEVEL, thinking/output knobs). It
is the one kind that was always file-defined; the rewrite gives it its own
shelf (`agents/engine/<name>/`) and folder-nesting **conf inheritance**:

    engine/thinker/            → thinker's engine.conf
    engine/thinker/breakthrough/ → thinker's conf, overlaid with breakthrough's

so "an engine exactly like another, with additions" is a nested folder that
holds only the additions. The effective conf is computed at scan time
(parents before children in the tree walk) and stored merged.

Engines are single-select (radio) in the form, so nesting contributes NO
`requires` gating — the inheritance shows up entirely in the merged conf.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from dotenv import dotenv_values

from ..utils import ordering_index_or_end
from .base import Tag, common_fields, walk_tag_tree

CONF_FILE = "engine.conf"


# ============================================================
# Model-family ordering (picker sort keys)
# ============================================================
# An engine's capability rank derives from its conf's ANTHROPIC_MODEL — the
# picker sorts agents by their engine's family, then version, then name.

ORDERED_MODEL_FAMILIES = ["fable", "mythos", "opus", "sonnet", "haiku"]   # most capable first; unknown families sink past the end (add new families here or their agents sort last). mythos = fable's same-tier Project-Glasswing sibling, pre-added so a future mythos conf can't repeat the fable-sorted-last bug
_FAMILY_RE = re.compile(rf"({'|'.join(ORDERED_MODEL_FAMILIES)})-(\d+)(?:-(\d+))?")


def parse_model_id(model: str) -> tuple[str, int, int] | None:
    """Extract (family, major, minor) from a model ID like 'claude-opus-4-7'.
    Returns None when no recognized family is present. The regex's family
    alternation is derived from ORDERED_MODEL_FAMILIES, so a new family means
    one list-entry change — no parallel regex to update."""
    m = _FAMILY_RE.search(model)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3) or 0)


def engine_sort_key(model: str) -> tuple[int, tuple[int, int]]:
    """Ordering fragment for an engine's ANTHROPIC_MODEL: family rank
    (ORDERED_MODEL_FAMILIES — fable first, haiku last, unknown past the end),
    then version descending. Callers append their own name tiebreak."""
    family, major, minor = parse_model_id(model) or (None, 0, 0)
    return (ordering_index_or_end(family, ORDERED_MODEL_FAMILIES), (-major, -minor))


def _read_conf(conf_path: Path) -> dict[str, str]:
    """Parse an `engine.conf` (dotenv KEY=VALUE) into a dict, dropping
    valueless keys (a bare `KEY` line dotenv reads as None — forwarding
    `-e KEY=None` was never meaningful). Missing file → empty (a nesting-only
    engine that just adds description, or inherits everything)."""
    if not conf_path.is_file():
        return {}
    return {k: v for k, v in dotenv_values(conf_path).items() if v is not None}


@dataclass(frozen=True)
class Engine(Tag):
    """A discovered engine. `conf` is the EFFECTIVE (inheritance-merged) env
    dict, stored as sorted pairs for hashability; read it via `conf_map`."""
    parentheses: ClassVar[tuple[str, str]] = ("(", ")")
    root: ClassVar[str] = "engine"
    nutshell: ClassVar[str] = "how hard the agent THINKS"

    conf: tuple[tuple[str, str], ...] = ()

    @property
    def conf_map(self) -> dict[str, str]:
        """The effective conf as a dict (the launch-time engine handler turns
        this into `-e KEY=VALUE` args + the `--effort` flag)."""
        return dict(self.conf)

    @classmethod
    def scan(cls, agents_dir: Path) -> list["Engine"]:
        """Discover every engine under `agents/engine/`, computing each one's
        inheritance-merged conf. The tree walk yields parents before children,
        so a child's effective conf = its parent's effective conf overlaid
        with the child's own `engine.conf`."""
        effective: dict[str, dict[str, str]] = {}
        out: list[Engine] = []
        for tag_dir, ancestors in walk_tag_tree(agents_dir / cls.root):
            own = _read_conf(tag_dir / CONF_FILE)
            parent = effective[ancestors[-1]] if ancestors else {}
            merged = {**parent, **own}
            effective[tag_dir.name] = merged
            fields = common_fields(tag_dir)
            fields.pop("_info")
            out.append(cls(**fields, conf=tuple(sorted(merged.items()))))
        return out
