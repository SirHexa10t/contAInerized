# Gemini CLI's image layer — the harness's CLI, its ENTRYPOINT, and the
# image's WEEKLY OS security upgrade. The LAST layer of every chain that runs
# Gemini CLI, with the shared Node layer (profession/_node, claimed in this
# harness's tag.info) directly beneath it. Same shape and reasons as
# harness/claude-code/Dockerfile: the weekly SOFTWARE_STACK_REFRESH rebuilds
# this one layer — the OS upgrade and the CLI's reinstall — and nothing
# beneath it.
#
# The CLI installs from npm as the claude user; NPM_CONFIG_PREFIX (set by the
# Node layer) puts it in ~/.local, whose bin/ the base image has on PATH.
#
# What this layer does NOT do yet, deliberately (plans/adding_an_ai.md, order
# of work step 4): turn off the CLI's self-update, which is a settings.json
# key (general.enableAutoUpdate), not an environment switch — it lands with
# the settings the launcher renders for this harness. Until then the adapter
# is `startable=False` and no container runs this image.
#
# npm blocks install scripts by default in this npm, so node-pty's native
# build is skipped (it needs build tools the image lacks anyway); the CLI then
# runs its shell tool through child_process instead of a PTY.

ARG PARENT_IMAGE=claude-agents:base
FROM ${PARENT_IMAGE}

ARG SOFTWARE_STACK_REFRESH=bootstrap

USER root
RUN : "${SOFTWARE_STACK_REFRESH}" \
 && { apt-get update \
   && apt-get upgrade -y --no-install-recommends \
   && rm -rf /var/lib/apt/lists/*; } \
   || { rm -rf /var/lib/apt/lists/*; echo "  ⚠ OS package upgrade failed; continuing with the cached packages" >&2; }

USER claude
RUN : "${SOFTWARE_STACK_REFRESH}" \
 && npm install -g --no-audit --no-fund @google/gemini-cli

# The ALWAYS-ON policies' denies at this CLI's FIXED tier: a system path it
# reads whatever config root or flags it is started with, so a second copy of
# the CLI the agent starts itself still obeys them (gate fixed-tier; the path
# is policy.mapping's [fixed], the content agents_crud.fixed_policy's). Written
# as root and baked in, never mounted: the container's user can neither edit
# nor replace it, and a bind-mounted dir would present its host owner, which
# Gemini CLI refuses for its policies dir. FIXED_POLICY_B64 sits in this RUN's
# cache key, which is what keeps the bake honest — a changed tag.rules changes
# the arg and rebuilds this layer alone — and that holds because ensure_image
# runs docker build on every launch (gate build-cache declined an image-exists
# shortcut), so never turn the arg into a literal. No arg has a default and a
# missing one FAILS the build (build_arg_flags skips an unstaged arg silently,
# and 0 is a legitimate count). Two guarantees, two checks — trim neither: the
# file EXISTING at its path (on Gemini CLI its .toml name alone nulls a
# workspace's admin policy paths) and its CONTENT, the parsed rule count equal
# to the one the launcher rendered ("non-empty and parses" would also pass a
# file holding only comments). The dir must be root's alone.
ARG FIXED_POLICY_PATH
ARG FIXED_POLICY_B64
ARG FIXED_POLICY_COUNT
USER root
RUN { [ -n "$FIXED_POLICY_PATH" ] && [ -n "$FIXED_POLICY_B64" ] && [ -n "$FIXED_POLICY_COUNT" ]; } \
    || { echo "  ✗ no always-on policy was passed for the fixed tier (FIXED_POLICY_PATH / _B64 / _COUNT)" >&2; exit 1; } \
 && install -d -m 0755 "$(dirname "$FIXED_POLICY_PATH")" \
 && printf '%s' "$FIXED_POLICY_B64" | base64 -d > "$FIXED_POLICY_PATH" \
 && chmod 0644 "$FIXED_POLICY_PATH" \
 && [ "$(stat -c %u:%a "$(dirname "$FIXED_POLICY_PATH")")" = "0:755" ] \
 && python3 -c 'import json, sys, tomllib; p, want = sys.argv[1], int(sys.argv[2]); n = lambda d: sum(map(n, d.values())) if isinstance(d, dict) else (len(d) if isinstance(d, list) else 0); doc = (json.load if p.endswith(".json") else tomllib.load)(open(p, "rb")); got = n(doc) if p.endswith(".json") else len(doc.get("rule", [])); sys.exit(0 if got == want else f"  ✗ {p} holds {got} rules; the launcher rendered {want}")' "$FIXED_POLICY_PATH" "$FIXED_POLICY_COUNT"
USER claude

ENTRYPOINT ["gemini"]
