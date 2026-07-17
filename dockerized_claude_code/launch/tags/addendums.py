"""Per-launch CLAUDE.md addendum composition.

A `## Launch-time addendums` section, built from `### <title>` sub-sections,
appended to the agent's source `.md` at install time. Two sources, in order:

  1. BASE_ADDENDUMS — launcher-universal notices (project summary, privacy).
     These belong to no tag, so they live here rather than in a tag.info.
  2. Each active tag's `[addendum]` table from its own `tag.info`, in chain
     order (professions → specialties → policies).

Tag addendum bodies may use `{placeholder}` fields from PLACEHOLDERS below —
launcher-known values a static tag.info can't carry (in-container paths,
which optional creds are present). An addendum whose body references a
placeholder that resolves EMPTY this launch is dropped whole (that's how the
Credentials notice disappears on a creds-less host). The registry validator
rejects unknown placeholder names at scan time (KNOWN_PLACEHOLDERS).
"""

from string import Formatter
from typing import NamedTuple

from ..file_access import installed_cred_clis
from ..paths import (
    CLAUDE_CONFIG_IN_CONTAINER, CLAUDE_SUMMARY_IN_CONTAINER,
    FIREWALL_WHITELIST_FILE, state_domain_resolve_status_path,
)
from .base import Tag


class Addendum(NamedTuple):
    """One addendum: a `title` (rendered `### <title>`) and a markdown `body`
    (verbatim underneath). An empty `body` means inactive this launch —
    filtered out before rendering."""
    title: str
    body: str


ADDENDUM_SECTION_TITLE = "Launch-time addendums"

# Names tag.info addendum bodies may reference. The VALUES are computed per
# launch by _placeholder_values (installed_cred_clis probes the host);
# the registry validator imports this set to reject typo'd names at scan.
KNOWN_PLACEHOLDERS = frozenset({
    "cred_clis",                # space-joined CLIs with optional creds present ('' when none)
    "domain_resolve_status",    # in-container path of the firewall's pending/failed status file
    "firewall_whitelist_file",  # host-side path of the user's whitelist file
})


SEEK_SUMMARY = Addendum(
    "Project summary",
    f"""A comprehensive project summary lives at `{CLAUDE_SUMMARY_IN_CONTAINER}`. `Read` it when the current task would benefit from project context.
If that file is missing or empty, suggest running `/write-summary` to create / populate it.""",
)

MAINTAIN_PRIVACY = Addendum(
    "Privacy",
    """**Never persist personal or runtime-environment details into project text.** When writing or editing code comments, docstrings, READMEs, summaries, TODOs, or any file that lives in the project tree, exclude:

- **Personal identifiers** — user emails, names, GitHub handles, system usernames, OAuth account info, API keys / tokens, credential file paths.
- **Operator-environment state** — which CLIs / tools are installed on the current machine, which agents the operator has configured, what `/home/<user>/` looks like, current shell environment variables, mounted paths specific to this session.

A future reader of any persisted text should see the same content regardless of who ran the command. If a fact wouldn't be true for a different operator's clone of the repo, it doesn't belong.

**Exception (rare):** when the user explicitly asks for such a detail to be written, surface an extra confirmation *before* writing it — name the specific personal / environment detail and ask the user to confirm. Issue this confirmation even when running under a permission-bypass mode — the bypass covers routine actions, not embedding identifying information into persistent files.""",
)

# Universal notices — active on EVERY launch, ahead of any tag addendum.
BASE_ADDENDUMS: list[Addendum] = [SEEK_SUMMARY, MAINTAIN_PRIVACY]


def _placeholder_values() -> dict[str, str]:
    """Per-launch values for KNOWN_PLACEHOLDERS. Computed at compose time —
    installed_cred_clis reflects the host's optional_creds/ state NOW, not
    at import."""
    return {
        "cred_clis":               installed_cred_clis(),
        "domain_resolve_status":   str(state_domain_resolve_status_path(CLAUDE_CONFIG_IN_CONTAINER)),
        "firewall_whitelist_file": str(FIREWALL_WHITELIST_FILE),
    }


def referenced_placeholders(body: str) -> set[str]:
    """The `{field}` names a body references (str.format grammar). Used here
    for the empty-value drop rule and by the registry validator to reject
    unknown names at scan time."""
    return {field for _, field, _, _ in Formatter().parse(body) if field}


def _tag_addendums(tags: list[Tag]) -> list[Addendum]:
    """The active tags' addendums, formatted. An addendum referencing a
    placeholder whose value is empty THIS launch is dropped whole — the
    notice would be describing something absent (no creds → no Credentials
    section)."""
    values = _placeholder_values()
    out: list[Addendum] = []
    for tag in tags:
        if tag.addendum is None:
            continue
        title, body = tag.addendum
        referenced = referenced_placeholders(body)
        if any(not values[name] for name in referenced):
            continue
        out.append(Addendum(title, body.format(**values)))
    return out


def compose(tags: list[Tag]) -> str:
    """Render the `## Launch-time addendums` section for an instance's active
    tags (chain order — professions, specialties, policies). Base notices
    first, then one `### <title>` sub-section per tag addendum. Returns `""`
    when nothing is active — the caller then appends nothing and the
    state-dir CLAUDE.md matches the source `.md` byte-for-byte."""
    active = [a for a in (*BASE_ADDENDUMS, *_tag_addendums(tags)) if a.body]
    if not active:
        return ""
    return f"## {ADDENDUM_SECTION_TITLE}\n\n" + "\n\n".join(
        f"### {a.title}\n\n{a.body}" for a in active
    )
