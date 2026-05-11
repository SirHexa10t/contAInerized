#!/usr/bin/env bash
# init-firewall.sh — iptables-based outbound whitelist for the {auto} mode.
#
# Originally vendored from Anthropic's devcontainer reference (MIT-licensed):
#   https://github.com/anthropics/claude-code/tree/main/.devcontainer
# Now diverged: the launcher resolves the whitelist in Python (built-ins +
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
# redirect; subsequent DNS lookups fail silently, no whitelist entries get
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

# --- Whitelist --------------------------------------------------------------
# WHITELIST_DOMAINS is space-separated, produced by the launcher via
# user_additions.resolved_whitelist_domains(). Already deduped and apex/www
# expanded — nothing to parse here.

# Resolution + rule-write runs per-domain in parallel (xargs -P $((nproc * 8))
# — DNS-bound waits scale up generously past core count since each worker is
# almost always sleeping on `getent`) so one slow / dead domain doesn't stall
# the whole list. Each lookup is also capped at 8s by `timeout` so a domain
# whose auth server hangs can't hold its slot indefinitely. Entries that
# already look like a literal IPv4 address or a CIDR range (`1.2.3.4`,
# `10.0.0.0/8`) skip DNS entirely and go straight to iptables — iptables
# accepts both forms natively as the `-d` argument. The firewall is
# iptables (v4-only) and Docker's
# default bridge is v4-only too, so we resolve via `getent ahostsv4` —
# skipping the AAAA query that `ahosts` would otherwise issue alongside the
# A query (slow / hanging AAAA lookups were the main cause of long silent
# stretches during firewall init). `iptables -w 10` waits up to 10s for the
# xtables lock — concurrent workers' -A calls serialise on it cleanly.
# Workers always return 0 (so xargs doesn't trip the script's `set -e`);
# failure is tracked by appending to $FIREWALL_FAILED instead, which the
# parent reads after the parallel pass. For domains that don't resolve, the
# post-loop summary tells the user how to test whether a skipped entry is
# actually IPv6-only.
allow_domain() {
    local domain="$1"
    local ips

    # Literal IPv4 or CIDR — hand straight to iptables, no DNS. The regex is
    # lenient (allows out-of-range octets / prefix lengths); iptables itself
    # rejects anything actually invalid, so we don't pre-validate here.
    if [[ "$domain" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(/[0-9]+)?$ ]]; then
        iptables -w 10 -A OUTPUT -d "$domain" -p tcp --dport 443 -j ACCEPT
        iptables -w 10 -A OUTPUT -d "$domain" -p tcp --dport 80  -j ACCEPT
        return 0
    fi

    ips=$(timeout "$WAIT_SECONDS" getent ahostsv4 "$domain" 2>/dev/null | awk '{print $1}' | sort -u || true)
    if [ -z "$ips" ]; then
        echo "init-firewall.sh: warning: '$domain' did not resolve; skipping" >&2
        echo "$domain" >> "$FIREWALL_FAILED"
    else
        for ip in $ips; do
            iptables -w 10 -A OUTPUT -d "$ip" -p tcp --dport 443 -j ACCEPT
            iptables -w 10 -A OUTPUT -d "$ip" -p tcp --dport 80  -j ACCEPT
        done
    fi
    return 0
}
export -f allow_domain

# Shared failure-tracking file across all parallel workers — `>>` appends are
# atomic for short writes (< PIPE_BUF), so concurrent writes don't corrupt.
FIREWALL_FAILED=$(mktemp)
export FIREWALL_FAILED
trap 'rm -f "$FIREWALL_FAILED"' EXIT

total_count=$(echo ${WHITELIST_DOMAINS:-} | wc -w)
# Tunables for the parallel resolve loop. PARALLELISM scales with cores (DNS-
# bound work — workers sleep on getent, so we go past core count); WAIT_SECONDS
# caps each per-domain lookup so a hanging auth server can't hold its slot.
PARALLELISM=$(( $(nproc) * 8 ))
WAIT_SECONDS=8
export WAIT_SECONDS   # allow_domain runs in child bash workers spawned by xargs; the function sees it through env
echo "init-firewall.sh: resolving whitelist of ${total_count} domains (up to ${PARALLELISM} in parallel, ${WAIT_SECONDS}s timeout each)..."

if [ "$total_count" -gt 0 ]; then
    printf '%s\n' ${WHITELIST_DOMAINS:-} | xargs -n 1 -P "${PARALLELISM}" -I {} bash -c 'allow_domain "$@"' _ {}
fi

fail_count=$(wc -l < "$FIREWALL_FAILED")
ok_count=$((total_count - fail_count))

if [ "$fail_count" -gt 0 ]; then
    echo "init-firewall.sh: resolved ${ok_count}/${total_count} domains; ${fail_count} skipped (see warnings above)."
    echo "init-firewall.sh:   skipped entries may be typos / dead / IPv6-only — the firewall is IPv4-only, so a domain that exists only on IPv6 can't be routed regardless. To test the IPv6 hypothesis for a specific entry, run on the host:  getent ahostsv6 <domain>" >&2
    # Pause so the user has a chance to read the skip list before Claude Code
    # launches and scrolls everything off-screen. `</dev/tty` ensures we read
    # from the controlling terminal even though the script runs under sudo;
    # `|| true` so headless / non-TTY launches (rare — compose runs with -it)
    # don't trip `set -e`.
    read -n 1 -s -r -p "init-firewall.sh:   [press any key to continue] " _ </dev/tty || true
    echo
else
    echo "init-firewall.sh: resolved all ${total_count} domains."
fi

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

# Positive test: api.anthropic.com SHOULD be reachable.
if ! curl --connect-timeout 5 -s -o /dev/null -I https://api.anthropic.com; then
    echo "init-firewall.sh: ERROR: api.anthropic.com unreachable through the firewall." >&2
    echo "  The whitelist may have failed to resolve it at startup (check warnings above)." >&2
    exit 1
fi
# (No success line — the resolve-summary above already reports the counts, and
# the self-test branches exit 1 loudly on failure. Reaching here is success by
# inference.)
