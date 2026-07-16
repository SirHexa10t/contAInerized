"""Specialty kind — `{ }` — "exceptional ACCESS/RUNNING capabilities".

A specialty is deliberately "several things at once": a Claude Code arg,
container configuration, an optionally-owned image layer, companion requests
— e.g. `{auto}` (skip-permissions arg + a want for firewall), `{dood}` (an
image layer + docker-socket mount), `{firewall}` (iptables/networking).

Defined by `agents/specialty/<name>/tag.info`. A specialty's image layer, if
any, lives in the profession tree as a `_<name>` hidden dir (see
`profession.discover_layers`) — its tree position supplies the specialty's
`requires` (so `{dood}`'s `_dood` under `code/` ⇒ requires {code}).

`agents/specialty/combos.info` (kind-root, not nested) holds multi-tag
entanglement warnings — relations no single tag owns (dood+auto), where
per-tag placement would duplicate the message. One-sided concerns live in
the concerned tag's own `[wants]` table instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from .base import Tag, TagError, common_fields, is_hidden_asset_dir, read_toml
from .profession import Layer

COMBOS_FILE = "combos.info"


@dataclass(frozen=True)
class Combo:
    """One multi-tag entanglement warning from `combos.info`: the set of tag
    names that must ALL be selected for `message` to render (red) in the
    form's warning zone."""
    tags: frozenset[str]
    message: str


@dataclass(frozen=True)
class Specialty(Tag):
    parentheses: ClassVar[tuple[str, str]] = ("{", "}")
    root: ClassVar[str] = "specialty"
    nutshell: ClassVar[str] = "exceptional ACCESS/RUNNING capabilities"

    warn: bool = False
    claude_args: tuple[str, ...] = ()
    layer: Layer | None = None

    @classmethod
    def scan(cls, agents_dir: Path, layers: dict[str, Layer]) -> list["Specialty"]:
        """Discover every specialty (a dir with `tag.info` directly under
        `agents/specialty/`). Kind-specific keys: `warn` (bool), `claude_args`
        (list). A specialty named the same as a discovered hidden layer claims
        that layer — inheriting its `requires` and image contribution."""
        root = agents_dir / cls.root
        out: list[Specialty] = []
        if not root.is_dir():
            return out
        for tag_dir in sorted(root.iterdir(), key=lambda p: p.name):
            if not tag_dir.is_dir() or is_hidden_asset_dir(tag_dir):
                continue   # `_`-dir → skip; a stray non-tag dir raises inside is_hidden_asset_dir
            fields = common_fields(tag_dir)
            info: dict[str, Any] = fields.pop("_info")
            warn = bool(info.get("warn", False))
            claude_args = tuple(info.get("claude_args", []))
            layer = layers.get(tag_dir.name)
            out.append(cls(
                **fields,
                requires=(layer.requires if layer else frozenset()),
                warn=warn,
                claude_args=claude_args,
                layer=layer,
            ))
        return out


def scan_combos(agents_dir: Path) -> list[Combo]:
    """Parse `agents/specialty/combos.info` into `Combo`s. Absent file → no
    combos. Each `[warnings]` key is a `+`-joined set of ≥2 tag names
    ("dood + auto"); the value is the warning message. Name existence is
    validated cross-kind by the registry, not here."""
    combos_path = agents_dir / Specialty.root / COMBOS_FILE
    if not combos_path.is_file():
        return []
    data = read_toml(combos_path)
    warnings = data.get("warnings", {})
    if not isinstance(warnings, dict):
        raise TagError(f"{combos_path}: [warnings] must be a table")
    out: list[Combo] = []
    for key, message in warnings.items():
        names = frozenset(part.strip() for part in key.split("+"))
        if len(names) < 2 or "" in names:
            raise TagError(f"{combos_path}: combo key '{key}' must join ≥2 tag names with '+'")
        if not isinstance(message, str):
            raise TagError(f"{combos_path}: warning for '{key}' must be a string")
        out.append(Combo(tags=names, message=message))
    return out
