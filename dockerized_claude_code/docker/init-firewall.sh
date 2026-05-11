#!/usr/bin/env bash
# init-firewall.sh — iptables-based outbound allowlist for the {auto} mode.
#
# Originally vendored from Anthropic's devcontainer reference (MIT-licensed):
#   https://github.com/anthropics/claude-code/tree/main/.devcontainer
# Now diverged: the launcher resolves the allowlist in Python (built-ins +
# user's firewall_whitelist.txt + apex/www counterparts, deduped) and passes
# it in via the WHITELIST_DOMAINS env var. This script just iterates it.
#
# Invoked by docker/auto-entrypoint.sh on container start (via sudo). The
# sudoers entry installed by Dockerfile.auto restricts claude to ONLY this
# command, and `Defaults env_keep += "WHITELIST_DOMAINS"` preserves the env
# var across the privilege boundary.
#
# Re-run protection: a marker in /var/run blocks any second invocation, so an
# attacker can't set their own WHITELIST_DOMAINS and reapply a permissive
# firewall after the first (legitimate) run has finished. /var/run is
# root-owned; the marker is created here (running as root via sudo) and the
# claude user can't remove it.
#
# Image requirements (handled by docker/Dockerfile.auto):
#   - iptables installed
#   - container started with CAP_NET_ADMIN (added by docker/compose.auto.yml)

set -euo pipefail

# --- Init-once marker -------------------------------------------------------
MARKER=/var/run/init-firewall.applied
if [ -e "$MARKER" ]; then
    echo "init-firewall.sh: firewall already applied for this container; refusing to re-run." >&2
    exit 1
fi
touch "$MARKER"

# --- Reset filter chains ----------------------------------------------------
# DON'T flush the nat table — Docker's embedded DNS resolver at 127.0.0.11 is
# a fake address redirected to dockerd via a NAT rule installed inside the
# container's namespace. Flushing nat (`iptables -t nat -F`) destroys that
# redirect; subsequent DNS lookups fail silently, no allowlist entries get
# added, and outbound dies with ConnectionRefused once claude starts.
iptables -F
iptables -X

# Default policies: deny outbound, allow inbound + forward as docker default
iptables -P INPUT  ACCEPT
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

# Loopback — always allowed
iptables -A INPUT  -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

# Established/related — return traffic for our outbound
iptables -A INPUT  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# DNS — required to resolve the domains below
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT

# --- Allowlist --------------------------------------------------------------
# WHITELIST_DOMAINS is space-separated, produced by the launcher via
# user_additions.resolved_whitelist_domains(). Already deduped and apex/www
# expanded — nothing to parse here.

allow_domain() {
    local domain="$1"
    local ips
    ips=$(getent ahosts "$domain" 2>/dev/null | awk '{print $1}' | sort -u || true)
    if [ -z "$ips" ]; then
        echo "init-firewall.sh: warning: '$domain' did not resolve; skipping" >&2
        return
    fi
    for ip in $ips; do
        iptables -A OUTPUT -d "$ip" -p tcp --dport 443 -j ACCEPT
        iptables -A OUTPUT -d "$ip" -p tcp --dport 80  -j ACCEPT
    done
}

domain_count=0
for domain in ${WHITELIST_DOMAINS:-}; do
    allow_domain "$domain"
    domain_count=$((domain_count + 1))
done

# --- Catch-all REJECT -------------------------------------------------------
# Belt-and-suspenders with the `iptables -P OUTPUT DROP` policy — under
# iptables-nft / iptables-legacy backend mismatches, the policy isn't always
# honored, but explicit `-A` rules are. REJECT (vs DROP) returns
# ICMP-port-unreachable so applications fail fast instead of waiting for TCP
# timeout.
iptables -A OUTPUT  -j REJECT --reject-with icmp-port-unreachable
iptables -A FORWARD -j REJECT --reject-with icmp-port-unreachable

# --- Self-test --------------------------------------------------------------
# Without this, a backend mismatch (rules written but not honored) would
# silently leave the unattended agent free to reach anywhere. Fail loudly so
# the container terminates rather than starting claude on top of a
# non-functional firewall.
echo "init-firewall.sh: testing enforcement..."

# Negative test: example.com is NOT in the allowlist; should be unreachable.
if curl --connect-timeout 3 -s -o /dev/null -I https://example.com; then
    echo "init-firewall.sh: ERROR: firewall not enforcing — https://example.com is reachable" >&2
    echo "  despite a default-deny policy and a final REJECT rule." >&2
    echo "" >&2
    echo "  Most likely an iptables backend mismatch. Diagnose inside the container:" >&2
    echo "    iptables -L OUTPUT -n -v   # rules visible to iptables-legacy view" >&2
    echo "    nft list ruleset           # rules visible to nft view" >&2
    echo "  If one shows the rules and the other is empty, the binary writes to a" >&2
    echo "  different backend than the kernel's netfilter dataplane uses. Switching" >&2
    echo "  the script's iptables calls to 'iptables-nft' or 'iptables-legacy' (or" >&2
    echo "  rewriting in 'nft' syntax) is the usual fix." >&2
    exit 1
fi

# Positive test: api.anthropic.com SHOULD be reachable.
if ! curl --connect-timeout 5 -s -o /dev/null -I https://api.anthropic.com; then
    echo "init-firewall.sh: ERROR: api.anthropic.com unreachable through the firewall." >&2
    echo "  The allowlist may have failed to resolve it at startup (check warnings above)." >&2
    exit 1
fi

echo "init-firewall.sh: enforcement verified — allowlist of ${domain_count} domains active, all other outbound rejected."
