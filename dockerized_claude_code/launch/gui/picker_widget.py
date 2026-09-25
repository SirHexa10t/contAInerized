"""The reusable full-screen picker: what a ROW is, and the loop that selects
one (launch/gui).

`pick_with_preview` is the widget — a filterable list on the left, a preview
pane on the right, and one of select / delete / modify / cancel as the
outcome. It knows nothing about agents, instances or clusters: a caller hands
it `PickerEntry` rows and gets back the opaque `value` of whichever row was
chosen. `menu_picker` builds the launcher's actual menus on top of it.

Split out of `menu_picker.py` 2026-09-03, which was 1541 lines holding both
the widget and every menu built with it. The seam is one-way and was already
implicit: nothing in here refers to a single menu-side name.

What lives here:
  - `PickerEntry` / `ContEntry` / `MemberEntry`
                                  the row contract, including the DEFERRED
                                  preview slots (a row may hand over a
                                  callable instead of text, so a slow
                                  preview never blocks the first paint)
  - `PickerAction` / `PickerRowMarker` / `PickerCwdHint` / `WorkspaceView`
                                  the outcome enum, and the row decorations
                                  whose glyphs+styles are data on an enum
                                  rather than branches in a renderer — plus
                                  a row's workspace as ONE fact (path + its
                                  cwd relation)
  - `_PreviewLoader`              when to compute a preview and where to
                                  park it
  - `_tags_column` / `_cont_tags_column`
                                  the tag-chip columns rows render with
  - `_ScrollingControl`, `_cursor_step`, `_focusable_indices`, `_accent_style`
                                  navigation over rows the cursor may skip,
                                  and the accent bar's colour
  - `pick_with_preview`           the application + key bindings + loop
"""

from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property
from typing import Any, cast

from prompt_toolkit import Application                                     # dep — declared in pyproject.toml [project]
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.filters import Condition
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import Style

from ..cluster.member import Member
from ..tags import Instance, Tag, TagProblem
from ..tags.base import SQUASH_AT, first_glyph
from ..utils import reset_terminal
from .picker_previews import _last_prompt_display, cont_preview, member_preview
from .styles import (
    STATUS_HEIGHT, STYLE_DICT, STYLE_TAG_INVALID, TITLE_HEIGHT, UiClass,
    _fragment_source, _normalize, _plain, squashed_tag_style, tag_style,
)


# ============================================================
# Row decoration + layout — the widget's own vocabulary
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
HINT_PREVIEW_SUFFIX  = "  •  F12 hide preview"
HINT_PREVIEW_HIDDEN  = "  •  F12 show preview"
HINT_FIND_SUFFIX     = "  •  alt+f find"

