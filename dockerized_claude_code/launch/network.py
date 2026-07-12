"""Host-side network helpers — currently the {auto}-mode firewall whitelist
resolver. DNS resolution happens here, on the host, before `docker compose run`
spins up the container. In-container, init-firewall.sh just writes iptables
rules from the pre-resolved address list — no DNS calls, no parallel-xargs
plumbing, no `getent` timeouts to babysit.

Concurrency model (two-phase, streaming):
  - Phase 1 (critical): api.anthropic.com + console.anthropic.com resolve
    synchronously (from the caller's perspective) — these MUST be in the
    container's initial iptables ruleset, so the launcher blocks until
    they're known. agent_modifiers_handler._apply_auto fires the whole thing via
    start_whitelist_resolution(state_dir) during compose_chain;
    docker_config.run_compose awaits via wait_for_critical_addresses()
    before staging WHITELIST_ADDRESSES and firing `docker compose run`.
  - Phase 2 (rest): every other whitelist entry resolves in a background
    thread that streams ready-to-open `addr[:port]` tokens onto an internal
    queue. A daemon updater thread (start_firewall_updater, spawned right
    before `docker compose run`) drains the queue in bursts and applies each
    burst with ONE `docker exec --user root <container> sh -c 'iptables -I
    OUTPUT 1 ... && ...'` — batching dozens of ACCEPT rules per exec instead
    of one exec per rule (the old per-rule pace took minutes for the full
    list; see benchmark/bench_firewall_updater.py). Rules insert BEFORE the
    catch-all REJECT, so arrival order doesn't matter. The launcher proceeds
    into Claude Code as soon as Phase 1 finishes; Phase 2 + the updater run
    alongside Claude Code's startup, growing the firewall in real time.

Status surface: one file per launch — `domains_pending_resolve.yml` inside
the per-instance state dir (bind-mounted into the container at
/home/claude/.claude/). Holds status + pending list + failed list;
auto-addendum.md points the agent here for "I hit a connection refused"
classification. Rewritten atomically as the picture changes.

Cross-launch DNS cache: RESOLVED_DOMAINS_CACHE_FILE (at the AGENTS_STATE
root). When fresh (mtime < a few hours, gated by is_file_recent in utils),
every host listed in the cache short-circuits the DNS cascade — the
launcher reuses the cached IPs directly. Rewritten at end of Phase 2 with
the full resolved set from this launch (cache hits + fresh DNS results),
so successive launches keep accumulating coverage while inside the TTL
window. A stale file is ignored on read and rebuilt from scratch.

Module boundary vs agent_modifiers_handler: the resolution policy + curated domain
list are network-layer policy + DNS plumbing, not chain composition / handler
dispatch — different concerns, different module. Future network-shaped logic
(port scans, host reachability checks, etc.) co-locates here.

Caveat — DNS-by-IP-pinning risk: the launcher resolves hostnames to IPs on the
host, then iptables allows exactly those IPs. If the container's DNS resolver
later returns a DIFFERENT IP for the same hostname (different POP, round-robin
rotation, etc.), the connection won't match the iptables rule and will be
rejected. In practice the host and the container's `dockerd`-forwarded
resolver share an upstream chain (the host's /etc/resolv.conf), and dockerd's
queries usually hit the host's freshly-populated DNS cache from this same
resolution pass, so they line up.

CDN widening (the mitigation): hosts whose resolved IPs sit inside a known
CDN provider's published block (CDN_IPV4_RANGES in
template_code/firewall_domains.py — Cloudflare / Fastly / GitHub /
CloudFront) get the whole containing block whitelisted instead of the
momentary IPs, so POP rotation inside the block can't strand them. The
pinning caveat above still fully applies to hosts OUTSIDE any known block,
and to entries with an explicit :port (those stay pinned — opening a whole
provider block on a custom port is a broader grant than the entry asked
for). See _tokens_for for the policy and firewall_domains.py for the
security tradeoff this widening deliberately makes.

Imports nothing heavy: file_access for the user's whitelist file + atomic
write helper, paths for the two status-file locations, template_code for the
domain + CDN-range data, stdlib for subprocess + threading + ipaddress. agent_modifiers_handler._apply_auto is the entry point caller (calls
start_whitelist_resolution during compose_chain); docker_config.run_compose
pairs the await + updater-spawn.

Cycle note: docker_config imports this module (for is_critical_pending /
wait_for_critical_addresses / start_firewall_updater), and the updater code
below needs docker_config's docker-subprocess helpers (wait_for_container_running
+ docker_exec_root_subprocess) to inject iptables rules into the running container. The
two functions that need them (_updater_worker, _flush_rules) do lazy
`from .docker_config import ...` at call time so import-time evaluation
doesn't hit a half-loaded module."""

