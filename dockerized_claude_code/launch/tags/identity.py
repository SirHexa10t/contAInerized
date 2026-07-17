"""Per-launch instance identity.

An `Instance` is a fully-resolved launch: an agent (its `.md` persona) plus
the four axis selections as concrete `Tag` objects (engine + professions +
specialties + policies), plus session / workspace / new-vs-continuing. The
selections come from the agent's `.lego` defaults (a fresh create) or the
per-instance store (a continue), with the create-form editing them in
between.

`image_chain` computes the active-tag chain — `["base", <professions…>,
<specialties…>]`. Professions are ordered so a required profession precedes
its dependents (code before web); specialties follow all professions (their
`requires` reference professions, satisfied by the whole profession group
being ahead).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..file_access import agent_md_index, has_continuable_jsonl, last_history_mtime
from ..paths import instance_state_dir_path, state_md_path
from .base import DockerContribution, Tag
from .engine import Engine
from .lego import AgentBuild
from .policy import Policy
from .profession import Profession
from .registry import Registry
from .specialty import Specialty

SESSION_SEP = "__"


def _topo_professions(professions: tuple[Profession, ...]) -> list[Profession]:
    """Order professions so each follows the professions it requires (code
    before web). Ties broken by name for determinism. A selection with an
    unmet requirement (prevented by the form, but guarded here) appends the
    stragglers rather than looping forever."""
    placed: list[Profession] = []
    placed_names: set[str] = set()
    remaining = sorted(professions, key=lambda p: p.name)
    while remaining:
        ready = [p for p in remaining if p.requires <= placed_names]
        if not ready:
            placed.extend(remaining)   # unsatisfiable requires — shouldn't happen post-validation
            break
        for p in ready:
            placed.append(p)
            placed_names.add(p.name)
            remaining.remove(p)
    return placed


def image_chain(professions: tuple[Profession, ...],
                specialties: tuple[Specialty, ...]) -> list[str]:
    """The active-tag chain: `["base", <professions…>, <specialties…>]`.
    Professions topologically ordered (requirements first); specialties
    alphabetical after all professions. Drives handler dispatch and the
    addendum composition. (Image naming/building uses `Instance.build_steps`
    — the layer-bearing subset — since run-only specialties like {firewall}
    contribute container config but no image content.)"""
    profs = _topo_professions(professions)
    specs = sorted(specialties, key=lambda s: s.name)
    return ["base", *(p.name for p in profs), *(s.name for s in specs)]


@dataclass(frozen=True)
class Agent:
    """A pickable agent (a Create row / a bare-name CLI target): its persona
    `.md` plus the `.lego` build defaults. Promoted to an `Instance` once
    workspace + session + the tag form have answered."""
    name: str
    md_path: Path
    build: AgentBuild


@dataclass(frozen=True)
class Instance:
    """A fully-resolved launch. Frozen; all fields hashable (tag objects are
    frozen, selections are tuples). Identity (`instance`, `state_dir`) and
    launch-shape (`chain`, `build_steps`, `conf`, history probes) hang off
    the one record, so every stage reads the same source of truth."""
    agent: str
    md_path: Path
    session: str
    workspace: str | None
    is_brand_new: bool
    engine: Engine | None
    professions: tuple[Profession, ...] = ()
    specialties: tuple[Specialty, ...] = ()
    policies: tuple[Policy, ...] = ()

    @property
    def instance(self) -> str:
        """Canonical `<agent>__<session>` id — the state-dir name and store key."""
        return f"{self.agent}{SESSION_SEP}{self.session}"

    @property
    def state_dir(self) -> Path:
        return instance_state_dir_path(self.instance)

    @property
    def state_md(self) -> Path:
        return state_md_path(self.state_dir)

    @property
    def chain(self) -> list[str]:
        return image_chain(self.professions, self.specialties)

    @property
    def active_tags(self) -> list[Tag]:
        """Every active tag as objects, in chain order — professions
        (requirement order), specialties (alphabetical), then policies.
        Drives the addendum composition; anything wanting 'all my tags,
        ordered' reads this instead of re-deriving."""
        return [
            *_topo_professions(self.professions),
            *sorted(self.specialties, key=lambda s: s.name),
            *self.policies,
        ]

    @property
    def build(self) -> AgentBuild:
        """The instance's axis selections as name strings — what the store
        persists and the form pre-checks (inverse of resolve_build)."""
        return AgentBuild(
            engine=self.engine.name if self.engine else None,
            professions=tuple(p.name for p in self.professions),
            specialties=tuple(s.name for s in self.specialties),
            policies=tuple(p.name for p in self.policies),
        )

    @property
    def build_steps(self) -> list[tuple[str, Path, DockerContribution | None]]:
        """(name, dockerfile, contribution) per image layer in chain order:
        professions (requirement order), then layer-bearing specialties
        (alphabetical — dood's `_dood` dir). The contribution supplies the
        layer's `[build] arg_forward`. Run-only specialties (auto, firewall)
        don't appear: they contribute container config, not image content.
        Empty for a bare agent (base image only)."""
        out: list[tuple[str, Path, DockerContribution | None]] = [
            (p.name, p.path / "Dockerfile", p.docker)
            for p in _topo_professions(self.professions)
        ]
        out += [(s.name, s.layer.path / "Dockerfile", s.layer.docker)
                for s in sorted(self.specialties, key=lambda s: s.name) if s.layer]
        return out

    @property
    def docker_contributions(self) -> list[DockerContribution]:
        """Active tags' `tag.docker` records in chain order — professions
        first, then each specialty's own record + (if any) its claimed
        layer's. docker_config folds these into build/run flags."""
        out = [p.docker for p in _topo_professions(self.professions) if p.docker]
        for s in sorted(self.specialties, key=lambda s: s.name):
            if s.docker:
                out.append(s.docker)
            if s.layer and s.layer.docker:
                out.append(s.layer.docker)
        return out

    @property
    def unmet_wants(self) -> list[tuple[str, str, str]]:
        """(wanter, wanted, message) for every active tag whose `[wants]`
        names a tag that is NOT active — e.g. {auto} without {firewall}.
        The form renders these live in its warning zone; run.py prints them
        as a launch warning (a want never blocks — it's a request, not a
        requirement)."""
        active_tags = (*self.professions, *self.specialties, *self.policies)
        active = {t.name for t in active_tags}
        return [(t.name, wanted, message)
                for t in active_tags
                for wanted, message in t.wants
                if wanted not in active]

    @property
    def conf(self) -> dict[str, str]:
        """The engine's effective env conf (`-e KEY=VALUE` source + effort)."""
        return self.engine.conf_map if self.engine else {}

    @property
    def workspace_readonly(self) -> bool:
        """True when any active specialty asks for the workspace mounted
        read-only (the hard `{frozen}`-style guarantee — the agent physically
        cannot write to the project). docker_config.set_container_mounts reads
        this to pick the `/workspace` mount's access mode."""
        return any(s.workspace_readonly for s in self.specialties)

    @property
    def claude_args(self) -> list[str]:
        """CLI args contributed by the selected specialties (e.g. auto's
        `--dangerously-skip-permissions`), in chain order."""
        by_name = {s.name: s for s in self.specialties}
        out: list[str] = []
        for name in self.chain:
            if name in by_name:
                out.extend(by_name[name].claude_args)
        return out

    @property
    def has_continuable_history(self) -> bool:
        return has_continuable_jsonl(self.state_dir)

    @property
    def last_used_mtime(self) -> float | None:
        return last_history_mtime(self.state_dir)


