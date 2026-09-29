"""A harness's POLICY MAPPING — the launcher's policy words (`tags/rules.py`)
in one CLI's own permission surface, read from
`agents/harness/<key>/policy.mapping` (TOML). The permissions twin of the
harness's `engine.mapping` (`tags/harness.py`): a policy states what it wants
in the launcher's words, the mapping says how this CLI spells it, and a word
the mapping has no row for is REPORTED as unmapped, never invented (operator,
2026-09-26). `Harness.render_policy` is the entry point.

Two formats, because the two CLIs the launcher has read keep permissions in
structurally different places, and the file names its own:

  settings       rules are strings in the CLI's settings file, merged with
                 every other fragment (Claude Code: `permissions.deny` holds
                 `Bash(git *)`, `WebFetch` …). Tables: tools, shell (`word`
                 and `stem` rule templates, `{command}` filled in), lists
                 (stance → the dotted settings key its rules land under),
                 mode.<word>, fixed.
  policy-engine  rules are records in a TOML file of their own, which the CLI
                 is pointed at by a flag (Gemini CLI's Policy Engine:
                 `[[rule]]` tables of toolName, commandPrefix, decision,
                 priority …). Tables: tools, shell (the shell tool's name),
                 file (its path inside the config root, and the args naming
                 it, `{path}` filled in), rules.allow / rules.deny (fields
                 every rule of that stance carries, `{policy}` filled in),
                 mode.<word>, fixed.

Both formats must STATE their FIXED tier: `[fixed] path`, the system
location the CLI reads whatever config root or flags it is started with,
where the image bakes the ALWAYS-ON policies' denies as root
(`agents_crud.fixed_policy`, the harness Dockerfiles) so that a second copy
of the CLI the agent starts itself still obeys them — or `[fixed] none`, the
reason there is none. Stated rather than optional, so a harness cannot lose
the tier by an author's omission (gate fixed-tier, 2026-09-26) — the adapter
record's house rule applied to this file: "a harness that lacks one is a
`None`, never a guess" (`launch/ai/adapter.py`). Denies only:
on a tiered engine a fixed allow would outrank every per-instance deny, and a
fixed demand could not differ per member (`Policy.scan` holds always-on
policies to deny).

A harness without the file maps nothing, so every word of every policy is
unmapped on it: exactly true of a CLI whose permission surface nobody has
read yet, and what makes a deny refuse there instead of evaporating.

Leaf within the tag package, beside `rules.py`: imports `base` and `rules`
(and the stdlib-only `launch.toml_emit` for the rules file's quoting).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .. import toml_emit
from .base import TagError, read_toml
from .rules import CAPABILITIES, MODES, Actions, Rules

POLICY_MAPPING_FILE = "policy.mapping"
SETTINGS_FORMAT = "settings"
POLICY_ENGINE_FORMAT = "policy-engine"
FORMATS = (SETTINGS_FORMAT, POLICY_ENGINE_FORMAT)
STANCES = ("allow", "deny")        # the stances that carry rules; [demand] carries a mode
_TABLES = {
    SETTINGS_FORMAT: ("format", "tools", "shell", "lists", "mode", "fixed"),
    POLICY_ENGINE_FORMAT: ("format", "tools", "shell", "file", "rules", "mode", "fixed"),
}
# What the fixed file holds — a settings file or a rules file — and so how the
# harness Dockerfile's build-time check counts its rules: every list entry of a
# .json, the [[rule]] tables of a .toml (against FIXED_POLICY_COUNT).
_FIXED_SUFFIX = {SETTINGS_FORMAT: ".json", POLICY_ENGINE_FORMAT: ".toml"}
# The fields a policy-engine mapping may set on its rules, beside the ones the
# renderer writes itself (toolName, commandPrefix / commandRegex, and
# decision, which is the stance's own word).
_RULE_FIELDS = {"priority": int, "modes": list, "denyMessage": str}
_MAX_PRIORITY = 999                # the Policy Engine's schema clamps priority to [0, 999]
_JS_REGEX_SPECIAL = re.compile(r"[\\^$.*+?()[\]{}|/]")
RULES_FILE_HEADER = (
    "# Written by the launcher from this instance's policies (agents/policy/*/tag.rules,\n"
    "# rendered through the harness's policy.mapping). Rewritten on every launch and\n"
    "# mounted read-only: an edit here would not survive, and could not be made.\n"
)
FIXED_FILE_HEADER = (
    "# Baked into the image by the launcher, as root: the ALWAYS-ON policies' denies\n"
    "# (agents/policy/*/tag.rules, rendered through the harness's policy.mapping), at\n"
    "# the CLI's fixed tier. Rebuilt with the image whenever they change.\n"
)


@dataclass(frozen=True)
class PolicyRendering:
    """One policy in one harness's words: `settings`, a fragment for the CLI's
    settings file (dotted keys already nested), merged like any other; `rules`,
    records for the CLI's own rules file (policy-engine format only); and
    `unmapped`, every word this harness has no row for, as the tag.rules line
    that named it."""
    settings: dict[str, Any]
    rules: tuple[dict[str, Any], ...]
    unmapped: tuple["Unmapped", ...]


@dataclass(frozen=True)
class Unmapped:
    """A policy word one harness cannot express: the table it sits in
    (`allow`, `deny`, `demand`), its key there (`tools`, `shell`,
    `shell_stems`, `mode`) and the word itself."""
    stance: str
    key: str
    word: str

    def __str__(self) -> str:
        return f"[{self.stance}] {self.key} {self.word!r}"


@dataclass(frozen=True)
class PolicyMapping:
    """One harness's `policy.mapping`, parsed. Fields a format does not use
    stay empty; everything is tuples, so the Harness record stays hashable."""
    format: str
    tools: tuple[tuple[str, tuple[str, ...]], ...] = ()                  # capability → tool names, file order
    shell_word: tuple[str, ...] = ()                                     # settings: rule templates for a command word
    shell_stem: tuple[str, ...] = ()                                     # settings: rule templates for a command stem
    shell_tool: str | None = None                                        # policy-engine: the tool whose commands the rules match
    lists: tuple[tuple[str, str], ...] = ()                              # settings: stance → dotted settings key
    file_path: str | None = None                                         # policy-engine: the rules file, relative to the config root
    file_args: tuple[str, ...] = ()                                      # policy-engine: the CLI args that name it
    rule_fields: tuple[tuple[str, tuple[tuple[str, Any], ...]], ...] = ()   # policy-engine: stance → fields its rules carry
    modes: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()      # mode → (dotted settings key, value) pairs
    fixed_path: str | None = None                                        # the fixed tier's absolute system path, or None: `[fixed] none` (or no mapping at all)

    @property
    def maps_shell(self) -> bool:
        return bool(self.shell_word or self.shell_tool)

    def render(self, policy: str, rules: Rules) -> PolicyRendering:
        """`rules` (the policy named `policy`) in this CLI's words. Allow
        and deny each render capabilities first — every mapped one for
        `all = true` — then shell words, then stems, so two renderings of one
        policy compare equal; a demanded mode renders into settings."""
        tools = dict(self.tools)
        produced: dict[str, list[Any]] = {stance: [] for stance in STANCES}
        unmapped: list[Unmapped] = []
        for stance in STANCES:
            actions: Actions = getattr(rules, stance)
            for capability in (tuple(tools) if actions.all else actions.tools):
                if capability in tools:
                    produced[stance] += self._tool_rules(tools[capability])
                else:
                    unmapped.append(Unmapped(stance, "tools", capability))
            for kind, key, commands in (("word", "shell", actions.shell), ("stem", "shell_stems", actions.shell_stems)):
                for command in commands:
                    if self.maps_shell:
                        produced[stance] += self._shell_rules(kind, command)
                    else:
                        unmapped.append(Unmapped(stance, key, command))
        settings: dict[str, Any] = {}
        if rules.mode is not None:
            pairs = dict(self.modes).get(rules.mode)
            if pairs is None:
                unmapped.append(Unmapped("demand", "mode", rules.mode))
            for key, value in pairs or ():
                _set_dotted(settings, key, value)
        if self.format == SETTINGS_FORMAT:
            for stance, key in self.lists:
                if produced[stance]:
                    _set_dotted(settings, key, produced[stance])
            return PolicyRendering(settings, (), tuple(unmapped))
        records = tuple({**rule, "decision": stance, **self._fields(stance, policy)}
                        for stance in STANCES for rule in produced[stance])
        return PolicyRendering(settings, records, tuple(unmapped))

    def args(self, config_root: str) -> tuple[str, ...]:
        """The CLI args that point this harness at its rules file inside
        `config_root` (the container path), or () when it keeps none."""
        if self.file_path is None:
            return ()
        path = str(PurePosixPath(config_root) / self.file_path)
        return tuple(arg.replace("{path}", path) for arg in self.file_args)

    def _tool_rules(self, names: tuple[str, ...]) -> list[Any]:
        """A capability's rules: each tool name is a rule string in settings;
        one record naming them all for the Policy Engine."""
        if self.format == SETTINGS_FORMAT:
            return list(names)
        return [{"toolName": list(names)}]

    def _shell_rules(self, kind: str, command: str) -> list[Any]:
        """A shell command's rules: the templates filled in for settings; for
        the Policy Engine, a word as `commandPrefix` (it adds the word
        boundary itself) and a stem as an escaped `commandRegex`, which the
        engine anchors at the command's start."""
        if self.format == SETTINGS_FORMAT:
            templates = self.shell_word if kind == "word" else self.shell_stem
            return [template.replace("{command}", command) for template in templates]
        if kind == "word":
            return [{"toolName": self.shell_tool, "commandPrefix": command}]
        return [{"toolName": self.shell_tool, "commandRegex": _regex_literal(command)}]

    def _fields(self, stance: str, policy: str) -> dict[str, Any]:
        """The fields every rule of `stance` carries, `{policy}` filled in."""
        fields = dict(dict(self.rule_fields).get(stance, ()))
        return {key: (value.replace("{policy}", policy) if isinstance(value, str)
                      else list(value) if isinstance(value, tuple) else value)
                for key, value in fields.items()}