import ipaddress
import os
import queue
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from .file_access import (
    is_file_recent, parse_lines, user_firewall_whitelist_lines, write_text,
)
from .paths import RESOLVED_DOMAINS_CACHE_FILE, state_domain_resolve_status_path
from .template_code.firewall_domains import BUILTIN_FIREWALL_DOMAINS, CDN_IPV4_RANGES
from .utils import shell_capture, split_host_port


# ============================================================
# Always-allowed domains + CDN ranges — data lives in template_code
# ============================================================
# BUILTIN_FIREWALL_DOMAINS (the curated always-allowed list; the user's
# firewall_whitelist.txt is unioned in at start_whitelist_resolution time)
# and CDN_IPV4_RANGES (published provider blocks driving the CDN widening
# below) are pure data — they live in template_code/firewall_domains.py per
# that package's data-only convention. Inside the container, init-firewall.sh
# reads pre-resolved addresses via $WHITELIST_ADDRESSES — no DNS dependency
# in the firewall hot path.


# ============================================================
# DNS resolution + entry shape
# ============================================================
# Each whitelist entry can be a hostname, a hostname:port, a literal IPv4
# address (with/without :port), or a CIDR range (with/without :port).
# Each resolved (or literal) entry ultimately becomes one or more `<ip>[:port]`
# / `<cidr>[:port]` strings — Phase 1 collects them for the initial
# WHITELIST_ADDRESSES env-var (consumed by init-firewall.sh at container
# start), Phase 2 streams them to the updater for `docker exec iptables -I`
# into the running container. Literal IPs/CIDRs pass through untouched.

_IP_OR_CIDR_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(/[0-9]+)?$")

class HostnameEntry(NamedTuple):
    """A whitelist entry that needs DNS — the raw entry string, its hostname,
    and the port suffix (`""` when the entry didn't specify one, in which
    case `_DEFAULT_OPEN_PORTS` is opened instead). Carried through both
    phases of the resolution cascade so a resolved host can be matched back
    to every (entry, port) pair that shares its hostname."""
    entry: str
    host: str
    port: str

# Reason string written to the status file's `failed:` section when a host
# exhausts every cascade stage. One constant so phase 1 + phase 2 emit
# identical text — the agent / user can grep for it.
_FAILED_RESOLVE_REASON = "DNS resolution failed after all cascade stages"

# Cascade timeouts: a host that fails resolution at pass N is retried at pass
# N+1 with the next (larger) per-host timeout. The cascade exists to recover
# from contention-induced false negatives — see _cascade below for the full
# rationale. Worst-case budget per host is the sum (50s, only reached by a
# host that times out at every single stage); typical wall time for the full
# whitelist is well under 10s.
_RESOLVE_TIMEOUT_STAGES = (3, 5, 8, 13, 21)

# Parallelism heuristic: DNS-bound work (workers mostly sleeping on getent),
# so we go well past core count — each worker is almost always idle waiting
# for the resolver, so threads above core count don't starve CPU and do
# shorten total wall time. nproc * 8 works well empirically.
_RESOLVE_PARALLELISM = (os.cpu_count() or 1) * 8

# Cross-launch DNS cache TTL — how long RESOLVED_DOMAINS_CACHE_FILE is treated
# as authoritative on read. Tuned around CDN rotation: most providers refresh
# A records on the order of an hour, so a few hours buys cheap launches across
# a working day while bounding stale-IP risk. Refresh on read happens via
# is_file_recent in utils; the launcher itself never inspects time.
_RESOLUTION_CACHE_TTL_SECONDS = 6 * 60 * 60

# In-process cache: {host: [ip, ...]} loaded from RESOLVED_DOMAINS_CACHE_FILE
# at the start of start_whitelist_resolution, consulted by _resolve_a_records
# to short-circuit DNS. Empty when the on-disk cache is missing or stale.
# Read-only from cascade worker threads (only the main thread writes, before
# the pool starts), so no lock needed on this dict.
_resolution_cache: dict[str, list[str]] = {}


def _resolve_a_records(host: str, timeout: float) -> list[str]:
    """Resolve `host` to its IPv4 A records — cross-launch cache first, then
    `getent ahostsv4` (runs against the host's resolver chain with whatever
    caching it has). Returns sorted IPs, or [] on timeout / NXDOMAIN /
    IPv6-only domains. subprocess + timeout gives cleaner cancellation than
    socket.getaddrinfo, which has no kwarg timeout. `timeout` is per-call,
    supplied by _cascade per cascade stage. Output tokens are validated
    against _IPV4_RE — resolver output is the one externally-controlled
    string in this pipeline, and everything downstream (WHITELIST_ADDRESSES,
    the batched `sh -c` iptables script) must only ever see well-formed
    addresses."""
    if host in _resolution_cache:
        return list(_resolution_cache[host])
    try:
        r = shell_capture("getent", "ahostsv4", host, timeout=timeout)
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0:
        return []
    tokens = {line.split()[0] for line in r.stdout.splitlines() if line.strip()}
    return sorted(t for t in tokens if _IPV4_RE.match(t))


