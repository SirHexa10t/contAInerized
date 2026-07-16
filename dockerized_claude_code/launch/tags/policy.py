"""Policy kind — `< >` — "what's PERMITTED".

A policy is exactly a Claude Code `settings.json` fragment and nothing more
(the classifier law: needs-only-a-fragment ⇒ policy; needs-anything-else ⇒
specialty). Defined by `agents/policy/<name>/` with a `tag.info`
(description, shortname, risk_level) and a `policy.json` (the fragment).

At launch the selected policies' fragments deep-merge with the shared
settings template into a per-instance `settings.json`, bind-mounted
read-only so the agent can't relax its own leash. `merge_fragments` is that
pure merge (built + tested now, consumed in P2): dicts recurse, lists
concatenate + dedupe, scalar conflicts abort loudly (silent last-wins would
make policy combinations order-dependent).
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from .base import Tag, TagError, common_fields, is_hidden_asset_dir

POLICY_FILE = "policy.json"


@dataclass(frozen=True)
class Policy(Tag):
    parentheses: ClassVar[tuple[str, str]] = ("<", ">")
    root: ClassVar[str] = "policy"
    nutshell: ClassVar[str] = "what's PERMITTED"

    risk_level: int = 0

    def load_fragment(self) -> dict[str, Any]:
        """(Re)read this policy's `policy.json`. Validated at scan time, so
        this is safe to call in the launch path; kept as a method rather than
        a stored dict to keep the frozen record hashable."""
        return _read_fragment(self.path / POLICY_FILE)

    @classmethod
    def scan(cls, agents_dir: Path) -> list["Policy"]:
        """Discover every policy (a dir with `tag.info` directly under
        `agents/policy/`). Each must carry a `policy.json` that parses to a
        JSON object — validated here so a broken fragment fails at startup,
        not mid-launch. `risk_level` comes from `tag.info` (default 0)."""
        root = agents_dir / cls.root
        out: list[Policy] = []
        if not root.is_dir():
            return out
        for tag_dir in sorted(root.iterdir(), key=lambda p: p.name):
            if not tag_dir.is_dir() or is_hidden_asset_dir(tag_dir):
                continue   # `_`-dir → skip; a stray non-tag dir raises inside is_hidden_asset_dir
            fields = common_fields(tag_dir)
            info = fields.pop("_info")
            _read_fragment(tag_dir / POLICY_FILE)   # validate now; discard
            out.append(cls(**fields, risk_level=int(info.get("risk_level", 0))))
        return out


def _read_fragment(path: Path) -> dict[str, Any]:
    """Parse a `policy.json`; must exist and be a JSON object. Fail loud
    (TagError naming the path) otherwise."""
    if not path.is_file():
        raise TagError(f"{path.parent}: policy is missing {POLICY_FILE}")
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise TagError(f"{path}: invalid JSON ({e})") from e
    if not isinstance(data, dict):
        raise TagError(f"{path}: policy fragment must be a JSON object, got {type(data).__name__}")
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
            if key not in dst:
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
