"""Pure expansion of the raw `{firewall}` whitelist into resolvable work
items — no DNS, no threads, no disk. Turns the union of BUILTIN_FIREWALL_DOMAINS
and the user's `firewall_whitelist.txt` into literal IPv4/CIDR entries (straight
to iptables), hostname entries (need resolution), and skipped entries
(IPv6 literals the v4-only pipeline can't honor). `resolver.py` consumes these;
keeping the transformation here makes the security-relevant parsing unit-
testable in isolation."""

import ipaddress
import re
from collections.abc import Iterable
from typing import NamedTuple

from ..utils import split_host_port

# A whitelist entry can be a hostname, hostname:port, literal IPv4 (±:port), or
# CIDR (±:port). This matches the literal forms — a match means "no DNS needed";
# anything else is treated as a hostname for the resolver.
_IP_OR_CIDR_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(/[0-9]+)?$")

# Reason string for the status file's `skipped:` section — IPv6 whitelist
# entries, which the IPv4-only pipeline can't honor.
_SKIPPED_IPV6_REASON = "IPv6 entry — the container network and firewall are IPv4-only; whitelist the hostname or its IPv4 range instead"


class HostnameEntry(NamedTuple):
    """A whitelist entry that needs DNS — the raw entry string, its hostname,
    the port suffix (`""` when the entry didn't specify one, in which case
    `_DEFAULT_OPEN_PORTS` is opened instead), and whether the entry carried a
    `*.` wildcard prefix (which asks for all-provider-blocks widening — see
    resolver._tokens_for). Carried through both phases of the resolution cascade
    so a resolved host can be matched back to every entry that shares its
    hostname."""
    entry: str
    host: str
    port: str
    wildcard: bool = False


class ExpandedWhitelist(NamedTuple):
    """_expand_whitelist's result: literal IPv4/CIDR entries (straight to
    iptables, no DNS), hostname entries (need resolution), and skipped
    entries as (entry, reason) pairs — currently IPv6 literals, which the
    v4-only pipeline can't honor and shouldn't waste a DNS cascade on."""
    literals: list[str]
    hostnames: list[HostnameEntry]
    skipped: list[tuple[str, str]]


def _is_ipv6_literal(entry: str) -> bool:
    """True when `entry` is an IPv6 address or CIDR. The v4-only pipeline
    skips these (surfaced via the status file's `skipped:` section) instead
    of feeding them to the DNS cascade as if they were hostnames."""
    try:
        return ipaddress.ip_network(entry, strict=False).version == 6
    except ValueError:
        return False


def _expand_whitelist(raw_entries: Iterable[str]) -> ExpandedWhitelist:
    """Pure expansion of the raw whitelist into work items: set aside IPv6
    literals as skipped, dedupe, keep `*.` wildcard entries flagged (base
    host resolves; _tokens_for widens to the whole provider), add the bare
    apex for every `www.X` entry (typing the `www.` form clearly means the
    apex too), then split literal IPv4/CIDR entries (pass straight to
    iptables, no DNS) from hostnames that need resolution. A wildcard and a
    plain entry for the same (host, port) collapse to the wildcard — it's
    the superset. All returned lists are sorted for deterministic downstream
    ordering. Extracted as a pure function so the security-relevant
    transformation is unit-testable without threads or DNS."""
    skipped: list[tuple[str, str]] = []
    candidates: set[str] = set()
    for raw in set(raw_entries):
        if _is_ipv6_literal(raw):
            skipped.append((raw, _SKIPPED_IPV6_REASON))
        else:
            candidates.add(raw)
    candidates |= {c.removeprefix("www.") for c in candidates if c.startswith("www.")}
    literals: set[str] = set()
    by_key: dict[tuple[str, str], tuple[str, bool]] = {}
    for entry in sorted(candidates):
        wildcard = entry.startswith("*.")
        host, port = split_host_port(entry.removeprefix("*."))
        if _IP_OR_CIDR_RE.match(host):
            literals.add(entry.removeprefix("*."))   # a `*.` on a literal is meaningless — drop it
        elif wildcard or (host, port) not in by_key:
            by_key[(host, port)] = (entry, wildcard)
    hostnames = [HostnameEntry(e, h, p, w) for (h, p), (e, w) in by_key.items()]
    return ExpandedWhitelist(sorted(literals), sorted(hostnames), sorted(skipped))


def _index_by_host(entries: list[HostnameEntry]) -> dict[str, list[HostnameEntry]]:
    """Group entries by hostname — multiple entries can share a host (the
    user wrote both `api.anthropic.com:443` and the bare apex, or apex+www
    stripping produced duplicates). The dict turns the per-resolve scan in
    each `on_ok` from O(N) into O(1)."""
    out: dict[str, list[HostnameEntry]] = {}
    for e in entries:
        out.setdefault(e.host, []).append(e)
    return out
