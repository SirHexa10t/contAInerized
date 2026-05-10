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
#      Dockerfile.auto restricts the claude user to ONLY this exact command.
#   2. Invalidate sudo's credential cache so a runaway agent can't piggyback
#      on it for additional sudo calls. Defense in depth — sudoers already
#      restricts the scope, but invalidating is cheap and explicit.
#   3. exec claude with whatever args compose passed in (the
#      --dangerously-skip-permissions from compose.auto.yml + run.py's
#      resume_flag/sys.argv passthrough).

set -euo pipefail

sudo /usr/local/bin/init-firewall.sh
sudo -k

exec claude "$@"
