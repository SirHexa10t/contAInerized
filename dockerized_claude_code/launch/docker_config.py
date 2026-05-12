"""Docker-side launcher orchestration — everything between "we picked an agent"
and `docker compose run`. The image-build chain (ensure_image), the env-var
setup compose reads at substitution time (set_container_env + the per-key
register_* helpers callers outside this module use to stage compose vars), and
the compose invocation itself (run_compose).

Imports from paths (paths the env exposes) and user_additions (optional-creds
INSTALL_* flags). agent_composition imports the register_* helpers from here
(so its handlers don't touch os.environ directly); run.py is otherwise the
only consumer.
"""

import os
import shutil
import subprocess
import sys
from datetime import date

from .paths import ACCOUNT_FILE, CREDENTIALS_FILE, PROJECT
from .user_additions import optional_creds_install_env, optional_creds_token_env
from .utils import read_json_field

# Weekly cache buster for the Dockerfile's curl-piped downloads (uv, rich-cli,
# Claude Code in `base`; rustup in `prog`). The value flips every Monday, so the
# next build after a week-roll re-fetches those binaries; intra-week builds reuse
# the cache. Bump this manually (e.g. to "force-2026-05-08") to force a refresh
# mid-week without `--no-cache`.
SOFTWARE_STACK_REFRESH = date.today().strftime("%Y-W%W")


def chain_image_tag(chain):
    """The docker image tag for a chain. ['base'] → 'claude-agents:base'.
    ['base', 'prog', 'auto'] → 'claude-agents:prog.auto' (lowercase to match
    the lowercase compose/Dockerfile filenames)."""
    if len(chain) == 1:
        return "claude-agents:base"
    return "claude-agents:" + ".".join(step.lower() for step in chain[1:])


def chain_compose_files(chain):
    """The compose `-f <path>` arg list for a chain. Always includes compose.yml;
    adds compose.<step>.yml (lowercased) for each non-base step in order."""
    args = ["-f", str(PROJECT / "docker" / "compose.yml")]
    for step in chain[1:]:
        args += ["-f", str(PROJECT / "docker" / f"compose.{step.lower()}.yml")]
    return args


def require_docker():
    """Exit early with a clean message if `docker` isn't on PATH. Run.py calls this
    at startup so a missing daemon surfaces as a one-liner instead of a deeper-down
    docker-compose traceback later."""
    if shutil.which("docker") is None:
        sys.exit("docker is required but was not found in PATH.")


def conf_env_args(conf):
    """Convert a per-agent `.conf` dict (from agent_composition.load_conf) into a
    list of `-e KEY=VALUE` args for `docker compose run`. Each conf entry becomes
    a runtime env var inside the container."""
    return [item for k, v in conf.items() for item in ("-e", f"{k}={v}")]


# === Compose env registrations ===
# Each register_* below stages a single env var (or a small group of related
# ones) for docker compose substitution. Callers outside this module use these
# instead of touching os.environ directly, so the launcher↔compose contract
# stays in one place. read_workspace_pref is the symmetric read-side helper
# (for the one host-shell var the launcher reads back, not writes).

def register_target_image(tag):
    """Expose `tag` to compose's $TARGET_IMAGE substitution — the name the
    current `docker compose build`/`run` step labels its output image with."""
    os.environ["TARGET_IMAGE"] = tag


def register_parent_image(tag):
    """Expose `tag` to compose.<step>.yml's $PARENT_IMAGE substitution — what
    each non-base Dockerfile's `FROM ${PARENT_IMAGE}` resolves to per chain step."""
    os.environ["PARENT_IMAGE"] = tag


def register_software_stack_refresh():
    """Expose this week's cache-buster value to compose's $SOFTWARE_STACK_REFRESH
    — flipped weekly so curl-piped Dockerfile installs (uv, rich-cli, Claude Code,
    rustup) re-fetch once per week. Constant for the launcher invocation."""
    os.environ["SOFTWARE_STACK_REFRESH"] = SOFTWARE_STACK_REFRESH


def register_agent_state(state_path):
    """Expose the per-instance state dir to compose's $AGENT_STATE substitution —
    bind-mount source for /home/claude/.claude inside the container (where Claude
    Code stores conversation history, projects, and per-agent settings)."""
    os.environ["AGENT_STATE"] = str(state_path)


def register_agent_name(name):
    """Expose the agent's clean name to compose's $AGENT_NAME substitution —
    used in container labels and by the in-container shell prompt."""
    os.environ["AGENT_NAME"] = name


def register_agent_status_line(label):
    """Expose the pre-styled status line to compose's $AGENT_STATUS_LINE — the
    ANSI heading Claude Code shows at the top of the session."""
    os.environ["AGENT_STATUS_LINE"] = label


def register_workspace(workspace):
    """Expose the host-side workspace path to compose's $AI_WORKSPACE — used both
    as the /workspace bind-mount source and as the env var passed into the
    container."""
    os.environ["AI_WORKSPACE"] = workspace


def register_oauth_files():
    """Expose the shared OAuth file paths to compose's $ACCOUNT_FILE and
    $CREDENTIALS_FILE substitutions — bind-mount sources so every container
    inherits a single login session. Both paths are constants in paths — no
    params needed."""
    os.environ["ACCOUNT_FILE"] = str(ACCOUNT_FILE)
    os.environ["CREDENTIALS_FILE"] = str(CREDENTIALS_FILE)


