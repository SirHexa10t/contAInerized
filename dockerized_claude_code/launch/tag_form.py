"""The tag-selection form + the shared TUI style system.

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
(`tag_style`, `STYLE_TAG_*`, `RICH_BY_STYLE`), and the display coercers
(`_normalize` / `_plain`). menu_picker imports these from here (one-way:
this module never imports the picker).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from prompt_toolkit import Application                                     # dep — declared in pyproject.toml [project]
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from .tags import AgentBuild, Engine, Policy, PolicyStance, Profession, Registry, Specialty, Tag
from .tags.engine import sorted_engines

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
    checked radio can't be unchecked directly (pick another instead)."""
    key: str
    label: str | list[tuple[str, str]]
    body: list[tuple[str, str]] = field(default_factory=list)
    checked: bool = False
    attached_to: str | None = None
    header: bool = False
    group: str | None = None


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
                  preamble: list[str] | None = None) -> list[str] | None:
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

    `preamble` lines render dim (comment-like) between the title and the
    rows — context the form was opened with (instance name, workspace).

    Rows with `header=True` render but are skipped by navigation; rows with
    a `group` behave as radios (see FormOption).

    Returns the checked options' keys in display order, or None on cancel."""
    if not options:
        raise ValueError("options must be non-empty")
    rows = ordered_form_options(options)
    warning_map = warnings or {}
    req_map = requires or {}
    wants_map = wants or {}
    preamble_lines = preamble or []
    by_key = {o.key: o for o in rows if not o.header}
    confirm_index = len(rows)              # the confirm button is the last navigable row
    # Navigation stops: every non-header row, then the confirm button.
    stops = [i for i, o in enumerate(rows) if not o.header] + [confirm_index]
    state: dict[str, Any] = {"cursor": stops[0], "confirmed": False}

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

    def toggle(opt: FormOption) -> None:
        """Space on a row: plain rows flip (with requires-cascade); radio rows
        check-and-exclude their group (a checked radio stays checked — pick a
        different member to move the dot)."""
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
        for i, opt in enumerate(rows):
            frags: list[tuple[str, str]] = []
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
            if i == state["cursor"]:
                frags = [(f"{UiClass.CURSOR.css} {style}".strip(), text)
                         for style, text in frags]
            out.extend(frags)
            out.append(("", "\n"))
        if out:
            out.pop()   # trailing newline
        return out

    def body_fragments() -> list[tuple[str, str]]:
        if state["cursor"] >= confirm_index:
            return []
        body = rows[state["cursor"]].body
        return _indent_fragments(body) if body else []

    def warning_fragments() -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        checked = checked_keys()
        entries = active_warnings(checked, warning_map) + wants_warnings(checked, wants_map)
        for header, body in entries:
            out.append((UiClass.WARNING.css, f"  {header}\n"))
            out.extend((UiClass.WARNING.css, f"  {line}\n") for line in body)
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
        return Point(0, min(state["cursor"], len(rows) - 1))

    def move(delta: int) -> None:
        i = stops.index(state["cursor"])
        state["cursor"] = stops[(i + delta) % len(stops)]

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
            toggle(rows[state["cursor"]])

    @kb.add("enter")
    def _(event: KeyPressEvent) -> None: confirm(event)

    @kb.add("escape")
    def _(event: KeyPressEvent) -> None: event.app.exit()

    @kb.add("c-c")
    def _(event: KeyPressEvent) -> None: event.app.exit()

    header_windows = [Window(FormattedTextControl(title_fragments), height=TITLE_HEIGHT)]
    if preamble_lines:
        header_windows.append(Window(FormattedTextControl(preamble_fragments),
                                     height=len(preamble_lines)))
    body_layout = HSplit([
        *header_windows,
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


# ============================================================
# The tag form — registry → FormOptions → AgentBuild
# ============================================================

def _tag_row(tag: Tag, checked: bool, group: str | None = None) -> FormOption:
    """One selectable form row: colored kind-punctuated label + the tag's
    short description, a dim `(requires: …)` parenthetical when it has
    prerequisites, and the full description as the focused-row body — led by
    the tag's underlined FULLNAME, so the abbreviation in the label is never
    a puzzle (`{dood}` focuses to `Docker-outside-of-Docker: can run …`).
    Keys are the tags' full names (what `.lego` / instances.toml store)."""
    label: list[tuple[str, str]] = [(tag_style(tag), tag.label), ("", " ")]
    label.append(("", tag.short_description))
    if tag.requires:
        label.append((UiClass.STATUS.css, f"  (requires: {', '.join(sorted(tag.requires))})"))
    return FormOption(
        key=tag.name,
        label=label,
        body=[(STYLE_UNDERLINE, tag.fullname), ("", f": {tag.full_description}")],
        checked=checked,
        group=group,
    )


def _tag_form_options(registry: Registry, current: AgentBuild) -> list[FormOption]:
    """The full sectioned form: one header per kind (its nutshell), engines
    as a radio group at the top (pre-dotted from `current.engine` — the
    caller passes the RESOLVED engine, so the dot shows what would actually
    run), then professions / specialties / policies as checkboxes pre-checked
    from `current`'s axis lists. Policies are ordered by shortname WITH its
    leading symbol (`!` < `+` < `-` in ASCII), so same-stance policies sit
    together: demands, then grants, then denials."""
    checked = {*current.professions, *current.specialties, *current.policies}

    def header(kind_cls: type[Tag]) -> FormOption:
        return FormOption(
            key=f"#{kind_cls.root}",
            label=[(UiClass.TITLE.css, f"{kind_cls.root.upper()} — {kind_cls.nutshell}")],
            header=True,
        )

    out: list[FormOption] = [header(Engine)]
    out += [_tag_row(tag, checked=(tag.name == current.engine), group="engine")
            for tag in sorted_engines(registry.engines.values())]
    for kind_cls, members in ((Profession, list(registry.professions.values())),
                              (Specialty, list(registry.specialties.values())),
                              (Policy, sorted(registry.policies.values(),
                                              key=lambda p: p.shortname))):
        out.append(header(kind_cls))
        out += [_tag_row(tag, checked=tag.name in checked) for tag in members]
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


def prompt_tags(registry: Registry, current: AgentBuild, *,
                instance: str, workspace: str) -> AgentBuild | None:
    """Run the tag form (see the module docstring for the full behavior) and
    return the selection as a new AgentBuild, or None when the user cancels
    (Esc) — callers abort their create / modify flow on None rather than
    persisting anything. `instance` + `workspace` are the already-answered
    prompts, echoed as the form's preamble so the user sees the full context
    they're configuring."""
    options = _tag_form_options(registry, current)
    keys = checkbox_form(TITLE_TAGS_FORM, options,
                         warnings=_combo_warnings(registry),
                         requires=_form_requires(registry),
                         wants=_form_wants(registry),
                         preamble=[f"# instance:  {instance}",
                                   f"# workspace: {workspace}"])
    if keys is None:
        return None
    picked = set(keys)
    return AgentBuild(
        engine=next((n for n in registry.engines if n in picked), current.engine),
        professions=tuple(n for n in registry.professions if n in picked),
        specialties=tuple(n for n in registry.specialties if n in picked),
        policies=tuple(n for n in registry.policies if n in picked),
    )
