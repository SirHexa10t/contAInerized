"""Identity dataclasses + modifier taxonomy for the launcher.

Two identity layers, bottom-up:

  AgentIdentity     — what's true of the agent on disk: which .md file (and
                       derived .conf, [tag]s). Mode-, instance-, and workspace-
                       independent.
  InstanceIdentity  — adds session suffix + workspace + modes + is_brand_new
                       (NEW vs continuing). Fully resolved for the launch —
                       constructed once all three prompts (workspace, session,
                       modes) have answered, or directly from disk for cont.

Plus the modifier taxonomy:

  InstanceModifiers — the canonical enumeration of every filename-derived [tag]
                       and per-instance {mode}, with declaration order encoding
                       chain composition order. Each member carries its on-disk
                       string (.value), lowercased filename form, picker-legend
                       description, and a 'tag' / 'mode' kind classifier; the
                       tags() / modes() classmethods give subset views. Both
                       agents_crud (for the auto+DooD warning + picker sort
                       keys) and agent_modifiers_handler (for handler dispatch + chain
                       composition) consume this — it lives here because the
                       structs layer is the deepest both can import from
                       without circularity.

Inheritance (not composition) so a function taking the parent type happily
accepts any subclass. Construction:

  - resolve_pick / picker entries (in agents_crud / menu_picker) return
    AgentIdentity (new) or InstanceIdentity (cont, with stored workspace +
    modes + is_brand_new=False).
  - resolve_target (in run.py) promotes AgentIdentity → InstanceIdentity once
    workspace + session + modes prompts have answered, stamping is_brand_new=True.

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
)
from .paths import AGENT_MD_BY_NAME, AGENT_WORKSPACE_MAP_FILE, instance_state_dir_path, state_md_path
from .utils import parse_stem


# ============================================================
# Modifier taxonomy
# ============================================================

# ANSI escape → prompt_toolkit style. Single source of truth for the
# warning-aware colors emitted by InstanceModifiers.colored_label /
# colored_chain — keys are the escapes embedded in the ANSI output, values
# are the prompt_toolkit style strings consumers translate to when they need
# fragment form (the picker imports this dict directly). Walrus operators
# bind each escape to a module-private name so colored_label can reference
# them without a parallel constants block to maintain. The two opening codes
# are 8 bytes each by design — rich's markdown-table column-width detection
# counts bytes including escapes, so unequal-length codes would shift the F8
# legend's description column for whichever colour has the shorter prefix.
ANSI_TO_PT_STYLE: dict[str, str] = {
    (_WARN_ANSI  := "\033[01;91m"): "bold fg:ansibrightred",
    (_SAFE_ANSI  := "\033[22;92m"): "fg:ansibrightgreen",
    (_RESET_ANSI := "\033[0m"):     "",
}

class InstanceModifiers(Enum):
    """Every build-chain modifier — the always-on `BASE` plus filename-derived
    [tags] and per-instance {modes} unified into a single ordered taxonomy.
    Member-name prefix encodes kind: `TAG_*` for filename-derived tags, `MODE_*`
    for per-instance modes; the unprefixed `BASE` is implicit (always active,
    never user-toggled) and naturally falls out of the `tags()` / `modes()`
    subset views.
    Each member carries:
      • `.value`         — canonical on-disk string (filename + JSON form)
      • `.description`   — one-sentence picker-legend explanation
    Enum declaration order encodes chain composition order: base → tags → modes.
    Subset views (`tags()` / `modes()` for the members; `tag_values()` /
    `mode_values()` for the canonical strings) are memoized — call them freely
    without buffering locally."""

    BASE      = ("base", "always-on starting image — every agent gets it; no user-facing toggle")
    TAG_CODE  = ("code", "coding/programming-oriented; built with various programs and toolchains (Rust, Node, build-essential, uv)")
    MODE_WARN_AUTO = ("auto", "autonomous; Doesn't need permission to perform actions. Comes with a firewall for a slight security-increase. Danger: hard to control!")
    MODE_WARN_DOOD = ("DooD", "Docker outside-of Docker; Can run Docker. Danger: authority to do anything (effectively host-root)!")
    MODE_WEB  = ("web", "headless browser automation (playwright + chromium baked in); for web-scraping / browser-testing / dynamic-page extraction. Built on [code]; no display server needed.")

    def __new__(cls, value: str, description: str) -> "InstanceModifiers":
        # Set _value_ here (rather than in __init__) so the enum metaclass
        # registers each member by its string value in `_value2member_map_`
        # before init runs. That makes `InstanceModifiers("auto")` a valid
        # value-lookup — used by the JSON-load boundary via from_value()
        # below (raises ValueError on unknowns, fail-fast).
        obj = object.__new__(cls)
        obj._value_ = value
        return obj

    def __init__(self, value: str, description: str) -> None:
        self.description = description

    @classmethod
    def from_value(cls, value: str) -> "InstanceModifiers":
        """Look up a member by its `.value` string; raises ValueError on
        unknowns. Used at JSON-load boundaries (continuable_instances /
        resolve_pick) to convert modes-map strings
        into typed members — defective entries fail fast here rather than
        propagating downstream as silent string mismatches.

        The `# type: ignore` exists because mypy reads InstanceModifiers'
        __init__ literally and thinks calling InstanceModifiers(value) is a
        constructor call missing `description` — but Python's enum machinery
        treats EnumClass(value) as a value-lookup, not construction."""
        return cls(value)  # type: ignore[call-arg]

    @property
    def label(self) -> str:
        """User-facing label with the kind-distinguishing wrapping: `[code]` for
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

    def colored_label(self) -> str:
        """The member's `.label` wrapped in the warning-aware ANSI color:
        bright red for members whose Python name contains `_WARN_` (the
        dangerous modes flagged at the taxonomy level — MODE_WARN_AUTO drops
        permission prompts, MODE_WARN_DOOD grants host-docker access), bright
        green otherwise. Single source of truth for every site that colors a
        modifier label. Consumers needing prompt_toolkit-fragment form (e.g.
        the picker) translate the ANSI codes via the module-level
        `ANSI_TO_PT_STYLE` mapping."""
        color = _WARN_ANSI if "_WARN_" in self.name else _SAFE_ANSI
        return f"{color}{self.label}{_RESET_ANSI}"

    @classmethod
    def in_order(cls, modifiers: Iterable["InstanceModifiers"]) -> tuple["InstanceModifiers", ...]:
        """Return the given modifiers as a tuple in enum-declaration order,
        deduped. Single source of truth for the 'subset of modifiers in
        canonical (BASE → tags → modes) order' shape — chain composition,
        status-line rendering, and any future site that needs to project a
        modifier subset back onto the taxonomy's ordering route through here."""
        given = set(modifiers)
        return tuple(m for m in cls if m in given)

    @classmethod
    @cache
    def _prerequisites(cls) -> dict["InstanceModifiers", frozenset["InstanceModifiers"]]:
        """Modifier → set of other modifiers that must be active for this one
        to apply. Modifiers absent from the mapping have no prerequisites.
        Looked up via `applies_to()`; defined here (single classmethod) rather
        than as per-member values in the value-tuple so members without
        prereqs aren't burdened with an empty placeholder, and so member
        references resolve correctly (they're not yet members during the
        class body's value-tuple construction)."""
        return {
            cls.MODE_WARN_DOOD: frozenset({cls.TAG_CODE}),
            cls.MODE_WEB:       frozenset({cls.TAG_CODE}),
        }

    def applies_to(self, tags: set["InstanceModifiers"]) -> bool:
        """True iff this modifier's prerequisites (if any) are all present in
        `tags`. Modifiers with no prereqs always return True. Used by
        `prompt_for_modes` to gate per-mode prompts on the agent's tag set —
        {DooD} and {web} only apply to [code] agents."""
        return InstanceModifiers._prerequisites().get(self, frozenset()) <= tags

    @classmethod
    def colored_chain(cls, modifiers: Iterable["InstanceModifiers"]) -> str:
        """Space-separated ANSI-colored labels in canonical (declaration) order.
        Used directly by the status line; the picker parses this back into
        prompt_toolkit fragments via `ANSI_TO_PT_STYLE`. Each label
        self-resets so consecutive labels don't bleed colour."""
        return " ".join(m.colored_label() for m in cls.in_order(modifiers))

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
    def tags(self) -> tuple["InstanceModifiers", ...]:
        """Filename-grammar tags from the .md's stem, converted to typed
        modifier members (e.g. (InstanceModifiers.TAG_CODE,) for
        `name[code].md`). Tuple so the dataclass stays hashable should we
        ever want it as a dict key.

        Strings come out of `parse_stem`; we map each via `from_value` at
        this boundary so the rest of the launcher works with typed members.
        Unknown filename tags raise ValueError here (fail-fast on a typo'd
        filename, same contract modes get at the JSON-load boundary)."""
        return tuple(InstanceModifiers.from_value(t) for t in parse_stem(self.md_path.stem)[1])


