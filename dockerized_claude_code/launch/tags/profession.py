"""Profession kind — `[ ]` — "capability: what tools it can USE".

A profession is an image layer: a `Dockerfile` (+ optional `tag.docker`,
`addendum.md`) under `agents/profession/`. **Folder nesting encodes
requirement** — the tree *is* the dependency declaration:

    profession/code/          → [code]
    profession/code/web/      → [web], requires {code}

Specialties may own an image layer that is *part-profession* (dood is a real
image layer, but shouldn't be offered as a standalone profession). Such a
layer lives in the profession tree under a `_`-prefixed dir
(`profession/code/_dood/`): hidden from profession discovery, its tree
position still supplying the owning specialty's `requires`. `discover_layers`
finds these; `Specialty.scan` consumes them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .base import (
    HIDDEN_PREFIX, INFO_FILE, DockerContribution, Tag, TagError,
    common_fields, is_hidden_asset_dir, parse_docker, read_toml, walk_tag_tree,
)

TOOLKIT_FILE = "template.form"


_BUILD_ARG_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True)
class ToolkitEntry:
    """One row of a profession's `template.form` — a tool shown in the "Edit
    Toolkits" form. `run_command` + `language` are flavor shown in the focused
    row's panel (how you invoke it; what kind of language it is). `default`
    seeds a fresh `~/.claude-agents/<profession>_profile.toml`; the user's
    file wins after that (see `tags/toolkit_profile.py`).

    Two shapes:
      - toggleable (the norm): `build_arg` names the Dockerfile `ARG` its
        toggle drives — RIGHT HERE, so the install definition is local to the
        tag dir. `approx_size_mb` is its footprint.
      - `locked = True`: informational, un-toggleable (grayed in the form) —
        a tool that's present regardless of choice (e.g. Python, baked into
        the base image). No `build_arg` (nothing to gate) and no
        `approx_size_mb` (it's not an added install). `default` is its shown
        check state.

    Scope: language toolchains — the creds-driven service CLIs are NOT
    manifest entries (their install is creds-presence,
    `container_env.install_creds_flags`), and the mandatory
    compiler/linker/headers are unconditional Dockerfile installs with no
    entry anywhere."""
    key: str
    description: str
    run_command: str
    language: str
    default: bool
    locked: bool = False
    approx_size_mb: int | None = None
    build_arg: str = ""


def _load_toolkit(path: Path) -> dict[str, ToolkitEntry]:
    """Parse a `template.form` into `{key: ToolkitEntry}`. Fail loud on a
    missing required field, a malformed `build_arg` (it becomes a docker
    `--build-arg` verbatim), or two entries claiming the same build-arg — a
    manifest typo should surface at scan time, not silently drop a tool from
    the "Edit Toolkits" form or no-op its toggle. Locked entries carry
    neither `build_arg` nor `approx_size_mb` (see ToolkitEntry)."""
    out: dict[str, ToolkitEntry] = {}
    claimed: dict[str, str] = {}
    for key, fields in read_toml(path).items():
        locked = bool(fields.get("locked", False))
        try:
            entry = ToolkitEntry(
                key=key,
                description=fields["description"],
                run_command=fields["run_command"],
                language=fields["language"],
                default=fields["default"],
                locked=locked,
                approx_size_mb=fields.get("approx_size_mb") if locked else fields["approx_size_mb"],
                build_arg=fields.get("build_arg", "") if locked else fields["build_arg"],
            )
        except KeyError as exc:
            raise TagError(f"{path}: entry '{key}' missing required field {exc}") from exc
        if not locked:
            if not _BUILD_ARG_RE.match(entry.build_arg):
                raise TagError(f"{path}: entry '{key}' build_arg {entry.build_arg!r} is not a valid ARG name (expected [A-Z][A-Z0-9_]*)")
            if entry.build_arg in claimed:
                raise TagError(f"{path}: entries '{claimed[entry.build_arg]}' and '{key}' both claim build_arg '{entry.build_arg}'")
            claimed[entry.build_arg] = key
        out[key] = entry
    return out


@dataclass(frozen=True)
class Layer:
    """A specialty-owned image layer discovered in the profession tree under a
    `_<name>` dir. `requires` is the set of professions enclosing it (so
    `profession/code/_dood/` ⇒ requires {code}); `docker` is the layer's own
    `tag.docker` (build-args, socket mount for dood, …). Consumed by
    `Specialty.scan` to fill the specialty's `requires` + image contribution."""
    name: str
    path: Path
    requires: frozenset[str] = frozenset()
    docker: DockerContribution | None = None


@dataclass(frozen=True)
class Profession(Tag):
    parentheses: ClassVar[tuple[str, str]] = ("[", "]")
    root: ClassVar[str] = "profession"
    nutshell: ClassVar[str] = "capability: what tools it can USE"

    toolkit_path: Path | None = None   # this profession's template.form, if it has one

    @classmethod
    def scan(cls, agents_dir: Path) -> list["Profession"]:
        """Discover every offered profession (a tag dir under
        `agents/profession/`, `_`-dirs excluded). Nesting depth becomes
        `requires`: all enclosing profession names. A sibling `template.form`
        (like `tag.docker`, optional) marks the profession as configurable."""
        out: list[Profession] = []
        for tag_dir, ancestors in walk_tag_tree(agents_dir / cls.root):
            fields = common_fields(tag_dir)
            fields.pop("_info")
            toolkit_path = tag_dir / TOOLKIT_FILE
            out.append(cls(**fields, requires=frozenset(ancestors),
                           toolkit_path=toolkit_path if toolkit_path.is_file() else None))
        return out

    def load_toolkit(self) -> dict[str, ToolkitEntry]:
        """This profession's configurable installs — `{}` if it has no
        `template.form`. Parsed fresh each call (small file, no cache) so the
        picker's "Edit Toolkits" menu always sees a just-edited manifest."""
        return _load_toolkit(self.toolkit_path) if self.toolkit_path else {}

    @classmethod
    def discover_layers(cls, agents_dir: Path) -> dict[str, Layer]:
        """Find every `_<name>` hidden asset dir in the profession tree and
        return `{name: Layer}`. Each layer's `requires` is the set of
        enclosing professions. Fail loud on: a duplicate layer name, or a
        hidden dir that (illegally) contains a `tag.info` — a hidden dir is an
        asset, never itself a tag."""
        root = agents_dir / cls.root
        layers: dict[str, Layer] = {}

        def rec(d: Path, ancestors: tuple[str, ...]) -> None:
            for child in sorted(d.iterdir(), key=lambda p: p.name):
                if not child.is_dir():
                    continue
                if not is_hidden_asset_dir(child):        # tag dir (stray dirs already raised) → descend
                    rec(child, ancestors + (child.name,))
                    continue
                name = child.name[len(HIDDEN_PREFIX):]    # `_<name>` → a specialty-claimed image layer
                if not name:
                    raise TagError(f"{child}: hidden dir needs a name after '{HIDDEN_PREFIX}'")
                if name in layers:
                    raise TagError(f"duplicate hidden layer '{name}' ({layers[name].path} and {child})")
                stray = next(child.rglob(INFO_FILE), None)
                if stray is not None:
                    raise TagError(f"{child}: hidden asset dir must not contain a tag ({stray})")
                layers[name] = Layer(name=name, path=child,
                                     requires=frozenset(ancestors), docker=parse_docker(child))

        if root.is_dir():
            rec(root, ())
        return layers