def _load_resolution_cache() -> None:
    """Populate the in-process resolution cache from RESOLVED_DOMAINS_CACHE_FILE
    when the file is fresh (is_file_recent checks mtime). A stale or missing
    file leaves the cache empty — every host then goes through the full DNS
    cascade and the cache is rebuilt from scratch by _save_resolution_cache
    at end of Phase 2. Format: one `host=ip[,ip]*` line per entry."""
    global _resolution_cache
    _resolution_cache = {}
    if not is_file_recent(RESOLVED_DOMAINS_CACHE_FILE, _RESOLUTION_CACHE_TTL_SECONDS):
        return
    for line in parse_lines(RESOLVED_DOMAINS_CACHE_FILE):
        host, sep, ips_part = line.partition("=")
        if sep:
            ips = [ip.strip() for ip in ips_part.split(",") if ip.strip()]
            if ips:
                _resolution_cache[host.strip()] = ips


def _save_resolution_cache(resolved: dict[str, list[str]]) -> None:
    """Rewrite RESOLVED_DOMAINS_CACHE_FILE from `resolved` (a {host: [ip, ...]}
    snapshot). Called at end of Phase 2 with the full resolved set from this
    launch — cache hits roll through unchanged, fresh DNS hits get persisted.
    write_text refreshes mtime, so the TTL window starts from this moment for
    the next launch's is_file_recent check."""
    lines = [
        "# {auto}-mode firewall resolved-domains cache.",
        f"# TTL: {_RESOLUTION_CACHE_TTL_SECONDS // 3600}h since this file's mtime. While fresh,",
        "# the launcher reuses these IPs and skips DNS for any host listed here.",
        "# Lines: <host>=<ip>[,<ip>]*",
        "",
    ]
    for host in sorted(resolved):
        ips = resolved[host]
        if ips:   # skip failed/empty entries — only successes are worth caching
            lines.append(f"{host}={','.join(ips)}")
    write_text(RESOLVED_DOMAINS_CACHE_FILE, "\n".join(lines) + "\n")


def _cascade(hosts: Iterable[str], on_resolved: Callable[[str, list[str]], None], on_terminal_failure: Callable[[str], None]) -> None:
    """Run cascading-timeout resolution over `hosts`. For each host, invoke
    `on_resolved(host, ips)` exactly once on success or `on_terminal_failure(host)`
    if every cascade stage exhausted its budget. Subsequent passes operate
    only on the previous pass's failures.

    --- Why the cascade exists (contention-recovery story) ---

    A single wide-fanout pass — dozens of simultaneous `getent` queries
    against the host's resolver (or dockerd's embedded DNS at 127.0.0.11
    for in-container queries) — can result in some queries being silently
    dropped or returning spurious NXDOMAIN. Empirically, measured against
    this project's full BUILTIN_FIREWALL_DOMAINS list, one in every ~30
    first-pass queries came back as a false negative; solo-retrying the same
    hostname seconds later returned a clean IP. The embedded resolver
    appears to get overwhelmed and lose request slots under load.

    Two effects in tandem rescue the false negatives:
      1. Each subsequent pass operates on a SMALLER candidate set (only the
         previous pass's failures), so parallel fan-out drops naturally and
         the resolver isn't stressed.
      2. The per-host timeout grows each pass, so genuinely-slow domains
         (real upstream auth servers taking time, not just contention drops)
         get more breathing room before being declared dead.

    A host resolving at pass N doesn't get re-queried. Worst-case cost per
    host is the sum of all stages in _RESOLVE_TIMEOUT_STAGES; in practice
    pass 1 resolves the vast majority and the cascade rarely reaches pass 3.

    Callback-driven so the two phases above (Phase 1 critical, Phase 2 rest-
    streaming) can hook resolution events to different actions (synchronous
    accumulate vs. push-to-queue + status-file update)."""
    pending = list(set(hosts))   # dedupe — duplicate hostnames share one lookup
    for timeout in _RESOLVE_TIMEOUT_STAGES:
        if not pending:
            return
        with ThreadPoolExecutor(max_workers=min(_RESOLVE_PARALLELISM, len(pending))) as pool:
            pass_results = list(pool.map(lambda h: (h, _resolve_a_records(h, timeout)), pending))
        next_pending = []
        for host, ips in pass_results:
            if ips:
                on_resolved(host, ips)
            else:
                next_pending.append(host)
        pending = next_pending
    for host in pending:
        on_terminal_failure(host)


