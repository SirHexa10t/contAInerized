#!/usr/bin/env python3
"""Benchmark: firewall-updater pacing — per-rule docker execs (the old
scheme) vs the shipped burst-batched updater (`network._updater_worker` +
`_flush_rules`).

No docker required: `docker_exec_root_subprocess` and
`wait_for_container_running` are stubbed with a fixed-latency sleep standing
in for a real `docker exec` round-trip (~50-150ms in practice; EXEC_LATENCY_S
below is deliberately at the low end so the old scheme's simulation finishes
quickly — real-world gaps are proportionally larger).

Two conditions over the same token stream (the builtin whitelist size, all
default-port → 2 rules per token):
  (a) old per-rule scheme — one exec per (address, port) pair, emulated
      inline the way `_insert_iptables_accept` used to run;
  (b) shipped batched updater — tokens arrive in BURST_COUNT bursts (a
      realistic cascade shape: most hosts resolve in pass 1, stragglers
      trickle in), each burst drained into `_flush_rules`'s chunked
      `sh -c` execs by the real `_updater_worker` code.

Prints per-condition exec counts, wall time, and rules/sec.

Run from the project root:
  python3 -m launch.benchmark.bench_firewall_updater
"""

import queue
import time
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import patch

from .. import network
from ..template_code.firewall_domains import BUILTIN_FIREWALL_DOMAINS

EXEC_LATENCY_S = 0.05   # simulated docker-exec round-trip (real: ~0.05-0.15s)
BURST_COUNT = 4         # resolution bursts feeding the updater (≈ cascade passes that produced results)


def _fake_exec_factory(counter: list[int]) -> Callable[..., SimpleNamespace]:
    def fake_exec(container: str, *cmd: str) -> SimpleNamespace:
        counter[0] += 1
        time.sleep(EXEC_LATENCY_S)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return fake_exec


def _bench_per_rule(tokens: list[str]) -> tuple[int, float]:
    """The old scheme: one exec per (address, port) rule."""
    counter = [0]
    fake_exec = _fake_exec_factory(counter)
    t0 = time.perf_counter()
    for token in tokens:
        for rule in network._iptables_rules_for(token):
            fake_exec("container", "sh", "-c", rule)
    return counter[0], time.perf_counter() - t0


def _bench_batched(tokens: list[str]) -> tuple[int, float]:
    """The shipped scheme: the real _updater_worker draining a queue that
    receives `tokens` in BURST_COUNT bursts (each burst fully queued before
    the worker sees it — matching how a cascade pass lands many results at
    once while the previous batch's exec is in flight)."""
    counter = [0]
    q: queue.Queue = queue.Queue()
    per_burst = max(1, len(tokens) // BURST_COUNT)
    for i in range(0, len(tokens), per_burst):
        for t in tokens[i:i + per_burst]:
            q.put(t)
    q.put(network._phase2_done)

    t0 = time.perf_counter()
    with patch.object(network, "_phase2_queue", q), \
         patch("launch.docker_config.wait_for_container_running", return_value=True), \
         patch("launch.docker_config.docker_exec_root_subprocess", side_effect=_fake_exec_factory(counter)):
        network._updater_worker("bench-container")
    return counter[0], time.perf_counter() - t0


def main() -> None:
    # Realistic shape: every builtin domain resolved to one default-port
    # address token (real launches add user entries + apex duplicates).
    tokens = [f"192.0.2.{i % 250}" for i in range(len(BUILTIN_FIREWALL_DOMAINS))]
    n_rules = sum(len(network._iptables_rules_for(t)) for t in tokens)
    print(f"Simulating {len(tokens)} resolved tokens → {n_rules} iptables rules "
          f"(exec latency {EXEC_LATENCY_S * 1000:.0f} ms, {BURST_COUNT} resolution bursts).")
    print()

    old_execs, old_wall = _bench_per_rule(tokens)
    new_execs, new_wall = _bench_batched(tokens)

    print(f"{'scheme':<22} {'docker execs':>12} {'wall':>9} {'rules/sec':>10}")
    print(f"{'per-rule (old)':<22} {old_execs:>12} {old_wall:>8.2f}s {n_rules / old_wall:>10.0f}")
    print(f"{'burst-batched (new)':<22} {new_execs:>12} {new_wall:>8.2f}s {n_rules / new_wall:>10.0f}")
    print()
    print(f"Batched updater applies the full whitelist in {new_execs} execs instead of "
          f"{old_execs} — {old_wall / new_wall:.0f}× faster at this latency; the gap grows "
          f"linearly with real docker-exec latency.")


if __name__ == "__main__":
    main()
