"""The tag system — four *kinds* of agent modifier (engine / profession /
specialty / policy), whose *members* are discovered from the `agents/` file
tree rather than hard-coded. Replaces the old `InstanceModifiers` enum +
filename grammar + modes map (see `refactoring-replan.md`).

Public surface:
  - `Tag`, `DockerContribution`, `TagError` — base record + parsed docker
    contribution + the fail-loud exception (from `base`).
  - `Engine`, `Profession`, `Specialty`, `Policy` — the four kind classes,
    each with a `scan(agents_dir)` classmethod. `Profession.discover_layers`
    and `Specialty` also surface `Layer` / `Combo`.
  - `merge_fragments` — deep-merge of policy settings fragments.
  - `AgentBuild`, `load_lego` — the per-agent `.lego` build file.
  - `Registry`, `scan_all` — discover + validate the whole tree, then query.

P0 status: nothing in the live launcher imports this yet — it is exercised
only by `tests/test_tags.py` against fixture trees.
"""

from .base import DockerContribution, Tag, TagError
from .engine import Engine
from .identity import (
    Agent, Instance, agent_md_path, image_chain, load_agent, resolve_build,
)
from .lego import AgentBuild, load_lego
from .policy import Policy, merge_fragments
from .profession import Layer, Profession
from .registry import Registry, scan_all
from .specialty import Combo, Specialty, scan_combos
from . import addendums, store

__all__ = [
    "Tag", "DockerContribution", "TagError",
    "Engine", "Profession", "Specialty", "Policy",
    "Layer", "Combo", "scan_combos", "merge_fragments",
    "AgentBuild", "load_lego",
    "Registry", "scan_all",
    "Agent", "Instance", "image_chain", "resolve_build", "agent_md_path",
    "load_agent", "store", "addendums",
]
