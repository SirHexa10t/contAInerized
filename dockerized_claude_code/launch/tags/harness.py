"""Harness kind — `⟦ ⟧` — "WHICH agent CLI wraps the AI".

A harness is the program a container actually runs: the agent CLI around an
AI — `⟦ClaudeCode⟧` around `⟪Claude⟫`, `⟦GeminiCLI⟧` around `⟪Gemini⟫`,
`⟦CodexCLI⟧` around `⟪ChatGPT⟫`, `⟦GrokBuild⟧` around `⟪Grok⟫`. The sixth tag
kind (operator, 2026-09-14), a 0-or-1 axis like the AI and the engine: an
instance runs exactly one, resolved to its AI's default harness (the AI's
`harness` field, a member key here) when its build names none.

A member dir `agents/harness/<key>/` carries up to three files:
  tag.info       — the kind's usual fields plus `vendor`, `ais` (the AI members
                   this CLI can run — a build pairing it with another AI is
                   refused), `binary` (the executable in the image),
                   `package` (where it installs from) and, optionally,
                   `layer`: the name of a hidden image layer
                   (`agents/profession/_<name>/`) that must sit directly
                   beneath this CLI's own layer — `node` for the npm-installed
                   CLIs. One layer can serve many harnesses; the chain stays
                   linear because it goes in immediately before the harness.
  engine.mapping  — the launcher's budget PURPOSES → this CLI's native settings
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
  policy.mapping — the launcher's POLICY words (`tags/rules.py`) → this CLI's
                   permission surface: tool names per capability, how a shell
                   command becomes a rule, where the rules land
                   (`tags/policy_mapping.py`). Optional: a harness without one
                   maps nothing, so every policy word is unmapped on it.

`Harness.render(budget, ai, model=…, effort=…)` is the adapter boundary for
settings: an engine's `tag.budget` (AI-neutral) × the model and effort the
instance runs (`Instance.model` / `.effort`) × this file → the native
settings the container receives. `Harness.render_policy(name, rules)`
is its permissions twin: a policy's `tag.rules` × `policy.mapping` → this
CLI's rules. Which harnesses the launcher can
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
from .policy_mapping import NO_POLICY_MAPPING, PolicyMapping, PolicyRendering, parse_policy_mapping
from .profession import Layer
from .rules import Rules

ENGINE_MAPPING_FILE = "engine.mapping"
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
    engine_mapping: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()   # its engine.mapping: purpose → native (key, template) pairs
    providers: tuple[tuple[str, str], ...] = ()                       # AI member → this CLI's provider slug ({provider})
    dockerfile: Path | None = None   # the CLI's image layer (`<dir>/Dockerfile`), built LAST in every chain that runs it (Instance.build_steps); None for a harness the launcher cannot build yet — whether it can START is the adapter's `startable`, not this
    layer: Layer | None = None       # the hidden layer it claims (`layer = "node"`), built directly beneath its own — the prerequisite its install needs
    policy_mapping: PolicyMapping | None = None   # its policy.mapping, or None: it maps no policy word yet

    def runs(self, ai_name: str) -> bool:
        """Whether this harness can run the AI member `ai_name`."""
        return ai_name in self.ais

    def engine_templates(self, purpose: str) -> tuple[tuple[str, str], ...] | None:
        """The native settings for a purpose (`thinking.off`,
        `max_output_tokens`), or None when this CLI has no words for it."""
        return dict(self.engine_mapping).get(purpose)

    @property
    def needs_providers(self) -> bool:
        """Whether any template names `{provider}` — then `[providers]` must
        cover every AI this harness runs (scan checks)."""
        return any("{provider}" in text for _, pairs in self.engine_mapping for pair in pairs for text in pair)

    def render(self, budget: Budget, ai: Ai, *, model: str, effort: str | None) -> Rendering:
        """The budget in this CLI's settings for the AI `ai`: `model` and
        `effort` — decided by the instance (`Instance.model` / `.effort`:
        its picks, else the engine's model at that model's highest level)
        — through the `model` / `effort` rows (no effort row for None),
        then every switch and amount the budget sets through its purpose's
        row. Order is the budget's field order, so two renderings of one
        budget compare byte for byte. The one path to ANTHROPIC_MODEL /
        GEMINI_MODEL for a pick and the engine's model alike."""
        provider = dict(self.providers).get(ai.name)
        settings: list[tuple[str, str]] = []
        unmapped: list[str] = []

        def apply(purpose: str, value: object) -> None:
            templates = self.engine_templates(purpose)
            if templates is None:
                unmapped.append(purpose)
                return
            for key, template in templates:
                settings.append((_fill(key, value, provider, self, ai), _fill(template, value, provider, self, ai)))

        apply("model", model)
        if effort is not None:
            apply("effort", effort)
        for name in SWITCHES:
            if (flag := getattr(budget, name)) is not None:
                apply(f"{name}.{'on' if flag else 'off'}", flag)
        for name in AMOUNTS:
            if (amount := getattr(budget, name)) is not None:
                apply(name, amount)
        return Rendering(tuple(settings), tuple(unmapped))

    def render_policy(self, policy: str, rules: Rules) -> PolicyRendering:
        """The policy named `policy` in this CLI's permission surface — its
        settings fragment, its rules-file records, and every word this CLI
        has no row for (all of them when it has no policy.mapping)."""
        return (self.policy_mapping or NO_POLICY_MAPPING).render(policy, rules)

    @property
    def policy_file(self) -> str | None:
        """The rules file this CLI reads its policies from, relative to its
        config root, or None when its policies live in its settings."""
        return self.policy_mapping.file_path if self.policy_mapping else None

    def policy_args(self, config_root: str) -> tuple[str, ...]:
        """The CLI args pointing this harness at its rules file inside the
        container's `config_root` — () when it keeps none."""
        return self.policy_mapping.args(config_root) if self.policy_mapping else ()

    @classmethod
    def scan(cls, agents_dir: Path, layers: dict[str, Layer] | None = None) -> list["Harness"]:
        """Discover every harness under `agents/harness/`. Members do not nest
        (a harness is not a refinement of another); each must name its vendor,
        at least one AI, its binary and its package, and carry an
        engine.mapping (possibly sparse: every purpose is optional;
        `[providers]` must cover every AI the harness runs once a template
        uses `{provider}`); a policy.mapping is optional, and checked in
        full when present. A `layer` it names must be one of `layers` (the
        profession tree's hidden dirs) and sit at that tree's ROOT: a layer
        nested under a profession would require that profession of every
        instance in this CLI, which is not a harness's call to make."""
        out: list[Harness] = []
        for tag_dir, ancestors in walk_tag_tree(agents_dir / cls.root):
            if ancestors:
                raise TagError(f"{tag_dir}: harness members do not nest ({ancestors[-1]} has a sub-member)")
            fields = common_fields(tag_dir)
            info = fields.pop("_info")
            engine_mapping, providers = _engine_mapping(tag_dir)
            dockerfile = tag_dir / "Dockerfile"
            member = cls(**fields, **_own_fields(info, tag_dir), engine_mapping=engine_mapping, providers=providers,
                         dockerfile=dockerfile if is_file(dockerfile) else None,
                         layer=_claimed_layer(info, tag_dir, layers or {}),
                         policy_mapping=parse_policy_mapping(tag_dir))
            for name in dict(providers):
                if name not in member.ais:
                    raise TagError(f"{tag_dir}/{ENGINE_MAPPING_FILE}: [providers] names {name!r}, which this harness does not run (ais: {', '.join(member.ais)})")
            if member.needs_providers:
                for name in member.ais:
                    if name not in dict(providers):
                        raise TagError(f"{tag_dir}/{ENGINE_MAPPING_FILE}: a template uses {{provider}} but [providers] has no slug for {name!r}")
            out.append(member)
        return out


