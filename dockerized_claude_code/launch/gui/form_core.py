"""The generic full-screen form — every piece both concrete forms share
(launch/gui).

Extracted from tag_form 2026-09-03, when the second form (cluster
membership) was found to have grown its OWN copy of half of this. What lives
here is the machinery; what a given form ASKS lives in forms.py or
cluster_form.py:

  FormOption / ordered_form_options   the row model, incl. `attached_to`
                                      ordering, `header`, `group` (radio),
                                      `locked`
  TextField (+ field_row_fragments / field_errors / refresh_auto)
                                      editable text rows above the options
  ConfirmGate / confirm_gate          the confirm + really-done? state
                                      machine BOTH forms run on — the words,
                                      the rules, and the question fragment
  answer_first_bind / header_windows / confirm_row_fragments
                                      the rest of the shared scaffold
  active_warnings / wants_warnings / requires_closure
                                      the warning zone's pure logic
  FormBody / run_form                 the scaffold every form RUNS ON: the
                                      text fields, the cursor, the key map,
                                      the confirm gate, the warning window
                                      and the layout — defined once
  checkbox_form                       the multi-select primitive: a row
                                      model + one Space action on that
                                      scaffold

Placement rule this module exists to enforce: a form must not restate any of
the above. The copies drifted twice — first the really-done? message rendered
at the bottom of one form and the top of the other (2026-09-02), then the
cluster form's "no members" complaint top-aligned while its field complaints
hugged the button, and its cursor sat one line above the highlighted row
(2026-09-09). Since then the forms share `run_form` and cannot restate the
layout at all; `TestFormPlacementParity` pins where complaints render and
where the cursor sits, for both forms at once.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from prompt_toolkit import Application                                     # dep — declared in pyproject.toml [project]
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from .styles import (
    STATUS_HEIGHT, STYLE_AGENT_NAME, STYLE_DICT, STYLE_LOCKED, TITLE_HEIGHT,
    UiClass, _fragment_source, _normalize,
)

# ============================================================
# UI strings + layout
# ============================================================

FORM_HINT_TEXT      = "↑↓ navigate  •  Space toggle  •  Enter confirm  •  Esc cancel"
FORM_CHOICE_HINT    = "↑↓ navigate  •  Space toggle  •  ←→ choose  •  Enter confirm  •  Esc cancel"   # a form with a choices row
FORM_CONFIRM_LABEL  = "[ Confirm ]"
CHECKBOX_ON         = "[x] "
CHECKBOX_OFF        = "[ ] "
RADIO_ON            = "(•) "         # radio-group rows (`FormOption.group`) render round
RADIO_OFF           = "( ) "
BULLET_ON           = "● "           # a radio ATTACHED under another row — a sub-choice, like an AI's models — renders as a bullet
BULLET_OFF          = "○ "
ATTACHED_CONNECTOR  = "  └─ "        # prefix for options rendered attached beneath their anchor
TITLE_TAGS_FORM     = "Configure instance tags  (Space to toggle):"
# Shown dim under the toolkit form's title — sizes are ballpark and
# platform/time-dependent (rust/node/cmake measured, the rest estimated).
TOOLKIT_SIZE_NOTE   = "# sizes are approximate — amd64, mid-July 2026"

# TITLE_HEIGHT / STATUS_HEIGHT come from `styles` — the picker reserves the
# same chrome, so the count is defined once there.



# ============================================================
# Checkbox form (multi-select) — generic primitive behind prompt_tags
# ============================================================

@dataclass(frozen=True)
class FormResult:
    """What a CONFIRMED `checkbox_form` hands back: the keys of the checked
    options in display order, and the text fields' values (an empty dict for
    a form that carried no fields). Cancel is None, and nothing else.

    One record instead of the `list[str] | tuple[dict[str, str], list[str]]`
    union this returned until 2026-09-18 — which is a correctness fix, not
    tidiness. Each of the four call sites narrowed that union its own way,
    and two of them narrowed SILENTLY WRONG the day their form gains a
    field: `edit_profiles_form`'s bare `set(result)` would iterate the
    2-tuple and raise on the unhashable dict, and `--stop`'s
    `isinstance(result, list)` would simply go False and stop NOTHING, with
    no error and no message (bug-investigator, strict-reviewer; gate
    stop-form-reuse). A field is one keyword argument away at every call
    site, so the union made that a permanent possibility.

    Deliberately NOT iterable, sized or truthy: `__iter__`, `__len__` or
    `__bool__` here would re-create the very "what shape is it?" guessing
    one layer down. Read `.checked` or `.field_values` by name.

    `field_values` rather than `values`, `checked` rather than `keys`: the
    dict spellings read as bound methods at a glance on a record four call
    sites share, and `.fields` would read as the TextField objects instead
    of the text typed into them."""
    checked: list[str]
    field_values: dict[str, str] = field(default_factory=dict)
    choices: dict[str, str | None] = field(default_factory=dict)   # each choices row's pick, by key (None: its first, unset position)


@dataclass
class FormOption:
    """One row in `checkbox_form`. `key` is the canonical string the form
    returns when the box is checked (a tag name, for the tag form); `label`
    is the row's display (plain string or (style, text) fragments); `body`
    is the focused-row explanation shown under the list, as (style, text)
    fragments (text may contain newlines). `attached_to` names another
    option's key this row renders directly beneath, with a connector line —
    visual proximity for related options. Purely layout: no dependency
    logic — the user can check either, neither, or both. `folds_with_anchor`
    adds one visibility rule to it: the row renders, and takes the cursor,
    only while that anchor row is checked — the dotted AI's models open under
    it, every other AI's fold away.

    `header=True` makes the row a non-focusable section header — skipped by
    navigation, never checked, never returned. `group` puts the row in a
    radio group: checking it unchecks the group's other members, and a
    checked radio can't be unchecked directly (pick another instead). A group
    is never optional: the one this form had (an AI's models, 2026-09-28)
    became "exactly one, always" the same week, at the operator's word — a
    group that may be empty is a checkbox's job. An attached radio draws as
    a bullet (●/○), a sub-choice under its anchor.

    `label` may be a FUNCTION of the checked keys, for a row whose words
    follow the form's state (the follow-the-engine bullet names the model the
    dotted engine means). `choices` makes the row a HORIZONTAL pick instead
    of a box: a function of the checked keys returning (value, text) pairs,
    drawn side by side as bullets, `choice` the picked value — None until
    ←/→ moves it, and again whenever the picked value leaves the list.
    `choice_default` (a function of the checked keys) names the value None
    stands for: it is drawn dotted then, ←/→ start from it, and landing back
    on it returns the pick to None — no separate "default" position
    (operator, 2026-09-29). Such a row is never checked; its pick comes back
    in `FormResult.choices`. With no pairs it is not shown at all, so a pick
    with nothing to pick never takes the cursor.

    `locked=True` makes the row informational: still focusable (its `body`
    shows) and still counted in the result by its fixed `checked` state, but
    grayed out and inert to Space — the user can read it and see whether it's
    on, but can't change it. For a mandatory-and-always-present item shown so
    the user knows it's included regardless of their choices."""
    key: str
    label: str | list[tuple[str, str]] | Callable[[frozenset[str]], list[tuple[str, str]]]
    body: list[tuple[str, str]] = field(default_factory=list)
    checked: bool = False
    attached_to: str | None = None
    header: bool = False
    group: str | None = None
    folds_with_anchor: bool = False
    locked: bool = False
    choices: Callable[[frozenset[str]], list[tuple[str | None, str]]] | None = None
    choice: str | None = None
    choice_default: Callable[[frozenset[str]], str | None] | None = None


