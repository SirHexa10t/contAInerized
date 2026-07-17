"""Interactive agent UI: full-screen picker (prompt_toolkit) plus supporting
line-prompt helpers for workspace path and session suffix. Pulls picker-entry
builders and state lookups from agents_crud; has no agent-domain logic.

Public API:

  select_agent(registry)
      Run the agent/session picker (main menu + nested deletion submenu) until the
      user picks something or cancels. Discovers agents/instances and handles
      deletions internally.
      -> Agent (new) | Instance (cont) | None on cancel/empty

  ask_for_workspace(agent, default=None)
      Line prompt for a workspace path; tab-completes against the host filesystem.
      -> absolute path string

  prompt_session(agent, workspace, current=None)
      Line prompt for a session suffix; rejects collisions with existing
      instances (except `current` — the modify flow's keep-the-name case).
      -> session suffix string

  prompt_tags(registry, current)
      Full-screen checkbox form over every discovered profession, specialty,
      and policy (registry order), pre-checked from `current` (an AgentBuild —
      `.lego` defaults for creates, the store entry for modifies). Labels
      carry each tag's kind punctuation + warn coloring + a `(requires: …)`
      parenthetical; checking a tag auto-checks its requirements and
      unchecking a requirement unchecks its dependents (live, in-form).
      combos.info warnings render live above the confirm row while their
      full tag set is checked. The engine axis isn't in the form (it stays
      whatever `current.engine` says — engine switching is a `.lego` edit).
      -> AgentBuild | None on cancel (Esc)

  checkbox_form(title, options, warnings=None, requires=None, wants=None)
      Generic full-screen multi-select form primitive behind prompt_tags.
      ↑↓ cycles rows (options + the confirm button), Space toggles, Enter
      confirms, Esc cancels. An option can render attached beneath an anchor
      option (`attached_to`) with a connector line — visual proximity for
      related options. `requires` maps option keys to prerequisite keys and
      drives the live check-cascade (see requires_closure); `wants` renders
      advisory unmet-companion messages in the warning zone (wants_warnings).
      -> list of checked option keys in display order | None on cancel

  pick_with_preview(title, entries, *, allow_delete=False, allow_modify=False)
      Generic full-screen picker primitive used by select_agent.
      -> (PickerAction.SELECT, value) | (PickerAction.DELETE, value)
         | (PickerAction.MODIFY, value) | (None, None) on cancel

  confirm_dialog(message)
      Inline [y/N] prompt.
      -> bool

  print_launch_banner(inst, cred_names)
      Print the multi-line "about to launch" summary (agent definition, engine,
      per-axis tag lines, creds, user whitelist count) before docker builds
      the image. Conditional lines — only shown when applicable; everything
      comes off the Instance. The user-whitelist line counts
      user_firewall_whitelist_lines() on demand only when {firewall} is active.

Generic-picker entry shape (pick_with_preview):
    {
        'display':    str | list[(style, text)] | FormattedText,
        'preview':    str,
        'value':      any,    # opaque; returned to the caller on selection
        'deletable':  bool,   # optional; defaults True. When False, Del is a no-op on this row.
        'modifiable': bool,   # optional; defaults True. When False, F2 is a no-op on this row.
    }
"""

import dataclasses
import io
import readline
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from prompt_toolkit import Application                                     # dep — declared in pyproject.toml [project]
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.styles import Style
from rich.console import Console                                           # dep — declared in pyproject.toml [project]
from rich.markdown import Markdown
from rich.theme import Theme

from .agents_crud import (
    creatable_agents, delete_instance, engine_sort_key, instance_from_store,
    list_all_instances, modify_instance,
)
from .file_access import (
    expand_user_path, home_dir, is_dir,
    path_exists, read_text, resolved_cwd, resolved_path,
    tab_complete_paths, user_firewall_whitelist_lines,
)
from .paths import (
    DEFAULT_WORKSPACE, DEFAULTING_DIRS, DOCKERIZED_CLAUDE_ROOT,
    FIREWALL_WHITELIST_FILE, instance_state_dir_path,
)
from .tags import (
    Agent, AgentBuild, Instance, Registry, Tag, resolve_build,
)
from .tags.identity import SESSION_SEP
from .utils import ordering_index_or_end, plural, relative_time


# ============================================================
# UI strings
# ============================================================

HINT_BASE_TEXT       = "↑↓ navigate  •  type to filter  •  Enter select  •  Esc cancel"
HINT_DELETE_SUFFIX   = "  •  Del delete"
HINT_MODIFY_SUFFIX   = "  •  F2 modify"
HINT_LEGEND_SUFFIX   = "  •  F8 legend"
HINT_LEGEND_OPEN     = "F8 / Esc close legend"
FILTER_LABEL         = "filter: "
EMPTY_FILTER_MESSAGE = "(no matches)"
DIVIDER_CHAR         = "│"
CONFIRM_PROMPT_FMT   = "{message}  [y/N]: "
CONFIRM_YES_ANSWERS  = ("y", "yes")

# Checkbox form (see checkbox_form below)
FORM_HINT_TEXT      = "↑↓ navigate  •  Space toggle  •  Enter confirm  •  Esc cancel"
FORM_CONFIRM_LABEL  = "[ Confirm ]"
CHECKBOX_ON         = "[x] "
CHECKBOX_OFF        = "[ ] "
ATTACHED_CONNECTOR  = "  └─ "        # prefix for options rendered attached beneath their anchor
TITLE_TAGS_FORM     = "Configure instance tags  (Space to toggle):"

# ============================================================
# Layout
# ============================================================

LIST_WEIGHT    = 2
PREVIEW_WEIGHT = 3
TITLE_HEIGHT   = 1
STATUS_HEIGHT  = 2
DIVIDER_WIDTH  = 1
PAGE_JUMP      = 10  # rows skipped per PageUp/PageDown

# Style class names + their corresponding style strings live as the
# PickerClass enum below.

# ============================================================
# Agent-picker UI strings
# ============================================================

TITLE_AGENT_PICKER = "Select an agent:"
TITLE_DELETE_MENU  = "‼️  DELETE AGENT INSTANCES  ‼️"

# Row marker glyphs + their styles live on the PickerRowMarker enum below.
# Cwd-relation labels ("(CURRENT DIR) " / "(DEFAULT DIR) ") live on
# PickerCwdHint there too.

DELMENU_LABEL  = "(Move onto deletions menu)"
BACK_LABEL     = "(Move back to Agent Selection)"
DELMENU_PREVIEW = "Open the deletion sub-menu to remove agent instances and their state directories."
BACK_PREVIEW    = "Return to the main agent picker."
CONFIRM_DELETE_FMT = "Delete '{name}'?"

