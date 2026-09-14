"""Per-launch instance identity.

An `Instance` is a fully-resolved launch: an agent (its `.md` persona) plus
the four axis selections as concrete `Tag` objects (engine + professions +
specialties + policies), plus session / workspace / new-vs-continuing. The
selections come from the agent's `.lego` defaults (a fresh create) or the
per-instance store (a continue), with the create-form editing them in
between.

`image_chain` computes the active-tag chain — `["base", <professions…>,
<specialties…>]`. Both groups are ordered so a required tag precedes its
dependents (code before web; cowork before manager); specialties follow all
professions, so a specialty's profession requirements are satisfied by the
whole profession group being ahead.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from ..container_probe import CONTAINER_NAME_CHARS
from ..file_access import agent_md_index
from ..transcripts import (
    continuable_jsonl_bytes, has_continuable_jsonl, last_history_mtime,
)
from ..paths import INBOX_SEPARATOR, instance_state_dir_path, state_md_path
from .base import DockerContribution, Tag
from .ai import Ai, Rendering
from .engine import Engine
from .lego import AgentBuild
from .policy import Policy
from .profession import Profession
from .registry import Registry, TagProblem
from .specialty import Specialty

SESSION_SEP = "__"
# The two specialties the launcher recognises by NAME (see Instance.is_cowork /
# is_manager for why): `{cowork}` makes an instance eligible for multi-agent
# group hosting (the launcher bind-mounts its group dir), and `{manager}` — the
# specialty nested inside cowork/ — makes the hub honour its control requests.
# Both are launcher/hub behaviours with nothing for a tag manifest to declare.
# Renaming either tag dir means changing its constant here.
COWORK_SPECIALTY = "cowork"
MANAGER_SPECIALTY = "manager"
MUXER_SPECIALTY = "muxer"

# ============================================================
# Label legality — the one rule every name a person types is held to
# ============================================================
# An instance's session, a cluster's session, a member's role: each is typed
# once and then becomes a directory name, part of a docker `--name`, a tmux
# session/window address, a herdr workspace label, a git branch component
# (cluster worktrees) and a cowork inbox / prompt-marker token. Every rejected
# character is paired with the place it would mean something else in — that
# reason IS the message the user reads. Until 2026-09-09 three validators
# carried three different subsets of this (cluster labels; cowork's two
# separators; nothing at all for instance names, so a dotted project dir
# auto-named an instance that reached tmux as an address — cluster/solo.py);
# now one rule, and each caller adds only its own separator. git's remaining
# ref rules (no `..`, `@{`, `.lock`, no component starting with `.`) are
# unreachable once `.` and `@` are out. After the table, docker's container
# name charset (`container_probe.CONTAINER_NAME_CHARS`) is the catch-all for
# everything else — `é`, `!`, `#`, quotes — so `docker run --name` never gets
# to refuse a name at the end of a build.
_TMUX_TARGET = "tmux addresses windows as session:window.pane"
_GIT_REF = "git refuses it in a ref name, and the label is a component of a worktree branch"
_WHITESPACE = "a tmux window name and a shell word both end at whitespace"
FORBIDDEN_IN_LABELS: dict[str, str] = {
    ":": _TMUX_TARGET,
    ".": _TMUX_TARGET,
    "/": "the label becomes a path component",
    INBOX_SEPARATOR: "{cowork} names inbox dirs `<group>@<sender>`, and the two name spaces should stay legible side by side",
    " ": _WHITESPACE,
    "\t": _WHITESPACE,
    "\n": _WHITESPACE,
    "~": _GIT_REF,
    "^": _GIT_REF,
    "?": _GIT_REF,
    "*": _GIT_REF,
    "[": _GIT_REF,
    "\\": _GIT_REF,
}


def label_error(label: str) -> str | None:
    """Why `label` cannot be an identifier, or None when it can — in the shape
    a form validator returns (`gui.form_core.TextField.validate`), so a form
    shows it live beside the field and a model wraps it in its own exception
    (`cluster.member.valid_label` → ClusterError, `cowork.group._legal_label`
    → ValueError). Reports the FIRST problem and never sanitises: a rewritten
    name would leave the thing keyed under a name it does not answer to.
    Non-printable characters are refused as a class after the table — an
    escape sequence or a zero-width space in a tmux status token, a tab title
    or a picker row is not a name, whatever its code point — and docker's
    container-name charset is the final catch-all, since every name here ends
    up in a `docker run --name`."""
    if not label:
        return "cannot be empty"
    for char, reason in FORBIDDEN_IN_LABELS.items():
        if char in label:
            shown = repr(char) if char.strip() else "whitespace"
            return f"may not contain {shown} — {reason}"
    if not label.isprintable():
        return ("may not contain control or invisible characters — it is shown as "
                "a tmux status token, a tab title and a picker row")
    if not CONTAINER_NAME_CHARS.fullmatch(label):
        return ("may only use letters, digits, '_' and '-' — a docker container "
                "name (`docker run --name`) accepts nothing else")
    return None


# Letters with an obvious ASCII spelling that Unicode decomposition (NFKD)
# leaves whole, because they are letters of their own rather than a base
# letter plus an accent. NFKD already handles every accented Latin letter
# (`é` → `e` + a combining mark, which `suggested_label` then drops) and the
# compatibility forms (ligatures, fullwidth digits, superscripts); this table
# is only what it cannot. Scripts with no ASCII spelling (CJK, Cyrillic) are
# deliberately absent — a wrong guess is worse than a `-`.
_TRANSLITERATIONS = str.maketrans({
    "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o", "Ø": "O",
    "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "þ": "th",
    "Þ": "Th", "ı": "i",
})


def suggested_label(text: str) -> str:
    """`text` bent into a legal label for a form's DEFAULT. Letters with an
    ASCII spelling get it first (`café` → `cafe`, `Straße` → `Strasse` — NFKD
    decomposition with the marks dropped, plus `_TRANSLITERATIONS`); then
    every character `label_error` still refuses becomes `-`, runs of `-`
    collapse to one and the ends are trimmed (`my.app` → `my-app`, `.hidden`
    → `hidden`); "" comes back when nothing legal remains, so the caller
    falls back to its own default. For suggestions ONLY — a name the user
    typed is never rewritten (`label_error` refuses, it does not fix: a
    rewritten id would leave the thing keyed under a name it does not answer
    to); a suggestion nobody has typed yet has no owner to betray. Each
    character is judged through the same rule, so there is no second table
    of legality to keep in step — only the spelling table above."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATIONS))
    unaccented = "".join(ch for ch in decomposed
                         if not unicodedata.category(ch).startswith("M"))
    bent = "".join(ch if label_error(ch) is None else "-" for ch in unaccented)
    return re.sub(r"-{2,}", "-", bent).strip("-")


