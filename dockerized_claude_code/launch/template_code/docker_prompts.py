"""Docker-side user-facing copy — progress notices + completion prompts.
Pure strings consumed by docker_config.* sites that print them. Lives under
`launch/template_code/`
— same convention: data only, no logic. Adding a new docker-side user-facing
string means adding it here and referencing it from docker_config.

Strings with `{name}` placeholders are `.format()`'d at the use site; each
constant's leading comment names the placeholders it accepts."""

# Build progress — one line per chain step in docker_config.ensure_image.
# Substitutes:
#   {step}    — chain step name, e.g. "code"
#   {target}  — docker tag the step produces, e.g. "claude-agents:code"
BUILDING_STEP = "  Building {step} → {target}..."

# {firewall} — printed in docker_config.run_container while blocking on
# Phase 1 (critical Anthropic addresses).
FIREWALL_WAITING = "  Waiting for critical {firewall} addresses..."

# Install failures — surfaced via prompt_keypress in
# docker_config.prompt_install_failures, between ensure_image and
# The (header, body) pair matches the shape prompt_keypress / prompt_yn
# consume; the body is rendered with one indent under the header.
# Substitutes:
#   {failures}  — header: comma-separated tool names (e.g. "jira, vercel")
#   {instance}  — body:   the per-instance id (e.g. "poet__myproject")
# run_container, and the {step}/{target} names in BUILDING_STEP come from the
# instance's build_steps (base + layer-bearing tags).
INSTALL_FAILURES_HEADER = "⚠ Failed installs: {failures}"
INSTALL_FAILURES_BODY: list[str] = [
    "Could be a networking issue. Perhaps the providers are down, or blocked your IP",
    "To retry the installation, re-run with --refresh-installs:",
    "  python3 run.py {instance} --refresh-installs",
]
