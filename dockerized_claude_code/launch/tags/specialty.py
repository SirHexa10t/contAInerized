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
from .policy import POLICY_FILE, read_fragment
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
    workspace_readonly: bool = False   # mount /workspace read-only (docker_config.set_container_mounts honors it via Instance.workspace_readonly)
    layer: Layer | None = None
    policy_dir: Path | None = None     # a claimed policy/_<name>/ hidden fragment — merged into settings.json alongside the selected policies

    def load_fragment(self) -> dict[str, Any]:
        """The settings fragment this specialty owns via a claimed
        `policy/_<name>/policy.json`, or `{}` when it claims none. Same shape
        as `Policy.load_fragment` so `install_settings` merges both uniformly
        (that's how `{ro}` contributes its Write/Edit/NotebookEdit deny)."""
        return read_fragment(self.policy_dir / POLICY_FILE) if self.policy_dir else {}

    @classmethod
    def scan(cls, agents_dir: Path, layers: dict[str, Layer],
             policy_fragments: dict[str, Path]) -> list["Specialty"]:
        """Discover every specialty (a dir with `tag.info` directly under
        `agents/specialty/`). Kind-specific keys: `warn` (bool), `claude_args`
        (list), `workspace_readonly` (bool — mount the workspace `:ro`). A
        specialty named the same as a discovered hidden layer claims that
        layer (inheriting its `requires` + image contribution); one named the
        same as a hidden policy fragment (`policy/_<name>/`) claims that
        settings fragment."""
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
            workspace_readonly = bool(info.get("workspace_readonly", False))
            layer = layers.get(tag_dir.name)
            out.append(cls(
                **fields,
                requires=(layer.requires if layer else frozenset()),
                warn=warn,
                claude_args=claude_args,
                workspace_readonly=workspace_readonly,
                layer=layer,
                policy_dir=policy_fragments.get(tag_dir.name),
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
