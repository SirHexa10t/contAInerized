"""The `{firewall}` whitelist resolver's coordinator — the core of the
`launch.firewall` package. DNS resolution happens here, on the host, before
`docker run` spins up the container. In-container, init-firewall.sh just
writes iptables rules from the pre-resolved address list — no DNS calls, no
parallel-xargs plumbing, no `getent` timeouts to babysit.

Four self-contained concerns live in sibling submodules: `.whitelist` — the
pure raw-entry → work-item expansion (HostnameEntry, _expand_whitelist,
_index_by_host); `.status` — the lock-guarded `domains_pending_resolve.yml`
tracker + its `_status` singleton; `.cdn_ranges` — the fetched-and-cached
provider block table this module widens against (`load()` once per launch,
`provider_ranges(ips)` per resolution); `.iptables` — token → rule strings
and the batched apply into a live container.

What stays here is the coupled core: the DNS cascade + cross-launch cache,
the widening POLICY, and the two-phase + updater + refresher thread
orchestration. Those share
sequenced, in-place-mutated module state (no locks; strict phase hand-off)
that only stays correct as one module. The package's public API is re-
exported from `launch/firewall/__init__.py`; consumers import from there.

Concurrency model (two-phase, streaming):
  - Phase 1 (critical): api.anthropic.com + console.anthropic.com resolve
    synchronously (from the caller's perspective) — these MUST be in the
    container's initial iptables ruleset, so the launcher blocks until
    they're known. tag_handlers._apply_firewall fires the whole thing via
    start_whitelist_resolution(state_dir) during apply_tags;
    docker_config.run_container awaits via wait_for_critical_addresses()
    before staging WHITELIST_ADDRESSES and firing `docker run`.
  - Phase 2 (rest): every other whitelist entry resolves in a background
    thread that streams ready-to-open `addr[:port]` tokens onto an internal
    queue. A daemon updater thread (start_firewall_updater, spawned right
    before `docker run`) drains the queue in bursts and applies each
    burst with ONE `docker exec --user root <container> sh -c 'iptables -I
    OUTPUT 1 ... && ...'` — batching dozens of ACCEPT rules per exec instead
    of one exec per rule (per-rule pacing would take minutes for the full
    list; see benchmark/bench_firewall_updater.py). Rules insert BEFORE the
    catch-all REJECT, so arrival order doesn't matter. The launcher proceeds
    into Claude Code as soon as Phase 1 finishes; Phase 2 + the updater run
    alongside Claude Code's startup, growing the firewall in real time.

Status surface: one file per launch — `domains_pending_resolve.yml` inside
the per-instance state dir (bind-mounted into the container at
/home/claude/.claude/). Holds status + pending list + failed list;
the Firewall addendum points the agent here for "I hit a connection refused"
classification. Rewritten atomically as the picture changes.

Cross-launch DNS cache: RESOLVED_DOMAINS_CACHE_FILE (in firewall_cache/ under
AGENTS_STATE). The cache never REPLACES a lookup — every host gets fresh DNS every
launch (a cached answer can go stale the moment the user's VPN exit or the
CDN's steering changes, and a relaunch must pick up the new truth). While
fresh (mtime gated by is_file_recent in utils) it contributes two things:
its IPs are UNIONED into each successful resolution (host-resolver vs
container-resolver divergence: rules for both answers), and it's the
FALLBACK when a host's DNS fails outright (flaky VPN resolver mid-launch).
Rewritten with this launch's fresh DNS results only — never the union — so
a dead IP ages out after one TTL instead of being immortalized by the
rolling mtime.

Post-launch drift healing: DNS pins go stale MID-session too (VPN node
swap, CDN steering change). Once Phase 2 finishes, the updater
hands off to a refresher daemon (_refresher_worker) that re-resolves the
whole hostname list every _REFRESH_INTERVAL_SECONDS and batch-inserts
rules for any address not already emitted. Rules only accumulate (additive
ACCEPTs before the REJECT); nothing is ever revoked mid-session, so a
transient DNS failure can't cut off a working host.

Module boundary vs tag_handlers: the resolution policy + curated domain
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
CDN provider's published IPv4 block get the whole containing block
whitelisted instead of the momentary IPs, so POP rotation inside the block
can't strand them. The provider blocks are NOT baked into the source —
each provider's published range list is fetched live (see `.cdn_ranges`)
and cached on disk between launches. The pinning
caveat above still fully applies to hosts OUTSIDE any known block, and to
entries with an explicit :port (those stay pinned — opening a whole
provider block on a custom port is a broader grant than the entry asked
for). See _tokens_for for the policy and `.cdn_ranges` for the security
tradeoff this widening deliberately makes.

Wildcard entries: `*.example.com` means "subdomains too — including ones
that can't be known in advance" (some CDNs mint per-request shard
hostnames that only exist once the service hands them out). DNS can't
enumerate a wildcard, so the policy is provider-shaped: resolve the base
host, and if it sits on a known provider, open ALL of that provider's
published blocks (not just the containing one) — any subdomain served
from that provider's edge is then covered. A wildcard whose base isn't on
a known provider degrades to base-host pinning and is called out in the
status file's `wildcard_gaps:` section rather than silently half-working.

IPv6 entries are skipped, not resolved-and-failed: the whole pipeline is
IPv4 (getent ahostsv4 → iptables), docker networks are v4-only unless the
daemon opts in, and init-firewall.sh slams v6 egress shut regardless. A
v6 literal in the whitelist lands in the status file's `skipped:` section
with the reason, instead of burning the full DNS cascade and polluting
`failed:`.

Imports nothing heavy: the four sibling submodules (.whitelist, .cdn_ranges,
.iptables, .status), file_access for the cache files + atomic write helper,
paths for the cache locations, template_code for the curated domain list,
stdlib for subprocess + threading + ipaddress. tag_handlers._apply_firewall
is the entry point caller (calls start_whitelist_resolution during
apply_tags); docker_config.run_container pairs the await + updater-spawn.

Cycle note: docker_config imports the `launch.firewall` facade (for
is_critical_pending / wait_for_critical_addresses / start_firewall_updater),
which imports this module — so nothing here may import docker_config back.
The two calls that need to reach a live container (`_updater_worker`'s
readiness gates) take them from `container_probe`, a LEAF module that exists
precisely so this needs no lazy call-time import; `.iptables` takes its
`docker exec` from there for the same reason (the cycle both dodge is
described in that module)."""

