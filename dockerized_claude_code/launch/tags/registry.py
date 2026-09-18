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

from ..paths import COMMANDS_DIR_NAME
from .addendums import KNOWN_PLACEHOLDERS, referenced_placeholders
from .base import Tag, TagError, is_scope, scope_note
from .ai import Ai
from .engine import Engine
from .harness import Harness
from .lego import AgentBuild
from .policy import Policy
from .profession import Layer, Profession
from .specialty import Combo, Specialty, scan_combos

__all__ = ["Registry", "TagProblem", "scan_all"]


@dataclass(frozen=True)
class TagProblem:
    """A name in a stored build that cannot stand there — `unknown` (typo, or
    a tag renamed/removed since the instance was set up), `wrong_axis` (a
    real tag of another kind), `incompatible` (a real harness that cannot run
    the entry's AI), or `forbidden` (a real tag whose `forbid_on` names the
    scope the build lives in — `{dood}` as a member's own tag). Carries the
    display punctuation of the EXPECTED kind (so `{web}` renders in the
    profession's brackets even though `web` no longer exists) and the sorted
    list of valid names of that kind — for `incompatible`, the harnesses that
    CAN run the AI — for the "did you mean one of these" report; for
    `forbidden`, `hint` says where the tag can go instead (`scope_note`).
    Produced by `Registry.resolve_store_build`."""
    name: str
    axis: str                       # store key: professions / specialties / policies / engine / ai / harness
    kind: str                       # expected kind label (profession / specialty / policy / engine / ai / harness)
    parentheses: tuple[str, str]
    reason: str                     # "unknown" | "wrong_axis" | "incompatible" | "forbidden"
    actual_kind: str | None         # the kind it actually is, when reason == "wrong_axis"
    options: tuple[str, ...]        # valid names of the expected kind, sorted
    hint: str = ""                  # for "forbidden": where the tag can go instead

    @property
    def label(self) -> str:
        o, c = self.parentheses
        return f"{o}{self.name}{c}"