# ============================================================
# Two-phase resolution + streaming firewall updates
# ============================================================
# The whitelist split into two tiers:
#   Phase 1 ("critical"): api.anthropic.com + console.anthropic.com.
#     Resolved synchronously, before `docker compose run` fires — Claude Code
#     cannot do anything without api.anthropic.com, so it MUST be in the
#     initial iptables ruleset that init-firewall.sh applies at container
#     start. Failing here aborts the launch (loud).
#   Phase 2 ("rest"): everything else from BUILTIN_FIREWALL_DOMAINS ∪ the
#     user's firewall_whitelist.txt. Resolved in a background thread while
#     the launcher fires `docker compose run` and Claude Code starts. As
#     each domain resolves on the host, a sibling "updater" thread (see
#     start_firewall_updater) runs `docker exec --user root <container>
#     iptables -I OUTPUT 1 -d <ip> ...` to insert an ACCEPT rule into the
#     running container's iptables — BEFORE the catch-all REJECT, so the
#     order in which rules arrive doesn't matter.
#
# Per-resolution progress lands on the agent-visible status surface — see the
# `_WhitelistResolutionStatus` section below. Cross-launch DNS results land
# in RESOLVED_DOMAINS_CACHE_FILE at AGENTS_STATE root (host-side, not mounted).
#
# Security model: the host-side `docker exec` route bypasses the container's
# sudoers (claude can't sudo iptables, but the launcher running outside the
# container can — docker exec --user root grants root from outside the
# namespace). claude has no influence over what the launcher decides to
# resolve — the whitelist is computed once at the start, in Python, from
# files on the host. So the firewall growing post-launch doesn't relax the
# claude-can't-modify-firewall invariant.

_CRITICAL_HOSTS = ("api.anthropic.com", "console.anthropic.com")

# HTTPS + HTTP — opened for any whitelist entry that doesn't specify :port.
_DEFAULT_OPEN_PORTS = ("443", "80")

# Plain IPv4 (no CIDR suffix) — what a validated resolver token must look like.
_IPV4_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")


# === CDN widening ===
# When a whitelisted host's resolved IPs sit inside a known CDN provider's
# published block, whitelist the WHOLE containing block instead of pinning
# the momentary IPs — POP rotation inside the block then can't strand the
# host behind a stale pin (the exact failure that kept forcing manual
# whitelist additions for CDN-fronted sites). The provider table lives in
# template_code/firewall_domains.py, which also documents the security
# tradeoff (a block is shared by every customer of that CDN).

# Parsed once at import: (network, provider, cidr-string) per curated block.
# IPv4Network (not ip_network) so a v6 block sneaking into the data table
# fails loudly here rather than mis-matching silently.
_CDN_NETWORKS: tuple[tuple[ipaddress.IPv4Network, str, str], ...] = tuple(
    (ipaddress.IPv4Network(cidr), provider, cidr)
    for provider, cidrs in CDN_IPV4_RANGES.items()
    for cidr in cidrs
)

# CIDR blocks already widened this launch — each block is opened at most
# once no matter how many hosts resolve into it. Phase 1 and Phase 2 run
# strictly sequentially (Phase 2's thread starts when Phase 1 finishes) and
# each phase's callbacks run serially in its own worker thread, so no lock.
_seen_cdn_ranges: set[str] = set()


def _cdn_provider_ranges(ips: Iterable[str]) -> tuple[str | None, list[str]]:
    """(provider, containing CIDR blocks) when any of `ips` sits inside a
    curated CDN block; (None, []) otherwise. Malformed / non-IPv4 tokens are
    skipped. The provider label is for the status-file annotation; the CIDR
    list drives the actual widening in _tokens_for."""
    provider: str | None = None
    ranges: list[str] = []
    for ip_str in ips:
        try:
            addr = ipaddress.IPv4Address(ip_str)
        except ValueError:
            continue
        for network, prov, cidr in _CDN_NETWORKS:
            if addr in network:
                provider = provider or prov
                if cidr not in ranges:
                    ranges.append(cidr)
                break
    return provider, ranges


def _tokens_for(host: str, ips: list[str], port: str) -> tuple[list[str], str | None]:
    """The `addr[:port]` tokens to open for a resolved (host, port) pair, plus
    the CDN provider label when widening happened (None otherwise).

    Policy:
      - No CDN match, or the entry carries an explicit :port → pin the
        resolved IPs exactly as before. (Port-specific entries stay pinned
        deliberately: opening a whole provider block on a custom port is a
        broader grant than the entry asked for.)
      - CDN match on a default-port entry → emit each containing block once
        per launch (_seen_cdn_ranges dedupes across hosts) plus any resolved
        IP that falls OUTSIDE the matched blocks (mixed A records: some
        edge, some origin). IPs covered by a block — emitted now or by an
        earlier host — need no rule of their own.

    `host` is unused in the computation but kept in the signature so call
    sites read naturally and future per-host policy has its hook."""
    provider, ranges = _cdn_provider_ranges(ips)
    if provider is None or port:
        return [f"{ip}:{port}" if port else ip for ip in ips], None
    new_ranges = [c for c in ranges if c not in _seen_cdn_ranges]
    _seen_cdn_ranges.update(new_ranges)
    networks = [ipaddress.IPv4Network(c) for c in ranges]
    uncovered = [ip for ip in ips if not any(ipaddress.IPv4Address(ip) in n for n in networks)]
    return new_ranges + uncovered, provider


