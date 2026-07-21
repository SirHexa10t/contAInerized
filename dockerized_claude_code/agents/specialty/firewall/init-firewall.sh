#!/usr/bin/env bash
# init-firewall.sh — iptables-based outbound whitelist for the {firewall} specialty.
#
# All whitelist DNS resolution happens on the host, in launch/network.py.
# The pre-resolved entries arrive here as a space-separated
# $WHITELIST_ADDRESSES env var — each one is `<ip>[:port]` or
# `<cidr>[:port]`, ready for iptables -A directly. $1 (optional) is the
# launcher-resolved api.anthropic.com IP: the positive self-test probes it
# via `curl --resolve`, so enforcement verification never depends on the
# container's DNS. The only DNS this script does itself is a lightweight
# nameserver health probe (below) to un-bury a dead first resolver.
#
# Invoked by firewall-entrypoint.sh (this tag dir; bind-mounted alongside)
# on container start, via sudo. The sudoers entry baked into the base image
# restricts claude to ONLY this command, and `Defaults env_keep +=
# "WHITELIST_ADDRESSES"` preserves the env var across the privilege boundary.
#
# Re-run protection: a marker in /var/run blocks any second invocation, so an
# attacker can't set their own WHITELIST_ADDRESSES and reapply a permissive
# firewall after the first (legitimate) run has finished. /var/run is
# root-owned; the marker is created here (running as root via sudo) and the
# claude user can't remove it.
#
# Requirements:
#   - iptables installed (base image)
#   - container started with CAP_NET_ADMIN (this specialty's tag.docker)

set -euo pipefail

# --- Init-once marker -------------------------------------------------------
MARKER=/var/run/init-firewall.applied
if [ -e "$MARKER" ]; then
    echo "init-firewall.sh: firewall already applied for this container; refusing to re-run." >&2
    exit 1
fi
touch "$MARKER"

# --- Nameserver health -------------------------------------------------------
# Docker copies the host's resolv.conf into every container, dead entries and
# all. A VPN kill-switch commonly drops container→LAN DNS while the host
# itself resolves fine — leaving a dead nameserver FIRST in the list, which
# costs glibc's ~5s failover on EVERY fresh lookup the agent makes. Probe
# each nameserver with a raw DNS query (bash /dev/udp, 1s budget) and rewrite
# the file so a responsive one leads. Dead ones stay listed as failover — if
# the network flips again mid-session (VPN down), resolution still works,
# just slowly. The rewrite is in-place (`cat >`): docker bind-mounts
# resolv.conf, so the file can be edited but not replaced (rename → EBUSY).
ns_responds() {   # $1 = nameserver IP → 0 iff it answers a DNS query within 1s
    # Raw A query for api.anthropic.com; tid \x41\x42 keeps byte 1 printable
    # for `read`. Any response byte = alive; parsing is not the point.
    local query='\x41\x42\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x03api\x09anthropic\x03com\x00\x00\x01\x00\x01'
    # Brace group so 2>/dev/null is scoped to the open (a bare `exec N<>…
    # 2>…` would redirect the WHOLE script's stderr for good — swallowing
    # every diagnostic below); fd 3 itself persists past the group.
    { exec 3<>"/dev/udp/$1/53"; } 2>/dev/null || return 1
    if ! printf "$query" >&3 2>/dev/null; then exec 3>&- 3<&-; return 1; fi
    if read -r -t 1 -n 1 -u 3; then exec 3>&- 3<&-; return 0; fi
    exec 3>&- 3<&-
    return 1
}

mapfile -t nameservers < <(awk '/^nameserver /{print $2}' /etc/resolv.conf)
if [ "${#nameservers[@]}" -gt 1 ]; then
    alive=(); dead=()
    for ns in "${nameservers[@]}"; do
        if ns_responds "$ns"; then alive+=("$ns"); else dead+=("$ns"); fi
    done
    if [ "${#alive[@]}" -gt 0 ] && [ "${#dead[@]}" -gt 0 ] && [ "${nameservers[0]}" != "${alive[0]}" ]; then
        {
            grep -v '^nameserver ' /etc/resolv.conf || true
            printf 'nameserver %s\n' "${alive[@]}" "${dead[@]}"
        } > /tmp/resolv.conf.reordered
        cat /tmp/resolv.conf.reordered > /etc/resolv.conf
        rm -f /tmp/resolv.conf.reordered
        echo "init-firewall.sh: nameserver(s) ${dead[*]} unresponsive from this container (VPN kill-switch?);"
        echo "  reordered resolv.conf to lead with ${alive[0]} — the dead-first order costs ~5s per DNS lookup."
    fi
fi

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

# Positive test: api.anthropic.com SHOULD be reachable through the applied
# rules. The launcher pre-resolved it on the host and hands the IP in as $1 —
# `--resolve` pins curl to that address, so container DNS plays NO part in
# this probe. (The old hostname probe conflated the two: a dead first
# nameserver in docker-copied resolv.conf made DNS failover eat the whole
# 5s connect budget and abort perfectly healthy launches.)
SELFTEST_HOST="api.anthropic.com"
SELFTEST_ADDR="${1:-}"
probe_rc=0
if [ -n "$SELFTEST_ADDR" ]; then
    curl --connect-timeout 5 -s -o /dev/null -I \
         --resolve "${SELFTEST_HOST}:443:${SELFTEST_ADDR}" "https://${SELFTEST_HOST}" || probe_rc=$?
else
    # Bare invocation without the launcher (no $1): fall back to a hostname
    # probe with a budget that survives one dead-nameserver failover (~5s).
    curl --connect-timeout 15 -s -o /dev/null -I "https://${SELFTEST_HOST}" || probe_rc=$?
fi
if [ "$probe_rc" -ne 0 ]; then
    echo "init-firewall.sh: ERROR: ${SELFTEST_HOST}${SELFTEST_ADDR:+ (${SELFTEST_ADDR})} unreachable through the firewall (curl exit ${probe_rc})." >&2
    case "$probe_rc" in
        7)  echo "  Exit 7 = connection refused: our own REJECT rule answered, so the probed" >&2
            echo "  address is NOT covered by the applied whitelist — launcher staging fault, or" >&2
            echo "  a host-side resolve that shifted between staging and start. Re-launch; if it" >&2
            echo "  persists, compare \$WHITELIST_ADDRESSES against the probed address." >&2 ;;
        28) echo "  Exit 28 = timeout: packets LEFT the container and nothing answered. That is" >&2
            echo "  an upstream/network problem (VPN tunnel down? host offline?), not a firewall" >&2
            echo "  fault — the rules let the traffic through." >&2 ;;
        *)  echo "  See 'man curl' EXIT CODES. The probe used a launcher-resolved address, so" >&2
            echo "  container DNS is only a factor if the no-\$1 hostname fallback was in use." >&2 ;;
    esac
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
