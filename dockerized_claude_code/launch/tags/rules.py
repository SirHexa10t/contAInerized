"""The policy RULES — what a policy allows, denies or demands, in the
launcher's OWN words, read from `agents/policy/<name>/tag.rules` (TOML). No
harness's names appear here: `tools` names CAPABILITIES, `shell` and
`shell_stems` name commands, `mode` names a session mode, and each harness's
`policy.mapping` translates all of it into that CLI's own permission surface
(`tags/harness.py`, `Harness.render_policy`). The policy twin of an engine's
`tag.budget` and a harness's `engine.mapping` (operator, 2026-09-26): a policy
author never learns a CLI's tool names, and adding a harness never touches a
policy.

A file holds the one table its policy's stance names (a hidden fragment,
which has no stance, may hold several):

    [allow]                        [deny]                        [demand]
    tools = ["web"]                tools = ["write"]             mode = "plan"
    shell = ["git status"]         shell = ["sudo"]
    shell_stems = ["make"]         shell_stems = ["git push"]
    all = true    # [allow] only

A shell entry is a WORD or a STEM, and the difference is deliberate:
  shell        a command as a word: `git` matches `git` and `git status`,
               never `gitk`;
  shell_stems  anything that starts with it: `git push` also matches
               `git pushall`, an alias, which is why vcs-safe denies stems.
Both CLIs the launcher has read draw the same line (Claude Code's `Bash(x *)`
against `Bash(x*)`; Gemini CLI's `commandPrefix` against a `commandRegex`),
and the hand-written fragments drew it before this file existed: rendering
words where they wrote stems would have narrowed a deny (gate policy-mapping,
2026-09-26).

`all = true` allows every tool a harness maps, whatever capabilities exist
by then, so `all-actions` cannot fall behind the vocabulary the way a copied
word list would (agent-writer, same gate). It is an [allow] word only.

Leaf within the tag package: imports `base` only, like `budget.py`, so
`policy.py` (which owns the file) and `harness.py` (which translates it) can
both import it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import TagError

RULES_FILE = "tag.rules"

# The capabilities a policy names tools by. Each harness's policy.mapping
# lists the tools that make up each one on that CLI, and a capability means
# exactly those tools — a tool the mapping does not list is outside every
# rule (plans/ISSUES.md, "a capability is only as wide as its mapping").
# Order is the order a rendering lists them in.
CAPABILITIES = ("shell", "web", "read", "write", "subagents")

# The session modes a policy may demand. A word, not a switch: the tree
# already uses a second one (`_cowork`'s dontAsk, a raw Claude Code fragment
# until cowork turns portable), and it joins this list then.
MODES = ("plan",)

# The tables a tag.rules may hold — one per stance, named as PolicyStance's
# values are, so a policy's table and its stance can be compared directly.
TABLES = ("allow", "deny", "demand")
ACTION_KEYS = ("tools", "shell", "shell_stems")
ALL_KEY = "all"

# What a shell word or stem may be made of: the characters of a command
# line's leading words, single-spaced. Narrow on purpose: quotes, globs,
# parentheses and backslashes each mean something different to each CLI's
# matcher, and a rule whose reach nobody can predict is worse than none.
_SHELL_WORDS = re.compile(r"[A-Za-z0-9_./=+,@%-]+(?: [A-Za-z0-9_./=+,@%-]+)*")


@dataclass(frozen=True)
class Actions:
    """What one [allow] or [deny] table names: capabilities (`tools`), shell
    commands as words and as stems, or, in [allow] only, every tool (`all`).
    Empty when the file has no such table."""
    tools: tuple[str, ...] = ()
    shell: tuple[str, ...] = ()
    shell_stems: tuple[str, ...] = ()
    all: bool = False

    @property
    def empty(self) -> bool:
        return not (self.all or self.tools or self.shell or self.shell_stems)


@dataclass(frozen=True)
class Rules:
    """One policy's rules: what it allows, what it denies, and the session
    mode it demands (None for none)."""
    allow: Actions = Actions()
    deny: Actions = Actions()
    mode: str | None = None

    @property
    def tables(self) -> tuple[str, ...]:
        """The tables this file holds, in TABLES order — what the scan
        compares with a policy's stance."""
        held = {"allow": not self.allow.empty, "deny": not self.deny.empty, "demand": self.mode is not None}
        return tuple(table for table in TABLES if held[table])