# ============================================================
# Agent-picker styles (inline, applied per-segment)
# ============================================================

STYLE_AGENT_NAME     = "bold fg:ansibrightblue"
STYLE_DEL_NAME       = "bold fg:ansired"
STYLE_WORKSPACE_HINT = "italic fg:ansibrightblack"
# Tag coloring is warn-driven: a warn-flagged specialty renders bold bright
# red, everything else bright green (`_tag_style`). The warn flag lives on
# the tag itself (specialty tag.info), so adding a new dangerous specialty
# doesn't require touching the picker.
STYLE_TAG_WARN = "bold fg:ansibrightred"
STYLE_TAG_SAFE = "fg:ansibrightgreen"


def _tag_style(tag: Tag) -> str:
    """The picker style for one tag's label — red when the tag carries a
    truthy `warn` (specialties only; other kinds have no such field)."""
    return STYLE_TAG_WARN if getattr(tag, "warn", False) else STYLE_TAG_SAFE


class PickerAction(Enum):
    """Closed set of actions pick_with_preview returns alongside the selected
    entry's value. None (returned for cancel/escape) sits outside the enum so
    callers can branch on `if action is None` idiomatically."""
    SELECT = "select"     # Enter — user picked a row
    DELETE = "delete"     # Del   — user pressed delete on a row (only fires for deletable rows)
    MODIFY = "modify"     # F2    — user pressed modify on a row (only fires for modifiable rows)


class PickerClass(Enum):
    """prompt_toolkit CSS-like class names + the style applied to spans tagged
    with each. Bundling both on the enum member keeps the class name and its
    style co-located (adding a new entry threads through STYLE_DICT below
    without a second hand-maintained list). Members expose:
      .cls_name — the CSS-like class string used in prompt_toolkit style refs
      .style    — the prompt_toolkit style string applied to that class
      .css      — `class:<cls_name>` — what span tuples want as the style key
    """
    TITLE    = ("picker-title",    "bold fg:ansibrightcyan")
    DIVIDER  = ("picker-divider",  "fg:ansibrightblack")
    STATUS   = ("picker-status",   "fg:ansibrightblack")
    FILTER   = ("picker-filter",   "bold fg:ansiyellow")
    CURSOR   = ("picker-cursor",   "reverse")
    PREVIEW  = ("picker-preview",  "")
    NO_MATCH = ("picker-no-match", "italic fg:ansibrightblack")
    WARNING  = ("picker-warning",  "bold fg:ansibrightred")

    def __init__(self, cls_name: str, style: str) -> None:
        self.cls_name = cls_name
        self.style = style

    @property
    def css(self) -> str:
        return f"class:{self.cls_name}"


STYLE_DICT = {e.cls_name: e.style for e in PickerClass}


class PickerRowMarker(Enum):
    """Row prefix marker — pairs the glyph that prefixes a row with the style
    applied to it. Bundling so that 'kind of row' is one named thing instead of
    a (glyph, style) pair manually assembled at each call site. The shared
    DEL_MARKER style is preserved by giving DELMNU and DLET the same colour
    string — they're two different *markers* that happen to render the same.

    Members expose:
      .glyph      — the marker text (emoji + label)
      .style      — prompt_toolkit style applied to the glyph
      .fragment() — (style, glyph+suffix) tuple ready for a FormattedText segment
    """
    NEW    = ("✨ Create",       "fg:ansigreen")
    CONT   = ("🏷️ Cont.",        "fg:ansiyellow")
    DELMNU = ("⚠️ DELETE‼️",     "fg:ansired")
    DLET   = ("🗑 DELETE",       "fg:ansired")
    BACK   = ("🚪  Back",        "")

    def __init__(self, glyph: str, style: str) -> None:
        self.glyph = glyph
        self.style = style

    def fragment(self, suffix: str = "") -> tuple[str, str]:
        """Build the (style, text) tuple FormattedText expects — glyph then an
        arbitrary suffix (spacing for column alignment, or extra trailing text
        like the back-row's label) in this marker's style."""
        return (self.style, f"{self.glyph}{suffix}")


class PickerCwdHint(Enum):
    """The cwd-relation tag shown on a Cont row's workspace. CURRENT/DEFAULT
    mark a healthy relation to where the launcher was invoked from; INVALID
    flags a stored workspace path that no longer exists / isn't a directory
    so the user can spot it before continuing (or hit F2 to repoint it).
    Same bundling rationale as PickerRowMarker — label text and style are a
    fixed pair, not two parallel constants. CURRENT/DEFAULT share a yellow
    style; kept as separate enum members so the colours can diverge later
    without re-threading call sites."""
    CURRENT = ("(CURRENT DIR) ", "bold fg:ansiyellow")
    DEFAULT = ("(DEFAULT DIR) ", "bold fg:ansiyellow")
    INVALID = ("(INVALID DIR) ", "bold fg:ansired")

    def __init__(self, label: str, style: str) -> None:
        self.label = label
        self.style = style

    @property
    def fragment(self) -> tuple[str, str]:
        """(style, label) tuple ready for a FormattedText segment. Property
        rather than method since the label is fixed — no per-call suffix."""
        return (self.style, self.label)


NO_WORKSPACE_DISPLAY = "?"            # subtitle placeholder when a Cont row's store entry is missing or stale


@dataclass(frozen=True)
class ContEntry:
    """One Cont/DELETE row's data — what `continuable_instances` produces and
    `pick_with_preview` consumes. `identity` is what the picker hands back
    on selection; the *_display strings are pre-rendered for the agent-name
    column / hint area; the is_*_dir booleans drive the
    CURRENT/DEFAULT/INVALID workspace tags (only one can be True per row —
    invalid implies ws_resolved is None, which makes the other two False)."""
    identity: Instance
    tags_display: str
    workspace_display: str
    is_current_dir: bool
    is_default_dir: bool
    is_invalid_dir: bool
    last_used_display: str

    @property
    def preview(self) -> str:
        """Cont-row preview markdown rendered to ANSI. Italic lead-in,
        horizontal rule, then a YAML-fenced metadata block (rich syntax-colors
        keys/values)."""
        inst = self.identity
        return _render_md(
            f"*Continue session `{inst.instance}`.*\n\n"
            f"---\n\n"
            f"```yaml\n"
            f"Agent:     {inst.agent}\n"
            f"Session:   {inst.session}\n"
            f"Workspace: {self.workspace_display}\n"
            f"Engine:    {inst.engine.name if inst.engine else '(default)'}\n"
            f"Tags:      {self.tags_display}\n"
            f"State:     {inst.state_dir}\n"
            f"Last used: {self.last_used_display}\n"
            f"```\n"
        )


