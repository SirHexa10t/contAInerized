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
  checkbox_form                       the multi-select primitive itself

Placement rule this module exists to enforce: a form must not restate any of
the above. The copies had already drifted once — the same really-done?
message rendered at the bottom of one form and the top of the other — and
`TestFormTailsMatch` now pins that they cannot diverge again.
"""

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
    STATUS_HEIGHT, STYLE_DICT, STYLE_LOCKED, TITLE_HEIGHT, UiClass,
    _fragment_source, _normalize,
)

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

# TITLE_HEIGHT / STATUS_HEIGHT come from `styles` — the picker reserves the
# same chrome, so the count is defined once there.



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
        out += [(style, text + "\n") for style, text in gate.question()]
        if out:
            out[-1] = (out[-1][0], out[-1][1].rstrip("\n"))
        return out

    def confirm_fragments() -> list[tuple[str, str]]:
        return confirm_row_fragments(FORM_CONFIRM_LABEL,
                                     state["cursor"] == confirm_index)

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
    # The shared confirm/really-done? machine (tag_form.confirm_gate), built
    # AFTER that derivation so its baseline includes the auto-filled values.
    # Only forms WITH fields ask: a fieldless form's Enter is unambiguous,
    # and open-look-Enter should stay one keystroke there.
    gate = confirm_gate(
        snapshot=lambda: (tuple(f.value for f in field_rows),
                          tuple(o.checked for o in rows)),
        ready=lambda: not field_errors(field_rows),
        asks_when_unchanged=bool(field_rows))

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
            gate.confirm(event)
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

    # Every binding runs behind the really-done? interception (the gate's
    # own rule), so the answer key never doubles as its usual action.
    bind = answer_first_bind(kb, gate)

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
    bind("enter", gate.confirm)
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

    if not gate.confirmed():
        return None
    keys = [o.key for o in rows if o.checked]
    if fields is None:
        return keys
    return {f.key: f.value.strip() for f in field_rows}, keys


# ============================================================
# The tag form — registry → FormOptions → AgentBuild
# ============================================================

