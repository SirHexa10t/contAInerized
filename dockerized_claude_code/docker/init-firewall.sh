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

# Reset all chains
iptables -F
iptables -X
iptables -t nat -F 2>/dev/null || true
iptables -t nat -X 2>/dev/null || true

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

for domain in "${ALLOWED_DOMAINS[@]}"; do
    # `getent ahosts` returns one or more IPv4/IPv6 lines per name
    ips=$(getent ahosts "$domain" 2>/dev/null | awk '{print $1}' | sort -u || true)
    if [ -z "$ips" ]; then
        echo "init-firewall.sh: warning: '$domain' did not resolve; skipping" >&2
        continue
    fi
    for ip in $ips; do
        iptables -A OUTPUT -d "$ip" -p tcp --dport 443 -j ACCEPT
        iptables -A OUTPUT -d "$ip" -p tcp --dport 80  -j ACCEPT
    done
done

echo "init-firewall.sh: allowlist applied (${#ALLOWED_DOMAINS[@]} domains; outbound otherwise dropped)"