@dataclass(frozen=True)
class PickerEntry:
    """One row in `pick_with_preview`. `display` is the prompt_toolkit
    FormattedText fragment list (list of (style, text) tuples), `preview` is
    the right-pane markdown rendered to ANSI, `value` is what the picker
    hands back on selection (Agent for Create rows, Instance
    for Cont/Delete rows, `_OPEN_DELMENU` for the delete-menu opener, `None`
    for Back rows). `deletable` / `modifiable` default True; the producer
    sets them False to disable Del / F2 on the row (Create / Back / opener).
    `display` defaults to a fresh empty list per instance to keep the
    dataclass safe — never shared across rows."""
    display: list[tuple[str, str]] = field(default_factory=list)
    preview: str = ""
    value: Any = None
    deletable: bool = True
    modifiable: bool = True


# Sentinel entry value signalling "open the delete submenu" — used in the
# main picker where most rows hold an identity dataclass; this is the one
# non-identity row, so a distinct singleton lets the dispatcher match by
# `is` rather than tagging identities with extra metadata.
_OPEN_DELMENU = object()


def continuable_instances(registry: Registry) -> list[ContEntry]:
    """ContEntry list for the picker's Cont/DELETE rows. Orphans (missing .md)
    skipped — instance_from_store returns None for those. Sorted by active
    tag set (tag-less first, then registry order: specialties dominate,
    professions next), then engine capability, then agent/session. Marks
    instances whose workspace resolves to the current working directory (for
    the picker's CURRENT DIR hint). The contained Instance is what the picker
    hands back on selection — stored workspace + resolved tag objects baked
    in so the modify flow's pre-fill reads straight off the identity.
    A store entry naming an unknown tag fails fast here (validate_build
    raises) — `python -m launch.audit` reports the same defect non-fatally
    when the picker is the wrong place to crash on a typo."""
    # Symlinks normalized via .resolve() so e.g. /home/<user> matches /var/users/<user>
    # when one symlinks to the other. Subdirs deliberately don't count — being in a
    # project under $HOME doesn't make /ai_workspace your "default" workspace.
    cwd = resolved_cwd()
    defaulting_dir_active = cwd in {resolved_path(d) for d in DEFAULTING_DIRS}
    default_workspace_resolved = resolved_path(DEFAULT_WORKSPACE)

    out = []
    for dir_name in list_all_instances():
        inst = instance_from_store(dir_name, registry)
        if inst is None:
            continue
        ws = inst.workspace
        ws_resolved = resolved_path(ws) if ws and is_dir(ws) else None
        last_mtime = inst.last_used_mtime
        active = (*inst.professions, *inst.specialties, *inst.policies)
        out.append(ContEntry(
            identity=inst,
            tags_display=", ".join(t.name for t in active) or "(none)",
            workspace_display=ws if ws else NO_WORKSPACE_DISPLAY,                                    # show stored value even when invalid; `?` sentinel only when no entry at all
            is_current_dir=ws_resolved == cwd,
            is_default_dir=defaulting_dir_active and ws_resolved == default_workspace_resolved,      # cwd ∈ DEFAULTING_DIRS and ws matches DEFAULT_WORKSPACE — tagged `(DEFAULT DIR)`
            is_invalid_dir=bool(ws) and ws_resolved is None,                                         # ws set but path doesn't exist / isn't a directory — tagged `(INVALID DIR)`
            last_used_display=relative_time(last_mtime) if last_mtime is not None else "(never)",
        ))

    spec_order, prof_order = list(registry.specialties), list(registry.professions)

    def cont_sort_key(e: ContEntry) -> tuple[Any, ...]:
        i = e.identity
        return (
            tuple(sorted(ordering_index_or_end(s.name, spec_order) for s in i.specialties)),
            tuple(sorted(ordering_index_or_end(p.name, prof_order) for p in i.professions)),
            engine_sort_key(i.conf.get("ANTHROPIC_MODEL", "")),
            i.agent,
            i.session,
        )

    out.sort(key=cont_sort_key)
    return out


def _render_md(text: str, *, theme: dict[str, str] | None = None) -> str:
    """Render markdown text to an ANSI-encoded string for the picker's preview pane.
    Width is fixed to 80; prompt_toolkit re-wraps if the pane is narrower. Optional
    `theme` (dict of Rich style names → style strings) overrides Markdown's defaults
    for this render — used by the legend to colour-code the tag tables."""
    buf = io.StringIO()
    Console(
        file=buf, force_terminal=True, color_system="truecolor", width=80,
        theme=Theme(theme) if theme else None,
    ).print(Markdown(text))
    return buf.getvalue()


def _build_composition_legend(registry: Registry) -> str:
    """Build the F8 'composition legend' shown over the preview pane — one
    markdown table per tag kind (engines, professions, specialties, policies),
    header = the kind's nutshell, rows = each discovered member's label +
    description first line. Warn-flagged specialties get their red inline via
    `_ANSI_BY_STYLE`; the `markdown.code: none` override stops rich's default
    code styling from overlaying the injected colors."""
    ANSI_BY_STYLE = {STYLE_TAG_WARN: "\033[01;91m", STYLE_TAG_SAFE: "\033[22;92m"}

    def colored(tag: Tag) -> str:
        return f"{ANSI_BY_STYLE[_tag_style(tag)]}{tag.label}\033[0m"

    def table(title: str, nutshell: str, members: Iterable[Tag]) -> str:
        rows = "\n".join(
            f"| {colored(t)} | {t.description.splitlines()[0] if t.description else ''} |"
            for t in members
        )
        return (
            f"# {title}\n\n{nutshell}\n\n"
            f"| {title[:-1]} | Description |\n|-----|-------------|\n{rows}\n\n"
        )

    # Engines stay out of the legend for now — the picker never renders engine
    # labels (engine choice lives in `.lego`, not the tag column or the form).
    md = (
        table("Professions", "Tools it can use — each is a docker image layer.", registry.professions.values())
        + table("Specialties", "Exceptional access or running conditions.", registry.specialties.values())
        + table("Policies",    "What it's permitted to do — pure settings fragments.", registry.policies.values())
    )
    return _render_md(md, theme={"markdown.code": "none"})


