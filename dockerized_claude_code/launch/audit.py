"""Audit the launcher's persistent state for inconsistencies.

Reports:
  - orphan state dirs (instance dir present but no matching agent .md)
  - no_history (state dir has no history.jsonl — the last-used signal we rely on)
  - ghost workspace-map entries (entry without a corresponding state dir)
  - bad workspaces (mapping points to a non-existent or non-directory path)
  - ws_map issues (workspace-map file missing, empty, or not valid JSON)
  - ghost_mode entries (modes-map entry without a corresponding state dir)
  - empty_modes_entry (modes-map entry whose list is empty — _write_modes_entry
                       refuses to persist these; presence implies a bypassed
                       write path)
  - bad_mode entries (modes-map value isn't a list, or contains a mode string
                      not in InstanceModifiers.mode_values())
  - modes_map issues (modes-map file missing or not valid JSON)
  - oauth issues (.claude.json / .credentials.json missing, empty, or not valid JSON)

Run from the project root:
  python -m launch.audit
"""

import json
from pathlib import Path
from typing import Any

from .agents_crud import list_all_instances
from .file_access import agent_md_index, is_dir, path_exists, read_text
from .paths import (
    ACCOUNT_FILE, AGENT_MODES_MAP_FILE, AGENT_WORKSPACE_MAP_FILE,
    AGENTS_STATE, CREDENTIALS_FILE, instance_state_dir_path, state_history_path,
)
from .structs import InstanceModifiers, SESSION_SEP


def _load_or_issue(kind: str, path: Path) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """Parse the JSON map at `path`; return (mapping, issues). On missing file
    or invalid JSON, return ({}, [single issue tagged with `kind`]); an empty
    file parses as {} (matching the launcher's semantics). `kind` is the issue
    category ('ws_map' / 'modes_map') reported on failure. Parses directly
    rather than through file_access's cached loaders — those sys.exit on
    corruption (fail-fast is right for a launch), while the audit's job is to
    report the same corruption non-fatally and keep checking."""
    if not path_exists(path):
        return {}, [(kind, path.name, "file is missing")]
    content = read_text(path).strip()
    try:
        return (json.loads(content) if content else {}), []
    except json.JSONDecodeError as e:
        return {}, [(kind, path.name, f"invalid JSON: {e}")]


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


def _modes_map_issues(modes: dict[str, Any], actual: set[str]) -> list[tuple[str, str, str]]:
    """Per-entry findings for the modes map, returned as (kind, dir_name, msg)
    tuples. Symmetric to the workspace-map loop in main() — extracted as a
    helper so each finding kind has direct unit-test coverage. Covers:
      ghost_mode           — entry whose instance has no state dir
      bad_mode (non-list)  — value isn't a JSON list (corrupted/hand-edited)
      empty_modes_entry    — list is empty, violating _write_modes_entry's invariant
      bad_mode (unknown)   — list element isn't in InstanceModifiers.mode_values()"""
    known = set(InstanceModifiers.mode_values())
    out = []
    for dir_name, mode_list in modes.items():
        if dir_name not in actual:
            out.append(("ghost_mode", dir_name, "modes-map entry has no state dir"))
            continue
        if not isinstance(mode_list, list):
            out.append(("bad_mode", dir_name, f"value is not a list: {mode_list!r}"))
            continue
        if not mode_list:
            out.append(("empty_modes_entry", dir_name, "modes-map entry has an empty list"))
            continue
        for mode in mode_list:
            if mode not in known:
                out.append(("bad_mode", dir_name, f"unknown mode '{mode}'"))
    return out


def main() -> None:
    issues: list[tuple[str, str, str]] = []

    # Map files may legitimately be missing on a fresh install — _load_or_issue
    # reports + degrades to {} so the per-entry checks below still run cleanly.
    mapping, ws_issues = _load_or_issue("ws_map", AGENT_WORKSPACE_MAP_FILE)
    issues.extend(ws_issues)
    modes, modes_issues = _load_or_issue("modes_map", AGENT_MODES_MAP_FILE)
    issues.extend(modes_issues)

    # Shared OAuth files — these must be populated after login.
    for path in (ACCOUNT_FILE, CREDENTIALS_FILE):
        msg = _check_json_file(path)
        if msg is not None:
            issues.append(("oauth", path.name.lstrip("."), msg))

    instances = list_all_instances()
    actual = set(instances)

    # Per-instance checks — `dir_name` is the `<agent>__<session>` string, not an InstanceIdentity.
    # CLAUDE.md drift isn't checked: install_latest_md fully rewrites it every launch
    # (source `.md` + composed-addendum), so any pre-launch divergence is transient.
    for dir_name in instances:
        agent, _, session = dir_name.partition(SESSION_SEP)
        if agent not in agent_md_index():
            issues.append(("orphan", dir_name, f"agent '{agent}' has no .md file"))
            continue
        if not state_history_path(instance_state_dir_path(dir_name)).is_file():
            issues.append(("no_history", dir_name, "no history.jsonl found (instance never started?)"))

    # Workspace-map entries — same shape: `dir_name` is the map key, a `<agent>__<session>` string.
    for dir_name, ws in mapping.items():
        if dir_name not in actual:
            issues.append(("ghost", dir_name, "workspace-map entry has no state dir"))
            continue
        if not ws or not is_dir(ws):
            issues.append(("badworkspace", dir_name, f"workspace not a directory: {ws}"))

    # Modes-map entries — symmetric to the workspace-map loop above, but with
    # additional shape/value validation since modes_map values are lists rather
    # than plain strings.
    issues.extend(_modes_map_issues(modes, actual))

    if not issues:
        print(f"All clear. {len(instances)} instance(s) under {AGENTS_STATE}.")
        return

    width = max(len(kind) for kind, _, _ in issues)
    for kind, target, msg in sorted(issues):
        print(f"  [{kind:<{width}}]  {target}: {msg}")


if __name__ == "__main__":
    main()
