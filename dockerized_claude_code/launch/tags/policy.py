"""Policy kind — `< >` — "what's PERMITTED".

A policy is exactly a set of RULES in the launcher's own words and nothing
more (the classifier law: needs-only-rules ⇒ policy; needs-anything-else — a
hook, a mount, a CLI flag — ⇒ specialty). Defined by `agents/policy/<name>/`
with a `tag.info` (descriptions, shortname, stance) and a `tag.rules`
(`tags/rules.py`: what it allows, denies or demands, named by capability,
shell command and mode — never by one CLI's tool names). Until 2026-09-26 the
second file was a Claude Code settings fragment (`tags/migrations.py` refuses
one left behind); each harness's `policy.mapping` now decides what the words
become (`Harness.render_policy`).

At launch the selected policies render through the instance's harness: for
Claude Code into settings fragments that deep-merge with the shared settings
template into a per-instance `settings.json`, for Gemini CLI into a rules
file of its own — each bind-mounted read-only so the agent can't act outside
given limits. `merge_fragments` is the settings merge: dicts recurse, lists
concatenate + dedupe, scalar conflicts abort loudly (silent last-wins would
make policy combinations order-dependent).

A `_<name>` dir under `agents/policy/` is a **hidden fragment** — not an
offered policy, but rules a same-named *specialty* claims as part of its own
contribution (the policy-tree analogue of the profession tree's `_<name>`
image layers). It holds a `tag.rules`, and/or RAW settings for one harness as
`<harness>.json`: the escape hatch for what the words cannot say yet (`_cowork`'s
Stop hook, `_cluster-cowork`'s prompt hook), which refuses on any other
harness. `discover_fragments` finds them; `Specialty.scan` claims them.
That's how `{ro}` bundles a hard `/workspace` `:ro` mount with the soft
write-tool deny in one tag.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from .base import HIDDEN_PREFIX, INFO_FILE, Tag, TagError, common_fields, read_toml, walk_tag_tree
from .migrations import refuse_retired_policy_file
from .rules import RULES_FILE, Rules, parse_rules

RAW_SUFFIX = ".json"               # a hidden fragment's raw settings for one harness: `<harness key>.json`


class PolicyStance(Enum):
    """Which way a policy moves the leash — drives its color everywhere:
      ALLOW      (orange)     — grants ability, loosens the sandbox
      DENY       (blue)       — restricts, tightens the sandbox
      DEMAND     (bold white)  — mandates a behavior (e.g. start in plan mode)
    The tag.info key is `stance = "allow" | "deny" | "demand"`."""
    ALLOW = "allow"
    DENY = "deny"
    DEMAND = "demand"


@dataclass(frozen=True)
class Policy(Tag):
    parentheses: ClassVar[tuple[str, str]] = ("<", ">")
    root: ClassVar[str] = "policy"
    nutshell: ClassVar[str] = "what's PERMITTED"

    stance: PolicyStance = PolicyStance.ALLOW
    always_on: bool = False   # static tag: applied to EVERY instance; shown locked in the form; never listed in .lego / instances.toml
    rules: Rules = Rules()    # its tag.rules, parsed and checked at scan

    @classmethod
    def discover_fragments(cls, agents_dir: Path) -> dict[str, "PolicyFragment"]:
        """Find every `_<name>` hidden fragment dir under `agents/policy/` and
        return `{name: PolicyFragment}` (underscore stripped). Each is a
        specialty-owned fragment: it holds a `tag.rules`, one or more raw
        `<harness>.json` settings, or both, nothing else, and NO `tag.info`
        (it's an asset, not an offered tag). Which harnesses the raw files
        name is the registry's check. The profession-tree `_<name>` layers are
        the image-side twin of this."""
        root = agents_dir / cls.root
        out: dict[str, PolicyFragment] = {}
        if not root.is_dir():
            return out
        for d in sorted(root.iterdir(), key=lambda p: p.name):
            if not d.is_dir() or not d.name.startswith(HIDDEN_PREFIX):
                continue
            if (d / INFO_FILE).is_file():
                raise TagError(f"{d}: a hidden policy fragment ('_'-prefixed) must not contain {INFO_FILE}")
            refuse_retired_policy_file(d)
            raw: list[str] = []
            for f in sorted(d.iterdir(), key=lambda p: p.name):
                if f.name == RULES_FILE:
                    continue
                if not f.is_file() or f.suffix != RAW_SUFFIX:
                    raise TagError(f"{f}: a hidden policy fragment holds only {RULES_FILE} and <harness>{RAW_SUFFIX} files")
                read_fragment(f)   # validate now; discard
                raw.append(f.stem)
            rules = read_rules(d / RULES_FILE) if (d / RULES_FILE).is_file() else None
            if rules is None and not raw:
                raise TagError(f"{d}: a hidden policy fragment needs a {RULES_FILE} or a <harness>{RAW_SUFFIX}")
            name = d.name[len(HIDDEN_PREFIX):]
            out[name] = PolicyFragment(name=name, path=d, rules=rules, raw_harnesses=tuple(raw))
        return out

    @classmethod
    def scan(cls, agents_dir: Path) -> list["Policy"]:
        """Discover every policy — a dir with `tag.info` under `agents/policy/`,
        nested like specialties: a policy inside another's dir requires it
        (`vcs-safe/no-git-write/` ⇒ `<-gw>` requires `<-gpush>`, operator
        2026-09-28), so the form's check-cascade brings the parent along; an
        always-on policy cannot nest (it would require a parent an instance
        may not carry — the registry refuses it). Each must carry a `tag.rules` whose one table is
        the one its stance names — `[deny]` for a deny policy, and so on —
        since the stance is what colours it everywhere and a policy that
        says one thing while doing another would be coloured wrong; checked
        here so a broken file fails at startup, not mid-launch. `stance`
        comes from `tag.info` (default "allow" — granting is the common
        case); an unrecognized value is a TagError. `always_on = true` marks
        a STATIC tag — applied to every instance unconditionally
        (agents_crud.install_settings) and baked into each CLI's fixed tier
        (agents_crud.fixed_policy), rendered locked in the form, and
        rejected/dropped if a .lego or store entry lists it
        (registry.validate_build / resolve_store_build); it must be a deny."""
        out: list[Policy] = []
        for tag_dir, ancestors in walk_tag_tree(agents_dir / cls.root):   # `_`-dirs skipped; a stray non-tag dir raises
            fields = common_fields(tag_dir)
            info = fields.pop("_info")
            refuse_retired_policy_file(tag_dir)
            raw_stance = info.get("stance", PolicyStance.ALLOW.value)
            try:
                stance = PolicyStance(raw_stance)
            except ValueError:
                raise TagError(
                    f"{tag_dir}/tag.info: stance must be one of "
                    f"{[s.value for s in PolicyStance]}, got {raw_stance!r}"
                ) from None
            rules_path = tag_dir / RULES_FILE
            if not rules_path.is_file():
                raise TagError(f"{tag_dir}: policy is missing {RULES_FILE}")
            rules = read_rules(rules_path)
            if rules.tables != (stance.value,):
                raise TagError(f"{rules_path}: holds [{'], ['.join(rules.tables)}] but the policy's stance is "
                               f"{stance.value!r} — its rules must be exactly the [{stance.value}] table")
            always_on = bool(info.get("always_on", False))
            if always_on and stance is not PolicyStance.DENY:
                # One check is enough: the line above already holds a deny
                # policy's rules to exactly the [deny] table. Hidden fragments
                # have no always_on, so they never reach the fixed tier either.
                raise TagError(f"{tag_dir}/tag.info: always_on = true needs stance = \"deny\" — an always-on "
                               f"policy is baked into each CLI's fixed tier, where an allow would outrank every "
                               f"per-instance deny (Gemini CLI: admin tier 5.x over user tier 4.x) and a demand "
                               f"could not differ per cluster member (gate fixed-tier, 2026-09-26)")
            out.append(cls(**fields, requires=frozenset(ancestors), stance=stance, always_on=always_on, rules=rules))
        return out


@dataclass(frozen=True)
class PolicyFragment:
    """A hidden `_<name>` fragment, as the specialty of the same name claims
    it: its rules in the launcher's words (None without a tag.rules) and the
    harnesses it carries RAW settings for — hooks and modes the words cannot
    say yet, each valid on its own harness only."""
    name: str
    path: Path
    rules: Rules | None = None
    raw_harnesses: tuple[str, ...] = ()

    def raw_fragment(self, harness: str) -> dict[str, Any] | None:
        """The raw settings this fragment carries for `harness`, or None when
        it carries none for it. Validated at scan time, so safe in a launch."""
        return read_fragment(self.path / f"{harness}{RAW_SUFFIX}") if harness in self.raw_harnesses else None


def read_rules(path: Path) -> Rules:
    """Parse and check a `tag.rules` (`tags/rules.py`)."""
    return parse_rules(read_toml(path), path)


def read_fragment(path: Path) -> dict[str, Any]:
    """Parse a raw `<harness>.json` settings fragment; must be a JSON object.
    Fail loud (TagError naming the path) otherwise."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise TagError(f"{path}: invalid JSON ({e})") from e
    if not isinstance(data, dict):
        raise TagError(f"{path}: a settings fragment must be a JSON object, got {type(data).__name__}")
    return data



def merge_fragments(items: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Deep-merge named policy fragments into one settings dict, order given.

    Rules: nested objects recurse; lists concatenate then dedupe (order-
    preserving — the `permissions.allow`/`deny` case); equal scalars coexist;
    a **scalar conflict** (or a shape clash, dict-vs-not) raises `TagError`
    naming both contributing policies and the conflicting key path. `items`
    is (policy-name, fragment) pairs so the error can name culprits."""
    result: dict[str, Any] = {}
    owner: dict[tuple[str, ...], str] = {}   # keypath → policy that last set a scalar/list there

    def merge_into(dst: dict[str, Any], src: dict[str, Any], name: str, path: tuple[str, ...]) -> None:
        for key, value in src.items():
            here = path + (key,)
            if key not in dst and isinstance(value, dict):
                # Recursed into rather than copied whole, so every key inside
                # gets its owner: a later conflict at `permissions.defaultMode`
                # must name the policy that set it, not "?".
                dst[key] = {}
                merge_into(dst[key], value, name, here)
            elif key not in dst:
                dst[key] = deepcopy(value)
                owner[here] = name
            elif isinstance(dst[key], dict) and isinstance(value, dict):
                merge_into(dst[key], value, name, here)
            elif isinstance(dst[key], list) and isinstance(value, list):
                merged = list(dst[key])
                merged.extend(v for v in value if v not in merged)
                dst[key] = merged
                owner[here] = name
            elif dst[key] == value:
                pass   # identical scalar/value — no conflict
            else:
                prior = owner.get(here, "?")
                dotted = ".".join(here)
                raise TagError(
                    f"policy conflict at '{dotted}': <{prior}> sets {dst[key]!r}, "
                    f"<{name}> sets {value!r} — resolve by dropping one policy"
                )

    for policy_name, fragment in items:
        merge_into(result, fragment, policy_name, ())
    return result
