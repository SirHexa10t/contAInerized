"""Claude-Code-side experience configuration — pieces of the in-container UX
the launcher controls from the host side.

  - `build_status_line(inst_id)` — pre-styled ANSI string the launcher
    forwards via the AGENT_STATUS_LINE env var so Claude Code renders a
    cyan-agent / grey-workspace / green-email / blue-instance status line
    at the bottom of its session.
  - `set_terminal_title(name)` — emits an OSC 0 escape so the terminal
    emulator's window/tab title becomes `Claude Code — <name>`, letting
    the user tell concurrent agent tabs apart at a glance.

These don't fit `docker_config` (which owns docker orchestration — builds,
runs, mounts, env-var staging) or `menu_picker` (which owns the launcher's
own picker/prompt UI, before the container starts). They're Claude-Code-side
display concerns that happen to be staged from the host.

Leaf-shaped: imports paths (ACCOUNT_FILE) and file_access (read_json_field)
only. docker_config calls into here from set_container_env (status line) and
run_compose (terminal title); nothing else does."""

from .file_access import read_json_field
from .paths import ACCOUNT_FILE


def build_status_line(inst_id) -> str:
    """ANSI label for Claude Code's bottom status line — cyan agent + grey
    workspace + green email + blue instance (`<agent>__<session>`). The
    `<email> :` prefix drops out when .claude.json is missing or lacks a
    recognisable email field. Accepts any InstanceIdentity (or subclass —
    SessionIdentity works too); only reads .agent / .session / .workspace /
    .instance."""
    CYAN, BLUE, GREEN, GREY, RESET = "\033[36m", "\033[34m", "\033[32m", "\033[90m", "\033[0m"
    session_complete = (f"{inst_id.agent.replace('-', ' ').title()} - {inst_id.session.replace('-', ' ').replace('_', ' ').title()}"
                        f" {GREY}( {inst_id.workspace} ){RESET}")
    mail_at_instance = f"{BLUE}{inst_id.instance}{RESET}"
    email = read_json_field(ACCOUNT_FILE, "oauthAccount", "emailAddress")
    if email:
        mail_at_instance = f"{GREEN}{email}{RESET} : {mail_at_instance}"
    return f"{CYAN}● {session_complete}\t\t{mail_at_instance}"


def set_terminal_title(name: str) -> None:
    """Send an OSC 0 escape so the terminal emulator's window/tab title
    becomes `Claude Code — <name>`. Helps the user tell concurrent agent
    tabs apart. Called by docker_config.run_compose just before exec'ing
    the container."""
    print(f"\033]0;Claude Code — {name}\007", end="", flush=True)
