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
  - bad_name (an instance's session, or a cluster's directory name, that the
    launcher's label rule refuses — `tags.identity.label_error`, applied to
    every NEW name since 2026-09-09, so these predate it or were made by
    hand. Such an instance still launches but cannot be recruited into a
    group and, under {muxer}, mistargets tmux — F2 in the picker renames it;
    such a cluster is SKIPPED by discovery and never shows in the picker —
    rename its directory)
  - bad_cluster (a clusters/<name>/cluster.toml that fails to load — corrupt
    TOML, a member with an illegal id, a missing project — which discovery
    skips silently)
  - store issues (instances.toml not valid TOML; a MISSING file is fine —
    instances then run on their agents' `.lego` defaults)
  - oauth issues (.claude.json / .credentials.json missing, empty, or not valid JSON)
  - cowork state under ~/.claude-agents/group_hosting/:
      orphan_group — a participant dir whose instance was deleted (the work
                     inside may still be wanted, so nothing auto-cleans it)
      bad_session  — a session.json discovery SKIPS: unreadable, filed in the
                     wrong instance's tree, or in a dir renamed off its key
      rejected     — captures / control requests the hub parked instead of
                     processing; each is a turn or command that went nowhere
      stale_pid    — hub.pid names a dead process (harmless — the next hub
                     clears it — but after a crash it is worth knowing)

Run from the project root:
  python -m launch.audit
"""

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any

from .agents_crud import list_all_instances
from .cluster import state as cluster_state
from .cluster.member import ClusterError
from .cowork import control, group as grp, lifecycle, mailbox
from .file_access import (
    agent_md_index, is_dir, is_file, iter_files, iter_subdirs, path_exists,
    read_text,
)
from .paths import (
    ACCOUNT_FILE, AGENTS_DIR, AGENTS_STATE, CREDENTIALS_FILE, INSTANCES_FILE,
    cluster_state_path, clusters_dir, cowork_outbox_path, group_hosting_dir,
    hub_pid_path, instance_state_dir_path, state_history_path,
)
from .tags import Registry, TagError, scan_all
from .tags.identity import SESSION_SEP, label_error
from .tags.store import entry_to_build

Issue = tuple[str, str, str]   # (kind, target, message)


def _stray_root_instances(state_root: Path) -> list[Issue]:
    """Instance dirs still sitting at the ~/.claude-agents/ ROOT — instances
    now live under instances/, and the launcher only looks there, so a
    `<agent>__<session>` dir left at the root is silently ignored (its history
    and tags are invisible). Report each so the user relocates it. The
    `SESSION_SEP in name` filter is the same one list_all_instances uses, so
    the sibling root dirs (cache/, firewall_cache/, user_extras/, instances/
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


def _illegal_instance_names(instances: list[str]) -> list[Issue]:
    """Instance dirs whose SESSION half the label rule refuses
    (`tags.identity.label_error` — the rule the form has applied to every new
    name since 2026-09-09, so these predate it or were made by hand). They
    still launch as plain instances, but cannot be recruited into a group and
    would mistarget tmux under {muxer}; F2 in the picker renames them."""
    out: list[Issue] = []
    for dir_name in instances:
        _, _, session = dir_name.partition(SESSION_SEP)
        if (error := label_error(session)) is not None:
            out.append(("bad_name", dir_name,
                        f"session name {error} — F2 in the picker renames it"))
    return out


def _cluster_issues() -> list[Issue]:
    """Findings under clusters/: a dir whose NAME the label rule refuses
    (`bad_name` — `cluster_state.discover` skips it silently, so the picker
    never shows it) and a cluster.toml that fails to load for any other
    reason (`bad_cluster` — corrupt TOML, a member with an illegal id, a
    missing project key). Degrades to no findings on a host that never made
    a cluster; a subdir without cluster.toml is not a cluster (discovery
    ignores it too)."""
    out: list[Issue] = []
    for directory in sorted(iter_subdirs(clusters_dir()), key=lambda d: d.name):
        if not is_file(cluster_state_path(directory.name)):
            continue
        if (error := label_error(directory.name)) is not None:
            out.append(("bad_name", directory.name,
                        f"cluster name {error} — the picker cannot show it; "
                        f"rename the directory"))
            continue
        try:
            cluster_state.load(directory.name)
        except (ClusterError, tomllib.TOMLDecodeError, OSError) as e:
            out.append(("bad_cluster", directory.name,
                        f"cluster.toml fails to load — discovery skips it: {e}"))
    return out


def _cowork_issues() -> list[Issue]:
    """Findings under the group-hosting tree. Composed from the cowork
    package's own reporters (`orphan_group_dirs`, `misfiled_sessions`) plus two
    piles only the audit watches: `rejected/` dirs — where the hub parks what
    it could not process, deliberately never deleting — and a pidfile naming a
    dead process. Every check degrades to "no findings" on a host that has
    never used {cowork} (the tree simply is not there)."""
    out: list[Issue] = []
    for orphan in grp.orphan_group_dirs():
        out.append(("orphan_group", orphan.name,
                    "group-hosting dir for a deleted instance — review the work "
                    "inside, then remove it"))
    for misfiled, why in grp.misfiled_sessions():
        out.append(("bad_session", f"{misfiled.parent.name}/{misfiled.name}", why))
    for instance_dir in sorted(iter_subdirs(group_hosting_dir()), key=lambda d: d.name):
        for label, rejected in (
            ("capture(s)", cowork_outbox_path(instance_dir.name) / mailbox.REJECTED_SUBDIR),
            ("control request(s)", instance_dir / control.CONTROL_SUBDIR / control.REJECTED_SUBDIR),
        ):
            count = sum(1 for _ in iter_files(rejected))
            if count:
                out.append(("rejected", instance_dir.name,
                            f"{count} {label} parked in {rejected.name}/ — the hub "
                            f"could not process them; inspect, then delete"))
    if lifecycle.owner() is None and path_exists(hub_pid_path()):
        out.append(("stale_pid", hub_pid_path().name,
                    "names a process that is not running — a crashed hub left it; "
                    "the next `cowork serve` clears it"))
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

    issues.extend(_illegal_instance_names(instances))
    issues.extend(_store_entry_issues(entries, actual, registry))
    issues.extend(_cowork_issues())
    issues.extend(_cluster_issues())

    if not issues:
        print(f"All clear. {len(instances)} instance(s) under {AGENTS_STATE}.")
        return

    width = max(len(kind) for kind, _, _ in issues)
    for kind, target, msg in sorted(issues):
        print(f"  [{kind:<{width}}]  {target}: {msg}")


if __name__ == "__main__":
    main()
