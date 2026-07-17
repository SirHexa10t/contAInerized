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

from pathlib import Path

from .docker_config import add_docker_mount, mount_target_is_staged
from .file_access import copy_file, enforce_ssh_dir_perms, present_optional_cred_services
from .paths import (
    FIREWALL_WHITELIST_FILE, FIREWALL_WHITELIST_TEMPLATE, OPTIONAL_CREDS_MOUNTS,
    OPTIONAL_CREDS_README_PATH, OPTIONAL_CREDS_README_TEMPLATE,
    optional_creds_service_path,
)
from .tags import Instance


# ============================================================
# First-launch template files (~/.claude-agents/user_extras/...)
# ============================================================

def plant_user_extras(inst: Instance) -> None:
    """Drop the user-facing helper files into ~/.claude-agents/user_extras/
    so users discovering the directories know what to put in them:
      - optional_creds_readme.txt — always; refreshed when the template moves
        on (the file describes launcher behaviour and shouldn't drift behind).
      - firewall_whitelist.txt — only when the firewall machinery is active
        (the {auto} specialty, until the docker flip extracts {firewall}).
        Created on first launch and left alone afterward — it holds the
        user's actual whitelist entries, so their edits are preserved.

    Called by run.py:setup_state once the instance is resolved."""
    copy_file(OPTIONAL_CREDS_README_TEMPLATE, OPTIONAL_CREDS_README_PATH, overwrite_if_changed=True)
    if any(s.name == "auto" for s in inst.specialties):
        copy_file(FIREWALL_WHITELIST_TEMPLATE, FIREWALL_WHITELIST_FILE)


# ============================================================
# Optional credentials — ~/.claude-agents/user_extras/optional_creds/<service>/
# ============================================================

def optional_creds_mounts() -> list[str]:
    """For each entry under ~/.claude-agents/user_extras/optional_creds/ that
    matches a known service, stage a bind-mount onto the CLI's default config
    path inside the container. Read-write — cloud CLIs refresh tokens / write
    cache. Missing entries are silently skipped — opt-in is via presence on
    the host. Returns the sorted list of mounted service names (trailing `/`
    stripped) for the launch banner.

    Two key-shape conventions in OPTIONAL_CREDS_MOUNTS:
      - `<service>`  — bind-mount the source entry as-is to the target path
                       (the existing pattern: aws, gcloud, ssh, etc.)
      - `<name>/`    — contents-mount: top-level entries inside the source
                       dir mount individually into the target dir. Each
                       per-entry mount is collision-checked against
                       previously-staged mounts (`mount_target_is_staged`);
                       a clash raises RuntimeError so the launch exits with
                       a clear "move/rename to proceed" message rather than
                       silently shadowing a launcher-owned mount. Used for
                       `home/` — loose dotfiles that don't belong to a known
                       service.

    Special case — `ssh`: host-side perms are fixed up before mounting (700
    on the dir, 600 on each file, 644 on `*.pub` / `*_hosts`). ssh refuses to
    read keys with looser perms; the user shouldn't have to know that."""
    services = sorted(present_optional_cred_services())
    banner_names: list[str] = []
    for name in services:
        src = optional_creds_service_path(name)
        if name == "ssh":
            enforce_ssh_dir_perms(src)
        mount_target, _ = OPTIONAL_CREDS_MOUNTS[name]
        if name.endswith("/"):
            target_dir = Path(mount_target)
            for entry in sorted(src.iterdir()):
                entry_target = target_dir / entry.name
                if mount_target_is_staged(entry_target):
                    raise RuntimeError(
                        f"optional_creds/{name}{entry.name} would shadow a "
                        f"launcher-staged mount at {entry_target}. Move or rename "
                        f"the {name}{entry.name} entry, or drop the conflicting "
                        f"launcher-side mount, then retry."
                    )
                add_docker_mount(entry, entry_target)
        else:
            add_docker_mount(src, mount_target)
        banner_names.append(name.rstrip("/"))
    return banner_names