def _agent_description(md_text: str) -> str:
    """First line of an agent .md, stripped of any markdown heading marker — used as
    the right-hand description on a Create row in the picker. An empty .md
    yields "" rather than crashing the picker on splitlines()[0]."""
    return next(iter(md_text.splitlines()), "").lstrip("# ").strip()


def _create_preview(agent: Agent) -> str:
    """Build the Create-row preview markdown from a creatable_agents Agent
    and render to ANSI. Italic source line, horizontal rule, then the .md content as-is."""
    return _render_md(
        f"*Create a new instance of `{agent.name}` — `agents/{agent.md_path.name}`*\n\n"
        f"---\n\n"
        f"{read_text(agent.md_path)}"
    )


def _tags_column(tags: Iterable[Tag]) -> tuple[list[tuple[str, str]], int]:
    """Render a tag set for cont-row / Create-row display as prompt_toolkit
    `(style, text)` fragments — each tag's kind-punctuated label in its
    warn-aware color, space-separated. Returns (fragments, visible width).
    Empty input → ([], 0). A trailing space fragment is appended to non-empty
    output so the widest row in the column gets a built-in separator before
    its right neighbor (the agent / instance name)."""
    fragments: list[tuple[str, str]] = []
    for tag in tags:
        if fragments:
            fragments.append(("", " "))
        fragments.append((_tag_style(tag), tag.label))
    if not fragments:
        return [], 0
    fragments.append(("", " "))   # trailing separator — bakes into the column width
    visible = sum(len(text) for _, text in fragments)
    return fragments, visible


def _normalize(display: str | Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Coerce any accepted display form into a list of (style, text) tuples."""
    if isinstance(display, str):
        return [("", display)]
    return list(display)


def _plain(display: str | Iterable[tuple[str, str]]) -> str:
    """Plain-text view of a display, used for filter matching."""
    return "".join(text for _, text in _normalize(display))


# ============================================================
# Checkbox form (multi-select) — generic primitive behind prompt_tags
# ============================================================

@dataclass
class FormOption:
    """One checkbox row in `checkbox_form`. `key` is the canonical string the
    form returns when the box is checked (a tag name, for the tag form);
    `label` is the row's display (plain string or (style, text)
    fragments); `body` is the focused-row explanation shown under the list.
    `attached_to` names another option's key this row renders directly
    beneath, with a connector line — visual proximity for related options
    (e.g. a future standalone `firewall` beneath `auto`). Purely layout:
    no dependency logic — the user can check either, neither, or both."""
    key: str
    label: str | list[tuple[str, str]]
    body: list[str] = field(default_factory=list)
    checked: bool = False
    attached_to: str | None = None


def ordered_form_options(options: list[FormOption]) -> list[FormOption]:
    """Display order for form rows: anchor options keep their given order;
    each attached option is re-inserted directly after its anchor (several
    attachments to one anchor keep their given relative order). Options
    attached to an unknown key are appended at the end unattached-style —
    better a detached row than a vanished one."""
    anchors = [o for o in options if o.attached_to is None]
    known = {o.key for o in anchors}
    out: list[FormOption] = []
    for anchor in anchors:
        out.append(anchor)
        out.extend(o for o in options if o.attached_to == anchor.key)
    out.extend(o for o in options if o.attached_to is not None and o.attached_to not in known)
    return out


def active_warnings(checked: set[str],
                    warnings: dict[frozenset[str], tuple[str, list[str]]]) -> list[tuple[str, list[str]]]:
    """The (header, body) warning entries whose key-combination is fully
    covered by the checked set. The form's warning zone recomputes this on
    every toggle, so dangerous combinations surface the moment the last box
    of the combo is ticked — and disappear when it's unticked."""
    return [entry for combo, entry in warnings.items() if combo <= checked]


def wants_warnings(checked: set[str],
                   wants: dict[str, tuple[tuple[str, str], ...]]) -> list[tuple[str, list[str]]]:
    """(header, body) warning entries for every checked tag whose `[wants]`
    names an UNchecked tag — e.g. {auto} ticked without {firewall}. Rendered
    in the same red zone as combo warnings, live per toggle. A want never
    blocks confirmation — it's a request with a message, not a requirement."""
    return [
        (f"'{wanter}' wants '{wanted}':", message.splitlines())
        for wanter, entries in wants.items() if wanter in checked
        for wanted, message in entries if wanted not in checked
    ]


def requires_closure(key: str, requires: dict[str, frozenset[str]]) -> set[str]:
    """Transitive prerequisite set for `key`, excluding `key` itself — what
    must also be checked when `key` is checked. Drives checkbox_form's
    check-cascade in both directions: checking a key checks its closure;
    unchecking a key unchecks every checked option whose closure contains it.
    Keys absent from `requires` (or with no prerequisites) yield set()."""
    seen: set[str] = set()
    stack = [key]
    while stack:
        for dep in requires.get(stack.pop(), ()):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


