#!/usr/bin/env python3
"""Benchmark: what a background transcript read does to the UI thread —
worker THREAD versus worker PROCESS.

bench_preview_segments established that the `Last prompt` read dominates a
Cont preview's cost (99.9% on a 155 MB state dir). Moving it to a thread is
not enough: the parse is CPU-bound (one `.splitlines()` over the whole file,
then a `json.loads` per line), and a CPU-bound thread convoys the GIL — the
render loop's ticks arrive late, which the user feels as the picker freezing
even though the read is "in the background". This benchmark measures exactly
that: a fake UI thread ticks every 5 ms and records how late each tick fires,
while the read runs (a) on a thread, (b) in a spawned child process — the
mechanism `menu_picker._read_last_prompt` uses.

Run from the project root (a file path argument adds a real state dir):
  python3 -m launch.benchmark.bench_preview_gil [state_dir ...]
"""

import multiprocessing
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from ..file_access import last_prompt_in_state
from .bench_preview_segments import _synthetic_state_dir

TICK_SECONDS = 0.005      # one simulated render tick — prompt_toolkit-ish cadence
SYNTHETIC_MB = (8, 64)


def main() -> None:
    subjects: list[tuple[str, Path]] = [
        (f"synthetic {mb:>3} MB", _synthetic_state_dir(mb)) for mb in SYNTHETIC_MB
    ]
    subjects += [(f"REAL {Path(arg).name[:24]}", Path(arg)) for arg in sys.argv[1:]]

    pool = ProcessPoolExecutor(max_workers=1,
                               mp_context=multiprocessing.get_context("spawn"))
    t0 = time.perf_counter()
    pool.submit(int, 1).result()
    print(f"child warmup: {time.perf_counter() - t0:.2f}s (paid once, on the worker)\n")
    header = (f"{'state dir':<22} {'backend':<9} {'tick p50':>9} {'tick p95':>9} "
              f"{'tick max':>9}   verdict")
    print(header)
    print("-" * len(header))
    for name, state_dir in subjects:
        last_prompt_in_state(state_dir)                    # warm the page cache
        _report(name, "thread", lambda: last_prompt_in_state(state_dir))
        _report(name, "process",
                lambda: pool.submit(last_prompt_in_state, state_dir).result())
    pool.shutdown(wait=False, cancel_futures=True)


def _report(name: str, backend: str, read: Callable[[], object]) -> None:
    late = sorted(_tick_lateness_during(read))
    p50, p95, top = late[len(late) // 2], late[int(len(late) * 0.95)], late[-1]
    verdict = "smooth" if top < 4 * TICK_SECONDS else "STALLS THE UI"
    print(f"{name:<22} {backend:<9} {p50 * 1000:>7.1f}ms {p95 * 1000:>7.1f}ms "
          f"{top * 1000:>7.1f}ms   {verdict}")


def _tick_lateness_during(read: Callable[[], object]) -> list[float]:
    """How late each 5 ms UI tick fires while `read` runs on a worker thread.
    (The process condition still WAITS on a thread — `Future.result()` — which
    is precisely how the picker's loader consumes `_read_last_prompt`.)"""
    lateness: list[float] = []
    done = threading.Event()

    def run_and_signal() -> None:
        read()
        done.set()

    worker = threading.Thread(target=run_and_signal)
    worker.start()
    while not done.is_set():
        t0 = time.perf_counter()
        time.sleep(TICK_SECONDS)
        lateness.append(time.perf_counter() - t0 - TICK_SECONDS)
    worker.join()
    return lateness


if __name__ == "__main__":
    main()
