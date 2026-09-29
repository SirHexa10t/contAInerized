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
# the unit tests need), the one gate checker [code] doesn't provide, and the
# library one test drives (Gemini CLI's policy engine, below):
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

# Gemini CLI's policy engine, as a LIBRARY for one test
# (launch/tests/test_gemini_policy_engine.py): it runs the rules the launcher
# renders through the CLI's own engine, which no other test can stand in for —
# and a check that did not run is not a check that passed. Pinned to
# launch/tests/probes/gemini-cli.version, the version policy.mapping was
# verified against (the test fails on any other, so a bump here cannot pass
# without re-verifying). Under /opt and off PATH: nothing runs it as a CLI, and
# a gemini-cli harness layer's own `gemini` stays the only one a shell finds.
# Resilient like the optional installs: a failed install warns, and the test —
# told by the ENV below that the bundle was promised — FAILS rather than skips.
USER root
RUN : "${FORCE_INSTALLS_REFRESH}" \
 && npm install --prefix /opt/gemini-cli-test --no-audit --no-fund @google/gemini-cli@0.61.0 \
 || echo "  ⚠ @google/gemini-cli (the policy-engine test's library) failed to install; that test will fail" >&2
ENV LAUNCHER_TEST_GEMINI_BUNDLE=/opt/gemini-cli-test/node_modules/@google/gemini-cli/bundle
USER claude
