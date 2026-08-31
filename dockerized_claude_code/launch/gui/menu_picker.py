"""The full-screen picker (launch/gui): main menu, deletion submenu, the
"Edit Preferences" opener, plus supporting line-prompt helpers for workspace
path and session suffix. Pulls picker-entry builders and state lookups from
agents_crud; has no agent-domain logic. Every *form* (tag form, toolkit
form, checkbox_form) and the shared TUI style system live in the sibling
`tag_form.py` — this module only opens them and reuses their styles.

Public API:

  select_agent(registry)
      Run the agent/session picker (main menu + nested deletion submenu +
      "Edit Preferences" submenu) until the user picks something or cancels.
      Discovers agents/instances and handles deletions + toolkit-profile
      edits internally.
      -> Agent (new) | Instance (cont) | None on cancel/empty

  ask_for_workspace(agent, default=None)
      Line prompt for a workspace path; tab-completes against the host filesystem.
      -> absolute path string

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

(The pre-launch banner lives in claude_code_config.print_launch_banner —
launch-stage output, not picker UI.)

Generic-picker entry shape (pick_with_preview):
    {
        'display':    str | list[(style, text)] | FormattedText,
        'preview':    str,
        'value':      any,    # opaque; returned to the caller on selection
        'deletable':  bool,   # optional; defaults True. When False, Del is a no-op on this row.
        'modifiable': bool,   # optional; defaults True. When False, F2 is a no-op on this row.
        'selectable': bool,   # optional; defaults True. When False the row is information-only:
                              # rendered, but the cursor skips it, so Enter/Del/F2 can't target it.
    }
"""

import atexit
import dataclasses
import io
import multiprocessing
import readline
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from functools import cached_property
from enum import Enum
from pathlib import Path
from typing import Any, cast

from prompt_toolkit import Application                                     # dep — declared in pyproject.toml [project]
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.styles import Style
from rich import box                                                       # dep — declared in pyproject.toml [project]
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from ..agents_crud import (
    creatable_agents, delete_instance, instance_from_store, invalid_tags_report,
    list_all_instances, modify_instance,
)
from ..cluster import state as cluster_state
from ..cluster.legoset import (
    ClusterTemplate, assemble, discover_templates, load_legoset, reassemble,
    validate,
)
from ..cluster.member import ClusterError, valid_label
from ..docker_config import (
    cluster_container_id, docker_running_instances_subprocess,
)
from ..file_access import (
    expand_user_path, is_dir, last_prompt_in_state, path_exists, read_text,
    resolved_cwd, resolved_path, tab_complete_paths,
)
from ..paths import (
    AGENTS_COMMANDS_DIR, AGENTS_DIR, DEFAULT_WORKSPACE, DEFAULTING_DIRS,
    instance_state_dir_path,
)
from .cluster_form import TextField, prefill_picks, prompt_members
from .tag_form import (
    RICH_BY_STYLE, STYLE_DICT, STYLE_TAG_INVALID, FormOption, UiClass,
    _fragment_source, _normalize, _plain,
    checkbox_form, edit_profiles_menu, prompt_tags, squashed_tag_style,
    tag_style,
)
from ..tags import Agent, AgentBuild, Instance, Registry, Tag, resolve_build
from ..tags.base import SQUASH_AT, first_glyph
from ..tags.engine import engine_sort_key, sorted_engines
from ..tags.identity import SESSION_SEP
from ..utils import ordering_index_or_end, relative_time, reset_terminal


# ============================================================
# UI strings
# ============================================================

HINT_BASE_TEXT       = "↑↓ navigate  •  type to filter  •  Enter select  •  Esc cancel"
HINT_DELETE_SUFFIX   = "  •  Del delete"
HINT_MODIFY_SUFFIX   = "  •  F2 modify"
WHEEL_LINES          = 3     # rows the wheel moves per notch, on either side
# Shown above a long preview so its scroll position is legible. Only for genuinely
# long content — on a preview that fits, a position marker is noise.
PREVIEW_POSITION       = "\x1b[2m   line {} / {}\x1b[0m"
PREVIEW_POSITION_FLOOR = 40
HINT_LEGEND_SUFFIX   = "  •  F8 legend"
HINT_LEGEND_OPEN     = "F8 / Esc close legend"
FILTER_LABEL         = "filter: "
EMPTY_FILTER_MESSAGE = "(no matches)"
PREVIEW_LOADING_TEXT = "… loading preview (keep browsing)"
LAST_PROMPT_LOADING  = "[loading…]"   # stands in for the Last prompt value while transcripts are read
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

PREFERENCES_LABEL  = "(Edit Preferences)"
DELMENU_LABEL  = "(Move onto deletions menu)"
BACK_LABEL     = "(Move back to Agent Selection)"
PREFERENCES_PREVIEW = ("One merged form, one section per profile file. Toolkits: which language "
                    "toolchains a configurable profession's shared image installs (today: [code]'s "
                    "Rust / Node / CMake) — edits ~/.claude-agents/<profession>_profile.toml; a "
                    "changed toggle rebuilds only that tool's Docker layer on the next launch. "
                    "UI configs: launcher preferences — the {mux} backend, herdr vs tmux — edits "
                    "~/.claude-agents/ui_profile.toml, read at every launch. Service CLIs "
                    "(gh, gcloud, aws, ...) are not chosen here — they install when matching creds "
                    "exist under user_extras/optional_creds/.")
DELMENU_PREVIEW = "Open the deletion sub-menu to remove agent instances and their state directories."
BACK_PREVIEW    = "Return to the main agent picker."
CONFIRM_DELETE_FMT = "Delete '{name}'?"

# ============================================================
# Agent-picker styles (inline, applied per-segment)
# ============================================================

STYLE_AGENT_NAME     = "bold fg:ansibrightblue"
# The bookmark shapes the picker's two CONTRASTED row kinds lead with (see
# PickerRowMarker): an agent row is a green tab with a fading end, its
# instances nest beneath behind a dim grey ▸. Green by request (iterated from
# an all-grey first pass) — it is also Create's KIND colour, the same green
# the preview's accent bar shows, so the tab and the bar agree.
STYLE_TAB            = "bg:ansigreen fg:black bold"         # the tab body behind "Create" — black text on the green art, per request
STYLE_TAB_TIP        = "fg:ansigreen"                       # its fading end: foreground == tab background, so the shade ramp reads as the tab dissolving, not as characters
STYLE_NEST_MARK      = "fg:ansibrightblack"                 # instance rows: dim marker, indented under the agent's tab
# The cluster-template rows wear the same tab shape in CYAN — a third kind
# beside create-green and continue-yellow, same colour its preview accent shows.
# Existing clusters nest beneath in the CONT shape (also cyan), their members a
# level deeper still — shape says create/continue, colour says cluster.
STYLE_CLUSTER_TAB     = "bg:ansicyan fg:black bold"
STYLE_CLUSTER_TAB_TIP = "fg:ansicyan"
STYLE_CLUSTER_NEST    = "fg:ansicyan"
STYLE_MEMBER_COUNT    = "fg:ansigreen"    # the "(N members)" column on a template row
# The tab's end, as a shade ramp (▓▒░ — Block Elements, U+2580–259F). NOT a
# triangle: ▶ is a Geometric Shape, i.e. a TYPOGRAPHIC character the font
# renders at text size, so it sat visibly shorter than the row (reported from
# a live launch). Block elements are the one shape family terminal emulators
# rasterize THEMSELVES — kitty's box_drawing module, and the same procedural
# drawing in VTE/gnome-terminal, WezTerm, and alacritty — full-cell and
# seam-free with the font never consulted. That is the transplantable half of
# "kitty stops trusting fonts": an application cannot draw pixels, but it can
# emit only the code points the emulator draws procedurally. The ramp is the
# classic powerline "fade" separator, built from universal characters.
TAB_TIP = "▓▒░"
# The set the tip must stay inside — tested, so a prettier font glyph cannot
# sneak back in and reintroduce the short-triangle rendering.
BLOCK_ELEMENTS = range(0x2580, 0x25A0)
STYLE_DEL_NAME       = "bold fg:ansired"
STYLE_WORKSPACE_HINT = "italic fg:ansibrightblack"

# A running instance's row is information-only (see PickerEntry.selectable):
# the name greys out to read as unavailable, and the red tag is what draws the
# eye. Emitted conditionally like the PickerCwdHint labels — no reserved
# column, so non-running rows keep their tighter spacing.
STYLE_RUNNING_NAME   = "fg:ansibrightblack"                      # grey — this instance can't be launched right now
RUNNING_HINT         = ("bold fg:ansibrightred", "(RUNNING) ")   # (style, label) fragment, same shape as PickerCwdHint.fragment
TAG_EMPHASIS         = "bold underline"   # style SUFFIX for tags an emphasize set names (see _tags_column) — on top of the tag's own color, so the color language survives the shout


class PickerAction(Enum):
    """Closed set of actions pick_with_preview returns alongside the selected
    entry's value. None (returned for cancel/escape) sits outside the enum so
    callers can branch on `if action is None` idiomatically."""
    SELECT = "select"     # Enter — user picked a row
    DELETE = "delete"     # Del   — user pressed delete on a row (only fires for deletable rows)
    MODIFY = "modify"     # F2    — user pressed modify on a row (only fires for modifiable rows)


