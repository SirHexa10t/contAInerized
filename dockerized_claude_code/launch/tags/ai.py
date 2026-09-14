"""AI kind — `⟪ ⟫` — "WHICH AI runs the agent".

An AI is a member of `agents/ai/`: the vendor's model line plus the agent CLI
(harness) that runs it — `⟪Claude⟫` run by Claude Code, `⟪Gemini⟫` by Gemini
CLI, `⟪ChatGPT⟫` by Codex CLI, `⟪Grok⟫` by Grok Build. It is the fifth tag
kind (2026-09-13), a 0-or-1 axis like the engine: an instance runs on exactly
one, the member marked `default = true` when its build names none.

The kind's root carries one shared file, and each member dir three:
  ai/capability.standards — the DATED capability standards every member must
                    answer (`YYYYQn`: the frontier's level in that quarter, set
                    by the model released then that raised the record — the
                    Artificial Analysis index it reached, and whether AA
                    estimated it). The two ends, `cheapest` and `best`, are
                    implicit: each AI's own extremes (`tags/budget.py`).
  tag.info        — the kind's usual fields plus `vendor`, `harness` (the CLI's
                    name), `default`, and the tag's own colours `fg` / `bg` as
                    hex (the first kind coloured per MEMBER, after the logos).
  efforts.tiers   — this AI's TIER for every standard: the model id and effort
                    word that meet it (cheapest of the configurations that do;
                    the AI's best, when none does); a `[scale]` table lists
                    the AI's effort vocabulary.
  knobs.mapping   — the launcher's budget PURPOSES → this AI's native settings,
                    as `{value}` templates (`{value/100}`, `{value*4}` convert
                    units once, here). A purpose the AI cannot express is simply
                    absent: rendering reports it as unmapped, never invents.

`Ai.render(budget)` is the adapter boundary for settings: an engine's
`tag.budget` (AI-neutral) × this AI's two files → the native settings the
container receives. Colours and vocabulary are validated at scan time under the
tree's strict rule, so a typo fails the suite, not a launch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable
from typing import Any, ClassVar

from .base import Tag, TagError, common_fields, read_toml, walk_tag_tree
from .budget import AMOUNTS, BEST, CHEAPEST, SWITCHES, Budget, is_standard, sorted_standards

STANDARDS_FILE = "capability.standards"
TIERS_FILE = "efforts.tiers"
KNOBS_FILE = "knobs.mapping"
_HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{6}$")
_TEMPLATE = re.compile(r"\{value(?:([*/])(\d+(?:\.\d+)?))?\}")


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


@dataclass(frozen=True)
class Rendering:
    """A budget in one AI's native settings — `settings` as ordered pairs
    (env vars for Claude Code, settings paths for Gemini, config keys for
    Codex …), and the budget purposes this AI has no words for."""
    settings: tuple[tuple[str, str], ...]
    unmapped: tuple[str, ...]

    @property
    def map(self) -> dict[str, str]:
        return dict(self.settings)


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
    harness: str = ""                 # the agent CLI's name — "Claude Code" (its adapter: launch/ai)
    default: bool = False
    fg: str = ""
    bg: str = ""
    scale: tuple[str, ...] = ()       # this AI's effort words, weakest first (may be empty: no effort setting)
    tiers: tuple[tuple[str, Tier], ...] = ()                            # standard → tier, cheapest first
    knobs: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()   # purpose → native (key, template) pairs

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

    def knob(self, purpose: str) -> tuple[tuple[str, str], ...] | None:
        """The native settings for a purpose (`thinking.off`,
        `max_output_tokens`), or None when this AI has no words for it."""
        return dict(self.knobs).get(purpose)

    def render(self, budget: Budget) -> Rendering:
        """The budget in this AI's settings: its tier for the standard (model
        and effort) through the `model` / `effort` knobs, then every switch and
        amount the budget sets through its purpose's knob. Order is the
        budget's field order, so two renderings of one budget compare byte for
        byte."""
        if budget.standard is None:
            raise TagError(f"engine budget sets no standard — {self.name} cannot render it")
        tier = self.tier(budget.standard)
        settings: list[tuple[str, str]] = []
        unmapped: list[str] = []

        def apply(purpose: str, value: object) -> None:
            knob = self.knob(purpose)
            if knob is None:
                unmapped.append(purpose)
                return
            settings.extend((key, _fill(template, value)) for key, template in knob)

        apply("model", tier.model)
        if tier.effort is not None:
            apply("effort", tier.effort)
        for name in SWITCHES:
            if (flag := getattr(budget, name)) is not None:
                apply(f"{name}.{'on' if flag else 'off'}", flag)
        for name in AMOUNTS:
            if (amount := getattr(budget, name)) is not None:
                apply(name, amount)
        return Rendering(tuple(settings), tuple(unmapped))

    @classmethod
    def scan(cls, agents_dir: Path) -> list["Ai"]:
        """Discover every AI under `agents/ai/`. Members do not nest (an AI is
        not a refinement of another); each must carry all three files, a tier
        for exactly the standards the kind's shared file declares (plus the
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
            out.append(cls(**fields, **_own_fields(info, tag_dir),
                           **_tiers(tag_dir, keys), knobs=_knobs(tag_dir)))
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
            raise TagError(f"{tag_dir}/tag.info: {key} must be a non-empty string")
        own[key] = value.strip()
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


