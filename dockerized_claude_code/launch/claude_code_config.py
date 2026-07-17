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
run_container (terminal title); nothing else does."""

from .file_access import read_json_field
from .paths import ACCOUNT_FILE
from .tags import Instance, PolicyStance, Tag

# Field-driven tag colors for the status line — same dispatch as the picker's
# `_tag_style` (menu_picker), in raw ANSI: warn-flagged specialties bright
# red; policies by stance (DENY blue, ALLOW orange, OBLIGATION bold white);
# everything else bright green.
_WARN_ANSI = "\033[01;91m"
_SAFE_ANSI = "\033[22;92m"
_RESET_ANSI = "\033[0m"
_STANCE_ANSI = {
    PolicyStance.ALLOW:      "\033[38;5;208m",
    PolicyStance.DENY:       "\033[01;94m",
    PolicyStance.OBLIGATION: "\033[01;97m",
}


def _tag_ansi(tag: Tag) -> str:
    if getattr(tag, "warn", False):
        return _WARN_ANSI
    stance = getattr(tag, "stance", None)
    if stance is not None:
        return _STANCE_ANSI[stance]
    return _SAFE_ANSI


def colored_tag_chain(tags: tuple[Tag, ...]) -> str:
    """Space-separated ANSI-colored tag labels (see _tag_ansi for the color
    dispatch). Each label self-resets so consecutive labels don't bleed."""
    return " ".join(f"{_tag_ansi(t)}{t.label}{_RESET_ANSI}" for t in tags)


def build_status_line(inst: Instance) -> str:
    """ANSI label for Claude Code's bottom status line — cyan agent + grey
    workspace + green email + blue instance (`<agent>__<session>`), with the
    active tag chain (warning-aware reds + greens) trailing. The `<email> :`
    prefix drops out when .claude.json is missing or lacks a recognisable
    email field."""
    CYAN, BLUE, GREEN, GREY, RESET = "\033[36m", "\033[34m", "\033[32m", "\033[90m", "\033[0m"
    def cap(name: str) -> str:
        return name.replace('-', ' ').replace('_', ' ').title()

    email = read_json_field(ACCOUNT_FILE, "oauthAccount", "emailAddress")
    chain = colored_tag_chain((*inst.professions, *inst.specialties, *inst.policies))
    # Whole prefix (email + separator) drops out when the field is absent —
    # interpolating the raw lookup would render the literal string "None".
    email_part = f"{GREEN}{email}{RESET} : " if email else ""
    return (f"{CYAN}● {cap(inst.agent)} - {cap(inst.session)} {GREY}( {inst.workspace} ){RESET}"
            f"\t\t{email_part}{BLUE}{inst.instance}{RESET}"
            f"  {chain}")


def set_terminal_title(name: str) -> None:
    """Send an OSC 0 escape so the terminal emulator's window/tab title
    becomes `Claude Code — <name>`. Helps the user tell concurrent agent
    tabs apart. Called by docker_config.run_container just before exec'ing
    the container."""
    print(f"\033]0;Claude Code — {name}\007", end="", flush=True)
