"""Tag registry — discover the whole `agents/` tree, validate it, look tags up.

`scan_all(agents_dir)` runs every kind's scanner (plus hidden-layer and
combos discovery), assembles a `Registry`, and validates it as a whole before
returning — so a defective tree aborts at startup with a `TagError` naming
the fault, never mid-launch. `Registry` is the read-only source of truth
every consumer (form, picker, launch stages) queries.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .addendums import KNOWN_PLACEHOLDERS, referenced_placeholders
from .base import Tag, TagError
from .engine import Engine
from .lego import AgentBuild
from .policy import Policy
from .profession import Layer, Profession
from .specialty import Combo, Specialty, scan_combos

__all__ = ["Registry", "TagProblem", "scan_all"]


@dataclass(frozen=True)
class TagProblem:
    """A name in an `instances.toml` entry that doesn't resolve to a real tag
    on its axis — either `unknown` (typo, or a tag renamed/removed since the
    instance was set up) or `wrong_axis` (a real tag of another kind). Carries
    the display punctuation of the EXPECTED kind (so `{web}` renders in the
    profession's brackets even though `web` no longer exists) and the sorted
    list of valid names of that kind, for the "did you mean one of these"
    report. Produced by `Registry.resolve_store_build`."""
    name: str
    axis: str                       # store key: professions / specialties / policies / engine
    kind: str                       # expected kind label (profession / specialty / policy / engine)
    parentheses: tuple[str, str]
    reason: str                     # "unknown" | "wrong_axis"
    actual_kind: str | None         # the kind it actually is, when reason == "wrong_axis"
    options: tuple[str, ...]        # valid names of the expected kind, sorted

    @property
    def label(self) -> str:
        o, c = self.parentheses
        return f"{o}{self.name}{c}"


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

    # (store axis key, kind class, its member map). The class supplies the
    # expected kind label (`.root`) and display punctuation (`.parentheses`),
    # so validate_build / resolve_store_build stay single-sourced off the
    # kind definitions. Engine is a 0-or-1 axis; the three others are lists.
    def _axis_specs(self) -> "list[tuple[str, type[Tag], Mapping[str, Tag]]]":
        return [
            ("engine", Engine, self.engines),
            ("professions", Profession, self.professions),
            ("specialties", Specialty, self.specialties),
            ("policies", Policy, self.policies),
        ]

    @staticmethod
    def _axis_names(build: AgentBuild, axis: str) -> list[str]:
        if axis == "engine":
            return [build.engine] if build.engine else []
        return list(getattr(build, axis))

    def validate_build(self, build: AgentBuild, source: Path | str) -> None:
        """Fail loud if a `.lego` names a tag that doesn't exist, or puts a
        tag on the wrong axis (a profession listed under `specialties`, etc.).
        `source` names the file in errors. Used for SHIPPED `.lego` files,
        whose correctness is a repo invariant — a fault is a bug, so raising
        is right. User-editable `instances.toml` entries go through
        `resolve_store_build` instead, which reports rather than raises."""
        for axis, cls, _ in self._axis_specs():
            for name in self._axis_names(build, axis):
                actual = self.kind_of(name)
                if actual is None:
                    raise TagError(f"{source}: {axis} references unknown tag '{name}'")
                if actual != cls.root:
                    raise TagError(f"{source}: '{name}' is a {actual}, not a {cls.root} — wrong axis")
                if getattr(self.get(name), "always_on", False):
                    raise TagError(f"{source}: '{name}' is always-on — applied to every instance automatically; don't list it")

    def resolve_store_build(self, build: AgentBuild) -> "tuple[AgentBuild, list[TagProblem]]":
        """Split a stored build (an `instances.toml` entry — user-editable, so
        possibly stale after a tag rename or a typo) into a CLEANED build
        keeping only names that resolve to their axis's kind, plus a
        `TagProblem` for every name dropped. Never raises: a bad stored tag
        must surface as a blocked, flagged instance in the picker, not a
        crash. (Shipped `.lego` files use the raising `validate_build`.)"""
        kept: dict[str, list[str]] = {"professions": [], "specialties": [], "policies": []}
        kept_engine: str | None = None
        problems: list[TagProblem] = []
        for axis, cls, kind_map in self._axis_specs():
            for name in self._axis_names(build, axis):
                actual = self.kind_of(name)
                if actual == cls.root and getattr(self.get(name), "always_on", False):
                    continue   # static tag in an old/hand-edited entry — applied anyway; drop the mention silently
                if actual == cls.root:
                    if axis == "engine":
                        kept_engine = name
                    else:
                        kept[axis].append(name)
                else:
                    problems.append(TagProblem(
                        name=name, axis=axis, kind=cls.root, parentheses=cls.parentheses,
                        reason="unknown" if actual is None else "wrong_axis",
                        actual_kind=actual, options=tuple(sorted(kind_map)),
                    ))
        cleaned = AgentBuild(engine=kept_engine, professions=tuple(kept["professions"]),
                             specialties=tuple(kept["specialties"]), policies=tuple(kept["policies"]))
        return cleaned, problems


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
    """Discover + validate the whole tree. Order matters: hidden assets first
    (image layers + policy fragments — specialties consume them), then the
    four kinds, then combos; finally the cross-cutting validation pass."""
    layers = Profession.discover_layers(agents_dir)
    fragments = Policy.discover_fragments(agents_dir)
    reg = Registry(
        engines=_by_name(Engine.scan(agents_dir), "engine"),
        professions=_by_name(Profession.scan(agents_dir), "profession"),
        specialties=_by_name(Specialty.scan(agents_dir, layers, fragments), "specialty"),
        policies=_by_name(Policy.scan(agents_dir), "policy"),
        combos=tuple(scan_combos(agents_dir)),
    )
    _validate(reg, layers, fragments)
    return reg


def _validate(reg: Registry, layers: dict[str, Layer], fragments: dict[str, Path]) -> None:
    """Cross-cutting checks no single scanner can make (fail loud on the
    first fault):
      - names unique across ALL kinds (one namespace);
      - every hidden layer / policy fragment is claimed by exactly one specialty;
      - `requires` (tree-derived) resolve to real professions;
      - `wants` and combo references resolve to real tags (any kind)."""
    # Global name uniqueness across kinds.
    seen: dict[str, str] = {}
    for kind, m in reg._kind_maps():
        for name in m:
            if name in seen:
                raise TagError(f"tag name '{name}' used by both {seen[name]} and {kind} — names must be unique across kinds")
            seen[name] = kind

    # Every hidden asset must be claimed by a same-named specialty.
    for layer_name in layers:
        if layer_name not in reg.specialties:
            raise TagError(f"hidden layer '_{layer_name}' has no matching specialty '{layer_name}'")
    for fragment_name in fragments:
        if fragment_name not in reg.specialties:
            raise TagError(f"hidden policy fragment 'policy/_{fragment_name}' has no matching specialty '{fragment_name}'")

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

    # addendum bodies only reference launcher-known placeholders — a typo'd
    # `{cred_cils}` would otherwise crash compose at launch time.
    for tag in reg.get_all():
        if tag.addendum is None:
            continue
        unknown = referenced_placeholders(tag.addendum[1]) - KNOWN_PLACEHOLDERS
        if unknown:
            raise TagError(
                f"{tag.path}: [addendum] body references unknown placeholder(s) "
                f"{sorted(unknown)} — known: {sorted(KNOWN_PLACEHOLDERS)}"
            )
