"""User-side contributions to agent containers — skills (custom_skills/, <workspace>/.skills/)
and optional credentials (~/.claude-agents/optional_creds/<service>/). Both follow the same
pattern: scan a known host location, return -v flags for whatever's present. Collected here
so run.py can stay focused on docker compose orchestration.

Path constants for the host-side and container-side locations (FIREWALL_WHITELIST_FILE,
OPTIONAL_CREDS_DIR, OPTIONAL_CREDS_MOUNTS, SKILLS_IN_CONTAINER, PROJECT_CUSTOM_SKILLS_DIR)
live in paths.py."""

from pathlib import Path

from .paths import (
    FIREWALL_WHITELIST_FILE, OPTIONAL_CREDS_DIR, OPTIONAL_CREDS_MOUNTS,
    PROJECT_CUSTOM_SKILLS_DIR, SKILLS_IN_CONTAINER,
)
from .utils import parse_lines


# ============================================================
# Skills — project-bundled (custom_skills/) + per-workspace (.skills/)
# ============================================================

def aggregated_skills_mounts(workspace, state_path):
    """Surface skills from `custom_skills/` (this project's bundled skills) and
    `<workspace>/.skills/` (the user's per-workspace skills) as the agent's skills
    directory. Each `<name>/SKILL.md` becomes a `/<name>` slash command. When both
    sources have a skill with the same name, the workspace's wins (last-write).
    Both sources are optional; if neither exists, no mounts are added.

    Mount points are pre-created on the host (under `<state>/skills/<name>/`) as
    the launcher user, so Docker doesn't auto-create them as root — which would
    otherwise leave undeletable directories blocking `delete_instance`'s rmtree."""
    skills = {}  # name -> source path
    for source_dir in (PROJECT_CUSTOM_SKILLS_DIR, Path(workspace) / ".skills"):
        if not source_dir.is_dir():
            continue
        for skill in source_dir.iterdir():
            if skill.is_dir() and (skill / "SKILL.md").is_file():
                skills[skill.name] = skill   # workspace overrides project-bundled
    skills_root_on_host = state_path / "skills"
    args = []
    for name, source in sorted(skills.items()):
        (skills_root_on_host / name).mkdir(parents=True, exist_ok=True)
        args.extend(["-v", f"{source}:{SKILLS_IN_CONTAINER}/{name}:ro"])
    return args


# ============================================================
# Optional credentials — ~/.claude-agents/optional_creds/<service>/
# ============================================================

_OPTIONAL_CREDS_README = """\
(Auto-generated on first launch by run.py — safe to edit or delete; only re-created if missing.)

This directory holds credentials for cloud CLI tools (aws, gcloud, gh, glab, kube, vercel,
railway, etc.) — each subdirectory or file dropped here is bind-mounted into agent
containers at the matching default path, so the corresponding CLI just works.

For the full list of supported services and their target paths, see OPTIONAL_CREDS_MOUNTS
in launch/user_additions.py. For [prog]-tagged agents, the matching CLI is also
auto-installed in the prog image when the cred dir is present (each tool has its own
INSTALL_<TOOL> build-arg).
"""


def ensure_optional_creds_readme():
    """Create ~/.claude-agents/optional_creds/ + a README on first launch, so users who
    discover the directory know what to put in it. Idempotent — won't overwrite user edits."""
    OPTIONAL_CREDS_DIR.mkdir(parents=True, exist_ok=True)
    readme = OPTIONAL_CREDS_DIR / "README.txt"
    if not readme.exists():
        readme.write_text(_OPTIONAL_CREDS_README)


def optional_creds_mounts():
    """For each entry under ~/.claude-agents/optional_creds/ that matches a known service,
    emit a -v flag mounting it to the matching default path inside the container. Returns
    (mount_args, mounted_names). Missing entries are silently skipped — opt-in is via
    presence on the host."""
    args = []
    names = []
    for name, target in OPTIONAL_CREDS_MOUNTS.items():
        host = OPTIONAL_CREDS_DIR / name
        if host.exists():
            args.extend(["-v", f"{host}:{target}"])
            names.append(name)
    return args, names


def optional_creds_install_env():
    """Build the INSTALL_<TOOL>=1|0 build-arg dict for Dockerfile.prog, keyed by
    presence of each optional_creds/<name>/ entry on the host. Returns the dict;
    docker_config.register_install_creds_flags is the caller that surfaces it to
    compose."""
    return {
        f"INSTALL_{name.upper()}": ("1" if (OPTIONAL_CREDS_DIR / name).exists() else "0")
        for name in OPTIONAL_CREDS_MOUNTS
    }


# ============================================================
# Firewall whitelist — user-facing file location + operations
# ============================================================

_FIREWALL_WHITELIST_TEMPLATE = """\
# User-defined firewall allowlist for {auto} mode.
#
# Each non-empty, non-comment line is treated as a domain to allow alongside
# the built-in list (Anthropic API, GitHub, npm, PyPI, crates.io, plus DNS).
# See BUILTIN_FIREWALL_DOMAINS in launch/agent_composition.py for the full set.
#
# - Matching is by IP, not hostname.
# - For an apex domain (`foo.com`, `foo.co.uk`), the launcher auto-adds the `www.` counterpart — both forms get allowed even when they resolve to different IPs (common on CDN-fronted sites).
# - If you type the `www.` form yourself (`www.foo.com`, `www.api.foo.com`), the launcher also registers the non-www counterpart, so both forms are covered.
# - Subdomain entries without `www.` (`api.foo.com`) are passed through as-is — no `www.` prefix is added.
# - Wildcards like `*.foo.com` don't expand — each subdomain you want needs its own line. A leading `*.` is silently stripped, so `*.foo.com` is treated the same as `foo.com` (no crash, but no subdomain coverage either).
#
# Examples — uncomment any line below to grant outbound access on the next
# {auto} launch:
#
# x.com
# news.ycombinator.com
# stackoverflow.com
# docs.python.org
#
# Changes apply on the next {auto} launch — the firewall runs once at container
# start and snapshots the resolved IPs for each domain.
"""


def ensure_firewall_whitelist():
    """Create ~/.claude-agents/firewall_whitelist.txt with a commented preamble on
    first launch so users discovering the file know what to put in it. Idempotent —
    won't overwrite user edits."""
    FIREWALL_WHITELIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not FIREWALL_WHITELIST_FILE.exists():
        FIREWALL_WHITELIST_FILE.write_text(_FIREWALL_WHITELIST_TEMPLATE)


def firewall_whitelist_count():
    """Count active domain lines in the user's firewall_whitelist.txt — for the
    launch banner. Excludes built-ins and the auto-added apex/www counterparts;
    this is "how many entries did the user write themselves"."""
    return sum(1 for _ in parse_lines(FIREWALL_WHITELIST_FILE))