def _claimed_layer(info: dict[str, Any], tag_dir: Path, layers: dict[str, Layer]) -> Layer | None:
    """The hidden layer a harness's `layer` key names, resolved — or None
    when it names none. A name no hidden dir carries, or one nested under a
    profession, fails the scan (so `bash check.sh`), never a build."""
    name = info.get("layer")
    if name is None:
        return None
    if not isinstance(name, str) or not name.strip():
        raise TagError(f"{tag_dir}/tag.info: layer must be the name of a hidden layer (agents/profession/_<name>/)")
    layer = layers.get(name.strip())
    if layer is None:
        raise TagError(f"{tag_dir}/tag.info: layer {name!r} is not a hidden layer "
                       f"(known: {', '.join(sorted(layers)) or 'none'}) — create agents/profession/_{name}/")
    if layer.requires:
        raise TagError(f"{tag_dir}/tag.info: layer {name!r} sits under {', '.join(sorted(layer.requires))} — "
                       f"a harness's layer must be at agents/profession/'s root, since claiming it "
                       f"would otherwise force that profession on every instance in this CLI")
    return layer


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


def _engine_mapping(tag_dir: Path) -> tuple[tuple[tuple[str, tuple[tuple[str, str], ...]], ...], tuple[tuple[str, str], ...]]:
    """`engine.mapping` → ((purpose, native pairs), (AI, provider slug)).
    Purposes are `model`, `effort`, `<switch>.on` / `.off`, or an amount; every
    value a string template; nested tables spell `thinking.on` as
    `[thinking.on]`. The optional `[providers]` table maps AI member names to
    this CLI's provider slugs for `{provider}`."""
    path = tag_dir / ENGINE_MAPPING_FILE
    if not path.is_file():
        raise TagError(f"{tag_dir}: missing {ENGINE_MAPPING_FILE}")
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
                raise TagError(f"{harness.path}/{ENGINE_MAPPING_FILE}: {{provider}} but no [providers] slug for {ai.name!r}")
            return provider
        if op is None:
            return str(value).lower() if isinstance(value, bool) else str(value)
        n = float(number)
        result = float(value) / n if op == "/" else float(value) * n   # type: ignore[arg-type]
        return str(int(result)) if result == int(result) else f"{result:g}"
    return _TEMPLATE.sub(repl, template)
