"""Audit the launcher's persistent state for inconsistencies.

Reports:
  - orphan state dirs (instance dir present but no matching agent .md)
  - drifted CLAUDE.md (state dir's CLAUDE.md differs from agent's current .md)
  - no_history (state dir has no history.jsonl — the last-used signal we rely on)
  - ghost workspace-map entries (entry without a corresponding state dir)
  - bad workspaces (mapping points to a non-existent or non-directory path)
  - ws_map issues (file missing, empty, or not valid JSON)
  - oauth issues (.claude.json / .credentials.json missing, empty, or not valid JSON)

Run from the project root:
  python -m launch.audit
"""

import json

from .agents_crud import list_all_instances
from .file_access import (
    find_md_for_agent, is_dir, load_workspace_map, path_exists, read_text,
    rglob_paths,
)
from .paths import (
    ACCOUNT_FILE, AGENT_WORKSPACE_MAP_FILE, AGENTS_STATE, CREDENTIALS_FILE,
    HISTORY_JSONL_FILENAME, instance_state_dir_path, state_md_path,
)
from .structs import InstanceIdentity, SESSION_SEP


def _check_json_file(path):
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


def main():
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

    # Shared OAuth files — these must be populated after login.
    for path in (ACCOUNT_FILE, CREDENTIALS_FILE):
        msg = _check_json_file(path)
        if msg is not None:
            issues.append(("oauth", path.name.lstrip("."), msg))

    instances = list_all_instances()
    actual = set(instances)

    # Per-instance checks — `dir_name` is the `<agent>__<session>` string, not an InstanceIdentity.
    for dir_name in instances:
        agent, _, session = dir_name.partition(SESSION_SEP)
        md_path = find_md_for_agent(agent)
        if md_path is None:
            issues.append(("orphan", dir_name, f"agent '{agent}' has no .md file"))
            continue
        sm = state_md_path(InstanceIdentity.state_dir_for(agent, session))
        if path_exists(sm) and read_text(sm) != read_text(md_path):
            issues.append(("drifted", dir_name, f"CLAUDE.md differs from {md_path.name}"))
        if not list(rglob_paths(instance_state_dir_path(dir_name), HISTORY_JSONL_FILENAME)):
            issues.append(("no_history", dir_name, f"no {HISTORY_JSONL_FILENAME} found (instance never started?)"))

    # Workspace-map entries — same shape: `dir_name` is the map key, a `<agent>__<session>` string.
    for dir_name, ws in mapping.items():
        if dir_name not in actual:
            issues.append(("ghost", dir_name, "workspace-map entry has no state dir"))
            continue
        if not ws or not is_dir(ws):
            issues.append(("badworkspace", dir_name, f"workspace not a directory: {ws}"))

    if not issues:
        print(f"All clear. {len(instances)} instance(s) under {AGENTS_STATE}.")
        return

    width = max(len(kind) for kind, _, _ in issues)
    for kind, target, msg in sorted(issues):
        print(f"  [{kind:<{width}}]  {target}: {msg}")


if __name__ == "__main__":
    main()
