"""Fixtures the gui test modules share — real objects resolved against the
real tag tree, never fakes: an `Instance` built the way a launch builds one,
so a picker or preview test exercises the same resolution path the launcher
does. Not a test module (unittest discovers `test*.py`), so nothing here runs
on its own; it was copy-pasted into four test files until 2026-09-09."""

from pathlib import Path

from launch.paths import AGENTS_DIR
from launch.tags import AgentBuild, Instance, resolve_build, scan_all

REGISTRY = scan_all(AGENTS_DIR)


def make_inst(agent="poet", session="s", workspace="/tmp", *,
              professions=(), specialties=(), policies=()):
    """A real Instance resolved against the real registry (engine falls back
    agent-name → default, exactly like a launch)."""
    build = AgentBuild(engine=None, professions=tuple(professions),
                       specialties=tuple(specialties), policies=tuple(policies))
    return Instance(agent=agent, md_path=Path(f"/fake/{agent}.md"), session=session,
                    workspace=workspace, is_brand_new=False,
                    **resolve_build(build, agent, REGISTRY))
