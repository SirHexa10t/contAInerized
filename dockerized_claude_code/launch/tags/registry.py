"""Tag registry — discover the whole `agents/` tree, validate it, look tags up.

`scan_all(agents_dir)` runs every kind's scanner (plus hidden-layer and
combos discovery), assembles a `Registry`, and validates it as a whole before
returning — so a defective tree aborts at startup with a `TagError` naming
the fault, never mid-launch. `Registry` is then the read-only source of truth
every consumer (form, picker, launch stages) will query in later phases; in
P0 nothing live consumes it yet.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .base import Tag, TagError
from .engine import Engine
from .lego import AgentBuild
from .policy import Policy
from .profession import Layer, Profession
from .specialty import Combo, Specialty, scan_combos


@dataclass
class Registry:
    """Every discovered tag, grouped by kind, plus combo warnings. Names are
    unique across ALL kinds (validated), so `get`/`kind_of` search the union."""
    engines: dict[str, Engine] = field(default_factory=dict)
    professions: dict[str, Profession] = field(default_factory=dict)
    specialties: dict[str, Specialty] = field(default_factory=dict)
    policies: dict[str, Policy] = field(default_factory=dict)
    combos: tuple[Combo, ...] = ()

    def _kind_maps(self) -> list[tuple[str, Mapping[str, Tag]]]:
        # Mapping (not dict) so the per-kind dicts (dict[str, Engine], …) are
        # assignable here — dict's value type is invariant, Mapping's covariant.
        return [
            ("engine", self.engines), ("profession", self.professions),
            ("specialty", self.specialties), ("policy", self.policies),
        ]

    def all_names(self) -> set[str]:
        return {n for _, m in self._kind_maps() for n in m}

    def get_all(self) -> list[Tag]:
        """Every discovered tag across all kinds, engine → policy order."""
        return [t for _, m in self._kind_maps() for t in m.values()]

    def get(self, name: str) -> Tag | None:
        for _, m in self._kind_maps():
            if name in m:
                return m[name]
        return None

    def kind_of(self, name: str) -> str | None:
        for kind, m in self._kind_maps():
            if name in m:
                return kind
        return None

    def validate_build(self, build: AgentBuild, source: Path) -> None:
        """Fail loud if a `.lego` (or, later, an instance-store entry) names a
        tag that doesn't exist, or puts a tag on the wrong axis (a profession
        listed under `specialties`, etc.). `source` names the file in errors."""
        axis_checks = [
            (build.engine and [build.engine] or [], "engine", "engine"),
            (build.professions, "profession", "professions"),
            (build.specialties, "specialty", "specialties"),
            (build.policies, "policy", "policies"),
        ]
        for names, want_kind, axis in axis_checks:
            for name in names:
                actual = self.kind_of(name)
                if actual is None:
                    raise TagError(f"{source}: {axis} references unknown tag '{name}'")
                if actual != want_kind:
                    raise TagError(f"{source}: '{name}' is a {actual}, not a {want_kind} — wrong axis")


def _by_name(tags: list, kind_label: str) -> dict:
    """Index discovered tags by name, raising on a within-kind duplicate
    (two dirs resolving to the same name — possible via nesting)."""
    out: dict = {}
    for tag in tags:
        if tag.name in out:
            raise TagError(f"duplicate {kind_label} '{tag.name}' ({out[tag.name].path} and {tag.path})")
        out[tag.name] = tag
    return out


def scan_all(agents_dir: Path) -> Registry:
    """Discover + validate the whole tree. Order matters: hidden layers first
    (specialties consume them), then the four kinds, then combos; finally the
    cross-cutting validation pass."""
    layers = Profession.discover_layers(agents_dir)
    reg = Registry(
        engines=_by_name(Engine.scan(agents_dir), "engine"),
        professions=_by_name(Profession.scan(agents_dir), "profession"),
        specialties=_by_name(Specialty.scan(agents_dir, layers), "specialty"),
        policies=_by_name(Policy.scan(agents_dir), "policy"),
        combos=tuple(scan_combos(agents_dir)),
    )
    _validate(reg, layers)
    return reg


def _validate(reg: Registry, layers: dict[str, Layer]) -> None:
    """Cross-cutting checks no single scanner can make (fail loud on the
    first fault):
      - names unique across ALL kinds (one namespace);
      - every hidden layer is claimed by exactly one specialty;
      - `requires` (tree-derived) resolve to real professions;
      - `wants` and combo references resolve to real tags (any kind)."""
    # Global name uniqueness across kinds.
    seen: dict[str, str] = {}
    for kind, m in reg._kind_maps():
        for name in m:
            if name in seen:
                raise TagError(f"tag name '{name}' used by both {seen[name]} and {kind} — names must be unique across kinds")
            seen[name] = kind

    # Every hidden layer must be claimed by a same-named specialty.
    for layer_name in layers:
        if layer_name not in reg.specialties:
            raise TagError(f"hidden layer '_{layer_name}' has no matching specialty '{layer_name}'")

    # requires resolve to professions (professions nest under professions;
    # specialties inherit their layer's profession ancestors).
    for tag in [*reg.professions.values(), *reg.specialties.values()]:
        for req in tag.requires:
            if req not in reg.professions:
                raise TagError(f"{tag.path}: requires unknown profession '{req}'")

    # wants + combos reference any real tag.
    known = reg.all_names()
    for tag in reg.get_all():
        for wanted in tag.wants_map:
            if wanted not in known:
                raise TagError(f"{tag.path}: wants unknown tag '{wanted}'")
    for combo in reg.combos:
        for name in combo.tags:
            if name not in known:
                raise TagError(f"combos.info: combo references unknown tag '{name}'")
