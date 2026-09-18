"""AI kind — `⟪ ⟫` — "WHICH AI runs the agent".

An AI is a member of `agents/ai/`: the vendor's model line plus the agent CLI
(harness) that runs it — `⟪Claude⟫` run by Claude Code, `⟪Gemini⟫` by Gemini
CLI, `⟪ChatGPT⟫` by Codex CLI, `⟪Grok⟫` by Grok Build. It is the fifth tag
kind (2026-09-13), a 0-or-1 axis like the engine: an instance runs on exactly
one, the member marked `default = true` when its build names none.

The kind's root carries one shared file, and each member dir two:
  ai/capability.standards — the DATED capability standards every member must
                    answer (`YYYYQn`: the frontier's level in that quarter, set
                    by the model released then that raised the record — the
                    Artificial Analysis index it reached, and whether AA
                    estimated it). The two ends, `cheapest` and `best`, are
                    implicit: each AI's own extremes (`tags/budget.py`).
  tag.info        — the kind's usual fields plus `vendor`, `harness` (the key
                    of its default `agents/harness/` member — the CLI that
                    wraps it unless an instance picks another), `key_env`
                    (the vendor's API-key variable — the one every harness
                    reads; `credentials/keys/<ai>.env` defines it), `default`,
                    and the tag's own colours `fg` / `bg` as hex (the first
                    kind coloured per MEMBER, after the logos).
  plan_harnesses  — (tag.info) the harnesses this vendor's SUBSCRIPTION may
                    run in. Vendors differ, so it is per-AI data rather than
                    "its own CLI": Anthropic gates Pro/Max to Claude Code,
                    while OpenAI also permits its ChatGPT sign-in in OpenCode
                    and xAI its SuperGrok login in three harnesses
                    (plans/credentials.md records who permits what, with its
                    sources). The picker warns when a build pairs an AI with a
                    harness outside its list: that pairing is billed per token
                    through an API key, not by the plan.
  foreign_harness_report
                  — (tag.info) what running this AI outside its own CLI has
                    been OBSERVED to cost. The one field here carrying FIELD
                    EVIDENCE rather than a vendor's published terms, and the
                    picker quotes it VERBATIM as the warning's first line — so
                    the sentence must hedge and date itself (the scan
                    refuses one that does not). A launcher that hid a cost its
                    operators keep hitting would be no more honest than one
                    that invented a number. Empty where nothing is reported.
  key_free_tier   — (tag.info) what an API key ALONE gets on this vendor: the
                    floor a pairing outside `plan_harnesses` actually lands
                    on, which differs sharply and is published for some
                    vendors and not others (Google keeps a recurring free
                    tier on an unpaid key; Anthropic gives new API users a
                    one-time credit and meters from the first token after
                    it). Left EMPTY where no vendor page establishes it —
                    the warning then says nothing rather than guessing, and
                    no AI is ranked against another.
  efforts.tiers   — this AI's TIER for every standard: the model id and effort
                    word that meet it (cheapest of the configurations that do;
                    the AI's best, when none does); a `[scale]` table lists
                    the AI's effort vocabulary.

The HARNESS (the CLI around the AI, `tags/harness.py`) owns the other half:
its `knobs.mapping` turns an engine's budget and this AI's tier into the CLI's
native settings (`Harness.render(budget, ai)`) — the settings surface is the
CLI's, not the model's (moved there 2026-09-14). Colours and vocabulary are
validated at scan time under the tree's strict rule, so a typo fails the
suite, not a launch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable
from typing import Any, ClassVar

from .base import Tag, TagError, common_fields, read_toml, walk_tag_tree
from .budget import BEST, CHEAPEST, is_standard, sorted_standards

STANDARDS_FILE = "capability.standards"
TIERS_FILE = "efforts.tiers"
_HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{6}$")
# A report is shown WORD FOR WORD, so the sentence itself must say it is one.
_HEDGED = re.compile(r"report|alleg|observ|unverified|anecdot", re.IGNORECASE)
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True)
class Standard:
    """One dated capability standard, as `agents/ai/capability.standards`
    records it: the quarter, the model that set it, the Artificial Analysis
    Intelligence Index it reached, and whether that figure is AA's estimate."""
    key: str
    set_by: str
    index: float
    estimated: bool = False


@dataclass(frozen=True)
class Tier:
    """One AI's answer to one standard: the model to run and the effort to ask
    of it (None for a model with no effort setting, e.g. a non-reasoning
    model)."""
    model: str
    effort: str | None = None


def sorted_ais(ais: Iterable["Ai"]) -> list["Ai"]:
    """The order every AI list shows: the default member first (it is the
    dotted radio and what an unset `ai` means), then by name — the analogue of
    `engine.sorted_engines`, so the form and the legend cannot disagree."""
    return sorted(ais, key=lambda ai: (not ai.default, ai.name))