_TagT = TypeVar("_TagT", bound=Tag)


def _topo_by_requires(tags: tuple[_TagT, ...], satisfied: set[str]) -> list[_TagT]:
    """Order `tags` so each follows the tags it requires, ignoring requirements
    already in `satisfied` (a later group treats every earlier group's names as
    given). Ties broken by name for determinism. A selection with an unmet
    requirement (prevented by the form, but guarded here) appends the
    stragglers rather than looping forever."""
    placed: list[_TagT] = []
    placed_names = set(satisfied)
    remaining = sorted(tags, key=lambda t: t.name)
    while remaining:
        ready = [t for t in remaining if t.requires <= placed_names]
        if not ready:
            placed.extend(remaining)   # unsatisfiable requires — shouldn't happen post-validation
            break
        for t in ready:
            placed.append(t)
            placed_names.add(t.name)
            remaining.remove(t)
    return placed


def _ordered_groups(professions: tuple[Profession, ...],
                    specialties: tuple[Specialty, ...],
                    ) -> tuple[list[Profession], list[Specialty]]:
    """Professions and specialties in chain order — the ONE ordering every
    chain-shaped view derives from (image_chain, active_tags, build_steps,
    docker_contributions). A specialty's profession requirements are satisfied
    by the whole profession group being ahead; its specialty requirements (the
    tree nests: `{manager}` inside `cowork/`) by the topo order within its own
    group. One helper rather than four inline sorts, because the addendum
    composition and the image chain drifting apart would be a subtle bug."""
    profs = _topo_by_requires(professions, set())
    specs = _topo_by_requires(specialties, {p.name for p in profs})
    return profs, specs