def checkbox_form(title: str, options: list[FormOption],
                  warnings: dict[frozenset[str], tuple[str, list[str]]] | None = None,
                  requires: dict[str, frozenset[str]] | None = None,
                  wants: dict[str, tuple[tuple[str, str], ...]] | None = None) -> list[str] | None:
    """Render a full-screen multi-select form; block until confirm or cancel.

    ↑↓ cycle through the rows (options first, then the [ Confirm ] button,
    wrapping around); Space toggles the focused checkbox (or confirms, on
    the button); Enter confirms from anywhere; Esc / Ctrl-C cancels. The
    focused option's `body` renders in an explanation panel under the list;
    `warnings` entries whose combination is fully checked render live, in
    warning red, directly above the confirm button.

    `requires` maps option keys to prerequisite option keys and drives the
    live check-cascade: checking a box also checks its transitive
    prerequisites; unchecking a box that others depend on unchecks those
    dependents. No disabling or indentation — every row stays freely
    toggleable, the cascade just keeps the set consistent.

    `wants` maps option keys to their (wanted-key, message) requests —
    rendered in the warning zone while the wanter is checked and the wanted
    key isn't (see wants_warnings). Purely advisory.

    Returns the checked options' keys in display order, or None on cancel."""
    if not options:
        raise ValueError("options must be non-empty")
    rows = ordered_form_options(options)
    warning_map = warnings or {}
    req_map = requires or {}
    wants_map = wants or {}
    by_key = {o.key: o for o in rows}
    confirm_index = len(rows)              # the confirm button is the last navigable row
    row_count = len(rows) + 1
    state: dict[str, Any] = {"cursor": 0, "confirmed": False}

    def cascade(toggled: FormOption) -> None:
        """Keep the checked set requires-consistent after `toggled` flips."""
        if toggled.checked:
            for key in requires_closure(toggled.key, req_map):
                if key in by_key:
                    by_key[key].checked = True
        else:
            for opt in rows:
                if opt.checked and toggled.key in requires_closure(opt.key, req_map):
                    opt.checked = False

    def checked_keys() -> set[str]:
        return {o.key for o in rows if o.checked}

    def option_fragments() -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for i, opt in enumerate(rows):
            frags: list[tuple[str, str]] = []
            if opt.attached_to is not None:
                frags.append((PickerClass.STATUS.css, ATTACHED_CONNECTOR))
            frags.append(("", CHECKBOX_ON if opt.checked else CHECKBOX_OFF))
            frags.extend(_normalize(opt.label))
            if i == state["cursor"]:
                frags = [(f"{PickerClass.CURSOR.css} {style}".strip(), text)
                         for style, text in frags]
            out.extend(frags)
            out.append(("", "\n"))
        if out:
            out.pop()   # trailing newline
        return out

    def body_fragments() -> list[tuple[str, str]]:
        if state["cursor"] >= confirm_index:
            return []
        lines = rows[state["cursor"]].body
        return [("", "\n".join(f"  {line}" if line else "" for line in lines))]

    def warning_fragments() -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        checked = checked_keys()
        entries = active_warnings(checked, warning_map) + wants_warnings(checked, wants_map)
        for header, body in entries:
            out.append((PickerClass.WARNING.css, f"  {header}\n"))
            out.extend((PickerClass.WARNING.css, f"  {line}\n") for line in body)
        if out:
            out[-1] = (out[-1][0], out[-1][1].rstrip("\n"))
        return out

    def confirm_fragments() -> list[tuple[str, str]]:
        style = PickerClass.CURSOR.css if state["cursor"] == confirm_index else PickerClass.TITLE.css
        return [("", "  "), (style, FORM_CONFIRM_LABEL)]

    def title_fragments() -> list[tuple[str, str]]:
        return [(PickerClass.TITLE.css, title)]

    def hint_fragments() -> list[tuple[str, str]]:
        return [(PickerClass.STATUS.css, FORM_HINT_TEXT)]

    def cursor_pos() -> Point:
        return Point(0, min(state["cursor"], len(rows) - 1))

    def move(delta: int) -> None:
        state["cursor"] = (state["cursor"] + delta) % row_count

    def confirm(event: KeyPressEvent) -> None:
        state["confirmed"] = True
        event.app.exit()

    kb = KeyBindings()

    @kb.add("up")
    def _(event: KeyPressEvent) -> None: move(-1)

    @kb.add("down")
    def _(event: KeyPressEvent) -> None: move(1)

    @kb.add(" ")
    def _(event: KeyPressEvent) -> None:
        if state["cursor"] == confirm_index:
            confirm(event)
        else:
            opt = rows[state["cursor"]]
            opt.checked = not opt.checked
            cascade(opt)

    @kb.add("enter")
    def _(event: KeyPressEvent) -> None: confirm(event)

    @kb.add("escape")
    def _(event: KeyPressEvent) -> None: event.app.exit()

    @kb.add("c-c")
    def _(event: KeyPressEvent) -> None: event.app.exit()

    body_layout = HSplit([
        Window(FormattedTextControl(title_fragments), height=TITLE_HEIGHT),
        Window(height=1, char=" "),
        Window(FormattedTextControl(option_fragments,
                                    get_cursor_position=cursor_pos,
                                    focusable=True,
                                    show_cursor=False),
               wrap_lines=False, dont_extend_height=True),
        Window(height=1, char=" "),
        Window(FormattedTextControl(body_fragments), wrap_lines=True),   # flexible filler — focused option's explanation
        Window(FormattedTextControl(warning_fragments), wrap_lines=True, dont_extend_height=True),
        Window(height=1, char=" "),
        Window(FormattedTextControl(confirm_fragments), height=1),
        Window(FormattedTextControl(hint_fragments), height=STATUS_HEIGHT),
    ])

    Application(
        layout=Layout(body_layout),
        key_bindings=kb,
        style=Style.from_dict(STYLE_DICT),
        full_screen=True,
    ).run()

    if not state["confirmed"]:
        return None
    return [o.key for o in rows if o.checked]


def _tag_form_options(registry: Registry, current: AgentBuild) -> list[FormOption]:
    """FormOption rows for every discovered profession, specialty, and policy
    (registry order within each kind), pre-checked from `current`'s axis
    lists. Row label = the tag's warn-aware colored kind-punctuated label,
    its description's first line, and — when the tag has prerequisites — a
    dim `(requires: …)` parenthetical. Body = the full description. Keys are
    the tags' full names (what `.lego` / instances.toml store)."""
    checked = {*current.professions, *current.specialties, *current.policies}
    out: list[FormOption] = []
    for tag in (*registry.professions.values(), *registry.specialties.values(),
                *registry.policies.values()):
        label: list[tuple[str, str]] = [(_tag_style(tag), tag.label), ("", " ")]
        first_line = tag.description.splitlines()[0] if tag.description else ""
        label.append(("", first_line))
        if tag.requires:
            label.append((PickerClass.STATUS.css, f"  (requires: {', '.join(sorted(tag.requires))})"))
        out.append(FormOption(
            key=tag.name,
            label=label,
            body=tag.description.splitlines(),
            checked=tag.name in checked,
        ))
    return out


def _combo_warnings(registry: Registry) -> dict[frozenset[str], tuple[str, list[str]]]:
    """specialty/combos.info entries re-shaped for checkbox_form's warning
    zone: {tag-name set: (first message line, remaining lines)}."""
    out: dict[frozenset[str], tuple[str, list[str]]] = {}
    for combo in registry.combos:
        first, *rest = combo.message.splitlines() or [""]
        out[combo.tags] = (first, rest)
    return out


def _form_requires(registry: Registry) -> dict[str, frozenset[str]]:
    """{tag name: prerequisite tag names} across the three form kinds — the
    shape checkbox_form's check-cascade consumes. Tags without prerequisites
    are omitted (requires_closure treats absent keys as empty)."""
    return {
        tag.name: frozenset(tag.requires)
        for tag in (*registry.professions.values(), *registry.specialties.values(),
                    *registry.policies.values())
        if tag.requires
    }


