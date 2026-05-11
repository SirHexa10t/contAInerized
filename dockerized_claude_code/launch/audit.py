"""Audit the launcher's persistent state for inconsistencies.

Reports:
  - orphan state dirs (instance dir present but no matching agent .md)
  - drifted CLAUDE.md (state dir's CLAUDE.md differs from agent's current .md)
  - no_history (state dir has no history.jsonl — the last-used signal we rely on)
  - ghost workspace-map entries (entry without a corresponding state dir)
  - bad workspaces (mapping points to a non-existent or non-directory path)
  - ws_map issues (file missing, empty, or not valid JSON)
  - oauth issues (.claude.json / .credentials.json missing, empty, or not valid JSON)

Run from the project root either way:
  python -m launch.audit
  python launch/audit.py
"""

import json
import sys
from pathlib import Path

# Make project root importable so `from launch.agents_crud …` resolves whether we're invoked
# as a module (`python -m launch.audit`) or as a script (`python launch/audit.py`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launch.agent_composition import find_md_for_agent
from launch.agents_crud import (
    SESSION_SEP,
    list_all_instances, load_workspace_map, state_md,
)
from launch.paths import ACCOUNT_FILE, AGENT_WORKSPACE_MAP_FILE, AGENTS_STATE, CREDENTIALS_FILE


def _check_json_file(path):
    """Return an issue string if the file is missing, empty, has invalid JSON, or holds an
    empty object/array; None otherwise."""
    if not path.exists():
        return "file is missing"
    text = path.read_text().strip()
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
    if not AGENT_WORKSPACE_MAP_FILE.exists():
        issues.append(("ws_map", AGENT_WORKSPACE_MAP_FILE.name, "file is missing"))
        mapping = {}
    else:
        try:
            mapping = load_workspace_map()
        except json.JSONDecodeError as e:
            issues.append(("ws_map", AGENT_WORKSPACE_MAP_FILE.name, f"invalid JSON: {e}"))
            mapping = {}

    # Shared OAuth files — these must be populated after login.
    for label, path in (("claude.json", ACCOUNT_FILE), ("credentials.json", CREDENTIALS_FILE)):
        msg = _check_json_file(path)
        if msg is not None:
            issues.append(("oauth", label, msg))

    instances = list_all_instances()
    actual = set(instances)

    # Per-instance checks
    for inst_id in instances:
        agent, _, session = inst_id.partition(SESSION_SEP)
        md_path = find_md_for_agent(agent)
        if md_path is None:
            issues.append(("orphan", inst_id, f"agent '{agent}' has no .md file"))
            continue
        sm = state_md(agent, session)
        if sm.exists() and sm.read_text() != md_path.read_text():
            issues.append(("drifted", inst_id, f"CLAUDE.md differs from {md_path.name}"))
        if not list((AGENTS_STATE / inst_id).rglob("history.jsonl")):
            issues.append(("no_history", inst_id, "no history.jsonl found (instance never started?)"))

    # Workspace-map entries
    for inst_id, ws in mapping.items():
        if inst_id not in actual:
            issues.append(("ghost", inst_id, "workspace-map entry has no state dir"))
            continue
        if not ws or not Path(ws).is_dir():
            issues.append(("badworkspace", inst_id, f"workspace not a directory: {ws}"))

    if not issues:
        print(f"All clear. {len(instances)} instance(s) under {AGENTS_STATE}.")
        return

    width = max(len(kind) for kind, _, _ in issues)
    for kind, target, msg in sorted(issues):
        print(f"  [{kind:<{width}}]  {target}: {msg}")


if __name__ == "__main__":
    main()
