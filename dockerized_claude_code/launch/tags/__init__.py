"""The tag system — four *kinds* of tag (engine / profession / specialty /
policy), whose *members* are discovered from the `agents/` file tree rather
than hard-coded (design: `refactoring-replan.md`).

Public surface:
  - `Tag`, `DockerContribution`, `TagError` — base record + parsed docker
    contribution + the fail-loud exception (from `base`).
  - `Engine`, `Profession`, `Specialty`, `Policy` — the four kind classes,
    each with a `scan(agents_dir)` classmethod. `Profession.discover_layers`
    and `Specialty` also surface `Layer` / `Combo`.
  - `merge_fragments` — deep-merge of policy settings fragments.
  - `AgentBuild`, `load_lego` — the per-agent `.lego` build file.
  - `Registry`, `scan_all` — discover + validate the whole tree, then query.
  - `Agent`, `Instance`, `resolve_build` — the identity records launches run on.
  - `store` / `migrations` — the instances.toml store and one-shot
    retired-format conversions.
"""

from .base import DockerContribution, Tag, TagError
from .engine import Engine
from .identity import (
    Agent, Instance, agent_md_path, effective_engine_name, image_chain,
    load_agent, resolve_build,
)
from .lego import AgentBuild, load_lego
from .policy import Policy, PolicyStance, merge_fragments
from .profession import Layer, Profession
from .registry import Registry, scan_all
from .specialty import Combo, Specialty, scan_combos
from . import addendums, migrations, store

__all__ = [
    "Tag", "DockerContribution", "TagError",
    "Engine", "Profession", "Specialty", "Policy", "PolicyStance",
    "Layer", "Combo", "scan_combos", "merge_fragments",
    "AgentBuild", "load_lego",
    "Registry", "scan_all",
    "Agent", "Instance", "image_chain", "resolve_build", "agent_md_path",
    "effective_engine_name",
    "load_agent", "store", "migrations", "addendums",
]
