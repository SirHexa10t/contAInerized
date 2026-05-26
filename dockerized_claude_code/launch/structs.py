"""Identity dataclasses + modifier taxonomy for the launcher.

Three identity layers, bottom-up:

  AgentIdentity     — what's true of the agent on disk: which .md file (and
                       derived .conf, [tag]s). Mode-, instance-, and workspace-
                       independent.
  InstanceIdentity  — adds session suffix + workspace + is_brand_new (NEW vs
                       continuing). Stable across mode changes — modes are a
                       per-launch decision, layered on top by SessionIdentity.
                       This is what 'one launch targets' from resume-detection
                       onward.
  SessionIdentity   — adds this launch's resolved modes.

Plus the modifier taxonomy:

  InstanceModifiers — the canonical enumeration of every filename-derived [tag]
                       and per-instance {mode}, with declaration order encoding
                       chain composition order. Each member carries its on-disk
                       string (.value), lowercased filename form, picker-legend
                       description, and a 'tag' / 'mode' kind classifier; the
                       tags() / modes() classmethods give subset views. Both
                       agents_crud (for the auto+DooD warning + picker sort
                       keys) and agent_composition (for handler dispatch + chain
                       composition) consume this — it lives here because the
                       structs layer is the deepest both can import from
                       without circularity.

Inheritance (not composition) so a function taking the parent type happily
accepts any subclass. Construction:

  - resolve_pick / picker entries (in agents_crud) return AgentIdentity (new)
    or SessionIdentity (cont, with stored workspace + modes + is_brand_new=False).
  - resolve_target (in run.py) promotes AgentIdentity → InstanceIdentity once
    session + workspace are known, stamping is_brand_new=True at that promotion.
  - inst_id.with_modes(modes) promotes InstanceIdentity → SessionIdentity once
    compose_runtime has resolved them.

Pure data-types module — leaf-ish within launch/, depending only on paths,
file_access (for `conf_path_for` etc.), and utils (for `parse_stem`).
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from functools import cache
from pathlib import Path

from .file_access import (
    conf_path_for, has_continuable_jsonl, is_dir, last_history_mtime,
    load_modes_map,
)
from .paths import AGENT_MD_BY_NAME, AGENT_WORKSPACE_MAP_FILE, instance_state_dir_path, state_md_path
from .utils import parse_stem


# ============================================================
# Modifier taxonomy
# ============================================================

class InstanceModifiers(Enum):
    """Every build-chain modifier — the always-on `BASE` plus filename-derived
    [tags] and per-instance {modes} unified into a single ordered taxonomy.
    Member-name prefix encodes kind: `TAG_*` for filename-derived tags, `MODE_*`
    for per-instance modes; the unprefixed `BASE` is implicit (always active,
    never user-toggled) and naturally falls out of the `tags()` / `modes()`
    subset views.
    Each member carries:
      • `.value`         — canonical on-disk string (filename + JSON form)
      • `.slug`          — lowercased value, used wherever a case-stable
                           identifier is needed (image-tag construction, etc.;
                           e.g. {DooD} → 'dood')
      • `.description`   — one-sentence picker-legend explanation
    Enum declaration order encodes chain composition order: base → tags → modes.
    Subset views (`tags()` / `modes()` for the members; `tag_values()` /
    `mode_values()` for the canonical strings) are memoized — call them freely
    without buffering locally."""

    BASE      = ("base", "always-on starting image — every agent gets it; no user-facing toggle")
    TAG_PROG  = ("prog", "programming-oriented; built with various programs and toolchains (Rust, Node, build-essential, uv)")
    MODE_AUTO = ("auto", "autonomous; Doesn't need permission to perform actions. Built with a firewall slightly increased security. Danger: hard to control!")
    MODE_DOOD = ("DooD", "Docker outside-of Docker; Can run Docker. Danger: authority to do anything (effectively host-root)!")

    def __init__(self, value: str, description: str) -> None:
        self._value_ = value
        self.description = description

    @property
    def slug(self) -> str:
        return self.value.lower()

    @property
    def label(self) -> str:
        """User-facing label with the kind-distinguishing wrapping: `[prog]` for
        tags (square brackets — the filename-grammar form), `{auto}` / `{DooD}`
        for modes (curly braces). Single source of truth for any picker prompt
        / dialog / banner that needs to name a specific modifier. BASE has no
        user-facing label (never user-toggled), so it's not reachable here in
        practice — its bare value is returned as a safe fallback."""
        if self.name.startswith("TAG_"):
            return f"[{self.value}]"
        if self.name.startswith("MODE_"):
            return f"{{{self.value}}}"
        return self.value

    @classmethod
    def format_prefix(cls, values) -> str:
        """Render an iterable of modifier value strings (any mix of tag and
        mode values) as space-separated bracketed labels with a trailing
        space: '[prog] {auto} ' / '' for empty input. Wrapping comes from
        each member's `.label` (tags → `[..]`, modes → `{..}`), so the
        wrapping convention lives on the enum and callers don't replicate
        it. Unknown values fall back to `[v]` rendering — covers typo'd
        filename tags (the more common origin of strays); compose_chain
        rejects unknowns separately at launch time. BASE is excluded from
        the labels dict — it's not a tag or mode and never appears in
        user-facing value lists, so any 'base' that slipped in would also
        hit the unknown-tag fallback."""
        if not values:
            return ""
        labels = {m.value: m.label for m in (*cls.tags(), *cls.modes())}
        return "".join(labels.get(v, f"[{v}]") + " " for v in values)

    # The four subset-view classmethods below are memoized via @functools.cache
    # (key = cls; single-class system → 100% hit rate after the first call). The
    # enum's members and their name-prefix kind are immutable for the process's
    # lifetime, so no invalidation is ever needed. Pattern: @classmethod outside,
    # @cache inside — the cache wraps the underlying function; classmethod
    # forwards `cls` as the cache key.

    @classmethod
    @cache
    def tags(cls) -> tuple["InstanceModifiers", ...]:
        """Tuple of tag members (name prefix `TAG_`) in declaration order."""
        return tuple(m for m in cls if m.name.startswith("TAG_"))

    @classmethod
    @cache
    def modes(cls) -> tuple["InstanceModifiers", ...]:
        """Tuple of mode members (name prefix `MODE_`) in declaration order."""
        return tuple(m for m in cls if m.name.startswith("MODE_"))

    @classmethod
    @cache
    def tag_values(cls) -> tuple[str, ...]:
        """Tuple of canonical `.value` strings for the tag members, declaration
        order. Use when comparing against on-disk strings (filename parser
        output, JSON contents, sort-key ordering lists)."""
        return tuple(m.value for m in cls.tags())

    @classmethod
    @cache
    def mode_values(cls) -> tuple[str, ...]:
        """Tuple of canonical `.value` strings for the mode members, declaration order."""
        return tuple(m.value for m in cls.modes())

SESSION_SEP = "__"


# ============================================================
# Identity dataclasses
# ============================================================

@dataclass(frozen=True)
class AgentIdentity:
    """Agent-level identity: which .md file (and derived .conf / tags) define
    this agent's behavior. Used by the picker's Create rows (before session +
    workspace are chosen) and as the parent class for the two subclasses below."""
    agent: str                          # clean agent name without [tag] / (parent) suffixes; matches the filename's leading word

    @property
    def md_path(self) -> Path:
        """Source agent .md file under agents/, looked up by agent name via
        AGENT_MD_BY_NAME (dict access — O(1)). The agent's filename .stem
        still carries [tags] / (parent) — the conf_path / tags properties
        parse those out. Identity is constructed after the agent's existence
        has been verified upstream, so the lookup won't return None in
        practice — the assert narrows the Optional and would also surface a
        callsite that skipped the upstream verification."""
        md = AGENT_MD_BY_NAME.get(self.agent)
        assert md is not None, f"AgentIdentity({self.agent!r}) has no .md file — verify upstream"
        return md

    @property
    def conf_path(self) -> Path | None:
        """Path to the .conf file backing this agent: '(parent).conf' if the
        filename had a (parent) suffix, otherwise '<agent>.conf', falling back
        to default.conf. None if even the default is absent."""
        return conf_path_for(self.md_path)

    @property
    def tags(self) -> tuple[str, ...]:
        """Filename-grammar tags from the .md's stem (e.g. ('prog',) for
        `name[prog].md`). Tuple so the dataclass stays hashable should we ever
        want it as a dict key."""
        return tuple(parse_stem(self.md_path.stem)[1])


@dataclass(frozen=True)
class InstanceIdentity(AgentIdentity):
    """Per-instance identity: agent + which session + which workspace, plus a
    flag for whether this launch is creating a brand-new instance or continuing
    an existing one. Stable across mode changes — modes are layered on top by
    SessionIdentity below. Constructed by resolve_target once session +
    workspace are known, and used by everything up to (and including) the
    modes resolution step."""
    session: str                        # user-chosen suffix differentiating parallel instances of the same agent
    workspace: str | None               # host-side path bind-mounted into the container at /workspace; None when continuable_instances built us from a stale workspace-map entry (resolve_target re-prompts; set_container_mounts also falls back to DEFAULT_WORKSPACE)
    is_brand_new: bool                  # True for a freshly-promoted AgentIdentity, False for a cont pick — drives resume + modes-resolution branches

    @property
    def instance(self) -> str:
        """Canonical instance id `<agent>__<session>`; the state-dir name and
        the key used by agent_workspace_map / agent_modes_map."""
        return InstanceIdentity.instance_name(self.agent, self.session)

    @property
    def state_dir(self) -> Path:
        """Host-side per-instance state directory; bind-mounted into the
        container at /home/claude/.claude."""
        return InstanceIdentity.state_dir_for(self.agent, self.session)

    @property
    def state_md(self) -> Path:
        """Path to the CLAUDE.md inside this instance's state dir — written by
        install_latest_md on each launch from the source agent .md."""
        return state_md_path(self.state_dir)

    @property
    def stored_modes(self) -> list[str]:
        """Modes persisted in agent_modes_map.json for this instance (empty
        list if no entry). Used by compose_runtime on cont launches to pick up
        whatever the modify flow last persisted. Reads through file_access's
        cached load_modes_map() so repeated property accesses don't re-read
        the JSON file."""
        return load_modes_map().get(self.instance, [])

    @property
    def has_continuable_history(self) -> bool:
        """Whether this instance has an actual conversation transcript that
        `claude --continue` can load — delegates the disk scan to file_access
        so the dataclass stays free of I/O. See has_continuable_jsonl for the
        scan logic + history-vs-session-jsonl rationale."""
        return has_continuable_jsonl(self.state_dir)

    @property
    def last_used_mtime(self) -> float | None:
        """Mtime of the most-recently-written history.jsonl under this instance's
        state dir, or None if no history file exists yet. Used by the picker's
        Cont row preview for the 'Last used' relative timestamp. Delegates the
        rglob + stat to file_access."""
        return last_history_mtime(self.state_dir)

    def validate_workspace(self) -> None:
        """Exit if the workspace path is set but doesn't resolve to a real
        directory (stale agent_workspace_map.json entry). Workspace=None /
        empty string passes through silently so the caller can decide to
        prompt for a new value instead of treating absence as an error —
        empty-string is normalized to None here since `Path("").is_dir()`
        spuriously returns True (it resolves to cwd) and we don't want to
        bind-mount the launcher's cwd into the container."""
        if not self.workspace:
            return
        if not is_dir(self.workspace):
            sys.exit(
                f"Workspace for '{self.instance}' is not a valid directory: {self.workspace}\n"
                f"Fix the entry in {AGENT_WORKSPACE_MAP_FILE}"
            )

    def with_modes(self, modes: Iterable[str]) -> SessionIdentity:
        """Promote this InstanceIdentity into a full SessionIdentity by
        attaching the resolved modes — called once compose_runtime has prompted
        (for brand-new) or loaded (for cont) the per-instance mode list.
        Carries is_brand_new through unchanged."""
        return SessionIdentity(
            agent=self.agent, session=self.session, workspace=self.workspace,
            is_brand_new=self.is_brand_new, modes=tuple(modes),
        )

    @staticmethod
    def instance_name(agent: str, session: str) -> str:
        """Compose the canonical state-dir id `<agent>__<session>` from raw
        strings. Complement to the `instance` property — used by picker prompts
        that don't have an identity in hand yet (e.g. validating a freshly-
        typed session suffix before constructing one)."""
        return f"{agent}{SESSION_SEP}{session}"

    @staticmethod
    def state_dir_for(agent: str, session: str) -> Path:
        """Path to an instance's state directory from raw strings. Complement
        to the `state_dir` property — same prompt-side use case as
        instance_name. `_for` suffix avoids name-collision with the property."""
        return instance_state_dir_path(InstanceIdentity.instance_name(agent, session))