# What a harness without a policy.mapping maps: nothing — no tools, no shell,
# no modes — so every word renders unmapped.
NO_POLICY_MAPPING = PolicyMapping(format=SETTINGS_FORMAT)


def parse_policy_mapping(tag_dir: Path) -> PolicyMapping | None:
    """`<tag_dir>/policy.mapping` → `PolicyMapping`, or None when the harness
    has none. Every table is checked against its format, every capability
    and mode against the launcher's vocabulary, and a tool may sit under one
    capability only; anything else is a `TagError` naming the file, so a
    mapping cannot quietly render a rule narrower than it reads."""
    path = tag_dir / POLICY_MAPPING_FILE
    if not path.is_file():
        return None
    data = read_toml(path)
    fmt = data.get("format")
    if fmt not in FORMATS:
        raise TagError(f"{path}: format must be one of: {', '.join(FORMATS)} — got {fmt!r}")
    for table in data:
        if table not in _TABLES[fmt]:
            raise TagError(f"{path}: [{table}] is not a table of the {fmt} format — the tables are: "
                           f"{', '.join(t for t in _TABLES[fmt] if t != 'format')}")
    tools = _tools(data.get("tools", {}), path)
    modes = _modes(data.get("mode", {}), path)
    fixed_path = _fixed(data.get("fixed"), fmt, path)
    if fmt == SETTINGS_FORMAT:
        word, stem = _settings_shell(data.get("shell"), path)
        lists = _lists(data.get("lists"), path)
        _check_disjoint(path, [key for _, key in lists], modes)
        return PolicyMapping(format=fmt, tools=tools, shell_word=word, shell_stem=stem, lists=lists, modes=modes,
                             fixed_path=fixed_path)
    file_path, file_args = _file(data.get("file"), path)
    _check_flag_follows_fixed_tier(path, file_args, fixed_path)
    return PolicyMapping(format=fmt, tools=tools, shell_tool=_engine_shell(data.get("shell"), tools, path),
                         file_path=file_path, file_args=file_args,
                         rule_fields=_rule_fields(data.get("rules"), path), modes=modes, fixed_path=fixed_path)


