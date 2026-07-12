#!/usr/bin/env python3
"""Benchmark: file reads (OS-page-cache-hot) vs in-process dict lookups for
the project's agent .md files.

Loads every entry of `agent_md_index().values()` into a dict (one disk read
per file), then runs N iterations of:
  (a) re-reading every file from disk via Path.read_text()
  (b) looking up every file's content from the dict

Prints totals + per-operation averages + the ratio. Settles the question of
whether a Python-level read_text cache would meaningfully outperform raw
re-reads at the launcher's scale (~10 small `.md` files).

Run from the project root:
  python3 -m launch.benchmark.bench_md_cache_vs_read
"""

import time

from ..file_access import agent_md_index


N = 10_000   # outer iterations; inner is the agent count. Total ops ≈ 100k for a typical agent count.


def main() -> None:
    if not agent_md_index():
        print("No .md files in agents/ — nothing to benchmark.")
        return
    paths = tuple(agent_md_index().values())

    # Stage 1 — populate the dict (also warms the OS page cache for stage 2).
    cache = {p: p.read_text() for p in paths}
    total_bytes = sum(len(v) for v in cache.values())
    print(f"Loaded {len(cache)} .md files into dict ({total_bytes:,} chars total).")
    print(f"Running {N:,} outer iterations × {len(paths)} files = {N * len(paths):,} ops per condition.")
    print()

    # Stage 2a — file reads (page-cache-hot from stage 1).
    t0 = time.perf_counter()
    for _ in range(N):
        for p in paths:
            _ = p.read_text()
    file_total = time.perf_counter() - t0

    # Stage 2b — dict lookups.
    t0 = time.perf_counter()
    for _ in range(N):
        for p in paths:
            _ = cache[p]
    dict_total = time.perf_counter() - t0

    ops = N * len(paths)
    print(f"File reads (page-cache-hot):  total {file_total*1000:7.2f} ms   per op {file_total*1e6/ops:7.3f} µs")
    print(f"Dict lookups:                 total {dict_total*1000:7.2f} ms   per op {dict_total*1e6/ops:7.3f} µs")
    print()
    print(f"File reads are {file_total/dict_total:.1f}× slower than dict lookups.")


if __name__ == "__main__":
    main()
