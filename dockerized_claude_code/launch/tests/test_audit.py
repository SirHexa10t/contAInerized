"""Tests for launch.audit — the launcher's state-integrity report.

Three pure helpers cover the audit's per-data-source logic:
  `_check_json_file`     — classifies a JSON state file's status
  `_load_store`          — parses instances.toml, degrading corruption to an issue
  `_store_entry_issues`  — validates store entries (ghost/badworkspace/bad_tags)
All are easily-testable in isolation. `main()` is the I/O orchestrator that
loads the store and walks state dirs — left out of unit tests since it
exercises the same primitives plus a great deal of file system access.
Tag validation runs against the real shipped agents/ tree (scan_all), the
same registry the launcher itself uses."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from unittest.mock import patch

from launch import paths
from launch.audit import (
    _check_json_file, _cowork_issues, _load_store, _store_entry_issues,
    _stray_root_instances, build_parser,
)
from launch.cowork import control, mailbox
from launch.paths import AGENTS_DIR
from launch.tags import scan_all

REGISTRY = scan_all(AGENTS_DIR)


class TestCheckJsonFile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "state.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_missing_file(self):
        self.assertEqual(_check_json_file(self.path), "file is missing")

    def test_empty_file(self):
        self.path.write_text("")
        self.assertEqual(_check_json_file(self.path), "file is empty")

    def test_whitespace_only_is_empty(self):
        # _check_json_file strips before checking
        self.path.write_text("   \n\t  \n")
        self.assertEqual(_check_json_file(self.path), "file is empty")

    def test_invalid_json(self):
        self.path.write_text("not json {")
        result = _check_json_file(self.path)
        self.assertIsNotNone(result)
        self.assertIn("invalid JSON", result)

    def test_empty_object(self):
        self.path.write_text("{}")
        self.assertEqual(_check_json_file(self.path), "contents are an empty object")

    def test_empty_array(self):
        # The check is `if not data` — falsy for both {} and []
        self.path.write_text("[]")
        self.assertEqual(_check_json_file(self.path), "contents are an empty object")

    def test_populated_object_is_clean(self):
        self.path.write_text(json.dumps({"key": "value"}))
        self.assertIsNone(_check_json_file(self.path))

    def test_populated_array_is_clean(self):
        self.path.write_text(json.dumps(["entry"]))
        self.assertIsNone(_check_json_file(self.path))

    def test_nested_structure_is_clean(self):
        self.path.write_text(json.dumps({"a": {"b": [1, 2]}}))
        self.assertIsNone(_check_json_file(self.path))


def _entry(workspace, **axes):
    """A store entry dict with the given axis name lists (missing axes → [])."""
    return {
        "workspace": workspace,
        "engine": axes.get("engine"),
        "professions": axes.get("professions", []),
        "specialties": axes.get("specialties", []),
        "policies": axes.get("policies", []),
    }


class TestStoreEntryIssues(unittest.TestCase):
    """Each finding kind exercised in isolation, then a multi-entry mix to
    confirm the helper accumulates rather than short-circuits. Valid tag
    names come from the real tree (code / auto); 'fox' stands in for typos."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ws = self.tmpdir.name   # a real directory → clean workspace

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_empty_store_returns_empty_list(self):
        self.assertEqual(_store_entry_issues({}, set(), REGISTRY), [])

    def test_valid_entry_returns_empty_list(self):
        entries = {"a__s": _entry(self.ws, professions=["code"], specialties=["auto"])}
        self.assertEqual(_store_entry_issues(entries, {"a__s"}, REGISTRY), [])

    def test_ghost_when_instance_missing(self):
        result = _store_entry_issues({"a__gone": _entry(self.ws)}, set(), REGISTRY)
        self.assertEqual(result, [("ghost", "a__gone", "instances.toml entry has no state dir")])

    def test_ghost_precludes_other_checks(self):
        # A ghost entry is only reported once — workspace/tag checks apply
        # only to live instances.
        result = _store_entry_issues({"a__gone": _entry(None, professions=["fox"])}, set(), REGISTRY)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "ghost")

    def test_missing_workspace_flagged(self):
        result = _store_entry_issues({"a__s": _entry(None)}, {"a__s"}, REGISTRY)
        self.assertEqual(result[0][:2], ("badworkspace", "a__s"))

    def test_nondir_workspace_flagged(self):
        result = _store_entry_issues({"a__s": _entry("/no/such/dir")}, {"a__s"}, REGISTRY)
        self.assertEqual(result[0][:2], ("badworkspace", "a__s"))

    def test_unknown_tag_flagged_as_bad_tags(self):
        entries = {"a__s": _entry(self.ws, professions=["fox"])}
        result = _store_entry_issues(entries, {"a__s"}, REGISTRY)
        self.assertEqual(len(result), 1)
        kind, target, msg = result[0]
        self.assertEqual((kind, target), ("bad_tags", "a__s"))
        self.assertIn("fox", msg)

    def test_wrong_axis_flagged_as_bad_tags(self):
        # 'code' is a real tag — but a profession, not a specialty. The
        # wrong-axis diagnostic from validate_build surfaces here.
        entries = {"a__s": _entry(self.ws, specialties=["code"])}
        result = _store_entry_issues(entries, {"a__s"}, REGISTRY)
        self.assertEqual(result[0][0], "bad_tags")

    def test_bad_workspace_and_bad_tags_both_reported(self):
        # Unlike ghost, a bad workspace doesn't preclude the tag check —
        # both defects surface in one pass.
        entries = {"a__s": _entry(None, professions=["fox"])}
        kinds = {k for k, _, _ in _store_entry_issues(entries, {"a__s"}, REGISTRY)}
        self.assertEqual(kinds, {"badworkspace", "bad_tags"})

    def test_no_registry_skips_tag_checks(self):
        # When the tree itself failed to scan, per-entry tag validation is
        # skipped (the single 'tags' issue covers the root cause).
        entries = {"a__s": _entry(self.ws, professions=["fox"])}
        self.assertEqual(_store_entry_issues(entries, {"a__s"}, None), [])

    def test_multi_entry_accumulation(self):
        entries = {
            "a__valid": _entry(self.ws, professions=["code"]),   # clean
            "a__ghost": _entry(self.ws),                         # ghost
            "a__badws": _entry("/no/such/dir"),                  # badworkspace
            "a__typo":  _entry(self.ws, specialties=["fox"]),    # bad_tags
        }
        actual = {"a__valid", "a__badws", "a__typo"}
        kinds_by_target = {t: k for k, t, _ in _store_entry_issues(entries, actual, REGISTRY)}
        self.assertEqual(kinds_by_target, {
            "a__ghost": "ghost",
            "a__badws": "badworkspace",
            "a__typo":  "bad_tags",
        })
        self.assertNotIn("a__valid", kinds_by_target)


