"""Harness kind — `⟦ ⟧` — "WHICH agent CLI wraps the AI".

A harness is the program a container actually runs: the agent CLI around an
AI — `⟦ClaudeCode⟧` around `⟪Claude⟫`, `⟦GeminiCLI⟧` around `⟪Gemini⟫`,
`⟦CodexCLI⟧` around `⟪ChatGPT⟫`, `⟦GrokBuild⟧` around `⟪Grok⟫`. The sixth tag
kind (operator, 2026-09-14), a 0-or-1 axis like the AI and the engine: an
instance runs exactly one, resolved to its AI's default harness (the AI's
`harness` field, a member key here) when its build names none.

A member dir `agents/harness/<key>/` carries two files:
  tag.info       — the kind's usual fields plus `vendor`, `ais` (the AI members
                   this CLI can run — a build pairing it with another AI is
                   refused), `binary` (the executable in the image) and
                   `package` (where it installs from).
  knobs.mapping  — the launcher's budget PURPOSES → this CLI's native settings
                   (env vars, settings.json paths, config.toml keys …) as
                   templates: `{value}` is the budget's number or the AI's
                   tier word (`{value/100}`, `{value*4}` convert units once,
                   here); `{provider}` is this CLI's slug for the instance's
                   AI, from a `[providers]` table (a multi-AI CLI spells a
                   model `anthropic/claude-sonnet-5`), usable in keys too. A
                   purpose the CLI cannot express is simply absent: rendering
                   reports it as unmapped, never invents. The mapping lived in
                   the AI's dir until 2026-09-14; the settings surface is the
                   CLI's.

`Harness.render(budget, ai)` is the adapter boundary for settings: an engine's
`tag.budget` (AI-neutral) × the AI's tier for its standard × this file → the
native settings the container receives. Which harnesses the launcher can
actually RUN is a matter of code — `launch/ai/` holds one adapter per harness
it has learnt, keyed by the member's name — so a member here can be described
and stored before it can be launched.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from .ai import Ai
from ..file_access import is_file
from .base import Tag, TagError, common_fields, read_toml, walk_tag_tree
from .budget import AMOUNTS, SWITCHES, Budget

KNOBS_FILE = "knobs.mapping"
PROVIDERS_TABLE = "providers"
_TEMPLATE = re.compile(r"\{(?:value(?:([*/])(\d+(?:\.\d+)?))?|(provider))\}")


@dataclass(frozen=True)
class Rendering:
    """A budget in one harness's native settings — `settings` as ordered pairs
    (env vars for Claude Code, settings paths for Gemini CLI, config keys for
    Codex CLI …), and the budget purposes this harness has no words for."""
    settings: tuple[tuple[str, str], ...]
    unmapped: tuple[str, ...]

    @property
    def map(self) -> dict[str, str]:
        return dict(self.settings)


def sorted_harnesses(harnesses: Iterable["Harness"], default_ai: str | None) -> list["Harness"]:
    """The order every harness list shows: the default AI's harnesses first
    (what an unset `harness` most often means), then by name — the analogue
    of `ai.sorted_ais`, so the form and the legend cannot disagree."""
    return sorted(harnesses, key=lambda h: (default_ai not in h.ais, h.name))


@dataclass(frozen=True)
class Harness(Tag):
    parentheses: ClassVar[tuple[str, str]] = ("⟦", "⟧")
    root: ClassVar[str] = "harness"
    nutshell: ClassVar[str] = "WHICH agent CLI wraps the AI — the program the container runs"

    vendor: str = ""
    ais: tuple[str, ...] = ()        # the AI members this CLI runs (validated against agents/ai/ in the registry)
    binary: str = ""                 # the executable in the image (`docker run … <binary> …`)
    package: str = ""                # where it installs from — "npm @google/gemini-cli"
    knobs: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()   # purpose → native (key, template) pairs
    providers: tuple[tuple[str, str], ...] = ()                       # AI member → this CLI's provider slug ({provider})
    dockerfile: Path | None = None   # the CLI's image layer (`<dir>/Dockerfile`), built LAST in every chain that runs it (Instance.build_steps); None for a harness the launcher cannot run yet

    def runs(self, ai_name: str) -> bool:
        """Whether this harness can run the AI member `ai_name`."""
        return ai_name in self.ais

    def knob(self, purpose: str) -> tuple[tuple[str, str], ...] | None:
        """The native settings for a purpose (`thinking.off`,
        `max_output_tokens`), or None when this CLI has no words for it."""
        return dict(self.knobs).get(purpose)

    @property
    def needs_providers(self) -> bool:
        """Whether any template names `{provider}` — then `[providers]` must
        cover every AI this harness runs (scan checks)."""
        return any("{provider}" in text for _, pairs in self.knobs for pair in pairs for text in pair)

    def render(self, budget: Budget, ai: Ai) -> Rendering:
        """The budget in this CLI's settings for the AI `ai`: the AI's tier
        for the budget's standard (model and effort) through the `model` /
        `effort` knobs, then every switch and amount the budget sets through
        its purpose's knob. Order is the budget's field order, so two
        renderings of one budget compare byte for byte."""
        if budget.effort_tier is None:
            raise TagError(f"engine budget sets no effort_tier — {self.name} cannot render it for {ai.name}")
        tier = ai.tier(budget.effort_tier)
        provider = dict(self.providers).get(ai.name)
        settings: list[tuple[str, str]] = []
        unmapped: list[str] = []

        def apply(purpose: str, value: object) -> None:
            knob = self.knob(purpose)
            if knob is None:
                unmapped.append(purpose)
                return
            for key, template in knob:
                settings.append((_fill(key, value, provider, self, ai), _fill(template, value, provider, self, ai)))

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
    def scan(cls, agents_dir: Path) -> list["Harness"]:
        """Discover every harness under `agents/harness/`. Members do not nest
        (a harness is not a refinement of another); each must name its vendor,
        at least one AI, its binary and its package, and carry a knobs
        mapping (possibly sparse: every purpose is optional; `[providers]`
        must cover every AI the harness runs once a template uses
        `{provider}`)."""
        out: list[Harness] = []
        for tag_dir, ancestors in walk_tag_tree(agents_dir / cls.root):
            if ancestors:
                raise TagError(f"{tag_dir}: harness members do not nest ({ancestors[-1]} has a sub-member)")
            fields = common_fields(tag_dir)
            info = fields.pop("_info")
            knobs, providers = _knobs(tag_dir)
            dockerfile = tag_dir / "Dockerfile"
            member = cls(**fields, **_own_fields(info, tag_dir), knobs=knobs, providers=providers,
                         dockerfile=dockerfile if is_file(dockerfile) else None)
            for name in dict(providers):
                if name not in member.ais:
                    raise TagError(f"{tag_dir}/{KNOBS_FILE}: [providers] names {name!r}, which this harness does not run (ais: {', '.join(member.ais)})")
            if member.needs_providers:
                for name in member.ais:
                    if name not in dict(providers):
                        raise TagError(f"{tag_dir}/{KNOBS_FILE}: a template uses {{provider}} but [providers] has no slug for {name!r}")
            out.append(member)
        return out


def _own_fields(info: dict[str, Any], tag_dir: Path) -> dict[str, Any]:
    """The kind's own tag.info keys, type-checked."""
    own: dict[str, Any] = {}
    for key in ("vendor", "binary", "package"):
        value = info.get(key, "")
        if not isinstance(value, str) or not value.strip():
            raise TagError(f"{tag_dir}/tag.info: {key} must be a non-empty string")
        own[key] = value.strip()
    ais = info.get("ais")
    if not isinstance(ais, list) or not ais or not all(isinstance(a, str) and a.strip() for a in ais):
        raise TagError(f"{tag_dir}/tag.info: ais must be a non-empty list of AI member names (the AIs this CLI runs)")
    own["ais"] = tuple(a.strip() for a in ais)
    return own