@dataclass(frozen=True)
class InstanceIdentity(AgentIdentity):
    """Per-launch identity: agent + session + workspace + modes + is_brand_new.
    Fully resolved by the time it's constructed — either via resolve_target
    (which prompts workspace, session, and modes for brand-new launches) or
    via continuable_instances / resolve_pick (which loads workspace + modes
    off the maps for cont launches). Modes are typed enum members (not
    strings) — JSON-load boundaries convert via InstanceModifiers.from_value
    and raise on unknowns."""
    session: str                                # user-chosen suffix differentiating parallel instances of the same agent
    workspace: str | None                       # host-side path bind-mounted into the container at /workspace; None when continuable_instances built us from a stale workspace-map entry (resolve_target re-prompts; set_container_mounts also falls back to DEFAULT_WORKSPACE)
    is_brand_new: bool                          # True for a freshly-promoted AgentIdentity, False for a cont pick — drives resume + modes-persistence branches
    modes: tuple["InstanceModifiers", ...]      # per-instance opt-ins like (MODE_WARN_AUTO,) or (MODE_WARN_AUTO, MODE_WARN_DOOD); tuple keeps the dataclass hashable

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

    @property
    def chain(self) -> tuple[str, ...]:
        """Active modifier values for this launch in InstanceModifiers
        declaration order — BASE always first, then tags, then modes. Drives
        both the docker image build order (returned as-is from compose_chain)
        and the launch-time CLAUDE.md addendum order (consumed by
        memory_addendums.composed_addendum via `modifier.value in chain`).

        Chain stays string-shaped because its consumers (chain_image_tag,
        chain_compose_files) build image tags and Dockerfile filenames from
        these values directly. Both tags and modes are typed enum members
        by this point (AgentIdentity.tags / .modes convert at the property /
        JSON-load boundary respectively), so no runtime validation block is
        needed here — unknowns crash at the boundary rather than slipping
        through to compose."""
        return tuple(m.value for m in InstanceModifiers.in_order(
            (InstanceModifiers.BASE, *self.tags, *self.modes)))

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
