"""The `{firewall}` outbound-whitelist subsystem (host side).

  - `resolver` — the coordinator: two-phase DNS cascade, live-fetched CDN-range
    widening, and the streaming iptables updater + drift-heal refresher.
    Stays one module because those pieces share sequenced, in-place-mutated,
    lock-free state.
  - `whitelist` — pure raw-entry → work-item expansion (no DNS/threads/disk).
  - `status` — the lock-guarded `domains_pending_resolve.yml` progress tracker.

This `__init__` is the package's public face: consumers import the five entry
points from `launch.firewall` and never touch the submodules directly. (The
test suite reaches into `launch.firewall.resolver` — that module is the single
namespace every `patch`/attribute reference targets, since the coordinator
reads its collaborators from its own globals.)"""

from .resolver import (
    is_critical_pending,
    selftest_address,
    start_firewall_updater,
    start_whitelist_resolution,
    wait_for_critical_addresses,
)

__all__ = [
    "is_critical_pending",
    "selftest_address",
    "start_firewall_updater",
    "start_whitelist_resolution",
    "wait_for_critical_addresses",
]
