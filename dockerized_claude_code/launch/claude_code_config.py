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
from .structs import InstanceIdentity, InstanceModifiers


def build_status_line(inst_id: InstanceIdentity) -> str:
    """ANSI label for Claude Code's bottom status line — cyan agent + grey
    workspace + green email + blue instance (`<agent>__<session>`), with the
    modifier chain (warning-aware reds + greens) trailing. The `<email> :`
    prefix drops out when .claude.json is missing or lacks a recognisable
    email field."""
    CYAN, BLUE, GREEN, GREY, RESET = "\033[36m", "\033[34m", "\033[32m", "\033[90m", "\033[0m"
    def cap(name):
        return name.replace('-', ' ').replace('_', ' ').title()

    email = read_json_field(ACCOUNT_FILE, "oauthAccount", "emailAddress")
    chain = InstanceModifiers.colored_chain(inst_id.tags + inst_id.modes)
    return (f"{CYAN}● {cap(inst_id.agent)} - {cap(inst_id.session)} {GREY}( {inst_id.workspace} ){RESET}"
            f"\t\t{GREEN}{email}{RESET}{ ' : ' if email else ''}{BLUE}{inst_id.instance}{RESET}"
            f"  {chain}")


def set_terminal_title(name: str) -> None:
    """Send an OSC 0 escape so the terminal emulator's window/tab title
    becomes `Claude Code — <name>`. Helps the user tell concurrent agent
    tabs apart. Called by docker_config.run_compose just before exec'ing
    the container."""
    print(f"\033]0;Claude Code — {name}\007", end="", flush=True)