def rules_file_text(sections: list[tuple[str, tuple[dict[str, Any], ...]]], header: str = RULES_FILE_HEADER) -> str:
    """A policy-engine rules file — one instance's by default, or the fixed
    tier's with `FIXED_FILE_HEADER`: the header, then each policy's records
    as `[[rule]]` tables under a comment naming the policy, quoted by
    `launch.toml_emit` like every TOML file the launcher writes (a test
    round-trips the output through tomllib)."""
    lines = [header.rstrip("\n")]
    for policy, records in sections:
        for index, record in enumerate(records):
            lines += ["", f"# {policy}"] if index == 0 else [""]
            lines.append("[[rule]]")
            lines += [f"{toml_emit.key(key)} = {toml_emit.value(value)}" for key, value in record.items()]
    return "\n".join(lines) + "\n"


def _regex_literal(text: str) -> str:
    """`text` as a regular expression matching itself, escaped for the
    JavaScript engine the CLI runs (every escape here is valid there, in any
    flag mode). `rules.py` keeps shell entries to a charset where only `.`
    and `+` need it; the full set is escaped anyway, so widening that charset
    cannot open a rule up."""
    return _JS_REGEX_SPECIAL.sub(lambda match: "\\" + match.group(0), text)


def _set_dotted(target: dict[str, Any], dotted: str, value: Any) -> None:
    """Set `a.b.c` in a nested dict, creating the tables on the way."""
    *parents, leaf = dotted.split(".")
    for part in parents:
        target = target.setdefault(part, {})
    target[leaf] = value


