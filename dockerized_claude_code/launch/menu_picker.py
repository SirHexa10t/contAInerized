"""Interactive agent UI: full-screen picker (prompt_toolkit) plus supporting
line-prompt helpers for workspace path and session suffix. Pulls picker-entry
builders and state lookups from agents_crud; has no agent-domain logic.
The tag-selection form (`prompt_tags` / `checkbox_form`) and the shared TUI
style system live in `tag_form.py` — this module drives it from the create
and F2-modify flows and reuses its styles.

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
from rich import box                                                       # dep — declared in pyproject.toml [project]
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from .agents_crud import (
    creatable_agents, delete_instance, instance_from_store,
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
from .tag_form import (
    RICH_BY_STYLE, STYLE_DICT, UiClass, _normalize, _plain, prompt_tags,
    tag_style,
)
from .tags import Agent, AgentBuild, Instance, Registry, Tag, resolve_build
from .tags.engine import engine_sort_key
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
# UiClass enum in tag_form (shared by the form and this picker).

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


class PickerAction(Enum):
    """Closed set of actions pick_with_preview returns alongside the selected
    entry's value. None (returned for cancel/escape) sits outside the enum so
    callers can branch on `if action is None` idiomatically."""
    SELECT = "select"     # Enter — user picked a row
    DELETE = "delete"     # Del   — user pressed delete on a row (only fires for deletable rows)
    MODIFY = "modify"     # F2    — user pressed modify on a row (only fires for modifiable rows)


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


def _render_md(text: str) -> str:
    """Render markdown text to an ANSI-encoded string for the picker's preview
    pane. Width is fixed to 80; prompt_toolkit re-wraps if the pane is
    narrower."""
    buf = io.StringIO()
    Console(
        file=buf, force_terminal=True, color_system="truecolor", width=80,
    ).print(Markdown(text))
    return buf.getvalue()


def _build_composition_legend(registry: Registry) -> str:
    """Build the F8 'composition legend' shown over the preview pane — one
    table per tag kind, header = the kind's nutshell, rows = each discovered
    member's underlined fullname + short description (the fullname spells
    out what the shortname abbreviates). Built with rich Table objects and
    rich styles — injecting raw ANSI into markdown-table source made rich
    count escape bytes as width and misalign the columns."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="truecolor", width=80)
    sections: list[tuple[str, str, str, Iterable[Tag]]] = [
        ("Engines",     "Engine",     "How hard the agent thinks — a model/effort budget.", registry.engines.values()),
        ("Professions", "Profession", "Tools it can use — each is a docker image layer.", registry.professions.values()),
        ("Specialties", "Specialty",  "Exceptional access or running conditions.", registry.specialties.values()),
        # Policies sort by shortname WITH its symbol (`!` < `+` < `-`), so
        # same-stance policies group: obligations, grants, denials.
        ("Policies",    "Policy",     "What it's permitted to do — orange grants, blue denies, white obligates.",
         sorted(registry.policies.values(), key=lambda t: t.shortname)),
    ]
    for title, singular, nutshell, members in sections:
        console.print(Markdown(f"# {title}\n\n{nutshell}"))
        console.print()
        table = Table(box=box.SIMPLE_HEAD, header_style="cyan", pad_edge=False)
        table.add_column(singular)
        table.add_column("Description")
        for t in members:
            table.add_row(
                Text(t.label, style=RICH_BY_STYLE[tag_style(t)]),
                Text.assemble((t.fullname, "underline"), f": {t.short_description}"),
            )
        console.print(table)
    return buf.getvalue()


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
        fragments.append((tag_style(tag), tag.label))
    if not fragments:
        return [], 0
    fragments.append(("", " "))   # trailing separator — bakes into the column width
    visible = sum(len(text) for _, text in fragments)
    return fragments, visible


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
            return [(UiClass.NO_MATCH.css, EMPTY_FILTER_MESSAGE)]
        out = []
        for i in state["shown"]:
            segments = _normalize(entries[i].display)
            if i == state["cursor"]:
                segments = [(f"{UiClass.CURSOR.css} {style}".strip(), text)
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
        return [(UiClass.TITLE.css, title)]

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
        out = [(UiClass.STATUS.css, hint), ("", "\n")]
        if state["filter"]:
            out.append((UiClass.FILTER.css, FILTER_LABEL))
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
            return UiClass.DIVIDER.css
        value = entries[state["cursor"]].value
        if isinstance(value, Instance):             # cont row
            return PickerRowMarker.CONT.style       # fg:ansiyellow
        if isinstance(value, Agent):                # new row
            return PickerRowMarker.NEW.style        # fg:ansigreen
        return UiClass.DIVIDER.css

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
            Window(width=DIVIDER_WIDTH, char=DIVIDER_CHAR, style=UiClass.DIVIDER.css),
            Window(width=1, char="▌", style=accent_style),   # preview-side accent bar; colour reflects selected row's kind
            Window(
                FormattedTextControl(preview_text),
                wrap_lines=True,
                width=D(weight=PREVIEW_WEIGHT),
                style=UiClass.PREVIEW.css,
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
            new_build = prompt_tags(registry, old_inst.build,
                                    instance=f"{old_inst.agent}{SESSION_SEP}{new_session}",
                                    workspace=new_workspace)
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