# See the assignment in `pick_with_preview` for why this is not the default.
ESCAPE_FLUSH_SECONDS = 0.05
FILTER_LABEL         = "filter: "
EMPTY_FILTER_MESSAGE = "(no matches)"
PREVIEW_LOADING_TEXT = "… loading preview (keep browsing)"
LAST_PROMPT_LOADING  = "[loading…]"   # stands in for the Last prompt value while transcripts are read
DIVIDER_CHAR         = "│"
# The BREAK row's rule — the same box-drawing family as the divider above, so
# it is drawn by the terminal itself (see TAB_TIP for why that matters).
BREAK_CHAR           = "─"
LIST_WEIGHT    = 2
PREVIEW_WEIGHT = 3
DIVIDER_WIDTH  = 1
PAGE_JUMP      = 10  # rows skipped per PageUp/PageDown
# The bookmark shapes the picker's two CONTRASTED row kinds lead with (see
# PickerRowMarker): an agent row is a green tab with a fading end, its
# instances nest beneath behind a dim grey ▸. Green by request (iterated from
# an all-grey first pass) — it is also Create's KIND colour, the same green
# the preview's accent bar shows, so the tab and the bar agree.
STYLE_TAB            = "bg:ansigreen fg:black bold"         # the tab body behind "Create" — black text on the green art, per request
STYLE_TAB_TIP        = "fg:ansigreen"                       # its fading end: foreground == tab background, so the shade ramp reads as the tab dissolving, not as characters
STYLE_NEST_MARK      = "fg:ansibrightblack"                 # instance rows: dim marker, indented under the agent's tab
# The cluster-template rows wear the same tab shape in CYAN — a third kind
# beside create-green and continue-yellow, same colour its preview accent
# shows, and (since 2026-09-17) the only row whose accent is cyan: creating a
# cluster is what that colour marks.
# An existing cluster is a TOP-LEVEL row in the CONT shape (cyan LEAD, yellow
# accent — it exists, like an instance), listed
# after every template row rather than indented under its own: a cluster is
# not an instance OF a template the way an instance is of its agent — nothing
# is shared after creation (no common CLAUDE.md), so nesting would claim a
# relationship that is not there (operator, 2026-09-09). Its MEMBERS nest
# beneath it, one level in, exactly as instances nest under their agent —
# shape says create/continue, colour says cluster, depth says "belongs to".
STYLE_CLUSTER_TAB     = "bg:ansicyan fg:black bold"
STYLE_CLUSTER_TAB_TIP = "fg:ansicyan"
STYLE_CLUSTER_NEST    = "fg:ansicyan"
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
TAG_EMPHASIS         = "bold underline"   # style SUFFIX for tags an emphasize set names (see _tags_column) — on top of the tag's own color, so the color language survives the shout
STYLE_WORKSPACE_HINT = "italic fg:ansibrightblack"   # the workspace path at a row's tail
# The preview's edge bar (see PickerRowMarker.accent) speaks ONE language: what
# kind of thing the highlighted row is. Creating something is a colour per
# thing created; everything that already EXISTS — an instance, a cluster, a
# member — shares the continue colour, because that is the distinction the bar
# is read for (operator, 2026-09-17: "blue should be reserved for new
# clusters"). Named, so the next row kind picks from this list instead of
# inventing a fourth colour.
ACCENT_CREATE_AGENT   = "fg:ansigreen"    # + Agent  — matches the green tab (STYLE_TAB)
ACCENT_CREATE_CLUSTER = "fg:ansicyan"     # + Cluster — matches the cyan tab (STYLE_CLUSTER_TAB); this colour is the creation tab's alone
ACCENT_EXISTING       = "fg:ansiyellow"   # an instance, a cluster, or a member that is already on disk