def _check_disjoint(path: Path, list_keys: list[str], modes: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]) -> None:
    """No mode key may sit on or under a rule list's key (or the reverse):
    one policy renders both into the same settings fragment."""
    for mode, pairs in modes:
        for key, _ in pairs:
            for list_key in list_keys:
                if key == list_key or key.startswith(list_key + ".") or list_key.startswith(key + "."):
                    raise TagError(f"{path}: [mode.{mode}] {key!r} collides with the rule list at {list_key!r}")


def _tools(table: Any, path: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(table, dict):
        raise TagError(f"{path}: [tools] must be a table of capability = [tool names]")
    owner: dict[str, str] = {}
    out: list[tuple[str, tuple[str, ...]]] = []
    for capability, names in table.items():
        if capability not in CAPABILITIES:
            raise TagError(f"{path}: [tools] names capability {capability!r} — the capabilities are: {', '.join(CAPABILITIES)}")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n.strip() for n in names):
            raise TagError(f"{path}: [tools] {capability} must be a non-empty list of tool names")
        for name in names:
            if name in owner:
                raise TagError(f"{path}: [tools] {name!r} sits under both {owner[name]} and {capability} — a tool belongs to one capability")
            owner[name] = capability
        out.append((capability, tuple(names)))
    return tuple(out)


def _modes(table: Any, path: Path) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    if not isinstance(table, dict):
        raise TagError(f"{path}: [mode] must hold one [mode.<word>] table per mode")
    out: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for mode, pairs in table.items():
        if mode not in MODES:
            raise TagError(f"{path}: [mode.{mode}] is not a mode — the modes are: {', '.join(MODES)}")
        if not isinstance(pairs, dict) or not pairs or not all(isinstance(v, str) for v in pairs.values()):
            raise TagError(f"{path}: [mode.{mode}] must be a non-empty table of settings key = string")
        out.append((mode, tuple(pairs.items())))
    return tuple(out)