# === Agent-visible whitelist-resolution status ===
# The `domains_pending_resolve.yml` file (under each instance's state dir,
# bind-mounted into the container at /home/claude/.claude/) is the runtime
# surface the agent reads to classify a `ConnectionRefused`: pending vs.
# failed vs. neither. memory/auto-addendum.md points the agent here.
#
# Phase 1, Phase 2, and the docker-exec updater all mutate the same in-memory
# state and rewrite the file from it — the class below bundles the lock, the
# dict, and the file path so those invariants stay co-located (lock guards
# both the dict mutation AND the file write; missing path = no-op write).
# All public methods take the lock themselves; callers don't.

class _WhitelistResolutionStatus:
    """Tracks DNS resolution progress for the {auto}-mode firewall whitelist
    and mirrors the in-memory state to `domains_pending_resolve.yml`. Single-
    process singleton (one launcher = one resolution = one tracker)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = None
        self.status: str = "uninit"                  # → "resolving" while host work is in flight, "complete" at end
        self.resolved: dict[str, list[str]] = {}     # host → [ip, ...]
        self.pending: list[str] = []                 # hosts still waiting on DNS
        self.failed: dict[str, str] = {}             # host → reason string
        self.cdn: dict[str, str] = {}                # host → CDN provider whose block was widened for it

    def init(self, state_dir) -> None:
        """Reset to a clean 'resolving' state and record where to write — wipes
        any leftover from a previous run on this instance so the agent never
        observes stale content. Called once at the start of every {auto} launch
        from start_whitelist_resolution, gated by that function's idempotency
        check."""
        with self._lock:
            self._path = state_domain_resolve_status_path(state_dir)
            self.status = "resolving"
            self.resolved = {}
            self.pending = []
            self.failed = {}
            self.cdn = {}
            self._write()

    def set_pending(self, hosts) -> None:
        """Replace the pending-host list (called once at start_whitelist_resolution
        after the full whitelist is assembled)."""
        with self._lock:
            self.pending = sorted(hosts)
            self._write()

    def mark_resolved(self, host: str, ips: list[str], cdn: str | None = None) -> None:
        """Move `host` from pending → resolved; file the IPs. `cdn` names the
        provider whose published block was widened for this host (None when
        the IPs were pinned as-is) — surfaced in the status file so a human
        or agent can see which hosts are rotation-proof."""
        with self._lock:
            self.resolved[host] = list(ips)
            if cdn:
                self.cdn[host] = cdn
            if host in self.pending:
                self.pending.remove(host)
            self._write()

    def mark_failed(self, host: str, reason: str) -> None:
        """Move `host` from pending → failed with `reason`."""
        with self._lock:
            self.failed[host] = reason
            if host in self.pending:
                self.pending.remove(host)
            self._write()

    def complete(self) -> None:
        """Flip top-level status to 'complete' — every entry has been
        resolved or terminally failed; no more updates coming."""
        with self._lock:
            self.status = "complete"
            self._write()

    def resolved_snapshot(self) -> dict[str, list[str]]:
        """Read-only snapshot of {host: [ip, ...]} for callers that need to
        persist the full resolution map (used by _phase2_worker to feed
        _save_resolution_cache)."""
        with self._lock:
            return dict(self.resolved)

    def _write(self) -> None:
        """Atomic rewrite of the pending-status file. Caller holds the lock.
        No-op before init() has set a path — defensive against spurious
        early calls."""
        if self._path is None:
            return
        write_text(self._path, self._format_yml())

    def _format_yml(self) -> str:
        """Render the agent-visible YAML body. Caller holds the lock. Sections
        explicitly commented so the agent can interpret each state correctly
        without reading the launcher's source."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lines = [
            "# {auto}-mode firewall whitelist — pending/failed view",
            "# Auto-generated by the host launcher; updated as DNS resolution",
            "# progresses. The agent uses this file to classify a connection",
            "# refused: still pending vs. terminally failed vs. neither.",
            "",
            f"status: {self.status}    # 'resolving' = host still working;  'complete' = every entry resolved or terminally failed",
            f"last_updated: {now}",
            "",
            "# Pending — host is still resolving these. Rule may arrive within seconds;",
            "# if you can't reach a pending host, retry shortly before surfacing to the user.",
            "pending:",
        ]
        for host in sorted(self.pending):
            lines.append(f"  - {host}")
        lines.append("")
        lines.append("# Failed — DNS resolution failed terminally for these (likely IPv6-only,")
        lines.append("# dead host, typo, or transient error). Won't be reachable this session;")
        lines.append("# surface to the user if you need one — a re-launch may succeed.")
        lines.append("failed:")
        for host in sorted(self.failed):
            lines.append(f"  {host}: {self.failed[host]}")
        lines.append("")
        lines.append("# CDN-widened — these hosts resolved into a known CDN provider's published")
        lines.append("# range, so the whole containing block was whitelisted (rotation-proof)")
        lines.append("# rather than pinning the momentary IPs. Note: a provider block is shared")
        lines.append("# by every customer of that CDN.")
        lines.append("cdn:")
        for host in sorted(self.cdn):
            lines.append(f"  {host}: {self.cdn[host]}")
        return "\n".join(lines) + "\n"


