"""User-side contributions to agent containers — skills (custom_skills/ +
<workspace>/.skills/) and optional credentials (~/.claude-agents/user_extras/
optional_creds/<service>/). This module is a thin facade: each public function
orchestrates calls into file_access (for disk I/O) and docker_config (for
docker-side staging). No file access or arg shape lives here.

The split exists because the two concerns are independent layers:
  - file_access decides what's present on disk and reads file contents.
  - docker_config owns the docker-side wire shape: env-var dict formatters
    (INSTALL_<TOOL> + token vars) and the add_docker_mount accumulator.
  - This module names the user-facing operations and routes them through
    those two. Adding a new user-contribution kind means one function here +
    its file_access + docker_config helpers.

The first-launch template-file writers (ensure_optional_creds_readme,
ensure_firewall_whitelist) and the banner-side count (firewall_whitelist_count)
also live in file_access — re-exported here so run.py can pull everything
user-side from one place.
"""

from .docker_config import add_docker_mount, install_creds_flags, token_env_dict
from .file_access import (
    discover_workspace_skills, ensure_firewall_whitelist,
    ensure_optional_creds_readme, firewall_whitelist_count, optional_cred_tokens,
    prepare_skill_mount_dirs, present_optional_cred_services,
)
from .paths import (
    OPTIONAL_CREDS_DIR, OPTIONAL_CREDS_MOUNTS, RO_MOUNT_OPTION, SKILLS_IN_CONTAINER,
)


# ============================================================
# Skills — project-bundled (custom_skills/) + per-workspace (.skills/)
# ============================================================

def aggregated_skills_mounts(workspace, state_path):
    """Surface skills from `custom_skills/` (this project's bundled skills) and
    `<workspace>/.skills/` (the user's per-workspace skills) as the agent's skills
    directory. Each `<name>/SKILL.md` becomes a `/<name>` slash command. When both
    sources have a skill with the same name, the workspace's wins (last-write).
    Both sources are optional; if neither exists, no mounts are staged.

    Mount points are pre-created on the host (under `<state>/skills/<name>/`) as
    the launcher user, so Docker doesn't auto-create them as root — which would
    otherwise leave undeletable directories blocking `delete_instance`'s rmtree.
    Pure side-effect — the count surfaces to the launch banner via staged_mounts()."""
    skills = discover_workspace_skills(workspace)
    prepare_skill_mount_dirs(state_path, skills)
    for name, src in sorted(skills.items()):
        add_docker_mount(src, f"{SKILLS_IN_CONTAINER}/{name}:{RO_MOUNT_OPTION}")


# ============================================================
# Optional credentials — ~/.claude-agents/user_extras/optional_creds/<service>/
# ============================================================

def optional_creds_mounts():
    """For each entry under ~/.claude-agents/user_extras/optional_creds/ that
    matches a known service, stage a bind-mount onto the CLI's default config
    path inside the container. Read-write — cloud CLIs refresh tokens / write
    cache. Missing entries are silently skipped — opt-in is via presence on
    the host. Returns the sorted list of mounted service names (for the
    launch banner)."""
    services = sorted(present_optional_cred_services())
    for name in services:
        add_docker_mount(OPTIONAL_CREDS_DIR / name, OPTIONAL_CREDS_MOUNTS[name])
    return services


def optional_creds_install_env():
    """Build the INSTALL_<TOOL>=0|1 build-arg dict for Dockerfile.prog, keyed
    by presence of each optional_creds/<name>/ entry on the host. Returns the
    dict; docker_config.set_container_env spreads it into the compose env
    accumulator via the bulk dict-update."""
    return install_creds_flags(present_optional_cred_services())


def optional_creds_token_env():
    """Return `{env_var: token_value}` for each optional_creds/<name>/token
    file present on the host, looked up via OPTIONAL_CREDS_TOKEN_ENV_VARS.
    Empty files and services without a token file are silently skipped.
    docker_config.set_container_env spreads this into the compose env
    accumulator; values reach the container via `subprocess.call(env=...)`,
    never written to the launcher's own os.environ (so they don't leak
    through `ps auxe` on the host)."""
    return token_env_dict(optional_cred_tokens())
