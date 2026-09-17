"""Audit the launcher's persistent state for inconsistencies.

Reports:
  - tags issues (the agents/ tag tree itself fails to scan — malformed
    tag.info, strict-rule violations, dangling requires; reported once,
    non-fatally, and per-entry tag validation is skipped)
  - stray root instances (a `<agent>__<session>` dir still at the old
    ~/.ai-agents/ root — instances now live under instances/, so the
    launcher no longer sees one left at the root)
  - orphan state dirs (instance dir present but no matching agent .md)
  - no_history (state dir has no history.jsonl — the last-used signal we rely on)
  - ghost store entries (instances.toml entry without a corresponding state dir)
  - badworkspace (entry's workspace points to a non-existent or non-directory path)
  - bad_tags (entry references an engine/profession/specialty/policy that the
    tag tree doesn't define, or puts a name on the wrong axis)
  - implicit_ai / implicit_harness (an instances.toml entry, a cluster member
    table or an agent .lego that names no `ai` / `harness` — it runs the
    tree's default AI and that AI's default harness; anything written before
    the two kinds existed (2026-09-13 / 14) is most likely meant for `claude`
    in `claude-code`, the only option then: F2 dots them for an instance, a
    line in the .lego for an agent)
  - forbidden_tag (a build carries a real tag at a scope its tag.info's
    `forbid_on` refuses — {dood} as a member's own tag, {clstr} in a solo
    store entry; the message says where the tag can go)
  - bad_lego (an agent's .lego fails to parse)
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
  - unmigrated (a login file still at the state root, or the retired
    ~/.claude-agents dir beside the state dir — the audit is READ-ONLY and
    never migrates; any launcher entry (run.py, q, cluster.py) does, once)
  - oauth issues (a harness's auth file under credentials/<harness>/ — the
    files its adapter names — missing, empty, not valid JSON, recording no
    login, or readable by others)
  - key_file issues (credentials/keys/<ai>.env readable by others, a line
    docker would mis-read — quoted value, `export`, a bare name — or a
    variable other than the one the AI's tag.info names as key_env)
  - cowork state under ~/.ai-agents/group_hosting/:
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
from collections.abc import Iterable
import json
import tomllib
from pathlib import Path
from typing import Any

from .agents_crud import list_all_instances
from .cluster import state as cluster_state
from .cluster.member import ClusterError
from .cowork import control, group as grp, lifecycle, mailbox
from .file_access import (
    agent_md_index, file_mode, is_dir, is_file, iter_files, iter_subdirs, key_file_problems, login_state, path_exists,
    read_text,
)
from .ai import ADAPTERS, CLAUDE_CODE
from .paths import (
    AGENTS_DIR, AGENTS_STATE, INSTANCES_FILE,
    cluster_state_path, clusters_dir, cowork_outbox_path, group_hosting_dir,
    credentials_dir, hub_pid_path, instance_state_dir_path, key_file, state_history_path,
)
from .tags import AgentBuild, Registry, TagError, scan_all, scope_note
from .tags.identity import SESSION_SEP, label_error
from .tags.lego import load_lego
from .tags.store import entry_to_build

Issue = tuple[str, str, str]   # (kind, target, message)


def _stray_root_instances(state_root: Path) -> list[Issue]:
    """Instance dirs still sitting at the ~/.ai-agents/ ROOT — instances
    now live under instances/, and the launcher only looks there, so a
    `<agent>__<session>` dir left at the root is silently ignored (its history
    and tags are invisible). Report each so the user relocates it. The
    `SESSION_SEP in name` filter is the same one list_all_instances uses, so
    the sibling root dirs (cache/, firewall_cache/, user_extras/, instances/
    itself) are naturally skipped."""
    return [("stray", d.name, "instance dir at the ~/.ai-agents/ root — move it into instances/")
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


def _implicit_axes(build: AgentBuild, target: str, registry: Registry | None) -> list[Issue]:
    """The `implicit_ai` / `implicit_harness` findings for one build (a store
    entry, a cluster member, an agent's .lego): an unset axis runs the tree's
    default — named when the tree scanned — and, for anything written before
    the kinds existed, that default is most likely what was meant."""
    out: list[Issue] = []
    if build.ai is None:
        default = registry.default_ai if registry else None
        runs = f" — it runs the tree's default, {default.label}" if default else ""
        out.append(("implicit_ai", target,
                    f"names no ai{runs}; anything from before 2026-09-13 is most likely meant for "
                    f"claude — set ai = \"claude\" (F2 dots it for an instance)"))
    if build.harness is None:
        harness = registry.harness_for(build) if registry else None
        runs = f" — it runs its AI's default, {harness.label}" if harness else ""
        out.append(("implicit_harness", target,
                    f"names no harness{runs}; anything from before 2026-09-14 is most likely meant for "
                    f"claude-code — set harness = \"claude-code\" (F2 dots it for an instance)"))
    return out


def _scope_issues(build: AgentBuild, target: str, registry: Registry | None, scope: str) -> list[Issue]:
    """The `forbidden_tag` findings for one build living in `scope` (`solo`
    for a store entry or a .lego, `cluster` for a cluster's shared set,
    `member` for a member's own additions): a real tag whose `forbid_on`
    names the scope — `{dood}` as a member's own tag, `{clstr}` in a solo
    entry — in the words the form and the launch use (`scope_note`)."""
    if registry is None:
        return []
    return [("forbidden_tag", target, f"{tag.label} cannot be a {scope} build's tag — {scope_note(tag, scope)}")
            for tag in registry.forbidden(build, scope)]


def _lego_issues(legos: Iterable[tuple[str, Path]], registry: Registry | None) -> list[Issue]:
    """Findings for the agents' .lego files: `bad_lego` (fails to parse) and
    the implicit-axis findings (`_implicit_axes`) — a shipped agent that names
    no AI would silently follow a change of the tree's default."""
    out: list[Issue] = []
    for name, path in sorted(legos):
        try:
            build = load_lego(path)
        except TagError as e:
            out.append(("bad_lego", path.name, str(e)))
            continue
        out.extend(_implicit_axes(build, path.name, registry))
        out.extend(_scope_issues(build, path.name, registry, "solo"))
    return out


def _unmigrated_issues(state_root: Path) -> list[Issue]:
    """`unmigrated` findings: state an older launcher left where the current
    one no longer looks — a Claude Code login file at the state root (the
    credentials moved under credentials/claude-code/ on 2026-09-15) or the
    retired `~/.claude-agents` dir beside the state dir (renamed 2026-09-14).
    The audit never migrates (it is read-only); every launcher entry does, as
    its first step — so the fix is one launch. Without this finding the audit
    said "clean" on exactly the state that produced the 2026-09-15 incident."""
    out: list[Issue] = []
    from .tags.migrations import RETIRED_STATE_DIR_NAME
    for f in CLAUDE_CODE.auth_files:
        if is_file(state_root / f.name):
            out.append(("unmigrated", f.name, "a login file at the state root — the launcher reads "
                        f"credentials/{CLAUDE_CODE.key}/ now; run any launcher entry once to move it"))
    retired = state_root.parent / RETIRED_STATE_DIR_NAME
    if is_dir(retired):
        out.append(("unmigrated", RETIRED_STATE_DIR_NAME, f"the retired state dir beside {state_root.name} — "
                    "the launcher renames it into place when the current dir is absent, else leaves both; merge or remove it"))
    return out


def _auth_file_issues() -> list[Issue]:
    """`oauth` findings: for every harness the launcher can run (an adapter
    exists), each auth file its adapter names under credentials/<harness>/
    that is missing, empty or not valid JSON — populated by a login inside a
    container, never by the launcher. Only JSON-shaped files are parsed; an
    env-shaped one is checked for presence."""
    out: list[Issue] = []
    for key, adapter in sorted(ADAPTERS.items()):
        for f in adapter.auth_files:
            path = credentials_dir(key) / f.name
            msg = _check_json_file(path) if f.blank == "{}" else (None if path_exists(path) else "file is missing")
            if msg is None and f.login_key and login_state(path, f) == "none":
                msg = f"records no login (no {f.login_key}) — log in inside a container once"
            if msg is not None:
                out.append(("oauth", f"credentials/{key}/{f.name}", msg))
            mode = file_mode(path)
            if mode is not None and mode & 0o077:
                out.append(("oauth", f"credentials/{key}/{f.name}", f"mode {mode:o} — holds tokens; chmod 600"))
    return out


def _key_file_issues(registry: Registry | None) -> list[Issue]:
    """`key_file` findings for credentials/keys/<ai>.env: a file readable by
    anyone but its owner (an API key — 0600 expected), or one docker would
    mis-read or that defines anything but the AI's `key_env`
    (`file_access.key_file_problems` — names and lines, never a value). A
    missing key file is not a finding: the harness's login is the other way in."""
    out: list[Issue] = []
    for ai in (registry.ais.values() if registry else ()):
        path = key_file(ai.name)
        if not is_file(path):
            continue
        mode = file_mode(path)
        if mode is not None and mode & 0o077:
            out.append(("key_file", f"credentials/keys/{path.name}", f"mode {mode:o} — an API key; chmod 600"))
        out.extend(("key_file", f"credentials/keys/{path.name}", problem) for problem in key_file_problems(path, ai.key_env))
    return out


def _store_entry_issues(entries: dict[str, Any], actual: set[str],
                        registry: Registry | None) -> list[Issue]:
    """Per-entry findings for instances.toml, as (kind, instance_id, msg)
    tuples. Extracted so each finding kind has direct unit-test coverage:
      ghost        — entry whose instance has no state dir
      badworkspace — workspace missing/None or not a directory on disk
      bad_tags     — axis references the tag tree can't resolve (unknown name
                     or wrong axis), caught via the same validate_build the
                     launcher itself uses; skipped when the tree failed to
                     scan (`registry` is None) — the 'tags' issue covers it.
      implicit_ai / implicit_harness — the entry names no ai / harness
                     (`_implicit_axes`)."""
    out: list[Issue] = []
    for instance_id, entry in entries.items():
        if instance_id not in actual:
            out.append(("ghost", instance_id, "instances.toml entry has no state dir"))
            continue
        ws = entry.get("workspace")
        if not ws or not is_dir(ws):
            out.append(("badworkspace", instance_id, f"workspace not a directory: {ws}"))
        build = entry_to_build(entry)
        if registry is not None:
            try:
                registry.validate_build(build, f"instances.toml[{instance_id}]", scope=None)   # scope: `_scope_issues` reports it as its own kind
            except TagError as e:
                out.append(("bad_tags", instance_id, str(e)))
        out.extend(_implicit_axes(build, instance_id, registry))
        out.extend(_scope_issues(build, instance_id, registry, "solo"))
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


def _cluster_issues(registry: Registry | None = None) -> list[Issue]:
    """Findings under clusters/: a dir whose NAME the label rule refuses
    (`bad_name` — `cluster_state.discover` skips it silently, so the picker
    never shows it), a cluster.toml that fails to load for any other reason
    (`bad_cluster` — corrupt TOML, a member with an illegal id, a missing
    project key), and per member the implicit-axis findings
    (`_implicit_axes`, target `<cluster>[<member>]`). Degrades to no findings
    on a host that never made a cluster; a subdir without cluster.toml is not
    a cluster (discovery ignores it too)."""
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
            cluster = cluster_state.load(directory.name)
        except (ClusterError, tomllib.TOMLDecodeError, OSError) as e:
            out.append(("bad_cluster", directory.name,
                        f"cluster.toml fails to load — discovery skips it: {e}"))
            continue
        if cluster:
            out.extend(_scope_issues(cluster.tags, directory.name, registry, "cluster"))
        for member in (cluster.members if cluster else ()):
            out.extend(_implicit_axes(member.build, f"{directory.name}[{member.id}]", registry))
            out.extend(_scope_issues(member.build, f"{directory.name}[{member.id}]", registry, "member"))
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
        registry = scan_all(AGENTS_DIR)   # NOT startup.open_launcher: the audit is read-only and must never migrate — `_unmigrated_issues` reports what a launch would move
    except TagError as e:
        registry = None
        issues.append(("tags", AGENTS_DIR.name, str(e)))

    entries, store_issues = _load_store(INSTANCES_FILE)
    issues.extend(store_issues)

    issues.extend(_unmigrated_issues(AGENTS_STATE))
    issues.extend(_auth_file_issues())
    issues.extend(_key_file_issues(registry))

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
    issues.extend(_lego_issues(((name, AGENTS_DIR / f"{name}.lego") for name in agent_md_index()), registry))
    issues.extend(_cowork_issues())
    issues.extend(_cluster_issues(registry))

    if not issues:
        print(f"All clear. {len(instances)} instance(s) under {AGENTS_STATE}.")
        return

    width = max(len(kind) for kind, _, _ in issues)
    for kind, target, msg in sorted(issues):
        print(f"  [{kind:<{width}}]  {target}: {msg}")


if __name__ == "__main__":
    main()