@dataclass
class Registry:
    """Every discovered tag, grouped by kind, plus combo warnings. Names are
    unique across ALL kinds (validated), so `get`/`kind_of` search the union."""
    ais: dict[str, Ai] = field(default_factory=dict)
    harnesses: dict[str, Harness] = field(default_factory=dict)
    engines: dict[str, Engine] = field(default_factory=dict)
    professions: dict[str, Profession] = field(default_factory=dict)
    specialties: dict[str, Specialty] = field(default_factory=dict)
    policies: dict[str, Policy] = field(default_factory=dict)
    combos: tuple[Combo, ...] = ()

    def _kind_maps(self) -> list[tuple[str, Mapping[str, Tag]]]:
        # Mapping (not dict) so the per-kind dicts (dict[str, Engine], …) are
        # assignable here — dict's value type is invariant, Mapping's covariant.
        return [
            ("ai", self.ais), ("harness", self.harnesses), ("engine", self.engines), ("profession", self.professions),
            ("specialty", self.specialties), ("policy", self.policies),
        ]

    @property
    def default_ai(self) -> Ai | None:
        """The AI an instance runs when its build names none — the member
        marked `default = true` (the scan holds the kind to exactly one);
        None only for a tree without an `ai/` shelf (fixtures)."""
        return next((ai for ai in self.ais.values() if ai.default), None)

    def ai_for(self, build: AgentBuild) -> Ai | None:
        """The AI a build runs on: its named one, else the default."""
        return self.ais[build.ai] if build.ai else self.default_ai

    def harness_for(self, build: AgentBuild) -> Harness | None:
        """The harness a build runs in: its named one, else its AI's default
        harness (the AI's `harness` key); None only for a tree without the
        shelves (fixtures)."""
        if build.harness:
            return self.harnesses[build.harness]
        ai = self.ai_for(build)
        return self.harnesses.get(ai.harness) if ai else None

    def harnesses_running(self, ai_name: str) -> tuple[str, ...]:
        """The harness member names that can run the AI `ai_name`, sorted."""
        return tuple(sorted(name for name, h in self.harnesses.items() if h.runs(ai_name)))

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
    # kind definitions. AI, harness and engine are 0-or-1 axes; the three
    # others are lists.
    def _axis_specs(self) -> "list[tuple[str, type[Tag], Mapping[str, Tag]]]":
        return [
            ("ai", Ai, self.ais),
            ("harness", Harness, self.harnesses),
            ("engine", Engine, self.engines),
            ("professions", Profession, self.professions),
            ("specialties", Specialty, self.specialties),
            ("policies", Policy, self.policies),
        ]

    @staticmethod
    def _axis_names(build: AgentBuild, axis: str) -> list[str]:
        if axis == "ai":
            return [build.ai] if build.ai else []
        if axis == "harness":
            return [build.harness] if build.harness else []
        if axis == "engine":
            return [build.engine] if build.engine else []
        return list(getattr(build, axis))

    def forbidden(self, build: AgentBuild, scope: str) -> list[Tag]:
        """The tags among `build`'s three list axes that cannot be picked
        into `scope` (their `forbid_on` names it), in axis order — the ONE
        question the form, the launch, the creation flows and the audit ask.
        Names that resolve to nothing are not this method's concern
        (`resolve_store_build` reports those)."""
        is_scope(scope)
        out: list[Tag] = []
        for names in (build.professions, build.specialties, build.policies):
            for name in names:
                tag = self.get(name)
                if tag is not None and scope in tag.forbid_on:
                    out.append(tag)
        return out

    def validate_build(self, build: AgentBuild, source: Path | str, *, scope: str | None) -> None:
        """Fail loud if a `.lego` names a tag that doesn't exist, or puts a
        tag on the wrong axis (a profession listed under `specialties`, etc.),
        or picks a tag its `forbid_on` keeps out of `scope` — the scope the
        build is about to live in; `None` skips only that check (the audit
        reports it as its own finding kind). Required, not defaulted: every
        caller decides which scope it is validating for. `source` names the
        file in errors. Used for SHIPPED `.lego` files, whose correctness is
        a repo invariant — a fault is a bug, so raising is right.
        User-editable `instances.toml` entries go through
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
        # The pair must work: a harness runs only the AIs it names.
        ai, harness = self.ai_for(build), self.harness_for(build)
        if build.harness and ai is not None and harness is not None and not harness.runs(ai.name):
            raise TagError(f"{source}: harness '{harness.name}' cannot run the '{ai.name}' AI — "
                           f"it runs {', '.join(harness.ais)}; the harnesses that can: {', '.join(self.harnesses_running(ai.name)) or 'none'}")
        if scope is not None:
            for tag in self.forbidden(build, scope):
                elsewhere = ", ".join(s for s in ("solo", "cluster", "member") if s not in tag.forbid_on)
                raise TagError(f"{source}: {tag.label} cannot be a {scope} build's tag — {scope_note(tag, scope)} "
                               f"(the build itself is fine {'for: ' + elsewhere if elsewhere else 'nowhere'})")

    def resolve_store_build(self, build: AgentBuild, *, scope: str | None) -> "tuple[AgentBuild, list[TagProblem]]":
        """Split a stored build (an `instances.toml` entry — user-editable, so
        possibly stale after a tag rename or a typo) into a CLEANED build
        keeping only names that resolve to their axis's kind and may stand in
        `scope` — the scope the build lives in (`None` skips only that
        check); required, not defaulted, because a member's stored build is
        the UNION of the cluster's tags and its own, and resolving that union
        at scope `member` would flag a legal cluster-wide `{dood}` on every
        member (`Cluster.member_instance` resolves the two halves apart) —
        plus a `TagProblem` for every name dropped: reason `forbidden` for a
        real tag whose `forbid_on` names the scope, with `hint` saying where
        it can go. Never raises: a bad stored tag must surface as a blocked,
        flagged instance in the picker, not a crash. (Shipped `.lego` files
        use the raising `validate_build`.)"""
        if scope is not None:
            is_scope(scope)
        kept: dict[str, list[str]] = {"professions": [], "specialties": [], "policies": []}
        kept_engine: str | None = None
        kept_ai: str | None = None
        kept_harness: str | None = None
        problems: list[TagProblem] = []
        for axis, cls, kind_map in self._axis_specs():
            for name in self._axis_names(build, axis):
                actual = self.kind_of(name)
                if actual == cls.root and getattr(self.get(name), "always_on", False):
                    continue   # static tag in an old/hand-edited entry — applied anyway; drop the mention silently
                if actual == cls.root:
                    tag = self.get(name)
                    if scope is not None and tag is not None and scope in tag.forbid_on:
                        problems.append(TagProblem(
                            name=name, axis=axis, kind=cls.root, parentheses=cls.parentheses,
                            reason="forbidden", actual_kind=None, options=(), hint=scope_note(tag, scope)))
                    elif axis == "engine":
                        kept_engine = name
                    elif axis == "ai":
                        kept_ai = name
                    elif axis == "harness":
                        kept_harness = name
                    else:
                        kept[axis].append(name)
                else:
                    problems.append(TagProblem(
                        name=name, axis=axis, kind=cls.root, parentheses=cls.parentheses,
                        reason="unknown" if actual is None else "wrong_axis",
                        actual_kind=actual, options=tuple(sorted(kind_map)),
                    ))
        # A real harness that cannot run the entry's AI is dropped too — the
        # instance falls back to the AI's own harness — and reported, with the
        # harnesses that could take its place as the options.
        ai = self.ai_for(AgentBuild(ai=kept_ai))
        if kept_harness is not None and ai is not None and not self.harnesses[kept_harness].runs(ai.name):
            problems.append(TagProblem(
                name=kept_harness, axis="harness", kind=Harness.root, parentheses=Harness.parentheses,
                reason="incompatible", actual_kind=None, options=self.harnesses_running(ai.name)))
            kept_harness = None
        cleaned = AgentBuild(ai=kept_ai, harness=kept_harness, engine=kept_engine,
                             professions=tuple(kept["professions"]),
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
    six kinds, then combos; finally the cross-cutting validation pass."""
    layers = Profession.discover_layers(agents_dir)
    fragments = Policy.discover_fragments(agents_dir)
    reg = Registry(
        ais=_by_name(Ai.scan(agents_dir), "ai"),
        harnesses=_by_name(Harness.scan(agents_dir), "harness"),
        engines=_by_name(Engine.scan(agents_dir), "engine"),
        professions=_by_name(Profession.scan(agents_dir), "profession"),
        specialties=_by_name(Specialty.scan(agents_dir, layers, fragments), "specialty"),
        policies=_by_name(Policy.scan(agents_dir), "policy"),
        combos=tuple(scan_combos(agents_dir)),
    )
    _validate(reg, layers, fragments, agents_dir)
    return reg