def ordered_form_options(options: list[FormOption]) -> list[FormOption]:
    """Display order for form rows: anchor options keep their given order;
    each attached option is re-inserted directly after its anchor, and ITS
    attachments after it, recursively (several attachments to one anchor
    keep their given relative order) — an AI, its model bullets, the effort
    row under a bullet. Options attached to an unknown key are appended at
    the end unattached-style — better a detached row than a vanished one —
    with their own attachments after them, as under any anchor. A
    CYCLE of attachments is a ValueError: the rows are code, so a loop is a
    programming error, and it must not reach a render (bug-investigator,
    gate effort-row)."""
    by_key = {o.key: o for o in options}
    for option in options:
        seen = {option.key}
        anchor = option.attached_to
        while anchor is not None and anchor in by_key:
            if anchor in seen:
                raise ValueError(f"form rows attached in a cycle through {option.key!r}")
            seen.add(anchor)
            anchor = by_key[anchor].attached_to
    out: list[FormOption] = []

    def place(option: FormOption) -> None:
        out.append(option)
        for child in options:
            if child.attached_to == option.key:
                place(child)

    for option in options:
        if option.attached_to is None:
            place(option)
    for option in options:
        if option.attached_to is not None and option.attached_to not in by_key:
            place(option)
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


FIELD_END_MARK    = "▏"                        # marks the value's end; IS the cursor when it sits there
# The really-done? question a no-change confirm raises — see `confirm_gate`,
# which both forms drive, so they ask with the same words AND the same rules.
UNCHANGED_QUESTION = "nothing was changed — really done?  (y closes · any other key stays)"


