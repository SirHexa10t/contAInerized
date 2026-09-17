"""Claude-Code-side experience configuration — pieces of the in-container UX
the launcher controls from the host side.

  - `build_status_line(inst_id)` — pre-styled ANSI string the launcher
    forwards via the AGENT_STATUS_LINE env var so Claude Code renders a
    cyan-agent / grey-workspace / green-email / blue-instance status line
    at the bottom of its session.
  - `set_terminal_title(name)` — emits an OSC 0 escape so the terminal
    emulator's window/tab title becomes `Claude Code — <name>`, letting
    the user tell concurrent agent tabs apart at a glance.
  - `print_launch_banner(inst, cred_names)` — the multi-line pre-build
    summary (agent definition, engine, per-axis tag lines, creds, firewall
    whitelist count, unmet-wants warnings) run.py prints before docker
    builds the image.

Everything the launcher renders ABOUT an instance lives here — these don't
fit `docker_config` (docker orchestration — builds, runs, mounts, env-var
staging) or `gui/` (the interactive picker/forms, before a launch is
decided). Small service: imports paths + file_access + utils leaves.
docker_config calls in from set_container_env (status line) and
run_container (terminal title); run.py prints the banner; nothing else
does."""

from collections.abc import Sequence

from .ai import Adapter, active_adapter
from .file_access import home_dir, read_json_field, user_firewall_whitelist_lines, home_relative, login_recorded, is_file
from .paths import DOCKERIZED_CLAUDE_ROOT, FIREWALL_WHITELIST_FILE, auth_file_path, credentials_dir, key_file
from .tags import Ai, Instance, PolicyStance, Tag
from .tags.base import SQUASH_AT
from .utils import plural

# Field-driven tag colors for the status line — same dispatch as the picker's
# `tag_style` (gui.styles), in raw ANSI: warn-flagged specialties bright
# red; policies by stance (DENY blue, ALLOW orange, DEMAND bold white);
# everything else bright green. Each color exists in two forms: the label
# foreground, and the chip BACKGROUND (black glyph on the tag's color) that
# the squashed chain uses — kept as a parallel table because ANSI encodes
# fg and bg as unrelated numbers, so one cannot be derived from the other.
_WARN_ANSI = "\033[01;91m"
_SAFE_ANSI = "\033[22;92m"
_RESET_ANSI = "\033[0m"
_STANCE_ANSI = {
    PolicyStance.ALLOW:      "\033[38;5;208m",
    PolicyStance.DENY:       "\033[01;94m",
    PolicyStance.DEMAND:     "\033[01;97m",
}
_WARN_CHIP_ANSI = "\033[30;101m"
_SAFE_CHIP_ANSI = "\033[30;102m"
_STANCE_CHIP_ANSI = {
    PolicyStance.ALLOW:      "\033[30;48;5;208m",
    PolicyStance.DENY:       "\033[30;104m",
    PolicyStance.DEMAND:     "\033[30;107m",
}


def _tag_ansi(tag: Tag) -> str:
    if getattr(tag, "warn", False):
        return _WARN_ANSI
    stance = getattr(tag, "stance", None)
    if stance is not None:
        return _STANCE_ANSI[stance]
    return _SAFE_ANSI


def _tag_chip_ansi(tag: Tag) -> str:
    """The squashed form's style: black glyph on the tag's usual color."""
    if getattr(tag, "warn", False):
        return _WARN_CHIP_ANSI
    stance = getattr(tag, "stance", None)
    if stance is not None:
        return _STANCE_CHIP_ANSI[stance]
    return _SAFE_CHIP_ANSI


def colored_tag_chain(tags: tuple[Tag, ...]) -> str:
    """The active tags as one ANSI-colored run for the status line.

    Below SQUASH_AT tags: space-separated full labels in each tag's color. At
    SQUASH_AT or more, the labels would crowd out the line's actual content
    (agent, workspace, instance id), so each tag collapses to its
    `squash_glyph` on a chip of its color — same rule, same one-char form, and
    same one-space separation as the picker's tag columns, so the two displays
    teach each other (and two same-colored neighbours read as two tags rather
    than one block). Every piece self-resets so styles don't bleed."""
    if len(tags) >= SQUASH_AT:
        return " ".join(f"{_tag_chip_ansi(t)}{t.squash_glyph}{_RESET_ANSI}"
                        for t in tags)
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

    email = _account_email()
    chain = colored_tag_chain((*inst.professions, *inst.specialties, *inst.policies))
    # Whole prefix (email + separator) drops out when the field is absent —
    # interpolating the raw lookup would render the literal string "None".
    email_part = f"{GREEN}{email}{RESET} : " if email else ""
    return (f"{CYAN}● {cap(inst.agent)} - {cap(inst.session)} {GREY}( {inst.workspace} ){RESET}"
            f"\t\t{email_part}{BLUE}{inst.instance}{RESET}"
            f"  {chain}")