def _settings_shell(table: Any, path: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if table is None:
        return (), ()
    if not isinstance(table, dict) or set(table) != {"word", "stem"}:
        raise TagError(f"{path}: [shell] takes exactly word and stem, each a list of rule templates")
    out = []
    for kind in ("word", "stem"):
        templates = table[kind]
        if not isinstance(templates, list) or not templates or not all(isinstance(t, str) for t in templates):
            raise TagError(f"{path}: [shell] {kind} must be a non-empty list of rule templates")
        for template in templates:
            if "{command}" not in template or re.search(r"\{(?!command\})", template):
                raise TagError(f"{path}: [shell] {kind} template {template!r} must use {{command}} and no other placeholder")
        out.append(tuple(templates))
    return out[0], out[1]


def _lists(table: Any, path: Path) -> tuple[tuple[str, str], ...]:
    if not isinstance(table, dict) or set(table) != set(STANCES) or \
            not all(isinstance(v, str) and v.strip() for v in table.values()):
        raise TagError(f"{path}: [lists] must name the settings key for both allow and deny")
    return tuple((stance, table[stance]) for stance in STANCES)


def _engine_shell(table: Any, tools: tuple[tuple[str, tuple[str, ...]], ...], path: Path) -> str | None:
    if table is None:
        return None
    shell_tools = dict(tools).get("shell", ())
    if not isinstance(table, dict) or set(table) != {"tool"} or table["tool"] not in shell_tools:
        raise TagError(f"{path}: [shell] takes exactly tool, one of [tools] shell ({', '.join(shell_tools) or 'none listed'})")
    return table["tool"]


# Gemini CLI's two flags for a policy file, one word apart and opposite: the
# user-tier one REPLACES the default user dir, the admin-tier one SUPPLEMENTS
# the system dir — and is ignored whenever that dir holds a .toml, which a
# fixed tier puts there (v0.61.0, packages/core/src/policy/config.ts). Which
# one a policy-engine mapping may use is decided by its own [fixed] table,
# because each correct pairing is protected by a DIFFERENT mechanism against
# a trusted workspace's settings.adminPolicyPaths (workspace-settable,
# union-merged, landing at the admin tier — gate fixed-tier's queue thread):
#   [fixed] path + --policy        the populated system dir nulls that array
#   [fixed] none + --admin-policy  argv replaces it (`argv.adminPolicy ?? …`)
# and the two mismatches fail differently (bug-investigator):
#   [fixed] path + --admin-policy  the flag is ignored — injection stays blocked,
#                                  but the file loads by directory read, beside
#                                  the CLI's own auto-saved allows
#   [fixed] none + --policy        NEITHER: the workspace's paths load above ours
_USER_TIER_FLAG = "--policy"
_ADMIN_TIER_FLAG = "--admin-policy"


def _check_flag_follows_fixed_tier(path: Path, args: tuple[str, ...], fixed_path: str | None) -> None:
    """Refuse a policy-engine mapping whose per-instance flag does not
    match its fixed tier — both directions, since both fail silently."""
    if fixed_path is not None and _ADMIN_TIER_FLAG in args:
        raise TagError(f"{path}: [file] args use {_ADMIN_TIER_FLAG}, which Gemini CLI ignores once [fixed] puts a "
                       f".toml in its system policies dir — the file would then load only by directory read, "
                       f"beside the CLI's own auto-saved allows. Pass it by {_USER_TIER_FLAG}")
    if fixed_path is None and _USER_TIER_FLAG in args:
        raise TagError(f"{path}: [file] args use {_USER_TIER_FLAG} with no fixed tier — protected by neither "
                       f"mechanism: no .toml in the system dir nulls a workspace's adminPolicyPaths and no "
                       f"{_ADMIN_TIER_FLAG} on argv replaces it, so its rules would load at the admin tier above "
                       f"these. Pass the file by {_ADMIN_TIER_FLAG}")


def _fixed(table: Any, fmt: str, path: Path) -> str | None:
    """`[fixed]`: exactly one of `path` (an absolute path of the format's
    file type) or `none` (the reason this CLI has no fixed tier)."""
    suffix = _FIXED_SUFFIX[fmt]
    if not isinstance(table, dict) or len(table) != 1 or not set(table) <= {"path", "none"}:
        raise TagError(f"{path}: [fixed] must state the fixed tier — path = an absolute {suffix} path, or "
                       f"none = \"<why this CLI has none>\" — so no harness loses it by omission")
    if "none" in table:
        if not isinstance(table["none"], str) or not table["none"].strip():
            raise TagError(f"{path}: [fixed] none must say why this CLI has no fixed tier")
        return None
    fixed = table["path"]
    pure = PurePosixPath(fixed) if isinstance(fixed, str) else None
    if pure is None or not pure.is_absolute() or ".." in pure.parts or pure.suffix != suffix:
        raise TagError(f"{path}: [fixed] path must be an absolute {suffix} path (the {fmt} format's file)")
    return fixed


def _file(table: Any, path: Path) -> tuple[str, tuple[str, ...]]:
    if not isinstance(table, dict) or set(table) != {"path", "args"}:
        raise TagError(f"{path}: [file] takes exactly path and args")
    relative, args = table["path"], table["args"]
    pure = PurePosixPath(relative) if isinstance(relative, str) else None
    if pure is None or pure.is_absolute() or ".." in pure.parts or pure.suffix != ".toml":
        raise TagError(f"{path}: [file] path must be a .toml path inside the config root, got {relative!r}")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args) or not any("{path}" in a for a in args):
        raise TagError(f"{path}: [file] args must be a list of strings that names the file with {{path}}")
    return relative, tuple(args)


