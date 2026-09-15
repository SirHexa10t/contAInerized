"""The tag system — five *kinds* of tag (ai / engine / profession / specialty /
policy), whose *members* are discovered from the `agents/` file tree rather
than hard-coded (design: `refactoring-replan.md`).

Public surface:
  - `Tag`, `DockerContribution`, `TagError` — base record + parsed docker
    contribution + the fail-loud exception (from `base`).
  - `Ai`, `Engine`, `Profession`, `Specialty`, `Policy` — the five kind classes,
    each with a `scan(agents_dir)` classmethod. `Profession.discover_layers`
    and `Specialty` also surface `Layer` / `Combo`.
  - `merge_fragments` — deep-merge of policy settings fragments.
  - `AgentBuild`, `load_lego` — the per-agent `.lego` build file.
  - `Registry`, `scan_all` — discover + validate the whole tree, then query.
  - `Agent`, `Instance`, `resolve_build` — the identity records launches run on.
  - `store` / `migrations` — the instances.toml store and one-shot
    retired-format conversions.
  - `ToolkitEntry` — one row of a profession's optional `template.form`
    (`Profession.load_toolkit()`); `toolkit_profile` owns the per-user
    `~/.ai-agents/<profession>_profile.toml` that toggles them.
"""

from .ai import Ai, Standard, Tier, sorted_ais
from .base import DockerContribution, Tag, TagError
from .budget import BEST, CHEAPEST, Budget, is_standard, rank_of, sorted_standards
from .engine import Engine, sorted_engines, standard_rank
from .harness import Harness, Rendering, sorted_harnesses
from .identity import (
    Agent, Instance, agent_md_path, effective_engine_name, image_chain,
    load_agent, resolve_build,
)
from .lego import AgentBuild, load_lego
from .policy import Policy, PolicyStance, merge_fragments
from .profession import Layer, Profession, ToolkitEntry
from .registry import Registry, TagProblem, scan_all
from .specialty import Combo, Specialty, scan_combos
from . import addendums, migrations, store, toolkit_profile

__all__ = [
    "Tag", "DockerContribution", "TagError",
    "Ai", "Tier", "Standard", "Rendering", "Budget", "BEST", "CHEAPEST", "is_standard", "rank_of",
    "sorted_standards", "sorted_ais", "sorted_engines", "standard_rank", "Harness", "sorted_harnesses",
    "Engine", "Profession", "Specialty", "Policy", "PolicyStance",
    "Layer", "Combo", "scan_combos", "merge_fragments", "ToolkitEntry",
    "AgentBuild", "load_lego",
    "Registry", "TagProblem", "scan_all",
    "Agent", "Instance", "image_chain", "resolve_build", "agent_md_path",
    "effective_engine_name",
    "load_agent", "store", "migrations", "addendums", "toolkit_profile",
]
