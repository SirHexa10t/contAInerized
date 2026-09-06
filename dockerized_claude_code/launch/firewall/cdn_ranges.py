"""Which IPv4 blocks each known CDN provider publishes — fetched, never baked.

When a whitelisted host's resolved IPs sit inside a provider's published
block, the resolver whitelists the WHOLE containing block instead of pinning
the momentary IPs, so POP rotation inside the block cannot strand the host
behind a stale pin. This module owns that table: where it comes from, how it
is cached between launches, and whether a given address is inside it.

Split out of `resolver.py` 2026-09-03, which was 1146 lines doing four jobs.
This one is genuinely separable: the only things the resolver asks of it are
`load()` once per launch and `provider_ranges(ips)` per resolution, plus
`blocks_for(provider)` for the wildcard-widening case. Nothing here knows
about DNS, threads, phases or iptables.

⚠ Security tradeoff (deliberate, and the reason this module is documented
rather than hidden): a provider block is shared by every customer of that
CDN — allowing a block makes OTHER sites served from those same addresses
reachable too (HTTPS routing is SNI-based, one IP serves many customers).
Widening only triggers when a *whitelisted* host is detected on the
provider, and wildcards only widen further because the user explicitly asked
for subdomain coverage — but the effective grant is "this CDN's edge", not
"this one site".

The blocks come from each provider's own published range list, fetched over
HTTPS on the host and cached per provider under FIREWALL_CACHE_DIR (per-file
mtime = per-provider freshness). Per launch, each provider resolves through a
graceful chain: fresh cache → live fetch (saved back) → stale cache (with a
warning) → provider skipped for this launch (hosts on it just stay
IP-pinned). Nothing here hardcodes address space — when a provider
re-publishes its ranges, the next stale-cache launch picks them up.
"""

import ipaddress
import json
import sys
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from urllib import request as urllib_request

from ..file_access import is_file_recent, parse_lines, path_exists, write_text
from ..paths import cdn_ranges_cache_path

# How long a cached provider list stays usable without a refetch. Separate
# from the resolver's DNS-answer TTL (same 3 days today) because they answer
# different questions: a DNS answer goes stale when steering changes, a
# published CIDR list when the provider re-publishes — the second is far
# rarer, and tying them to one number only hid that.
_CACHE_TTL_SECONDS = 3 * 24 * 60 * 60

# One HTTPS fetch per provider list; generous because the AWS list is ~2 MB.
_RANGE_FETCH_TIMEOUT = 15

# Some published endpoints (GitHub's API among them) reject requests with no
# User-Agent, so every fetch sends a stable, honest one.
_RANGE_FETCH_USER_AGENT = "claude-agents-launcher"


def _http_get(url: str) -> str:
    """GET `url` and return the body as text. Raises on any HTTP/socket
    problem — callers treat a raised fetch as 'this provider is unavailable
    right now' and fall back to cache."""
    request = urllib_request.Request(url, headers={"User-Agent": _RANGE_FETCH_USER_AGENT})
    with urllib_request.urlopen(request, timeout=_RANGE_FETCH_TIMEOUT) as response:
        return response.read().decode("utf-8", errors="replace")


def _clean_cidrs(candidates: Iterable[str]) -> list[str]:
    """Normalize fetched range strings to sorted, collapsed IPv4 CIDRs.
    Non-IPv4 / malformed entries are dropped silently (published lists mix
    v6 in freely); collapsing merges adjacent and overlapping blocks so the
    downstream containment scans and iptables rules stay minimal. Fetched
    bodies are external input — nothing that doesn't parse as an IPv4
    network may survive into rule generation."""
    networks = []
    for candidate in candidates:
        try:
            net = ipaddress.ip_network(candidate.strip(), strict=False)
        except ValueError:
            continue
        if net.version == 4:
            networks.append(net)
    return [str(n) for n in ipaddress.collapse_addresses(networks)]