def _rule_fields(table: Any, path: Path) -> tuple[tuple[str, tuple[tuple[str, Any], ...]], ...]:
    if not isinstance(table, dict) or set(table) != set(STANCES):
        raise TagError(f"{path}: [rules] must hold [rules.allow] and [rules.deny]")
    out: list[tuple[str, tuple[tuple[str, Any], ...]]] = []
    priorities: dict[str, int] = {}
    for stance in STANCES:
        fields = table[stance]
        if not isinstance(fields, dict):
            raise TagError(f"{path}: [rules.{stance}] must be a table")
        for key, value in fields.items():
            expected = _RULE_FIELDS.get(key)
            if expected is None:
                aside = " (the decision is the stance's own word)" if key == "decision" else ""
                raise TagError(f"{path}: [rules.{stance}] sets {key!r}{aside} — the fields are: {', '.join(_RULE_FIELDS)}")
            if not isinstance(value, expected) or isinstance(value, bool):
                raise TagError(f"{path}: [rules.{stance}] {key} must be {expected.__name__}")
            if key == "modes" and not (isinstance(value, list) and all(isinstance(m, str) and m for m in value)):
                raise TagError(f"{path}: [rules.{stance}] modes must be a list of mode names")
        priority = fields.get("priority")
        if not isinstance(priority, int) or not 0 <= priority <= _MAX_PRIORITY:
            raise TagError(f"{path}: [rules.{stance}] priority must be an integer from 0 to {_MAX_PRIORITY}")
        priorities[stance] = priority
        out.append((stance, tuple((k, tuple(v) if isinstance(v, list) else v) for k, v in fields.items())))
    if priorities["deny"] <= priorities["allow"]:
        raise TagError(f"{path}: [rules.deny] priority must be above [rules.allow]'s — a deny has to outrank every allow")
    return tuple(out)
