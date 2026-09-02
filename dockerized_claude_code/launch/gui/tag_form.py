"""Every launcher form + the shared TUI style system (launch/gui).

Owns the kind-sectioned tag form (prompt_tags), the per-profession toolkit
form (edit_profiles_menu), and the generic checkbox_form primitive behind
both. Sibling menu_picker imports from here (one-way: this module never
imports the picker).

Public API:

  prompt_tags(registry, current, *, instance, workspace)
      Full-screen form sectioned by kind: a preamble naming the pre-picked
      instance + workspace (rendered as two dim comment lines), engines as a
      radio group at the top (pre-dotted from `current.engine` — callers
      pass the RESOLVED engine), then checkboxes for every profession,
      specialty, and policy, pre-checked from `current` (an AgentBuild —
      `.lego` defaults for creates, the store entry for modifies). Labels
      carry each tag's kind punctuation + field-driven coloring + its short
      description + a `(requires: …)` parenthetical; the focused row's body
      panel leads with the tag's underlined FULLNAME, then the full
      description. Checking a tag auto-checks its requirements and
      unchecking a requirement unchecks its dependents (live, in-form).
      combos.info + unmet-wants warnings render live above the confirm row.
      Policies are ordered by shortname, symbols included — `!…` then `+…`
      then `-…` — so same-stance policies group together.
      -> AgentBuild | None on cancel (Esc)

  checkbox_form(title, options, warnings=None, requires=None, wants=None,
                preamble=None)
      Generic full-screen multi-select form primitive behind prompt_tags.
      ↑↓ cycles rows (options + the confirm button; `header=True` rows are
      skipped), Space toggles (radio semantics for rows sharing a `group`),
      Enter confirms, Esc cancels. An option can render attached beneath an
      anchor option (`attached_to`) with a connector line — visual proximity
      for related options. `requires` maps option keys to prerequisite keys
      and drives the live check-cascade (see requires_closure); `wants`
      renders advisory unmet-companion messages in the warning zone
      (wants_warnings); `preamble` lines render dim under the title.
      -> list of checked option keys in display order | None on cancel

Also home to the style system both TUI surfaces share — `UiClass` (the
prompt_toolkit CSS classes + STYLE_DICT), the field-driven tag colors
(`tag_style`, `STYLE_TAG_*`, `RICH_BY_STYLE`), the display coercers
(`_normalize` / `_plain`), and `_fragment_source` (the typing-only adapter
every FormattedTextControl call site passes its fragment builder through).
menu_picker imports these from here (one-way: this module never imports the
picker).
"""

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Iterable, cast, overload

from prompt_toolkit import Application                                     # dep — declared in pyproject.toml [project]
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import AnyFormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from ..paths import toolkit_profile_path, ui_profile_path
from ..tags import (
    AgentBuild, Engine, Policy, PolicyStance, Profession, Registry, Specialty,
    Tag, ToolkitEntry,
)
from ..tags.engine import sorted_engines
from ..tags.toolkit_profile import load_profile, save_profile
from ..tags.ui_profile import load_ui_form, load_ui_profile, save_ui_profile

# ============================================================
# UI strings + layout
# ============================================================

FORM_HINT_TEXT      = "↑↓ navigate  •  Space toggle  •  Enter confirm  •  Esc cancel"
FORM_CONFIRM_LABEL  = "[ Confirm ]"
CHECKBOX_ON         = "[x] "
CHECKBOX_OFF        = "[ ] "
RADIO_ON            = "(•) "         # radio-group rows (`FormOption.group`) render round
RADIO_OFF           = "( ) "
ATTACHED_CONNECTOR  = "  └─ "        # prefix for options rendered attached beneath their anchor
TITLE_TAGS_FORM     = "Configure instance tags  (Space to toggle):"
# Shown dim under the toolkit form's title — sizes are ballpark and
# platform/time-dependent (rust/node/cmake measured, the rest estimated).
TOOLKIT_SIZE_NOTE   = "# sizes are approximate — amd64, mid-July 2026"

TITLE_HEIGHT  = 1
STATUS_HEIGHT = 2


# ============================================================
# Shared style system (used by the form AND menu_picker)
# ============================================================

class UiClass(Enum):
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


STYLE_DICT = {e.cls_name: e.style for e in UiClass}

# Tag coloring is field-driven, so adding a new tag never touches the UI:
#   warn-flagged specialty      → bold bright red (danger)
#   policy, stance=DENY         → blue       (tightens the leash)
#   policy, stance=ALLOW        → orange     (grants ability, loosens it)
#   policy, stance=DEMAND       → bold white (mandates a behavior)
#   engine                      → cyan       (a budget, not a capability/risk)
#   everything else             → bright green
STYLE_TAG_WARN       = "bold fg:ansibrightred"
STYLE_TAG_SAFE       = "fg:ansibrightgreen"
STYLE_TAG_DENY       = "bold fg:ansibrightblue"
STYLE_TAG_ALLOW      = "bold fg:#ff8700"
STYLE_TAG_DEMAND     = "bold fg:ansiwhite"
STYLE_TAG_ENGINE     = "fg:ansibrightcyan"
STYLE_UNDERLINE      = "underline"   # the fullname lead-in of description text
STYLE_TAG_INVALID    = "fg:ansiblack bg:ansired"   # a stored tag name that no longer resolves (picker Cont rows)
STYLE_LOCKED         = "fg:ansibrightblack"         # a form row the user can't toggle (grayed; e.g. [code]'s always-on Python)

