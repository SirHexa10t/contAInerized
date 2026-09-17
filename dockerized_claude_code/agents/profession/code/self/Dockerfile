# Launcher-development image — extends its PARENT_IMAGE (default
# claude-agents:code) with the launcher's OWN dev toolchain, so an agent whose
# workspace is this repository passes `bash check.sh` without per-session
# installs. Built when an instance has the [self] profession set.
#
# WHY a layer at all: containers run `--rm` and ~/.local is not a mount, so
# anything pip-installed at runtime vanishes on relaunch — observed repeatedly
# as "mypy keeps disappearing while ruff survives" (ruff is baked into [code];
# mypy was baked nowhere). ISSUES.md records that confusion, and rejects baking
# these into the general-purpose [code] image because most instances never
# touch this repo. A profession layer is the fix with the right blast radius:
# only [self] instances pay for it.
#
# Contents mirror pyproject.toml's [project] dependencies (the runtime imports
# the unit tests need) plus the one gate checker [code] doesn't provide:
#   - prompt_toolkit / / rich → pip3 --user as the claude user
#     (PEP 668 image, hence --break-system-packages; lands in ~/.local inside
#     the image, which no runtime mount shadows)
#   - mypy → uv tool install, the same pattern as ruff in [code] and
#     playwright in [webdev]
# Unpinned deliberately: the gate should see what a fresh `pip install` sees;
# --refresh-installs keeps "fresh" honest on demand.

ARG PARENT_IMAGE=claude-agents:code
FROM ${PARENT_IMAGE}

# FORCE_INSTALLS_REFRESH (--refresh-installs) keys the cache — referenced in
# the no-op `:`. Never the weekly SOFTWARE_STACK_REFRESH: that is the harness
# layer's word (built last), and a weekly buster here cascaded into every
# specialty layer above until 2026-09-16.
ARG FORCE_INSTALLS_REFRESH=stable
RUN : "${FORCE_INSTALLS_REFRESH}" \
 && pip3 install --user --break-system-packages prompt_toolkit rich \
 && uv tool install mypy