def _form_wants(registry: Registry) -> dict[str, tuple[tuple[str, str], ...]]:
    """{tag name: its (wanted, message) requests} across the three form kinds
    — the shape checkbox_form's wants zone consumes. Tags without wants are
    omitted."""
    return {
        tag.name: tag.wants
        for tag in (*registry.professions.values(), *registry.specialties.values(),
                    *registry.policies.values())
        if tag.wants
    }


def pick_with_preview(title: str, entries: list[PickerEntry], *, allow_delete: bool = False, allow_modify: bool = False, legend_text: str | None = None) -> tuple[PickerAction | None, Any]:
    """Render a full-screen picker; block until the user picks or cancels.

    legend_text — optional ANSI string. When provided, F8 toggles it as an overlay
    over the preview pane (Esc closes it). The agent picker passes LEGEND_TEXT so
    users can recall what each tag's kind punctuation means."""
    if not entries:
        raise ValueError("entries must be non-empty")

    state: dict[str, Any] = {
        "cursor": 0,
        "filter": "",
        "shown": list(range(len(entries))),
        "result": (None, None),
        "legend_open": False,
    }

    def refilter() -> None:
        q = state["filter"].lower()
        state["shown"] = [i for i in range(len(entries))
                          if q in _plain(entries[i].display).lower()]
        if state["shown"] and state["cursor"] not in state["shown"]:
            state["cursor"] = state["shown"][0]
        elif not state["shown"]:
            state["cursor"] = 0

    def list_fragments() -> list[tuple[str, str]]:
        if not state["shown"]:
            return [(PickerClass.NO_MATCH.css, EMPTY_FILTER_MESSAGE)]
        out = []
        for i in state["shown"]:
            segments = _normalize(entries[i].display)
            if i == state["cursor"]:
                segments = [(f"{PickerClass.CURSOR.css} {style}".strip(), text)
                            for style, text in segments]
            out.extend(segments)
            out.append(("", "\n"))
        if out and out[-1] == ("", "\n"):
            out.pop()
        return out

    def preview_text() -> ANSI | str:
        if state["legend_open"] and legend_text is not None:
            return ANSI(legend_text)
        if not state["shown"]:
            return ""
        # Wrap in ANSI(...) so rich-rendered escape codes in Create-row previews show
        # as styled text. Plain previews (Cont rows, etc.) pass through unchanged.
        return ANSI(entries[state["cursor"]].preview)

    def title_fragments() -> list[tuple[str, str]]:
        return [(PickerClass.TITLE.css, title)]

    def status_fragments() -> list[tuple[str, str]]:
        if state["legend_open"]:
            hint = HINT_LEGEND_OPEN
        else:
            hint = HINT_BASE_TEXT
            if allow_delete:
                hint += HINT_DELETE_SUFFIX
            if allow_modify:
                hint += HINT_MODIFY_SUFFIX
            if legend_text is not None:
                hint += HINT_LEGEND_SUFFIX
        out = [(PickerClass.STATUS.css, hint), ("", "\n")]
        if state["filter"]:
            out.append((PickerClass.FILTER.css, FILTER_LABEL))
            out.append(("", state["filter"]))
        return out

    def cursor_pos() -> Point:
        if not state["shown"]:
            return Point(0, 0)
        return Point(0, state["shown"].index(state["cursor"]))

    kb = KeyBindings()

    def move(delta: int) -> None:
        if not state["shown"]:
            return
        n = len(state["shown"])
        i = state["shown"].index(state["cursor"])
        state["cursor"] = state["shown"][(i + delta) % n]

    @kb.add("up")
    def _(event: KeyPressEvent) -> None: move(-1)

    @kb.add("down")
    def _(event: KeyPressEvent) -> None: move(1)

    @kb.add("pageup")
    def _(event: KeyPressEvent) -> None: move(-PAGE_JUMP)

    @kb.add("pagedown")
    def _(event: KeyPressEvent) -> None: move(PAGE_JUMP)

    @kb.add("home")
    def _(event: KeyPressEvent) -> None:
        if state["shown"]:
            state["cursor"] = state["shown"][0]

    @kb.add("end")
    def _(event: KeyPressEvent) -> None:
        if state["shown"]:
            state["cursor"] = state["shown"][-1]

    @kb.add("enter")
    def _(event: KeyPressEvent) -> None:
        if state["shown"]:
            state["result"] = (PickerAction.SELECT, entries[state["cursor"]].value)
            event.app.exit()

    @kb.add("escape")
    def _(event: KeyPressEvent) -> None:
        if state["legend_open"]:
            state["legend_open"] = False
            return
        state["result"] = (None, None)
        event.app.exit()

    @kb.add("c-c")
    def _(event: KeyPressEvent) -> None:
        state["result"] = (None, None)
        event.app.exit()

    @kb.add("f8")
    def _(event: KeyPressEvent) -> None:
        if legend_text is not None:
            state["legend_open"] = not state["legend_open"]

    @kb.add("backspace")
    def _(event: KeyPressEvent) -> None:
        if state["filter"]:
            state["filter"] = state["filter"][:-1]
            refilter()

    @kb.add(Keys.Any)
    def _(event: KeyPressEvent) -> None:
        ch = event.data
        if ch and len(ch) == 1 and ch.isprintable():
            state["filter"] += ch
            refilter()

    if allow_delete:
        @kb.add("delete")
        def _on_delete_key(event: KeyPressEvent) -> None:
            if not state["shown"]:
                return
            entry = entries[state["cursor"]]
            if not entry.deletable:
                return  # silently ignored — caller marked this row non-deletable
            state["result"] = (PickerAction.DELETE, entry.value)
            event.app.exit()

    if allow_modify:
        @kb.add("f2")
        def _on_modify_key(event: KeyPressEvent) -> None:
            if not state["shown"]:
                return
            entry = entries[state["cursor"]]
            if not entry.modifiable:
                return  # silently ignored — caller marked this row non-modifiable
            state["result"] = (PickerAction.MODIFY, entry.value)
            event.app.exit()

    def accent_style() -> str:
        """Colour the preview's left-edge accent bar based on the selected row's kind:
        green for Create rows (Agent), yellow for Cont rows (Instance), dim
        default for menu/back rows."""
        if not state["shown"]:
            return PickerClass.DIVIDER.css
        value = entries[state["cursor"]].value
        if isinstance(value, Instance):             # cont row
            return PickerRowMarker.CONT.style       # fg:ansiyellow
        if isinstance(value, Agent):                # new row
            return PickerRowMarker.NEW.style        # fg:ansigreen
        return PickerClass.DIVIDER.css

    body = HSplit([
        Window(FormattedTextControl(title_fragments), height=TITLE_HEIGHT),
        VSplit([
            Window(
                FormattedTextControl(list_fragments,
                                     get_cursor_position=cursor_pos,
                                     focusable=True,
                                     show_cursor=False),
                wrap_lines=False,
                width=D(weight=LIST_WEIGHT),
            ),
            Window(width=DIVIDER_WIDTH, char=DIVIDER_CHAR, style=PickerClass.DIVIDER.css),
            Window(width=1, char="▌", style=accent_style),   # preview-side accent bar; colour reflects selected row's kind
            Window(
                FormattedTextControl(preview_text),
                wrap_lines=True,
                width=D(weight=PREVIEW_WEIGHT),
                style=PickerClass.PREVIEW.css,
            ),
        ]),
        Window(FormattedTextControl(status_fragments), height=STATUS_HEIGHT),
    ])

    Application(
        layout=Layout(body),
        key_bindings=kb,
        style=Style.from_dict(STYLE_DICT),
        full_screen=True,
    ).run()

    return state["result"]