import ipaddress
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from ..ai import active_harness
from ..file_access import (
    force_remove, is_file_recent, parse_lines, user_firewall_whitelist_lines, write_text,
)
from ..container_probe import (
    wait_for_container_running,
    wait_for_firewall_applied,
)
from ..paths import AGENTS_STATE, RESOLVED_DOMAINS_CACHE_FILE
from . import cdn_ranges, iptables
from .status import _status
from .whitelist import (
    HostnameEntry, _expand_whitelist, _index_by_host,
)
# Re-exported (unused here): the test suite treats this module as the firewall
# namespace it patches/reads, so the two submodules' names stay reachable as
# `resolver.X` (the package facade re-exports only the public API).
from .status import _WhitelistResolutionStatus                                # noqa: F401
from .whitelist import _SKIPPED_IPV6_REASON, _is_ipv6_literal                 # noqa: F401
from ..template_code.firewall_domains import BUILTIN_FIREWALL_DOMAINS
from ..utils import shell_capture


# ============================================================
# Always-allowed domains — data lives in template_code
# ============================================================
# BUILTIN_FIREWALL_DOMAINS (the curated always-allowed list; the user's
# firewall_whitelist.txt is unioned in at start_whitelist_resolution time)
# is pure data — it lives in template_code/firewall_domains.py per that
# package's data-only convention. The CDN provider ranges that drive the
# widening below are NOT data anywhere in the source: they're fetched from
# each provider's published list (see the CDN provider ranges section).
# Inside the container, init-firewall.sh reads pre-resolved addresses via
# $WHITELIST_ADDRESSES — no DNS dependency in the firewall hot path.


# ============================================================
# DNS resolution
# ============================================================
# Entry parsing (hostname / :port / literal IPv4 / CIDR / `*.` wildcard) lives
# in .whitelist._expand_whitelist; each resolved-or-literal entry
# becomes one or more `<ip>[:port]` / `<cidr>[:port]` strings — Phase 1 collects
# them for the initial WHITELIST_ADDRESSES env-var (consumed by
# init-firewall.sh at container start), Phase 2 streams them to the updater
# for `docker exec iptables -I` into the running container.

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

