"""Per-launch CLAUDE.md addendum text + composer.

A `## Launch-time addendums` section, built from `### <title>` sub-sections,
appended to the agent's source `.md` at install time — keyed by tag **name**
and driven by an instance's `chain` (`["base", <professions…>,
<specialties…>]`). Iterating the chain preserves section order (base first).

Kept as code rather than static `addendum.md` files because the content isn't
static: the Credentials notice is computed at import from
`installed_cred_clis()` (which optional creds are present on the host), and
Firewall/Privacy interpolate launcher path/label constants. Empty bodies drop
out; an all-empty result renders nothing (install writes the source `.md`
byte-for-byte).

"""

from typing import NamedTuple

from ..file_access import installed_cred_clis
from ..paths import (
    CLAUDE_CONFIG_IN_CONTAINER, CLAUDE_SUMMARY_IN_CONTAINER,
    FIREWALL_WHITELIST_FILE, state_domain_resolve_status_path,
)


class Addendum(NamedTuple):
    """One addendum: a `title` (rendered `### <title>`) and a markdown `body`
    (verbatim underneath). An empty `body` means inactive this launch —
    filtered out before rendering."""
    title: str
    body: str


ADDENDUM_SECTION_TITLE = "Launch-time addendums"


SEEK_SUMMARY = Addendum(
    "Project summary",
    f"""A comprehensive project summary lives at `{CLAUDE_SUMMARY_IN_CONTAINER}`. `Read` it when the current task would benefit from project context.
If that file is missing or empty, suggest running `/write-summary` to create / populate it.""",
)

FIREWALL_NOTICE = Addendum(
    "Firewall",
    f"""You are currently running with a firewall in place, a fact the user is aware of. Blocked outbound requests surface as `ECONNREFUSED` / `ConnectionRefused` / "Connection refused" from WebFetch, curl, npm install, git clone, etc. (immediate, not a timeout).

**Before bothering the user about a block, check `{state_domain_resolve_status_path(CLAUDE_CONFIG_IN_CONTAINER)}` first.** Brief retries are appropriate for hosts listed under `pending:` (DNS may resolve within seconds). Hosts under `failed:` were unresolvable at launch but are re-attempted every few minutes — retry once more before surfacing them as whitelist offers. Entries under `skipped:` can never work as written (the reason column says why — e.g. IPv6 on a v4-only network); relay the corrective form to the user. Hosts under `wildcard_gaps:` come from `*.` entries only honored for their base host (unknown CDN provider), so a refused subdomain there is expected — surface it to the user.

**If a whitelisted host worked earlier and now refuses**, its DNS answer likely changed (VPN exit swap, CDN rotation). The launcher re-resolves every whitelisted hostname every ~5 minutes and opens newly-reported addresses automatically — retry shortly before escalating.

**If a host you'd expect to reach (not in any section above) still gives `ConnectionRefused`**, inform the user that you may access it if the appropriate domain-name, `*.` wildcard (covers rotating subdomains on known CDNs), or IP/CIDR (tell the user how to discover all appropriate CDN addresses) were added to the host-side file: `{FIREWALL_WHITELIST_FILE}`

**Surface every legitimate block as a whitelist offer** (treat this as `feedback`-type guidance per your auto-memory taxonomy — the user has asked for it directly), even when a separate obstacle exists (JS-rendered SPA, login wall, etc.) and even when an alternative source is available. Mention secondary obstacles and alternatives separately — never as a reason to skip the whitelist offer.""",
)

_installed_clis = installed_cred_clis()
CREDENTIALS_NOTICE = Addendum(
    "Credentials",
    f"The user has provided credentials for the following CLI tools, which are already installed and ready to use: {_installed_clis}"
    if _installed_clis else "",
)

WEB_NOTICE = Addendum(
    "Headless browser",
    """You're in a headless-browser profession — the `playwright` CLI is available for browser automation. Run `playwright install chromium` (or `firefox` / `webkit`) before first use; subsequent `[code][web]` instances share the same browser cache so the install is idempotent and fast when warm. For Python: `uv pip install playwright` in your project venv.""",
)

MAINTAIN_PRIVACY = Addendum(
    "Privacy",
    """**Never persist personal or runtime-environment details into project text.** When writing or editing code comments, docstrings, READMEs, summaries, TODOs, or any file that lives in the project tree, exclude:

- **Personal identifiers** — user emails, names, GitHub handles, system usernames, OAuth account info, API keys / tokens, credential file paths.
- **Operator-environment state** — which CLIs / tools are installed on the current machine, which agents the operator has configured, what `/home/<user>/` looks like, current shell environment variables, mounted paths specific to this session.

A future reader of any persisted text should see the same content regardless of who ran the command. If a fact wouldn't be true for a different operator's clone of the repo, it doesn't belong.

**Exception (rare):** when the user explicitly asks for such a detail to be written, surface an extra confirmation *before* writing it — name the specific personal / environment detail and ask the user to confirm. Issue this confirmation even when running under a permission-bypass mode — the bypass covers routine actions, not embedding identifying information into persistent files.""",
)


# Addendums keyed by tag name — `base` (the always-on chain root) carries the
# universal notices; a tag's addendums activate when its name is in the chain.
# Iteration in `compose` follows chain order, so `base` renders first.
ADDENDUMS_BY_TAG: dict[str, list[Addendum]] = {
    "base":     [SEEK_SUMMARY, MAINTAIN_PRIVACY],
    "code":     [CREDENTIALS_NOTICE],
    "firewall": [FIREWALL_NOTICE],
    "web":      [WEB_NOTICE],
}


def compose(chain: list[str]) -> str:
    """Render the `## Launch-time addendums` section for an instance's `chain`
    (`["base", …]`). One `### <title>` sub-section per non-empty addendum body,
    in chain order. Returns `""` when nothing active — the caller then appends
    nothing and the state-dir CLAUDE.md matches the source `.md` byte-for-byte."""
    sub_sections = [
        f"### {a.title}\n\n{a.body}"
        for name in chain
        for a in ADDENDUMS_BY_TAG.get(name, ())
        if a.body
    ]
    if not sub_sections:
        return ""
    return f"## {ADDENDUM_SECTION_TITLE}\n\n" + "\n\n".join(sub_sections)