def effective_engine_name(build: AgentBuild, agent: str, registry: Registry) -> str:
    """The engine that would actually run: `build.engine` → an engine named
    like the agent → `default`. Shared by resolve_build and the form's
    radio pre-check (the form shows the concrete outcome, not the fallback
    chain)."""
    return build.engine or (agent if agent in registry.engines else "default")


def resolve_build(build: AgentBuild, agent: str, registry: Registry) -> dict:
    """Turn an `AgentBuild` (name lists from a `.lego`) into resolved tag
    objects, as a kwargs dict for `Instance`. Engine falls back via
    `effective_engine_name`. References are assumed already validated
    (`Registry.validate_build`); a missing one surfaces as a KeyError, which
    the caller has validated away upstream."""
    engine = registry.engines.get(effective_engine_name(build, agent, registry))
    return {
        "engine": engine,
        "professions": tuple(registry.professions[n] for n in build.professions),
        "specialties": tuple(registry.specialties[n] for n in build.specialties),
        "policies": tuple(registry.policies[n] for n in build.policies),
    }


def agent_md_path(agent: str) -> Path | None:
    """The agent's source `.md`, by clean name (reuses the file-access index)."""
    return agent_md_index().get(agent)


def load_agent(name: str, agents_dir: Path) -> Agent | None:
    """Build an `Agent` from a clean name: its `.md` (via the index) + its
    `.lego` defaults (missing `.lego` → empty build). None when no such
    agent `.md` exists."""
    from .lego import load_lego   # local import — lego imports nothing from here
    md = agent_md_path(name)
    if md is None:
        return None
    return Agent(name=name, md_path=md, build=load_lego(agents_dir / f"{name}.lego"))
