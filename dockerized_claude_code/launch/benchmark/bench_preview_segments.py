#!/usr/bin/env python3
"""Benchmark: where a Cont-row preview's build time actually goes.

The preview has three segments, composed by `ContEntry.preview`:
  metadata     — the YAML block rendered through rich markdown (_render_md)
  last prompt  — the newest human prompt, read from EVERY session JSONL in the
                 state dir (_last_prompt_display → last_prompt_in_state)
  tags         — the expanded tag list rendered through rich Text (_tags_preview)

The question this settles: is the transcript read the dominant cost, and by how
much? If it is, the picker can render the two cheap segments synchronously with
a `[loading…]` stand-in for the prompt, instead of hiding the whole pane behind
a placeholder while the read runs.

Measures each segment per-entry over synthetic state dirs of increasing
transcript size, plus any real state dirs passed as arguments (or discovered
under `instances_dir()` when none are). Reads run OS-page-cache-HOT (one warmup
pass first): a real first highlight can be colder, which UNDERSTATES the
last-prompt share — the conclusion only strengthens.

Run from the project root:
  python3 -m launch.benchmark.bench_preview_segments [state_dir ...]
"""

import dataclasses
import json
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from ..agents_crud import list_all_instances
from ..gui.menu_picker import ContEntry, _last_prompt_display, _render_md, _tags_preview
from ..paths import AGENTS_DIR, instance_state_dir_path
from ..tags import AgentBuild, Instance, resolve_build, scan_all

REPS = 3                                  # per segment; the mean is reported
SYNTHETIC_MB = (0, 1, 8, 64)              # transcript sizes to fabricate
FILLER = ("The retry loop in retry.py needs exponential backoff, a five-attempt "
          "cap, and a log line per failure naming the attempt number. ")


def main() -> None:
    registry = scan_all(AGENTS_DIR)
    build = AgentBuild(engine=None, professions=("code",),
                       specialties=("auto", "cowork", "manager"),
                       policies=("no-sudo",))
    identity = Instance(agent="bench", md_path=Path("/dev/null"), session="bench",
                        workspace="/tmp", is_brand_new=False,
                        **resolve_build(build, "bench", registry))

    subjects = [(f"synthetic {mb:>3} MB", _synthetic_state_dir(mb)) for mb in SYNTHETIC_MB]
    for real in _real_state_dirs():
        size = sum(f.stat().st_size for f in real.rglob("*.jsonl"))
        subjects.append((f"REAL {real.name[:28]} ({size / 2**20:.0f} MB)", real))

    print(f"{REPS} reps per segment, page-cache-hot (cold first reads only "
          f"widen the last-prompt share).\n")
    header = f"{'state dir':<34} {'metadata':>10} {'tags':>10} {'last prompt':>12} {'total':>10}   last-prompt share"
    print(header)
    print("-" * len(header))
    for name, state_dir in subjects:
        _report(name, state_dir, identity)


def _entry(inst: Instance) -> ContEntry:
    return ContEntry(identity=inst, workspace_display="/srv/api",
                     is_current_dir=False, is_default_dir=False,
                     is_invalid_dir=False, last_used_display="2 hours ago")


def _report(name: str, state_dir: Path, identity: Instance) -> None:
    inst = dataclasses.replace(identity, state_dir_override=state_dir)
    _last_prompt_display(state_dir)       # warmup: page cache + rich imports

    metadata = _timed(lambda: _render_md(
        "*Continue session `bench__bench`.*\n\n---\n\n```yaml\n"
        "Agent:     bench\nSession:   bench\nWorkspace: /srv/api\n"
        "Engine:    default\nState:     /x\nLast used: 2 hours ago\n```\n"))
    tags = _timed(lambda: _tags_preview(inst))
    prompt = _timed(lambda: _last_prompt_display(state_dir))
    # A fresh ContEntry per rep — cached_property would serve rep 1's answer.
    # Reported as a sanity check on the segment sum; the share is computed from
    # the segments themselves, so run-to-run variance cannot push it past 100%.
    total = _timed(lambda: _entry(inst).preview)

    share = prompt / (metadata + tags + prompt)
    print(f"{name:<34} {metadata * 1000:>8.1f}ms {tags * 1000:>8.1f}ms "
          f"{prompt * 1000:>10.1f}ms {total * 1000:>8.1f}ms   {share:>6.1%}")


def _timed(operation: Callable[[], object]) -> float:
    total = 0.0
    for _ in range(REPS):
        t0 = time.perf_counter()
        operation()
        total += time.perf_counter() - t0
    return total / REPS


def _synthetic_state_dir(target_mb: int) -> Path:
    """A state dir whose one transcript weighs ~`target_mb`, shaped like the
    real thing: mostly tool_result echoes and assistant turns (which the prompt
    scan must parse and reject), with a human prompt every ~50 lines."""
    root = Path(tempfile.mkdtemp(prefix=f"bench_preview_{target_mb}mb_"))
    if target_mb == 0:
        return root
    transcript = root / "projects" / "-workspace" / "s.jsonl"
    transcript.parent.mkdir(parents=True)
    with transcript.open("w") as fh:
        written, line_number = 0, 0
        while written < target_mb * 2**20:
            line_number += 1
            if line_number % 50 == 0:
                event = {"type": "user", "timestamp": "2026-08-07T10:00:00Z",
                         "message": {"role": "user",
                                     "content": f"prompt #{line_number}: {FILLER}"}}
            elif line_number % 2 == 0:
                event = {"type": "user", "timestamp": "2026-08-07T10:00:00Z",
                         "message": {"role": "user", "content": [
                             {"type": "tool_result", "content": FILLER * 8}]}}
            else:
                event = {"type": "assistant", "timestamp": "2026-08-07T10:00:00Z",
                         "message": {"role": "assistant", "content": [
                             {"type": "text", "text": FILLER * 6}]}}
            written += fh.write(json.dumps(event) + "\n")
    return root


def _real_state_dirs() -> list[Path]:
    """State dirs to measure from the real machine: CLI arguments if given,
    else every launcher instance that has any transcript at all."""
    if len(sys.argv) > 1:
        return [Path(arg) for arg in sys.argv[1:]]
    return [d for name in list_all_instances()
            if any((d := instance_state_dir_path(name)).rglob("*.jsonl"))]


if __name__ == "__main__":
    main()
