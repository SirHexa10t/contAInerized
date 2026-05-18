#!/usr/bin/env python3
"""Benchmark: file reads (OS-page-cache-hot) vs in-process dict lookups for
the project's agent .md files.

Loads every entry of `AGENT_MD_FILES` into a dict (one disk read per file),
then runs N iterations of:
  (a) re-reading every file from disk via Path.read_text()
  (b) looking up every file's content from the dict

Prints totals + per-operation averages + the ratio. Settles the question of
whether a Python-level read_text cache would meaningfully outperform raw
re-reads at the launcher's scale (~10 small `.md` files).

Run from the project root:
  python3 -m launch.benchmark.bench_md_cache_vs_read
"""

import time

from ..paths import AGENT_MD_FILES


N = 10_000   # outer iterations; inner is len(AGENT_MD_FILES). Total ops ≈ 100k for a typical agent count.


def main() -> None:
    if not AGENT_MD_FILES:
        print("No .md files in agents/ — nothing to benchmark.")
        return

    # Stage 1 — populate the dict (also warms the OS page cache for stage 2).
    cache = {p: p.read_text() for p in AGENT_MD_FILES}
    total_bytes = sum(len(v) for v in cache.values())
    print(f"Loaded {len(cache)} .md files into dict ({total_bytes:,} chars total).")
    print(f"Running {N:,} outer iterations × {len(AGENT_MD_FILES)} files = {N * len(AGENT_MD_FILES):,} ops per condition.")
    print()

    # Stage 2a — file reads (page-cache-hot from stage 1).
    t0 = time.perf_counter()
    for _ in range(N):
        for p in AGENT_MD_FILES:
            _ = p.read_text()
    file_total = time.perf_counter() - t0

    # Stage 2b — dict lookups.
    t0 = time.perf_counter()
    for _ in range(N):
        for p in AGENT_MD_FILES:
            _ = cache[p]
    dict_total = time.perf_counter() - t0

    ops = N * len(AGENT_MD_FILES)
    print(f"File reads (page-cache-hot):  total {file_total*1000:7.2f} ms   per op {file_total*1e6/ops:7.3f} µs")
    print(f"Dict lookups:                 total {dict_total*1000:7.2f} ms   per op {dict_total*1e6/ops:7.3f} µs")
    print()
    print(f"File reads are {file_total/dict_total:.1f}× slower than dict lookups.")


if __name__ == "__main__":
    main()
