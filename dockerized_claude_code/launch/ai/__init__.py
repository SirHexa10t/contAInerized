"""The AI the launcher runs, and the CLI that runs it — the code half.

The AIs themselves are TAG MEMBERS (`agents/ai/<key>/`, `launch/tags/ai.py`):
their vendor, colours, tiers per capability standard and settings vocabulary
are tree data.
This leaf package holds what must be code: the launcher's current choice
(`catalog`: `active_ai_key()`, `set_active_ai()`, `DEFAULT_AI_KEY`) and the
harness adapters — `Harness`, the record of an agent CLI's names (binary,
flags, config-root files, env vars, critical hosts), one instance per module
(`claude_code`), reached through `harness_for(key)` / `active_harness()`. A
LEAF: imports nothing in-project, so every layer may read it (`paths.py`
derives the container's config-root names from the Claude adapter). Adapters
for the other AIs grow here one seam at a time; plans/adding_an_ai.md is the
checklist, plans/harness_commonality.md the matrix of what splits."""

from .catalog import DEFAULT_AI_KEY, active_ai_key, set_active_ai
from .claude_code import CLAUDE_CODE
from .harness import Harness

# Every harness the launcher has an adapter for, by the AI key it serves. An AI
# in the tree without one (ChatGPT, Gemini, Grok today) can be described —
# budgets render, tiers rank — but not yet run.
HARNESSES: dict[str, Harness] = {h.ai_key: h for h in (CLAUDE_CODE,)}


def harness_for(key: str) -> Harness:
    """The adapter record for the AI `key`, or a LookupError naming the gap —
    never a silent fallback to Claude's names for another AI."""
    try:
        return HARNESSES[key]
    except KeyError:
        raise LookupError(f"no harness adapter for the {key!r} AI yet — "
                          f"plans/adding_an_ai.md lists what one needs") from None


def refusal_for(ai_key: str, label: str) -> str | None:
    """Why an instance on the AI `ai_key` cannot launch, or None: the one
    message for run.py's instance launch and the cluster's member check. An
    AI without an adapter is refused BEFORE any docker work — never run with
    Claude's binary and another AI's settings."""
    if ai_key in HARNESSES:
        return None
    return (f"  {label} has no harness adapter yet — the launcher can describe an "
            f"instance on it, not run it.\n  plans/adding_an_ai.md lists what an adapter "
            f"needs; F2 in the picker switches the instance's AI.")


def adopt(ai_key: str | None) -> None:
    """Make the launched instance's AI the running one for the rest of this
    process — `set_active_ai` with the instance's spelling (None: the store's
    "default"). Called by each SOLO launch path as soon as its instance is
    resolved and not refused, BEFORE the first harness word is read (the
    settings install, the banner, the title all read `active_harness()`);
    a cluster adopts none — its members' harnesses are resolved per member."""
    set_active_ai(ai_key)


def active_harness() -> Harness:
    """The adapter for the AI the launcher runs right now (`active_ai_key()`)."""
    return harness_for(active_ai_key())


__all__ = ["CLAUDE_CODE", "DEFAULT_AI_KEY", "HARNESSES", "Harness", "active_ai_key", "active_harness",
           "adopt", "harness_for", "refusal_for", "set_active_ai"]
