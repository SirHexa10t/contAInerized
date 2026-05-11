#!/usr/bin/env bash
# auto-entrypoint.sh — wrapper invoked as the container entrypoint when an
# instance has the {auto} mode (selected by docker/compose.auto.yml).
#
# Pure plumbing — no flag decisions live here. compose.auto.yml's entrypoint
# field bakes in --dangerously-skip-permissions; this script just runs the
# iptables firewall and forwards every arg to claude.
#
# Flow:
#   1. Run init-firewall.sh as root via sudo. The sudoers entry installed by
#      Dockerfile.auto restricts the claude user to ONLY this exact command,
#      and preserves WHITELIST_DOMAINS across the privilege boundary so the
#      script can read the launcher-supplied allowlist.
#   2. Invalidate sudo's credential cache so a runaway agent can't piggyback
#      on it for additional sudo calls. Defense in depth — sudoers already
#      restricts the scope, but invalidating is cheap and explicit.
#   3. Drop WHITELIST_DOMAINS from the env so claude (and anything it spawns)
#      can't read or replay it. The firewall is already in place; the var has
#      done its job. Re-run protection inside init-firewall.sh would block
#      tampering anyway, but unsetting closes the leak proactively.
#   4. exec claude with whatever args compose passed in (the
#      --dangerously-skip-permissions from compose.auto.yml + run.py's
#      resume_flag/sys.argv passthrough).

set -euo pipefail

sudo /usr/local/bin/init-firewall.sh
sudo -k
unset WHITELIST_DOMAINS

exec claude "$@"
