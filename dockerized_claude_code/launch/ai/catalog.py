"""Which HARNESS the launcher is RUNNING right now — a key into
`agents/harness/`, read at CALL time by every consumer, never bound into a
module constant.

The harnesses themselves are tag members (`agents/harness/<key>/`,
`tags/harness.py`) and the AIs likewise (`agents/ai/`), so the tree is the one
list; this module holds only the launcher's current choice. `DEFAULT_HARNESS_KEY`
names the default AI's default harness (a test holds the two together). Each
solo launch path adopts the resolved instance's harness (`launch/ai.adopt`) as
soon as it is known; a value computed from it at import would keep the old
harness: `tags/engine.py` once bound a filename that way.
"""

DEFAULT_HARNESS_KEY = "claude-code"

_active: str | None = None


def active_harness_key() -> str:
    """The key of the harness the launcher runs now — the set choice, else
    the default AI's default harness."""
    return _active or DEFAULT_HARNESS_KEY


def set_active_harness(key: str | None) -> None:
    """Make `key` the running harness for the rest of this launch (None
    resets to the default). Called by the launch path once the instance is
    known."""
    global _active
    _active = key