_status = _WhitelistResolutionStatus()


# === Thread/concurrency plumbing — kept as flat module-level globals ===
# These are the executors / futures / queues / threads that orchestrate
# Phase 1 + Phase 2 + the iptables-updater. They don't share invariants the
# way the status surface does (no shared lock; each piece has independent
# lifecycle), so they stay as globals here.

# Phase 1 (critical) — synchronous from the caller's perspective via a
# blocking await on _phase1_future.result(). All three are set together
# inside start_whitelist_resolution; the public-API guards key off
# _phase1_future / _phase2_queue is None for "not started yet".
_phase1_executor: ThreadPoolExecutor | None = None
_phase1_future: "Future[list[str]] | None" = None      # → list of address strings ready for WHITELIST_ADDRESSES

# Phase 2 (rest) — producer thread writes ready-to-open `addr[:port]` token
# strings to this queue; the updater thread drains them in bursts.
_phase2_queue: queue.Queue | None = None
_phase2_done = object()    # sentinel: end-of-stream
_phase2_thread: threading.Thread | None = None

# Firewall updater — daemon thread; lifetime bounded by the launcher process,
# which itself blocks inside `docker compose run` for the container's lifetime.
_updater_thread: threading.Thread | None = None


def _expand_whitelist(raw_entries: Iterable[str]) -> tuple[list[str], list[HostnameEntry]]:
    """Pure expansion of the raw whitelist into work items: dedupe, strip
    `*.` wildcard prefixes, add the bare apex for every `www.X` entry (typing
    the `www.` form clearly means the apex too), then split literal IP/CIDR
    entries (pass straight to iptables, no DNS) from hostnames that need
    resolution. Both returned lists are sorted for deterministic downstream
    ordering. Extracted as a pure function so the security-relevant
    transformation is unit-testable without threads or DNS."""
    deduped = {d.removeprefix("*.") for d in set(raw_entries)}
    deduped |= {d.removeprefix("www.") for d in deduped if d.startswith("www.")}
    literals: list[str] = []
    hostnames: list[HostnameEntry] = []
    for entry in sorted(deduped):
        host, port = split_host_port(entry)
        if _IP_OR_CIDR_RE.match(host):
            literals.append(entry)
        else:
            hostnames.append(HostnameEntry(entry, host, port))
    return literals, hostnames


def _index_by_host(entries: list[HostnameEntry]) -> dict[str, list[tuple[str, str]]]:
    """Build `{host: [(entry, port), ...]}` from a list of (entry, host, port)
    triples — multiple entries can share a host (the user wrote both
    `api.anthropic.com:443` and the bare apex, or apex+www stripping produced
    duplicates). The dict turns the per-resolve scan in each `on_ok` from O(N)
    into O(1)."""
    out: dict[str, list[tuple[str, str]]] = {}
    for e in entries:
        out.setdefault(e.host, []).append((e.entry, e.port))
    return out


def _phase1_worker(critical_hostnames: list[HostnameEntry], literal_entries: list[str], rest_hostnames: list[HostnameEntry]) -> list[str]:
    """Phase 1 body: cascade through critical hosts (Anthropic), then kick off
    Phase 2 in its own thread before returning. Result is the list of address
    strings to stage as WHITELIST_ADDRESSES for the initial firewall — that's
    critical IPs plus literal IP/CIDR entries (which need no resolution).
    Raises if any critical host fails terminally — those are non-optional and
    the launcher should abort loudly rather than start a half-broken agent.
    Critical hosts get the same CDN widening as Phase 2 (api.anthropic.com is
    CDN-fronted — widening it in the INITIAL ruleset is what protects the
    very first request against POP rotation)."""
    critical_addresses: list[str] = []
    critical_failed: list[str] = []
    by_host = _index_by_host(critical_hostnames)

    def on_ok(host: str, ips: list[str]) -> None:
        widened: str | None = None
        for _entry, port in by_host.get(host, []):
            tokens, provider = _tokens_for(host, ips, port)
            widened = widened or provider
            critical_addresses.extend(tokens)
        _status.mark_resolved(host, ips, cdn=widened)

    def on_fail(host: str) -> None:
        _status.mark_failed(host, _FAILED_RESOLVE_REASON)
        critical_failed.append(host)

    _cascade(by_host, on_ok, on_fail)

    if critical_failed:
        raise RuntimeError(
            f"Critical Anthropic domains failed to resolve: {critical_failed}. "
            f"Claude Code cannot operate without them; aborting launch."
        )

    # Phase 2 starts now — runs in its own thread, producer side of _phase2_queue.
    global _phase2_thread
    _phase2_thread = threading.Thread(
        target=_phase2_worker, args=(rest_hostnames,),
        daemon=True, name="phase2-cascade",
    )
    _phase2_thread.start()

    return critical_addresses + list(literal_entries)


