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
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from ..cluster.legoset import ClusterTemplate, auto_roles
from ..cluster.member import member_id
from .tag_form import STYLE_DICT, TITLE_HEIGHT, UiClass, _fragment_source

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


def prompt_members(agents: list[tuple[str, str]], initial: list[Pick], *,
                   title: str, preamble: list[str] | None = None,
                   ) -> list[Pick] | None:
    """Run the membership form; return the ordered picks, or None on cancel.

    `agents` is every pickable agent as (name, one-line description) — the
    caller derives it from the registry exactly as the picker's Create rows do.
    `initial` is the template prefill (possibly empty for a from-scratch
    cluster). Keys follow the house convention set by checkbox_form: Space is
    the row action (here: add), Enter confirms from anywhere, Esc cancels —
    plus Backspace as add's inverse. Confirming an EMPTY membership is refused
    silently while the panel says why: a cluster with no members is nothing.
    """
    if not agents:
        raise ValueError("agents must be non-empty")
    preamble_lines = preamble or []
    confirm_index = len(agents)
    # `agents` arrives in picker order (creatable_agents), so its indices ARE
    # the rank the members panel sorts by — the panel then previews the actual
    # window order, not the meaningless pick sequence.
    agent_rank = {name: index for index, (name, _) in enumerate(agents)}
    state: dict[str, Any] = {"cursor": 0, "confirmed": False,
                             "picks": list(initial)}

    def counts() -> Counter:
        return Counter(agent for agent, _ in state["picks"])

    def option_fragments() -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        tally = counts()
        width = max(len(name) for name, _ in agents)
        for index, (name, description) in enumerate(agents):
            count = tally.get(name, 0)
            frags: list[tuple[str, str]] = [
                (STYLE_COUNT, f"  ×{count} ") if count else (STYLE_COUNT_ZERO, "   · "),
                (STYLE_MEMBER_NAME, f" {name:<{width}}"),
                (UiClass.STATUS.css, f"  {description}"),
            ]
            if index == state["cursor"]:
                frags = [(f"{UiClass.CURSOR.css} {style}".strip(), text)
                         for style, text in frags]
            out.extend(frags)
            out.append(("", "\n"))
        out.pop()
        return out

    def members_fragments() -> list[tuple[str, str]]:
        picks = state["picks"]
        if not picks:
            return [(UiClass.WARNING.css, EMPTY_WARNING)]
        ids = preview_ids(picks, agent_rank)
        out: list[tuple[str, str]] = [("", f"  members ({len(ids)}):  ")]
        for i, identifier in enumerate(ids):
            if i:
                out.append((UiClass.STATUS.css, " · "))
            out.append((STYLE_MEMBER_NAME, identifier))
        return out

    def confirm_fragments() -> list[tuple[str, str]]:
        style = (UiClass.CURSOR.css if state["cursor"] == confirm_index
                 else UiClass.TITLE.css)
        return [("", "  "), (style, CONFIRM_LABEL)]

    def cursor_pos() -> Point:
        return Point(0, min(state["cursor"], len(agents) - 1))

    def move(delta: int) -> None:
        state["cursor"] = (state["cursor"] + delta) % (confirm_index + 1)

    def focused_agent() -> str | None:
        return agents[state["cursor"]][0] if state["cursor"] < confirm_index else None

    def add(event: KeyPressEvent) -> None:
        if (agent := focused_agent()) is not None:
            add_pick(state["picks"], agent)
        else:
            confirm(event)   # Space on the confirm row confirms, as in checkbox_form

    def remove(_: KeyPressEvent) -> None:
        if (agent := focused_agent()) is not None:
            remove_last(state["picks"], agent)

    def confirm(event: KeyPressEvent) -> None:
        if not state["picks"]:
            return           # nothing to create; the panel is already saying so
        state["confirmed"] = True
        event.app.exit()

    kb = KeyBindings()
    kb.add("up")(lambda event: move(-1))
    kb.add("down")(lambda event: move(1))
    for key in (" ", "+", "right"):
        kb.add(key)(add)
    for key in ("backspace", "-", "left", "delete"):
        kb.add(key)(remove)
    kb.add("enter")(confirm)
    kb.add("escape")(lambda event: event.app.exit())
    kb.add("c-c")(lambda event: event.app.exit())

    header: list[Window] = [Window(FormattedTextControl(
        _fragment_source(lambda: [(UiClass.TITLE.css, title)])), height=TITLE_HEIGHT)]
    if preamble_lines:
        header.append(Window(FormattedTextControl(_fragment_source(
            lambda: [(UiClass.STATUS.css, "\n".join(preamble_lines))])),
            height=len(preamble_lines)))
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
                lambda: [(UiClass.STATUS.css, HINT_TEXT)])), height=2),
        ])),
        key_bindings=kb,
        style=Style.from_dict(STYLE_DICT),
        full_screen=True,
    ).run()
    return state["picks"] if state["confirmed"] else None
