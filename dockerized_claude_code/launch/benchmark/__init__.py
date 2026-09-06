"""Measurement scripts, run by hand — never by the gate.

Each member answers one question the project actually had to settle, and
exists so the answer stays reproducible rather than becoming folklore: thread
vs process for transcript reads, cached dict vs re-read for the agent .md
files, per-rule vs batched iptables execs, where a preview's build time goes.
Run one with `python3 -m launch.benchmark.<name>`.

They are NOT tests: they measure, they take seconds to minutes, and their
numbers depend on the host. What the gate does check is that they still work
— `TestBenchmarksStayRunnable` imports each one and resolves its patch
targets, because a refactor that moves a patched name leaves a benchmark
broken and silent (that is exactly how one of them spent months reporting a
fabricated result).
"""