class TestStrayRootInstances(unittest.TestCase):
    """_stray_root_instances flags `<agent>__<session>` dirs left at the old
    ~/.claude-agents/ root (instances now live under instances/). The sibling
    root dirs — cache/, user_extras/, instances/ itself — carry no `__` and
    must not be flagged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_missing_root_is_clean(self):
        self.assertEqual(_stray_root_instances(self.root / "nope"), [])

    def test_flags_only_dunder_dirs(self):
        for name in ("poet__a", "refactorer__b", "instances", "cache", "user_extras"):
            (self.root / name).mkdir()
        (self.root / "instances" / "researcher__c").mkdir()   # correctly-placed — not a root stray
        strays = _stray_root_instances(self.root)
        self.assertEqual([target for _, target, _ in strays], ["poet__a", "refactorer__b"])
        self.assertTrue(all(kind == "stray" for kind, _, _ in strays))

    def test_all_relocated_is_clean(self):
        (self.root / "instances").mkdir()
        (self.root / "instances" / "poet__a").mkdir()
        self.assertEqual(_stray_root_instances(self.root), [])


class TestLoadStore(unittest.TestCase):
    """_load_store parses instances.toml directly — corruption degrades to a
    reported issue (the audit keeps checking), and a MISSING file is clean:
    instances then run on their agents' `.lego` defaults."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "instances.toml"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_valid_store_loads_with_no_issues(self):
        self.path.write_text('[a__b]\nworkspace = "/ws"\n')
        mapping, issues = _load_store(self.path)
        self.assertEqual(mapping, {"a__b": {"workspace": "/ws"}})
        self.assertEqual(issues, [])

    def test_missing_file_is_clean(self):
        mapping, issues = _load_store(self.path)
        self.assertEqual(mapping, {})
        self.assertEqual(issues, [])

    def test_corrupt_toml_degrades_to_single_issue_not_exit(self):
        self.path.write_text('[a__b\nworkspace ===')
        mapping, issues = _load_store(self.path)
        self.assertEqual(mapping, {})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0][0], "store")
        self.assertIn("invalid TOML", issues[0][2])

    def test_empty_file_is_empty_map_with_no_issues(self):
        # Matches the launcher's own semantics: zero-byte store == no entries.
        self.path.write_text("")
        mapping, issues = _load_store(self.path)
        self.assertEqual(mapping, {})
        self.assertEqual(issues, [])