class PickerRowMarker(Enum):
    """Row lead-in — the fragments that prefix a row, bundled with the accent
    colour the preview's edge bar shows while that row is selected. Bundled so
    'kind of row' is one named thing instead of parallel constants assembled
    at each call site.

    The two row kinds the picker CONTRASTS — agents (Create) and their
    instances (Cont) — wear bookmark shapes instead of emoji: the agent row
    leads with a grey tab dissolving through a shade ramp (the Starship
    segment look, fade variant), and its instances nest beneath it behind a
    dim, indented ▸. The shape-work is confined to characters terminals draw
    PROCEDURALLY (see TAB_TIP) — two font lessons paid for this: the
    private-use powerline wedges () are tofu on stock fonts, and even the
    universally-COVERED triangle ▶ renders at typographic size, visibly
    shorter than the row (both observed live). Cell backgrounds and block
    elements are the only full-height primitives an application can rely on.
    ▸ on the Cont rows is deliberately exempt: it is a bullet next to text,
    not furniture that must span the row.
    The tab wears Create's KIND colour (green, matching the preview's accent
    bar) with black text; the nest marker stays dim grey — so colour, shape,
    and depth all separate the two kinds the same way.

    Members expose:
      .lead        — tuple of (style, text) fragments that start the row
      .accent      — preview accent-bar style while the row is selected
      .fragments() — the lead plus an optional alignment suffix, ready to
                     splat into a FormattedText list
    """
    # Both creation tabs lead with `+` — the creation intent in one glyph, and
    # the two tab NAMES then say what gets created (an agent instance; a
    # cluster) instead of one saying the verb and the other the noun.
    NEW     = (((STYLE_TAB, " + Agent "), (STYLE_TAB_TIP, TAB_TIP)), "fg:ansigreen")
    CONT    = (((STYLE_NEST_MARK, "   ▸ Cont."),),              "fg:ansiyellow")
    CLUSTER = (((STYLE_CLUSTER_TAB, " + Cluster "), (STYLE_CLUSTER_TAB_TIP, TAB_TIP)),
               "fg:ansicyan")
    # "Cont.", the same word instance rows use — an existing cluster IS a
    # continuation; the cyan is what says "cluster" (kind = colour, verb = word).
    CLSTR   = (((STYLE_CLUSTER_NEST, "   ▸ Cont."),),           "fg:ansicyan")
    MEMBER  = (((STYLE_NEST_MARK, "        · "),),              "fg:ansicyan")
    TOOLS  = ((("fg:ansicyan", "🧰 Toolkits"),),               "")
    DELMNU = ((("fg:ansired", "⚠️ DELETE‼️"),),                "")
    DLET   = ((("fg:ansired", "🗑 DELETE"),),                  "")
    BACK   = ((("", "🚪  Back"),),                             "")

    def __init__(self, lead: tuple[tuple[str, str], ...], accent: str) -> None:
        self.lead = lead
        self.accent = accent

    def fragments(self, suffix: str = "") -> list[tuple[str, str]]:
        """The lead fragments plus `suffix` (alignment spacing, or trailing
        text like the back-row's label) as its OWN default-styled fragment —
        never glued onto the last lead fragment, whose style may carry a
        background that would smear across the gap."""
        return [*self.lead, ("", suffix)] if suffix else list(self.lead)

    def width(self, suffix: str = "") -> int:
        """The lead's width in cells (every lead character is single-width —
        ASCII plus Block Elements). What cross-marker column alignment
        computes from: the cluster tab is wider than the agent tab, so landing
        both rows' NAMES in one column means measuring, not guessing."""
        return sum(len(text) for _, text in self.lead) + len(suffix)


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
    invalid implies ws_resolved is None, which makes the other two False).
    `is_running` means a container for this instance is up right now, so the
    row renders greyed with the RUNNING tag and is information-only — docker
    would refuse a second container on the same `--name` anyway."""
    identity: Instance
    workspace_display: str
    is_current_dir: bool
    is_default_dir: bool
    is_invalid_dir: bool
    last_used_display: str
    is_running: bool = False

    @cached_property
    def preview(self) -> str:
        """The full Cont-row preview — `_compose` with the real `Last prompt`
        value, which is the expensive part: benchmarked at 99.9% of the build
        on a 155 MB state dir (3.9 s of a 3.9 s total; metadata + tags are
        ~5 ms — see benchmark/bench_preview_segments.py). The picker therefore
        never computes THIS on the UI thread: it shows `preview_quick` and
        resolves this form on the loader's worker.

        A cached_property, so the read happens once per screen-session however
        often the row is re-rendered. (cached_property assigns via `__dict__`,
        which a frozen dataclass permits — only `__setattr__` is blocked.)"""
        return self._compose(_last_prompt_display(self.identity.state_dir))

    @cached_property
    def preview_quick(self) -> str:
        """The instant form: identical to `preview` except the `Last prompt`
        value reads `[loading…]` — the ~5 ms of metadata + tags the UI thread
        CAN afford, shown while the worker reads the transcripts. The stand-in
        appears only when the instance has any session history at all
        (a cheap glob+stat), so a fresh instance never flashes a loading line
        for a field it will not get. The rare inverse — history whose turns
        are all tool echoes, so no prompt ever resolves — shows the stand-in
        once, then loses the field when the full form lands: honest, brief."""
        return self._compose(LAST_PROMPT_LOADING
                             if self.identity.has_continuable_history else None)

    def _compose(self, prompt: str | None) -> str:
        """The preview markdown rendered to ANSI: italic lead-in, horizontal
        rule, a YAML-fenced metadata block (rich syntax-colors keys/values) —
        with a `Last prompt` field iff `prompt` is given — then the full tag
        list, every active tag expanded to its colored label, full name, and
        one-line description. The preview is where the tags can be READ: the
        row's column shows one-char chips once it holds SQUASH_AT of them, so
        the list is the lookup, not a bonus."""
        inst = self.identity
        return _render_md(
            f"*Continue session `{inst.instance}`.*\n\n"
            f"---\n\n"
            f"```yaml\n"
            f"Agent:     {inst.agent}\n"
            f"Session:   {inst.session}\n"
            f"Workspace: {self.workspace_display}\n"
            f"Engine:    {inst.engine.name if inst.engine else '(default)'}\n"
            f"State:     {inst.state_dir}\n"
            f"Last used: {self.last_used_display}\n"
            + (f"\nLast prompt:\n  {prompt}\n" if prompt else "")
            + "```\n"
        ) + _tags_preview(inst)


@dataclass(frozen=True)
class PickerEntry:
    """One row in `pick_with_preview`. `display` is the prompt_toolkit
    FormattedText fragment list (list of (style, text) tuples), `preview` is
    the right-pane ANSI text — either the string itself, or a zero-arg callable
    producing it, resolved when the row is first highlighted (Cont rows pass a
    callable: their preview reads the instance's transcripts for the `Last
    prompt` line, and paying that per instance at menu OPEN would make startup
    scale with everyone's conversation history). `value` is what the picker
    hands back on selection (Agent for Create rows, Instance
    for Cont/Delete rows, `_OPEN_DELMENU` for the delete-menu opener, `None`
    for Back rows). `deletable` / `modifiable` default True; the producer
    sets them False to disable Del / F2 on the row (Create / Back / opener).
    `selectable=False` makes the row INFORMATION-ONLY: still rendered, but the
    cursor never lands on it, so Enter / Del / F2 can't target it (a running
    instance — see RUNNING_HINT). `display` defaults to a fresh empty list per
    instance to keep the dataclass safe — never shared across rows."""
    display: list[tuple[str, str]] = field(default_factory=list)
    preview: str | Callable[[], str] = ""
    preview_quick: str | Callable[[], str] | None = None   # cheap stand-in pane shown while `preview` resolves
    value: Any = None
    deletable: bool = True
    modifiable: bool = True
    selectable: bool = True

    @property
    def preview_ready(self) -> bool:
        """True once `preview` holds the rendered string — the signal the
        picker's render path uses to decide between showing it and showing a
        loading placeholder while a worker resolves it. A plain-string preview
        (Create / Back rows) is born ready."""
        return not callable(self.preview)

    def preview_ansi(self) -> str:
        """The rendered preview, resolving a deferred one.

        Resolution REPLACES `preview` with the produced string (via
        `object.__setattr__` — the one mutation this frozen dataclass permits
        itself), and that is not an optimisation: it is what makes
        `preview_ready` answer without computing anything, which the render
        path depends on to stay non-blocking. The heavy work is cached on
        ContEntry's side too (`cached_property`), so re-resolving after a
        rebuild costs a dict lookup."""
        if callable(self.preview):
            object.__setattr__(self, "preview", self.preview())
        return cast(str, self.preview)

    def quick_ansi(self) -> str:
        """The stand-in pane, resolved the same way. Cheap by contract —
        everything in it except the transcript-fed field, ~5 ms — so the UI
        thread calls this directly while the loader's worker builds the real
        one. Callers check `preview_quick is not None` first."""
        if callable(self.preview_quick):
            object.__setattr__(self, "preview_quick", self.preview_quick())
        return cast(str, self.preview_quick)


# Sentinel entry values signalling "open the delete submenu" / "open the
# toolkits editor" — used in the main picker where most rows hold an
# identity dataclass; these are the non-identity rows, so distinct
# singletons let the dispatcher match by `is` rather than tagging
# identities with extra metadata.
_OPEN_DELMENU = object()
_OPEN_PREFERENCES = object()


@dataclasses.dataclass(frozen=True)
class _ClusterTemplateRow:
    """A cluster-template row's value: which `.legoset` to open the membership
    form on. A dataclass rather than a singleton because there is one row PER
    template — the dispatcher matches by type, then reads the path."""
    name: str
    path: Path


@dataclasses.dataclass(frozen=True)
class _ClusterRow:
    """An EXISTING cluster's row value. Carries only the session name: every
    handler reloads the cluster from disk, so a row built before some other
    handler mutated the cluster cannot act on a stale member list."""
    session: str


@dataclasses.dataclass(frozen=True)
class _MemberRow:
    """One member's row value — the unit the picker EDITS (F2 re-tags, Del
    removes from the cluster). Session + member id; same reload-on-act rule
    as _ClusterRow."""
    session: str
    member_id: str


def continuable_instances(registry: Registry,
                          running: frozenset[str] | None = None) -> list[ContEntry]:
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
    when the picker is the wrong place to crash on a typo.

    Also flags which instances are running right now (`is_running`). `running`
    is the docker_running_instances_subprocess snapshot to flag from — pass it
    when the caller already probed (select_agent shares ONE `docker ps` per
    menu build between these rows and the cluster rows); None probes here, so
    the marks still refresh per menu rebuild (including on return from the
    delete / toolkits submenus) without costing a subprocess per keystroke. An
    undeterminable docker state marks nothing, deliberately: over-flagging
    would wrongly lock rows the user can actually launch."""
    # Symlinks normalized via .resolve() so e.g. /home/<user> matches /var/users/<user>
    # when one symlinks to the other. Subdirs deliberately don't count — being in a
    # project under $HOME doesn't make /ai_workspace your "default" workspace.
    cwd = resolved_cwd()
    defaulting_dir_active = cwd in {resolved_path(d) for d in DEFAULTING_DIRS}
    default_workspace_resolved = resolved_path(DEFAULT_WORKSPACE)
    if running is None:
        running = docker_running_instances_subprocess() or frozenset()   # None (can't tell) → flag nothing

    out = []
    for dir_name in list_all_instances():
        inst = instance_from_store(dir_name, registry)
        if inst is None:
            continue
        ws = inst.workspace
        ws_resolved = resolved_path(ws) if ws and is_dir(ws) else None
        last_mtime = inst.last_used_mtime
        out.append(ContEntry(
            identity=inst,
            workspace_display=ws if ws else NO_WORKSPACE_DISPLAY,                                    # show stored value even when invalid; `?` sentinel only when no entry at all
            is_current_dir=ws_resolved == cwd,
            is_default_dir=defaulting_dir_active and ws_resolved == default_workspace_resolved,      # cwd ∈ DEFAULTING_DIRS and ws matches DEFAULT_WORKSPACE — tagged `(DEFAULT DIR)`
            is_invalid_dir=bool(ws) and ws_resolved is None,                                         # ws set but path doesn't exist / isn't a directory — tagged `(INVALID DIR)`
            last_used_display=relative_time(last_mtime) if last_mtime is not None else "(never)",
            is_running=dir_name in running,
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


LAST_PROMPT_PREVIEW_CHARS = 250   # enough to recognise a conversation; not a transcript viewer

# The child process that parses transcripts — created on first use, kept for
# the launcher's lifetime (one warm child serves every preview of every menu),
# torn down at exit. See _read_last_prompt for why it exists at all.
_PROMPT_POOL: ProcessPoolExecutor | None = None


def _read_last_prompt(state_dir: Path) -> tuple[str, float] | None:
    """`last_prompt_in_state`, run in a CHILD PROCESS — because a thread is not
    background enough. Parsing a large transcript is CPU-bound (~500k
    `json.loads` calls, plus one `.splitlines()` holding the GIL for the whole
    68 MB string), and a CPU-bound thread convoys the GIL: measured on a real
    155 MB state dir, the render thread's 5 ms tick stalled up to 803 ms while
    a worker THREAD read it, versus 3.5 ms worst-case with the read in a child
    process (launch/benchmark/bench_preview_gil.py reproduces this). The
    worker thread that calls this blocks in `.result()`, which waits GIL-FREE.

    Spawn, not fork: the parent runs prompt_toolkit with live threads by the
    time this fires, and forking a threaded process copies lock state mid-use.
    The child imports only `launch.file_access` (the pickled target), so the
    one-time warmup is ~0.2 s — paid on the worker, never on the UI thread.

    Falls back to the in-process read when the pool cannot serve (a sandbox
    forbidding subprocesses, a killed child): the GIL stutter returns, but the
    preview still resolves — degraded beats broken. The fallback also makes
    this the test seam: a patched-in fake is unpicklable, so tests exercising
    the display logic stay in-process without special-casing."""
    global _PROMPT_POOL
    try:
        if _PROMPT_POOL is None:
            _PROMPT_POOL = ProcessPoolExecutor(
                max_workers=1, mp_context=multiprocessing.get_context("spawn"))
            atexit.register(_PROMPT_POOL.shutdown, wait=False, cancel_futures=True)
        return _PROMPT_POOL.submit(last_prompt_in_state, state_dir).result()
    except Exception:                          # noqa: BLE001 — any pool failure degrades, none may break the picker
        return last_prompt_in_state(state_dir)


def _last_prompt_display(state_dir: Path) -> str | None:
    """The last human prompt this instance received, condensed for the preview
    pane — or None when it has none yet (the field then drops out entirely,
    rather than showing an empty label).

    Condensed two ways, each for a reason: whitespace runs (including
    newlines) collapse to single spaces, because the value sits inside the
    preview's YAML fence and a raw line starting ``` would close the fence
    around the rest of the metadata; and anything past
    LAST_PROMPT_PREVIEW_CHARS is cut at an ellipsis, because the field exists
    to recognise the conversation, not to reread it."""
    found = _read_last_prompt(state_dir)
    if found is None:
        return None
    condensed = " ".join(found[0].split())
    if len(condensed) > LAST_PROMPT_PREVIEW_CHARS:
        condensed = condensed[:LAST_PROMPT_PREVIEW_CHARS] + "…"
    return condensed or None


class _PreviewLoader:
    """Resolves slow previews OFF the UI thread, so highlighting a heavy row
    never stalls the picker.

    The render path asks `text()` for the highlighted row. A ready preview
    comes back at once; an unresolved one comes back as PREVIEW_LOADING_TEXT
    while the resolution runs on the one worker thread, which pokes the
    application's thread-safe `invalidate()` when it finishes — prompt_toolkit
    then re-renders and the ready branch serves the real thing. Meanwhile
    every keystroke works: the UI thread never touches a transcript.

    One worker, deliberately: previews resolve in highlight order and each at
    most once (`_submitted`), so holding an arrow key across heavy rows queues
    quick sequential reads instead of forking a thread per row. The worker
    itself stays GIL-quiet: the expensive segment runs in a child process
    (`_read_last_prompt` — a CPU-bound thread would convoy the render loop),
    so this thread mostly WAITS. A resolution that RAISES becomes a
    visible `(preview failed …)` pane rather than an eternal placeholder,
    because `_submitted` rightly blocks a retry and a silent swallow would
    look identical to loading forever."""

    def __init__(self, invalidate: Callable[[], None]) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1,
                                            thread_name_prefix="preview")
        self._invalidate = invalidate
        self._submitted: set[int] = set()

    def text(self, index: int, entry: PickerEntry) -> str:
        """The pane content for `entry` right now: the preview, or — with
        resolution scheduled — the richest stand-in available: the entry's
        quick form (everything but the transcript-fed field, benchmarked at
        ~5 ms against seconds for the full read) when it has one, else the
        bare PREVIEW_LOADING_TEXT line."""
        if entry.preview_ready:
            return entry.preview_ansi()
        if index not in self._submitted:
            self._submitted.add(index)
            self._executor.submit(self._resolve, entry)
        if entry.preview_quick is not None:
            return entry.quick_ansi()
        return PREVIEW_LOADING_TEXT

    def _resolve(self, entry: PickerEntry) -> None:
        try:
            entry.preview_ansi()
        except Exception as error:                      # noqa: BLE001 — see class docstring
            object.__setattr__(entry, "preview", f"(preview failed: {error})")
        self._invalidate()

    def shutdown(self) -> None:
        """Stop resolving. Queued rows are dropped (the picker is closing —
        nobody will read them); a resolution already running finishes, since
        its result lands in ContEntry's cache and greets the next menu."""
        self._executor.shutdown(wait=False, cancel_futures=True)


def _deferred_preview(entry: "ContEntry", *, quick: bool = False) -> Callable[[], str]:
    """A zero-arg producer of `entry`'s preview (or its quick form), for
    PickerEntry's deferred slots. A named closure rather than an inline lambda
    at the call sites: the loop variable must be bound NOW (a bare lambda
    would render whichever row the loop finished on), and the
    binding-by-default-arg idiom is exactly the kind of trap this spells out
    instead."""
    return (lambda: entry.preview_quick) if quick else (lambda: entry.preview)


def _tags_preview(inst: Instance) -> str:
    """The Cont preview's expanded tag list, ANSI-rendered: one line per
    active tag — colored label, underlined full name, one-line description —
    plus an alert line per invalid tag with what to do about it.

    Built with rich Text (like the F8 legend, same RICH_BY_STYLE colors)
    rather than inside the markdown, because per-tag coloring is the point:
    the legend taught these colors, the row may be showing them as bare chips,
    and this list is what maps a chip back to a name."""
    labels = [t.label for t in inst.active_tags] + [p.label for p in inst.invalid_tags]
    pad = max((len(label) for label in labels), default=0)
    lines = Text("Tags:\n", style="bold")
    # Padding sits OUTSIDE each styled span: the invalid style paints a red
    # background, and a red bar of trailing spaces would read as more alert.
    for tag in inst.active_tags:
        lines.append("  ")
        lines.append(tag.label, style=RICH_BY_STYLE[tag_style(tag)])
        lines.append(" " * (pad - len(tag.label)) + "  ")
        lines.append(tag.fullname or tag.name, style="underline")
        lines.append(f" — {tag.short_description}\n")
    if not inst.active_tags:
        lines.append("  (none)\n")
    for problem in inst.invalid_tags:
        lines.append("  ")
        lines.append(problem.label, style="black on red")
        lines.append(" " * (pad - len(problem.label)) + "  ")
        lines.append(f"{problem.reason.replace('_', ' ')} {problem.kind} — "
                     f"fix it via F2 before this instance can start\n")
    buf = io.StringIO()
    Console(file=buf, force_terminal=True, color_system="truecolor", width=80,
            ).print(lines, end="")
    return buf.getvalue()


def _tag_commands(registry: Registry) -> list[tuple[Tag, str, str]]:
    """`(tag, /command, description)` for every slash command a TAG grants.

    Read from the declarations rather than any directory: a tag names its
    commands in tag.info (`commands = [...]`) and the files live centrally in
    `agents/_commands/` — one findable place, and one file grantable by several
    tags. The registry has already validated that every declared name resolves
    to a real file, so the read here cannot miss. Any KIND may declare —
    `{manager}` and `[self]` both do — which is why this iterates all four.

    The description comes from the command file's own `description:` frontmatter —
    the same line Claude Code shows in its `/help` — so the legend cannot drift
    from what the command says about itself. A file without one is listed anyway
    with an empty description; silently hiding it would be worse than a blank cell.
    """
    return [(tag, f"/{name}",
             _frontmatter_description(AGENTS_COMMANDS_DIR / f"{name}.md"))
            for tag in registry.get_all() for name in tag.commands]


def _frontmatter_description(md: Path) -> str:
    """The `description:` line from a command file's YAML frontmatter, or "".

    Deliberately a line scan rather than a YAML parse: the frontmatter here is
    two or three flat keys, and a dependency (or a hand-rolled parser) for one
    field would be more to maintain than to read."""
    for line in read_text(md).splitlines()[:12]:
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


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
        ("Engines",     "Engine",     "How hard the agent thinks — a model/effort budget (most advanced first).", sorted_engines(registry.engines.values())),
        ("Professions", "Profession", "Tools it can use — each is a docker image layer.", registry.professions.values()),
        ("Specialties", "Specialty",  "Exceptional access or running conditions.", registry.specialties.values()),
        # Policies sort by shortname WITH its symbol (`!` < `+` < `-`), so
        # same-stance policies group: demands, grants, denials.
        ("Policies",    "Policy",     "What it's permitted to do — orange grants, blue denies, white demands.",
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

    # Commands a TAG grants, if any. Omitted entirely when none do, rather than
    # printing an empty table that implies the feature is broken. "Tag", not
    # "Specialty": any kind may declare (`[self]` is a profession).
    if commands := _tag_commands(registry):
        console.print(Markdown(
            "# Tag Commands\n\n"
            "Slash commands that arrive WITH a tag — present only in instances "
            "carrying a granting tag, unlike the shared commands every agent gets."))
        console.print()
        table = Table(box=box.SIMPLE_HEAD, header_style="cyan", pad_edge=False)
        table.add_column("Tag")
        table.add_column("Command")
        table.add_column("Description")
        for tag, command, description in commands:
            table.add_row(Text(tag.label, style=RICH_BY_STYLE[tag_style(tag)]),
                          Text(command, style="bold"),
                          Text(description))
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


def _tags_column(tags: Iterable[Tag],
                 emphasize: frozenset[str] = frozenset(),
                 ) -> tuple[list[tuple[str, str]], int]:
    """Render a tag set for cont-row / Create-row display as prompt_toolkit
    `(style, text)` fragments. Returns (fragments, visible width); empty input
    → ([], 0). A trailing space fragment is appended to non-empty output so
    the widest row in the column gets a built-in separator before its right
    neighbor (the agent / instance name). Tags named in `emphasize` get
    TAG_EMPHASIS on top of their usual color, both forms — the stop selector
    uses it to make `{muxer}` jump out.

    Two forms, chosen by how crowded the row is. Below SQUASH_AT tags: each
    tag's kind-punctuated label in its warn-aware color. At SQUASH_AT or more,
    the labels stop fitting anything, so each tag collapses to its
    one-character `squash_glyph` on a chip of its usual color
    (`squashed_tag_style`) — the full names move to the row's preview pane,
    which lists every tag expanded. Both forms space-separate, so two adjacent
    chips of the same color read as two tags rather than one block."""
    tag_list = list(tags)
    if not tag_list:
        return [], 0
    squash = len(tag_list) >= SQUASH_AT
    fragments: list[tuple[str, str]] = []
    for tag in tag_list:
        if fragments:
            fragments.append(("", " "))
        style, text = ((squashed_tag_style(tag_style(tag)), tag.squash_glyph)
                       if squash else (tag_style(tag), tag.label))
        if tag.name in emphasize:
            style = f"{style} {TAG_EMPHASIS}"
        fragments.append((style, text))
    fragments.append(("", " "))   # trailing separator — bakes into the column width
    visible = sum(len(text) for _, text in fragments)
    return fragments, visible


def _cont_tags_column(inst: Instance,
                      emphasize: frozenset[str] = frozenset(),
                      ) -> tuple[list[tuple[str, str]], int]:
    """A Cont row's tag column: the resolved tags (via `_tags_column`)
    followed by any `invalid_tags` — stored names that no longer resolve —
    in the red-background/black-foreground alert style so a stale/typo'd tag
    is impossible to miss. The invalid tags also block the instance from
    starting (see select_agent / resolve_target).

    The SQUASH_AT threshold counts BOTH parts: six tags where one is invalid
    are exactly as crowded as six valid ones, and mixing one squashed part
    with one labelled part would make the alert look like a different feature
    rather than one of the row's tags. A squashed invalid tag stays visible
    for the same reason the labelled form does — its chip is the alert red no
    valid tag uses."""
    problems = inst.invalid_tags
    if len(inst.active_tags) + len(problems) >= SQUASH_AT:
        chips = [(squashed_tag_style(tag_style(tag))
                  + (f" {TAG_EMPHASIS}" if tag.name in emphasize else ""),
                  tag.squash_glyph)
                 for tag in inst.active_tags]
        chips += [(STYLE_TAG_INVALID, first_glyph(problem.name))
                  for problem in problems]
        fragments: list[tuple[str, str]] = []
        for chip in chips:
            if fragments:
                fragments.append(("", " "))
            fragments.append(chip)
        fragments.append(("", " "))
        return fragments, sum(len(text) for _, text in fragments)
    fragments, width = _tags_column(inst.active_tags, emphasize)
    for problem in problems:
        if fragments:
            fragments.append(("", " "))
            width += 1
        fragments.append((STYLE_TAG_INVALID, problem.label))
        width += len(problem.label)
    if problems:
        fragments.append(("", " "))   # trailing separator, matching _tags_column
        width += 1
    return fragments, width


def _focusable_indices(entries: list[PickerEntry], shown: list[int]) -> list[int]:
    """Row indices the cursor may land on: visible after filtering AND
    selectable. Information-only rows (a running instance) are rendered but
    never focusable, which is what blocks Enter / Del / F2 on them."""
    return [i for i in shown if entries[i].selectable]


def _cursor_step(entries: list[PickerEntry], shown: list[int], cursor: int, delta: int) -> int:
    """Where the cursor lands after moving `delta` rows — stepping over
    information-only rows and wrapping at the ends. `cursor` unchanged when
    nothing is focusable; snaps to the first focusable row when the cursor
    isn't on one. Module-level and pure so the skip behaviour is unit-testable
    without driving a live prompt_toolkit Application."""
    landable = _focusable_indices(entries, shown)
    if not landable:
        return cursor
    if cursor not in landable:
        return landable[0]
    return landable[(landable.index(cursor) + delta) % len(landable)]


class _ScrollingControl(FormattedTextControl):
    """A `FormattedTextControl` that turns the wheel into a caller-supplied step.

    prompt_toolkit already delivers a mouse event only to the control under the
    pointer, so "scroll whichever side the mouse is over" needs no hit-testing of
    our own — each side just handles its own events. Returning None marks the
    event handled; returning NotImplemented would let it bubble and the other
    side would react too.
    """

    def __init__(self, *args: Any, on_scroll: Callable[[int], None], **kw: Any) -> None:
        # `on_scroll` receives NOTCHES (-1 up, +1 down), not lines: the list moves
        # one ROW per notch (precise selection) while the preview moves several
        # LINES, and only each side knows which it wants.
        super().__init__(*args, **kw)
        self._on_scroll = on_scroll

    def mouse_handler(self, mouse_event: MouseEvent) -> object:
        if mouse_event.event_type is MouseEventType.SCROLL_UP:
            self._on_scroll(-1)
            return None
        if mouse_event.event_type is MouseEventType.SCROLL_DOWN:
            self._on_scroll(1)
            return None
        return super().mouse_handler(mouse_event)


def pick_with_preview(title: str, entries: list[PickerEntry], *, allow_delete: bool = False, allow_modify: bool = False, legend_text: str | None = None) -> tuple[PickerAction | None, Any]:
    """Render a full-screen picker; block until the user picks or cancels.

    legend_text — optional ANSI string. When provided, F8 toggles it as an overlay
    over the preview pane (Esc closes it). The agent picker passes LEGEND_TEXT so
    users can recall what each tag's kind punctuation means."""
    if not entries:
        raise ValueError("entries must be non-empty")

    state: dict[str, Any] = {
        # First selectable row, not blindly 0 — entries[0] can be
        # information-only (a running instance heading the delete submenu).
        "cursor": next((i for i, e in enumerate(entries) if e.selectable), 0),
        "filter": "",
        "shown": list(range(len(entries))),
        "result": (None, None),
        "legend_open": False,
        # Lines the preview is scrolled down by. Reset whenever the preview's
        # CONTENT changes (a new row, or the legend opening), because a leftover
        # offset would open the next preview part-way down for no reason.
        "preview_scroll": 0,
    }

    def focusable() -> list[int]:
        return _focusable_indices(entries, state["shown"])

    def refilter() -> None:
        q = state["filter"].lower()
        state["shown"] = [i for i in range(len(entries))
                          if q in _plain(entries[i].display).lower()]
        if state["cursor"] in state["shown"] and entries[state["cursor"]].selectable:
            return                                    # current row survived the filter
        landable = focusable()
        # Fall back to the first visible row when nothing is focusable, so the
        # cursor stays inside `shown` for rendering (Enter is guarded anyway).
        state["cursor"] = landable[0] if landable else (state["shown"][0] if state["shown"] else 0)

    def scroll_list(notches: int) -> None:
        """Move the highlight by `notches` focusable rows.

        Moves the CURSOR rather than a viewport offset, so the wheel and the arrow
        keys can never disagree about which row is selected — and the preview
        follows along, which is what makes wheeling the list useful at all.
        Unselectable rows are skipped because `focusable()` already excludes them."""
        landable = focusable()
        if not landable:
            return
        here = state["cursor"]
        nearest = min(range(len(landable)), key=lambda i: abs(landable[i] - here))
        state["cursor"] = landable[max(0, min(nearest + notches, len(landable) - 1))]
        # A new row means a new preview, so it starts at the top — EXCEPT while the
        # legend is open: the side pane is then showing the legend, whose content
        # does not depend on the cursor, so resetting it would throw away the
        # reader's place in it for no reason.
        if not state["legend_open"]:
            state["preview_scroll"] = 0

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

    def _preview_source() -> str:
        """The preview's full text, before scrolling."""
        if state["legend_open"] and legend_text is not None:
            return legend_text
        if not state["shown"]:
            return ""
        return loader.text(state["cursor"], entries[state["cursor"]])

    def scroll_preview(notches: int) -> None:
        """Move the preview by `notches` wheel steps, clamped to its content.

        Clamped rather than free-running: scrolling a short preview off the top
        looks like the pane went blank. Two lines are always left reachable so the
        end of the text still reads as the end."""
        limit = max(0, _preview_lines_total() - 2)
        state["preview_scroll"] = max(
            0, min(state["preview_scroll"] + notches * WHEEL_LINES, limit))

    def _preview_lines_total() -> int:
        return len(_preview_source().splitlines())

    def preview_text() -> ANSI | str:
        source = _preview_source()
        if not source:
            return ""
        offset = state["preview_scroll"]
        lines = source.splitlines()
        if offset:
            # Slicing whole LINES, not characters: the text carries ANSI escapes
            # and cutting mid-sequence would leak the escape into the output.
            source = "\n".join(lines[offset:])
        # A one-line position marker, since there is no usable scrollbar (see the
        # preview Window). It states only what is actually known — how far down the
        # SOURCE we are — rather than implying a viewport size the slice cannot know.
        if len(lines) > PREVIEW_POSITION_FLOOR:
            source = f"{PREVIEW_POSITION.format(offset + 1, len(lines))}\n{source}"
        return ANSI(source)

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
        state["cursor"] = _cursor_step(entries, state["shown"], state["cursor"], delta)
        if not state["legend_open"]:      # the legend does not follow the cursor
            state["preview_scroll"] = 0

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
        if landable := focusable():
            state["cursor"] = landable[0]
            if not state["legend_open"]:
                state["preview_scroll"] = 0

    @kb.add("end")
    def _(event: KeyPressEvent) -> None:
        if landable := focusable():
            state["cursor"] = landable[-1]
            if not state["legend_open"]:
                state["preview_scroll"] = 0

    @kb.add("enter")
    def _(event: KeyPressEvent) -> None:
        # The selectable check is belt-and-braces: move()/refilter() keep the
        # cursor off information-only rows, but it also covers the
        # nothing-focusable case (every visible row is information-only).
        if state["shown"] and entries[state["cursor"]].selectable:
            state["result"] = (PickerAction.SELECT, entries[state["cursor"]].value)
            event.app.exit()

    @kb.add("escape")
    def _(event: KeyPressEvent) -> None:
        if state["legend_open"]:
            state["legend_open"] = False
            state["preview_scroll"] = 0
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
            state["preview_scroll"] = 0   # legend and preview scroll independently

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
            return PickerRowMarker.CONT.accent      # yellow — the kind colour the grey lead no longer carries
        if isinstance(value, Agent):                # new row
            return PickerRowMarker.NEW.accent       # green
        return UiClass.DIVIDER.css

    body = HSplit([
        Window(FormattedTextControl(_fragment_source(title_fragments)), height=TITLE_HEIGHT),
        VSplit([
            Window(
                _ScrollingControl(_fragment_source(list_fragments),
                                  get_cursor_position=cursor_pos,
                                  focusable=True,
                                  show_cursor=False,
                                  on_scroll=scroll_list),
                wrap_lines=False,
                width=D(weight=LIST_WEIGHT),
            ),
            Window(width=DIVIDER_WIDTH, char=DIVIDER_CHAR, style=UiClass.DIVIDER.css),
            Window(width=1, char="▌", style=accent_style),   # preview-side accent bar; colour reflects selected row's kind
            Window(
                _ScrollingControl(preview_text, on_scroll=scroll_preview),
                wrap_lines=True,
                width=D(weight=PREVIEW_WEIGHT),
                style=UiClass.PREVIEW.css,
                # NO ScrollbarMargin. It renders the WINDOW's own scroll state,
                # while the scrolling here is done by slicing the text before the
                # window ever sees it — so the bar described a viewport that does
                # not exist: it sat at the bottom while the text was at the top,
                # then shrank away as the sliced content got shorter. A correct bar
                # would mean scrolling the window instead of the text (and
                # ScrollbarMargin cannot be dragged either — it has no mouse
                # handler). The position indicator below is honest about what it
                # knows; see `preview_text`.
            ),
        ]),
        Window(FormattedTextControl(_fragment_source(status_fragments)), height=STATUS_HEIGHT),
    ])

    app: Application[None] = Application(
        layout=Layout(body),
        key_bindings=kb,
        style=Style.from_dict(STYLE_DICT),
        full_screen=True,
        # Without this prompt_toolkit never puts the terminal into mouse-reporting
        # mode, so NO mouse event reaches any control — the per-side scroll
        # handlers were correct and simply never called. It defaults to False.
        #
        # The trade-off, stated because it is felt: while the picker is open the
        # terminal's own click-drag selection is suppressed (the app owns the
        # mouse), so text cannot be selected out of a row or preview until the
        # picker closes. Holding Shift bypasses it in most terminals.
        mouse_support=True,
    )
    # Created here, after `app` exists, because the worker needs its
    # (thread-safe) invalidate; preview_text above reaches `loader` through the
    # closure, which resolves by the time the first render calls it.
    loader = _PreviewLoader(app.invalidate)
    try:
        app.run()
    finally:
        loader.shutdown()
        # mouse_support means the terminal streams `\e[<35;x;yM` reports the
        # whole time the picker is open. prompt_toolkit turns the mode off on
        # exit, but reports already IN FLIGHT land on the tty after it stops
        # reading — and echo as `35;77;15M` garbage at whatever prompt comes
        # next. Repairing + draining here closes that race (Esc included).
        reset_terminal(drain_input=True)

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


def _template_preview(template: ClusterTemplate, path: Path) -> str:
    """The cluster-template row's preview: what a cluster is and who the
    default members are (names in the picker's blue). No key tutorial — the
    picker's status bar and F8 legend own the keys, same as every other row."""
    description = f"{template.description}\n\n" if template.description else ""
    members = Text()
    for m in template.members:
        members.append("  • ", style="dim")
        members.append(m.id, style=RICH_MEMBER_NAME)
        members.append("\n")
    members.rstrip()
    return _render_parts(
        Markdown(f"*Create a cluster from `agents/{path.name}`*\n\n---\n\n"
                 f"{description}"
                 f"A **cluster** is N agents cohabiting one container on one "
                 f"project, each in its own multiplexer window, able to message "
                 f"each other by name.\n\nDefault members:"),
        Text(),
        members)


# The rich twin of STYLE_AGENT_NAME: previews render through rich, the rows
# through prompt_toolkit, and member names must wear the same blue in both.
RICH_MEMBER_NAME = "bold bright_blue"


def _render_parts(*parts: Any) -> str:
    """Markdown and rich renderables interleaved into one ANSI preview string —
    `_render_md`'s console, accepting prepared renderables. Exists because
    markdown cannot colour a SPAN, and the cluster previews colour member
    names and tag labels inline."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="truecolor",
                      width=80)
    for part in parts:
        console.print(part)
    return buf.getvalue()


def _member_line(registry: Registry, identifier: str, build: AgentBuild) -> Text:
    """One preview line for a member: bullet, BLUE name (the colour agent names
    wear everywhere in the picker), then its tag labels in their legend colours
    — a name that no longer resolves renders alert-style rather than vanishing."""
    line = Text("  • ", style="dim")
    line.append(identifier, style=RICH_MEMBER_NAME)
    names = (*((build.engine,) if build.engine else ()),
             *build.professions, *build.specialties, *build.policies)
    for name in names:
        line.append("  ")
        if (tag := registry.get(name)) is not None:
            line.append(tag.label, style=RICH_BY_STYLE[tag_style(tag)])
        else:
            line.append(name, style="black on red")
    return line


def _cluster_preview(registry: Registry, cluster: "cluster_state.Cluster") -> str:
    """An existing cluster's preview: who is in it, wearing what. Keys are the
    status bar's and legend's job, same as every other row."""
    origin = f" (from `{cluster.template}.legoset`)" if cluster.template else ""
    return _render_parts(
        Markdown(f"*Cluster `{cluster.session}`{origin}*\n\n---\n\n"
                 f"project: `{cluster.project}`"),
        Text(),
        *[_member_line(registry, m.id, m.build)
          for m in cluster_state.picker_order(cluster.members, registry)])


def _member_preview(registry: Registry, cluster: "cluster_state.Cluster",
                    member: "cluster_state.Member") -> str:
    """One member's preview: its agent, its tags — plus the one member-specific
    fact worth stating (the forced tags), with the generic key tutorial gone
    like every other preview's."""
    tags = _member_line(registry, member.id, member.build)
    return _render_parts(
        Markdown(f"*`{member.id}` — member of cluster `{cluster.session}`*"
                 f"\n\n---\n\n"
                 f"agent: `{member.agent}` · role: `{member.role}`"),
        Text(),
        tags,
        Markdown("\n*`{muxer}` and `{cluster}` are re-applied on every edit — "
                 "every member carries them.*"))


def _edit_member_flow(registry: Registry, session: str, member_id: str) -> None:
    """F2 on a member: the ordinary tag form, persisted into cluster.toml.

    Reloads the cluster (rows may be stale — see _ClusterRow) and saves through
    `Cluster.with_build`, which re-applies the forced specialties; the form
    happily lets a user untick them, and silently losing {cluster} would make
    the member introduce itself wrongly on its next launch."""
    cluster = cluster_state.load(session)
    if cluster is None or (member := cluster.member(member_id)) is None:
        print(f"\n  Cluster '{session}' changed on disk — no member '{member_id}'.")
        input("  Press Enter to return to the picker… ")
        return
    new_build = prompt_tags(registry, member.build,
                            instance=f"{member_id}  (cluster: {session})",
                            workspace=str(cluster.project))
    if new_build is None:
        return
    assert isinstance(new_build, AgentBuild)   # fieldless call — narrow the union
    cluster_state.save(cluster.with_build(member_id, new_build))


def _remove_member_flow(session: str, member_id: str) -> None:
    """Del on a member: shrink the cluster by one, guarding the last member —
    an empty cluster is unrepresentable (Cluster refuses it), and the honest
    gesture for 'remove the only member' is destroying the cluster."""
    cluster = cluster_state.load(session)
    if cluster is None or cluster.member(member_id) is None:
        return
    if len(cluster.members) == 1:
        print(f"\n  '{member_id}' is the only member — a cluster cannot be "
              f"empty.\n  Del on the cluster row destroys '{session}' instead.")
        input("  Press Enter to return to the picker… ")
        return
    if confirm_dialog(f"Remove member '{member_id}' from cluster '{session}'?"):
        cluster_state.save(cluster.without_member(member_id))


def _destroy_cluster_flow(session: str) -> None:
    """Del on a cluster row: the shared teardown (`state.destroy` — worktrees
    removed, branches kept, state dir deleted), behind a confirmation naming
    everything it takes with it."""
    cluster = cluster_state.load(session)
    if cluster is None:
        return
    if confirm_dialog(f"Destroy cluster '{session}' and its "
                      f"{len(cluster.members)} member(s)? (branches are kept)"):
        cluster_state.destroy(cluster)


def _session_field_error(value: str, current: str | None = None) -> str | None:
    """The cluster-name field's live complaint, or None. `current` is the name
    an EDIT arrived with — keeping your own name is never a collision (the
    same allowance prompt_session gives instances)."""
    if not value:
        return "cannot be empty"
    try:
        valid_label(value, "session name")
    except ClusterError as error:
        return str(error)
    if value != current and cluster_state.exists(value):
        return "a cluster of this name already exists"
    return None


def _project_field_error(value: str) -> str | None:
    """The project-path field's live complaint, or None. Same rule the
    workspace prompt enforces: it must BE a directory, `~` welcome."""
    if not value:
        return "cannot be empty"
    if not is_dir(expand_user_path(value)):
        return f"not a directory: {expand_user_path(value)}"
    return None


def _cluster_fields(project: str, session: str, *,
                    current: str | None = None,
                    derive: str | None = None) -> "list[TextField]":
    """The two text fields both cluster forms carry, live-validated. One
    builder so create and edit cannot drift in what they accept.

    PROJECT FIRST — it is what the name derives from: with `derive` (the
    template name, creation only), the name field auto-fills
    `<template>__<workspace-basename>` and keeps following the path as it is
    typed, until the user touches the name field — the same shape instance
    ids have (`<agent>__<session>`), and the same "basename is the default
    name" rule prompt_session used. An edit passes no `derive`: the existing
    name sits still, renames are deliberate."""
    def auto_name(values: dict[str, str]) -> str:
        raw = values.get("project", "").strip()
        base = Path(expand_user_path(raw)).name if raw else ""
        return f"{derive}__{base}" if base else str(derive)
    return [
        TextField(key="project", label="project path", value=project,
                  validate=_project_field_error),
        TextField(key="session", label="cluster name", value=session,
                  validate=lambda v: _session_field_error(v, current),
                  auto=auto_name if derive is not None else None),
    ]


def _agent_rows(registry: Registry) -> list[tuple[str, str]]:
    """Every pickable agent as (name, one-liner) — the membership form's list,
    in picker order (which the form's panel uses as its sort rank)."""
    return [(a.name, _agent_description(read_text(a.md_path)))
            for a in creatable_agents(registry)]


def _suffix_field_error(agent: str, value: str,
                        current: str | None = None) -> str | None:
    """The instance-name field's live complaint, or None — the collision rule
    prompt_session enforced, as a validator: `<agent>__<value>` must not name
    an existing instance, except the one an edit arrived as."""
    if not value:
        return "cannot be empty"
    candidate = f"{agent}{SESSION_SEP}{value}"
    if value != current and path_exists(instance_state_dir_path(candidate)):
        return f"instance '{candidate}' already exists"
    return None


def instance_fields(agent: str, *, workspace: str | None = None,
                    suffix: str | None = None,
                    current: str | None = None) -> "list[TextField]":
    """The instance form's two text fields — project path FIRST, then the
    session name that completes `<agent>__<name>`, auto-derived from the
    path's basename until the user types their own (exactly the cluster
    form's rule, exactly prompt_session's old default). An edit passes the
    stored values plus `current`, which pins the name (renames stay
    deliberate) and exempts it from its own collision check."""
    def auto_suffix(values: dict[str, str]) -> str:
        raw = values.get("workspace", "").strip()
        return (Path(expand_user_path(raw)).name if raw else "") or agent
    return [
        TextField(key="workspace", label="project path",
                  value=workspace if workspace is not None else DEFAULT_WORKSPACE,
                  validate=_project_field_error),
        TextField(key="session", label=f"name  ({agent}__…)",
                  value=suffix or "",
                  validate=lambda v: _suffix_field_error(agent, v, current),
                  auto=auto_suffix if current is None else None),
    ]


def _create_cluster_flow(registry: Registry, template_path: Path) -> None:
    """The whole creation flow for one template — ONE form: the cluster's
    name and project path as text fields above the agent list, membership by
    picking. Returns to the picker whatever happens; Enter on the created
    cluster's own row is what launches it."""
    template = load_legoset(template_path)   # row-build already validated it
    result = prompt_members(
        _agent_rows(registry), prefill_picks(template),
        title="New cluster  (type into the fields; Space adds an agent):",
        fields=_cluster_fields(DEFAULT_WORKSPACE, template.name,
                               derive=template.name))
    if result is None:
        return
    values, picks = result
    project = expand_user_path(values["project"])
    cluster_state.save(cluster_state.from_template(
        values["session"], Path(project), assemble(picks, AGENTS_DIR),
        template=template.name))
    # No summary, no Enter-pause: the picker redraws with the new cluster row
    # (and its member rows) — that IS the confirmation, same as editing.


def _edit_cluster_flow(registry: Registry, session: str) -> None:
    """F2 on a cluster row: the SAME form as creation, prefilled — rename in
    the name field (the whole cluster directory moves), repoint the project,
    grow or shrink the membership. Surviving members keep their CURRENT
    builds (a rename must not wipe per-member tag edits back to `.lego`
    defaults — `legoset.reassemble` is that guarantee); added ones start from
    their agent's `.lego` plus the forced tags, exactly as creation would."""
    cluster = cluster_state.load(session)
    if cluster is None:
        print(f"\n  Cluster '{session}' is gone from disk.")
        input("  Press Enter to return to the picker… ")
        return
    prefill = [(m.agent, None if m.role == m.agent else m.role)
               for m in cluster_state.picker_order(cluster.members, registry)]
    result = prompt_members(
        _agent_rows(registry), prefill,
        title=f"Edit cluster '{session}'  (fields + membership):",
        fields=_cluster_fields(str(cluster.project), cluster.session,
                               current=cluster.session))
    if result is None:
        return
    values, picks = result
    members = tuple(cluster_state.with_forced_tags(m)
                    for m in reassemble(cluster.members, picks, AGENTS_DIR))
    updated = dataclasses.replace(
        cluster, project=Path(expand_user_path(values["project"])),
        members=members)
    if values["session"] != cluster.session:
        cluster_state.rename(updated, values["session"])
    else:
        cluster_state.save(updated)
    # No summary, no Enter-pause: the picker redraws with the edited row, and
    # that IS the confirmation (the pause here was reported as noise).


STOP_FORM_TITLE = "Stop running containers  (Space to mark, Enter to stop):"


def prompt_stop(registry: Registry) -> list[str]:
    """The `--stop` selector: every RUNNING instance and cluster as a checkbox
    row wearing the picker's own Cont-row anatomy — tags · instance name ·
    cwd hint · workspace — minus the `(RUNNING)` hint, which would say nothing
    in a list that is running by definition. `{muxer}` is emphasized wherever
    present: sticky sessions are this flag's reason to exist (a muxer
    container outlives its terminal, so this list is how one is ended without
    re-attaching). Returns the picked docker ids, CONTAINER_NAME_PREFIX
    already stripped (the running-snapshot's spelling) — empty on Esc or when
    nothing runs.

    A running id that matches no store entry and no cluster still gets a bare
    row (id only): a stray is exactly what someone reaching for --stop most
    needs to be able to stop."""
    running = docker_running_instances_subprocess() or frozenset()
    options: list[FormOption] = []
    matched: set[str] = set()

    live = [e for e in continuable_instances(registry, running) if e.is_running]
    columns = {e.identity.instance:
               _cont_tags_column(e.identity, emphasize=frozenset({"muxer"}))
               for e in live}
    col_width = max((w for _, w in columns.values()), default=0)
    name_width = max((len(e.identity.instance) for e in live), default=0)
    for entry in live:
        matched.add(entry.identity.instance)
        fragments, width = columns[entry.identity.instance]
        display = [*fragments, ("", " " * (col_width - width)),
                   (STYLE_AGENT_NAME,
                    f"{entry.identity.instance:<{name_width}}"),
                   ("", "    ")]
        if entry.is_current_dir:
            display.append(PickerCwdHint.CURRENT.fragment)
        elif entry.is_default_dir:
            display.append(PickerCwdHint.DEFAULT.fragment)
        elif entry.is_invalid_dir:
            display.append(PickerCwdHint.INVALID.fragment)
        display.append((STYLE_WORKSPACE_HINT, entry.workspace_display))
        options.append(FormOption(
            key=entry.identity.instance, label=display,
            body=[("", f"last used {entry.last_used_display}   ·   stopping "
                       "ends the container; the conversation resumes on the "
                       "next launch")]))

    for cluster in cluster_state.discover():
        container_id = cluster_container_id(cluster.session)
        if container_id not in running:
            continue
        matched.add(container_id)
        options.append(FormOption(
            key=container_id,
            label=[(STYLE_MEMBER_COUNT, f"({len(cluster.members)} members)"),
                   ("", "  "),
                   (STYLE_AGENT_NAME, cluster.session), ("", "    "),
                   (STYLE_WORKSPACE_HINT, str(cluster.project))],
            body=[("", "members: " + ", ".join(cluster.ids))]))

    for stray in sorted(running - matched):
        options.append(FormOption(
            key=stray, label=[(STYLE_AGENT_NAME, stray)],
            body=[("", "a running launcher container with no store entry — "
                       "stoppable, not otherwise known here")]))

    if not options:
        print("  Nothing is running.")
        return []
    result = checkbox_form(STOP_FORM_TITLE, options)
    # No text fields ride this form, so a non-None return IS the checked-key
    # list — the tuple variant exists only for field-carrying forms. The
    # isinstance narrows for mypy rather than assumes.
    return result if isinstance(result, list) else []


def select_agent(registry: Registry) -> "Agent | Instance | cluster_state.Cluster | None":
    """Run the agent picker (main + nested deletion submenu) until selection or cancel.
    Returns an Agent (create), an Instance (continue), a Cluster (launch it),
    or None (cancel). Caller must ensure at least one agent .md exists before
    invoking."""
    legend_text = _build_composition_legend(registry)   # built once per call — the loop below only re-scans instances
    while True:
        agents = creatable_agents(registry)
        # ONE `docker ps` per menu build, shared by the instance rows and the
        # cluster rows below — a running row of either kind greys out.
        running = docker_running_instances_subprocess() or frozenset()
        instances = continuable_instances(registry, running)

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
        tag_by_inst = {i.identity.instance: _cont_tags_column(i.identity) for i in instances}
        tag_col_width  = max((w for _, w in tag_by_agent.values()), default=0)
        cont_col_width = max((w for _, w in tag_by_inst.values()), default=0)

        entries: list[PickerEntry] = []
        for agent in agents:
            tag_frags, tag_len = tag_by_agent[agent.name]
            entries.append(PickerEntry(
                display=[
                    *PickerRowMarker.NEW.fragments("  "),
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
                    *PickerRowMarker.CONT.fragments("      "),
                    *cont_frags,
                    ("", " " * (cont_col_width - cont_len)),
                    (STYLE_RUNNING_NAME if inst.is_running else STYLE_AGENT_NAME,
                     f"{identity.instance:<{instance_name_width}}"),
                    ("", "    "),
                ]
                if inst.is_running:
                    cont_display.append(RUNNING_HINT)
                if inst.is_current_dir:
                    cont_display.append(PickerCwdHint.CURRENT.fragment)
                elif inst.is_default_dir:
                    cont_display.append(PickerCwdHint.DEFAULT.fragment)
                elif inst.is_invalid_dir:
                    cont_display.append(PickerCwdHint.INVALID.fragment)
                cont_display.append((STYLE_WORKSPACE_HINT, inst.workspace_display))
                entries.append(PickerEntry(
                    display=cont_display,
                    preview=_deferred_preview(inst),   # reads transcripts on first highlight, not at menu open
                    preview_quick=_deferred_preview(inst, quick=True),
                    value=identity,
                    # A live container owns the name (Enter would hit a docker
                    # name conflict) and owns the state dir rw (Del would delete
                    # under it), so the row is information-only. selectable=False
                    # is what actually blocks all three keys; the other two are
                    # explicit belt-and-braces.
                    selectable=not inst.is_running,
                    deletable=not inst.is_running,
                    modifiable=not inst.is_running,
                ))

        # One row per `.legoset` — a cluster is CREATED from here (the
        # membership form). A template that fails to parse or names unknown
        # agents renders as an unselectable red row instead of crashing the
        # picker: templates are hand-authored files, and the picker is where
        # the author is.
        known_agents = frozenset(a.name for a in agents)
        for template_name, template_path in discover_templates(AGENTS_DIR).items():
            try:
                template = load_legoset(template_path)
                validate(template, known_agents)
            except ClusterError as error:
                entries.append(PickerEntry(
                    display=[*PickerRowMarker.CLUSTER.fragments("  "),
                             (STYLE_TAG_INVALID, template_name),
                             ("", f" — broken template: {error}")],
                    preview=_render_md(f"*`agents/{template_path.name}` failed to "
                                       f"load:*\n\n```\n{error}\n```"),
                    value=None, selectable=False, deletable=False, modifiable=False,
                ))
                continue
            # Same anatomy as an agent row — lead, a metadata column, the NAME
            # in the agents' name column, then " — description". The metadata
            # column holds the member count where agent rows hold tags, padded
            # so the name lands exactly where agent names do (the cluster tab
            # is wider, so the pad is measured, not copied).
            count = f"({len(template.members)} members)"
            name_column = PickerRowMarker.NEW.width("  ") + tag_col_width
            pad = max(name_column - PickerRowMarker.CLUSTER.width("  ")
                      - len(count), 1)
            entries.append(PickerEntry(
                display=[
                    *PickerRowMarker.CLUSTER.fragments("  "),
                    (STYLE_MEMBER_COUNT, count),
                    ("", " " * pad),
                    (STYLE_AGENT_NAME, f"{template_name:<{agent_name_width}}"),
                    ("", f" — {template.description or ', '.join(m.id for m in template.members)}"),
                ],
                preview=_template_preview(template, template_path),
                value=_ClusterTemplateRow(template_name, template_path),
                deletable=False,
                modifiable=False,
            ))

        # Existing clusters nest under the template rows, their members one
        # level deeper — the same parent/child shape agents and instances use.
        # Enter on the cluster row launches it, Del destroys it; a member row
        # is the editing unit: F2 re-tags, Del removes.
        for cluster in cluster_state.discover():
            cluster_running = cluster_container_id(cluster.session) in running
            cluster_display = [
                *PickerRowMarker.CLSTR.fragments("  "),
                (STYLE_RUNNING_NAME if cluster_running else STYLE_AGENT_NAME,
                 cluster.session),
                ("", f"  ({len(cluster.members)} members)    "),
            ]
            if cluster_running:
                cluster_display.append(RUNNING_HINT)
            cluster_display.append((STYLE_WORKSPACE_HINT, str(cluster.project)))
            entries.append(PickerEntry(
                display=cluster_display,
                preview=_cluster_preview(registry, cluster),
                value=_ClusterRow(cluster.session),
                # The same information-only rule running instances get: the
                # live container owns the name (Enter → docker name conflict)
                # and has the state dir mounted at /cluster (F2's rename would
                # move it out from under the container; Del would delete it).
                selectable=not cluster_running,
                deletable=not cluster_running,
                modifiable=not cluster_running,
            ))
            # Rows in picker order — the same derived order the windows will
            # launch in, so the list here IS the `^b 1..9` numbering.
            for member in cluster_state.picker_order(cluster.members, registry):
                member_tags, _ = _tags_column(build_tags(member.build))
                entries.append(PickerEntry(
                    display=[
                        *PickerRowMarker.MEMBER.fragments(""),
                        (STYLE_RUNNING_NAME if cluster_running else STYLE_AGENT_NAME,
                         member.id),
                        ("", "  "),
                        *member_tags,
                    ],
                    preview=_member_preview(registry, cluster, member),
                    value=_MemberRow(cluster.session, member.id),
                    # Members follow their cluster: F2/Del write cluster.toml
                    # inside the very dir the live container has mounted.
                    selectable=not cluster_running,
                    deletable=not cluster_running,
                    modifiable=not cluster_running,
                ))

        # Unconditional, unlike the toolkit-only era: the form's UI section
        # (the {mux} backend pick) is profession-independent, so the row must
        # exist even when no configurable profession does.
        entries.append(PickerEntry(
            display=[
                *PickerRowMarker.TOOLS.fragments("  "),
                ("", PREFERENCES_LABEL),
            ],
            preview=PREFERENCES_PREVIEW,
            value=_OPEN_PREFERENCES,
            deletable=False,
            modifiable=False,
        ))

        entries.append(PickerEntry(
            display=[
                *PickerRowMarker.DELMNU.fragments("  "),
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

        if action == PickerAction.DELETE:  # picker enforces deletability — cont, cluster, and member rows reach here
            if isinstance(value, _ClusterRow):
                _destroy_cluster_flow(value.session)
            elif isinstance(value, _MemberRow):
                _remove_member_flow(value.session, value.member_id)
            elif confirm_dialog(CONFIRM_DELETE_FMT.format(name=value.instance)):
                delete_instance(value)
            continue

        if action == PickerAction.MODIFY and isinstance(value, _ClusterRow):
            _edit_cluster_flow(registry, value.session)
            continue

        if action == PickerAction.MODIFY and isinstance(value, _MemberRow):
            _edit_member_flow(registry, value.session, value.member_id)
            continue

        if action == PickerAction.MODIFY:  # instance cont rows — the only other modifiable kind
            old_inst = value
            # ONE form: workspace + name as text fields above the tags — the
            # same no-terminal-prompt shape cluster editing has.
            result = prompt_tags(
                registry, old_inst.build, instance=old_inst.agent,
                fields=instance_fields(old_inst.agent,
                                       workspace=old_inst.workspace,
                                       suffix=old_inst.session,
                                       current=old_inst.session))
            if result is None:   # Esc — abort the modify, back to the picker
                continue
            values, new_build = result
            new_inst = dataclasses.replace(
                old_inst, session=values["session"],
                workspace=expand_user_path(values["workspace"]),
                invalid_tags=(),   # re-picking against the live registry clears any stale/typo'd tags
                **resolve_build(new_build, old_inst.agent, registry),
            )  # is_brand_new stays False via the dataclass replace
            modify_instance(old_inst, new_inst)
            continue

        if value is _OPEN_PREFERENCES:
            edit_profiles_menu(registry)
            continue

        if value is _OPEN_DELMENU:
            _delete_submenu(registry, legend_text)
            continue

        if isinstance(value, _ClusterTemplateRow):
            _create_cluster_flow(registry, value.path)
            continue   # created (or cancelled) — back to the picker either way

        if isinstance(value, _ClusterRow):
            # Enter on a cluster LAUNCHES it — run.py owns docker, so hand the
            # loaded cluster back the same way an Agent/Instance is handed.
            # Reloaded from disk (the row may predate an edit); vanished means
            # someone destroyed it underneath — explain, don't crash.
            if (picked_cluster := cluster_state.load(value.session)) is not None:
                return picked_cluster
            print(f"\n  Cluster '{value.session}' is gone from disk.")
            input("  Press Enter to return to the picker… ")
            continue

        if isinstance(value, _MemberRow):
            # A member alone is not launchable — say what the row is FOR
            # instead of silently ignoring the key.
            print("\n  A member launches with its cluster — Enter on the"
                  "\n  cluster row above. Here: F2 edits this member's tags,"
                  "\n  Del removes it from the cluster.")
            input("\n  Press Enter to return to the picker… ")
            continue

        if isinstance(value, Instance) and not value.is_startable:
            # A Cont row whose stored tags no longer resolve — explain and
            # bounce back to the picker (F2 re-picks; Del removes it) rather
            # than starting a half-resolved instance.
            print("\n" + invalid_tags_report(value) + "\n")
            input("  Press Enter to return to the picker… ")
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
            row = [*PickerRowMarker.DLET.fragments("  "),
                   (STYLE_RUNNING_NAME if inst.is_running else STYLE_DEL_NAME, identity.instance)]
            if inst.is_running:
                row.append(("", "  "))
                row.append(RUNNING_HINT)
            entries.append(PickerEntry(
                display=row,
                preview=_deferred_preview(inst),       # deferred, as in the main picker
                preview_quick=_deferred_preview(inst, quick=True),
                value=identity,
                # Deleting a live container's state dir (bind-mounted rw) could
                # corrupt the running session — information-only here too.
                selectable=not inst.is_running,
                deletable=not inst.is_running,
                modifiable=not inst.is_running,
            ))
        entries.append(PickerEntry(
            display=PickerRowMarker.BACK.fragments(f"  {BACK_LABEL}"),
            preview=BACK_PREVIEW,
            value=None,
            deletable=False,
        ))

        action, value = pick_with_preview(TITLE_DELETE_MENU, entries, allow_delete=True, legend_text=legend_text)
        if action is None or value is None:
            return
        if confirm_dialog(CONFIRM_DELETE_FMT.format(name=value.instance)):
            delete_instance(value)