def image_chain(professions: tuple[Profession, ...],
                specialties: tuple[Specialty, ...]) -> list[str]:
    """The active-tag chain: `["base", <professions…>, <specialties…>]`, in
    `_ordered_groups` order — see that helper for why the ordering is shared.
    Drives handler dispatch and the addendum composition, which is why order
    matters at all: a nested specialty's addendum should read after the one it
    extends. (Image naming/building uses `Instance.build_steps` — the
    layer-bearing subset — since run-only specialties like {firewall}
    contribute container config but no image content.)"""
    profs, specs = _ordered_groups(professions, specialties)
    return ["base", *(t.name for t in profs), *(t.name for t in specs)]


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
    ai: Ai | None = None                        # the AI this instance runs on (resolved: its build's, else the tree's default); None only in fixture trees without an ai/ shelf
    invalid_tags: tuple[TagProblem, ...] = ()   # store names that no longer resolve (see resolve_store_build); block start, flagged in the picker
    state_dir_override: Path | None = None      # when set, the state dir lives HERE instead of under instances/ — quickie parks its throwaway threads under quickie/ (default None = the normal instances/ home)

    @property
    def instance(self) -> str:
        """Canonical `<agent>__<session>` id — the state-dir name and store key."""
        return f"{self.agent}{SESSION_SEP}{self.session}"

    @property
    def state_dir(self) -> Path:
        """Where this instance's launcher-owned state lives (CLAUDE.md,
        settings.json, the mounted ~/.claude). Normally `instances/<id>`;
        `state_dir_override` redirects it (the quickie tool parks its threads
        under `quickie/` rather than cluttering the main instances/ list)."""
        return self.state_dir_override or instance_state_dir_path(self.instance)

    @property
    def state_md(self) -> Path:
        return state_md_path(self.state_dir)

    @property
    def chain(self) -> list[str]:
        return image_chain(self.professions, self.specialties)

    @property
    def active_tags(self) -> list[Tag]:
        """Every active tag as objects, in chain order (`_ordered_groups`),
        then policies. Drives the addendum composition; anything wanting 'all
        my tags, ordered' reads this instead of re-deriving."""
        profs, specs = _ordered_groups(self.professions, self.specialties)
        return [*profs, *specs, *self.policies]

    @property
    def build(self) -> AgentBuild:
        """The instance's axis selections as name strings — what the store
        persists and the form pre-checks (inverse of resolve_build)."""
        return AgentBuild(
            ai=self.ai.name if self.ai else None,
            engine=self.engine.name if self.engine else None,
            professions=tuple(p.name for p in self.professions),
            specialties=tuple(s.name for s in self.specialties),
            policies=tuple(p.name for p in self.policies),
        )

    @property
    def build_steps(self) -> list[tuple[str, Path, DockerContribution | None]]:
        """(name, dockerfile, contribution) per image layer in chain order
        (`_ordered_groups`): professions, then layer-bearing specialties
        (dood's `_dood` dir). The contribution supplies the layer's `[build]
        arg_forward`. Run-only specialties (auto, firewall) don't appear: they
        contribute container config, not image content. Empty for a bare agent
        (base image only)."""
        profs, specs = _ordered_groups(self.professions, self.specialties)
        out: list[tuple[str, Path, DockerContribution | None]] = [
            (p.name, p.path / "Dockerfile", p.docker) for p in profs
        ]
        out += [(s.name, s.layer.path / "Dockerfile", s.layer.docker)
                for s in specs if s.layer]
        return out

    @property
    def docker_contributions(self) -> list[DockerContribution]:
        """Active tags' `tag.docker` records in chain order (`_ordered_groups`)
        — professions first, then each specialty's own record + (if any) its
        claimed layer's. docker_config folds these into build/run flags."""
        profs, specs = _ordered_groups(self.professions, self.specialties)
        out = [p.docker for p in profs if p.docker]
        for s in specs:
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
    def rendering(self) -> "Rendering | None":
        """The engine's budget in this instance's AI's settings — None when
        either side is missing (a fixture tree)."""
        return self.ai.render(self.engine.budget) if self.engine and self.ai else None

    @property
    def conf(self) -> dict[str, str]:
        """The instance's native settings (`-e KEY=VALUE` source for Claude
        Code): its engine's budget rendered by its AI."""
        rendering = self.rendering
        return rendering.map if rendering else {}

    @property
    def model(self) -> str:
        """The model id the instance's AI runs for its engine's standard, or ""."""
        return self.ai.tier(self.engine.budget.standard).model if self.engine and self.ai and self.engine.budget.standard else ""

    @property
    def effort(self) -> str | None:
        """The effort word the instance's AI uses for its engine's standard (the
        `--effort` flag's value on Claude Code), or None."""
        return self.ai.tier(self.engine.budget.standard).effort if self.engine and self.ai and self.engine.budget.standard else None

    @property
    def is_muxer(self) -> bool:
        """True when `{muxer}` is active, i.e. this instance launches inside a
        terminal multiplexer instead of handing the terminal straight to claude.

        By name for the same reason as `is_cowork`/`is_manager`: what it gates is
        launcher behaviour (which command the container runs), not something a
        `tag.docker` manifest can express. `{cluster}` nests inside `{muxer}`, so
        a validly-built cluster member satisfies this too."""
        return any(s.name == MUXER_SPECIALTY for s in self.specialties)

    @property
    def workspace_readonly(self) -> bool:
        """True when any active specialty asks for the workspace mounted
        read-only (the hard `{frozen}`-style guarantee — the agent physically
        cannot write to the project). docker_config.set_container_mounts reads
        this to pick the `/workspace` mount's access mode."""
        return any(s.workspace_readonly for s in self.specialties)

    @property
    def is_cowork(self) -> bool:
        """True when `{cowork}` is active, i.e. this instance may be recruited
        into a multi-agent group. Matched BY NAME rather than by a tag.info
        field — a deliberate exception to the otherwise field-driven design,
        because the capability is a launcher-side behaviour (mounting the
        group-hosting dir) with nothing for a tag manifest to declare.
        Renaming the tag therefore means editing this constant too.
        docker_config.set_container_mounts reads this to decide whether to
        bind-mount `cowork_dir_path(...)` at COWORK_IN_CONTAINER."""
        return any(s.name == COWORK_SPECIALTY for s in self.specialties)

    @property
    def is_manager(self) -> bool:
        """True when `{manager}` is active, i.e. the hub honours this
        instance's control requests (roster / recruit / send / release /
        done). Same by-name exception as `is_cowork`, for the same reason:
        the gate is hub-side behaviour, not something a manifest can declare.
        The tag nests inside cowork/, so `is_manager` implies `is_cowork` on
        any validly-built instance — cowork.control checks only this one."""
        return any(s.name == MANAGER_SPECIALTY for s in self.specialties)

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
    def is_startable(self) -> bool:
        """False when the store entry named tags that no longer resolve
        (`invalid_tags`) — the launch is blocked with a fix-it report; F2 in
        the picker re-picks against the current tag set."""
        return not self.invalid_tags

    @property
    def has_continuable_history(self) -> bool:
        return has_continuable_jsonl(self.state_dir)

    @property
    def continuable_history_bytes(self) -> int:
        return continuable_jsonl_bytes(self.state_dir)

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
        "ai": registry.ai_for(build),
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