class PickerAction(Enum):
    """Closed set of actions pick_with_preview returns alongside the selected
    entry's value. None (returned for cancel/escape) sits outside the enum so
    callers can branch on `if action is None` idiomatically."""
    SELECT = "select"     # Enter — user picked a row
    DELETE = "delete"     # Del   — user pressed delete on a row (only fires for deletable rows)
    MODIFY = "modify"     # F2    — user pressed modify on a row (only fires for modifiable rows)
    FIND   = "find"       # alt+f — user asked to search past conversations (no row involved)

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

    `.accent` is the narrower question — does this row EXIST or would picking
    it create something — so the three constants above are its whole
    vocabulary: green creates an agent instance, cyan creates a cluster, and
    yellow marks everything already on disk (instances, clusters, members).

    Members expose:
      .lead        — tuple of (style, text) fragments that start the row
      .accent      — preview accent-bar style while the row is selected
      .fragments() — the lead plus an optional alignment suffix, ready to
                     splat into a FormattedText list
    """
    # Both creation tabs lead with `+` — the creation intent in one glyph, and
    # the two tab NAMES then say what gets created (an agent instance; a
    # cluster) instead of one saying the verb and the other the noun.
    NEW     = (((STYLE_TAB, " + Agent "), (STYLE_TAB_TIP, TAB_TIP)), ACCENT_CREATE_AGENT)
    CONT    = (((STYLE_NEST_MARK, "   ▸ Cont."),),              ACCENT_EXISTING)
    CLUSTER = (((STYLE_CLUSTER_TAB, " + Cluster "), (STYLE_CLUSTER_TAB_TIP, TAB_TIP)),
               ACCENT_CREATE_CLUSTER)
    # "Cont.", the same word instance rows use — an existing cluster IS a
    # continuation, so its ACCENT is the continue yellow every existing thing
    # shows; the cyan stays in the row's own LEAD, where it says "cluster"
    # (kind = lead colour, state = accent colour). No indent: a cluster is a
    # top-level row, not a child of its template (see the STYLE_CLUSTER_*
    # comment); its members take the indent instead.
    CLSTR   = (((STYLE_CLUSTER_NEST, "▸ Cont."),),              ACCENT_EXISTING)
    # A member exists too — and it is an instance in all but placement, so it
    # reads like one on the bar; its cluster is named in the pane beside it.
    MEMBER  = (((STYLE_NEST_MARK, "   · "),),                   ACCENT_EXISTING)
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
    """The cwd-relation tag shown on a row's workspace. CURRENT/DEFAULT mark
    a healthy relation to where the launcher was invoked from; INVALID flags
    a stored workspace path that no longer exists / isn't a directory so the
    user can spot it before continuing (or hit F2 to repoint it). Same
    bundling rationale as PickerRowMarker — label text and style are a fixed
    pair, not two parallel constants. CURRENT/DEFAULT share a yellow style;
    kept as separate enum members so the colours can diverge later without
    re-threading call sites. Which one applies is `menu_picker`'s call (it
    knows the launch site); a row carries the answer as `WorkspaceView`."""
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

@dataclass(frozen=True)
class WorkspaceView:
    """How a row's workspace reads on screen: the path as stored (or the
    caller's placeholder when nothing is stored) and its relation to where
    the launcher runs — at most ONE hint, because a path is the cwd, or the
    default, or invalid, never two of those. It was three booleans on the row
    until 2026-09-09, whose docstring had to say only one could be true;
    instance rows, cluster rows and `--stop`'s rows all carry this one."""
    display: str
    hint: PickerCwdHint | None

    @property
    def fragments(self) -> list[tuple[str, str]]:
        """The row's tail: the hint, if any, then the path, dim italic."""
        tail = [(STYLE_WORKSPACE_HINT, self.display)]
        return [self.hint.fragment, *tail] if self.hint is not None else tail

@dataclass(frozen=True)
class ContEntry:
    """One Cont/DELETE row's data — what `continuable_instances` produces and
    `pick_with_preview` consumes. `identity` is what the picker hands back
    on selection; `workspace` is the stored path with its cwd relation (the
    CURRENT/DEFAULT/INVALID hint) as one fact; `last_used_display` is
    pre-rendered for the pane. `is_running` means a container for this
    instance is up right now, so the row renders greyed with the RUNNING tag
    and is information-only — docker would refuse a second container on the
    same `--name` anyway."""
    identity: Instance
    workspace: WorkspaceView
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
        """The preview text — built by `picker_previews`, which owns every
        row kind's pane content; this row only supplies its own values."""
        return cont_preview(self.identity, self.workspace.display,
                            self.last_used_display, prompt)