def register_install_creds_flags():
    """Expose the $INSTALL_<TOOL>=1|0 build-args for Dockerfile.prog — one flag
    per recognised optional cred dir on the host, flipped on by presence. Source
    of truth for which tools/flags exist: optional_creds_install_env in user_additions."""
    os.environ.update(optional_creds_install_env())


def register_optional_creds_tokens():
    """Expose the API tokens read from `optional_creds/<name>/token` files as env
    vars (e.g. `$JIRA_API_TOKEN`) so compose can forward them into the container.
    Source of truth: OPTIONAL_CREDS_TOKEN_ENV_VARS in paths. Tokens stay in
    os.environ rather than on the docker compose command line, so they don't
    leak through `ps auxe` on the host."""
    os.environ.update(optional_creds_token_env())


def register_whitelist_domains(domains):
    """Expose `domains` to compose.auto.yml's $WHITELIST_DOMAINS substitution so
    init-firewall.sh inside the container can iterate it. Caller supplies the
    already-resolved list (built-ins + user entries + www counterparts); the
    join format compose reads is internal here."""
    os.environ["WHITELIST_DOMAINS"] = " ".join(domains)


def register_docker_gid(gid):
    """Expose `gid` to compose.dood.yml's $DOCKER_GID substitution, used as a
    build-arg in Dockerfile.dood so the in-container claude user can read the
    bind-mounted /var/run/docker.sock."""
    os.environ["DOCKER_GID"] = gid


def read_workspace_pref():
    """Read the host shell's $AI_WORKSPACE (the user's optional preferred default
    workspace) — returns None if unset. Distinct from register_workspace, which
    writes the same key as a compose substitution; this one reads what the user
    set in their shell."""
    return os.environ.get("AI_WORKSPACE")


# === Orchestration ===

def _build_status_line(agent, session, workspace):
    """ANSI label for Claude Code's bottom status line — cyan agent + grey
    workspace + green email + blue instance (`<agent>__<session>`). The
    `<email> :` prefix drops out when .claude.json is missing or lacks a
    recognisable email field."""
    CYAN, BLUE, GREEN, GREY, RESET = "\033[36m", "\033[34m", "\033[32m", "\033[90m", "\033[0m"
    session_complete = (f"{agent.replace('-', ' ').title()} - {session.replace('-', ' ').replace('_', ' ').title()}"
                        f" {GREY}( {workspace} ){RESET}")
    mail_at_instance = f"{BLUE}{agent}__{session}{RESET}"
    email = read_json_field(ACCOUNT_FILE, "oauthAccount", "emailAddress")
    if email:
        mail_at_instance = f"{GREEN}{email}{RESET} : {mail_at_instance}"
    return f"{CYAN}● {session_complete}\t\t{mail_at_instance}"


def set_container_env(agent, session, workspace, state_path):
    """Stage all per-launch compose vars in one shot — called by run.py before
    docker compose build/run. Each register_* called below has its own docstring
    covering the specific compose key it sets."""
    register_software_stack_refresh()
    register_agent_state(state_path)
    register_agent_name(agent)
    register_agent_status_line(_build_status_line(agent, session, workspace))
    register_workspace(workspace)
    register_oauth_files()
    register_install_creds_flags()
    register_optional_creds_tokens()


def ensure_image(chain):
    """Build each step in the chain sequentially. Each step's image is tagged
    according to chain_image_tag(chain[:i+1]); PARENT_IMAGE for non-base steps
    points to the prior step's tag so each Dockerfile's `FROM ${PARENT_IMAGE}`
    resolves to a freshly-built parent. Each build invocation uses only
    compose.yml + the step's own compose file (intermediates aren't included
    so their build-args don't surface in unrelated Dockerfile builds)."""
    prev_tag = None
    for i, step in enumerate(chain):
        target = chain_image_tag(chain[:i + 1])
        compose_files = chain_compose_files(["base"] if step == "base" else ["base", step])
        register_target_image(target)
        if prev_tag:
            register_parent_image(prev_tag)
        print(f"  Building {step} → {target}...")
        ret = subprocess.call(["docker", "compose"] + compose_files + ["build"])
        if ret != 0:
            sys.exit(ret)
        prev_tag = target


def run_compose(chain, instance, claude_args, resume_flag, volume_args, skill_mounts, cred_mounts, conf):
    """Build each image in the chain, set TARGET_IMAGE so compose's `image:`
    substitutes to the chain output, set the terminal title, then exec
    `docker compose run`. sys.exits with the container's return code."""
    ensure_image(chain)
    register_target_image(chain_image_tag(chain))
    compose_args = chain_compose_files(chain)
    print(f"\033]0;Claude Code — {instance}\007", end="", flush=True)
    cmd = (
        ["docker", "compose"] + compose_args + ["run", "--rm", "-it"]
        + volume_args             # tag- and mode-contributed mounts ([prog] caches, etc.; DooD socket / auto firewall live in their compose layers)
        + skill_mounts            # -v flags surfacing each skill from custom_skills/ + workspace's .skills/
        + cred_mounts             # -v flags surfacing each recognized service in optional_creds/
        + conf_env_args(conf)     # -e flags setting each per-agent conf key=value in the container
        + ["claude-code"]
        + resume_flag             # present if a resumed session
        + claude_args             # leftover argv (unrecognised flags + unresolved positional) → claude
    )
    sys.exit(subprocess.call(cmd))
