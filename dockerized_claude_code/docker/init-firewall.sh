#!/usr/bin/env bash
# init-firewall.sh — iptables-based outbound whitelist for the {auto} mode.
#
# All DNS resolution happens on the host, in launch/network.py:
# resolved_whitelist_domains(). The pre-resolved entries arrive here as a
# space-separated $WHITELIST_ADDRESSES env var — each one is `<ip>[:port]`
# or `<cidr>[:port]`, ready for iptables -A directly. This script just writes
# rules: no DNS, no parallelism, no timeouts to babysit.
#
# Invoked by docker/auto-entrypoint.sh on container start (via sudo). The
# sudoers entry installed by Dockerfile.auto restricts claude to ONLY this
# command, and `Defaults env_keep += "WHITELIST_ADDRESSES"` preserves the env
# var across the privilege boundary.
#
# Re-run protection: a marker in /var/run blocks any second invocation, so an
# attacker can't set their own WHITELIST_ADDRESSES and reapply a permissive
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
# redirect; subsequent DNS lookups by claude (and anything it spawns) fail
# silently, and outbound dies with ConnectionRefused once it starts.
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

# DNS — needed so claude (and tools it spawns) can resolve hostnames AFTER
# the firewall is in place. The whitelist itself doesn't need DNS — the
# launcher pre-resolved everything — but the agent still talks to services
# by hostname and needs the resolver path through to 127.0.0.11 / dockerd.
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT

# --- Whitelist --------------------------------------------------------------
# Each entry is `<ip>[:port]` or `<cidr>[:port]` (pre-resolved by the launcher).
# `:port` opens only that one port; absent → defaults to HTTPS (443) + HTTP (80).
# iptables accepts both raw IPv4 and CIDR ranges natively as -d arguments.
for entry in ${WHITELIST_ADDRESSES:-}; do
    if [[ "$entry" == *:* ]]; then
        ip="${entry%:*}"
        port="${entry##*:}"
        iptables -A OUTPUT -d "$ip" -p tcp --dport "$port" -j ACCEPT
    else
        iptables -A OUTPUT -d "$entry" -p tcp --dport 443 -j ACCEPT
        iptables -A OUTPUT -d "$entry" -p tcp --dport 80  -j ACCEPT
    fi
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

# Negative test: example.com is NOT in the whitelist; should be unreachable.
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

# Positive test: api.anthropic.com SHOULD be reachable. Note this depends on
# the container's DNS now returning an IP that the launcher's host-side resolve
# also saw — if they diverge (CDN POP rotation, etc.), this fails and would be
# the first symptom of the "DNS-pin drift" caveat documented in network.py.
if ! curl --connect-timeout 5 -s -o /dev/null -I https://api.anthropic.com; then
    echo "init-firewall.sh: ERROR: api.anthropic.com unreachable through the firewall." >&2
    echo "  Likely cause: the IP this container's DNS just returned doesn't match what" >&2
    echo "  the launcher's host-side resolve pinned at launch (CDN POP drift). Re-launch" >&2
    echo "  to refresh, or add the tenant explicitly to the user whitelist." >&2
    exit 1
fi
