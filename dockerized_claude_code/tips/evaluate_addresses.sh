#!/usr/bin/env bash
# evaluate_addresses.sh — which whitelist entries are worth upgrading to `*.` wildcards?
#
# The {auto} firewall honors `*.example.com` only when the base host sits on a
# CDN provider the launcher knows (launch/network.py, _RANGE_FETCHERS): the
# provider's entire published address space opens, so subdomains that rotate —
# or are minted per request — are covered. On any other host a wildcard
# degrades to base-host pinning (surfaced as `wildcard_gaps:` in the status
# file), so upgrading such an entry buys nothing.
#
# This script answers "which of my entries would a wildcard actually help?"
# using the same published range lists and the same resolver the launcher uses.
#
# Usage, sourced (list inline — comments and trailing commas are tolerated):
#
#     source tips/evaluate_addresses.sh
#     domains=(
#         # some comment
#         "www.crates.io",
#         "internal.example.net",
#     )
#     evaluate_addresses "${domains[@]}"
#
# or executed directly:
#
#     tips/evaluate_addresses.sh crates.io internal.example.net
#
# stdout — ready-to-paste whitelist lines: `*.<apex>   # via <provider>`
# stderr — everything else: entries that don't qualify (and why), fetch warnings
#
# Needs curl + jq + getent. Runtime: five range fetches up front, then one DNS
# lookup per unique domain — a few seconds for a hundred entries.

# Flat range table: provider / network-int / mask-int, one index per CIDR.
# "google" rows come from goog.json and "google-cloud" rows from cloud.json —
# an IP is Google-services space iff it matches the former and NOT the latter
# (the launcher's netmask subtraction, recast as a per-IP membership test).
_EA_PROVIDERS=()
_EA_NETS=()
_EA_MASKS=()

# The launcher's provider order, reused for stable output labels.
_EA_PROVIDER_ORDER=(cloudflare fastly github cloudfront google)

_ea_ip2int() {   # dotted quad → _EA_INT
    local IFS=. a b c d
    read -r a b c d <<<"$1"
    _EA_INT=$(( (a << 24) | (b << 16) | (c << 8) | d ))
}