# Shared TTL for both cross-launch caches: the resolved-domains file
# (RESOLVED_DOMAINS_CACHE_FILE) and the per-provider CDN-range files, both in
# firewall_cache/. Three days is safe on both fronts because neither
# cache ever substitutes for live data: resolutions always run fresh DNS
# (cached IPs only union in and rescue failures — stale entries can only ADD
# rules, never mask a new address), and provider ranges change on the order
# of months, so a 3-day-old published list is effectively current. Freshness
# check happens via is_file_recent in file_access; the launcher itself never
# inspects time.
_CACHE_TTL_SECONDS = 3 * 24 * 60 * 60

# In-process cache: {host: [ip, ...]} loaded from RESOLVED_DOMAINS_CACHE_FILE
# at the start of start_whitelist_resolution, consulted by _resolve_a_records
# as a union-partner / fallback (never a substitute for fresh DNS). Empty when
# the on-disk cache is missing or stale. Read-only from cascade worker threads
# (only the main thread writes, before the pool starts), so no lock needed.
_resolution_cache: dict[str, list[str]] = {}

# What actually gets persisted back to RESOLVED_DOMAINS_CACHE_FILE: only this
# launch's FRESH getent answers — never the cache-union — so a stale IP ages
# out after one TTL instead of being immortalized by each save refreshing the
# file's mtime. Written from resolver worker threads; per-key assignment of an
# immutable value is atomic under the GIL and each host is queried by at most
# one worker at a time (cascade stages are sequential, the refresher runs
# after Phase 2 has finished), so no lock.
_fresh_resolutions: dict[str, list[str]] = {}


# `getent` is glibc-only; on hosts without it (notably macOS) fall back to the
# stdlib resolver. Detected once — it's a static property of the host.
_HAVE_GETENT = shutil.which("getent") is not None


def _resolve_a_records(host: str, timeout: float) -> list[str]:
    """Resolve `host` to its IPv4 A records, ALWAYS querying live DNS — the
    cross-launch cache never substitutes for a lookup. On success, returns the
    fresh IPs unioned with the cached ones (host resolver and container resolver
    can disagree; rules for both answers keep either path open) and records the
    fresh answer in _fresh_resolutions for the end-of-phase cache save. On
    timeout / NXDOMAIN / IPv6-only / lookup error, falls back to the cached IPs
    — [] only when the cache has nothing either, which lets _cascade retry.

    Linux resolves via `getent ahostsv4` (subprocess with a real timeout —
    cleaner cancellation than socket.getaddrinfo, which has no kwarg timeout).
    Hosts without getent (macOS) fall back to socket.getaddrinfo, whose wait is
    bounded by the OS resolver. Either way, tokens are validated against
    _IPV4_RE — resolver output is the one externally-controlled string in this
    pipeline, and everything downstream (WHITELIST_ADDRESSES, the batched
    `sh -c` iptables script) must only ever see well-formed addresses."""
    cached = _resolution_cache.get(host, [])
    fresh = _getent_a_records(host, timeout) if _HAVE_GETENT else _socket_a_records(host)
    if not fresh:
        return list(cached)
    _fresh_resolutions[host] = fresh
    return sorted({*fresh, *cached})


def _getent_a_records(host: str, timeout: float) -> list[str]:
    """Fresh IPv4 A records via `getent ahostsv4` (Linux); [] on timeout,
    non-zero exit, or no A record. Tokens validated against _IPV4_RE."""
    try:
        r = shell_capture("getent", "ahostsv4", host, timeout=timeout)
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0:
        return []
    tokens = {line.split()[0] for line in r.stdout.splitlines() if line.strip()}
    return sorted(t for t in tokens if _IPV4_RE.match(t))


def _socket_a_records(host: str) -> list[str]:
    """Fresh IPv4 A records via socket.getaddrinfo — the cross-platform fallback
    for hosts without `getent` (macOS). No kwarg timeout; the wait is bounded by
    the OS resolver. Tokens validated against _IPV4_RE."""
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET)
    except OSError:
        return []
    tokens = {str(info[4][0]) for info in infos}   # sockaddr[0] is the IP; str() also satisfies the type checker
    return sorted(t for t in tokens if _IPV4_RE.match(t))


