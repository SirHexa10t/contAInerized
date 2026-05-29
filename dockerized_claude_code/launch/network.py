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
    thread that streams (host, ips) onto an internal queue. A daemon updater
    thread (start_firewall_updater, spawned right before `docker compose run`)
    consumes the queue and runs `docker exec --user root <container> iptables
    -I OUTPUT 1 -d <ip> ...` to insert ACCEPT rules into the running container
    — BEFORE the catch-all REJECT — as each domain resolves. The launcher
    proceeds into Claude Code as soon as Phase 1 finishes; Phase 2 + the
    updater run alongside Claude Code's startup, growing the firewall in
    real time.

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
resolution pass, so they line up. CDN-heavy services (AWS / GitHub / Cloud-
Flare-fronted sites) are where this would break if it ever does — flag-as-
suspect if a whitelisted service is dropping requests right after launch.

Imports nothing heavy: file_access for the user's whitelist file + atomic
write helper, paths for the two status-file locations, stdlib for subprocess
+ threading. agent_modifiers_handler._apply_auto is the entry point caller (calls
start_whitelist_resolution during compose_chain); docker_config.run_compose
pairs the await + updater-spawn.

Cycle note: docker_config imports this module (for is_critical_pending /
wait_for_critical_addresses / start_firewall_updater), and the updater code
below needs docker_config's docker-subprocess helpers (wait_for_container_running
+ docker_exec_root_subprocess) to inject iptables rules into the running container. The
two functions that need them (_updater_worker, _insert_iptables_accept) do
lazy `from .docker_config import ...` at call time so import-time evaluation
doesn't hit a half-loaded module."""

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

from .file_access import (
    is_file_recent, parse_lines, user_firewall_whitelist_lines, write_text,
)
from .paths import RESOLVED_DOMAINS_CACHE_FILE, state_domain_resolve_status_path
from .utils import shell_capture, split_host_port


# ============================================================
# Always-allowed domains in {auto} mode
# ============================================================
# Curated Python-side so all domain → IP resolution happens in one place,
# before the container starts. The user's firewall_whitelist.txt is unioned
# in at start_whitelist_resolution() time. Every form you want allowed must
# be listed explicitly (e.g. both `foo.com` and `www.foo.com` if both are
# needed). The one convenience: a `www.X` entry also implicitly allows `X`,
# since the user typing the `www.` form clearly meant the bare apex too.
# Inside the container, init-firewall.sh reads pre-resolved IPs via
# $WHITELIST_ADDRESSES — no DNS dependency in the firewall hot path.