@dataclass(frozen=True)
class SessionIdentity(InstanceIdentity):
    """Extends InstanceIdentity with this launch's resolved modes. Modes aren't
    intrinsic to *which instance this is* — they're a per-launch decision —
    which is why they live here rather than on the parent. Constructed via
    `inst_id.with_modes(...)`, or directly by continuable_instances when the
    picker pre-loads stored modes for the modify flow's pre-fill."""
    modes: tuple[str, ...]              # per-instance opt-ins like ('auto',) or ('auto', 'DooD'); tuple keeps the dataclass hashable

    @property
    def chain(self) -> tuple[str, ...]:
        """Active modifier values for this session in InstanceModifiers
        declaration order — BASE always first, then the session's tags,
        then its modes. Drives both the docker image build order (returned
        as-is from compose_chain) and the launch-time CLAUDE.md addendum
        order (consumed by memory_addendums.composed_addendum via
        `modifier.value in chain`).

        Validates self.tags / self.modes against the InstanceModifiers
        taxonomy on access — a typo'd filename tag or stale modes-map entry
        surfaces as a ValueError here rather than as a silent drop from the
        chain (and rather than as a deeper-down failure when an unknown
        value reaches docker / compose)."""
        if unknown := set(self.tags) - set(InstanceModifiers.tag_values()):
            raise ValueError(f"Unknown tag(s): {sorted(unknown)}. Known tags: {list(InstanceModifiers.tag_values())}")
        if unknown := set(self.modes) - set(InstanceModifiers.mode_values()):
            raise ValueError(f"Unknown mode(s): {sorted(unknown)}. Known modes: {list(InstanceModifiers.mode_values())}")
        return tuple(m.value for m in InstanceModifiers
                     if m is InstanceModifiers.BASE or m.value in self.tags or m.value in self.modes)
