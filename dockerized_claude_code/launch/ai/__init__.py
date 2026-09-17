"""The harness the launcher runs — the code half.

The harnesses (agent CLIs) and the AIs are TAG MEMBERS (`agents/harness/<key>/`
and `agents/ai/<key>/`; `launch/tags/harness.py`, `launch/tags/ai.py`): their
vendors, descriptions, the AIs each CLI runs, colours, tiers per capability
standard and settings vocabulary are tree data. This leaf package holds what
must be code: the launcher's current choice (`catalog`: `active_harness_key()`,
`set_active_harness()`, `DEFAULT_HARNESS_KEY`) and the ADAPTERS — `Adapter`,
the record of an agent CLI's names (binary, flags, config-root files, env
vars, critical hosts), one instance per module (`claude_code`), keyed by the
harness member's name and reached through `adapter_for(key)` /
`active_adapter()`. A LEAF: imports nothing in-project, so every layer may
read it (`paths.py` derives the container's config-root names from the Claude
Code adapter). Adapters for the other harnesses grow here one seam at a time;
plans/adding_an_ai.md is the checklist, plans/harness_commonality.md the
matrix of what splits."""

from .adapter import Adapter, AuthFile
from .catalog import DEFAULT_HARNESS_KEY, active_harness_key, set_active_harness
from .claude_code import CLAUDE_CODE

# Every harness the launcher has an adapter for, by the harness member it
# implements. A member in the tree without one (Gemini CLI, Codex CLI, Grok
# Build today) can be described and stored — budgets render, tiers rank — but
# not yet run.
ADAPTERS: dict[str, Adapter] = {a.key: a for a in (CLAUDE_CODE,)}


def adapter_for(key: str) -> Adapter:
    """The adapter record for the harness `key`, or a LookupError naming the
    gap — never a silent fallback to Claude Code's names for another CLI."""
    try:
        return ADAPTERS[key]
    except KeyError:
        raise LookupError(f"no adapter for the {key!r} harness yet — "
                          f"plans/adding_an_ai.md lists what one needs") from None


def refusal_for(harness_key: str, label: str) -> str | None:
    """Why an instance in the harness `harness_key` cannot launch, or None:
    the one message for run.py's instance launch, the quickie and the
    cluster's member check. A harness without an adapter is refused BEFORE
    any docker work — never run with Claude Code's binary and another CLI's
    settings."""
    if harness_key in ADAPTERS:
        return None
    return (f"  {label} has no adapter yet — the launcher can describe an "
            f"instance in it, not run it.\n  plans/adding_an_ai.md lists what an adapter "
            f"needs; F2 in the picker switches the instance's harness.")


def adopt(harness_key: str | None) -> None:
    """Make the launched instance's harness the running one for the rest of
    this process — `set_active_harness` with the instance's spelling (None:
    the default). Called by each SOLO launch path as soon as its instance is
    resolved and not refused, BEFORE the first harness word is read (the
    settings install, the banner, the title all read `active_adapter()`); a
    cluster adopts none — its members' adapters are resolved per member."""
    set_active_harness(harness_key)


def active_adapter() -> Adapter:
    """The adapter for the harness the launcher runs right now (`active_harness_key()`)."""
    return adapter_for(active_harness_key())


__all__ = ["ADAPTERS", "Adapter", "AuthFile", "CLAUDE_CODE", "DEFAULT_HARNESS_KEY", "active_adapter", "active_harness_key",
           "adopt", "adapter_for", "refusal_for", "set_active_harness"]