def _knobs(tag_dir: Path) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """`knobs.mapping` → (purpose, native pairs). Purposes are `model`,
    `effort`, `<switch>.on` / `.off`, or an amount; every value a string
    template. Nested tables spell `thinking.on` as `[thinking.on]`."""
    path = tag_dir / KNOBS_FILE
    if not path.is_file():
        raise TagError(f"{tag_dir}: missing {KNOBS_FILE}")
    data = read_toml(path)
    known = {"model", "effort", *AMOUNTS}
    out: list[tuple[str, tuple[tuple[str, str], ...]]] = []

    def pairs(purpose: str, table: Any) -> tuple[tuple[str, str], ...]:
        if not isinstance(table, dict) or not table:
            raise TagError(f"{path}: [{purpose}] must be a non-empty table of native key = template")
        for key, template in table.items():
            if not isinstance(template, str):
                raise TagError(f"{path}: [{purpose}] {key} must be a string template")
            for match in re.finditer(r"\{[^}]*\}", template):
                if not _TEMPLATE.fullmatch(match.group(0)):
                    raise TagError(f"{path}: [{purpose}] {key}: unknown placeholder {match.group(0)}")
        return tuple(table.items())

    for purpose, table in data.items():
        if purpose in SWITCHES:
            if not isinstance(table, dict) or set(table) - {"on", "off"}:
                raise TagError(f"{path}: [{purpose}] takes only .on and .off sub-tables")
            for state, sub in table.items():
                out.append((f"{purpose}.{state}", pairs(f"{purpose}.{state}", sub)))
        elif purpose in known:
            out.append((purpose, pairs(purpose, table)))
        else:
            raise TagError(f"{path}: unknown purpose [{purpose}] — the words are: "
                           f"{', '.join(sorted(known | set(SWITCHES)))}")
    return tuple(out)


def _fill(template: str, value: object) -> str:
    """Substitute `{value}` (and `{value/N}`, `{value*N}` for the unit
    conversions an AI needs — a percent to a fraction, tokens to characters)
    into a native-setting template. An integral result prints without a
    decimal point."""
    def repl(match: re.Match[str]) -> str:
        op, number = match.group(1), match.group(2)
        if op is None:
            return str(value).lower() if isinstance(value, bool) else str(value)
        n = float(number)
        result = float(value) / n if op == "/" else float(value) * n   # type: ignore[arg-type]
        return str(int(result)) if result == int(result) else f"{result:g}"
    return _TEMPLATE.sub(repl, template)