@dataclass(frozen=True)
class ConfirmGate:
    """The confirm/really-done? state machine BOTH full-screen forms run on
    (the tag form's `checkbox_form` and the cluster form's `prompt_members`).

    It existed twice, line-for-line, until 2026-09-02 — and the copies had
    already drifted in where they rendered the question: one hugged the
    confirm button, the other floated under the options, because each host
    embedded it in a different window. Now the words, the rules AND the
    fragment live here; a host only has to render `question()` in the
    bottom-hugging window it already has for warnings.

    The rules, one place: a confirm is refused outright while `ready()` is
    False (the host's warning zone is already explaining why); a confirm that
    changed nothing asks first, and the NEXT key is its answer, consumed so
    an `n` cannot leak into a text field; a form with nothing to type in
    never asks (`asks_when_unchanged=False`), because Enter there cannot be
    the accident the question guards against."""
    confirm: Callable[[KeyPressEvent], None]
    answers: Callable[[KeyPressEvent], bool]
    question: Callable[[], list[tuple[str, str]]]
    confirmed: Callable[[], bool]


def confirm_gate(*, snapshot: Callable[[], Any], ready: Callable[[], bool],
                 asks_when_unchanged: bool) -> ConfirmGate:
    """Build a ConfirmGate. `snapshot()` must return a comparable value
    describing everything the user could have changed — the baseline is taken
    HERE, at build time, so a host must build the gate after its initial
    derivations (auto-filled fields) have run."""
    baseline = snapshot()
    state = {"asked": False, "confirmed": False}

    def confirm(event: KeyPressEvent) -> None:
        if not ready():
            return
        if asks_when_unchanged and snapshot() == baseline and not state["asked"]:
            state["asked"] = True      # question() renders; answers() consumes the reply
            return
        state["confirmed"] = True
        event.app.exit()

    def answers(event: KeyPressEvent) -> bool:
        """True = this key was the question's answer (and is consumed)."""
        if not state["asked"]:
            return False
        state["asked"] = False
        if event.data in ("y", "Y"):
            state["confirmed"] = True
            event.app.exit()
        return True

    def question() -> list[tuple[str, str]]:
        if not state["asked"]:
            return []
        return [(UiClass.TITLE.css, f"  {UNCHANGED_QUESTION}")]

    return ConfirmGate(confirm=confirm, answers=answers, question=question,
                       confirmed=lambda: state["confirmed"])


def answer_first_bind(kb: KeyBindings, gate: ConfirmGate,
                      ) -> Callable[[Keys | str, Callable[[KeyPressEvent], None]], None]:
    """`bind(key, handler)` for a form whose keys must reach the gate FIRST:
    while the really-done? question is up, the next key answers it instead of
    doing its usual job. Both forms bind every key through this."""
    def bind(key: Keys | str,
             handler: Callable[[KeyPressEvent], None]) -> None:
        def wrapped(event: KeyPressEvent) -> None:
            if not gate.answers(event):
                handler(event)
        kb.add(key)(wrapped)
    return bind


def header_windows(title: str, preamble_lines: list[str]) -> list[Window]:
    """The title row plus the dim comment-block both forms open with."""
    out = [Window(FormattedTextControl(
        _fragment_source(lambda: [(UiClass.TITLE.css, title)])),
        height=TITLE_HEIGHT)]
    if preamble_lines:
        out.append(Window(FormattedTextControl(_fragment_source(
            lambda: [(UiClass.STATUS.css, "\n".join(preamble_lines))])),
            height=len(preamble_lines)))
    return out


