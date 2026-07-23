"""The agent-visible `{firewall}` resolution status file
(`domains_pending_resolve.yml`, bind-mounted into the container at
`~/.claude/`) — a single lock-guarded tracker that both records DNS
resolution progress in memory and mirrors it to disk on every change, so the
agent can classify a `ConnectionRefused` as pending / failed / skipped /
neither. `resolver.py` owns the one process-wide `_status` singleton; every
phase writes through it.

Phase 1, Phase 2, and the docker-exec updater all mutate the same in-memory
state and rewrite the file from it — the class bundles the lock, the dicts,
and the file path so those invariants stay co-located (the lock guards both
the dict mutation AND the file write; a missing path is a no-op write). All
public methods take the lock themselves; callers don't."""

import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from ..file_access import write_text
from ..paths import state_domain_resolve_status_path


class _WhitelistResolutionStatus:
    """Tracks DNS resolution progress for the {firewall} whitelist
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
        self.skipped: dict[str, str] = {}            # entry → why it was never attempted (e.g. IPv6)
        self.wildcard_gaps: list[str] = []           # `*.` hosts whose subdomains could NOT be covered

    def init(self, state_dir: Path) -> None:
        """Reset to a clean 'resolving' state and record where to write — wipes
        any leftover from a previous run on this instance so the agent never
        observes stale content. Called once at the start of every {firewall} launch
        from start_whitelist_resolution, gated by that function's idempotency
        check."""
        with self._lock:
            self._path = state_domain_resolve_status_path(state_dir)
            self.status = "resolving"
            self.resolved = {}
            self.pending = []
            self.failed = {}
            self.cdn = {}
            self.skipped = {}
            self.wildcard_gaps = []
            self._write()

    def set_pending(self, hosts: Iterable[str]) -> None:
        """Replace the pending-host list (called once at start_whitelist_resolution
        after the full whitelist is assembled)."""
        with self._lock:
            self.pending = sorted(hosts)
            self._write()

    def mark_resolved(self, host: str, ips: list[str], cdn: str | None = None) -> None:
        """Move `host` from pending (or failed — a refresher pass can heal a
        host that was dead at launch) → resolved; file the IPs. `cdn` names
        the provider whose published block was widened for this host (None
        when the IPs were pinned as-is) — surfaced in the status file so a
        human or agent can see which hosts are rotation-proof."""
        with self._lock:
            self.resolved[host] = list(ips)
            if cdn:
                self.cdn[host] = cdn
            if host in self.pending:
                self.pending.remove(host)
            self.failed.pop(host, None)
            self._write()

    def mark_failed(self, host: str, reason: str) -> None:
        """Move `host` from pending → failed with `reason`."""
        with self._lock:
            self.failed[host] = reason
            if host in self.pending:
                self.pending.remove(host)
            self._write()

    def mark_skipped(self, entries: Iterable[tuple[str, str]]) -> None:
        """Record entries that were never attempted, with their reasons —
        currently IPv6 literals. Called once, right after expansion."""
        with self._lock:
            self.skipped.update(entries)
            self._write()

    def mark_wildcard_gap(self, host: str) -> None:
        """Record that `host` came from a `*.` entry whose base doesn't sit on
        any known CDN provider — only the base host's own IPs are open, so
        subdomains on other addresses will still be refused. Surfaced so the
        user learns the wildcard is only half-honored BEFORE chasing ghosts."""
        with self._lock:
            if host not in self.wildcard_gaps:
                self.wildcard_gaps.append(host)
                self._write()

    def complete(self) -> None:
        """Flip top-level status to 'complete' — every entry has been
        resolved or terminally failed; no more updates coming."""
        with self._lock:
            self.status = "complete"
            self._write()

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
            "# {firewall} whitelist — pending/failed view",
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
        lines.append("# Skipped — entries the resolver never attempted, with the reason. Fix the")
        lines.append("# entry in the user whitelist to cover what it was aiming at.")
        lines.append("skipped:")
        for entry in sorted(self.skipped):
            lines.append(f"  {entry}: {self.skipped[entry]}")
        lines.append("")
        lines.append("# Wildcard gaps — `*.` entries whose base host is NOT on a known CDN")
        lines.append("# provider, so only the base host's own IPs are open; subdomains served")
        lines.append("# from other addresses will still be refused. Wildcards are only fully")
        lines.append("# honorable when the provider's published ranges are known.")
        lines.append("wildcard_gaps:")
        for host in sorted(self.wildcard_gaps):
            lines.append(f"  - {host}")
        lines.append("")
        lines.append("# CDN-widened — these hosts resolved into a known CDN provider's published")
        lines.append("# range, so the whole containing block was whitelisted (rotation-proof)")
        lines.append("# rather than pinning the momentary IPs (wildcard entries widen to ALL the")
        lines.append("# provider's blocks). Note: a provider block is shared by every customer")
        lines.append("# of that CDN.")
        lines.append("cdn:")
        for host in sorted(self.cdn):
            lines.append(f"  {host}: {self.cdn[host]}")
        return "\n".join(lines) + "\n"


# Process-wide singleton — one launcher = one resolution = one tracker.
_status = _WhitelistResolutionStatus()