BUILTIN_FIREWALL_DOMAINS = [
    # === Core launcher dependencies ===
    # Anthropic
    "api.anthropic.com",
    "console.anthropic.com",
    "www.claude.ai",
    # GitHub (git, releases, raw, codeload, container registry)
    "www.github.com",
    "api.github.com",
    "ssh.github.com",
    "www.raw.githubusercontent.com",
    "www.objects.githubusercontent.com",
    "codeload.github.com",
    "www.ghcr.io",
    # npm
    "registry.npmjs.org",
    # PyPI
    "www.pypi.org",
    "files.pythonhosted.org",
    # crates.io (Rust)
    "www.crates.io",
    "static.crates.io",
    "index.crates.io",

    # === Developer documentation & references ===
    # Q&A and community
    "www.stackoverflow.com",
    "www.stackexchange.com",     # covers DBA / Security / Code Review etc.; Server Fault and Super User live at their own apexes
    "www.gitlab.com",
    # Atlassian (Jira / Confluence / Bitbucket) marketing + docs; per-tenant subdomains
    # (e.g. <org>.atlassian.net) need their own entry in the user whitelist since
    # CloudFront sharding can put them on a different POP than the apex.
    "www.atlassian.net",
    "www.atlassian.com",
    # Language docs — Python (PyPI registry above)
    "docs.python.org",
    "peps.python.org",
    # Language docs — Rust (crates.io registry above)
    "doc.rust-lang.org",
    "www.rust-lang.org",
    "www.docs.rs",
    # Language docs — Node.js / JavaScript (npm registry above)
    "www.nodejs.org",
    "developer.mozilla.org",  # MDN — also covers HTML / CSS / Web APIs
    "www.npmjs.com",
    "tc39.es",     # ECMAScript spec
    # Language docs — TypeScript
    "www.typescriptlang.org",
    # Language docs — Go
    "go.dev",
    "pkg.go.dev",
    # Language docs — Java
    "docs.oracle.com",
    "openjdk.org",
    "www.mvnrepository.com",
    "search.maven.org",
    # Language docs — C# / .NET (also covers Azure, VS Code, TypeScript, etc.)
    "www.learn.microsoft.com",
    # Language docs — C / C++
    "www.en.cppreference.com",
    "www.isocpp.org",
    # Language docs — Ruby
    "www.ruby-lang.org",
    "www.ruby-doc.org",
    "www.rubygems.org",
    # Language docs — PHP
    "www.php.net",
    "www.packagist.org",
    # Language docs — Swift / Apple
    "www.swift.org",
    "www.developer.apple.com",
    # Language docs — Kotlin
    "www.kotlinlang.org",
    # Language docs — Other
    "www.haskell.org",
    "www.dart.dev",
    "www.elixir-lang.org",
    "www.hexdocs.pm",
    "www.scala-lang.org",
    "www.clojure.org",
    "www.julialang.org",
    "www.ocaml.org",
    "www.erlang.org",
    "www.r-project.org",
    "www.cran.r-project.org",
    "www.perl.org",
    "www.perldoc.perl.org",
    "www.lua.org",
    # Cloud / infra — AWS
    "docs.aws.amazon.com",
    "www.aws.amazon.com",
    "www.repost.aws",            # AWS re:Post Q&A
    # Cloud / infra — GCP
    "www.cloud.google.com",
    "firebase.google.com",
    # Cloud / infra — Azure (learn.microsoft.com above)
    "www.azure.microsoft.com",
    # Cloud / infra — Docker / Kubernetes / Helm
    "docs.docker.com",
    "www.kubernetes.io",
    "www.helm.sh",
    # Cloud / infra — HashiCorp (Terraform, Vault, Consul, Nomad)
    "developer.hashicorp.com",
    # Web standards
    "www.whatwg.org",            # HTML / DOM / Fetch specs
    "www.w3.org",                # W3C specs
    "www.caniuse.com",           # browser compat tables
    "www.web.dev",               # Google web best-practices
    # Frontend frameworks
    "www.react.dev",
    "www.vuejs.org",
    "www.angular.dev",
    "www.svelte.dev",
    "www.nextjs.org",
    "www.nuxt.com",
    "www.remix.run",
    "www.astro.build",
    # Browser automation ({web} mode — browser-binary CDN, bare apex only)
    "cdn.playwright.dev",
    # Backend frameworks — Python
    "docs.djangoproject.com",
    "flask.palletsprojects.com",
    "fastapi.tiangolo.com",
    # Backend frameworks — Node
    "www.expressjs.com",
    "www.nestjs.com",
    # Backend frameworks — Java
    "www.spring.io",
    "docs.spring.io",
    # Backend frameworks — Ruby
    "www.rubyonrails.org",
    "guides.rubyonrails.org",
    # Backend frameworks — PHP
    "www.laravel.com",
    "www.symfony.com",
    # ML / data
    "www.pytorch.org",
    "www.tensorflow.org",
    "www.scikit-learn.org",
    "www.numpy.org",
    "pandas.pydata.org",
    "www.jupyter.org",
    "www.huggingface.co",
    "www.arxiv.org",
    "www.paperswithcode.com",
    # AI / LLM APIs (Anthropic API endpoints above)
    "docs.anthropic.com",
    "platform.openai.com",
    # Databases
    "www.postgresql.org",
    "dev.mysql.com",
    "www.mariadb.com",
    "www.sqlite.org",
    "www.redis.io",
    "www.mongodb.com",
    "www.elastic.co",
    # Linux / systems
    "www.man7.org",              # Linux man pages
    "www.kernel.org",
    "wiki.archlinux.org",    # general Linux setup info, even off-Arch
    "access.redhat.com",
    "www.lwn.net",               # kernel and systems-internals reporting
    # Standards / RFCs
    "datatracker.ietf.org",
    "www.rfc-editor.org",
    "www.semver.org",
    "www.json.org",
    # Build & tooling
    "www.webpack.js.org",
    "www.vite.dev",
    "www.rollupjs.org",
    "www.esbuild.github.io",
    "www.cmake.org",
    "www.ninja-build.org",
    "www.git-scm.com",
    # Reliable tutorial / reference sites
    "www.realpython.com",        # Python
    "www.baeldung.com",          # Java / Spring
    "www.digitalocean.com",      # community tutorials
    "www.css-tricks.com",        # web / CSS
    "www.smashingmagazine.com",  # web / CSS
    "www.learnxinyminutes.com",  # quick-reference cheat sheets per language
    "cheatsheetseries.owasp.org",  # web / app security cheat sheets
    "www.martinfowler.com",      # architecture and refactoring
    "www.fly.io",                # systems / networking writing on fly.io/blog
]


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

