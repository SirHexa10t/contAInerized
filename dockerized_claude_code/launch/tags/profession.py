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

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .base import (
    HIDDEN_PREFIX, INFO_FILE, DockerContribution, Tag, TagError,
    common_fields, is_hidden_asset_dir, parse_docker, walk_tag_tree,
)


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

    @classmethod
    def scan(cls, agents_dir: Path) -> list["Profession"]:
        """Discover every offered profession (a tag dir under
        `agents/profession/`, `_`-dirs excluded). Nesting depth becomes
        `requires`: all enclosing profession names."""
        out: list[Profession] = []
        for tag_dir, ancestors in walk_tag_tree(agents_dir / cls.root):
            fields = common_fields(tag_dir)
            fields.pop("_info")
            out.append(cls(**fields, requires=frozenset(ancestors)))
        return out

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
