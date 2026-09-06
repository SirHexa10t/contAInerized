"""The cluster-membership form — pick agents, get members.

This is the widget `cluster_plan.md` flagged as its own milestone: the tag form
(`form_core.checkbox_form`) TOGGLES a fixed set, while a cluster membership is a
GROWING LIST — picking an agent that is already in adds *another* of it, which
is how a devteam holds two researchers. So this is a separate form with
accumulator semantics, not a checkbox variant.

**Deliberately absent: any per-member editing.** No role fields, no per-member
tag toggles (decided with the operator: fine detail mid-flow is noise during
setup — members are edited later, from the picker, like instances). The form
only grows and shrinks the list; every id the confirm will create is PREVIEWED
live in the members panel, so the auto-derived roles (`legoset.auto_roles` —
`golem__1`, `golem__2`) are never a surprise.

Same split as the tag form: the interactive Application here stays out of unit
scope. The rules it needs — what a pick is, adding/removing one, and which
member ids a pick list would produce — are NOT here: they live beside the
rest of the pick algebra in `cluster.legoset` (moved 2026-09-03), which had
always owned `auto_roles`/`assemble`/`reassemble` over the very same shape.
This module is the typing surface; the app is a thin loop over those.

It also RUNS ON form_core's shared scaffold rather than restating it:
`confirm_gate` (so the really-done? question has the same words, the same
rules and — the bug that prompted the extraction — the same PLACEMENT as the
tag form's), plus `answer_first_bind`, `header_windows`,
`confirm_row_fragments` and `TextField`. What stays local is what genuinely
differs: every key here is dual-purpose (Space is a literal inside a text
field but "one more of this agent" on an agent row), so one abstraction over
two key semantics would cost more than the copies it saved.
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

from ..cluster.legoset import Pick, add_pick, preview_ids, remove_last
from .form_core import (
    TextField, answer_first_bind, confirm_gate, confirm_row_fragments,
    field_errors, field_row_fragments, header_windows, refresh_auto,
)
from .styles import STYLE_DICT, UiClass, _fragment_source

# Mirrors menu_picker.STYLE_AGENT_NAME (not imported: menu_picker imports this
# module, and the picker's style constants are not worth a shared module yet).
STYLE_MEMBER_NAME = "bold fg:ansibrightblue"
STYLE_COUNT      = "bold fg:ansigreen"       # ×N chip while the agent is picked
STYLE_COUNT_ZERO = UiClass.STATUS.css        # dim placeholder while it is not
CONFIRM_LABEL = "[ Create cluster ]"
HINT_TEXT = ("  Space/+ add another of this agent · Backspace/- remove its last"
             " · Enter create · Esc cancel")
EMPTY_WARNING = "  no members yet — Space on an agent adds one"



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
        return out

    def warning_fragments() -> list[tuple[str, str]]:
        """Field complaints + the really-done? question, in their OWN
        `dont_extend_height` window just above the confirm row — the same
        place the tag form puts them. They used to ride the members panel,
        which is the FLEXIBLE filler, so they top-aligned under the options
        while the tag form's hugged the button: the same message in two
        different places (reported 2026-09-02)."""
        out: list[tuple[str, str]] = [
            (UiClass.WARNING.css, f"  {complaint}\n")
            for complaint in field_errors(field_rows)]
        out += [(style, text + "\n") for style, text in gate.question()]
        if out:
            out[-1] = (out[-1][0], out[-1][1].rstrip("\n"))
        return out

    def confirm_fragments() -> list[tuple[str, str]]:
        return confirm_row_fragments(CONFIRM_LABEL,
                                     state["cursor"] == confirm_index)

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
            gate.confirm(event)

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
    # The shared confirm/really-done? machine (form_core.confirm_gate) — same
    # words, same rules, same rendering as the tag form. Its snapshot spans
    # BOTH things a user can change here: the fields and the membership.
    gate = confirm_gate(
        snapshot=lambda: (tuple(f.value for f in field_rows),
                          tuple(state["picks"])),
        ready=lambda: bool(state["picks"]) and not field_errors(field_rows),
        asks_when_unchanged=bool(field_rows))

    kb = KeyBindings()

    bind = answer_first_bind(kb, gate)

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
    bind("enter", gate.confirm)
    bind("escape", lambda event: event.app.exit())
    bind("c-c", lambda event: event.app.exit())
    bind(Keys.Any, type_char)

    header = header_windows(title, preamble_lines)
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
            Window(FormattedTextControl(_fragment_source(warning_fragments)),
                   wrap_lines=True, dont_extend_height=True),
            Window(height=1, char=" "),
            Window(FormattedTextControl(_fragment_source(confirm_fragments)), height=1),
            Window(FormattedTextControl(_fragment_source(
                lambda: [(UiClass.STATUS.css, hint)])), height=2),
        ])),
        key_bindings=kb,
        style=Style.from_dict(STYLE_DICT),
        full_screen=True,
    ).run()
    if not gate.confirmed():
        return None
    return {f.key: f.value.strip() for f in field_rows}, state["picks"]