@dataclass(frozen=True)
class Ai(Tag):
    parentheses: ClassVar[tuple[str, str]] = ("⟪", "⟫")
    root: ClassVar[str] = "ai"
    nutshell: ClassVar[str] = "WHICH AI runs it — whose models answer the engine's standard"

    vendor: str = ""
    harness: str = ""                 # the KEY of its default harness member (agents/harness/<key>; the registry checks it runs this AI)
    key_env: str = ""                 # the vendor's API-key variable (ANTHROPIC_API_KEY …) — what credentials/keys/<ai>.env must define (plans/credentials.md)
    default: bool = False
    fg: str = ""
    bg: str = ""
    scale: tuple[str, ...] = ()       # this AI's effort words, weakest first (may be empty: no effort setting)
    plan_harnesses: tuple[str, ...] = ()   # the harness members this vendor lets its SUBSCRIPTION run in; every other harness needs an API key (metered). Empty = the launcher knows of no such gating for this AI, and warns about none
    key_free_tier: str = ""           # what an API key ALONE gets on this vendor — the floor a plan-less pairing lands on, in the vendor's published terms. Empty = not established, and the warning says nothing rather than guessing
    foreign_harness_report: str = ""  # what running this AI OUTSIDE its own CLI has been observed to cost — FIELD EVIDENCE, not vendor policy; the picker leads its warning with it, labelled REPORTED. Empty = nothing reported
    tiers: tuple[tuple[str, Tier], ...] = ()                            # standard → tier, cheapest first

    @property
    def style(self) -> str:
        """The tag's prompt_toolkit style — its own colours, not the kind's."""
        return f"fg:{self.fg} bg:{self.bg}"

    @property
    def standards(self) -> tuple[str, ...]:
        """The standards this AI answers, cheapest first — the same tuple for
        every member (scan checks them against the shared file)."""
        return tuple(key for key, _ in self.tiers)

    def tier(self, standard: str) -> Tier:
        return dict(self.tiers)[standard]

    @classmethod
    def scan(cls, agents_dir: Path) -> list["Ai"]:
        """Discover every AI under `agents/ai/`. Members do not nest (an AI is
        not a refinement of another); each must carry both files, a tier for
        exactly the standards the kind's shared file declares (plus the
        two ends), and — across the kind — exactly one `default = true`."""
        out: list[Ai] = []
        keys: tuple[str, ...] | None = None
        for tag_dir, ancestors in walk_tag_tree(agents_dir / cls.root):
            if ancestors:
                raise TagError(f"{tag_dir}: AI members do not nest ({ancestors[-1]} has a sub-member)")
            if keys is None:   # the shared file is read once, and only when there are members to hold to it
                keys = (CHEAPEST, *(s.key for s in load_standards(agents_dir / cls.root)), BEST)
            fields = common_fields(tag_dir)
            info = fields.pop("_info")
            out.append(cls(**fields, **_own_fields(info, tag_dir), **_tiers(tag_dir, keys)))
        defaults = [ai.name for ai in out if ai.default]
        if out and len(defaults) != 1:
            raise TagError(f"agents/ai: exactly one member must set default = true, found {defaults or 'none'}")
        return out


def _own_fields(info: dict[str, Any], tag_dir: Path) -> dict[str, Any]:
    """The kind's own tag.info keys, type-checked."""
    own: dict[str, Any] = {}
    for key in ("vendor", "harness"):
        value = info.get(key, "")
        if not isinstance(value, str) or not value.strip():
            raise TagError(f"{tag_dir}/tag.info: {key} must be a non-empty string"
                           + (" — the key of its default agents/harness/ member" if key == "harness" else ""))
        own[key] = value.strip()
    report = str(info.get("foreign_harness_report", "")).strip() if info.get("foreign_harness_report") is not None else ""
    if not isinstance(info.get("foreign_harness_report", ""), str):
        raise TagError(f"{tag_dir}/tag.info: foreign_harness_report must be a string — what running this AI outside "
                       f"its own CLI has been OBSERVED to cost (omit it when nothing is)")
    if report and not _HEDGED.search(report):
        raise TagError(f"{tag_dir}/tag.info: foreign_harness_report is quoted VERBATIM in the picker, so it must carry "
                       f"its own hedge — one of report / alleged / observed / unverified — and when it was seen. "
                       f"Field evidence must never read like a vendor's published term: {report!r}")
    own["foreign_harness_report"] = report
    floor = info.get("key_free_tier", "")
    if not isinstance(floor, str):
        raise TagError(f"{tag_dir}/tag.info: key_free_tier must be a string — what an API key alone gets on this "
                       f"vendor, in its published terms (omit it when that is not established)")
    own["key_free_tier"] = floor.strip()
    plan = info.get("plan_harnesses", [])
    if not isinstance(plan, list) or not all(isinstance(h, str) and h.strip() for h in plan):
        raise TagError(f"{tag_dir}/tag.info: plan_harnesses must be a list of harness member names — "
                       f"the ones this vendor lets its subscription run in (empty or absent: no known gating)")
    own["plan_harnesses"] = tuple(h.strip() for h in plan)
    key_env = info.get("key_env", "")
    if not isinstance(key_env, str) or not _ENV_NAME.match(key_env):
        raise TagError(f"{tag_dir}/tag.info: key_env must be the vendor's API-key variable, like ANTHROPIC_API_KEY, got {key_env!r}")
    own["key_env"] = key_env
    default = info.get("default", False)
    if not isinstance(default, bool):
        raise TagError(f"{tag_dir}/tag.info: default must be true or false")
    own["default"] = default
    for key in ("fg", "bg"):
        value = info.get(key, "")
        if not isinstance(value, str) or not _HEX_COLOUR.match(value):
            raise TagError(f"{tag_dir}/tag.info: {key} must be a hex colour like #ff8700, got {value!r}")
        own[key] = value.lower()
    return own