def _knobs(tag_dir: Path) -> tuple[tuple[tuple[str, tuple[tuple[str, str], ...]], ...], tuple[tuple[str, str], ...]]:
    """`knobs.mapping` → ((purpose, native pairs), (AI, provider slug)).
    Purposes are `model`, `effort`, `<switch>.on` / `.off`, or an amount; every
    value a string template; nested tables spell `thinking.on` as
    `[thinking.on]`. The optional `[providers]` table maps AI member names to
    this CLI's provider slugs for `{provider}`."""
    path = tag_dir / KNOBS_FILE
    if not path.is_file():
        raise TagError(f"{tag_dir}: missing {KNOBS_FILE}")
    data = read_toml(path)
    known = {"model", "effort", *AMOUNTS}
    out: list[tuple[str, tuple[tuple[str, str], ...]]] = []

    def check_template(purpose: str, key: str, text: str) -> None:
        for match in re.finditer(r"\{[^}]*\}", text):
            if not _TEMPLATE.fullmatch(match.group(0)):
                raise TagError(f"{path}: [{purpose}] {key}: unknown placeholder {match.group(0)}")

    def pairs(purpose: str, table: Any) -> tuple[tuple[str, str], ...]:
        if not isinstance(table, dict) or not table:
            raise TagError(f"{path}: [{purpose}] must be a non-empty table of native key = template")
        for key, template in table.items():
            if not isinstance(template, str):
                raise TagError(f"{path}: [{purpose}] {key} must be a string template")
            check_template(purpose, key, key)
            check_template(purpose, key, template)
        return tuple(table.items())

    providers: tuple[tuple[str, str], ...] = ()
    for purpose, table in data.items():
        if purpose == PROVIDERS_TABLE:
            if not isinstance(table, dict) or not table or not all(isinstance(v, str) and v.strip() for v in table.values()):
                raise TagError(f"{path}: [{PROVIDERS_TABLE}] must be a non-empty table of AI member = provider slug")
            providers = tuple((k, v.strip()) for k, v in table.items())
        elif purpose in SWITCHES:
            if not isinstance(table, dict) or set(table) - {"on", "off"}:
                raise TagError(f"{path}: [{purpose}] takes only .on and .off sub-tables")
            for state, sub in table.items():
                out.append((f"{purpose}.{state}", pairs(f"{purpose}.{state}", sub)))
        elif purpose in known:
            out.append((purpose, pairs(purpose, table)))
        else:
            raise TagError(f"{path}: unknown purpose [{purpose}] — the words are: "
                           f"{', '.join(sorted(known | set(SWITCHES) | {PROVIDERS_TABLE}))}")
    return tuple(out), providers


def _fill(template: str, value: object, provider: str | None, harness: "Harness", ai: Ai) -> str:
    """Substitute `{value}` (and `{value/N}`, `{value*N}` for the unit
    conversions a CLI needs — a percent to a fraction, tokens to characters)
    and `{provider}` (this CLI's slug for the AI) into a native-setting key or
    template. An integral result prints without a decimal point."""
    def repl(match: re.Match[str]) -> str:
        op, number, wants_provider = match.group(1), match.group(2), match.group(3)
        if wants_provider:
            if provider is None:
                raise TagError(f"{harness.path}/{KNOBS_FILE}: {{provider}} but no [providers] slug for {ai.name!r}")
            return provider
        if op is None:
            return str(value).lower() if isinstance(value, bool) else str(value)
        n = float(number)
        result = float(value) / n if op == "/" else float(value) * n   # type: ignore[arg-type]
        return str(int(result)) if result == int(result) else f"{result:g}"
    return _TEMPLATE.sub(repl, template)
