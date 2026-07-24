"""Audit the launcher's persistent state for inconsistencies.

Reports:
  - tags issues (the agents/ tag tree itself fails to scan — malformed
    tag.info, strict-rule violations, dangling requires; reported once,
    non-fatally, and per-entry tag validation is skipped)
  - stray root instances (a `<agent>__<session>` dir still at the old
    ~/.claude-agents/ root — instances now live under instances/, so the
    launcher no longer sees one left at the root)
  - orphan state dirs (instance dir present but no matching agent .md)
  - no_history (state dir has no history.jsonl — the last-used signal we rely on)
  - ghost store entries (instances.toml entry without a corresponding state dir)
  - badworkspace (entry's workspace points to a non-existent or non-directory path)
  - bad_tags (entry references an engine/profession/specialty/policy that the
    tag tree doesn't define, or puts a name on the wrong axis)
  - store issues (instances.toml not valid TOML; a MISSING file is fine —
    instances then run on their agents' `.lego` defaults)
  - oauth issues (.claude.json / .credentials.json missing, empty, or not valid JSON)

Run from the project root:
  python -m launch.audit
"""

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any

from .agents_crud import list_all_instances
from .file_access import agent_md_index, is_dir, iter_subdirs, path_exists, read_text
from .paths import (
    ACCOUNT_FILE, AGENTS_DIR, AGENTS_STATE, CREDENTIALS_FILE, INSTANCES_FILE,
    instance_state_dir_path, state_history_path,
)
from .tags import Registry, TagError, scan_all
from .tags.identity import SESSION_SEP
from .tags.store import entry_to_build

Issue = tuple[str, str, str]   # (kind, target, message)


def _stray_root_instances(state_root: Path) -> list[Issue]:
    """Instance dirs still sitting at the ~/.claude-agents/ ROOT — instances
    now live under instances/, and the launcher only looks there, so a
    `<agent>__<session>` dir left at the root is silently ignored (its history
    and tags are invisible). Report each so the user relocates it. The
    `SESSION_SEP in name` filter is the same one list_all_instances uses, so
    the sibling root dirs (cache/, cdn_ranges/, user_extras/, instances/
    itself) are naturally skipped."""
    return [("stray", d.name, "instance dir at the ~/.claude-agents/ root — move it into instances/")
            for d in sorted(iter_subdirs(state_root), key=lambda p: p.name)
            if SESSION_SEP in d.name]


def _load_store(path: Path) -> tuple[dict[str, Any], list[Issue]]:
    """Parse instances.toml; return (mapping, issues). Missing file → ({}, [])
    — a fresh install or a defaults-only setup is legitimate. Invalid TOML is
    reported non-fatally and degrades to {} so the other checks still run."""
    if not path_exists(path):
        return {}, []
    content = read_text(path).strip()
    try:
        return (tomllib.loads(content) if content else {}), []
    except tomllib.TOMLDecodeError as e:
        return {}, [("store", path.name, f"invalid TOML: {e}")]


def _check_json_file(path: Path) -> str | None:
    """Return an issue string if the file is missing, empty, has invalid JSON, or holds an
    empty object/array; None otherwise."""
    if not path_exists(path):
        return "file is missing"
    text = read_text(path).strip()
    if not text:
        return "file is empty"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return f"invalid JSON: {e}"
    if not data:
        return "contents are an empty object"
    return None


def _store_entry_issues(entries: dict[str, Any], actual: set[str],
                        registry: Registry | None) -> list[Issue]:
    """Per-entry findings for instances.toml, as (kind, instance_id, msg)
    tuples. Extracted so each finding kind has direct unit-test coverage:
      ghost        — entry whose instance has no state dir
      badworkspace — workspace missing/None or not a directory on disk
      bad_tags     — axis references the tag tree can't resolve (unknown name
                     or wrong axis), caught via the same validate_build the
                     launcher itself uses; skipped when the tree failed to
                     scan (`registry` is None) — the 'tags' issue covers it."""
    out: list[Issue] = []
    for instance_id, entry in entries.items():
        if instance_id not in actual:
            out.append(("ghost", instance_id, "instances.toml entry has no state dir"))
            continue
        ws = entry.get("workspace")
        if not ws or not is_dir(ws):
            out.append(("badworkspace", instance_id, f"workspace not a directory: {ws}"))
        if registry is not None:
            try:
                registry.validate_build(entry_to_build(entry), f"instances.toml[{instance_id}]")
            except TagError as e:
                out.append(("bad_tags", instance_id, str(e)))
    return out


def build_parser() -> argparse.ArgumentParser:
    """The audit CLI. It takes no arguments of its own — the parser exists so
    `-h`/`--help` prints this module's docstring (the full list of checks and
    how to run it) instead of the audit silently ignoring the flag. Using
    `__doc__` as the description keeps that help in one place. Split from main()
    so the help text is unit-testable without running a scan."""
    return argparse.ArgumentParser(
        prog="python -m launch.audit",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def main() -> None:
    build_parser().parse_args()   # no args of our own; this is what serves -h/--help
    issues: list[Issue] = []

    # The tag tree is the taxonomy every entry validates against; if it fails
    # to scan, report once and skip per-entry tag checks (they'd all fail with
    # the same root cause).
    registry: Registry | None
    try:
        registry = scan_all(AGENTS_DIR)
    except TagError as e:
        registry = None
        issues.append(("tags", AGENTS_DIR.name, str(e)))

    entries, store_issues = _load_store(INSTANCES_FILE)
    issues.extend(store_issues)

    # Shared OAuth files — these must be populated after login.
    for path in (ACCOUNT_FILE, CREDENTIALS_FILE):
        msg = _check_json_file(path)
        if msg is not None:
            issues.append(("oauth", path.name.lstrip("."), msg))

    issues.extend(_stray_root_instances(AGENTS_STATE))

    instances = list_all_instances()
    actual = set(instances)

    # Per-instance checks — `dir_name` is the `<agent>__<session>` string.
    # CLAUDE.md drift isn't checked: install_latest_md fully rewrites it every
    # launch (source `.md` + composed addendum), so any divergence is transient.
    for dir_name in instances:
        agent, _, session = dir_name.partition(SESSION_SEP)
        if agent not in agent_md_index():
            issues.append(("orphan", dir_name, f"agent '{agent}' has no .md file"))
            continue
        if not state_history_path(instance_state_dir_path(dir_name)).is_file():
            issues.append(("no_history", dir_name, "no history.jsonl found (instance never started?)"))

    issues.extend(_store_entry_issues(entries, actual, registry))

    if not issues:
        print(f"All clear. {len(instances)} instance(s) under {AGENTS_STATE}.")
        return

    width = max(len(kind) for kind, _, _ in issues)
    for kind, target, msg in sorted(issues):
        print(f"  [{kind:<{width}}]  {target}: {msg}")


if __name__ == "__main__":
    main()