def load_standards(ai_root: Path) -> tuple[Standard, ...]:
    """`agents/ai/capability.standards` → the dated standards, weakest first.
    Every table is a quarter (`YYYYQn`; the ends are not written — they are
    each AI's own), with `set_by` (the model that raised the record), `index`
    (a positive number) and an optional `estimated` flag; and because
    standards only rise with time, the indices must strictly increase along
    the dates — a later quarter with a lower figure is a data error."""
    path = ai_root / STANDARDS_FILE
    if not path.is_file():
        raise TagError(f"{ai_root}: missing {STANDARDS_FILE} — the dated standards every member's {TIERS_FILE} answers")
    data = read_toml(path)
    out: list[Standard] = []
    for key in sorted_standards(data):
        table = data[key]
        if key in (CHEAPEST, BEST) or not is_standard(key):
            raise TagError(f"{path}: [{key}] — a standard here is a quarter like 2025Q4 ({CHEAPEST} and {BEST} are implicit)")
        if not isinstance(table, dict):
            raise TagError(f"{path}: [{key}] must be a table")
        set_by, index, estimated = table.get("set_by"), table.get("index"), table.get("estimated", False)
        if not isinstance(set_by, str) or not set_by.strip():
            raise TagError(f"{path}: [{key}] needs set_by — the model that raised the record")
        if isinstance(index, bool) or not isinstance(index, (int, float)) or index <= 0:
            raise TagError(f"{path}: [{key}] index must be a positive number, got {index!r}")
        if not isinstance(estimated, bool):
            raise TagError(f"{path}: [{key}] estimated must be true or false")
        if set(table) - {"set_by", "index", "estimated"}:
            raise TagError(f"{path}: [{key}] has unknown keys {sorted(set(table) - {'set_by', 'index', 'estimated'})}")
        if out and index <= out[-1].index:
            raise TagError(f"{path}: standards only rise — [{key}] {index} is not above [{out[-1].key}] {out[-1].index}")
        out.append(Standard(key=key, set_by=set_by.strip(), index=float(index), estimated=estimated))
    return tuple(out)


def _tiers(tag_dir: Path, keys: tuple[str, ...]) -> dict[str, Any]:
    """`efforts.tiers` → `scale` and `tiers`: a table for exactly the given
    standards (the ends plus the shared file's quarters), no other, each a
    model string and an effort within the scale (or none, for a model without
    an effort setting)."""
    path = tag_dir / TIERS_FILE
    if not path.is_file():
        raise TagError(f"{tag_dir}: missing {TIERS_FILE}")
    data = read_toml(path)
    scale_table = data.pop("scale", {})
    efforts = scale_table.get("efforts", []) if isinstance(scale_table, dict) else None
    if not isinstance(efforts, list) or not all(isinstance(e, str) for e in efforts):
        raise TagError(f"{path}: [scale] efforts must be a list of strings")
    missing = [s for s in keys if s not in data]
    extra = [k for k in data if k not in keys]
    if missing or extra:
        raise TagError(f"{path}: standards must be exactly {', '.join(keys)} ({STANDARDS_FILE} plus the ends)"
                       + (f" — missing {missing}" if missing else "") + (f" — unknown {extra}" if extra else ""))
    tiers: list[tuple[str, Tier]] = []
    for key in keys:
        table = data[key]
        if not isinstance(table, dict) or not isinstance(table.get("model"), str) or not table["model"]:
            raise TagError(f"{path}: [{key}] needs a model string")
        effort = table.get("effort")
        if effort is not None and (not isinstance(effort, str) or (efforts and effort not in efforts)):
            raise TagError(f"{path}: [{key}] effort {effort!r} is not in this AI's scale {efforts}")
        unknown = set(table) - {"model", "effort"}
        if unknown:
            raise TagError(f"{path}: [{key}] has unknown keys {sorted(unknown)}")
        tiers.append((key, Tier(model=table["model"], effort=effort)))
    return {"scale": tuple(efforts), "tiers": tuple(tiers)}
