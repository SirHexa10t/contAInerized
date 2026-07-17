"""Tests for launch.agents_crud — model parsing + engine sort key, the
instances.json writers (persist / delete / modify against a temp store), and
the install_latest_md integration round-trip.

resolve_pick / creatable_agents / instance_from_store lean on the real
agents/ tree + the md index — their discovery halves are covered by
test_essential_files against the shipped tree."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import json

from launch import paths
from launch.agents_crud import (
    ORDERED_MODEL_FAMILIES, delete_instance, engine_sort_key,
    install_latest_md, install_settings, modify_instance, parse_model_id,
    persist_instance,
)
from launch.tags import Instance, TagError, scan_all, store
from launch.tags.addendums import (
    ADDENDUM_SECTION_TITLE, ADDENDUMS_BY_TAG, SEEK_SUMMARY,
)


# ============================================================
# parse_model_id
# ============================================================


class TestParseModelId(unittest.TestCase):
    def test_opus_with_minor(self):
        self.assertEqual(parse_model_id("claude-opus-4-7"), ("opus", 4, 7))

    def test_sonnet_with_minor(self):
        self.assertEqual(parse_model_id("claude-sonnet-4-6"), ("sonnet", 4, 6))

    def test_haiku_with_minor(self):
        self.assertEqual(parse_model_id("claude-haiku-4-5-20251001"), ("haiku", 4, 5))

    def test_major_only(self):
        # Minor defaults to 0 when absent
        self.assertEqual(parse_model_id("claude-opus-4"), ("opus", 4, 0))

    def test_unknown_family(self):
        self.assertIsNone(parse_model_id("claude-unknown-4-7"))

    def test_empty_string(self):
        self.assertIsNone(parse_model_id(""))

    def test_garbage_string(self):
        self.assertIsNone(parse_model_id("not-a-model"))

    def test_family_in_middle(self):
        # _FAMILY_RE uses `.search`, so the family can be anywhere
        self.assertEqual(parse_model_id("some-prefix-opus-4-7"), ("opus", 4, 7))

    def test_fable_family_recognised(self):
        # Regression: the Claude 5 launch left "fable" out of
        # ORDERED_MODEL_FAMILIES, so every fable-backed agent parsed as
        # "unknown family" and sank below haiku in the picker.
        self.assertEqual(parse_model_id("claude-fable-5"), ("fable", 5, 0))

    def test_mythos_family_recognised(self):
        # Pre-added insurance: mythos is fable's same-tier sibling
        # (Project Glasswing); recognising it now means a future mythos conf
        # can't repeat the fable-sorted-last bug.
        self.assertEqual(parse_model_id("claude-mythos-5"), ("mythos", 5, 0))


class TestOrderedModelFamilies(unittest.TestCase):
    def test_priority_order(self):
        # Most capable family first, haiku last — affects engine_sort_key.
        self.assertEqual(ORDERED_MODEL_FAMILIES, ["fable", "mythos", "opus", "sonnet", "haiku"])

    def test_every_shipped_engine_family_is_known(self):
        # The picker sorts unknown families past the end — silently, which is
        # how the fable gap went unnoticed. Guard: every ANTHROPIC_MODEL in
        # the repo's shipped engine confs must parse to a known family.
        registry = scan_all(paths.AGENTS_DIR)
        for name, engine in registry.engines.items():
            model = engine.conf_map.get("ANTHROPIC_MODEL", "")
            if not model:
                continue
            with self.subTest(engine=name, model=model):
                self.assertIsNotNone(
                    parse_model_id(model),
                    f"engine '{name}' model {model!r} has no recognised family — "
                    f"add it to ORDERED_MODEL_FAMILIES or its agents sort last",
                )


class TestEngineSortKey(unittest.TestCase):
    """engine_sort_key orders by family capability (fable → opus → sonnet →
    haiku), version descending inside a family; engines with an unrecognised
    or missing model sink past every known family."""

    def test_fable_sorts_before_opus_and_haiku(self):
        keys = {
            "f": engine_sort_key("claude-fable-5"),
            "o": engine_sort_key("claude-opus-4-8"),
            "s": engine_sort_key("claude-sonnet-4-6"),
            "h": engine_sort_key("claude-haiku-4-5"),
        }
        self.assertEqual(sorted(keys, key=keys.get), ["f", "o", "s", "h"])

    def test_unknown_family_sinks_last(self):
        self.assertLess(engine_sort_key("claude-haiku-4-5"), engine_sort_key("claude-mystery-9"))

    def test_missing_model_sinks_last(self):
        self.assertLess(engine_sort_key("claude-haiku-4-5"), engine_sort_key(""))

    def test_higher_version_first_within_family(self):
        self.assertLess(engine_sort_key("claude-opus-4-8"), engine_sort_key("claude-opus-4-7"))


# ============================================================
# instances.json writers — persist / delete / modify over a temp store
# ============================================================


def _tag(name):
    """Duck-typed tag stand-in — the writers only read `.name` off each
    selection member (via _build_of), so a SimpleNamespace suffices."""
    return SimpleNamespace(name=name)


def _inst(agent="poet", session="draft", workspace="/tmp", *,
          engine=None, professions=(), specialties=(), policies=(), md=Path("/fake/poet.md")):
    return Instance(agent=agent, md_path=md, session=session, workspace=workspace,
                    is_brand_new=False, engine=engine, professions=professions,
                    specialties=specialties, policies=policies)


class StoreWritersTestCase(unittest.TestCase):
    """Shared fixture: temp AGENTS_STATE (state dirs) + temp INSTANCES_FILE
    (the store), both patched for the duration of each test."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.addCleanup(self.tmpdir.cleanup)
        for patcher in (
            patch.object(paths, "AGENTS_STATE", root / "state"),
            patch.object(store, "INSTANCES_FILE", root / "instances.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class TestPersistInstance(StoreWritersTestCase):
    def test_writes_full_entry(self):
        persist_instance(_inst(engine=_tag("poet"), professions=(_tag("code"),),
                               specialties=(_tag("auto"),), policies=(_tag("no-sudo"),)))
        self.assertEqual(store.load()["poet__draft"], {
            "workspace": "/tmp", "engine": "poet", "professions": ["code"],
            "specialties": ["auto"], "policies": ["no-sudo"],
        })

    def test_replaces_existing_entry(self):
        persist_instance(_inst(specialties=(_tag("auto"),)))
        persist_instance(_inst(specialties=()))
        self.assertEqual(store.load()["poet__draft"]["specialties"], [])

    def test_engine_none_omitted_from_entry(self):
        # TOML has no null — an unset engine is simply absent, and readers
        # (entry_to_build) see the missing key as None.
        persist_instance(_inst())
        entry = store.load()["poet__draft"]
        self.assertNotIn("engine", entry)
        self.assertIsNone(store.entry_to_build(entry).engine)

    def test_other_entries_untouched(self):
        persist_instance(_inst(session="a"))
        persist_instance(_inst(session="b"))
        self.assertEqual(set(store.load()), {"poet__a", "poet__b"})


class TestDeleteInstance(StoreWritersTestCase):
    def setUp(self):
        super().setUp()
        # delete_instance logs each removal via force_remove — keep test output clean.
        patcher = patch("builtins.print")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_removes_state_dir_and_entry(self):
        inst = _inst()
        inst.state_dir.mkdir(parents=True)
        persist_instance(inst)
        delete_instance(inst)
        self.assertFalse(inst.state_dir.exists())
        self.assertNotIn("poet__draft", store.load())

    def test_missing_state_dir_still_cleans_entry(self):
        # force_remove treats "already absent" as success, so the stale store
        # entry is still swept.
        inst = _inst()
        persist_instance(inst)
        delete_instance(inst)
        self.assertNotIn("poet__draft", store.load())

    def test_failed_removal_keeps_entry_and_gates(self):
        inst = _inst()
        persist_instance(inst)
        with patch("launch.agents_crud.force_remove", return_value=False), \
             patch("launch.agents_crud.prompt_keypress") as gate:
            delete_instance(inst)
        gate.assert_called_once()
        self.assertIn("poet__draft", store.load())


class TestModifyInstance(StoreWritersTestCase):
    def test_rename_moves_dir_and_entry(self):
        old = _inst(session="a")
        old.state_dir.mkdir(parents=True)
        persist_instance(old)
        new = _inst(session="b")
        modify_instance(old, new)
        self.assertFalse(old.state_dir.exists())
        self.assertTrue(new.state_dir.exists())
        self.assertEqual(set(store.load()), {"poet__b"})

    def test_same_id_rewrites_entry_without_move(self):
        old = _inst(specialties=(_tag("auto"),))
        old.state_dir.mkdir(parents=True)
        persist_instance(old)
        modify_instance(old, _inst(specialties=()))
        self.assertTrue(old.state_dir.exists())
        self.assertEqual(store.load()["poet__draft"]["specialties"], [])

    def test_rename_onto_existing_instance_raises(self):
        old, blocker = _inst(session="a"), _inst(session="b")
        old.state_dir.mkdir(parents=True)
        blocker.state_dir.mkdir(parents=True)
        with self.assertRaises(ValueError):
            modify_instance(old, _inst(session="b"))

    def test_workspace_change_persisted(self):
        old = _inst(workspace="/tmp")
        old.state_dir.mkdir(parents=True)
        persist_instance(old)
        modify_instance(old, _inst(workspace="/opt"))
        self.assertEqual(store.load()["poet__draft"]["workspace"], "/opt")


# ============================================================
# install_latest_md — source `.md` + composed addendum → state-dir CLAUDE.md
# ============================================================


class TestInstallLatestMd(unittest.TestCase):
    """End-to-end check that install_latest_md writes the source body plus the
    chain-keyed addendum section to the state-dir CLAUDE.md in a single
    overwrite. Uses real (production) ADDENDUMS_BY_TAG for the base-substring
    assertion so a regression in the composition path surfaces here, not just
    in the tags addendum unit tests. A bare Instance (no professions) has
    chain ["base"], so the base addendums apply."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        root = Path(self.tmpdir.name)
        self.md_path = root / "agent.md"
        patcher = patch.object(paths, "AGENTS_STATE", root / "state")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _inst(self, body):
        self.md_path.write_text(body)
        return _inst(md=self.md_path)

    def test_source_body_is_at_top_of_resulting_md(self):
        inst = self._inst("Source line 1\nSource line 2\n")
        install_latest_md(inst)
        self.assertTrue(inst.state_md.read_text().startswith("Source line 1\nSource line 2\n"))

    def test_base_addendum_body_is_present_in_resulting_md(self):
        inst = self._inst("agent body\n")
        install_latest_md(inst)
        self.assertIn(SEEK_SUMMARY.body, inst.state_md.read_text())

    def test_section_heading_is_present_in_resulting_md(self):
        inst = self._inst("agent body\n")
        install_latest_md(inst)
        self.assertIn(f"## {ADDENDUM_SECTION_TITLE}", inst.state_md.read_text())

    def test_separator_between_source_body_and_addendum(self):
        # Source body ends with '\n', addendum is prefixed with '\n\n' — so the
        # transition is `body\n\n\n## Launch-time...` (one blank line gap).
        inst = self._inst("agent body\n")
        install_latest_md(inst)
        self.assertIn(f"agent body\n\n\n## {ADDENDUM_SECTION_TITLE}",
                      inst.state_md.read_text())

    def test_overwrite_replaces_previous_content(self):
        inst = self._inst("body v1\n")
        install_latest_md(inst)
        # Re-write source `.md`, reinstall — state-dir CLAUDE.md must reflect v2.
        self.md_path.write_text("body v2\n")
        install_latest_md(inst)
        result = inst.state_md.read_text()
        self.assertIn("body v2", result)
        self.assertNotIn("body v1", result)
        # Addendum still there post-overwrite.
        self.assertIn(SEEK_SUMMARY.body, result)

    def test_empty_addendum_yields_source_only(self):
        # Patch ADDENDUMS_BY_TAG to empty so compose() returns ''.
        # install_latest_md must skip the separator+addendum append, yielding
        # the source body byte-for-byte.
        inst = self._inst("just the body\n")
        with patch.dict(ADDENDUMS_BY_TAG, {}, clear=True):
            install_latest_md(inst)
        self.assertEqual(inst.state_md.read_text(), "just the body\n")


class TestInstallSettings(unittest.TestCase):
    """install_settings — base settings + policy fragments → the per-instance
    settings.json that gets RO-mounted over ~/.claude/settings.json. Policy
    fragments come through duck-typed stand-ins (`.name` + `.load_fragment()`
    are all it reads); the base file is the real shipped one."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        patcher = patch.object(paths, "AGENTS_STATE", Path(self.tmpdir.name) / "state")
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _policy(name, fragment):
        return SimpleNamespace(name=name, load_fragment=lambda: fragment)

    def _written(self, inst):
        return json.loads((inst.state_dir / "settings.json").read_text())

    def test_no_policies_yields_base_settings(self):
        inst = _inst()
        install_settings(inst)
        base = json.loads(paths.BASE_SETTINGS_FILE.read_text())
        self.assertEqual(self._written(inst), base)

    def test_policy_fragment_merges_onto_base(self):
        inst = _inst(policies=(self._policy("web-research",
                                            {"permissions": {"allow": ["WebSearch"]}}),))
        install_settings(inst)
        merged = self._written(inst)
        self.assertEqual(merged["permissions"], {"allow": ["WebSearch"]})
        self.assertIn("statusLine", merged)   # base settings preserved

    def test_two_policies_lists_concatenate(self):
        inst = _inst(policies=(
            self._policy("a", {"permissions": {"deny": ["Bash(sudo *)"]}}),
            self._policy("b", {"permissions": {"deny": ["WebFetch"]}}),
        ))
        install_settings(inst)
        self.assertEqual(self._written(inst)["permissions"]["deny"],
                         ["Bash(sudo *)", "WebFetch"])

    def test_scalar_conflict_aborts_naming_culprits(self):
        inst = _inst(policies=(
            self._policy("loose", {"cleanupPeriodDays": 90}),
            self._policy("tight", {"cleanupPeriodDays": 7}),
        ))
        with self.assertRaises(TagError) as ctx:
            install_settings(inst)
        self.assertIn("loose", str(ctx.exception))
        self.assertIn("tight", str(ctx.exception))

    def test_regenerated_each_call(self):
        inst = _inst(policies=(self._policy("p", {"x": {"a": 1}}),))
        install_settings(inst)
        install_settings(_inst())   # same instance id, no policies → base only
        self.assertNotIn("x", self._written(inst))


if __name__ == "__main__":
    unittest.main()
