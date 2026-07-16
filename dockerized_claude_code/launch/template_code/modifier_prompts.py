"""Per-modifier UI copy. Two mappings:

  MODIFIER_YN_PROMPTS    — per-mode opt-in copy (auto / DooD / web): header +
                           body. Keyed by InstanceModifiers member; consumed
                           by menu_picker's mode form (_mode_form_options) —
                           the header becomes the checkbox row's label (sans
                           the trailing '?'), the body the focused-row
                           explanation panel.

  MODIFIER_NOTICE_PROMPTS — combination warnings. Keyed by a frozenset of
                           modifiers ALL of which must be active; the value's
                           shape mirrors MODIFIER_YN_PROMPTS (header + body).
                           Rendered live by the form's warning zone (above
                           the confirm row, in warning red) while the full
                           combination is checked.

Pure data, no logic and no styling — consumers apply their own. Adding a
new opt-in mode means appending a member to InstanceModifiers + a YN entry
here. Adding a new dangerous combination means appending a NOTICE entry only.

Lives under `launch/template_code/` (alongside memory_addendums.py)
because it's user-facing copy keyed by modifier — same shape as the
launch-time CLAUDE.md addendums."""

from ..structs import InstanceModifiers


# {modifier: (header, body)} — body is a list of explanation lines; empty
# strings render as blank lines for visual separation.
MODIFIER_YN_PROMPTS: dict[InstanceModifiers, tuple[str, list[str]]] = {
    InstanceModifiers.MODE_WARN_AUTO: (
        "Auto / unattended mode?",
        [
            "Lets the agent run continuously without per-action permission prompts",
            "(passes --dangerously-skip-permissions to claude). The container runs",
            "behind an iptables outbound whitelist: ~140 curated developer domains",
            "(Anthropic, package registries, docs sites, cloud/tooling references)",
            "plus your own additions in user_extras/firewall_whitelist.txt — any",
            "other destination is dropped at the network layer. Whitelisted hosts",
            "on a known CDN (Cloudflare/Fastly/GitHub/CloudFront) get their",
            "provider block allowed, so IP rotation can't break them mid-session.",
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


# {modifier-combination: (header, body)} — combination warnings shown while
# the full set is checked in the mode form. Plain text — the form's warning
# zone applies its own (red) styling. Adding a new combination is one entry
# here; no code changes needed.
MODIFIER_NOTICE_PROMPTS: dict[frozenset[InstanceModifiers], tuple[str, list[str]]] = {
    frozenset({InstanceModifiers.MODE_WARN_AUTO, InstanceModifiers.MODE_WARN_DOOD}): (
        f"⚠ YOU'VE ENABLED BOTH {InstanceModifiers.MODE_WARN_AUTO.label} AND {InstanceModifiers.MODE_WARN_DOOD.label} - PROCEED WITH CAUTION,",
        [
            "THE AI AGENT HAS THE POWER TO DO ANYTHING ON YOUR COMPUTER,",
            "AND DOESN'T REQUIRE PERMISSION!",
        ],
    ),
}