def build_cluster_status_line(inst: Instance, member_id: str) -> str:
    """`build_status_line`'s CLUSTER-member twin — the bottom line in a
    member's pane. Same anatomy and colours, three differences that matter
    once N agents share a container:

    - it leads with the MEMBER ID, verbatim and uncapitalised, because that
      is the name siblings address it by (`ListAgents`, `SendMessage`, the
      queue's `member` field). Two members built from the same agent differ
      only in their role, so the capitalised agent name `build_status_line`
      shows would render them identically;
    - the blue slot carries the CLUSTER's name rather than an instance id —
      `<agent>__<session>` is not a thing here (every member shares the
      session), and the cluster is what the operator is looking at;
    - the workspace shown is the cluster's project.

    Staged per member (each tab's own `--env`), because container-wide env
    could only ever carry one member's line."""
    CYAN, BLUE, GREEN, GREY, RESET = "\033[36m", "\033[34m", "\033[32m", "\033[90m", "\033[0m"
    email = _account_email()
    email_part = f"{GREEN}{email}{RESET} : " if email else ""
    chain = colored_tag_chain((*inst.professions, *inst.specialties, *inst.policies))
    return (f"{CYAN}● {member_id} {GREY}( {inst.workspace} ){RESET}"
            f"\t\t{email_part}{BLUE}{inst.session}{RESET}"
            f"  {chain}")


def set_terminal_title(name: str) -> None:
    """Send an OSC 0 escape so the terminal emulator's window/tab title
    becomes `<CLI> — <name>` (`Claude Code — golem__s1`; the CLI's name is
    the catalog's, so another AI titles its own tabs). Helps the user tell
    concurrent agent tabs apart. Called by docker_config.run_container just
    before exec'ing the container."""
    print(f"\033]0;{active_adapter().name} — {name}\007", end="", flush=True)


def credentials_notice(adapter: Adapter, ai: Ai | None) -> str | None:
    """One plain line before docker runs, when the login state deserves it
    (plans/credentials.md): no key file and every auth file still blank — the
    CLI will ask for a login inside the container, and that login is kept for
    later launches; or a key file beside a stored login — the key wins (Claude
    Code reads ANTHROPIC_API_KEY before its /login session, and asks once
    whether to use it), so a subscription user should remove the key file.
    None when there is nothing to say."""
    if ai is None:
        return None
    key = key_file(ai.name)
    # ALL of them: the post-incident split — account recorded, token blank —
    # is exactly a state where the CLI will prompt (strict-reviewer, gate
    # one-startup).
    logged_in = all((path := auth_file_path(adapter, f.role)) is not None and login_recorded(path, f)
                    for f in adapter.auth_files)
    if is_file(key) and logged_in:
        return (f"  Note: {home_relative(key)} is present — the key takes precedence over the stored "
                f"{adapter.name} login for {ai.label}; remove the file to use the login instead.")
    if not is_file(key) and not logged_in:
        return (f"  Note: no API key file ({home_relative(key)}) and no {adapter.name} login yet — the CLI will ask "
                f"you to log in inside the container; the login is kept under {home_relative(credentials_dir(adapter.key))}/ for later launches.")
    return None


def _account_email() -> str | None:
    """The logged-in account's email from the harness's account file, or None
    (no such file, no such field — the banner then shows no email)."""
    account = auth_file_path(active_adapter(), "account")
    return read_json_field(account, "oauthAccount", "emailAddress") if account else None


def optional_creds_line(cred_names: Sequence[str]) -> str | None:
    """The banner line naming the optional-creds services this container
    mounts, or None when it mounts none — one spelling for the solo banner
    and the cluster's (a cluster carries the operator's creds like every solo
    instance, and says so the same way)."""
    if not cred_names:
        return None
    return f"  Optional creds:   {', '.join(cred_names)} (from user_extras/optional_creds/)"


def print_launch_banner(inst: Instance, cred_names: Sequence[str]) -> None:
    """Print the multi-line summary that appears before docker builds the
    image — agent definition path, engine, one line per active tag axis, and
    creds counts when applicable. Each line is conditional on having
    something to show (no empty 'Professions: ' if there are none). The
    user-whitelist line counts user_firewall_whitelist_lines() inline —
    only when {firewall} is active, so other launches don't touch the file
    at all. Takes the launch's Instance and pulls everything off it directly;
    kind punctuation comes from each tag's `.label`."""
    print(f"  Agent definition: {inst.md_path.relative_to(DOCKERIZED_CLAUDE_ROOT)}")
    if inst.engine:
        model = inst.model
        print(f"  Engine:           {inst.engine.label}{f' {model}' if model else ''} — {inst.engine.path.relative_to(DOCKERIZED_CLAUDE_ROOT)}")
    if inst.professions:
        print(f"  Professions:      {' '.join(p.label for p in inst.professions)}")
    if inst.specialties:
        print(f"  Specialties:      {' '.join(s.label for s in inst.specialties)}")
    if inst.policies:
        print(f"  Policies:         {' '.join(p.label for p in inst.policies)}")
    if (creds_line := optional_creds_line(cred_names)) is not None:
        print(creds_line)
    if any(s.name == "firewall" for s in inst.specialties):
        whitelist_count = len(user_firewall_whitelist_lines())
        display_path = "~/" + str(FIREWALL_WHITELIST_FILE.relative_to(home_dir()))
        print(f"  User whitelist:   {whitelist_count} domain{plural(whitelist_count)} (from {display_path})")
    # Unmet wants — advisory, never blocking: an active tag requested a
    # companion that isn't active (e.g. {auto} without {firewall}). The form
    # shows the same message live; repeating it here catches CLI-named and
    # store-migrated launches that never pass through the form.
    RED, RESET = "\033[01;91m", "\033[0m"
    for wanter, wanted, message in inst.unmet_wants:
        print(f"  {RED}⚠ '{wanter}' wants '{wanted}' (not active):{RESET}")
        for line in message.splitlines():
            print(f"      {RED}{line}{RESET}")
