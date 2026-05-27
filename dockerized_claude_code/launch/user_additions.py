"""User-side contributions to agent containers — optional credentials
(~/.claude-agents/user_extras/optional_creds/<service>/) and the first-launch
plant of the user-facing template files. Skills aren't handled here: bundled
skills (`custom_skills/`) ride along in DOCKER_BASE_MOUNTS as a single dir
mount, and workspace-side skills live at
`<workspace>/.claude/skills/<name>/SKILL.md` for Claude Code's native
discovery (no bind-mount needed for either). This module is a thin facade:
each public function orchestrates calls into file_access (for disk I/O) and
docker_config (for docker-side staging). No file access or arg shape lives
here.

The split exists because the two concerns are independent layers:
  - file_access decides what's present on disk and reads file contents.
  - docker_config owns the docker-side wire shape: env-var dict formatters
    (INSTALL_<TOOL> + token vars) and the add_docker_mount accumulator.
  - This module names the user-facing operations and routes them through
    those two. Adding a new user-contribution kind means one function here +
    its file_access + docker_config helpers.
"""

from .docker_config import add_docker_mount
from .file_access import copy_file, present_optional_cred_services
from .paths import (
    FIREWALL_WHITELIST_FILE, FIREWALL_WHITELIST_TEMPLATE, OPTIONAL_CREDS_MOUNTS,
    OPTIONAL_CREDS_README_PATH, OPTIONAL_CREDS_README_TEMPLATE,
    optional_creds_service_path,
)
from .structs import InstanceModifiers


# ============================================================
# First-launch template files (~/.claude-agents/user_extras/...)
# ============================================================

def plant_user_extras(modes: tuple[InstanceModifiers, ...]) -> None:
    """Idempotently drop the user-facing helper files into ~/.claude-agents/
    user_extras/ so users discovering the directories know what to put in
    them:
      - optional_creds_readme.txt — always; its presence helps users discover
        the optional_creds/ dir whether or not they ever use it.
      - firewall_whitelist.txt — only when {auto} is in `modes`. Outside
        {auto} the file would just sit unused.

    Both copies go through copy_file with its default overwrite_if_dest=False,
    so existing user edits to either file are preserved across re-launches.
    Called by run.py:setup_state once modes are resolved."""
    copy_file(OPTIONAL_CREDS_README_TEMPLATE, OPTIONAL_CREDS_README_PATH)
    if InstanceModifiers.MODE_WARN_AUTO in modes:
        copy_file(FIREWALL_WHITELIST_TEMPLATE, FIREWALL_WHITELIST_FILE)


# ============================================================
# Optional credentials — ~/.claude-agents/user_extras/optional_creds/<service>/
# ============================================================

def optional_creds_mounts() -> list[str]:
    """For each entry under ~/.claude-agents/user_extras/optional_creds/ that
    matches a known service, stage a bind-mount onto the CLI's default config
    path inside the container. Read-write — cloud CLIs refresh tokens / write
    cache. Missing entries are silently skipped — opt-in is via presence on
    the host. Returns the sorted list of mounted service names (for the
    launch banner). Tuple unpack discards the cli-name half — that's a
    [code]-addendum concern, not a mount concern."""
    services = sorted(present_optional_cred_services())
    for name in services:
        mount_target, _ = OPTIONAL_CREDS_MOUNTS[name]
        add_docker_mount(optional_creds_service_path(name), mount_target)
    return services