@dataclass(frozen=True, kw_only=True)
class MemberEntry(ContEntry):
    """A cluster MEMBER's row data: a ContEntry whose `identity` is the
    Instance the member launches as (`Cluster.member_instance` — a member is
    an instance in all but placement), plus what only a member has: which
    `cluster` it belongs to, its `Member` record (agent, role, OWN build), and
    the names of the tags it `inherited` from the cluster, so the pane can
    mark them. Everything the deferred-preview machinery does for an instance
    — the child-process `Last prompt` read, the `[loading…]` stand-in, the
    per-screen-session cache — happens here unchanged; only the pane's facts
    differ (`member_preview`). Keyword-only because a dataclass subclass may
    not add required fields after the base's defaulted `is_running`."""
    member: Member
    cluster: str
    inherited: frozenset[str]

    def _compose(self, prompt: str | None) -> str:
        return member_preview(self.identity, self.member, self.cluster,
                              self.workspace.display, self.last_used_display,
                              prompt, self.inherited)

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
    for Back rows). `marker` names the row's KIND — the same marker whose lead
    the producer splatted into `display` — so the widget can colour the
    preview's accent bar without knowing what the value is. `deletable` /
    `modifiable` / `pickable` gate Del / F2 / Enter per row and default True;
    the producer sets them False to make that key inert there (Create / Back
    / opener rows take no Del or F2; a cluster MEMBER takes no Enter — it
    launches with its cluster, and an inert key beats a pause that explains
    so every time). `selectable=False` makes the row INFORMATION-ONLY:
    still rendered, but the cursor never lands on it, so no key can target
    it (a running instance — see RUNNING_HINT).

    `separator=True` marks a BREAK row (`break_row`): a rule between two
    blocks of rows, never selectable and never filtered OUT — typing a word
    that no break contains must not weld one cluster's members onto the
    next's (operator, 2026-09-17). `_visible_indices` keeps them and collapses
    them, so a boundary never outlives what it separates.

    `display` defaults to a fresh empty list per instance to keep the
    dataclass safe — never shared across rows."""
    display: list[tuple[str, str]] = field(default_factory=list)
    preview: str | Callable[[], str] = ""
    preview_quick: str | Callable[[], str] | None = None   # cheap stand-in pane shown while `preview` resolves
    value: Any = None
    marker: PickerRowMarker | None = None
    deletable: bool = True
    modifiable: bool = True
    pickable: bool = True
    selectable: bool = True
    separator: bool = False

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

def _tags_column(tags: Iterable[Tag],
                 emphasize: frozenset[str] = frozenset(),
                 problems: Sequence[TagProblem] = (),
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
    chips of the same color read as two tags rather than one block.

    `problems` — stored names that no longer resolve — follow the tags in the
    red-background/black-foreground alert style so a stale/typo'd tag is
    impossible to miss (they also block the thing from starting). The
    SQUASH_AT threshold counts BOTH parts: six tags where one is invalid are
    exactly as crowded as six valid ones, and mixing one squashed part with
    one labelled part would make the alert look like a different feature
    rather than one of the row's tags. A squashed invalid tag stays visible
    for the same reason the labelled form does — its chip is the alert red no
    valid tag uses."""
    tag_list = list(tags)
    if not tag_list and not problems:
        return [], 0
    squash = len(tag_list) + len(problems) >= SQUASH_AT
    chips: list[tuple[str, str]] = []
    for tag in tag_list:
        style, text = ((squashed_tag_style(tag_style(tag)), tag.squash_glyph)
                       if squash else (tag_style(tag), tag.label))
        if tag.name in emphasize:
            style = f"{style} {TAG_EMPHASIS}"
        chips.append((style, text))
    chips += [(STYLE_TAG_INVALID, first_glyph(problem.name) if squash else problem.label)
              for problem in problems]
    fragments: list[tuple[str, str]] = []
    for chip in chips:
        if fragments:
            fragments.append(("", " "))
        fragments.append(chip)
    fragments.append(("", " "))   # trailing separator — bakes into the column width
    return fragments, sum(len(text) for _, text in fragments)

def _cont_tags_column(inst: Instance,
                      emphasize: frozenset[str] = frozenset(),
                      ) -> tuple[list[tuple[str, str]], int]:
    """A Cont row's tag column: the instance's resolved tags followed by its
    `invalid_tags` — `_tags_column` fed from the one identity record."""
    return _tags_column(inst.active_tags, emphasize, problems=inst.invalid_tags)