class TestCoworkIssues(unittest.TestCase):
    """The group-hosting checks. Real trees in a tmpdir, because every one of
    these findings is a statement about on-disk layout — and one AGENTS_STATE
    patch moves the whole feature (the builders read it at call time)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        p = patch.object(paths, "AGENTS_STATE", self.state)
        p.start()
        self.addCleanup(p.stop)

    def _instance(self, name: str) -> None:
        paths.instance_state_dir_path(name).mkdir(parents=True)

    def _kinds(self):
        return [kind for kind, _, _ in _cowork_issues()]

    def test_a_host_that_never_used_cowork_has_no_findings(self):
        self.assertEqual(_cowork_issues(), [])

    def test_a_participants_dir_with_a_live_instance_is_clean(self):
        self._instance("golem__a")
        paths.cowork_outbox_path("golem__a").mkdir(parents=True)
        self.assertEqual(_cowork_issues(), [])

    def test_an_orphaned_group_dir_is_reported_not_cleaned(self):
        paths.cowork_dir_path("deleted__x").mkdir(parents=True)
        issues = _cowork_issues()
        self.assertEqual(self._kinds(), ["orphan_group"])
        self.assertIn("review the work", issues[0][2])
        self.assertTrue(paths.cowork_dir_path("deleted__x").exists())   # report only

    def test_a_misfiled_session_is_reported_with_its_location(self):
        self._instance("golem__a")
        wrong = paths.cowork_group_path("golem__a", "boss__p-widget")
        wrong.mkdir(parents=True)
        paths.group_session_path(wrong).write_text(
            '{"manager": "boss__p", "project": "widget", "task": "t"}')
        issues = _cowork_issues()
        self.assertEqual(self._kinds(), ["bad_session"])
        self.assertIn("golem__a/boss__p-widget", issues[0][1])
        self.assertIn("boss__p", issues[0][2])

    def test_an_unreadable_session_is_reported(self):
        self._instance("boss__p")
        broken = paths.cowork_group_path("boss__p", "boss__p-widget")
        broken.mkdir(parents=True)
        paths.group_session_path(broken).write_text("{not json")
        self.assertEqual(self._kinds(), ["bad_session"])

    def test_rejected_captures_are_counted_per_instance(self):
        self._instance("golem__a")
        rejected = paths.cowork_outbox_path("golem__a") / mailbox.REJECTED_SUBDIR
        rejected.mkdir(parents=True)
        (rejected / "a.json").write_text("{}")
        (rejected / "b.json").write_text("{}")
        issues = _cowork_issues()
        self.assertEqual(self._kinds(), ["rejected"])
        self.assertIn("2 capture(s)", issues[0][2])

    def test_rejected_control_requests_are_counted_too(self):
        self._instance("boss__p")
        rejected = (paths.cowork_dir_path("boss__p") / control.CONTROL_SUBDIR
                    / control.REJECTED_SUBDIR)
        rejected.mkdir(parents=True)
        (rejected / "r.txt").write_text("roster")
        issues = _cowork_issues()
        self.assertEqual(self._kinds(), ["rejected"])
        self.assertIn("control request", issues[0][2])

    def test_an_empty_rejected_dir_is_not_a_finding(self):
        self._instance("golem__a")
        (paths.cowork_outbox_path("golem__a") / mailbox.REJECTED_SUBDIR).mkdir(parents=True)
        self.assertEqual(_cowork_issues(), [])

    def test_a_stale_pidfile_is_reported(self):
        pid_path = paths.hub_pid_path()
        pid_path.parent.mkdir(parents=True)
        pid_path.write_text("999999999\n")          # far past any real pid
        self.assertEqual(self._kinds(), ["stale_pid"])

    def test_a_live_hubs_pidfile_is_not_a_finding(self):
        import os
        pid_path = paths.hub_pid_path()
        pid_path.parent.mkdir(parents=True)
        pid_path.write_text(f"{os.getpid()}\n")     # us: definitely alive
        self.assertEqual(_cowork_issues(), [])


class TestAuditCli(unittest.TestCase):
    """The audit's only CLI surface is -h/--help (it takes no other args); the
    help text is the module docstring, so it lists every check."""

    def test_help_exits_zero_and_explains_the_checks(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(buf):
            build_parser().parse_args(["-h"])
        self.assertEqual(cm.exception.code, 0)
        out = buf.getvalue()
        self.assertIn("python -m launch.audit", out)   # prog + how-to-run
        self.assertIn("bad_tags", out)                 # representative checks from the docstring
        self.assertIn("oauth", out)

    def test_no_args_parses_clean(self):
        self.assertEqual(vars(build_parser().parse_args([])), {})

    def test_unknown_flag_is_rejected(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            build_parser().parse_args(["--bogus"])


if __name__ == "__main__":
    unittest.main()