_STYLE_BY_STANCE = {
    PolicyStance.ALLOW:      STYLE_TAG_ALLOW,
    PolicyStance.DENY:       STYLE_TAG_DENY,
    PolicyStance.DEMAND:     STYLE_TAG_DEMAND,
}

# Rich equivalents for the same tag styles (rich and prompt_toolkit name
# colors differently) — consumed by menu_picker's F8 legend tables.
RICH_BY_STYLE = {
    STYLE_TAG_WARN:       "bold bright_red",
    STYLE_TAG_SAFE:       "bright_green",
    STYLE_TAG_DENY:       "bold bright_blue",
    STYLE_TAG_ALLOW:      "bold #ff8700",
    STYLE_TAG_DEMAND:     "bold white",
    STYLE_TAG_ENGINE:     "bright_cyan",
}


def squashed_tag_style(style: str) -> str:
    """The chip form of a tag style: the tag's usual foreground color turned
    into the BACKGROUND, with a black glyph on top. Used wherever SQUASH_AT or
    more tags share a row and each collapses to `Tag.squash_glyph` — with the
    name gone, the color block is what still says "specialty, dangerous" or
    "policy, deny" at a glance. Derived from the style string rather than
    listed per-constant so a new tag color cannot be forgotten here."""
    color = next((token.removeprefix("fg:") for token in style.split()
                  if token.startswith("fg:")), "ansiwhite")
    return f"fg:ansiblack bg:{color}"


def tag_style(tag: Tag) -> str:
    """The style for one tag's label — dispatched on the kind-specific fields
    (duck-typed: only specialties carry `warn`, only policies carry `stance`,
    only engines carry `conf`)."""
    if getattr(tag, "warn", False):
        return STYLE_TAG_WARN
    stance = getattr(tag, "stance", None)
    if stance is not None:
        return _STYLE_BY_STANCE[stance]
    if hasattr(tag, "conf"):
        return STYLE_TAG_ENGINE
    return STYLE_TAG_SAFE


