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

from .agents_crud import AGENT_MD_BY_NAME, list_all_instances
from .file_access import (
    is_dir, load_modes_map, load_workspace_map, path_exists, read_text,
)
from .paths import (
    ACCOUNT_FILE, AGENT_MODES_MAP_FILE, AGENT_WORKSPACE_MAP_FILE,
    AGENTS_STATE, CREDENTIALS_FILE, instance_state_dir_path, state_history_path,
)
from .structs import InstanceModifiers, SESSION_SEP


def _check_json_file(path) -> str | None:
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


def _modes_map_issues(modes: dict, actual: set) -> list[tuple[str, str, str]]:
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
    issues = []

    # Workspace map file (file may legitimately not exist on a fresh install — still report it).
    if not path_exists(AGENT_WORKSPACE_MAP_FILE):
        issues.append(("ws_map", AGENT_WORKSPACE_MAP_FILE.name, "file is missing"))
        mapping = {}
    else:
        try:
            mapping = load_workspace_map()
        except json.JSONDecodeError as e:
            issues.append(("ws_map", AGENT_WORKSPACE_MAP_FILE.name, f"invalid JSON: {e}"))
            mapping = {}

    # Modes map file (parallel to workspace map; entries are optional per-instance,
    # but a missing or corrupt file is still surfaced).
    if not path_exists(AGENT_MODES_MAP_FILE):
        issues.append(("modes_map", AGENT_MODES_MAP_FILE.name, "file is missing"))
        modes = {}
    else:
        try:
            modes = load_modes_map()
        except json.JSONDecodeError as e:
            issues.append(("modes_map", AGENT_MODES_MAP_FILE.name, f"invalid JSON: {e}"))
            modes = {}

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
        if agent not in AGENT_MD_BY_NAME:
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