def _subtract_networks(base: Iterable[str], remove: Iterable[str]) -> list[str]:
    """CIDRs covering every address in `base` that is NOT in `remove` —
    netmask-aware (a plain set difference would miss removals published at a
    different aggregation than the base). Used for providers that publish
    "all our space" and "the subset customers can rent" as separate lists,
    where only the difference — the provider's own services — should drive
    widening. Inputs are cleaned CIDR strings; result is collapsed."""
    remove_nets = [ipaddress.IPv4Network(c) for c in _clean_cidrs(remove)]
    remaining: list[ipaddress.IPv4Network] = []
    for cidr in _clean_cidrs(base):
        parts = [ipaddress.IPv4Network(cidr)]
        for removal in remove_nets:
            next_parts = []
            for part in parts:
                if not part.overlaps(removal):
                    next_parts.append(part)
                elif not removal.supernet_of(part):
                    next_parts.extend(part.address_exclude(removal))   # removal strictly inside part
            parts = next_parts
        remaining.extend(parts)
    return [str(n) for n in ipaddress.collapse_addresses(remaining)]


def _cloudflare_ranges() -> list[str]:
    """Cloudflare publishes a plain-text file, one IPv4 CIDR per line."""
    return _clean_cidrs(_http_get("https://www.cloudflare.com/ips-v4").splitlines())


def _fastly_ranges() -> list[str]:
    """Fastly publishes JSON: {"addresses": [v4 cidrs], "ipv6_addresses": [...]}."""
    return _clean_cidrs(json.loads(_http_get("https://api.fastly.com/public-ip-list"))["addresses"])


def _github_ranges() -> list[str]:
    """GitHub's /meta endpoint maps service names to mixed v4/v6 CIDR lists;
    the edge-serving services below cover web, API, git, release/raw assets,
    and Pages."""
    meta = json.loads(_http_get("https://api.github.com/meta"))
    services = ("web", "api", "git", "packages", "pages")
    return _clean_cidrs(cidr for service in services for cidr in meta.get(service, []))


def _cloudfront_ranges() -> list[str]:
    """AWS publishes one JSON for all services; CloudFront's entries are the
    CDN edge blocks."""
    prefixes = json.loads(_http_get("https://ip-ranges.amazonaws.com/ip-ranges.json"))["prefixes"]
    return _clean_cidrs(p["ip_prefix"] for p in prefixes if p.get("service") == "CLOUDFRONT")


def _google_ranges() -> list[str]:
    """Google publishes "all Google" (goog.json) and "rentable cloud"
    (cloud.json); the netmask-aware difference is Google's own services —
    the ranges its consumer-facing edges (and their minted subdomains)
    serve from."""
    def prefixes(url: str) -> list[str]:
        return [p["ipv4Prefix"] for p in json.loads(_http_get(url))["prefixes"] if "ipv4Prefix" in p]
    return _subtract_networks(
        prefixes("https://www.gstatic.com/ipranges/goog.json"),
        prefixes("https://www.gstatic.com/ipranges/cloud.json"),
    )


# Provider name → fetcher for its published IPv4 ranges. Adding a provider is
# one entry + one small fetcher above; the cache/fallback plumbing, widening,
# wildcard grants, and status annotations all key off this registry.
_RANGE_FETCHERS: dict[str, Callable[[], list[str]]] = {
    "cloudflare": _cloudflare_ranges,
    "fastly": _fastly_ranges,
    "github": _github_ranges,
    "cloudfront": _cloudfront_ranges,
    "google": _google_ranges,
}

# The launch's working view of provider ranges, filled by `load()` at the top
# of the resolver's Phase 1 (empty until then, and empty entries simply mean
# "no widening for that provider this launch"). `_provider_blocks` feeds
# wildcard all-blocks grants via `blocks_for`; `_cdn_networks` is the parsed
# flat view the per-IP containment scan iterates. Written once before any
# resolution callback runs, read-only afterwards — the resolver's phases hand
# off strictly, so no lock.
_provider_blocks: dict[str, list[str]] = {}
_cdn_networks: list[tuple[ipaddress.IPv4Network, str, str]] = []


def set_provider_blocks(blocks: dict[str, list[str]]) -> None:
    """Install `blocks` as the launch's provider-range view, rebuilding the
    parsed containment index alongside. IPv4Network (not ip_network) so a v6
    or malformed CIDR sneaking past a fetcher fails loudly here rather than
    mis-matching silently."""
    _provider_blocks.clear()
    _provider_blocks.update({provider: list(cidrs) for provider, cidrs in blocks.items()})
    _cdn_networks.clear()
    _cdn_networks.extend(
        (ipaddress.IPv4Network(cidr), provider, cidr)
        for provider, cidrs in _provider_blocks.items()
        for cidr in cidrs
    )


