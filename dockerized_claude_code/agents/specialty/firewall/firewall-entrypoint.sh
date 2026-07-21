#!/usr/bin/env bash
# firewall-entrypoint.sh — wrapper invoked as the container entrypoint when an
# instance has the {firewall} specialty (declared in ../tag.docker; the
# launcher passes `--entrypoint` on `docker run`).
#
# Pure plumbing — no flag decisions live here. Claude flags ({auto}'s
# --dangerously-skip-permissions, --effort, --continue, user args) all arrive
# as "$@" from the launcher; this script just runs the iptables firewall and
# forwards every arg to claude.
#
# Flow:
#   1. Run init-firewall.sh as root via sudo, handing the launcher-resolved
#      api.anthropic.com IP along as $1 (the DNS-free self-test target; the
#      sudoers entry names the command with no argument list, which permits
#      arguments). The entry restricts the claude user to ONLY this command,
#      and preserves WHITELIST_ADDRESSES across the privilege boundary so the
#      script can read the launcher-supplied whitelist.
#   2. Invalidate sudo's credential cache so a runaway agent can't piggyback
#      on it for additional sudo calls. Defense in depth — sudoers already
#      restricts the scope, but invalidating is cheap and explicit.
#   3. Drop WHITELIST_ADDRESSES and FIREWALL_SELFTEST_ADDR from the env so
#      claude (and anything it spawns) can't read or replay them. The
#      firewall is already in place; the vars have done their job. Re-run
#      protection inside init-firewall.sh would block tampering anyway, but
#      unsetting closes the leak proactively.
#   4. exec claude with whatever args the launcher passed in (effort/resume
#      flags, specialty claude_args like {auto}'s skip-permissions, and the
#      user's argv passthrough).

set -euo pipefail

sudo /usr/local/bin/init-firewall.sh "${FIREWALL_SELFTEST_ADDR:-}"
sudo -k
unset WHITELIST_ADDRESSES FIREWALL_SELFTEST_ADDR

exec claude "$@"