def _normalize(display: str | Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Coerce any accepted display form into a list of (style, text) tuples."""
    if isinstance(display, str):
        return [("", display)]
    return list(display)


def _plain(display: str | Iterable[tuple[str, str]]) -> str:
    """Plain-text view of a display, used for filter matching."""
    return "".join(text for _, text in _normalize(display))


def _fragment_source(build: Callable[[], list[tuple[str, str]]]) -> AnyFormattedText:
    """Hand one of our fragment builders to a prompt_toolkit FormattedTextControl.

    Purely a typing adapter — it returns `build` unchanged, so there is no
    runtime effect whatsoever. It exists because prompt_toolkit's
    `AnyFormattedText` accepts `list[tuple[str, str] | tuple[str, str,
    MouseHandler]]`, while our builders return the narrower `list[tuple[str,
    str]]`. `list` is invariant, so the two are not assignable even though every
    value we produce is valid — the mismatch is at the library boundary, not in
    our code.

    Fixing it here rather than by widening our own annotations keeps our
    contracts honest: this codebase never emits the 3-tuple mouse-handler form,
    so declaring that it might would make every fragment helper less precise.
    One cast in one place beats ten scattered suppressions."""
    return cast("AnyFormattedText", build)


# ============================================================
# Checkbox form (multi-select) — generic primitive behind prompt_tags
# ============================================================

@dataclass
class FormOption:
    """One row in `checkbox_form`. `key` is the canonical string the form
    returns when the box is checked (a tag name, for the tag form); `label`
    is the row's display (plain string or (style, text) fragments); `body`
    is the focused-row explanation shown under the list, as (style, text)
    fragments (text may contain newlines). `attached_to` names another
    option's key this row renders directly beneath, with a connector line —
    visual proximity for related options. Purely layout: no dependency
    logic — the user can check either, neither, or both.

    `header=True` makes the row a non-focusable section header — skipped by
    navigation, never checked, never returned. `group` puts the row in a
    radio group: checking it unchecks the group's other members, and a
    checked radio can't be unchecked directly (pick another instead).

    `locked=True` makes the row informational: still focusable (its `body`
    shows) and still counted in the result by its fixed `checked` state, but
    grayed out and inert to Space — the user can read it and see whether it's
    on, but can't change it. For a mandatory-and-always-present item shown so
    the user knows it's included regardless of their choices."""
    key: str
    label: str | list[tuple[str, str]]
    body: list[tuple[str, str]] = field(default_factory=list)
    checked: bool = False
    attached_to: str | None = None
    header: bool = False
    group: str | None = None
    locked: bool = False


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


def _is_word(char: str) -> bool:
    """Word characters for ctrl+arrow jumps — alphanumerics and underscore, so
    a jump in a path stops at every `/`, `-`, and `.` (component boundaries),
    the same feel as readline's default word motion."""
    return char.isalnum() or char == "_"


@dataclass
class TextField:
    """One editable text row in a form — how forms carry a name or a path
    instead of a serial input() prompt before them.

    Mutable on purpose: the app edits `value` in place while the form runs,
    with a real `cursor`: printable keys insert AT it, Backspace/Delete erase
    around it, ←/→ move it, ctrl+←/→ jump words, Home/End its extremes.
    Movement is never an edit (a form once ate a character on ← — the fix is
    that editing and motion are separate methods, wired to separate keys).
    `validate` returns an error STRING or None; while any field is invalid
    the error shows in the warning zone and confirm refuses, so a bad value
    cannot be committed, only corrected or cancelled.

    `auto` derives the value from the OTHER fields' values (e.g. a session
    name from the workspace path's basename) — live, after every edit, but
    only while this field is UNTOUCHED: the first manual EDIT in it sets
    `touched` and the derivation stops, because a user who typed a name meant
    that name (cursor motion doesn't count — looking is not typing). See
    `refresh_auto`, which also snaps the cursor to the end of any value it
    rewrites."""
    key: str
    label: str
    value: str
    validate: Callable[[str], str | None] = lambda _: None
    auto: "Callable[[dict[str, str]], str] | None" = None
    touched: bool = False
    cursor: int = -1        # -1 = "end of the initial value", resolved below

    def __post_init__(self) -> None:
        if not 0 <= self.cursor <= len(self.value):
            self.cursor = len(self.value)

    @property
    def error(self) -> str | None:
        return self.validate(self.value.strip())

    def insert(self, char: str) -> None:
        self.value = self.value[:self.cursor] + char + self.value[self.cursor:]
        self.cursor += len(char)
        self.touched = True

    def backspace(self) -> None:
        if self.cursor:
            self.value = self.value[:self.cursor - 1] + self.value[self.cursor:]
            self.cursor -= 1
        self.touched = True   # even at column 0 — reaching for erase is edit intent

    def delete(self) -> None:
        self.value = self.value[:self.cursor] + self.value[self.cursor + 1:]
        self.touched = True

    def left(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def right(self) -> None:
        self.cursor = min(len(self.value), self.cursor + 1)

    def home(self) -> None:
        self.cursor = 0

    def end(self) -> None:
        self.cursor = len(self.value)

    def word_left(self) -> None:
        i = self.cursor
        while i and not _is_word(self.value[i - 1]):
            i -= 1
        while i and _is_word(self.value[i - 1]):
            i -= 1
        self.cursor = i

    def word_right(self) -> None:
        i, size = self.cursor, len(self.value)
        while i < size and not _is_word(self.value[i]):
            i += 1
        while i < size and _is_word(self.value[i]):
            i += 1
        self.cursor = i


def field_errors(fields: list[TextField]) -> list[str]:
    """Every field's current complaint, labelled — the warning zone's content
    and the confirm gate share this one source."""
    return [f"{field.label}: {error}" for field in fields
            if (error := field.error) is not None]


def refresh_auto(fields: list[TextField]) -> None:
    """Re-derive every untouched auto field from the current values — called
    once when a form opens and after every field edit, which is what makes
    'the name follows the path until you type your own' true. A rewritten
    field's cursor snaps to its end: the user has never edited it (that is
    what untouched means), so there is no cursor position worth preserving."""
    values = {fld.key: fld.value.strip() for fld in fields}
    for fld in fields:
        if fld.auto is not None and not fld.touched:
            fld.value = fld.auto(values)
            fld.cursor = len(fld.value)


FIELD_VALUE_STYLE = "bold fg:ansibrightblue"   # field values wear the picker's agent-name blue
FIELD_END_MARK    = "▏"                        # marks the value's end; IS the cursor when it sits there
# The really-done? question a no-change confirm raises (see `confirm` in
# checkbox_form) — module-level so both forms ask with the same words.
UNCHANGED_QUESTION = "nothing was changed — really done?  (y closes · any other key stays)"


def field_row_fragments(fld: TextField, focused: bool,
                        label_width: int) -> list[tuple[str, str]]:
    """One TextField's display row: dim label column, the value in blue, the
    end-mark. Focused rows get the forms' shared treatment — every fragment
    reversed — and the character AT the cursor un-reverses (`noreverse` wins
    over the class, prompt_toolkit resolves style tokens left to right), so it
    reads as the classic block cursor punched into the highlighted row. With
    the cursor at the end, the end-mark plays that role. Trailing newline is
    the caller's."""
    label = (UiClass.STATUS.css, f"    {fld.label:<{label_width}}  ")
    if not focused:
        return [label, (FIELD_VALUE_STYLE, fld.value), ("", FIELD_END_MARK)]
    at = fld.value[fld.cursor:fld.cursor + 1]
    frags = [label, (FIELD_VALUE_STYLE, fld.value[:fld.cursor])]
    if at:
        frags += [(f"{FIELD_VALUE_STYLE} noreverse", at),
                  (FIELD_VALUE_STYLE, fld.value[fld.cursor + 1:]),
                  ("", FIELD_END_MARK)]
    else:
        frags.append(("noreverse", FIELD_END_MARK))
    return [(f"{UiClass.CURSOR.css} {style}".strip(), text)
            for style, text in frags]


def wants_warnings(checked: set[str],
                   wants: dict[str, tuple[tuple[str, str], ...]],
                   labels: dict[str, str] | None = None) -> list[tuple[str, list[str]]]:
    """(header, body) warning entries for every checked tag whose `[wants]`
    names an UNchecked tag — e.g. {auto} ticked without {firewall}. Rendered
    in the same red zone as combo warnings, live per toggle. A want never
    blocks confirmation — it's a request with a message, not a requirement.

    `labels` maps a key to its DISPLAY form and exists for one reason: the
    header must show what the user is meant to go and find. The form's rows
    are labelled `{cowork}` / `<+bash>`, so a header saying `'cowork' wants
    'free-bash'` names two things that appear nowhere on screen — the reader
    has to translate. Keys missing from the map fall back to themselves, so
    non-tag callers lose nothing by omitting it."""
    labels = labels or {}
    return [
        (f"'{labels.get(wanter, wanter)}' wants '{labels.get(wanted, wanted)}':",
         message.splitlines())
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


def _indent_fragments(frags: list[tuple[str, str]], indent: str = "  ") -> list[tuple[str, str]]:
    """Prefix every rendered line of a fragment run with `indent`, preserving
    per-fragment styles (a fragment's text may span multiple lines)."""
    out: list[tuple[str, str]] = [("", indent)]
    for style, text in frags:
        parts = text.split("\n")
        for i, part in enumerate(parts):
            if i:
                out.append(("", f"\n{indent}"))
            if part:
                out.append((style, part))
    return out


def checkbox_form(title: str, options: list[FormOption],
                  warnings: dict[frozenset[str], tuple[str, list[str]]] | None = None,
                  requires: dict[str, frozenset[str]] | None = None,
                  wants: dict[str, tuple[tuple[str, str], ...]] | None = None,
                  labels: dict[str, str] | None = None,
                  preamble: list[str] | None = None,
                  fields: list[TextField] | None = None,
                  ) -> "list[str] | tuple[dict[str, str], list[str]] | None":
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
    key isn't (see wants_warnings). Purely advisory. `labels` maps keys to
    the display form those warnings name them by — pass it when rows are
    labelled differently than they are keyed, or the warning points the
    user at a name that appears nowhere on screen.

    `preamble` lines render dim (comment-like) between the title and the
    rows — context the form was opened with (instance name, workspace).

    Rows with `header=True` render but are skipped by navigation; rows with
    a `group` behave as radios; rows with `locked=True` render grayed and
    ignore Space (see FormOption).

    `fields` (TextField rows) render ABOVE the options and are edited by
    typing while focused — Space is a literal there, a toggle on option rows;
    Backspace/Delete erase around the cursor, ←/→ move it (ctrl+←/→ by word,
    Home/End to the extremes) and never edit. Confirm additionally refuses
    while any field is invalid (the warning zone shows why), and a confirm
    that changed NOTHING asks `really done? (y/N)` first — Enter is easily
    mistaken for a field-navigation key.

    Returns the checked options' keys in display order — as
    `(field values, keys)` when `fields` were given — or None on cancel."""
    if not options:
        raise ValueError("options must be non-empty")
    rows = ordered_form_options(options)
    warning_map = warnings or {}
    req_map = requires or {}
    wants_map = wants or {}
    preamble_lines = preamble or []
    field_rows: list[TextField] = list(fields or [])
    fields_at = len(field_rows)            # option row i renders at index fields_at + i
    by_key = {o.key: o for o in rows if not o.header}
    confirm_index = fields_at + len(rows)  # the confirm button is the last navigable row
    # Navigation stops: the text fields, every non-header row, then confirm.
    stops = (list(range(fields_at))
             + [fields_at + i for i, o in enumerate(rows) if not o.header]
             + [confirm_index])
    state: dict[str, Any] = {"cursor": stops[0], "confirmed": False,
                             "asked": False}

    def focused_field() -> TextField | None:
        if state["cursor"] < fields_at:
            return field_rows[state["cursor"]]
        return None

    def cascade(toggled: FormOption) -> None:
        """Keep the checked set requires-consistent after `toggled` flips.
        Locked rows are never flipped by the cascade — their state is fixed."""
        if toggled.checked:
            for key in requires_closure(toggled.key, req_map):
                if key in by_key and not by_key[key].locked:
                    by_key[key].checked = True
        else:
            for opt in rows:
                if opt.checked and not opt.locked and toggled.key in requires_closure(opt.key, req_map):
                    opt.checked = False

    def toggle(opt: FormOption) -> None:
        """Space on a row: plain rows flip (with requires-cascade); radio rows
        check-and-exclude their group (a checked radio stays checked — pick a
        different member to move the dot). Locked rows are inert."""
        if opt.locked:
            return
        if opt.group is not None:
            if not opt.checked:
                for other in rows:
                    if other.group == opt.group:
                        other.checked = other is opt
            return
        opt.checked = not opt.checked
        cascade(opt)

    def checked_keys() -> set[str]:
        return {o.key for o in rows if o.checked and not o.header}

    def option_fragments() -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        label_width = max((len(f.label) for f in field_rows), default=0)
        for i, fld in enumerate(field_rows):
            out.extend(field_row_fragments(fld, i == state["cursor"], label_width))
            out.append(("", "\n"))
        if field_rows:
            out.append(("", "\n"))
        for i, opt in enumerate(rows):
            frags = []
            if opt.header:
                frags.extend(_normalize(opt.label))
            else:
                if opt.attached_to is not None:
                    frags.append((UiClass.STATUS.css, ATTACHED_CONNECTOR))
                if opt.group is not None:
                    frags.append(("", RADIO_ON if opt.checked else RADIO_OFF))
                else:
                    frags.append(("", CHECKBOX_ON if opt.checked else CHECKBOX_OFF))
                frags.extend(_normalize(opt.label))
            if opt.locked:   # gray the whole row — a fixed, un-toggleable entry
                frags = [(STYLE_LOCKED, text) for _, text in frags]
            if i + fields_at == state["cursor"]:
                frags = [(f"{UiClass.CURSOR.css} {style}".strip(), text)
                         for style, text in frags]
            out.extend(frags)
            out.append(("", "\n"))
        if out:
            out.pop()   # trailing newline
        return out

    def body_fragments() -> list[tuple[str, str]]:
        if not fields_at <= state["cursor"] < confirm_index:
            return []
        body = rows[state["cursor"] - fields_at].body
        return _indent_fragments(body) if body else []

    def warning_fragments() -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        checked = checked_keys()
        entries = (active_warnings(checked, warning_map)
                   + wants_warnings(checked, wants_map, labels)
                   + [(complaint, []) for complaint in field_errors(field_rows)])
        for header, body in entries:
            out.append((UiClass.WARNING.css, f"  {header}\n"))
            out.extend((UiClass.WARNING.css, f"  {line}\n") for line in body)
        if state["asked"]:
            out.append((UiClass.TITLE.css, f"  {UNCHANGED_QUESTION}\n"))
        if out:
            out[-1] = (out[-1][0], out[-1][1].rstrip("\n"))
        return out

    def confirm_fragments() -> list[tuple[str, str]]:
        style = UiClass.CURSOR.css if state["cursor"] == confirm_index else UiClass.TITLE.css
        return [("", "  "), (style, FORM_CONFIRM_LABEL)]

    def title_fragments() -> list[tuple[str, str]]:
        return [(UiClass.TITLE.css, title)]

    def preamble_fragments() -> list[tuple[str, str]]:
        return [(UiClass.STATUS.css, "\n".join(preamble_lines))]

    def hint_fragments() -> list[tuple[str, str]]:
        return [(UiClass.STATUS.css, FORM_HINT_TEXT)]

    def cursor_pos() -> Point:
        # Field rows render one line each; a blank line follows them.
        line = state["cursor"] + (1 if field_rows and state["cursor"] >= fields_at else 0)
        return Point(0, min(line, fields_at + (1 if field_rows else 0) + len(rows) - 1))

    def move(delta: int) -> None:
        i = stops.index(state["cursor"])
        state["cursor"] = stops[(i + delta) % len(stops)]

    refresh_auto(field_rows)    # the initial derivation, before any keystroke
    # The really-done? baseline, snapshotted AFTER that derivation: a confirm
    # that changed nothing against it asks before closing — some users press
    # Enter expecting field navigation. Only forms WITH fields ask (a
    # fieldless form's Enter is unambiguous, and open-look-Enter should stay
    # one keystroke there).
    baseline = (tuple(f.value for f in field_rows),
                tuple(o.checked for o in rows))

    def unchanged() -> bool:
        return baseline == (tuple(f.value for f in field_rows),
                            tuple(o.checked for o in rows))

    def confirm(event: KeyPressEvent) -> None:
        if field_errors(field_rows):
            return              # the warning zone is already explaining
        if field_rows and unchanged() and not state["asked"]:
            state["asked"] = True    # UNCHANGED_QUESTION renders; `answers` consumes the reply
            return
        state["confirmed"] = True
        event.app.exit()

    def answers(event: KeyPressEvent) -> bool:
        """While the really-done? question is up, the NEXT key is its answer
        and is CONSUMED — `y` closes, anything else stays — so an `n` cannot
        leak into a field as text. True = this key was the answer."""
        if not state["asked"]:
            return False
        state["asked"] = False
        if event.data in ("y", "Y"):
            state["confirmed"] = True
            event.app.exit()
        return True

    def field_edit(edit: Callable[[TextField], None]) -> Callable[[KeyPressEvent], None]:
        """Handler for a key that EDITS the focused field (and re-derives the
        auto fields) — a no-op anywhere else."""
        def handler(event: KeyPressEvent) -> None:
            if (fld := focused_field()) is not None:
                edit(fld)
                refresh_auto(field_rows)
        return handler

    def field_motion(motion: Callable[[TextField], None]) -> Callable[[KeyPressEvent], None]:
        """Handler for a key that MOVES the focused field's cursor — no edit,
        no auto refresh, a no-op anywhere else. Kept separate from field_edit
        so a motion key can never change text (a form once ate characters
        on ←)."""
        def handler(event: KeyPressEvent) -> None:
            if (fld := focused_field()) is not None:
                motion(fld)
        return handler

    def space(event: KeyPressEvent) -> None:
        if (fld := focused_field()) is not None:
            fld.insert(" ")
            refresh_auto(field_rows)
        elif state["cursor"] == confirm_index:
            confirm(event)
        else:
            toggle(rows[state["cursor"] - fields_at])

    def type_char(event: KeyPressEvent) -> None:
        # Printable keys type into a focused field; elsewhere they fall
        # through unused, exactly as before fields existed. Specials carry
        # escape sequences (unprintable) and are filtered out.
        if (fld := focused_field()) is not None and event.data \
                and event.data.isprintable():
            fld.insert(event.data)
            refresh_auto(field_rows)

    def cancel(event: KeyPressEvent) -> None:
        event.app.exit()

    kb = KeyBindings()

    def bind(key: Keys | str, handler: Callable[[KeyPressEvent], None]) -> None:
        """Every binding runs behind the really-done? interception, so the
        answer key — whatever it is — never doubles as its usual action."""
        def wrapped(event: KeyPressEvent) -> None:
            if not answers(event):
                handler(event)
        kb.add(key)(wrapped)

    bind("up", lambda event: move(-1))
    bind("down", lambda event: move(1))
    bind(" ", space)
    bind("backspace", field_edit(TextField.backspace))
    bind("delete", field_edit(TextField.delete))
    bind("left", field_motion(TextField.left))
    bind("right", field_motion(TextField.right))
    bind("c-left", field_motion(TextField.word_left))
    bind("c-right", field_motion(TextField.word_right))
    bind("home", field_motion(TextField.home))
    bind("end", field_motion(TextField.end))
    bind("enter", confirm)
    bind("escape", cancel)
    bind("c-c", cancel)
    bind(Keys.Any, type_char)

    header_windows = [Window(FormattedTextControl(_fragment_source(title_fragments)), height=TITLE_HEIGHT)]
    if preamble_lines:
        header_windows.append(Window(FormattedTextControl(_fragment_source(preamble_fragments)),
                                     height=len(preamble_lines)))
    body_layout = HSplit([
        *header_windows,
        Window(height=1, char=" "),
        Window(FormattedTextControl(_fragment_source(option_fragments),
                                    get_cursor_position=cursor_pos,
                                    focusable=True,
                                    show_cursor=False),
               wrap_lines=False, dont_extend_height=True),
        Window(height=1, char=" "),
        Window(FormattedTextControl(_fragment_source(body_fragments)), wrap_lines=True),   # flexible filler — focused option's explanation
        Window(FormattedTextControl(_fragment_source(warning_fragments)), wrap_lines=True, dont_extend_height=True),
        Window(height=1, char=" "),
        Window(FormattedTextControl(_fragment_source(confirm_fragments)), height=1),
        Window(FormattedTextControl(_fragment_source(hint_fragments)), height=STATUS_HEIGHT),
    ])

    Application(
        layout=Layout(body_layout),
        key_bindings=kb,
        style=Style.from_dict(STYLE_DICT),
        full_screen=True,
    ).run()

    if not state["confirmed"]:
        return None
    keys = [o.key for o in rows if o.checked]
    if fields is None:
        return keys
    return {f.key: f.value.strip() for f in field_rows}, keys


# ============================================================
# The tag form — registry → FormOptions → AgentBuild
# ============================================================

def _tag_row(tag: Tag, checked: bool, group: str | None = None) -> FormOption:
    """One selectable form row: colored kind-punctuated label + the tag's
    short description, a dim `(requires: …)` parenthetical when it has
    prerequisites, and the full description as the focused-row body — led by
    the tag's underlined FULLNAME, so the abbreviation in the label is never
    a puzzle (`{dood}` focuses to `Docker-outside-of-Docker: can run …`).
    Keys are the tags' full names (what `.lego` / instances.toml store).

    An always-on tag (a static policy like `<-su>`) renders locked: grayed,
    checked, inert to Space, with an `(always-on)` marker — the user sees
    it applies but can't change it (prompt_tags also filters it out of the
    returned build; it's never persisted)."""
    always_on = getattr(tag, "always_on", False)
    label: list[tuple[str, str]] = [(tag_style(tag), tag.label), ("", " ")]
    label.append(("", tag.short_description))
    if always_on:
        label.append((UiClass.STATUS.css, "  (always-on)"))
    if tag.requires:
        label.append((UiClass.STATUS.css, f"  (requires: {', '.join(sorted(tag.requires))})"))
    return FormOption(
        key=tag.name,
        label=label,
        body=[(STYLE_UNDERLINE, tag.fullname), ("", f": {tag.full_description}")],
        checked=True if always_on else checked,
        group=group,
        locked=always_on,
    )


def _tag_form_options(registry: Registry, current: AgentBuild, *,
                      engines: bool = True,
                      locked: frozenset[str] = frozenset(),
                      ) -> list[FormOption]:
    """The full sectioned form: one header per kind (its nutshell), engines
    as a radio group at the top (pre-dotted from `current.engine` — the
    caller passes the RESOLVED engine, so the dot shows what would actually
    run), then professions / specialties / policies as checkboxes pre-checked
    from `current`'s axis lists. Policies are ordered by shortname WITH its
    leading symbol (`!` < `+` < `-` in ASCII), so same-stance policies sit
    together: demands, then grants, then denials.

    `engines=False` drops that whole section — the CLUSTER-level form, where
    per-member thinking budgets have no meaning. `locked` names tags that
    render checked-and-inert (the treatment an `always_on` policy gets):
    the cluster form locks {mux}/{clstr}, and a MEMBER's form locks whatever
    its cluster already imposes, so a member can see what applies to it
    without being able to opt out."""
    checked = {*current.professions, *current.specialties, *current.policies}

    def header(kind_cls: type[Tag]) -> FormOption:
        return FormOption(
            key=f"#{kind_cls.root}",
            label=[(UiClass.TITLE.css, f"{kind_cls.root.upper()} — {kind_cls.nutshell}")],
            header=True,
        )

    out: list[FormOption] = []
    if engines:
        out.append(header(Engine))
        out += [_tag_row(tag, checked=(tag.name == current.engine), group="engine")
                for tag in sorted_engines(registry.engines.values())]
    for kind_cls, members in ((Profession, list(registry.professions.values())),
                              (Specialty, list(registry.specialties.values())),
                              (Policy, sorted(registry.policies.values(),
                                              key=lambda p: p.shortname))):
        out.append(header(kind_cls))
        for tag in members:
            row = _tag_row(tag, checked=tag.name in checked or tag.name in locked)
            out.append(replace(row, locked=True) if tag.name in locked else row)
    return out


def prompt_cluster_tags(registry: Registry, current: AgentBuild, *,
                        session: str, locked: frozenset[str],
                        ) -> "AgentBuild | None":
    """The CLUSTER-level tag form — step one of creating or editing a cluster
    (operator request, 2026-09-02: set `{cc}` once for the cluster instead of
    once per member). Returns the cluster's tag set, or None on Esc.

    Three differences from the instance form, all deliberate: no ENGINE
    section (a thinking budget is per member), `locked` rows for the tags
    that make a cluster a cluster ({mux}/{clstr} — checked and inert, the
    `always_on` treatment), and a preamble that says plainly what the
    selection does, because "these tags are forced on every member" is not
    something a tag list can imply on its own."""
    options = _tag_form_options(registry, current, engines=False, locked=locked)
    result = checkbox_form(
        f"Cluster tags for '{session}'  (Space to toggle):", options,
        warnings=_combo_warnings(registry),
        requires=_form_requires(registry),
        wants=_form_wants(registry),
        labels=_form_labels(registry),
        preamble=["# EVERY member of this cluster is FORCED to carry the tags",
                  "# selected here — and only these are set cluster-wide.",
                  "# Per-member tags (and each member's engine) stay on the",
                  "# member rows: pick a member in the picker and press F2.",
                  f"# {', '.join(sorted(locked))} cannot be unticked: they are",
                  "# what makes this a cluster."])
    if result is None:
        return None
    picked = set(cast("list[str]", result))
    # No engine axis, and always-on policies stay out of the build exactly as
    # in prompt_tags (they apply unconditionally and are never persisted).
    return AgentBuild(
        engine=None,
        professions=tuple(n for n in registry.professions if n in picked),
        specialties=tuple(n for n in registry.specialties if n in picked),
        policies=tuple(n for n, p in registry.policies.items()
                       if n in picked and not p.always_on))


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


def _form_labels(registry: Registry) -> dict[str, str]:
    """{tag name: punctuated label} for EVERY tag, all four kinds. The wants
    zone displays through this map so its header shows what the rows show —
    `'{cowork}' wants '<+bash>'` — rather than bare manifest names the user
    would have to translate. All kinds, not just the form's three: a want may
    point at any real tag."""
    return {tag.name: tag.label for tag in registry.get_all()}


@overload
def prompt_tags(registry: Registry, current: AgentBuild, *,
                instance: str, workspace: str | None = None,
                locked: frozenset[str] = frozenset(),
                fields: None = None) -> "AgentBuild | None": ...
@overload
def prompt_tags(registry: Registry, current: AgentBuild, *,
                instance: str, workspace: str | None = None,
                locked: frozenset[str] = frozenset(),
                fields: list[TextField],
                ) -> "tuple[dict[str, str], AgentBuild] | None": ...
def prompt_tags(registry: Registry, current: AgentBuild, *,
                instance: str, workspace: str | None = None,
                locked: frozenset[str] = frozenset(),
                fields: list[TextField] | None = None,
                ) -> "AgentBuild | tuple[dict[str, str], AgentBuild] | None":
    """Run the tag form (see the module docstring for the full behavior) and
    return the selection as a new AgentBuild — as `(field values, build)` when
    `fields` were given — or None when the user cancels (Esc); callers abort
    their create / modify flow on None rather than persisting anything.

    Two shapes, one form: WITHOUT fields, `instance` + `workspace` are
    already-answered prompts echoed as the preamble (the member-tag-edit call
    site). WITH fields, the workspace/name ARE the fields — no terminal
    prompt precedes the form — and the preamble only names the agent."""
    options = _tag_form_options(registry, current, locked=locked)
    preamble = ([f"# agent:  {instance}"] if fields is not None
                else [f"# instance:  {instance}",
                      f"# workspace: {workspace}"])
    result = checkbox_form(TITLE_TAGS_FORM, options,
                           warnings=_combo_warnings(registry),
                           requires=_form_requires(registry),
                           wants=_form_wants(registry),
                           labels=_form_labels(registry),
                           preamble=preamble,
                           fields=fields)
    if result is None:
        return None
    values: dict[str, str] = {}
    if fields is not None:
        values, keys = cast("tuple[dict[str, str], list[str]]", result)
    else:
        keys = cast("list[str]", result)
    picked = set(keys)
    # Always-on (static) tags come back checked — they're locked rows — but
    # are never part of the build: applied unconditionally, never persisted.
    build = AgentBuild(
        engine=next((n for n in registry.engines if n in picked), current.engine),
        professions=tuple(n for n in registry.professions if n in picked),
        specialties=tuple(n for n in registry.specialties if n in picked),
        policies=tuple(n for n, p in registry.policies.items()
                       if n in picked and not p.always_on),
    )
    return build if fields is None else (values, build)

def _toolkit_size_text(entry: ToolkitEntry) -> str:
    """The size column for a toolkit row: `~NNNMb` for an install, `included`
    for a locked entry (in the base image, no added footprint)."""
    return "included" if entry.locked else f"~{entry.approx_size_mb}MB"


def _toolkit_form_options(entries: dict[str, ToolkitEntry], profile: dict[str, bool]) -> list[FormOption]:
    """One `FormOption` per `template.form` entry, key-sorted. Toggleable rows
    are checked from the current profile (a key the profile doesn't mention
    yet — a tool added to the manifest after the profile was written — falls
    back to the entry's own `default`, matching `load_profile`'s
    reconciliation); locked rows show their fixed `default` state, grayed and
    un-toggleable. Key and size columns are padded so the `—` separators
    align down the form. The focused row's body panel carries the flavor:
    how you run the tool + what kind of language it is. Callers guard
    non-empty `entries`."""
    sizes = {key: _toolkit_size_text(entry) for key, entry in entries.items()}
    key_width = max(len(key) for key in entries)
    size_width = max(len(size) for size in sizes.values())
    out: list[FormOption] = []
    for key, entry in sorted(entries.items()):
        checked = entry.default if entry.locked else profile.get(key, entry.default)
        out.append(FormOption(
            key=key,
            label=[("", f"{key:<{key_width}} — {sizes[key]:<{size_width}} — {entry.description}")],
            body=[("", "run with "), (STYLE_UNDERLINE, entry.run_command),
                  ("", f"   ·   {entry.language}")],
            checked=checked,
            locked=entry.locked,
        ))
    return out


@dataclass(frozen=True)
class ProfileSection:
    """One profile-backed slice of the merged preferences form: its
    comment-title, its rows, and where its checked set lands on confirm —
    each section saves to its OWN file, which is what lets the one form front
    any number of them."""
    title: str
    options: list[FormOption]
    save: Callable[[set[str]], None]


def _toolkit_section(profession: Profession) -> ProfileSection | None:
    """The toolkit slice for one configurable profession — None without a
    template.form. Saves to that profession's own profile; only the
    toggleable rows' states persist (locked rows, e.g. Python, carry no
    toggle)."""
    entries = profession.load_toolkit()
    if not entries:
        return None
    path = toolkit_profile_path(profession.name)
    current = load_profile(path, entries)   # toggleable keys only
    return ProfileSection(
        title=f"Edit {profession.label} toolkit  (Space to toggle):",
        options=_toolkit_form_options(entries, current),
        save=lambda checked: save_profile(
            path, {key: key in checked for key in current}, entries),
    )


def _ui_section() -> ProfileSection:
    """The launcher-UI slice — `settings/ui.form` rendered over
    `ui_profile.toml`. ALWAYS present, unlike the toolkit slices: its
    preferences (the muxer backend) are profession-independent, which is why
    the picker row that opens this form no longer hides when no profession is
    configurable."""
    entries = load_ui_form()
    path = ui_profile_path()
    current = load_ui_profile(path, entries)
    return ProfileSection(
        title="Edit UI configs  (Space to toggle):",
        options=[FormOption(key=key,
                            label=[("", entry.description)],
                            body=[("", entry.body)],
                            checked=current.get(key, entry.default))
                 for key, entry in sorted(entries.items())],
        save=lambda checked: save_ui_profile(
            path, {key: key in checked for key in current}, entries),
    )


def edit_profiles_form(sections: list[ProfileSection],
                       preamble: list[str] | None = None) -> None:
    """The middle-handler the profiles menu rides: concatenate ANY number of
    sections into ONE checkbox form — the first section's title is the form's
    title, every later one becomes a `header=True` row — then fan the
    confirmed set back out, each section saving to its own file. Esc saves
    nothing anywhere. Option keys are namespaced per section internally, so
    two files may reuse a name without colliding (`attached_to` is not
    remapped — no profile row uses it)."""
    merged: list[FormOption] = []
    for index, section in enumerate(sections):
        if index:
            # The next section's title lands three newlines after the
            # previous section's last row (operator's spec): two blank
            # header rows — skipped by navigation like any header — then
            # the title row itself.
            merged += [FormOption(key=f"#gap{index}-{blank}", label="",
                                  header=True)
                       for blank in range(2)]
            merged.append(FormOption(
                key=f"#section{index}",
                label=[(UiClass.TITLE.css, section.title)],
                header=True))
        merged += [replace(option, key=f"{index}:{option.key}")
                   for option in section.options]
    result = checkbox_form(sections[0].title, merged, preamble=preamble)
    if result is None:   # Esc — cancel, no file touched
        return
    checked = set(result)
    for index, section in enumerate(sections):
        section.save({option.key for option in section.options
                      if f"{index}:{option.key}" in checked})


def edit_profiles_menu(registry: Registry) -> None:
    """Open the ONE merged preferences form: a toolkit section per
    configurable profession (today: [code] alone) plus the always-present UI
    section. The size-ballpark preamble only rides along when a toolkit
    section shows sizes."""
    sections = [section for profession in registry.professions.values()
                if (section := _toolkit_section(profession)) is not None]
    preamble = [TOOLKIT_SIZE_NOTE] if sections else None
    sections.append(_ui_section())
    edit_profiles_form(sections, preamble=preamble)
