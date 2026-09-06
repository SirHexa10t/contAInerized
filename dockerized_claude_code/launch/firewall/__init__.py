"""The `{firewall}` outbound-whitelist subsystem (host side).

  - `resolver` — the coordinator: two-phase DNS cascade, the widening policy,
    and the streaming iptables updater + drift-heal refresher. Stays one
    module because those pieces share sequenced, in-place-mutated, lock-free
    state.
  - `whitelist` — pure raw-entry → work-item expansion (no DNS/threads/disk).
  - `cdn_ranges` — the fetched-and-cached table of provider IPv4 blocks the
    resolver widens against. Split out of `resolver` 2026-09-03: its only
    couplings to the coordinator are `load()` once per launch and
    `provider_ranges(ips)` per resolution, and it knows nothing of DNS,
    phases or iptables.
  - `iptables` — what an opened address MEANS in iptables (`rules_for`) and
    how a batch of them reaches a live container (`flush`). Also split out
    2026-09-03: no coupling to the phase machinery, and it is where the
    token-validation security boundary sits, which is worth reading alone.
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
