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

# --- IPv6: deny everything --------------------------------------------------
# The whitelist pipeline is IPv4-only end to end (host-side `getent ahostsv4`
# → v4 iptables rules), so IPv6 can't be selectively opened — and docker
# networks only carry v6 when the daemon opts in. If this container DOES have
# a v6 stack, leaving ip6tables untouched would let any v6-capable host
# bypass the entire whitelist. Slam v6 shut: loopback only, established
# inbound-reply traffic, REJECT the rest. If ip6tables can't apply (kernel
# without v6 netfilter) that's only fatal when a v6 route actually exists —
# a v4-only container has nothing to leak.
if ip6tables -L >/dev/null 2>&1; then
    ip6tables -F
    ip6tables -X
    ip6tables -P INPUT  ACCEPT
    ip6tables -P FORWARD DROP
    ip6tables -P OUTPUT DROP
    ip6tables -A INPUT  -i lo -j ACCEPT
    ip6tables -A OUTPUT -o lo -j ACCEPT
    ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    ip6tables -A OUTPUT  -j REJECT
    ip6tables -A FORWARD -j REJECT
elif ip -6 route show default 2>/dev/null | grep -q .; then
    echo "init-firewall.sh: ERROR: container has an IPv6 default route but ip6tables is" >&2
    echo "  unusable — outbound IPv6 would bypass the IPv4 whitelist entirely. Aborting." >&2
    exit 1
fi

# --- Self-test --------------------------------------------------------------
# Without this, a backend mismatch (rules written but not honored) would
# silently leave the unattended agent free to reach anywhere. Fail loudly so
# the container terminates rather than starting claude on top of a
# non-functional firewall.
echo "init-firewall.sh: testing enforcement..."

# Negative test: 192.0.2.1 (TEST-NET-1, RFC 5737 documentation space) can never
# be legitimately whitelisted — the launcher only emits resolved public IPs and
# provider blocks. A REAL site is unusable as the probe here: CDN widening
# legitimately opens whole provider ranges, and any public host may share edge
# space with a whitelisted one (example.com moved onto a major CDN and started
# failing this test on perfectly healthy firewalls). With the firewall
# enforcing, our REJECT answers instantly and curl exits 7 ("couldn't
# connect"). Anything else means packets are LEAVING: documentation space is
# unrouted, so a non-enforcing firewall shows up as a timeout (exit 28) — or,
# should something actually answer, a success.
probe_rc=0
curl --connect-timeout 3 -s -o /dev/null https://192.0.2.1 || probe_rc=$?
if [ "$probe_rc" -ne 7 ]; then
    echo "init-firewall.sh: ERROR: firewall not enforcing — the probe to reserved address" >&2
    echo "  192.0.2.1 exited $probe_rc, expected 7 (= immediate refusal by our REJECT rule)." >&2
    echo "  Exit 28 (timeout) means the packet escaped the container and died upstream;" >&2
    echo "  exit 0 means something answered — either way outbound traffic is NOT being" >&2
    echo "  filtered despite a default-deny policy and a final REJECT rule." >&2
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

# --- Completion marker --------------------------------------------------------
# Signals the host-side launcher that the base firewall is fully applied AND
# verified. The phase-2 updater (network._updater_worker) polls for this file
# before its first `iptables -I` — without the gate, its rules raced this
# script: inserts landing before the flush above were silently wiped, and
# inserts landing mid-self-test could open provider blocks that made the old
# negative probe's target reachable, failing perfectly healthy launches.
# Mirror of paths.FIREWALL_DONE_IN_CONTAINER — keep in sync.
touch /var/run/init-firewall.done