def _read_cached_ranges(provider: str) -> list[str]:
    """The provider's cached CIDRs, regardless of file age ([] when the file
    is missing). Age policy lives in _load_cdn_ranges — this is just the
    read."""
    path = cdn_ranges_cache_path(provider)
    return _clean_cidrs(parse_lines(path)) if path_exists(path) else []


def _save_cached_ranges(provider: str, cidrs: list[str]) -> None:
    """Persist a fresh fetch so the next _CACHE_TTL_SECONDS of launches skip
    the network round-trip (and so a future failed fetch has something to
    fall back on)."""
    lines = [
        f"# Published IPv4 ranges for '{provider}', fetched by the launcher.",
        f"# Refetched when this file's mtime ages past {_CACHE_TTL_SECONDS // (60 * 60 * 24)} days.",
        *cidrs,
    ]
    write_text(cdn_ranges_cache_path(provider), "\n".join(lines) + "\n")


def load() -> None:
    """Populate the launch's provider-range view (see `set_provider_blocks`).
    Per provider: a fresh cache file wins outright; otherwise fetch the
    published list (in parallel across providers, saved back on success);
    a failed fetch falls back to the stale cache with a warning; no cache
    at all means the provider is skipped this launch — hosts on it degrade
    to plain IP pinning, nothing breaks. Runs at the top of Phase 1, so a
    cold cache adds one fetch round-trip to the launcher's critical-resolve
    wait; warm launches don't touch the network."""
    blocks: dict[str, list[str]] = {}
    to_fetch: list[str] = []
    for provider in _RANGE_FETCHERS:
        cached = _read_cached_ranges(provider)
        if cached and is_file_recent(cdn_ranges_cache_path(provider), _CACHE_TTL_SECONDS):
            blocks[provider] = cached
        else:
            to_fetch.append(provider)
    if to_fetch:
        def fetch(provider: str) -> list[str]:
            try:
                return _RANGE_FETCHERS[provider]()
            except Exception as exc:   # noqa: BLE001 — any fetch problem means "use fallback"
                print(f"  warning: fetching {provider} CDN ranges failed ({exc})", file=sys.stderr)
                return []
        with ThreadPoolExecutor(max_workers=len(to_fetch)) as pool:
            fetched = dict(zip(to_fetch, pool.map(fetch, to_fetch)))
        for provider, cidrs in fetched.items():
            if cidrs:
                blocks[provider] = cidrs
                _save_cached_ranges(provider, cidrs)
            elif stale := _read_cached_ranges(provider):
                blocks[provider] = stale
                print(f"  warning: using stale cached ranges for {provider}", file=sys.stderr)
            else:
                print(f"  warning: no ranges for {provider} this launch — its hosts stay IP-pinned", file=sys.stderr)
    set_provider_blocks(blocks)


def provider_ranges(ips: Iterable[str]) -> tuple[str | None, list[str]]:
    """(provider, containing CIDR blocks) when any of `ips` sits inside a
    known provider block (per this launch's fetched view — _cdn_networks);
    (None, []) otherwise. Malformed / non-IPv4 tokens are skipped. The
    provider label is for the status-file annotation; the CIDR list drives
    the actual widening in `resolver._tokens_for`."""
    provider: str | None = None
    ranges: list[str] = []
    for ip_str in ips:
        try:
            addr = ipaddress.IPv4Address(ip_str)
        except ValueError:
            continue
        for network, prov, cidr in _cdn_networks:
            if addr in network:
                provider = provider or prov
                if cidr not in ranges:
                    ranges.append(cidr)
                break
    return provider, ranges


def blocks_for(provider: str) -> list[str]:
    """EVERY block this provider publishes, for the wildcard case: a `*.`
    entry whose base host landed on `provider` opens the provider's whole
    edge, not just the containing block, because per-request shard hostnames
    cannot be enumerated through DNS.

    A copy, not the live list — the caller turns these into tokens and must
    not be able to mutate the launch's view while doing it."""
    return list(_provider_blocks[provider])