def break_row(width: int) -> PickerEntry:
    """A BREAK row `width` cells wide: a dim rule that separates two blocks of
    rows. Information only — the cursor never lands on it, so no key can
    target it — and the filter keeps it (`_visible_indices`), which is the
    point: filtered rows from two different clusters stay visibly apart."""
    return PickerEntry(display=[(UiClass.DIVIDER.css, BREAK_CHAR * width)],
                       selectable=False, pickable=False, deletable=False,
                       modifiable=False, separator=True)


def _visible_indices(entries: list[PickerEntry], query: str) -> list[int]:
    """The row indices the list shows for `query`: every row whose text
    contains it, plus every BREAK row — a break is a boundary, not content, so
    it is never filtered out by a word it does not contain.

    Collapsed, so a kept boundary always separates something: none leads the
    list, none trails it, and two that meet become one. Pure, so the rule is
    testable without driving a live prompt_toolkit Application."""
    q = query.lower()
    matched = [i for i in range(len(entries))
               if entries[i].separator or q in _plain(entries[i].display).lower()]
    out: list[int] = []
    for i in matched:
        if entries[i].separator and (not out or entries[out[-1]].separator):
            continue                      # nothing above it yet, or a break already sits there
        out.append(i)
    while out and entries[out[-1]].separator:
        out.pop()                         # nothing below it any more
    return out


# What the side pane is showing — the ONE answer both the layout (is the pane
# there at all) and the content function (what to compute) read, so they can
# never disagree about whether a preview is wanted.
PANE_HIDDEN, PANE_LEGEND, PANE_PREVIEW = "hidden", "legend", "preview"


def _pane_view(*, hidden: bool, legend_open: bool, has_legend: bool) -> str:
    """Which of the three the pane shows. F12 hides it outright; F8's legend
    wins over a preview while it is open (the pane is the legend's only home,
    so asking for the legend is asking for the pane — the F12 handler
    un-hides for it). A legend that no caller supplied is never open."""
    if hidden:
        return PANE_HIDDEN
    return PANE_LEGEND if (legend_open and has_legend) else PANE_PREVIEW


def _focusable_indices(entries: list[PickerEntry], shown: list[int]) -> list[int]:
    """Row indices the cursor may land on: visible after filtering AND
    selectable. Information-only rows (a running instance, a break) are
    rendered but never focusable, which is what blocks Enter / Del / F2 on
    them."""
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

def _accent_style(entry: PickerEntry | None) -> str:
    """The preview's left-edge accent bar: the highlighted row's KIND colour,
    read off its marker — green for an agent row, yellow for a Cont row, cyan
    for anything cluster-shaped — and the dim divider colour for no row, a row
    without a marker, or a marker without an accent (the menu openers). Until
    2026-09-09 this dispatched on `isinstance(value, Instance | Agent)`, the
    widget's one piece of agent knowledge — and so never showed the cyan the
    cluster markers declare."""
    if entry is None or entry.marker is None or not entry.marker.accent:
        return UiClass.DIVIDER.css
    return entry.marker.accent

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

