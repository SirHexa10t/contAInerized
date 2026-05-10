"""User-side contributions to agent containers — skills (custom_skills/, <workspace>/.skills/)
and optional credentials (~/.claude-agents/optional_creds/<service>/). Both follow the same
pattern: scan a known host location, return -v flags for whatever's present. Collected here
so run.py can stay focused on docker compose orchestration.

Imports from agent_composition only (PROJECT for custom_skills/, AGENTS_STATE for optional_creds/).
"""

from pathlib import Path

from .agent_composition import AGENTS_STATE, PROJECT


# ============================================================
# Skills — project-bundled (custom_skills/) + per-workspace (.skills/)
# ============================================================

SKILLS_IN_CONTAINER = "/home/claude/.claude/skills"
PROJECT_CUSTOM_SKILLS_DIR = PROJECT / "custom_skills"


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

# Each subpath under ~/.claude-agents/optional_creds/, when present on the host,
# gets bind-mounted to the matching default location inside the container so the
# corresponding CLI (aws/gcloud/gh/etc.) just works. Read-write so cloud CLIs can
# refresh tokens / write cache; presence on host is the opt-in.
OPTIONAL_CREDS_DIR = AGENTS_STATE / "optional_creds"
OPTIONAL_CREDS_MOUNTS = {
    "aws":     "/home/claude/.aws",
    "gcloud":  "/home/claude/.config/gcloud",
    "kube":    "/home/claude/.kube",
    "ssh":     "/home/claude/.ssh",
    "gh":      "/home/claude/.config/gh",
    "glab":    "/home/claude/.config/glab-cli",
    "vercel":  "/home/claude/.local/share/com.vercel.cli",
    "railway": "/home/claude/.config/railway",
    "npmrc":   "/home/claude/.npmrc",
    "pypirc":  "/home/claude/.pypirc",
}
# Each cred dir's presence flips the matching INSTALL_<TOOL>=1 build-arg below. The
# Dockerfile.prog decides what to do with it — most install the matching CLI; some
# entries with no install rule (ssh/npmrc are already covered by base + prog) just
# get the no-op default and a passthrough mount.


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
    """Build the INSTALL_<TOOL>=1|0 env vars Dockerfile.prog reads as build-args, based
    on which optional_creds/<name>/ entries exist on the host. Returns a dict the caller
    merges into os.environ before docker compose build."""
    return {
        f"INSTALL_{name.upper()}": ("1" if (OPTIONAL_CREDS_DIR / name).exists() else "0")
        for name in OPTIONAL_CREDS_MOUNTS
    }


# ============================================================
# Firewall whitelist — ~/.claude-agents/firewall_whitelist.txt
# ============================================================

# User-managed list of extra domains to allow through the {auto} mode firewall,
# alongside the built-in ALLOWED_DOMAINS in docker/init-firewall.sh. One domain
# per line, '#' for comments. compose.auto.yml bind-mounts it into the container;
# init-firewall.sh reads it after its built-in allowlist loop.
FIREWALL_WHITELIST_FILE = AGENTS_STATE / "firewall_whitelist.txt"

_FIREWALL_WHITELIST_TEMPLATE = """\
# User-defined firewall allowlist for {auto} mode.
#
# Each non-empty, non-comment line is treated as a domain to allow alongside
# the built-in list (Anthropic API, GitHub, npm, PyPI, crates.io, plus DNS).
# See docker/init-firewall.sh for the full set of built-ins.
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
    AGENTS_STATE.mkdir(parents=True, exist_ok=True)
    if not FIREWALL_WHITELIST_FILE.exists():
        FIREWALL_WHITELIST_FILE.write_text(_FIREWALL_WHITELIST_TEMPLATE)


def firewall_whitelist_count():
    """Count active domain lines in firewall_whitelist.txt — non-empty after
    stripping inline comments and surrounding whitespace."""
    if not FIREWALL_WHITELIST_FILE.exists():
        return 0
    return sum(1 for line in FIREWALL_WHITELIST_FILE.read_text().splitlines()
               if line.split("#", 1)[0].strip())