def confirm_dialog(message: str) -> bool:
    """Inline yes/no prompt rendered below the (now-closed) picker."""
    answer = input(CONFIRM_PROMPT_FMT.format(message=message)).strip().lower()
    return answer in CONFIRM_YES_ANSWERS


def _path_completer(text: str, state: int) -> str | None:
    """Tab-complete `text` as a host filesystem path; expands `~` for matching."""
    matches = tab_complete_paths(text)
    return matches[state] if state < len(matches) else None


def ask_for_workspace(agent: str, default: str | None = None) -> str:
    """Prompt for a workspace path; Enter uses `default` (or DEFAULT_WORKSPACE).
    Tab completes against the host filesystem. Returns the absolute path with `~`
    expanded but symlinks preserved — the form the user typed is what gets stored."""
    default = default if default is not None else DEFAULT_WORKSPACE
    prior_completer = readline.get_completer()
    prior_delims = readline.get_completer_delims()
    readline.set_completer(_path_completer)
    readline.set_completer_delims(" \t\n")
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")  # macOS / BSD libedit syntax
    else:
        readline.parse_and_bind("tab: complete")        # GNU readline syntax
    try:
        while True:
            entered = input(
                f"Workspace path for '{agent}' instance [{default}]: "
            ).strip() or default
            absolute = expand_user_path(entered)
            if is_dir(absolute):
                return absolute
            print(f"Not a directory: {absolute}")
    finally:
        readline.set_completer(prior_completer)
        readline.set_completer_delims(prior_delims)


def prompt_session(agent: str, workspace: str, current: str | None = None) -> str:
    """Prompt for a session suffix. Default = `current` (the modify flow —
    keep the existing name) or the last segment of the workspace path (the
    create flow). Rejects collisions with existing `{agent}__{suffix}` state
    dirs — except `current` itself, which is always accepted (keeping your
    own name isn't a collision). Shared by both flows so the collision loop
    exists exactly once."""
    default = current if current is not None else Path(workspace).name
    while True:
        suffix = input(f"Session suffix for '{agent}' [{default}]: ").strip() or default
        if not suffix:
            print("Session suffix cannot be empty.")
            continue
        candidate = f"{agent}{SESSION_SEP}{suffix}"
        if suffix != current and path_exists(instance_state_dir_path(candidate)):
            print(f"Instance '{candidate}' already exists. Pick another name.")
            continue
        return suffix


def build_of(inst: Instance) -> AgentBuild:
    """The instance's current axis selections as name strings — the form's
    pre-check shape (and what resolve_build turns back into tag objects)."""
    return AgentBuild(
        engine=inst.engine.name if inst.engine else None,
        professions=tuple(p.name for p in inst.professions),
        specialties=tuple(s.name for s in inst.specialties),
        policies=tuple(p.name for p in inst.policies),
    )


def prompt_tags(registry: Registry, current: AgentBuild) -> AgentBuild | None:
    """Full-screen tag-selection form: one checkbox per discovered profession /
    specialty / policy (assembled by `_tag_form_options`, defaults from
    `current`), requires-cascade live in the form, combos.info warnings
    rendered above the confirm button while their combination is checked.
    Returns a new AgentBuild carrying `current.engine` plus the selection
    split back into its axes, or None when the user cancels (Esc) — callers
    abort their create / modify flow on None rather than persisting anything."""
    options = _tag_form_options(registry, current)
    keys = checkbox_form(TITLE_TAGS_FORM, options,
                         warnings=_combo_warnings(registry),
                         requires=_form_requires(registry),
                         wants=_form_wants(registry))
    if keys is None:
        return None
    picked = set(keys)
    return AgentBuild(
        engine=current.engine,
        professions=tuple(n for n in registry.professions if n in picked),
        specialties=tuple(n for n in registry.specialties if n in picked),
        policies=tuple(n for n in registry.policies if n in picked),
    )


