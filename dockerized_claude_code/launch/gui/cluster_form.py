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

Same split as the tag form: the interactive Application stays out of unit
scope. The rules it needs — what a pick is, adding/removing one, and which
member ids a pick list would produce — are NOT here: they live beside the
rest of the pick algebra in `cluster.legoset` (moved 2026-09-03), which had
always owned `auto_roles`/`assemble`/`reassemble` over the very same shape.

It RUNS ON `form_core.run_form` (2026-09-09) and declares only what genuinely
differs from the tag form: how an agent row looks, which keys add or remove
on a row, the members panel, and the empty-membership complaint. An earlier
version of this docstring recorded the opposite verdict — "every key here is
dual-purpose, so one abstraction over two key semantics would cost more than
the copies it saved" — and kept its own copy of the cursor, the field key
map and the layout. That verdict was wrong on its premise: the tag form's
keys are dual-purpose in exactly the same way (Space types into a focused
field, toggles on a row), and both forms branched `field focused? → text
meaning : row meaning` identically — only the ROW half differed. The copies
then drifted twice (the really-done? placement, 2026-09-02; the top-aligned
"no members" complaint and a cursor one line high, 2026-09-09), which is the
cost the verdict had not priced. The scaffold owns the branch; a form supplies
the row half as `FormBody.actions`.
"""

from __future__ import annotations

from collections import Counter

from ..cluster.legoset import Pick, add_pick, preview_ids, remove_last
from .form_core import FormBody, TextField, run_form
from .styles import STYLE_AGENT_NAME, UiClass

STYLE_COUNT      = "bold fg:ansigreen"       # ×N chip while the agent is picked
STYLE_COUNT_ZERO = UiClass.STATUS.css        # dim placeholder while it is not
CONFIRM_LABEL = "[ Create cluster ]"
HINT_TEXT = ("Space/+ add another of this agent  •  Backspace/- remove its last"
             "  •  Enter create  •  Esc cancel")
EMPTY_WARNING = "no members yet — Space on an agent adds one"
# The ROW half of each key (see FormBody.actions): on a focused text field
# every one of these keeps its text meaning — Space, `+` and `-` type, the
# arrows move — which is the scaffold's rule, not this form's.
ADD_KEYS    = (" ", "+", "right", "c-right")
REMOVE_KEYS = ("backspace", "delete", "-", "left", "c-left")


def prompt_members(agents: list[tuple[str, str]], initial: list[Pick], *,
                   title: str, preamble: list[str] | None = None,
                   fields: list[TextField] | None = None,
                   ) -> tuple[dict[str, str], list[Pick]] | None:
    """Run the membership form; return `(field values, ordered picks)`, or
    None on cancel.

    `agents` is every pickable agent as (name, one-line description) — the
    caller derives it from the registry exactly as the picker's Create rows do.
    `initial` is the prefill (a template's members, or an existing cluster's
    when editing). `fields` render ABOVE the agent list. On an agent row
    Space/`+`/→ add one more of that agent and Backspace/Delete/`-`/← remove
    its last; confirm refuses while the membership is empty (the warning
    window says so) — every other key, the fields' editing, the really-done?
    rule and the layout are `run_form`'s."""
    if not agents:
        raise ValueError("agents must be non-empty")
    # `agents` arrives in picker order (creatable_agents), so its indices ARE
    # the rank the members panel sorts by — the panel then previews the actual
    # window order, not the meaningless pick sequence.
    agent_rank = {name: index for index, (name, _) in enumerate(agents)}
    picks: list[Pick] = list(initial)
    width = max(len(name) for name, _ in agents)

    def agent_rows() -> list[list[tuple[str, str]]]:
        tally = Counter(agent for agent, _ in picks)
        return [[(STYLE_COUNT, f"  ×{count} ") if (count := tally.get(name, 0))
                 else (STYLE_COUNT_ZERO, "   · "),
                 (STYLE_AGENT_NAME, f" {name:<{width}}"),
                 (UiClass.STATUS.css, f"  {description}")]
                for name, description in agents]

    def members_panel(_focused: int | None) -> list[tuple[str, str]]:
        """The live preview of every id the confirm would create."""
        if not picks:
            return []
        ids = preview_ids(picks, agent_rank)
        out: list[tuple[str, str]] = [("", f"  members ({len(ids)}):  ")]
        for i, identifier in enumerate(ids):
            if i:
                out.append((UiClass.STATUS.css, " · "))
            out.append((STYLE_AGENT_NAME, identifier))
        return out

    def add(row: int) -> None:
        add_pick(picks, agents[row][0])

    def remove(row: int) -> None:
        remove_last(picks, agents[row][0])

    values = run_form(title, preamble, fields, FormBody(
        rows=agent_rows,
        stops=range(len(agents)),
        actions={**dict.fromkeys(ADD_KEYS, add), **dict.fromkeys(REMOVE_KEYS, remove)},
        filler=members_panel,
        warnings=lambda: [] if picks else [EMPTY_WARNING],
        confirm_label=CONFIRM_LABEL,
        hint=HINT_TEXT,
        snapshot=lambda: tuple(picks),
        ready=lambda: bool(picks),
    ))
    return None if values is None else (values, picks)