def _phase2_worker(rest_hostnames: list[HostnameEntry]) -> None:
    """Phase 2 body: cascade through non-critical hosts. For each successful
    resolution, push the ready-to-open `addr[:port]` tokens (CDN-widened
    where applicable — see _tokens_for) onto `_phase2_queue` for the updater
    to batch into the container; for each terminal failure, just log via
    status file (no iptables work to do). Pushes `_phase2_done` sentinel
    last, flips the status file to 'complete', and rewrites the cross-launch
    resolution cache with everything resolved this launch (cache hits +
    fresh DNS) so the next launch's is_file_recent check sees a freshened
    mtime."""
    # Spawned only from _phase1_worker (which itself runs inside
    # start_whitelist_resolution's executor — created after _phase2_queue
    # was initialized). The assertion narrows the Optional for the type
    # checker AND would surface a programming error if any future caller
    # ever bypasses start_whitelist_resolution.
    assert _phase2_queue is not None
    q = _phase2_queue
    by_host = _index_by_host(rest_hostnames)

    def on_ok(host: str, ips: list[str]) -> None:
        widened: str | None = None
        for _entry, port in by_host.get(host, []):
            tokens, provider = _tokens_for(host, ips, port)
            widened = widened or provider
            for token in tokens:
                q.put(token)
        _status.mark_resolved(host, ips, cdn=widened)

    def on_fail(host: str) -> None:
        _status.mark_failed(host, _FAILED_RESOLVE_REASON)

    _cascade(by_host, on_ok, on_fail)

    _status.complete()
    _save_resolution_cache(_status.resolved_snapshot())
    q.put(_phase2_done)


def start_whitelist_resolution(state_dir: Path) -> None:
    """Reset the agent-visible status surface to a clean 'resolving' state,
    then fire Phase 1 (critical Anthropic — synchronous from the caller's
    perspective via wait_for_critical_addresses); once Phase 1 finishes
    Phase 2 (the rest, streaming) kicks off automatically. Idempotent;
    re-calling is a no-op past the first (so a re-call won't wipe the
    in-flight status either).

    Loads the cross-launch resolution cache up front so _resolve_a_records
    short-circuits any host that's been resolved recently. The cache file
    is rewritten at end of Phase 2.

    Pairs with:
      - wait_for_critical_addresses() — block until Phase 1 returns the
        initial WHITELIST_ADDRESSES set
      - start_firewall_updater(container_name) — spawn the daemon that
        consumes Phase 2 and incrementally inserts iptables rules"""
    global _phase1_executor, _phase1_future, _phase2_queue
    if _phase1_future is not None:
        return   # idempotent

    _status.init(state_dir)
    _load_resolution_cache()
    _seen_cdn_ranges.clear()

    literals, hostnames = _expand_whitelist([*BUILTIN_FIREWALL_DOMAINS, *user_firewall_whitelist_lines()])
    critical = [t for t in hostnames if t.host in _CRITICAL_HOSTS]
    rest = [t for t in hostnames if t.host not in _CRITICAL_HOSTS]

    # Populate the pending list now that we've assembled it (clearing already
    # zeroed everything else in _status).
    _status.set_pending({t.host for t in hostnames})

    _phase2_queue = queue.Queue()

    # Phase 1 runs in a single-worker executor so the caller can wait_for_critical_addresses()
    # later. Phase 1 internally kicks off Phase 2 once it finishes.
    _phase1_executor = ThreadPoolExecutor(max_workers=1)
    _phase1_future = _phase1_executor.submit(_phase1_worker, critical, literals, rest)


def is_critical_pending() -> bool:
    """True iff Phase 1 (critical Anthropic resolve) is in flight but not yet
    finished. docker_config.run_compose uses this to print a 'still resolving'
    indicator before the blocking await — so the user doesn't see a silent
    stall and think the launcher hung."""
    return _phase1_future is not None and not _phase1_future.done()


def wait_for_critical_addresses() -> list[str] | None:
    """Block until Phase 1 (critical Anthropic) completes and return the
    list of address strings to stage as WHITELIST_ADDRESSES for the initial
    in-container firewall. Returns None if start_whitelist_resolution() was
    never called (e.g. non-{auto} launches don't need a firewall). If a
    critical host failed terminally, _phase1_worker raised RuntimeError and
    that propagates here — the launcher should abort rather than start a
    half-broken agent."""
    if _phase1_future is None:
        return None
    # _phase1_executor is set alongside _phase1_future in start_whitelist_resolution;
    # if we reached this branch they're both initialized.
    assert _phase1_executor is not None
    try:
        return _phase1_future.result()
    finally:
        _phase1_executor.shutdown(wait=False)