def _validate(reg: Registry, layers: dict[str, Layer], fragments: dict[str, Path],
              agents_dir: Path) -> None:
    """Cross-cutting checks no single scanner can make (fail loud on the
    first fault):
      - names unique across ALL kinds (one namespace);
      - every hidden layer / policy fragment is claimed by exactly one specialty;
      - `requires` (tree-derived) resolve to real tags of the right kind:
        professions require professions; specialties additionally require
        specialties (their own tree nests too — `{manager}` inside `cowork/`);
      - `wants` and combo references resolve to real tags (any kind);
      - every declared command name resolves to a `commands/<name>.md` file;
      - every engine's capability standard is one the AIs define;
      - every AI's default harness is a member that runs it, every harness
        runs only AI members, and every `plan_harnesses` name is a harness
        that runs that AI;
      - a tag with a container-level mechanism (tag.docker's container
        fields, `workspace_readonly`) forbids `member`."""
    # Every engine's standard is one the AIs answer (the shared file's
    # quarters plus the ends) — a budget naming a quarter no efforts.tiers has
    # a tier for would otherwise fail at render time, in a launch.
    if reg.ais:
        defined = next(iter(reg.ais.values())).standards
        for engine in reg.engines.values():
            if engine.budget.effort_tier is not None and engine.budget.effort_tier not in defined:
                raise TagError(f"{engine.path}: effort_tier {engine.budget.effort_tier!r} is not one the AIs define "
                               f"({', '.join(defined)} — agents/ai/capability.standards)")

    # AI ↔ harness: an AI's default harness exists and lists the AI; a
    # harness lists only real AIs. Both directions are data, so a rename on
    # either shelf fails here, not in a launch.
    for ai in reg.ais.values():
        harness = reg.harnesses.get(ai.harness)
        if harness is None:
            raise TagError(f"{ai.path}: harness {ai.harness!r} is not a member of agents/harness/ "
                           f"(members: {', '.join(sorted(reg.harnesses)) or 'none'})")
        if not harness.runs(ai.name):
            raise TagError(f"{ai.path}: its default harness {ai.harness!r} does not list {ai.name!r} among the AIs it runs ({', '.join(harness.ais)})")
    for harness in reg.harnesses.values():
        for name in harness.ais:
            if name not in reg.ais:
                raise TagError(f"{harness.path}: ais names unknown AI {name!r} (members: {', '.join(sorted(reg.ais)) or 'none'})")
    # An AI's plan_harnesses must be harnesses that can actually run it — a
    # name that cannot is a claim about a pairing that does not exist, and the
    # picker's billing warning would then be derived from nonsense.
    for ai in reg.ais.values():
        for name in ai.plan_harnesses:
            harness = reg.harnesses.get(name)
            if harness is None:
                raise TagError(f"{ai.path}: plan_harnesses names {name!r}, which is not a member of agents/harness/ "
                               f"(members: {', '.join(sorted(reg.harnesses)) or 'none'})")
            if not harness.runs(ai.name):
                raise TagError(f"{ai.path}: plan_harnesses names {name!r}, which does not run {ai.name!r} "
                               f"(it runs {', '.join(harness.ais)})")

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

    # requires resolve per kind. A profession's come only from its own tree
    # (professions nest under professions). A specialty's come from two
    # sources with two shapes: tree ancestors (specialties — its own tree
    # nests too) and a claimed layer's ancestors (professions).
    for prof in reg.professions.values():
        for req in prof.requires:
            if req not in reg.professions:
                raise TagError(f"{prof.path}: requires unknown profession '{req}'")
    for spec in reg.specialties.values():
        for req in spec.requires:
            if req not in reg.professions and req not in reg.specialties:
                raise TagError(f"{spec.path}: requires unknown tag '{req}' "
                               f"(not a profession or specialty)")

    # A container-level mechanism is ONE setting for the one container a
    # cluster is, so its tag can be the cluster's but never a member's own,
    # and it must say so in data — the declaration is what the form greys out
    # and the launch refuses; the rule keeps the physics and the declaration
    # from drifting (2026-09-16). The mechanisms, exhaustively: a tag.docker
    # contribution with cap_add / mounts / env_forward / entrypoint (the tag's
    # own or its claimed layer's), and the specialty key `workspace_readonly`
    # (one /workspace mount). NOT a mechanism: an image layer — a layer-
    # claiming tag joins the union image, cluster-wide in effect but not
    # impossible per member; nor a harness's Dockerfile ENTRYPOINT — an image
    # fact of the tail layer (its tag.docker is [build]-only), and a harness
    # is per member by design. A new tag.info key with container reach joins
    # this list deliberately.
    for tag in reg.get_all():
        layer = getattr(tag, "layer", None)
        contributions = [c for c in (tag.docker, layer.docker if layer else None) if c]
        container_wide = any(c.container_level for c in contributions) or bool(getattr(tag, "workspace_readonly", False))
        if container_wide and "member" not in tag.forbid_on:
            raise TagError(f"{tag.path}/tag.info: this tag reaches the CONTAINER (cap_add / mounts / env_forward / "
                           f"entrypoint in tag.docker, or workspace_readonly), which is set once per container, so it "
                           f"must declare forbid_on = [\"member\"] — a cluster may carry it cluster-wide; a member alone cannot")

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

    # Declared commands resolve to real files — a typo'd name would otherwise
    # silently assemble an instance missing the command it was tagged for.
    # Validated against THIS scan's tree root (not the repo constant), so a
    # fixture tree in a test carries its own commands/ dir.
    commands_dir = agents_dir / COMMANDS_DIR_NAME
    for tag in reg.get_all():
        for command in tag.commands:
            if not (commands_dir / f"{command}.md").is_file():
                available = sorted(p.stem for p in commands_dir.glob("*.md"))
                raise TagError(
                    f"{tag.path}: commands references unknown command '{command}' "
                    f"— no {COMMANDS_DIR_NAME}/{command}.md; available: {available}")

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