def select_agent(registry: Registry) -> Agent | Instance | None:
    """Run the agent picker (main + nested deletion submenu) until selection or cancel.
    Caller must ensure at least one agent .md exists before invoking."""
    legend_text = _build_composition_legend(registry)   # built once per call — the loop below only re-scans instances
    while True:
        agents = creatable_agents(registry)
        instances = continuable_instances(registry)

        instances_by_agent: dict[str, list[ContEntry]] = {}
        for inst in instances:
            instances_by_agent.setdefault(inst.identity.agent, []).append(inst)

        agent_name_width = max(len(a.name) for a in agents)
        instance_name_width = max((len(i.identity.instance) for i in instances), default=0)

        # Tag column (Create rows) and tag column (Cont rows) are sized
        # INDEPENDENTLY — each scoped to its own population so a row's
        # agent / instance name sits tight against its tags. Tying the
        # two together (a shared max) pushed Create-row agent names way out
        # to align with the widest cont-row tag set, even though the columns
        # don't share a row.
        #
        # Create rows show the `.lego` default professions/specialties (the
        # names resolve through the registry for warn-aware coloring); Cont
        # rows show the instance's actual resolved tag objects.
        def build_tags(build: AgentBuild) -> list[Tag]:
            names = (*build.professions, *build.specialties, *build.policies)
            return [t for n in names if (t := registry.get(n)) is not None]

        tag_by_agent = {a.name: _tags_column(build_tags(a.build)) for a in agents}
        tag_by_inst = {i.identity.instance: _tags_column((*i.identity.professions, *i.identity.specialties, *i.identity.policies))
                       for i in instances}
        tag_col_width  = max((w for _, w in tag_by_agent.values()), default=0)
        cont_col_width = max((w for _, w in tag_by_inst.values()), default=0)

        entries: list[PickerEntry] = []
        for agent in agents:
            tag_frags, tag_len = tag_by_agent[agent.name]
            entries.append(PickerEntry(
                display=[
                    PickerRowMarker.NEW.fragment("  "),
                    *tag_frags,
                    ("", " " * (tag_col_width - tag_len)),
                    (STYLE_AGENT_NAME, f"{agent.name:<{agent_name_width}}"),
                    ("", f" — {_agent_description(read_text(agent.md_path))}"),
                ],
                preview=_create_preview(agent),
                value=agent,
                deletable=False,
                modifiable=False,
            ))
            for inst in instances_by_agent.get(agent.name, []):
                identity = inst.identity
                cont_frags, cont_len = tag_by_inst[identity.instance]
                cont_display = [
                    PickerRowMarker.CONT.fragment("      "),
                    *cont_frags,
                    ("", " " * (cont_col_width - cont_len)),
                    (STYLE_AGENT_NAME, f"{identity.instance:<{instance_name_width}}"),
                    ("", "    "),
                ]
                if inst.is_current_dir:
                    cont_display.append(PickerCwdHint.CURRENT.fragment)
                elif inst.is_default_dir:
                    cont_display.append(PickerCwdHint.DEFAULT.fragment)
                elif inst.is_invalid_dir:
                    cont_display.append(PickerCwdHint.INVALID.fragment)
                cont_display.append((STYLE_WORKSPACE_HINT, inst.workspace_display))
                entries.append(PickerEntry(
                    display=cont_display,
                    preview=inst.preview,
                    value=identity,
                ))

        entries.append(PickerEntry(
            display=[
                PickerRowMarker.DELMNU.fragment("  "),
                ("", DELMENU_LABEL),
            ],
            preview=DELMENU_PREVIEW,
            value=_OPEN_DELMENU,
            deletable=False,
            modifiable=False,
        ))

        action, value = pick_with_preview(TITLE_AGENT_PICKER, entries, allow_delete=True, allow_modify=True, legend_text=legend_text)
        if action is None:
            return None

        if action == PickerAction.DELETE:  # picker enforces deletability — only cont rows (Instance) reach here
            if confirm_dialog(CONFIRM_DELETE_FMT.format(name=value.instance)):
                delete_instance(value)
            continue

        if action == PickerAction.MODIFY:  # picker enforces modifiability — only cont rows reach here
            old_inst = value
            # Same prompt order as creation (resolve_target): workspace →
            # session → tags. The session prompt is the shared one — with
            # current= it accepts keeping the existing name.
            new_workspace = ask_for_workspace(old_inst.agent, default=old_inst.workspace)
            new_session = prompt_session(old_inst.agent, new_workspace, current=old_inst.session)
            new_build = prompt_tags(registry, build_of(old_inst))
            if new_build is None:   # Esc on the tag form — abort the modify, back to the picker
                continue
            new_inst = dataclasses.replace(
                old_inst, session=new_session, workspace=new_workspace,
                **resolve_build(new_build, old_inst.agent, registry),
            )  # is_brand_new stays False via the dataclass replace
            modify_instance(old_inst, new_inst)
            continue

        if value is _OPEN_DELMENU:
            _delete_submenu(registry, legend_text)
            continue

        return value  # Agent (new) | Instance (cont)


def _delete_submenu(registry: Registry, legend_text: str) -> None:
    """Flat deletion submenu — every row red. Loops until Esc / Back."""
    while True:
        instances = continuable_instances(registry)
        if not instances:
            return
        entries: list[PickerEntry] = []
        for inst in instances:
            identity = inst.identity
            entries.append(PickerEntry(
                display=[
                    PickerRowMarker.DLET.fragment("  "),
                    (STYLE_DEL_NAME, identity.instance),
                ],
                preview=inst.preview,
                value=identity,
            ))
        entries.append(PickerEntry(
            display=[PickerRowMarker.BACK.fragment(f"  {BACK_LABEL}")],
            preview=BACK_PREVIEW,
            value=None,
            deletable=False,
        ))

        action, value = pick_with_preview(TITLE_DELETE_MENU, entries, allow_delete=True, legend_text=legend_text)
        if action is None or value is None:
            return
        if confirm_dialog(CONFIRM_DELETE_FMT.format(name=value.instance)):
            delete_instance(value)


def print_launch_banner(inst: Instance, cred_names: list[str]) -> None:
    """Print the multi-line summary that appears before docker builds the
    image — agent definition path, engine, one line per active tag axis, and
    creds counts when applicable. Each line is conditional on having
    something to show (no empty 'Professions: ' if there are none). The
    user-whitelist line counts user_firewall_whitelist_lines() inline —
    only when {firewall} is active, so other launches don't touch the file
    at all. Takes the launch's Instance and pulls everything off it directly;
    kind punctuation comes from each tag's `.label`."""
    print(f"  Agent definition: {inst.md_path.relative_to(DOCKERIZED_CLAUDE_ROOT)}")
    if inst.engine:
        print(f"  Engine:           {inst.engine.label} — {inst.engine.path.relative_to(DOCKERIZED_CLAUDE_ROOT)}")
    if inst.professions:
        print(f"  Professions:      {' '.join(p.label for p in inst.professions)}")
    if inst.specialties:
        print(f"  Specialties:      {' '.join(s.label for s in inst.specialties)}")
    if inst.policies:
        print(f"  Policies:         {' '.join(p.label for p in inst.policies)}")
    if cred_names:
        print(f"  Optional creds:   {', '.join(cred_names)} (from user_extras/optional_creds/)")
    if any(s.name == "firewall" for s in inst.specialties):
        whitelist_count = len(user_firewall_whitelist_lines())
        display_path = "~/" + str(FIREWALL_WHITELIST_FILE.relative_to(home_dir()))
        print(f"  User whitelist:   {whitelist_count} domain{plural(whitelist_count)} (from {display_path})")
    # Unmet wants — advisory, never blocking: an active tag requested a
    # companion that isn't active (e.g. {auto} without {firewall}). The form
    # shows the same message live; repeating it here catches CLI-named and
    # store-migrated launches that never pass through the form.
    RED, RESET = "\033[01;91m", "\033[0m"
    for wanter, wanted, message in inst.unmet_wants:
        print(f"  {RED}⚠ '{wanter}' wants '{wanted}' (not active):{RESET}")
        for line in message.splitlines():
            print(f"      {RED}{line}{RESET}")
