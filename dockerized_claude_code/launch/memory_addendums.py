"""Memory addendum text + modifier→addendum mapping + wrapper-marker format.

Each addendum is one block of text that gets spliced into per-instance
MEMORY.md when its associated modifier is active. The blocks live here as
plain string constants (rather than `/workspace/memory/*.md` files) so
per-launch dynamic content and path references can be interpolated directly
from the launcher's path constants + helpers — no separate file to keep in
sync.

The wrapper-marker format also lives here: every wrapped block in MEMORY.md
has `_wrapper_start_line(name)` and `_wrapper_end_line(name)` as its first
and last lines, where `name` is the activating modifier's `.slug` (one
block per modifier, not per addendum). `_wrap_block` composes these around
the joined text; `addendum_text` is the per-modifier getter that
agent_composition.sync_memory_templates calls.

Values are evaluated at module import: `installed_cred_clis()` is queried
then, and CREDENTIALS_NOTICE collapses to '' when no creds are present so
the [prog] block gets suppressed entirely rather than ending in a trailing
colon. addendum_text filters '' entries before joining.
"""

from typing import Callable

from .file_access import installed_cred_clis
from .paths import (
    CLAUDE_CONFIG_IN_CONTAINER, CLAUDE_SUMMARY_IN_CONTAINER,
    FIREWALL_WHITELIST_FILE, state_domain_resolve_status_path,
)
from .structs import InstanceModifiers


# Wrapper banner around `<name>-instructions-{start,end}` marker lines in
# MEMORY.md — visual cue that the line is a machine-managed comment, not
# memory instructions for the agent.
MEMORY_BLOCK_WRAPPER_BANNER = "#" * 21

_wrapper_start_line: Callable[[str], str] = lambda name: f"{MEMORY_BLOCK_WRAPPER_BANNER} {name}-instructions-start {MEMORY_BLOCK_WRAPPER_BANNER}"
_wrapper_end_line:   Callable[[str], str] = lambda name: f"{MEMORY_BLOCK_WRAPPER_BANNER} {name}-instructions-end {MEMORY_BLOCK_WRAPPER_BANNER}"


def _wrap_block(name: str, content: str) -> str:
    """Compose the spliceable block for `name`: banner-wrapped start/end
    marker lines (from the two lambdas above) around `content`. Single
    source of truth for the marker format — every wrapped block in
    MEMORY.md comes through here, and splice_block's marker detection keys
    on these exact lines."""
    return f"{_wrapper_start_line(name)}\n{content.strip()}\n{_wrapper_end_line(name)}"


# === Addendum text constants ===

SEEK_SUMMARY = f"""A comprehensive project summary lives at `{CLAUDE_SUMMARY_IN_CONTAINER}`. `Read` it when the current task would benefit from project context.
If that file is missing or empty, suggest running `/write-summary` to create / populate it."""

FIREWALL_NOTICE = f"""You are currently running in `{InstanceModifiers.MODE_AUTO.label}` mode, a fact the user is aware of. A firewall is in place — blocked outbound requests surface as `ECONNREFUSED` / `ConnectionRefused` / "Connection refused" from WebFetch, curl, npm install, git clone, etc. (immediate, not a timeout).

**Before bothering the user about a block, check `{state_domain_resolve_status_path(CLAUDE_CONFIG_IN_CONTAINER)}` first.** Brief retries are appropriate for hosts listed under `pending:` (DNS may resolve within seconds). Hosts under `failed:` won't resolve this session — surface those as whitelist offers; a re-launch may succeed if the cause was transient.

**If a host you'd expect to reach (not in `pending:` or `failed:`) still gives `ConnectionRefused`**, inform the user that you may access it if the appropriate domain-name or IP/CIDR (tell the user how to discover all appropriate CDN addresses) were added to the host-side file: `{FIREWALL_WHITELIST_FILE}`

**Surface every legitimate block as a whitelist offer** (treat this as `feedback`-type guidance per your auto-memory taxonomy — the user has asked for it directly), even when a separate obstacle exists (JS-rendered SPA, login wall, etc.) and even when an alternative source is available. Mention secondary obstacles and alternatives separately — never as a reason to skip the whitelist offer."""

CREDENTIALS_NOTICE = (
    f"The user has provided credentials for the following CLI tools, which are already installed and ready to use: {installed_cred_clis()}"
    if installed_cred_clis() else ""
)


# Maps each modifier to the addendums it activates. Iteration order in
# sync_memory_templates follows InstanceModifiers declaration order — that's
# what determines block order in the synced MEMORY.md.
MODIFIER_ADDENDUMS = {
    InstanceModifiers.BASE:      [SEEK_SUMMARY],
    InstanceModifiers.TAG_PROG:  [CREDENTIALS_NOTICE],
    InstanceModifiers.MODE_AUTO: [FIREWALL_NOTICE],
}


def addendum_text(modifier: InstanceModifiers) -> str:
    """Return the joined addendum text for `modifier` — separator '\\n\\n\\n',
    empty values filtered out. '' is the 'no spliceable content this launch'
    signal: either the modifier has no addendums in MODIFIER_ADDENDUMS (the
    get() falls back to the empty tuple, which joins to ''), or every
    addendum is empty (e.g. CREDENTIALS_NOTICE collapsed under no-creds).
    Callers treat '' as a skip — splice_block isn't invoked, so neither
    add nor cleanup happens."""
    return "\n\n\n".join(a for a in MODIFIER_ADDENDUMS.get(modifier, ()) if a)