def parse_rules(data: dict[str, Any], path: Path) -> Rules:
    """A `tag.rules` mapping → `Rules`, checked word by word: tables from
    TABLES, keys from ACTION_KEYS (plus `all` in [allow]), capabilities from
    CAPABILITIES, modes from MODES, shell entries single-spaced command words.
    Anything else is a `TagError` naming the file, so a typo cannot render a
    rule that silently covers nothing."""
    for table in data:
        if table not in TABLES:
            raise TagError(f"{path}: unknown table [{table}] — the tables are: {', '.join(TABLES)}")
    rules = Rules(allow=_actions(data, "allow", path), deny=_actions(data, "deny", path),
                  mode=_demanded_mode(data, path))
    if not rules.tables:
        raise TagError(f"{path}: names no rule — give it an [allow], [deny] or [demand] table")
    return rules


def _actions(data: dict[str, Any], table: str, path: Path) -> Actions:
    """One [allow] / [deny] table, or empty Actions when the file has none."""
    raw = data.get(table)
    if raw is None:
        return Actions()
    if not isinstance(raw, dict):
        raise TagError(f"{path}: [{table}] must be a table")
    known = (*ACTION_KEYS, ALL_KEY) if table == "allow" else ACTION_KEYS
    for key in raw:
        if key not in known:
            aside = " (`all` is an [allow] word)" if key == ALL_KEY else ""
            raise TagError(f"{path}: [{table}] has unknown key {key!r}{aside} — the words are: {', '.join(known)}")
    if ALL_KEY in raw:
        if raw[ALL_KEY] is not True:
            raise TagError(f"{path}: [allow] {ALL_KEY} takes only true")
        others = [key for key in raw if key != ALL_KEY]
        if others:
            raise TagError(f"{path}: [allow] {ALL_KEY} = true already allows every tool — drop {', '.join(others)}")
        return Actions(all=True)
    tools, shell, stems = (_word_list(raw, key, table, path) for key in ACTION_KEYS)
    for word in tools:
        if word not in CAPABILITIES:
            raise TagError(f"{path}: [{table}] tools names {word!r} — the capabilities are: {', '.join(CAPABILITIES)}")
    for key, words in (("shell", shell), ("shell_stems", stems)):
        for word in words:
            if not _SHELL_WORDS.fullmatch(word):
                raise TagError(f"{path}: [{table}] {key} entry {word!r} must be command words, single-spaced, "
                               f"made of letters, digits and _ . / = + , @ % -")
    if both := sorted(set(shell) & set(stems)):
        raise TagError(f"{path}: [{table}] {both[0]!r} is both a shell word and a stem — the stem already covers the word")
    actions = Actions(tools=tools, shell=shell, shell_stems=stems)
    if actions.empty:
        raise TagError(f"{path}: [{table}] names nothing — give it tools, shell or shell_stems")
    return actions


def _word_list(raw: dict[str, Any], key: str, table: str, path: Path) -> tuple[str, ...]:
    """A list of distinct non-empty strings under `key`, or () when absent."""
    words = raw.get(key, [])
    if not isinstance(words, list) or not all(isinstance(w, str) and w for w in words):
        raise TagError(f"{path}: [{table}] {key} must be a list of non-empty strings")
    if twice := sorted({w for w in words if words.count(w) > 1}):
        raise TagError(f"{path}: [{table}] {key} names {twice[0]!r} twice")
    return tuple(words)


def _demanded_mode(data: dict[str, Any], path: Path) -> str | None:
    """The [demand] table's mode, or None when the file has no such table."""
    raw = data.get("demand")
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"mode"}:
        raise TagError(f"{path}: [demand] takes exactly one key, mode")
    if raw["mode"] not in MODES:
        raise TagError(f"{path}: [demand] mode must be one of: {', '.join(MODES)} — got {raw['mode']!r}")
    return raw["mode"]
