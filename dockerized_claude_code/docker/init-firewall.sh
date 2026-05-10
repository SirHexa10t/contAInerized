#!/usr/bin/env bash
# init-firewall.sh — iptables-based outbound allowlist for the {auto} mode.
#
# Vendored from Anthropic's devcontainer reference (MIT-licensed):
#   https://github.com/anthropics/claude-code/tree/main/.devcontainer
# Anthropic occasionally updates that script as the allowlisted endpoints
# evolve; refresh from upstream periodically (or merge their changes by hand
# below) and keep this header in sync.
#
# Invoked by docker/auto-entrypoint.sh on container start (via sudo). Sets up
# an iptables outbound allow-list so an unattended (--dangerously-skip-permissions)
# agent can only reach domains we trust: Anthropic API, GitHub, npm, PyPI,
# crates.io and DNS. Everything else is dropped at the network layer.
#
# To add domains: append to ALLOWED_DOMAINS below; re-run the {auto} agent
# (the script runs on every container start, so changes apply immediately).
#
# Image requirements (handled by docker/Dockerfile.auto):
#   - iptables installed
#   - container started with CAP_NET_ADMIN (added by docker/compose.auto.yml)

set -euo pipefail

# Reset filter chains. DON'T flush the nat table — Docker's embedded DNS resolver
# at 127.0.0.11 is a fake address redirected to dockerd via a NAT rule installed
# inside the container's namespace. Flushing nat (`iptables -t nat -F`) destroys
# that redirect; subsequent DNS lookups fail silently, no allowlist entries get
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

# Allowlist — resolve each, accept HTTPS (443) and HTTP (80) to the resulting IPs
ALLOWED_DOMAINS=(
    # Anthropic
    "api.anthropic.com"
    "console.anthropic.com"
    "claude.ai"

    # GitHub (git, releases, raw, codeload, container registry)
    "github.com"
    "api.github.com"
    "raw.githubusercontent.com"
    "objects.githubusercontent.com"
    "codeload.github.com"
    "ghcr.io"

    # npm
    "registry.npmjs.org"

    # PyPI
    "pypi.org"
    "files.pythonhosted.org"

    # crates.io (Rust)
    "crates.io"
    "static.crates.io"
    "index.crates.io"
)

# Resolve a single domain and add accept rules for HTTPS/HTTP. Used for both
# the built-in ALLOWED_DOMAINS list above and the user-managed whitelist below.
allow_domain() {
    local domain="$1"
    local source="${2:-built-in}"
    local ips
    ips=$(getent ahosts "$domain" 2>/dev/null | awk '{print $1}' | sort -u || true)
    if [ -z "$ips" ]; then
        echo "init-firewall.sh: warning: '$domain' ($source) did not resolve; skipping" >&2
        return
    fi
    for ip in $ips; do
        iptables -A OUTPUT -d "$ip" -p tcp --dport 443 -j ACCEPT
        iptables -A OUTPUT -d "$ip" -p tcp --dport 80  -j ACCEPT
    done
}

for domain in "${ALLOWED_DOMAINS[@]}"; do
    allow_domain "$domain"
done

# User whitelist — extra domains the user added on the host at
# ~/.claude-agents/firewall_whitelist.txt. compose.auto.yml bind-mounts it to
# the path below; missing file is tolerated defensively (the launcher's
# ensure_firewall_whitelist creates one on first launch and is idempotent after).
USER_WHITELIST=/usr/local/etc/firewall_whitelist.txt
if [ -f "$USER_WHITELIST" ]; then
    while IFS= read -r line; do
        # strip everything after '#' (inline comments + comment-only lines), trim whitespace
        domain=$(echo "$line" | sed 's/#.*//' | xargs)
        [ -n "$domain" ] && allow_domain "$domain" "user"
    done < "$USER_WHITELIST"
fi

# Catch-all REJECT at the end of OUTPUT/FORWARD. Belt-and-suspenders with the
# `iptables -P OUTPUT DROP` policy above — under iptables-nft / iptables-legacy
# backend mismatches, the policy isn't always honored, but explicit `-A` rules
# are. REJECT (vs DROP) returns ICMP-port-unreachable so applications fail fast
# instead of waiting for TCP timeout.
iptables -A OUTPUT  -j REJECT --reject-with icmp-port-unreachable
iptables -A FORWARD -j REJECT --reject-with icmp-port-unreachable

# Self-test — verify the firewall actually enforces. Without this, a backend
# mismatch (rules written but not honored) would silently leave the unattended
# agent free to reach anywhere. Fail loudly so the container terminates rather
# than starting claude on top of a non-functional firewall.
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

echo "init-firewall.sh: enforcement verified — allowlist of ${#ALLOWED_DOMAINS[@]} domains active, all other outbound rejected."