# A whitelist entry's parsed shape — (raw entry string, hostname, port). Carried
# through both phases of the resolution cascade so a resolved host can be matched
# back to every (entry, port) pair that shares its hostname. `port` is "" when
# the entry didn't specify one (and `_DEFAULT_OPEN_PORTS` is opened instead).
HostnameEntry = tuple[str, str, str]

# Reason string written to the status file's `failed:` section when a host
# exhausts every cascade stage. One constant so phase 1 + phase 2 emit
# identical text — the agent / user can grep for it.
_FAILED_RESOLVE_REASON = "DNS resolution failed after all cascade stages"

# Cascade timeouts: a host that fails resolution at pass N is retried at pass
# N+1 with the next (larger) per-host timeout. The cascade exists to recover
# from contention-induced false negatives — see _resolve_with_cascade below
# for the full rationale. Worst-case budget per host is the sum (29s); typical
# wall time for the full whitelist is well under 10s.
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
    supplied by _resolve_with_cascade per cascade stage."""
    if host in _resolution_cache:
        return list(_resolution_cache[host])
    try:
        r = shell_capture("getent", "ahostsv4", host, timeout=timeout)
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0:
        return []
    return sorted({line.split()[0] for line in r.stdout.splitlines() if line.strip()})


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
            self._write()

    def set_pending(self, hosts) -> None:
        """Replace the pending-host list (called once at start_whitelist_resolution
        after the full whitelist is assembled)."""
        with self._lock:
            self.pending = sorted(hosts)
            self._write()

    def mark_resolved(self, host: str, ips: list[str]) -> None:
        """Move `host` from pending → resolved; file the IPs."""
        with self._lock:
            self.resolved[host] = list(ips)
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

# Phase 2 (rest) — producer thread writes (host, ips) tuples to this queue,
# updater thread consumes them.
_phase2_queue: queue.Queue | None = None
_phase2_done = object()    # sentinel: end-of-stream
_phase2_thread: threading.Thread | None = None

# Firewall updater — daemon thread; lifetime bounded by the launcher process,
# which itself blocks inside `docker compose run` for the container's lifetime.
_updater_thread: threading.Thread | None = None


def _index_by_host(entries: list[HostnameEntry]) -> dict[str, list[tuple[str, str]]]:
    """Build `{host: [(entry, port), ...]}` from a list of (entry, host, port)
    triples — multiple entries can share a host (the user wrote both
    `api.anthropic.com:443` and the bare apex, or apex+www stripping produced
    duplicates). The dict turns the per-resolve scan in each `on_ok` from O(N)
    into O(1)."""
    out: dict[str, list[tuple[str, str]]] = {}
    for entry, host, port in entries:
        out.setdefault(host, []).append((entry, port))
    return out


def _phase1_worker(critical_hostnames: list[HostnameEntry], literal_entries: list[str], rest_hostnames: list[HostnameEntry]) -> list[str]:
    """Phase 1 body: cascade through critical hosts (Anthropic), then kick off
    Phase 2 in its own thread before returning. Result is the list of address
    strings to stage as WHITELIST_ADDRESSES for the initial firewall — that's
    critical IPs plus literal IP/CIDR entries (which need no resolution).
    Raises if any critical host fails terminally — those are non-optional and
    the launcher should abort loudly rather than start a half-broken agent."""
    critical_addresses: list[str] = []
    critical_failed: list[str] = []
    by_host = _index_by_host(critical_hostnames)

    def on_ok(host: str, ips: list[str]) -> None:
        _status.mark_resolved(host, ips)
        for _entry, port in by_host.get(host, []):
            for ip in ips:
                critical_addresses.append(f"{ip}:{port}" if port else ip)

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
    resolution, push (entry, host, port, ips) onto `_phase2_queue` for the
    updater to docker-exec into the container; for each terminal failure, just
    log via status file (no iptables work to do). Pushes `_phase2_done`
    sentinel last, flips the status file to 'complete', and rewrites the
    cross-launch resolution cache with everything resolved this launch (cache
    hits + fresh DNS) so the next launch's is_file_recent check sees a
    freshened mtime."""
    # Spawned only from _phase1_worker (which itself runs inside
    # start_whitelist_resolution's executor — created after _phase2_queue
    # was initialized). The assertion narrows the Optional for the type
    # checker AND would surface a programming error if any future caller
    # ever bypasses start_whitelist_resolution.
    assert _phase2_queue is not None
    q = _phase2_queue
    by_host = _index_by_host(rest_hostnames)

    def on_ok(host: str, ips: list[str]) -> None:
        _status.mark_resolved(host, ips)
        for entry, port in by_host.get(host, []):
            q.put((entry, host, port, ips))

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

    # Expand the whitelist (BUILTIN + user file, *.X stripping, apex-from-www).
    deduped = {d.removeprefix("*.") for d in set(BUILTIN_FIREWALL_DOMAINS) | set(user_firewall_whitelist_lines())}
    deduped |= {d.removeprefix("www.") for d in deduped if d.startswith("www.")}

    literals: list[str] = []
    hostnames: list[HostnameEntry] = []
    for entry in sorted(deduped):
        host, port = split_host_port(entry)
        if _IP_OR_CIDR_RE.match(host):
            literals.append(entry)
        else:
            hostnames.append((entry, host, port))

    critical = [t for t in hostnames if t[1] in _CRITICAL_HOSTS]
    rest = [t for t in hostnames if t[1] not in _CRITICAL_HOSTS]

    # Populate the pending list now that we've assembled it (clearing already
    # zeroed everything else in _status).
    _status.set_pending({host for _, host, _ in hostnames})

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
    global _phase1_executor, _phase1_future
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
    """Spawn a daemon thread that consumes Phase 2's `_phase2_queue` and runs
    `docker exec --user root <container_name> iptables -I OUTPUT 1 -d <ip>
    -p tcp --dport <port> -j ACCEPT` for each newly-resolved address — so
    the container's iptables ruleset grows incrementally as the host finishes
    resolving the non-critical whitelist.

    Best-effort: a failed `docker exec` (container exited mid-resolve, race
    on teardown, etc.) logs a stderr warning but doesn't abort — by that
    point Claude Code already has critical Anthropic access via the initial
    ruleset, and a missing docs domain isn't worth aborting.

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
    `_phase2_queue` and exec iptables rules into the running container.
    Lazy import of wait_for_container_running breaks the docker_config↔network
    import cycle (see module-top docstring)."""
    from .docker_config import wait_for_container_running

    if not wait_for_container_running(container_name):
        # Container never came up; nothing to update. Caller's docker compose
        # run will already have surfaced the underlying error.
        return

    # Updater is spawned only from start_firewall_updater, which guards on
    # `_phase2_queue is None` — so by this point the queue is initialized.
    assert _phase2_queue is not None
    for item in iter(_phase2_queue.get, _phase2_done):
        _entry, _host, port, ips = item
        for ip in ips:
            _insert_iptables_accept(container_name, ip, port)


def _insert_iptables_accept(container_name: str, ip: str, port: str) -> None:
    """Insert an iptables ACCEPT rule at position 1 of the OUTPUT chain (i.e.
    BEFORE the catch-all REJECT) for `ip`. If `port` is empty, opens the
    default HTTPS+HTTP pair; otherwise just that one port. Best-effort —
    warns on failure but doesn't raise. Lazy import of docker_exec_root_subprocess
    breaks the docker_config↔network import cycle (see module-top docstring)."""
    from .docker_config import docker_exec_root_subprocess

    targets = [port] if port else _DEFAULT_OPEN_PORTS
    for p in targets:
        r = docker_exec_root_subprocess(
            container_name,
            "iptables", "-I", "OUTPUT", "1",
            "-d", ip, "-p", "tcp", "--dport", str(p), "-j", "ACCEPT",
        )
        if r.returncode != 0:
            print(
                f"  warning: docker exec iptables -I (ip={ip} port={p}) failed: "
                f"{r.stderr.strip() or r.stdout.strip()}",
                file=sys.stderr,
            )
