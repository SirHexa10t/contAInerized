"""Interactive agent UI: full-screen picker (prompt_toolkit) plus supporting
line-prompt helpers for workspace path and session suffix. Pulls picker-entry
builders and state lookups from agents_crud; has no agent-domain logic.

Public API:

  select_agent()
      Run the agent/session picker (main menu + nested deletion submenu) until the
      user picks something or cancels. Discovers agents/instances and handles
      deletions internally.
      -> AgentIdentity (new) | InstanceIdentity (cont) | None on cancel/empty

  ask_for_workspace(agent, default=None)
      Line prompt for a workspace path; tab-completes against the host filesystem.
      -> absolute path string

  prompt_session(agent, workspace, current=None)
      Line prompt for a session suffix; rejects collisions with existing
      instances (except `current` — the modify flow's keep-the-name case).
      -> session suffix string

  prompt_modes(tags, current_modes=())
      Run all applicable mode prompts in InstanceModifiers.modes() priority
      order (auto; DooD / web only for [code] agents); pre-fills defaults from
      the existing modes list. Header / body copy per modifier lives in
      template_code/modifier_prompts.py. Used by run.py (new instances) and
      select_agent's modify flow.
      -> list[InstanceModifiers] of newly-selected modes

  pick_with_preview(title, entries, *, allow_delete=False, allow_modify=False)
      Generic full-screen picker primitive used by select_agent.
      -> (PickerAction.SELECT, value) | (PickerAction.DELETE, value)
         | (PickerAction.MODIFY, value) | (None, None) on cancel

  confirm_dialog(message)
      Inline [y/N] prompt.
      -> bool

  print_launch_banner(inst_id, cred_names)
      Print the multi-line "about to launch" summary (agent definition, conf,
      tags, modes, skills, creds, user whitelist count) before docker compose
      builds the image. Conditional lines for tags/modes/skills/creds/whitelist
      — only shown when applicable. md_path / conf_path / tags / modes all come
      off the InstanceIdentity. The user-whitelist line counts
      user_firewall_whitelist_lines() on demand only when {auto} is in modes.

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
import re
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

from .agent_modifiers_handler import prompt_for_modes
from .agents_crud import (
    agent_sort_key, creatable_agents, delete_instance,
    list_all_instances, mode_sort_key, modify_instance,
)
from .file_access import (
    agent_md_index, expand_user_path, home_dir, is_dir, load_modes_map,
    load_workspace_map, path_exists, read_text, resolved_cwd, resolved_path,
    tab_complete_paths, user_firewall_whitelist_lines,
)
from .paths import (
    DEFAULT_WORKSPACE, DEFAULTING_DIRS, DOCKERIZED_CLAUDE_ROOT,
    FIREWALL_WHITELIST_FILE,
)
from .structs import (
    ANSI_TO_PT_STYLE, AgentIdentity, InstanceIdentity, InstanceModifiers, SESSION_SEP,
)
from .utils import plural, relative_time


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
# Tag / mode coloring is per-member — InstanceModifiers.colored_label decides
# (green for safe, bold red for `_WARN_`-prefixed members). No central
# STYLE_TAG / STYLE_MODE_WARNING constant exists here: the color lives with
# the modifier so adding a new dangerous mode doesn't require touching the
# picker.


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


NO_WORKSPACE_DISPLAY = "?"            # subtitle placeholder when a Cont row's workspace map entry is missing or stale


@dataclass(frozen=True)
class ContEntry:
    """One Cont/DELETE row's data — what `continuable_instances` produces and
    `pick_with_preview` consumes. `identity` is what the picker hands back
    on selection; the *_display strings are pre-rendered for the agent-name
    column / hint area; the is_*_dir booleans drive the
    CURRENT/DEFAULT/INVALID workspace tags (only one can be True per row —
    invalid implies ws_resolved is None, which makes the other two False)."""
    identity: InstanceIdentity
    modes_display: str
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
        inst_id = self.identity
        return _render_md(
            f"*Continue session `{inst_id.instance}`.*\n\n"
            f"---\n\n"
            f"```yaml\n"
            f"Agent:     {inst_id.agent}\n"
            f"Session:   {inst_id.session}\n"
            f"Workspace: {self.workspace_display}\n"
            f"Modes:     {self.modes_display}\n"
            f"State:     {inst_id.state_dir}\n"
            f"Last used: {self.last_used_display}\n"
            f"```\n"
        )


@dataclass(frozen=True)
class PickerEntry:
    """One row in `pick_with_preview`. `display` is the prompt_toolkit
    FormattedText fragment list (list of (style, text) tuples), `preview` is
    the right-pane markdown rendered to ANSI, `value` is what the picker
    hands back on selection (AgentIdentity for Create rows, InstanceIdentity
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


def continuable_instances() -> list[ContEntry]:
    """ContEntry list for the picker's Cont/DELETE rows. Orphans (missing .md)
    skipped. Sorted first by mode set (mode-less first, then groups ordered by
    each mode's position in InstanceModifiers.modes()); within each mode group,
    sorted by (agent rank, session) as before. Marks instances whose workspace
    resolves to the current working directory (for the picker's CURRENT DIR
    hint). The contained InstanceIdentity is what the picker hands back on
    selection — stored workspace + modes are baked in so the modify flow's
    pre-fill can read them straight off the identity."""
    # Symlinks normalized via .resolve() so e.g. /home/<user> matches /var/users/<user>
    # when one symlinks to the other. Subdirs deliberately don't count — being in a
    # project under $HOME doesn't make /ai_workspace your "default" workspace.
    cwd = resolved_cwd()
    defaulting_dir_active = cwd in {resolved_path(d) for d in DEFAULTING_DIRS}
    default_workspace_resolved = resolved_path(DEFAULT_WORKSPACE)
    workspace_map = load_workspace_map()
    modes_map = load_modes_map()

    out = []
    for dir_name in list_all_instances():
        agent, _, session = dir_name.partition(SESSION_SEP)
        if agent not in agent_md_index():
            continue
        # Convert JSON string values → typed enum members at this boundary.
        # `from_value` raises ValueError on unknowns (defective modes-map
        # entries fail fast here — use `python -m launch.audit` to find them
        # non-fatally if the picker is the wrong place to crash on a typo).
        modes = tuple(InstanceModifiers.from_value(s) for s in modes_map.get(dir_name, []))
        ws = workspace_map.get(dir_name)
        ws_resolved = resolved_path(ws) if ws and is_dir(ws) else None
        inst_id = InstanceIdentity(agent=agent, session=session, workspace=ws, is_brand_new=False, modes=modes)
        last_mtime = inst_id.last_used_mtime
        out.append(ContEntry(
            identity=inst_id,
            modes_display=", ".join(m.value for m in modes) or "(none)",
            workspace_display=ws if ws else NO_WORKSPACE_DISPLAY,                                    # show stored value even when invalid; `?` sentinel only when no map entry at all
            is_current_dir=ws_resolved == cwd,
            is_default_dir=defaulting_dir_active and ws_resolved == default_workspace_resolved,      # cwd ∈ DEFAULTING_DIRS and ws matches DEFAULT_WORKSPACE — tagged `(DEFAULT DIR)`
            is_invalid_dir=bool(ws) and ws_resolved is None,                                         # ws set but path doesn't exist / isn't a directory — tagged `(INVALID DIR)`
            last_used_display=relative_time(last_mtime) if last_mtime is not None else "(never)",
        ))
    out.sort(key=lambda e: (
        mode_sort_key(e.identity.modes),                                # mode-less sinks to top; rest follow InstanceModifiers.modes() positions
        agent_sort_key((e.identity.agent, e.identity.md_path)),         # within each mode group: family/version/name
        e.identity.session,                                             # then session for tiebreak between instances of the same agent
    ))
    return out


def _render_md(text: str, *, theme: dict[str, str] | None = None) -> str:
    """Render markdown text to an ANSI-encoded string for the picker's preview pane.
    Width is fixed to 80; prompt_toolkit re-wraps if the pane is narrower. Optional
    `theme` (dict of Rich style names → style strings) overrides Markdown's defaults
    for this render — used by the legend to colour-code tag vs mode entries."""
    buf = io.StringIO()
    Console(
        file=buf, force_terminal=True, color_system="truecolor", width=80,
        theme=Theme(theme) if theme else None,
    ).print(Markdown(text))
    return buf.getvalue()


def _build_composition_legend() -> str:
    """Build the F8 'composition legend' shown over the preview pane.
    Tags + modes render as markdown tables for layout consistency with the
    Create-row previews. Per-row coloring comes from each modifier's
    `colored_label()` — warning-aware ANSI inlined into the markdown table
    source. The `markdown.code: none` override stops rich's default code
    styling from overlaying — letting the injected ANSI colors win."""
    rows_tags = "\n".join(
        f"| {m.colored_label()} | {m.description} |"
        for m in InstanceModifiers.tags()
    )
    rows_modes = "\n".join(
        f"| {m.colored_label()} | {m.description} |"
        for m in InstanceModifiers.modes()
    )
    tags_md = (
        "# Tags\n\n"
        "Agent core-affinities; dictate requirements and tools.\n\n"
        "| Tag | Description |\n"
        "|-----|-------------|\n"
        f"{rows_tags}\n"
    )
    modes_md = (
        "# Modes\n\n"
        "Special approach/capability activation.\n\n"
        "| Mode | Description |\n"
        "|------|-------------|\n"
        f"{rows_modes}\n"
    )
    return _render_md(tags_md + modes_md, theme={"markdown.code": "none"})


LEGEND_TEXT = _build_composition_legend()   # module-level so the picker doesn't rebuild on every keypress


def _agent_description(md_text: str) -> str:
    """First line of an agent .md, stripped of any markdown heading marker — used as
    the right-hand description on a Create row in the picker. An empty .md
    yields "" rather than crashing the picker on splitlines()[0]."""
    return next(iter(md_text.splitlines()), "").lstrip("# ").strip()


def _create_preview(agent: AgentIdentity) -> str:
    """Build the Create-row preview markdown from a creatable_agents AgentIdentity
    and render to ANSI. Italic source line, horizontal rule, then the .md content as-is."""
    return _render_md(
        f"*Create a new instance of `{agent.agent}` — `agents/{agent.md_path.name}`*\n\n"
        f"---\n\n"
        f"{read_text(agent.md_path)}"
    )


# Splitter for colored_chain output — captures each ANSI key in the
# ANSI_TO_PT_STYLE mapping. With the capture group, `re.split` returns
# [text-before-first-key, key, text, key, text, ...]: walking
# (parts[1::2], parts[2::2]) as (key, text) pairs gives one tuple per
# styled run.
_KEYS_PATTERN = re.compile("(" + "|".join(re.escape(k) for k in ANSI_TO_PT_STYLE) + ")")


def _modifier_display(modifiers: Iterable[InstanceModifiers]) -> tuple[list[tuple[str, str]], int]:
    """Render a modifier set for cont-row / Create-row display: parse
    `InstanceModifiers.colored_chain`'s ANSI output into prompt_toolkit
    `(style, text)` fragments via the `ANSI_TO_PT_STYLE` mapping. Returns
    (fragments, visible width). Empty input → ([], 0). A trailing space
    fragment is appended to non-empty output so the widest row in the column
    gets a built-in separator before its right neighbor (the agent /
    instance name)."""
    chain = InstanceModifiers.colored_chain(modifiers)
    if not chain:
        return [], 0
    parts = _KEYS_PATTERN.split(chain)
    fragments: list[tuple[str, str]] = [
        (ANSI_TO_PT_STYLE[key], text)
        for key, text in zip(parts[1::2], parts[2::2])
        if text
    ]
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


def pick_with_preview(title: str, entries: list[PickerEntry], *, allow_delete: bool = False, allow_modify: bool = False, legend_text: str | None = None) -> tuple[PickerAction | None, Any]:
    """Render a full-screen picker; block until the user picks or cancels.

    legend_text — optional ANSI string. When provided, F8 toggles it as an overlay
    over the preview pane (Esc closes it). The agent picker passes LEGEND_TEXT so
    users can recall what each [tag] / {mode} marker means."""
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
        green for Create rows (AgentIdentity), yellow for Cont rows
        (InstanceIdentity), dim default for menu/back rows."""
        if not state["shown"]:
            return PickerClass.DIVIDER.css
        value = entries[state["cursor"]].value
        if isinstance(value, InstanceIdentity):     # cont row — checked before AgentIdentity since InstanceIdentity isa AgentIdentity
            return PickerRowMarker.CONT.style       # fg:ansiyellow
        if isinstance(value, AgentIdentity):        # new row — plain AgentIdentity only
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
        if suffix != current and path_exists(InstanceIdentity.state_dir_for(agent, suffix)):
            print(f"Instance '{InstanceIdentity.instance_name(agent, suffix)}' already exists. Pick another name.")
            continue
        return suffix


def prompt_modes(tags: tuple[InstanceModifiers, ...], current_modes: tuple[InstanceModifiers, ...] = ()) -> list[InstanceModifiers]:
    """Thin wrapper over `agent_modifiers_handler.prompt_for_modes` — the actual
    dispatch logic (priority order + per-mode applicability gates + header /
    body lookup) lives in agent_modifiers_handler. This wrapper preserves the
    `from launch.menu_picker import prompt_modes` public-API shape that
    run.py and select_agent's modify flow rely on."""
    return prompt_for_modes(tags, current_modes)


def select_agent() -> AgentIdentity | InstanceIdentity | None:
    """Run the agent picker (main + nested deletion submenu) until selection or cancel.
    Caller must ensure at least one agent .md exists before invoking."""
    while True:
        agents = creatable_agents()
        instances = continuable_instances()

        instances_by_agent: dict[str, list[ContEntry]] = {}
        for inst in instances:
            instances_by_agent.setdefault(inst.identity.agent, []).append(inst)

        agent_name_width = max(len(a.agent) for a in agents)
        instance_name_width = max((len(i.identity.instance) for i in instances), default=0)

        # Tag column (Create rows) and mode column (Cont rows) are sized
        # INDEPENDENTLY — each scoped to its own population so a row's
        # agent / instance name sits tight against its modifiers. Tying the
        # two together (a shared max) pushed Create-row agent names way out
        # to align with the widest mode set, even though the columns don't
        # share a row.
        #
        # Per-member coloring: each tag/mode renders via `_modifier_display`,
        # which routes through `InstanceModifiers.colored_chain` and parses
        # the ANSI output back into pt-fragments via the `ANSI_TO_PT_STYLE`
        # mapping in structs. Adding a new color or shape tweaks structs, not
        # the picker.
        tag_by_agent = {a.agent: _modifier_display(a.tags) for a in agents}
        mode_by_inst = {i.identity.instance: _modifier_display(i.identity.modes) for i in instances}
        tag_col_width  = max((w for _, w in tag_by_agent.values()),  default=0)
        mode_col_width = max((w for _, w in mode_by_inst.values()), default=0)

        entries: list[PickerEntry] = []
        for agent in agents:
            tag_frags, tag_len = tag_by_agent[agent.agent]
            entries.append(PickerEntry(
                display=[
                    PickerRowMarker.NEW.fragment("  "),
                    *tag_frags,
                    ("", " " * (tag_col_width - tag_len)),
                    (STYLE_AGENT_NAME, f"{agent.agent:<{agent_name_width}}"),
                    ("", f" — {_agent_description(read_text(agent.md_path))}"),
                ],
                preview=_create_preview(agent),
                value=agent,
                deletable=False,
                modifiable=False,
            ))
            for inst in instances_by_agent.get(agent.agent, []):
                inst_id = inst.identity
                mode_frags, mode_len = mode_by_inst[inst_id.instance]
                cont_display = [
                    PickerRowMarker.CONT.fragment("      "),
                    *mode_frags,
                    ("", " " * (mode_col_width - mode_len)),
                    (STYLE_AGENT_NAME, f"{inst_id.instance:<{instance_name_width}}"),
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
                    value=inst_id,
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

        action, value = pick_with_preview(TITLE_AGENT_PICKER, entries, allow_delete=True, allow_modify=True, legend_text=LEGEND_TEXT)
        if action is None:
            return None

        if action == PickerAction.DELETE:  # picker enforces deletability — only cont rows (InstanceIdentity) reach here
            if confirm_dialog(CONFIRM_DELETE_FMT.format(name=value.instance)):
                delete_instance(value)
            continue

        if action == PickerAction.MODIFY:  # picker enforces modifiability — only cont rows reach here
            old_inst_id = value
            # Same prompt order as creation (resolve_target): workspace →
            # session → modes. The session prompt is the shared one — with
            # current= it accepts keeping the existing name.
            new_workspace = ask_for_workspace(old_inst_id.agent, default=old_inst_id.workspace)
            new_session = prompt_session(old_inst_id.agent, new_workspace, current=old_inst_id.session)
            new_modes = prompt_modes(old_inst_id.tags, old_inst_id.modes)
            new_inst_id = dataclasses.replace(
                old_inst_id, session=new_session, workspace=new_workspace, modes=tuple(new_modes)
            )  # is_brand_new stays False via the dataclass replace
            modify_instance(old_inst_id, new_inst_id)
            continue

        if value is _OPEN_DELMENU:
            _delete_submenu()
            continue

        return value  # AgentIdentity (new) | InstanceIdentity (cont)


def _delete_submenu() -> None:
    """Flat deletion submenu — every row red. Loops until Esc / Back."""
    while True:
        instances = continuable_instances()
        if not instances:
            return
        entries: list[PickerEntry] = []
        for inst in instances:
            inst_id = inst.identity
            entries.append(PickerEntry(
                display=[
                    PickerRowMarker.DLET.fragment("  "),
                    (STYLE_DEL_NAME, inst_id.instance),
                ],
                preview=inst.preview,
                value=inst_id,
            ))
        entries.append(PickerEntry(
            display=[PickerRowMarker.BACK.fragment(f"  {BACK_LABEL}")],
            preview=BACK_PREVIEW,
            value=None,
            deletable=False,
        ))

        action, value = pick_with_preview(TITLE_DELETE_MENU, entries, allow_delete=True, legend_text=LEGEND_TEXT)
        if action is None or value is None:
            return
        if confirm_dialog(CONFIRM_DELETE_FMT.format(name=value.instance)):
            delete_instance(value)


def print_launch_banner(inst_id: InstanceIdentity, cred_names: list[str]) -> None:
    """Print the multi-line summary that appears before docker compose builds the
    image — agent definition path, conf path, active tags + modes, and skills/creds
    counts when applicable. Each line is conditional on having something to show
    (no empty 'Tags: ' if there are none). The user-whitelist line counts
    user_firewall_whitelist_lines() inline — only when {auto} is in modes, so
    non-{auto} launches don't touch the file at all. Takes the launch's
    InstanceIdentity and pulls md_path / conf_path / tags / modes off it directly."""
    print(f"  Agent definition: {inst_id.md_path.relative_to(DOCKERIZED_CLAUDE_ROOT)}")
    print(f"  Configuration:    {inst_id.conf_path.relative_to(DOCKERIZED_CLAUDE_ROOT) if inst_id.conf_path else '(none — using defaults)'}")
    # Both tags and modes are typed enum members → `.label` directly. The
    # [..]/{..} wrapping comes from each member's `.label` property (single
    # source of truth in structs.InstanceModifiers).
    if inst_id.tags:
        print(f"  Tags:             {' '.join(t.label for t in inst_id.tags)}")
    if inst_id.modes:
        print(f"  Modes:            {' '.join(m.label for m in inst_id.modes)}")
    if cred_names:
        print(f"  Optional creds:   {', '.join(cred_names)} (from user_extras/optional_creds/)")
    if InstanceModifiers.MODE_WARN_AUTO in inst_id.modes:
        whitelist_count = len(user_firewall_whitelist_lines())
        display_path = "~/" + str(FIREWALL_WHITELIST_FILE.relative_to(home_dir()))
        print(f"  User whitelist:   {whitelist_count} domain{plural(whitelist_count)} (from {display_path})")
