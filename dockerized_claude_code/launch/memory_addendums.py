"""Per-launch CLAUDE.md addendum text + the composer that renders them as a
single markdown section appended to the agent's source `.md` at install time.

Each addendum is an `(title, body)` pair. The active set is determined by which
modifiers are in the session's chain (sess_id.chain); `composed_addendum`
iterates `InstanceModifiers` in declaration order (BASE → tags → modes) so
section ordering in the rendered CLAUDE.md matches modifier precedence, and
emits one `### <title>` sub-section per non-empty addendum body under a single
`## <ADDENDUM_SECTION_TITLE>` heading.

Bodies are evaluated at module import: `installed_cred_clis()` is queried then,
and the Credentials body collapses to `""` when no creds are present so the
sub-section gets suppressed entirely rather than rendering an empty Credentials
heading. Empties drop out at compose time; if every body is empty,
`composed_addendum` returns `""` and the caller appends nothing.

Consumed by `agents_crud.install_latest_md`, which composes
`source .md + "\\n\\n" + composed_addendum(sess_id.chain)` and writes the
state-dir CLAUDE.md in a single pass — no splice, no wrapper markers, no
post-write reconciliation."""

from typing import NamedTuple

from .file_access import installed_cred_clis
from .paths import (
    CLAUDE_CONFIG_IN_CONTAINER, CLAUDE_SUMMARY_IN_CONTAINER,
    FIREWALL_WHITELIST_FILE, state_domain_resolve_status_path,
)
from .structs import InstanceModifiers


class Addendum(NamedTuple):
    """One launch-time addendum: a human-readable `title` (rendered as a
    `### <title>` sub-heading) and a `body` of markdown content (rendered
    verbatim underneath). An empty `body` means the addendum is inactive
    this launch — `composed_addendum` filters it out before rendering."""
    title: str
    body: str


# Rendered as the single `## <title>` heading wrapping the addendum block in
# CLAUDE.md. Kept as a bare title (no `##` prefix) so tests assert on the text
# independent of heading level, and so a future heading-level change is one edit.
ADDENDUM_SECTION_TITLE = "Launch-time addendums"


SEEK_SUMMARY = Addendum(
    "Project summary",
    f"""A comprehensive project summary lives at `{CLAUDE_SUMMARY_IN_CONTAINER}`. `Read` it when the current task would benefit from project context.
If that file is missing or empty, suggest running `/write-summary` to create / populate it.""",
)

FIREWALL_NOTICE = Addendum(
    "Firewall",
    f"""You are currently running in `{InstanceModifiers.MODE_AUTO.label}` mode, a fact the user is aware of. A firewall is in place — blocked outbound requests surface as `ECONNREFUSED` / `ConnectionRefused` / "Connection refused" from WebFetch, curl, npm install, git clone, etc. (immediate, not a timeout).

**Before bothering the user about a block, check `{state_domain_resolve_status_path(CLAUDE_CONFIG_IN_CONTAINER)}` first.** Brief retries are appropriate for hosts listed under `pending:` (DNS may resolve within seconds). Hosts under `failed:` won't resolve this session — surface those as whitelist offers; a re-launch may succeed if the cause was transient.

**If a host you'd expect to reach (not in `pending:` or `failed:`) still gives `ConnectionRefused`**, inform the user that you may access it if the appropriate domain-name or IP/CIDR (tell the user how to discover all appropriate CDN addresses) were added to the host-side file: `{FIREWALL_WHITELIST_FILE}`

**Surface every legitimate block as a whitelist offer** (treat this as `feedback`-type guidance per your auto-memory taxonomy — the user has asked for it directly), even when a separate obstacle exists (JS-rendered SPA, login wall, etc.) and even when an alternative source is available. Mention secondary obstacles and alternatives separately — never as a reason to skip the whitelist offer.""",
)

_installed_clis = installed_cred_clis()
CREDENTIALS_NOTICE = Addendum(
    "Credentials",
    f"The user has provided credentials for the following CLI tools, which are already installed and ready to use: {_installed_clis}"
    if _installed_clis else "",
)

MAINTAIN_PRIVACY = Addendum(
    "Privacy",
    f"""**Never persist personal or runtime-environment details into project text.** When writing or editing code comments, docstrings, READMEs, summaries, TODOs, or any file that lives in the project tree, exclude:

- **Personal identifiers** — user emails, names, GitHub handles, system usernames, OAuth account info, API keys / tokens, credential file paths.
- **Operator-environment state** — which CLIs / tools are installed on the current machine, which agents the operator has configured, what `/home/<user>/` looks like, current shell environment variables, mounted paths specific to this session.

A future reader of any persisted text should see the same content regardless of who ran the command. If a fact wouldn't be true for a different operator's clone of the repo, it doesn't belong.

**Exception (rare):** when the user explicitly asks for such a detail to be written, surface an extra confirmation *before* writing it — name the specific personal / environment detail and ask the user to confirm. Issue this confirmation even when running under a permission-bypass mode like `{InstanceModifiers.MODE_AUTO.label}` — the bypass covers routine actions, not embedding identifying information into persistent files.""",
)


# Maps each modifier to the addendums it activates. Iteration order in
# `composed_addendum` follows InstanceModifiers declaration order — that's what
# determines sub-section order in the rendered CLAUDE.md.
MODIFIER_ADDENDUMS: dict[InstanceModifiers, list[Addendum]] = {
    InstanceModifiers.BASE:      [SEEK_SUMMARY, MAINTAIN_PRIVACY],
    InstanceModifiers.TAG_PROG:  [CREDENTIALS_NOTICE],
    InstanceModifiers.MODE_AUTO: [FIREWALL_NOTICE],
}


def composed_addendum(chain: tuple[str, ...]) -> str:
    """Render the full Launch-time-addendums markdown section for the active
    chain (tuple of modifier `.value`s — `sess_id.chain`). Iterates
    `InstanceModifiers` in declaration order; for each modifier in the chain,
    emits a `### <title>` sub-section per non-empty addendum body. Returns
    `""` when no modifier in the chain has any non-empty addendum body —
    `install_latest_md` treats that as "append nothing" and the resulting
    CLAUDE.md matches the source `.md` byte-for-byte."""
    sub_sections = [
        f"### {a.title}\n\n{a.body}"
        for modifier in InstanceModifiers
        if modifier.value in chain
        for a in MODIFIER_ADDENDUMS.get(modifier, ())
        if a.body
    ]
    if not sub_sections:
        return ""
    return f"## {ADDENDUM_SECTION_TITLE}\n\n" + "\n\n".join(sub_sections)