def confirm_row_fragments(label: str, focused: bool) -> list[tuple[str, str]]:
    """The confirm button's row — reversed while the cursor sits on it."""
    style = UiClass.CURSOR.css if focused else UiClass.TITLE.css
    return [("", "  "), (style, label)]


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
        return [label, (STYLE_AGENT_NAME, fld.value), ("", FIELD_END_MARK)]
    at = fld.value[fld.cursor:fld.cursor + 1]
    frags = [label, (STYLE_AGENT_NAME, fld.value[:fld.cursor])]
    if at:
        frags += [(f"{STYLE_AGENT_NAME} noreverse", at),
                  (STYLE_AGENT_NAME, fld.value[fld.cursor + 1:]),
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


# ============================================================
# The scaffold — what EVERY full-screen form runs on
# ============================================================

# What a key does on a FOCUSED TEXT FIELD, whichever form the field belongs
# to. Edits re-derive the auto fields; motions never touch text — a form once
# ate a character on ←, which is why the two tables are separate.
_FIELD_EDITS: dict[str, Callable[[TextField], None]] = {
    "backspace": TextField.backspace,
    "delete":    TextField.delete,
}
_FIELD_MOTIONS: dict[str, Callable[[TextField], None]] = {
    "left":    TextField.left,
    "right":   TextField.right,
    "c-left":  TextField.word_left,
    "c-right": TextField.word_right,
    "home":    TextField.home,
    "end":     TextField.end,
}
# Prefixed to a form's hint whenever it carries text fields — one wording for
# both forms (the membership form said it, the tag form never did).
FIELDS_HINT_PREFIX = "type into the focused field  •  "


@dataclass(frozen=True)
class FormBody:
    """What a concrete form hands `run_form`: its rows, and what its keys do
    to them. Everything else — the text fields above the rows, the cursor, the
    confirm gate, the warning window, the layout — is the scaffold's, defined
    once for every form.

      rows       the current fragments of every row, with NO cursor highlight
                 (the scaffold applies it to whichever row is focused)
      stops      the row indices the cursor may land on (a header is skipped)
      actions    key → what it does to the focused ROW, by row index. A form
                 declares only the ROW half of each key: on a focused field
                 the same key keeps its text meaning (Space types a space, ←
                 moves the cursor), and on the button Space confirms.
      filler     the flexible middle window, given the focused row's index
                 (None on a field or the button): the focused option's
                 explanation, the membership preview
      warnings   the form's own complaints, as lines; the scaffold renders
                 them, the field errors and the really-done? question in the
                 ONE bottom-hugging warning window
      snapshot   everything the user can change in the rows (the scaffold adds
                 the field values) — the really-done? baseline
      ready      an extra confirm gate beyond "no field is invalid"
      visible    whether row i renders NOW: a hidden row draws no line and
                 takes no cursor (a row folded under an unchecked anchor)
      choose     ←/→ on row i: True when the ROW took the key (a horizontal
                 pick moved), and then no action of the form's runs — the
                 row's kind decides before the form's own map, so a form
                 whose ← removes something can never do so on a pick
                 (strict-reviewer, gate model-picker-3)
    """
    rows: Callable[[], list[list[tuple[str, str]]]]
    stops: Sequence[int]
    actions: Mapping[str, Callable[[int], None]]
    filler: Callable[[int | None], list[tuple[str, str]]]
    warnings: Callable[[], list[str]]
    confirm_label: str
    hint: str
    snapshot: Callable[[], Any]
    ready: Callable[[], bool] = lambda: True
    visible: Callable[[int], bool] = lambda index: True
    choose: Callable[[int, int], bool] = lambda index, delta: False


# ←/→ as a row's own keys (FormBody.choose): the step each moves a pick by.
_CHOOSE_KEYS = {"left": -1, "right": 1}


def run_form(title: str, preamble: list[str] | None, fields: list[TextField] | None,
             body: FormBody) -> dict[str, str] | None:
    """Render a full-screen form; block until confirm or cancel. Returns the
    field values (an empty dict for a fieldless form), or None on Esc/Ctrl-C.

    THE definition of how a launcher form behaves. ↑↓ cycle the stops — the
    fields, then the body's rows, then the [ Confirm ] button, wrapping.
    Typing edits a focused field: Space is a literal there, Backspace/Delete
    erase around the cursor, ←/→ move it (ctrl+←/→ by word, Home/End to the
    extremes) and never edit. On a row the body's `actions` decide what a key
    does; on the button Space confirms. Enter confirms from anywhere, refused
    while a field is invalid or `body.ready()` says no (the warning window is
    already explaining why), and a confirm that changed nothing asks
    `really done?` first (see `confirm_gate`; only forms with fields ask).

    Header, fields+rows, the flexible filler, the warnings, the confirm row and
    the hint stack in that order, the warnings in a `dont_extend_height`
    window hugging the button. Two hand-rolled copies of this stack drifted
    twice before it was defined here once (module docstring)."""
    preamble_lines = preamble or []
    field_rows: list[TextField] = list(fields or [])
    fields_at = len(field_rows)                       # body row i renders at index fields_at + i
    row_count = len(body.rows())
    confirm_index = fields_at + row_count             # the confirm button is the last navigable row
    stops = ([*range(fields_at)]
             + [fields_at + i for i in body.stops]
             + [confirm_index])
    state: dict[str, Any] = {"cursor": stops[0]}

    def shown(stop: int) -> bool:
        """Whether a stop is on screen now: a field and the button always
        are, a row while `body.visible` says so."""
        return not fields_at <= stop < confirm_index or body.visible(stop - fields_at)

    def reconcile() -> None:
        """Keep the cursor on a shown stop. One whose row just folded away —
        a model bullet, when its AI's dot moved — goes to the nearest shown
        stop above it, which for an attached row is its anchor."""
        if shown(state["cursor"]):
            return
        position = stops.index(state["cursor"])
        state["cursor"] = next((s for s in reversed(stops[:position]) if shown(s)),
                               next(s for s in stops if shown(s)))

    def focused_field() -> TextField | None:
        if state["cursor"] < fields_at:
            return field_rows[state["cursor"]]
        return None

    def focused_row() -> int | None:
        index = state["cursor"] - fields_at
        return index if 0 <= index < row_count else None

    def option_fragments() -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        label_width = max((len(f.label) for f in field_rows), default=0)
        for i, fld in enumerate(field_rows):
            out.extend(field_row_fragments(fld, i == state["cursor"], label_width))
            out.append(("", "\n"))
        if field_rows:
            out.append(("", "\n"))    # one blank line between fields and rows — cursor_pos counts it
        for i, frags in enumerate(body.rows()):
            if not body.visible(i):
                continue                   # folded: no line at all
            if i + fields_at == state["cursor"]:
                frags = [(f"{UiClass.CURSOR.css} {style}".strip(), text)
                         for style, text in frags]
            out.extend(frags)
            out.append(("", "\n"))
        if out:
            out.pop()   # trailing newline
        return out

    def filler_fragments() -> list[tuple[str, str]]:
        return body.filler(focused_row())

    def warning_fragments() -> list[tuple[str, str]]:
        lines = [*body.warnings(), *field_errors(field_rows)]
        out = [(UiClass.WARNING.css, f"  {line}\n") for line in lines]
        out += [(style, text + "\n") for style, text in gate.question()]
        if out:
            out[-1] = (out[-1][0], out[-1][1].rstrip("\n"))
        return out

    def confirm_fragments() -> list[tuple[str, str]]:
        return confirm_row_fragments(body.confirm_label,
                                     state["cursor"] == confirm_index)

    hint = (FIELDS_HINT_PREFIX if field_rows else "") + body.hint

    def cursor_pos() -> Point:
        # COUNTED as option_fragments renders: a line per field, the blank
        # separator after them, then a line per SHOWN row. Arithmetic on the
        # cursor's index was right only while every row drew one line — gate
        # gui-dedup's off-by-one was that same offset, one separator short —
        # and a folded row draws none.
        head = fields_at + (1 if field_rows else 0)
        shown_rows = [i for i in range(row_count) if body.visible(i)]
        cursor = state["cursor"]
        line = cursor if cursor < fields_at else head + sum(1 for i in shown_rows if i < cursor - fields_at)
        last = head + len(shown_rows) - 1
        return Point(0, max(0, min(line, last)))

    def move(delta: int) -> None:
        """The next SHOWN stop in `delta`'s direction, wrapping."""
        position = stops.index(state["cursor"])
        for step in range(1, len(stops) + 1):
            candidate = stops[(position + delta * step) % len(stops)]
            if shown(candidate):
                state["cursor"] = candidate
                return

    reconcile()                 # the first stop may be a folded row
    refresh_auto(field_rows)    # the initial derivation, before any keystroke
    # Built AFTER that derivation so the baseline includes the auto-filled
    # values. Only forms WITH fields ask: a fieldless form's Enter is
    # unambiguous, and open-look-Enter should stay one keystroke there.
    gate = confirm_gate(
        snapshot=lambda: (tuple(f.value for f in field_rows), body.snapshot()),
        ready=lambda: not field_errors(field_rows) and body.ready(),
        asks_when_unchanged=bool(field_rows))

    def keyed(key: str) -> Callable[[KeyPressEvent], None]:
        """One handler per bound key: its FIELD meaning while a field is
        focused, Space-confirms on the button, else the body's ROW action."""
        def handler(event: KeyPressEvent) -> None:
            if (fld := focused_field()) is not None:
                if key in _FIELD_EDITS:
                    _FIELD_EDITS[key](fld)
                    refresh_auto(field_rows)
                elif key in _FIELD_MOTIONS:
                    _FIELD_MOTIONS[key](fld)
                elif event.data and event.data.isprintable():   # Space, +, - … are literals in a field
                    fld.insert(event.data)
                    refresh_auto(field_rows)
                return
            if state["cursor"] == confirm_index:
                if key == " ":
                    gate.confirm(event)
                return
            row = focused_row()
            if key in _CHOOSE_KEYS and row is not None and body.choose(row, _CHOOSE_KEYS[key]):
                return             # the row's kind took it: a pick moved
            action = body.actions.get(key)
            if action is not None and row is not None:
                action(row)
                reconcile()        # the action may have folded the cursor's own row away
        return handler

    def type_char(event: KeyPressEvent) -> None:
        # Printable keys type into a focused field; elsewhere they fall
        # through unused. Specials carry escape sequences (unprintable) and
        # are filtered out.
        if (fld := focused_field()) is not None and event.data \
                and event.data.isprintable():
            fld.insert(event.data)
            refresh_auto(field_rows)

    def cancel(event: KeyPressEvent) -> None:
        event.app.exit()

    kb = KeyBindings()
    # Every binding runs behind the really-done? interception (the gate's own
    # rule), so the answer key never doubles as its usual action.
    bind = answer_first_bind(kb, gate)
    bind("up", lambda event: move(-1))
    bind("down", lambda event: move(1))
    for key in dict.fromkeys([*_FIELD_EDITS, *_FIELD_MOTIONS, " ", *body.actions]):
        bind(key, keyed(key))
    bind("enter", gate.confirm)
    bind("escape", cancel)
    bind("c-c", cancel)
    bind(Keys.Any, type_char)

    Application(
        layout=Layout(HSplit([
            *header_windows(title, preamble_lines),
            Window(height=1, char=" "),
            Window(FormattedTextControl(_fragment_source(option_fragments),
                                        get_cursor_position=cursor_pos,
                                        focusable=True,
                                        show_cursor=False),
                   wrap_lines=False, dont_extend_height=True),
            Window(height=1, char=" "),
            # Flexible filler, so the warnings + confirm hug the bottom.
            Window(FormattedTextControl(_fragment_source(filler_fragments)), wrap_lines=True),
            Window(FormattedTextControl(_fragment_source(warning_fragments)), wrap_lines=True, dont_extend_height=True),
            Window(height=1, char=" "),
            Window(FormattedTextControl(_fragment_source(confirm_fragments)), height=1),
            Window(FormattedTextControl(_fragment_source(lambda: [(UiClass.STATUS.css, hint)])),
                   height=STATUS_HEIGHT),
        ])),
        key_bindings=kb,
        style=Style.from_dict(STYLE_DICT),
        full_screen=True,
    ).run()

    if not gate.confirmed():
        return None
    return {f.key: f.value.strip() for f in field_rows}


# ============================================================
# Checkbox form (multi-select) — the primitive behind prompt_tags
# ============================================================

def checkbox_form(title: str, options: list[FormOption],
                  warnings: dict[frozenset[str], tuple[str, list[str]]] | None = None,
                  requires: dict[str, frozenset[str]] | None = None,
                  wants: dict[str, tuple[tuple[str, str], ...]] | None = None,
                  labels: dict[str, str] | None = None,
                  preamble: list[str] | None = None,
                  fields: list[TextField] | None = None,
                  defaults: dict[frozenset[str], str] | None = None,
                  ) -> FormResult | None:
    """The multi-select form on `run_form`'s scaffold (keys, fields, confirm
    rules and layout are all documented there). What this form adds: Space
    toggles the focused checkbox, with the requires-cascade; the focused
    option's `body` renders in the flexible panel under the list; `warnings`
    entries whose combination is fully checked, and `wants` whose wanted key
    is unchecked, render live in the warning window.

    `requires` maps option keys to prerequisite option keys and drives the
    live check-cascade: checking a box also checks its transitive
    prerequisites; unchecking a box that others depend on unchecks those
    dependents. No disabling or indentation — every row stays freely
    toggleable, the cascade just keeps the set consistent. Radios take part
    too (`set_checked`): a prerequisite in a group is checked by moving its
    group's dot, and a member a moved dot leaves behind drops ITS
    dependents — so a model row requiring ⟪Claude⟫ clears when the AI dot
    moves to ⟪Gemini⟫. A group whose rows carry requires can be emptied by
    that cascade, so it needs `defaults` to fill it again.

    `defaults` maps a SET of keys to the radio row that set selects. When a
    row becomes checked and completes a set (every key in it checked), that
    set's row is dotted: the tag form keys each (engine, AI) pair to the
    model the engine's standard rates for that AI, so dotting either moves
    the model's dot — over a pick, since choosing the engine is what the
    pick fine-tunes. At open, a target group the options left EMPTY takes
    its complete set's row, so the group starts with one dot; a prefilled
    dot is kept. Non-reentrant by rule: a target may not appear in any set,
    and its own prerequisites must lie inside its set — so dotting it
    triggers no further default, and cannot chain (ValueError otherwise).
    All of it runs inside one `set_checked` call, which nothing observes
    midway: the group may be empty between its steps, never after.

    `wants` maps option keys to their (wanted-key, message) requests — purely
    advisory (see wants_warnings). `labels` maps keys to the display form
    those warnings name them by — pass it when rows are labelled differently
    than they are keyed, or the warning points the user at a name that
    appears nowhere on screen.

    `preamble` lines render dim (comment-like) between the title and the
    rows — context the form was opened with (instance name, workspace).

    Rows with `header=True` render but are skipped by navigation; rows with
    a `group` behave as radios; rows with `locked=True` render grayed and
    ignore Space; rows that `folds_with_anchor` render only while their
    anchor is checked (see FormOption). `fields` (TextField rows) render
    ABOVE the options.

    Returns a `FormResult` (the checked keys in display order, plus any
    field values), or None on cancel."""
    if not options:
        raise ValueError("options must be non-empty")
    rows = ordered_form_options(options)
    warning_map = warnings or {}
    req_map = requires or {}
    wants_map = wants or {}
    defaults_map = defaults or {}
    by_key = {o.key: o for o in rows if not o.header}
    for combo, target in defaults_map.items():
        if target not in by_key or by_key[target].group is None:
            raise ValueError(f"defaults: {target!r} is not a radio row — a set selects a group's member")
        if any(target in other for other in defaults_map):
            raise ValueError(f"defaults: {target!r} is a target AND in a set — defaults would chain")
        if not requires_closure(target, req_map) <= combo:
            raise ValueError(f"defaults: {target!r} requires {sorted(requires_closure(target, req_map) - combo)}, "
                             f"outside its set {sorted(combo)} — dotting it could fire another default")

    def set_checked(opt: FormOption, value: bool) -> None:
        """Set one row, keeping the checked set requires-consistent — the ONE
        way a row changes, by Space or by a cascade. A radio that becomes
        checked unchecks the rest of its group, each through here; then the
        row cascades: checked, it checks its transitive prerequisites;
        unchecked, it unchecks every row depending on it. Because a member
        the dot left is unchecked HERE too, its dependents follow it out —
        which a cascade run only for the newly-dotted member would miss
        (strict-reviewer, gate model-picker). Locked rows never change."""
        if opt.locked or opt.checked == value:
            return
        opt.checked = value
        if value:
            if opt.group is not None:
                for other in rows:
                    if other is not opt and other.group == opt.group:
                        set_checked(other, False)
            for key in requires_closure(opt.key, req_map):
                if key in by_key:
                    set_checked(by_key[key], True)
            for combo, target in defaults_map.items():
                if opt.key in combo and combo <= checked_keys():
                    set_checked(by_key[target], True)
                    # A fired default re-selects its target's sub-picks too:
                    # choosing an engine selects the model AND its default
                    # level. Only rows under the TARGET — a pick made under
                    # another bullet is not this default's to clear
                    # (researcher, gate effort-row). The at-open fill below
                    # is its own loop, so a stored pick survives opening.
                    for row in rows:
                        if row.attached_to == target and row.choices is not None:
                            row.choice = None
        else:
            for other in rows:
                if other.checked and opt.key in requires_closure(other.key, req_map):
                    set_checked(other, False)

    def toggle(index: int) -> None:
        """Space on a row: a checkbox flips; a radio checks-and-excludes its
        group, and a checked radio stays checked — pick a different member
        to move the dot. The cascades run through `set_checked`; locked rows
        are inert."""
        opt = rows[index]
        if opt.choices is not None or (opt.group is not None and opt.checked):
            return                     # a choices row moves with ←/→; a dotted radio stays
        set_checked(opt, not opt.checked)
        reconcile_choices()

    def checked_keys() -> set[str]:
        return {o.key for o in rows if o.checked and not o.header}

    def options_of(opt: FormOption) -> list[tuple[str | None, str]]:
        return opt.choices(frozenset(checked_keys())) if opt.choices is not None else []

    def reconcile_choices() -> None:
        """Every choices row back on a value its list still offers: a pick
        that left the list — a model that lacks the picked level got
        dotted — returns to None, its `choice_default`. None itself is never
        "not offered": it is the default wherever the default now lies."""
        for opt in rows:
            values = [value for value, _ in options_of(opt)]
            if opt.choices is not None and opt.choice is not None and opt.choice not in values:
                opt.choice = None

    def shift(index: int, delta: int) -> bool:
        """←/→ on a row (`FormBody.choose`): a choices row moves its pick one
        place, stopping at the ends, and says it took the key; ANY other row
        does not, and has no ←/→ action here either — a fallback could only
        move a pick the cursor is not on (gate model-picker-3). On a text
        field the scaffold never gets here: the field keeps the keys for its
        caret."""
        opt = rows[index]
        values = [value for value, _ in options_of(opt)]
        if opt.choices is None or opt.locked or not values:
            return False
        default = default_of(opt)
        current = opt.choice if opt.choice is not None else default
        position = values.index(current) if current in values else 0
        picked = values[max(0, min(len(values) - 1, position + delta))]
        opt.choice = None if picked == default else picked
        return True

    def default_of(opt: FormOption) -> str | None:
        return opt.choice_default(frozenset(checked_keys())) if opt.choice_default is not None else None

    def connector(opt: FormOption) -> str:
        """The attached-row connector, indented one step per level of
        attachment (an effort row sits under a bullet under an AI)."""
        depth, anchor, seen = 0, opt.attached_to, {opt.key}
        while anchor is not None and anchor in by_key and anchor not in seen:
            depth, seen, anchor = depth + 1, seen | {anchor}, by_key[anchor].attached_to
        return "  " * max(depth, 1) + ATTACHED_CONNECTOR.lstrip()

    def label_of(opt: FormOption) -> list[tuple[str, str]]:
        return opt.label(frozenset(checked_keys())) if callable(opt.label) else _normalize(opt.label)

    def visible(index: int) -> bool:
        """Folded under an unchecked anchor — or under a folded one, up the
        chain — or a choices row with nothing to choose (an effort row under
        a model that takes none): not drawn, no stop. An unknown anchor, or
        a loop the ordering let through: better shown than vanished."""
        return shown(rows[index], set())

    def shown(opt: FormOption, seen: set[str]) -> bool:
        if opt.choices is not None and not options_of(opt):
            return False
        if not opt.folds_with_anchor:
            return True
        anchor = by_key.get(opt.attached_to) if opt.attached_to is not None else None
        if anchor is None or anchor.key in seen:
            return True
        return anchor.checked and shown(anchor, seen | {opt.key})

    for combo, target in defaults_map.items():      # at open: an empty target group takes its default
        group = by_key[target].group
        if combo <= checked_keys() and not any(o.checked for o in rows if o.group == group):
            set_checked(by_key[target], True)
    reconcile_choices()                              # at open: a stored pick the list no longer offers

    def row_fragments() -> list[list[tuple[str, str]]]:
        out: list[list[tuple[str, str]]] = []
        for opt in rows:
            frags: list[tuple[str, str]] = []
            if opt.header:
                frags.extend(label_of(opt))
            elif opt.choices is not None:
                if opt.attached_to is not None:
                    frags.append((UiClass.STATUS.css, connector(opt)))
                frags.extend(label_of(opt))
                dotted = opt.choice if opt.choice is not None else default_of(opt)
                for value, text in options_of(opt):
                    frags.append(("", f"  {BULLET_ON if value == dotted else BULLET_OFF}{text}"))
            else:
                if opt.attached_to is not None:
                    frags.append((UiClass.STATUS.css, connector(opt)))
                if opt.group is not None and opt.attached_to is not None:
                    frags.append(("", BULLET_ON if opt.checked else BULLET_OFF))
                elif opt.group is not None:
                    frags.append(("", RADIO_ON if opt.checked else RADIO_OFF))
                else:
                    frags.append(("", CHECKBOX_ON if opt.checked else CHECKBOX_OFF))
                frags.extend(label_of(opt))
            if opt.locked:   # gray the whole row — a fixed, un-toggleable entry
                frags = [(STYLE_LOCKED, text) for _, text in frags]
            out.append(frags)
        return out

    def body_fragments(focused: int | None) -> list[tuple[str, str]]:
        if focused is None:
            return []
        body = rows[focused].body
        return _indent_fragments(body) if body else []

    def warning_lines() -> list[str]:
        checked = checked_keys()
        return [line
                for header, body in (active_warnings(checked, warning_map)
                                     + wants_warnings(checked, wants_map, labels))
                for line in (header, *body)]

    values = run_form(title, preamble, fields, FormBody(
        rows=row_fragments,
        stops=[i for i, o in enumerate(rows) if not o.header],
        actions={" ": toggle},
        filler=body_fragments,
        warnings=warning_lines,
        confirm_label=FORM_CONFIRM_LABEL,
        hint=FORM_CHOICE_HINT if any(o.choices is not None for o in rows) else FORM_HINT_TEXT,
        snapshot=lambda: (tuple(o.checked for o in rows), tuple(o.choice for o in rows)),
        visible=visible,
        choose=shift,
    ))
    if values is None:
        return None
    return FormResult(checked=[o.key for o in rows if o.checked],
                      field_values=values,
                      choices={o.key: o.choice for o in rows if o.choices is not None})
