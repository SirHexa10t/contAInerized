"""The cluster-membership form — pick agents, get members.

This is the widget `cluster_plan.md` flagged as its own milestone: the tag form
(`tag_form.checkbox_form`) TOGGLES a fixed set, while a cluster membership is a
GROWING LIST — picking an agent that is already in adds *another* of it, which
is how a devteam holds two researchers. So this is a separate form with
accumulator semantics, not a checkbox variant.

**Deliberately absent: any per-member editing.** No role fields, no per-member
tag toggles (decided with the operator: fine detail mid-flow is noise during
setup — members are edited later, from the picker, like instances). The form
only grows and shrinks the list; every id the confirm will create is PREVIEWED
live in the members panel, so the auto-derived roles (`legoset.auto_roles` —
`golem__1`, `golem__2`) are never a surprise.

Same split as tag_form: the interactive Application here stays out of unit
scope, so everything with rules — prefill, add/remove, the id preview — is a
pure helper the tests exercise directly, and the app is a thin loop over them.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable

from prompt_toolkit import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from ..cluster.legoset import ClusterTemplate, auto_roles
from ..cluster.member import member_id
from .tag_form import (
    STYLE_DICT, TITLE_HEIGHT, UNCHANGED_QUESTION, TextField, UiClass,
    _fragment_source, field_errors, field_row_fragments, refresh_auto,
)

# One pick = (agent, role | None). None means "the form added this one" — the
# role is auto-derived at confirm; a string is a role a TEMPLATE shipped, kept
# verbatim (researcher__primary survives any amount of adding and removing).
Pick = tuple[str, str | None]

# Mirrors menu_picker.STYLE_AGENT_NAME (not imported: menu_picker imports this
# module, and the picker's style constants are not worth a shared module yet).
STYLE_MEMBER_NAME = "bold fg:ansibrightblue"
STYLE_COUNT      = "bold fg:ansigreen"       # ×N chip while the agent is picked
STYLE_COUNT_ZERO = UiClass.STATUS.css        # dim placeholder while it is not
CONFIRM_LABEL = "[ Create cluster ]"
HINT_TEXT = ("  Space/+ add another of this agent · Backspace/- remove its last"
             " · Enter create · Esc cancel")
EMPTY_WARNING = "  no members yet — Space on an agent adds one"


def prefill_picks(template: ClusterTemplate) -> list[Pick]:
    """A template's members as the form's starting picks.

    A role equal to the agent's own name is `Member.of`'s DEFAULT — the
    template said nothing — so it comes back as None. That matters when the
    user then adds a second of that agent: both entries renumber
    (`golem__1`/`golem__2`), which a kept literal `golem` role would prevent
    (it would pin the id while its twin got a number)."""
    return [(m.agent, None if m.role == m.agent else m.role)
            for m in template.members]


def add_pick(picks: list[Pick], agent: str) -> None:
    """Picking an agent ADDS AN ENTRY — the interaction this form exists for.
    Appended at the end; pick SEQUENCE only drives duplicate numbering
    (`golem__1` was picked before `golem__2`) — display and window order are
    derived by picker-sort everywhere (state.picker_order), so where in the
    session a pick lands is not something the user has to compose."""
    picks.append((agent, None))


def remove_last(picks: list[Pick], agent: str) -> None:
    """Remove that agent's LAST entry (no-op at zero).

    Last-in-first-out, and template entries are not protected: prefills are a
    starting point, never a lock, so shrinking `devteam`'s two researchers to
    one drops `adversarial` first — the most recently listed."""
    for index in range(len(picks) - 1, -1, -1):
        if picks[index][0] == agent:
            del picks[index]
            return


def preview_ids(picks: list[Pick], agent_rank: dict[str, int] | None = None) -> list[str]:
    """The member ids confirming NOW would create — rendered live so the
    auto-derived roles are visible before anything is persisted.

    With `agent_rank` (agent name → its position in the form's agent list,
    which arrives in picker order), ids come back in the DERIVED order the
    cluster will actually display and launch in — so the panel is a truthful
    preview of the window list, not of a pick sequence that carries no
    meaning. Without it (rank unknown), pick order is kept."""
    ids = [(agent, member_id(agent, role)) for agent, role in auto_roles(picks)]
    if agent_rank is not None:
        ids.sort(key=lambda pair: (agent_rank.get(pair[0], len(agent_rank)),
                                   pair[1]))
    return [identifier for _, identifier in ids]


# TextField and its helpers live in tag_form (the shared form machinery, used
# by both this form and the instance/tag form); re-exported here because this
# module introduced them and its callers import them from here.


def prompt_members(agents: list[tuple[str, str]], initial: list[Pick], *,
                   title: str, preamble: list[str] | None = None,
                   fields: list[TextField] | None = None,
                   ) -> tuple[dict[str, str], list[Pick]] | None:
    """Run the membership form; return `(field values, ordered picks)`, or
    None on cancel.

    `agents` is every pickable agent as (name, one-line description) — the
    caller derives it from the registry exactly as the picker's Create rows do.
    `initial` is the prefill (a template's members, or an existing cluster's
    when editing). `fields` render ABOVE the agent list and are edited by
    typing while focused — which is why key handling branches on the focused
    row's kind: Space ADDS on an agent row but is a literal space in a path;
    same for `+`/`-`, and arrows MOVE THE CURSOR on a field (ctrl+arrows by
    word) while adding/removing on an agent row. Enter confirms from
    anywhere, Esc cancels, exactly the checkbox_form conventions. Confirm
    refuses while the membership is empty or any field is invalid — the
    warning zone is already saying why — and asks `really done? (y/N)` when
    nothing was changed, exactly checkbox_form's rule.
    """
    if not agents:
        raise ValueError("agents must be non-empty")
    preamble_lines = preamble or []
    field_rows: list[TextField] = list(fields or [])
    agents_at = len(field_rows)
    confirm_index = agents_at + len(agents)
    # `agents` arrives in picker order (creatable_agents), so its indices ARE
    # the rank the members panel sorts by — the panel then previews the actual
    # window order, not the meaningless pick sequence.
    agent_rank = {name: index for index, (name, _) in enumerate(agents)}
    state: dict[str, Any] = {"cursor": 0, "confirmed": False,
                             "asked": False, "picks": list(initial)}

    def counts() -> Counter:
        return Counter(agent for agent, _ in state["picks"])

    def focused_field() -> TextField | None:
        if state["cursor"] < agents_at:
            return field_rows[state["cursor"]]
        return None

    def focused_agent() -> str | None:
        index = state["cursor"] - agents_at
        return agents[index][0] if 0 <= index < len(agents) else None

    def option_fragments() -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        label_width = max((len(f.label) for f in field_rows), default=0)
        for index, field in enumerate(field_rows):
            out.extend(field_row_fragments(field, index == state["cursor"],
                                           label_width))
            out.append(("", "\n"))
        if field_rows:
            out.append(("", "\n"))
        tally = counts()
        width = max(len(name) for name, _ in agents)
        for index, (name, description) in enumerate(agents):
            count = tally.get(name, 0)
            frags = [
                (STYLE_COUNT, f"  ×{count} ") if count else (STYLE_COUNT_ZERO, "   · "),
                (STYLE_MEMBER_NAME, f" {name:<{width}}"),
                (UiClass.STATUS.css, f"  {description}"),
            ]
            if index + agents_at == state["cursor"]:
                frags = [(f"{UiClass.CURSOR.css} {style}".strip(), text)
                         for style, text in frags]
            out.extend(frags)
            out.append(("", "\n"))
        out.pop()
        return out

    def members_fragments() -> list[tuple[str, str]]:
        picks = state["picks"]
        out: list[tuple[str, str]] = []
        if not picks:
            out.append((UiClass.WARNING.css, EMPTY_WARNING))
        else:
            ids = preview_ids(picks, agent_rank)
            out.append(("", f"  members ({len(ids)}):  "))
            for i, identifier in enumerate(ids):
                if i:
                    out.append((UiClass.STATUS.css, " · "))
                out.append((STYLE_MEMBER_NAME, identifier))
        for complaint in field_errors(field_rows):
            out.append((UiClass.WARNING.css, f"\n  {complaint}"))
        if state["asked"]:
            out.append((UiClass.TITLE.css, f"\n  {UNCHANGED_QUESTION}"))
        return out

    def confirm_fragments() -> list[tuple[str, str]]:
        style = (UiClass.CURSOR.css if state["cursor"] == confirm_index
                 else UiClass.TITLE.css)
        return [("", "  "), (style, CONFIRM_LABEL)]

    def cursor_pos() -> Point:
        return Point(0, min(state["cursor"], confirm_index - 1))

    def move(delta: int) -> None:
        state["cursor"] = (state["cursor"] + delta) % (confirm_index + 1)

    def typed(field: TextField, char: str) -> None:
        field.insert(char)
        refresh_auto(field_rows)

    def add(event: KeyPressEvent) -> None:
        # Space/+: literal in a text field, "one more of this agent" on an
        # agent row, confirm on the button — the branch that makes fields and
        # accumulator rows coexist under one key map.
        if (field := focused_field()) is not None:
            if event.data and event.data.isprintable():
                typed(field, event.data)
            return
        if (agent := focused_agent()) is not None:
            add_pick(state["picks"], agent)
        else:
            confirm(event)

    def erase(event: KeyPressEvent) -> None:
        """Backspace: delete before the cursor on a field, remove that
        agent's last entry on an agent row."""
        if (field := focused_field()) is not None:
            field.backspace()
            refresh_auto(field_rows)
            return
        if (agent := focused_agent()) is not None:
            remove_last(state["picks"], agent)

    def delete_key(event: KeyPressEvent) -> None:
        """Delete: erase AT the cursor on a field, remove on an agent row."""
        if (field := focused_field()) is not None:
            field.delete()
            refresh_auto(field_rows)
            return
        if (agent := focused_agent()) is not None:
            remove_last(state["picks"], agent)

    def minus(event: KeyPressEvent) -> None:
        """`-`: a literal character in a field (paths carry them), the remove
        action on an agent row."""
        if (field := focused_field()) is not None:
            typed(field, "-")
            return
        if (agent := focused_agent()) is not None:
            remove_last(state["picks"], agent)

    def arrow(motion: Callable[[TextField], None],
              act: Callable[[list[Pick], str], None]) -> Callable[[KeyPressEvent], None]:
        """←/→ (and their ctrl+ word variants): CURSOR MOTION on a field,
        add/remove on an agent row, nothing on the confirm button.

        REGRESSION GUARD in prose: an earlier version routed every remove-ish
        key through one handler whose field branch fell through to
        backspace() — so pressing ← while a field was focused ATE CHARACTERS
        (reported from a live form). An arrow on a field only ever MOVES."""
        def handler(event: KeyPressEvent) -> None:
            if (field := focused_field()) is not None:
                motion(field)
            elif (agent := focused_agent()) is not None:
                act(state["picks"], agent)
        return handler

    def field_motion(motion: Callable[[TextField], None]) -> Callable[[KeyPressEvent], None]:
        """Home/End: cursor extremes on a field, nothing anywhere else."""
        def handler(event: KeyPressEvent) -> None:
            if (field := focused_field()) is not None:
                motion(field)
        return handler

    def type_char(event: KeyPressEvent) -> None:
        """Catch-all for printable keys — text entry on a focused field,
        ignored everywhere else (agent rows only answer their action keys)."""
        if (field := focused_field()) is not None and event.data \
                and event.data.isprintable():
            typed(field, event.data)

    refresh_auto(field_rows)   # the initial derivation, before any keystroke
    # The really-done? baseline — same rule as checkbox_form: a confirm that
    # changed neither a field nor the membership asks first, fields-only forms
    # excepted (there are none here in practice, but the tests drive some).
    baseline = (tuple(f.value for f in field_rows), tuple(state["picks"]))

    def unchanged() -> bool:
        return baseline == (tuple(f.value for f in field_rows),
                            tuple(state["picks"]))

    def confirm(event: KeyPressEvent) -> None:
        if not state["picks"] or field_errors(field_rows):
            return           # the warning zone is already explaining
        if field_rows and unchanged() and not state["asked"]:
            state["asked"] = True   # UNCHANGED_QUESTION renders; `answers` consumes the reply
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

    kb = KeyBindings()

    def bind(key: Keys | str, handler: Callable[[KeyPressEvent], None]) -> None:
        def wrapped(event: KeyPressEvent) -> None:
            if not answers(event):
                handler(event)
        kb.add(key)(wrapped)

    bind("up", lambda event: move(-1))
    bind("down", lambda event: move(1))
    for key in (" ", "+"):
        bind(key, add)
    bind("right", arrow(TextField.right, add_pick))
    bind("left", arrow(TextField.left, remove_last))
    bind("c-right", arrow(TextField.word_right, add_pick))
    bind("c-left", arrow(TextField.word_left, remove_last))
    bind("home", field_motion(TextField.home))
    bind("end", field_motion(TextField.end))
    bind("backspace", erase)
    bind("delete", delete_key)
    bind("-", minus)
    bind("enter", confirm)
    bind("escape", lambda event: event.app.exit())
    bind("c-c", lambda event: event.app.exit())
    bind(Keys.Any, type_char)

    header: list[Window] = [Window(FormattedTextControl(
        _fragment_source(lambda: [(UiClass.TITLE.css, title)])), height=TITLE_HEIGHT)]
    if preamble_lines:
        header.append(Window(FormattedTextControl(_fragment_source(
            lambda: [(UiClass.STATUS.css, "\n".join(preamble_lines))])),
            height=len(preamble_lines)))
    hint = (("  type into the focused field · " if field_rows else "  ")
            + HINT_TEXT.strip())
    Application(
        layout=Layout(HSplit([
            *header,
            Window(height=1, char=" "),
            Window(FormattedTextControl(_fragment_source(option_fragments),
                                        get_cursor_position=cursor_pos,
                                        focusable=True, show_cursor=False),
                   wrap_lines=False, dont_extend_height=True),
            Window(height=1, char=" "),
            # Flexible filler, so the panel + confirm hug the bottom like the
            # tag form's explanation zone does.
            Window(FormattedTextControl(_fragment_source(members_fragments)),
                   wrap_lines=True),
            Window(height=1, char=" "),
            Window(FormattedTextControl(_fragment_source(confirm_fragments)), height=1),
            Window(FormattedTextControl(_fragment_source(
                lambda: [(UiClass.STATUS.css, hint)])), height=2),
        ])),
        key_bindings=kb,
        style=Style.from_dict(STYLE_DICT),
        full_screen=True,
    ).run()
    if not state["confirmed"]:
        return None
    return {f.key: f.value.strip() for f in field_rows}, state["picks"]