def start_firewall_updater(container_name: str) -> None:
    """Spawn a daemon thread that consumes Phase 2's `_phase2_queue` of
    `addr[:port]` tokens and applies them to the running container's iptables
    in BATCHES — each burst of queued tokens becomes one `docker exec --user
    root <container> sh -c '<rule> && <rule> && ...'` instead of one exec per
    rule. At ~50-150ms per docker exec, the old per-rule pace meant the tail
    of a ~135-domain whitelist landed minutes after launch; batching applies
    each resolution burst in a handful of execs (see
    benchmark/bench_firewall_updater.py for measured pacing).

    Best-effort: a failed batch (container exited mid-resolve, race on
    teardown, etc.) retries once, then logs a stderr warning and moves on —
    by that point Claude Code already has critical Anthropic access via the
    initial ruleset, and a missing docs domain isn't worth aborting.

    No-op when Phase 2 isn't active (non-{auto} launches)."""
    global _updater_thread
    if _phase2_queue is None or _updater_thread is not None:
        return
    _updater_thread = threading.Thread(
        target=_updater_worker, args=(container_name,),
        daemon=True, name="firewall-updater",
    )
    _updater_thread.start()


def _updater_worker(container_name: str) -> None:
    """Daemon body for the updater. Wait briefly for the container to be
    running (docker compose run takes a moment to spin it up), then drain
    `_phase2_queue` in bursts: block for the first token, opportunistically
    scoop up everything else already queued, and flush the whole burst as
    one batched docker exec. Wall-clock pace is therefore one exec per
    resolution burst (~cascade pass), not one per rule. Lazy import of
    wait_for_container_running breaks the docker_config↔network import
    cycle (see module-top docstring)."""
    from .docker_config import wait_for_container_running

    if not wait_for_container_running(container_name):
        # Container never came up; nothing to update. Caller's docker compose
        # run will already have surfaced the underlying error.
        return

    # Updater is spawned only from start_firewall_updater, which guards on
    # `_phase2_queue is None` — so by this point the queue is initialized.
    assert _phase2_queue is not None
    q = _phase2_queue
    while True:
        first = q.get()
        if first is _phase2_done:
            return
        batch = [first]
        finished = False
        while True:   # drain whatever else has resolved by now — no waiting
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            if item is _phase2_done:
                finished = True
                break
            batch.append(item)
        _flush_rules(container_name, batch)
        if finished:
            return


# Rules per `docker exec sh -c` invocation — bounds the argv/script size.
# A full-whitelist launch is a few hundred rules → a handful of execs.
_UPDATER_BATCH_MAX_RULES = 100


def _iptables_rules_for(token: str) -> list[str]:
    """iptables command strings opening `token` (`addr[:port]`; addr may be a
    CIDR) at position 1 of the OUTPUT chain — BEFORE the catch-all REJECT.
    Port absent → the default HTTPS+HTTP pair. Tokens failing the strict
    address/port validation are dropped with a warning: these strings get
    joined into a `sh -c` script, so nothing that hasn't matched
    `^[0-9./]+$`-shaped patterns may pass (defense in depth on top of
    _resolve_a_records' own output validation)."""
    addr, port = split_host_port(token)
    if not _IP_OR_CIDR_RE.match(addr) or (port and not port.isdigit()):
        print(f"  warning: dropping malformed firewall token {token!r}", file=sys.stderr)
        return []
    ports = [port] if port else list(_DEFAULT_OPEN_PORTS)
    return [f"iptables -I OUTPUT 1 -d {addr} -p tcp --dport {p} -j ACCEPT" for p in ports]


def _flush_rules(container_name: str, tokens: list[str]) -> None:
    """Apply `tokens` to the running container's iptables in chunks of
    ≤_UPDATER_BATCH_MAX_RULES rules, one `docker exec --user root sh -c`
    per chunk. `&&`-joined so a mid-chunk failure surfaces as a non-zero
    exit; each failed chunk retries once (duplicate -I inserts from a
    partially-applied first attempt are harmless) then warns and moves on
    — best-effort, same policy the per-rule updater had. Lazy import of
    docker_exec_root_subprocess breaks the docker_config↔network import
    cycle (see module-top docstring)."""
    from .docker_config import docker_exec_root_subprocess

    rules = [rule for token in tokens for rule in _iptables_rules_for(token)]
    for i in range(0, len(rules), _UPDATER_BATCH_MAX_RULES):
        script = " && ".join(rules[i:i + _UPDATER_BATCH_MAX_RULES])
        result = docker_exec_root_subprocess(container_name, "sh", "-c", script)
        if result.returncode != 0:
            result = docker_exec_root_subprocess(container_name, "sh", "-c", script)   # one retry — transient exec races
        if result.returncode != 0:
            print(
                f"  warning: batched iptables insert failed ({len(rules[i:i + _UPDATER_BATCH_MAX_RULES])} rules): "
                f"{result.stderr.strip() or result.stdout.strip()}",
                file=sys.stderr,
            )
