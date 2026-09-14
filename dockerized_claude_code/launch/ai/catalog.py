"""Which AI the launcher is RUNNING right now — a key into `agents/ai/`, read
at CALL time by every consumer, never bound into a module constant.

The AIs themselves are tag members of `agents/ai/<key>/` (`tags/ai.py`), so
the tree is the one list; this module holds only the launcher's current
choice. `DEFAULT_AI_KEY` names the member the tree marks `default = true`
(a test holds the two together). The switch (`adding_an_ai.md`, "How the
operator switches AIs") sets the active key from the launched instance —
`set_active_ai(instance.ai.name)` — and a value computed from it at import
would keep the old AI: `tags/engine.py` once bound a filename that way.
"""

DEFAULT_AI_KEY = "claude"

_active: str | None = None


def active_ai_key() -> str:
    """The key of the AI the launcher runs now — the set choice, else the
    tree's default member."""
    return _active or DEFAULT_AI_KEY


def set_active_ai(key: str | None) -> None:
    """Make `key` the running AI for the rest of this launch (None resets to
    the default). Called by the launch path once the instance is known."""
    global _active
    _active = key