def _load_resolution_cache() -> None:
    """Populate the in-process resolution cache from RESOLVED_DOMAINS_CACHE_FILE
    when the file is fresh (is_file_recent checks mtime). A stale or missing
    file leaves the cache empty — hosts then run on fresh DNS alone, with no
    union partner or failure fallback, and the file is rebuilt from scratch
    by _save_resolution_cache at end of Phase 2. Format: one `host=ip[,ip]*`
    line per entry."""
    global _resolution_cache
    _resolution_cache = {}
    if not is_file_recent(RESOLVED_DOMAINS_CACHE_FILE, _CACHE_TTL_SECONDS):
        return
    for line in parse_lines(RESOLVED_DOMAINS_CACHE_FILE):
        host, sep, ips_part = line.partition("=")
        if sep:
            ips = [ip.strip() for ip in ips_part.split(",") if ip.strip()]
            if ips:
                _resolution_cache[host.strip()] = ips


def _save_resolution_cache(resolved: dict[str, list[str]]) -> None:
    """Rewrite RESOLVED_DOMAINS_CACHE_FILE from `resolved` (a {host: [ip, ...]}
    snapshot). Called with _fresh_resolutions — this launch's live getent
    answers only, deliberately excluding cache-union carryover (see
    _fresh_resolutions). Fires at end of Phase 2 and again after each
    refresher pass that found changes, so the file tracks current truth.
    write_text refreshes mtime, so the TTL window starts from this moment for
    the next launch's is_file_recent check."""
    lines = [
        "# {firewall} resolved-domains cache.",
        f"# TTL: {_CACHE_TTL_SECONDS // (60 * 60 * 24)} days since this file's mtime. While fresh,",
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
#     Resolved synchronously, before `docker run` fires — Claude Code
#     cannot do anything without api.anthropic.com, so it MUST be in the
#     initial iptables ruleset that init-firewall.sh applies at container
#     start. Failing here aborts the launch (loud).
#   Phase 2 ("rest"): everything else from BUILTIN_FIREWALL_DOMAINS ∪ the
#     user's firewall_whitelist.txt. Resolved in a background thread while
#     the launcher fires `docker run` and Claude Code starts. As
#     each domain resolves on the host, a sibling "updater" thread (see
#     start_firewall_updater) runs `docker exec --user root <container>
#     iptables -I OUTPUT 1 -d <ip> ...` to insert an ACCEPT rule into the
#     running container's iptables — BEFORE the catch-all REJECT, so the
#     order in which rules arrive doesn't matter.
#
# Per-resolution progress lands on the agent-visible status surface — see the
# `_WhitelistResolutionStatus` section below. Cross-launch DNS results land
# in RESOLVED_DOMAINS_CACHE_FILE under firewall_cache/ (host-side, not mounted).
#
# Security model: the host-side `docker exec` route bypasses the container's
# sudoers (claude can't sudo iptables, but the launcher running outside the
# container can — docker exec --user root grants root from outside the
# namespace). claude has no influence over what the launcher decides to
# resolve — the whitelist is computed once at the start, in Python, from
# files on the host, and the post-launch growth (Phase 2 stream + refresher
# passes) only ever re-resolves that same host-side list. So the firewall
# growing post-launch doesn't relax the claude-can't-modify-firewall
# invariant.

def _critical_hosts() -> tuple[str, ...]:
    """The hosts the active harness cannot operate without — from its adapter
    record (`launch/ai`), read at call time so a switch of AI moves them with
    the abort message below."""
    return active_harness().critical_hosts

# The critical hosts are served from Anthropic's OWN registered space — not a
# CDN (verified via ARIN RDAP 2026-07-21: NET-160-79-104-0-1 "AP-2440",
# Anthropic PBC, 160.79.104.0/21). A single momentary A record is the most
# drift-fragile rule shape there is, and these are the two hosts the agent
# cannot live without — so phase 1 widens their pins to this whole block,
# making IP rotation inside Anthropic's space a non-event. The block is
# static registered space, safe to keep as data (unlike the CDN provider
# ranges, which are fetched live because they churn).
_ANTHROPIC_BLOCKS = ("160.79.104.0/21",)
_ANTHROPIC_WIDEN_LABEL = "anthropic (own registered block)"

# The momentary resolved IP of the FIRST critical host, kept for the
# in-container self-test: init-firewall.sh probes it via `curl --resolve`
# (handed over as the script's $1), so the positive enforcement check never
# depends on the container's DNS latency. Set by phase 1; None before it.
_selftest_addr: str | None = None


def selftest_address() -> str | None:
    """The launcher-resolved api.anthropic.com IP for the in-container
    firewall self-test, or None when phase 1 hasn't resolved it (yet)."""
    return _selftest_addr


# Plain IPv4 (no CIDR suffix) — what a validated resolver token must look like.
_IPV4_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")


# CIDR tokens (`<cidr>` or `<cidr>:<port>` for wildcard-with-port entries)
# already widened this launch — each is opened at most once no matter how
# many hosts resolve into it. Phase 1, Phase 2, and the refresher run
# strictly sequentially (Phase 2's thread starts when Phase 1 finishes; the
# refresher starts when the updater sees Phase 2's end-of-stream sentinel)
# and each one's callbacks run serially in its own worker thread, so no lock.
_seen_cdn_ranges: set[str] = set()


def _tokens_for(host: str, ips: list[str], port: str, wildcard: bool = False) -> tuple[list[str], str | None, bool]:
    """The `addr[:port]` tokens to open for a resolved entry, the CDN provider
    label when widening happened (None otherwise), and a wildcard-gap flag
    (True when a `*.` entry could NOT be honored beyond its base host).

    Policy:
      - No CDN match → pin the resolved IPs. For a wildcard entry that's a
        gap: subdomains on other IPs stay blocked, and the status file says
        so (`wildcard_gaps:`) instead of letting it silently half-work.
      - CDN match + wildcard → open ALL the provider's published blocks, not
        just the containing ones. Subdomains can't be enumerated via DNS
        (per-request shard hostnames exist only once the service mints
        them), so "the provider's whole edge" is the only IP-shaped grant
        that matches what `*.` asks for. An explicit :port narrows every
        block token to that port rather than downgrading to pinning — a
        wildcard without widening would be meaningless.
      - CDN match, plain entry, explicit :port → pin. (Deliberate: opening a
        whole provider block on a custom port is a broader grant than the
        entry asked for.)
      - CDN match, plain entry, default ports → emit each containing block
        once per launch (_seen_cdn_ranges dedupes across hosts) plus any
        resolved IP that falls OUTSIDE the matched blocks (mixed A records:
        some edge, some origin). IPs covered by a block — emitted now or by
        an earlier host — need no rule of their own.

    `host` is unused in the computation but kept in the signature so call
    sites read naturally and future per-host policy has its hook."""
    provider, ranges = cdn_ranges.provider_ranges(ips)
    if provider is None or (port and not wildcard):
        return [f"{ip}:{port}" if port else ip for ip in ips], None, wildcard and provider is None
    if wildcard:
        ranges = cdn_ranges.blocks_for(provider)
    block_tokens = [f"{c}:{port}" if port else c for c in ranges]
    new_tokens = [t for t in block_tokens if t not in _seen_cdn_ranges]
    _seen_cdn_ranges.update(new_tokens)
    networks = [ipaddress.IPv4Network(c) for c in ranges]
    uncovered = [ip for ip in ips if not any(ipaddress.IPv4Address(ip) in n for n in networks)]
    return new_tokens + [f"{ip}:{port}" if port else ip for ip in uncovered], provider, False




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
# which itself blocks inside `docker run` for the container's lifetime.
_updater_thread: threading.Thread | None = None

# Refresher — daemon thread the updater hands off to once Phase 2's stream
# ends. Re-resolves the whole hostname list forever (every
# _REFRESH_INTERVAL_SECONDS) and flushes rules for addresses DNS newly
# reports — the mid-session self-heal for VPN-exit swaps and CDN steering
# changes that strand pinned hosts. Additive only: rules are never revoked
# mid-session.
_refresher_thread: threading.Thread | None = None

# Every `addr[:port]` token already opened this launch (initial ruleset +
# updater batches). The refresher diffs against this so each re-resolve
# flushes only genuinely new addresses. Same sequential-access story as
# _seen_cdn_ranges (phase 1 → phase 2 → refresher hand off strictly).
_emitted_tokens: set[str] = set()

# {host: [HostnameEntry, ...]} over the ENTIRE expanded whitelist (critical +
# rest), built once per launch in start_whitelist_resolution. The phase
# workers and the refresher all map a resolved host back to its entries here.
_all_entries_by_host: dict[str, list[HostnameEntry]] = {}


def _emit_tokens_for_host(host: str, ips: list[str]) -> tuple[list[str], str | None]:
    """The not-yet-emitted `addr[:port]` tokens for every whitelist entry
    sharing `host`, plus the status-file CDN annotation (provider name,
    wildcard-widened variant, or None). Updates _emitted_tokens and files any
    wildcard gap with the status tracker. The single funnel all three
    emitters go through — phase 1 (→ WHITELIST_ADDRESSES), phase 2 (→ the
    updater queue), refresher (→ direct flush) — so dedupe and annotation
    policy can't drift between them. Callers run strictly sequentially (see
    _seen_cdn_ranges), so the shared sets need no lock."""
    new_tokens: list[str] = []
    label: str | None = None
    for e in _all_entries_by_host.get(host, []):
        tokens, provider, gap = _tokens_for(host, ips, e.port, e.wildcard)
        if provider:
            label = f"{provider} (all blocks — wildcard)" if e.wildcard else (label or provider)
        if gap:
            _status.mark_wildcard_gap(host)
        new_tokens.extend(t for t in tokens if t not in _emitted_tokens)
        _emitted_tokens.update(tokens)
    return new_tokens, label


def _phase1_worker(critical_hostnames: list[HostnameEntry], literal_entries: list[str], rest_hostnames: list[HostnameEntry]) -> list[str]:
    """Phase 1 body: cascade through critical hosts (Anthropic), then kick off
    Phase 2 in its own thread before returning. Result is the list of address
    strings to stage as WHITELIST_ADDRESSES for the initial firewall — that's
    critical IPs plus literal IP/CIDR entries (which need no resolution).
    Raises if any critical host fails terminally — those are non-optional and
    the launcher should abort loudly rather than start a half-broken agent.
    Critical pins are widened to _ANTHROPIC_BLOCKS (the API is served from
    Anthropic's own registered range, not a CDN — see the constant), and the
    first critical's momentary IP is recorded for the in-container self-test
    (selftest_address). The generic CDN widening still applies too — inert
    today, but if Anthropic ever fronts these hosts with a known provider it
    resumes without a code change — which is why the provider ranges load
    here, before the first resolution callback can fire."""
    cdn_ranges.load()
    critical_addresses: list[str] = list(literal_entries)
    _emitted_tokens.update(literal_entries)
    critical_failed: list[str] = []

    def on_ok(host: str, ips: list[str]) -> None:
        global _selftest_addr
        tokens, cdn_label = _emit_tokens_for_host(host, ips)
        fresh_blocks = [b for b in _ANTHROPIC_BLOCKS if b not in _emitted_tokens]
        _emitted_tokens.update(fresh_blocks)
        critical_addresses.extend(tokens + fresh_blocks)
        if host == _critical_hosts()[0] and ips and _selftest_addr is None:
            _selftest_addr = ips[0]
        _status.mark_resolved(host, ips, cdn=cdn_label or _ANTHROPIC_WIDEN_LABEL)

    def on_fail(host: str) -> None:
        _status.mark_failed(host, _FAILED_RESOLVE_REASON)
        critical_failed.append(host)

    _cascade({e.host for e in critical_hostnames}, on_ok, on_fail)

    if critical_failed:
        raise RuntimeError(
            f"Critical hosts of {active_harness().name} failed to resolve: {critical_failed}. "
            f"{active_harness().name} cannot operate without them; aborting launch."   # the hosts and the words come from the same adapter record
        )

    global _phase2_thread
    _phase2_thread = threading.Thread(
        target=_phase2_worker, args=(rest_hostnames,),
        daemon=True, name="phase2-cascade",
    )
    _phase2_thread.start()

    return critical_addresses


def _phase2_worker(rest_hostnames: list[HostnameEntry]) -> None:
    """Phase 2 body: cascade through non-critical hosts. For each successful
    resolution, push the ready-to-open `addr[:port]` tokens (CDN-widened
    where applicable — see _tokens_for) onto `_phase2_queue` for the updater
    to batch into the container; for each terminal failure, just log via
    status file (no iptables work to do). Pushes `_phase2_done` sentinel
    last, flips the status file to 'complete', and rewrites the cross-launch
    resolution cache with this launch's fresh DNS answers so the next
    launch's is_file_recent check sees a freshened mtime."""
    # Spawned only from _phase1_worker (which itself runs inside
    # start_whitelist_resolution's executor — created after _phase2_queue
    # was initialized). The assertion narrows the Optional for the type
    # checker AND would surface a programming error if any future caller
    # ever bypasses start_whitelist_resolution.
    assert _phase2_queue is not None
    q = _phase2_queue

    def on_ok(host: str, ips: list[str]) -> None:
        tokens, cdn_label = _emit_tokens_for_host(host, ips)
        for token in tokens:
            q.put(token)
        _status.mark_resolved(host, ips, cdn=cdn_label)

    def on_fail(host: str) -> None:
        _status.mark_failed(host, _FAILED_RESOLVE_REASON)

    _cascade({e.host for e in rest_hostnames}, on_ok, on_fail)

    _status.complete()
    _save_resolution_cache(dict(_fresh_resolutions))
    q.put(_phase2_done)


def _remove_legacy_cache_locations() -> None:
    """One-time tidy-up of the pre-firewall_cache/ layout: the loose
    resolved_domains.txt at the AGENTS_STATE root and the old cdn_ranges/ dir.
    Both now live under firewall_cache/; these are stale caches, rebuilt at the
    new paths this run. force_remove is a silent no-op once they're gone, so
    after the first post-upgrade launch this costs only two stats."""
    force_remove(AGENTS_STATE / "resolved_domains.txt", name="pre-firewall_cache DNS cache")
    force_remove(AGENTS_STATE / "cdn_ranges", name="pre-firewall_cache CDN-range cache")


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
    global _phase1_executor, _phase1_future, _phase2_queue, _selftest_addr
    if _phase1_future is not None:
        return   # idempotent

    _remove_legacy_cache_locations()   # one-time tidy of the pre-firewall_cache/ layout
    _selftest_addr = None
    _status.init(state_dir)
    _load_resolution_cache()
    _seen_cdn_ranges.clear()
    _emitted_tokens.clear()
    _fresh_resolutions.clear()

    # The active harness's critical hosts lead the union — they are the ones
    # phase 1 resolves first and aborts on — then the launcher's own list, then
    # the user's file.
    literals, hostnames, skipped = _expand_whitelist([*_critical_hosts(), *BUILTIN_FIREWALL_DOMAINS, *user_firewall_whitelist_lines()])
    _all_entries_by_host.clear()
    _all_entries_by_host.update(_index_by_host(hostnames))
    critical_hosts = _critical_hosts()
    critical = [t for t in hostnames if t.host in critical_hosts]
    rest = [t for t in hostnames if t.host not in critical_hosts]

    # Populate the pending list + skip reasons now that we've assembled them
    # (clearing already zeroed everything else in _status).
    _status.set_pending({t.host for t in hostnames})
    _status.mark_skipped(skipped)

    _phase2_queue = queue.Queue()

    # Phase 1 runs in a single-worker executor so the caller can wait_for_critical_addresses()
    # later. Phase 1 internally kicks off Phase 2 once it finishes.
    _phase1_executor = ThreadPoolExecutor(max_workers=1)
    _phase1_future = _phase1_executor.submit(_phase1_worker, critical, literals, rest)


def is_critical_pending() -> bool:
    """True iff Phase 1 (critical Anthropic resolve) is in flight but not yet
    finished. docker_config.run_container uses this to print a 'still resolving'
    indicator before the blocking await — so the user doesn't see a silent
    stall and think the launcher hung."""
    return _phase1_future is not None and not _phase1_future.done()


def wait_for_critical_addresses() -> list[str] | None:
    """Block until Phase 1 (critical Anthropic) completes and return the
    list of address strings to stage as WHITELIST_ADDRESSES for the initial
    in-container firewall. Returns None if start_whitelist_resolution() was
    never called (e.g. non-{firewall} launches need no rules). If a
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
    rule. At ~50-150ms per docker exec, per-rule pacing would land the tail
    of a ~135-domain whitelist minutes after launch; batching applies
    each resolution burst in a handful of execs (see
    benchmark/bench_firewall_updater.py for measured pacing).

    Best-effort: a failed batch (container exited mid-resolve, race on
    teardown, etc.) retries once, then logs a stderr warning and moves on —
    by that point Claude Code already has critical Anthropic access via the
    initial ruleset, and a missing docs domain isn't worth aborting.

    No-op when Phase 2 isn't active (non-{firewall} launches)."""
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
    resolution burst (~cascade pass), not one per rule. When the end-of-
    stream sentinel arrives, launch-time work is done — hand off to the
    refresher daemon (_start_refresher), which owns drift healing for the
    rest of the session."""
    if not wait_for_container_running(container_name):
        # Container never came up; nothing to update. Caller's docker compose
        # run will already have surfaced the underlying error.
        return

    # "Running" only means the entrypoint started — init-firewall.sh is still
    # writing the base ruleset. Inserting now would race it: rules landing
    # before its flush get wiped, rules landing mid-self-test can open
    # provider blocks that break the enforcement probe. Wait for its
    # completion marker; if the container died instead (self-test failure,
    # already loud on the user's terminal), there's nothing to update and
    # exec-ing at the corpse would only add "container is not running" noise.
    if not wait_for_firewall_applied(container_name):
        return

    # Updater is spawned only from start_firewall_updater, which guards on
    # `_phase2_queue is None` — so by this point the queue is initialized.
    assert _phase2_queue is not None
    q = _phase2_queue
    while True:
        first = q.get()
        if first is _phase2_done:
            _start_refresher(container_name)
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
        iptables.flush(container_name, batch)
        if finished:
            _start_refresher(container_name)
            return


# Drift-heal cadence: how often the refresher re-resolves the whole hostname
# list once launch-time resolution is done. Five minutes bounds how long a
# VPN-exit swap or CDN steering change can strand a pinned host mid-session;
# the DNS load (~one query per whitelisted host per cycle) is negligible.
_REFRESH_INTERVAL_SECONDS = 5 * 60

# Per-host getent timeout inside a refresh pass. No cascade here — a host
# that misses one cycle (contention drop, resolver hiccup) is simply picked
# up by the next, so a single generous stage is enough.
_REFRESH_RESOLVE_TIMEOUT = 5


def _start_refresher(container_name: str) -> None:
    """Spawn the drift-heal daemon (idempotent). Called by the updater when
    Phase 2's stream ends — never earlier, so the refresher's re-resolutions
    can't interleave with the launch cascade (the shared _seen_cdn_ranges /
    _emitted_tokens sets rely on that strict hand-off)."""
    global _refresher_thread
    if _refresher_thread is not None:
        return
    _refresher_thread = threading.Thread(
        target=_refresher_worker, args=(container_name,),
        daemon=True, name="firewall-refresher",
    )
    _refresher_thread.start()


def _refresher_worker(container_name: str) -> None:
    """Daemon body: sleep, re-resolve, flush what changed, repeat until the
    launcher process exits (it blocks inside `docker compose run` for the
    container's lifetime, so this dies exactly when the firewall does). A
    failed pass warns and waits for the next cycle — one bad resolver moment
    must not kill drift healing for the rest of the session."""
    while True:
        time.sleep(_REFRESH_INTERVAL_SECONDS)
        try:
            _refresh_pass(container_name)
        except Exception as exc:   # noqa: BLE001 — daemon must survive anything
            print(f"  warning: firewall refresh pass failed ({exc}); retrying next cycle", file=sys.stderr)


def _refresh_pass(container_name: str) -> None:
    """One drift-heal cycle: fresh-resolve every whitelisted hostname and
    batch-insert rules for addresses not already open. Purely additive — a
    host that fails to resolve this cycle keeps its existing rules and is
    retried next cycle (never demoted), so transient DNS trouble can't cut
    off a working connection. The status file and the cross-launch cache are
    only rewritten when something actually changed, keeping the steady-state
    cycle write-free."""
    hosts = list(_all_entries_by_host)
    if not hosts:
        return
    with ThreadPoolExecutor(max_workers=min(_RESOLVE_PARALLELISM, len(hosts))) as pool:
        results = list(pool.map(lambda h: (h, _resolve_a_records(h, _REFRESH_RESOLVE_TIMEOUT)), hosts))
    new_tokens: list[str] = []
    for host, ips in results:
        if not ips:
            continue
        tokens, cdn_label = _emit_tokens_for_host(host, ips)
        if tokens:
            new_tokens.extend(tokens)
            _status.mark_resolved(host, ips, cdn=cdn_label)
    if new_tokens:
        iptables.flush(container_name, new_tokens)
        _save_resolution_cache(dict(_fresh_resolutions))
