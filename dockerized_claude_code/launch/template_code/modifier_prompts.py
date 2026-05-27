"""Per-modifier Y/N prompt copy. The headers and bodies that drive
`prompt_modifier` (in agent_modifiers_handler) — one entry per
opt-in mode. Pure data: no logic, no I/O. Adding a new mode means
appending a member to InstanceModifiers and adding a corresponding
entry here.

Lives under `launch/template_code/` (alongside memory_addendums.py)
because it's user-facing copy keyed by modifier — same shape as the
launch-time CLAUDE.md addendums."""

from ..structs import InstanceModifiers


# {modifier: (header, body)} — body is a list of explanation lines; empty
# strings render as blank lines for visual separation.
MODIFIER_PROMPTS: dict[InstanceModifiers, tuple[str, list[str]]] = {
    InstanceModifiers.MODE_WARN_AUTO: (
        "Auto / unattended mode?",
        [
            "Lets the agent run continuously without per-action permission prompts",
            "(passes --dangerously-skip-permissions to claude). The container runs",
            "behind an iptables outbound whitelist, so the agent can only reach",
            "Anthropic, GitHub, npm, PyPI, crates.io and DNS — anything else is",
            "dropped at the network layer.",
            "",
            "⚠ Even with the firewall, the agent has full filesystem write access",
            "  in its workspace and can run arbitrary code there. Use only for",
            "  tasks where you trust the agent to act on its own.",
        ],
    ),
    InstanceModifiers.MODE_WARN_DOOD: (
        f"Docker-out-of-Docker ({InstanceModifiers.MODE_WARN_DOOD.value}) mode?",
        [
            "This is for agents that need to run their own Docker containers",
            "(e.g., to test a project that uses docker compose). Without it,",
            "the agent can't reach the host's Docker daemon.",
            "",
            f"⚠ Avoid unless you actually need it. {InstanceModifiers.MODE_WARN_DOOD.value} bind-mounts",
            "  /var/run/docker.sock, which gives the container effective root",
            "  on the host (it can start any container as root, read host",
            "  paths via volume mounts, etc.).",
        ],
    ),
    InstanceModifiers.MODE_WEB: (
        f"Headless browser ({InstanceModifiers.MODE_WEB.value}) mode?",
        [
            "For agents that need to drive a real browser — web scraping, UI",
            "testing, dynamic-page content extraction. The playwright CLI and",
            "its system libs are installed in the image (~30MB add). Browser",
            "binaries download on first use with `playwright install chromium`",
            "(or firefox / webkit) — landing in the shared [code] cache, so",
            "subsequent [code][web] instances reuse them.",
            "",
            "First [code][web] launch on a fresh host has a ~30s one-time",
            "browser download. No display server needed — chromium runs",
            "headless. Python bindings install per-project with `uv pip",
            "install playwright` (~5MB).",
        ],
    ),
}