_ea_add_ranges() {   # $1 = provider label; stdin = candidate CIDRs, one per line
    local provider=$1 cidr bits mask
    while read -r cidr; do
        [[ $cidr =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(/[0-9]+)?$ ]] || continue   # v4 only; v6 and noise drop here
        bits=32
        [[ $cidr == */* ]] && bits=${cidr#*/}
        mask=$(( (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF ))
        _ea_ip2int "${cidr%%/*}"
        _EA_PROVIDERS+=("$provider")
        _EA_MASKS+=("$mask")
        _EA_NETS+=("$(( _EA_INT & mask ))")
    done
}

_ea_fetch() {
    curl -fsS --max-time 20 -A claude-agents-launcher "$1"
}

_ea_load_one() {   # $1 = provider, $2 = url, $3 = jq filter ("" = body is plain text)
    local body
    if ! body=$(_ea_fetch "$2"); then
        echo "  warning: $1 ranges unavailable ($2) — its hosts can't be classified" >&2
        return 1
    fi
    if [[ -n $3 ]] && ! body=$(jq -r "$3" <<<"$body"); then
        echo "  warning: $1 range list didn't parse — its hosts can't be classified" >&2
        return 1
    fi
    _ea_add_ranges "$1" <<<"$body"
}

_ea_load_ranges() {
    local tool
    for tool in curl jq getent; do
        command -v "$tool" >/dev/null || { echo "error: $tool is required" >&2; return 1; }
    done
    _EA_PROVIDERS=() _EA_NETS=() _EA_MASKS=()
    _ea_load_one cloudflare https://www.cloudflare.com/ips-v4 ""
    _ea_load_one fastly     https://api.fastly.com/public-ip-list '.addresses[]'
    _ea_load_one github     https://api.github.com/meta \
        '(.web // []) + (.api // []) + (.git // []) + (.packages // []) + (.pages // []) | .[]'
    _ea_load_one cloudfront https://ip-ranges.amazonaws.com/ip-ranges.json \
        '.prefixes[] | select(.service == "CLOUDFRONT") | .ip_prefix'
    # google needs BOTH lists: with goog.json alone, rentable GCP space would
    # misclassify as Google services — so it's both-or-neither.
    if _ea_load_one google https://www.gstatic.com/ipranges/goog.json '.prefixes[].ipv4Prefix // empty'; then
        _ea_load_one google-cloud https://www.gstatic.com/ipranges/cloud.json '.prefixes[].ipv4Prefix // empty' \
            || _ea_drop_provider google
    fi
    (( ${#_EA_NETS[@]} )) || { echo "error: no provider ranges could be fetched — nothing can be classified" >&2; return 1; }
}

_ea_drop_provider() {   # remove every row of provider $1 from the range table
    local i keep_p=() keep_n=() keep_m=()
    for (( i = 0; i < ${#_EA_NETS[@]}; i++ )); do
        [[ ${_EA_PROVIDERS[i]} == "$1" ]] && continue
        keep_p+=("${_EA_PROVIDERS[i]}") keep_n+=("${_EA_NETS[i]}") keep_m+=("${_EA_MASKS[i]}")
    done
    _EA_PROVIDERS=("${keep_p[@]}") _EA_NETS=("${keep_n[@]}") _EA_MASKS=("${keep_m[@]}")
}

_ea_classify() {   # $@ = IPv4 addresses → _EA_MATCHED ("prov" / "prov,prov" / "")
    local ip i ipint provider
    local -A domain_hits=()
    for ip in "$@"; do
        local -A ip_hits=()
        _ea_ip2int "$ip"
        ipint=$_EA_INT
        for (( i = 0; i < ${#_EA_NETS[@]}; i++ )); do
            (( (ipint & _EA_MASKS[i]) == _EA_NETS[i] )) && ip_hits[${_EA_PROVIDERS[i]}]=1
        done
        # Per-IP google subtraction: inside cloud.json = rentable space, not a
        # Google service, no matter what goog.json says about the same IP.
        [[ -n ${ip_hits[google-cloud]:-} ]] && unset 'ip_hits[google]'
        unset 'ip_hits[google-cloud]'
        for provider in "${!ip_hits[@]}"; do
            domain_hits[$provider]=1
        done
        unset ip_hits
    done
    _EA_MATCHED=""
    for provider in "${_EA_PROVIDER_ORDER[@]}"; do
        [[ -n ${domain_hits[$provider]:-} ]] && _EA_MATCHED+=${_EA_MATCHED:+,}$provider
    done
}

_ea_resolve4() {   # $1 = host → _EA_IPS (deduped IPv4 answers; empty on failure)
    local ip _rest
    local -A seen=()
    _EA_IPS=()
    while read -r ip _rest; do
        [[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ && -z ${seen[$ip]:-} ]] || continue
        seen[$ip]=1
        _EA_IPS+=("$ip")
    done < <(getent ahostsv4 "$1" 2>/dev/null)
}

evaluate_addresses() {
    if (( $# == 0 )); then
        echo "usage: evaluate_addresses <domain>..." >&2
        echo "  (or: ${BASH_SOURCE[0]} <domain>...)" >&2
        return 2
    fi
    _ea_load_ranges || return 1
    local arg entry base port
    local -A evaluated=()
    for arg in "$@"; do
        entry=${arg//[[:space:],]/}          # tolerate stray whitespace + array-style trailing commas
        [[ -z $entry ]] && continue
        if [[ $entry == *:*:* || $entry == *::* ]]; then
            echo "  - $entry: IPv6 — the firewall is IPv4-only, skipped" >&2
            continue
        fi
        entry=${entry#\*.}                   # already-wildcard entries get re-validated
        port=""
        if [[ $entry == *:* ]]; then
            port=":${entry##*:}"
            entry=${entry%:*}
        fi
        if [[ $entry =~ ^[0-9.]+(/[0-9]+)?$ ]]; then
            echo "  - $entry$port: IP/CIDR literal — wildcards don't apply" >&2
            continue
        fi
        base=${entry#www.}                   # the wildcard belongs on the apex; *.foo.com covers www too
        [[ -n ${evaluated[$base$port]:-} ]] && continue
        evaluated[$base$port]=1
        _ea_resolve4 "$base"
        if (( ${#_EA_IPS[@]} == 0 )); then
            echo "  - $base$port: no IPv4 answer (typo, dead host, or v6-only)" >&2
            continue
        fi
        _ea_classify "${_EA_IPS[@]}"
        if [[ -n $_EA_MATCHED ]]; then
            printf '*.%s%s   # via %s\n' "$base" "$port" "$_EA_MATCHED"
        else
            echo "  - $base$port: not on a known CDN provider — a wildcard would only cover the base host" >&2
        fi
    done
}

# Executed directly (not sourced): evaluate the command-line args.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    evaluate_addresses "$@"
fi