def pick_with_preview(title: str, entries: list[PickerEntry], *, allow_delete: bool = False, allow_modify: bool = False, allow_find: bool = False, legend_text: str | None = None) -> tuple[PickerAction | None, Any]:
    """Render a full-screen picker; block until the user picks or cancels.

    legend_text — optional ANSI string. When provided, F8 toggles it as an overlay
    over the preview pane (Esc closes it). The agent picker passes LEGEND_TEXT so
    users can recall what each tag's kind punctuation means.

    allow_find — offer alt+f. It returns `(FIND, None)`: the picker knows the
    key, not what a search IS. Rows have nothing to do with it, so unlike Del
    and F2 it fires with no row involved, and the caller decides what to
    search and what to do with the answer."""
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
        # F12: the pane is gone and NOTHING is computed for it — the list gets
        # the full width. Modelled on the legend, which already spares the
        # transcript reads by never asking the loader while it is open.
        "preview_hidden": False,
        # Lines the preview is scrolled down by. Reset whenever the preview's
        # CONTENT changes (a new row, or the legend opening), because a leftover
        # offset would open the next preview part-way down for no reason.
        "preview_scroll": 0,
    }

    def focusable() -> list[int]:
        return _focusable_indices(entries, state["shown"])

    def refilter() -> None:
        state["shown"] = _visible_indices(entries, state["filter"])
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

    def pane_view() -> str:
        return _pane_view(hidden=state["preview_hidden"],
                          legend_open=state["legend_open"],
                          has_legend=legend_text is not None)

    def _preview_source() -> str:
        """The pane's full text, before scrolling — and the one place a
        preview is ever ASKED FOR. A hidden pane returns before
        `loader.text`, so no transcript is read and no worker is scheduled
        for a row nobody is looking at; the legend takes the same shortcut."""
        view = pane_view()
        if view == PANE_HIDDEN:
            return ""
        if view == PANE_LEGEND:
            return legend_text or ""
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
        if pane_view() == PANE_LEGEND:
            hint = HINT_LEGEND_OPEN
        else:
            hint = HINT_BASE_TEXT
            if allow_delete:
                hint += HINT_DELETE_SUFFIX
            if allow_modify:
                hint += HINT_MODIFY_SUFFIX
            if allow_find:
                hint += HINT_FIND_SUFFIX
            if legend_text is not None:
                hint += HINT_LEGEND_SUFFIX
            # Says which way the key goes, so a hidden pane is never a mystery.
            hint += HINT_PREVIEW_HIDDEN if state["preview_hidden"] else HINT_PREVIEW_SUFFIX
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
        # An unpickable row swallows Enter outright — no exit, no redraw,
        # no message: the picker simply stays where it is.
        if not state["shown"]:
            return
        entry = entries[state["cursor"]]
        if entry.selectable and entry.pickable:
            state["result"] = (PickerAction.SELECT, entry.value)
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
            # The pane is the legend's only home: asking for it brings the
            # pane back if F12 had hidden it.
            if state["legend_open"]:
                state["preview_hidden"] = False
            state["preview_scroll"] = 0   # legend and preview scroll independently

    @kb.add("f12")
    def _(event: KeyPressEvent) -> None:
        # Hiding takes the legend with it — it lives in the pane — so the next
        # F12 brings back a preview, which is what "show the preview" means.
        state["preview_hidden"] = not state["preview_hidden"]
        if state["preview_hidden"]:
            state["legend_open"] = False
        state["preview_scroll"] = 0

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

    if allow_find:
        @kb.add("escape", "f")
        def _on_find_key(event: KeyPressEvent) -> None:
            # alt+f arrives as ESC then f. Binding it makes "escape" a PREFIX,
            # which is why the Application sets ttimeoutlen — see there.
            state["result"] = (PickerAction.FIND, None)
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
        return _accent_style(entries[state["cursor"]] if state["shown"] else None)

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
            # The whole side — divider, accent bar, pane — behind ONE
            # condition: F12 takes all three away and the list widens into the
            # space, rather than leaving a rule against a blank column.
            ConditionalContainer(
                VSplit([
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
                filter=Condition(lambda: pane_view() != PANE_HIDDEN),
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
    # How long Esc waits to see whether it is the start of alt+f, which
    # terminals send as ESC then f. An ATTRIBUTE, not a constructor argument
    # — prompt_toolkit sets it in `Application.__init__`'s body, and passing
    # it as a keyword raises TypeError before the picker can open (shipped
    # 2026-09-19, caught by the operator, whose launcher would not start).
    #
    # The default half-second is wrong here because Esc is this picker's
    # CANCEL key, and half a second of nothing after pressing it reads as a
    # hang. A real alt+f arrives as one burst with no gap, so 50 ms is
    # generous; the same trick as vim's ttimeoutlen.
    app.ttimeoutlen = ESCAPE_FLUSH_SECONDS
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
