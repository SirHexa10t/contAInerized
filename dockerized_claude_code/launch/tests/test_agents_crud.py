"""Tests for launch.agents_crud — the
instances.json writers (persist / delete / modify against a temp store), and
the install_latest_md integration round-trip.

resolve_pick / creatable_agents / instance_from_store lean on the real
agents/ tree + the md index — their discovery halves are covered by
test_essential_files against the shipped tree."""

import dataclasses
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import json

from launch import paths
from launch.ai import CLAUDE_CODE, GEMINI_CLI
from launch.agents_crud import (
    RESUME_SIZE_WARN_BYTES, compute_resume_flag, delete_instance,
    install_latest_md, install_settings, invalid_tags_report, modify_instance,
    persist_instance,
)
from launch.paths import AGENTS_DIR
from launch.tags import AgentBuild, Instance, TagError, load_lego, scan_all, store
from launch.tags.rules import parse_rules
from launch.tests.fixtures import REGISTRY
from launch.tags.identity import resolve_build
from launch.tags import addendums
from launch.tags.addendums import ADDENDUM_SECTION_TITLE, SEEK_SUMMARY


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
        # Patch BASE_ADDENDUMS empty so compose() returns '' (a bare instance
        # has no tag addendums). install_latest_md must skip the
        # separator+addendum append, yielding the source body byte-for-byte.
        inst = self._inst("just the body\n")
        with patch.object(addendums, "BASE_ADDENDUMS", []):
            install_latest_md(inst)
        self.assertEqual(inst.state_md.read_text(), "just the body\n")


class TestInstallSettings(unittest.TestCase):
    """install_settings — the instance's policies, rendered through its
    harness's policy.mapping, → the per-instance settings.json RO-mounted
    over the CLI's own, plus the harness's rules file when it keeps one.
    Driven through the REAL harness members, the real shipped base and real
    `Rules`; the policies are stand-ins (`.name`, `.label`, `.rules` and
    `.always_on` are all it reads)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        patcher = patch.object(paths, "AGENTS_STATE", Path(self.tmpdir.name) / "state")
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _policy(name, rules, always_on=False):
        return SimpleNamespace(name=name, label=f"<{name}>", always_on=always_on,
                               rules=parse_rules(rules, Path(name) / "tag.rules"))

    @staticmethod
    def _registry(*policies, harnesses=None):
        """A registry stand-in — install_settings reads `.policies` and the
        member of `.harnesses` it renders through (the real ones by default)."""
        return SimpleNamespace(policies={p.name: p for p in policies},
                               harnesses=harnesses or REGISTRY.harnesses)

    def _written(self, inst):
        return json.loads((inst.state_dir / "settings.json").read_text())

    def test_no_policies_yields_base_settings(self):
        inst = _inst()
        self.assertEqual(install_settings(inst, self._registry(), CLAUDE_CODE), ())
        base = json.loads(paths.base_settings_file(CLAUDE_CODE).read_text())
        self.assertEqual(self._written(inst), base)

    def test_policy_renders_onto_base(self):
        inst = _inst(policies=(self._policy("web-research", {"allow": {"tools": ["web"]}}),))
        install_settings(inst, self._registry(), CLAUDE_CODE)
        merged = self._written(inst)
        self.assertEqual(merged["permissions"], {"allow": ["WebFetch", "WebSearch"]})
        self.assertIn("statusLine", merged)   # base settings preserved

    def test_two_policies_lists_concatenate(self):
        inst = _inst(policies=(
            self._policy("a", {"deny": {"shell": ["sudo"]}}),
            self._policy("b", {"deny": {"tools": ["web"]}}),
        ))
        install_settings(inst, self._registry(), CLAUDE_CODE)
        self.assertEqual(self._written(inst)["permissions"]["deny"],
                         ["Bash(sudo *)", "Bash(sudo:*)", "WebFetch", "WebSearch"])

    def test_always_on_policy_applies_without_being_selected(self):
        # The static-tag path: <-su>-style policies come from the REGISTRY,
        # not the instance — every settings.json carries them.
        static = self._policy("no-sudo", {"deny": {"shell": ["sudo"]}}, always_on=True)
        offered = self._policy("web-research", {"allow": {"tools": ["web"]}})
        inst = _inst()   # no policies selected
        install_settings(inst, self._registry(static, offered), CLAUDE_CODE)
        merged = self._written(inst)
        self.assertEqual(merged["permissions"], {"deny": ["Bash(sudo *)", "Bash(sudo:*)"]})   # static applied
        self.assertNotIn("allow", merged["permissions"])                                       # non-static NOT applied unselected

    def test_scalar_conflict_aborts_naming_culprits(self):
        # A shipped pair that cannot hold together: <!plan> demands plan mode,
        # {cowork}'s raw Claude Code settings demand dontAsk — one scalar.
        inst = _inst(policies=(REGISTRY.policies["plan-first"],), specialties=(REGISTRY.specialties["cowork"],))
        with self.assertRaises(TagError) as ctx:
            install_settings(inst, self._registry(), CLAUDE_CODE)
        self.assertIn("plan-first", str(ctx.exception))
        self.assertIn("cowork", str(ctx.exception))

    def test_regenerated_each_call(self):
        inst = _inst(policies=(self._policy("p", {"deny": {"tools": ["web"]}}),))
        install_settings(inst, self._registry(), CLAUDE_CODE)
        install_settings(_inst(), self._registry(), CLAUDE_CODE)   # same instance id, no policies → base only
        self.assertNotIn("permissions", self._written(inst))

    def test_specialty_fragment_rules_render_with_the_policies(self):
        # {ro} claims policy/_read-only's rules: its write-tool deny lands
        # beside the selected policies' on either harness.
        inst = _inst(specialties=(REGISTRY.specialties["read-only"],))
        install_settings(inst, self._registry(), CLAUDE_CODE)
        self.assertEqual(self._written(inst)["permissions"]["deny"], ["Write", "Edit", "NotebookEdit"])

    def test_a_deny_the_harness_cannot_express_refuses_naming_tag_and_word(self):
        # A harness whose mapping lacks a capability: the deny must stop the
        # launch rather than evaporate, and say what to drop.
        gemini = REGISTRY.harnesses["gemini-cli"]
        mapping = dataclasses.replace(gemini.policy_mapping,
                                      tools=tuple(row for row in gemini.policy_mapping.tools if row[0] != "write"))
        registry = self._registry(harnesses={**REGISTRY.harnesses,
                                             "gemini-cli": dataclasses.replace(gemini, policy_mapping=mapping)})
        read_only = REGISTRY.specialties["read-only"]
        with self.assertRaises(TagError) as ctx:
            install_settings(_inst(specialties=(read_only,)), registry, GEMINI_CLI)
        self.assertIn(read_only.label, str(ctx.exception))
        self.assertIn("[deny] tools 'write'", str(ctx.exception))

    def test_a_harness_without_a_mapping_refuses_every_deny(self):
        codex = dataclasses.replace(GEMINI_CLI, key="codex-cli", name="Codex CLI")
        inst = _inst(policies=(self._policy("no-git", {"deny": {"shell": ["git"]}}),))
        with self.assertRaises(TagError) as ctx:
            install_settings(inst, self._registry(), codex)
        self.assertIn("no policy.mapping yet", str(ctx.exception))

    def test_an_allow_the_harness_cannot_express_is_a_note(self):
        gemini = REGISTRY.harnesses["gemini-cli"]
        mapping = dataclasses.replace(gemini.policy_mapping,
                                      tools=tuple(row for row in gemini.policy_mapping.tools if row[0] != "web"))
        registry = self._registry(harnesses={**REGISTRY.harnesses,
                                             "gemini-cli": dataclasses.replace(gemini, policy_mapping=mapping)})
        inst = _inst(policies=(self._policy("web-research", {"allow": {"tools": ["web"]}}),))
        (note,) = install_settings(inst, registry, GEMINI_CLI)
        self.assertIn("<web-research>", note)
        self.assertIn("[allow] tools 'web'", note)

    def test_raw_settings_for_another_harness_refuse(self):
        # {cowork}'s Stop hook is Claude Code settings no word can say yet;
        # on Gemini CLI the specialty would silently lose it.
        cowork = REGISTRY.specialties["cowork"]
        with self.assertRaises(TagError) as ctx:
            install_settings(_inst(specialties=(cowork,)), self._registry(), GEMINI_CLI)
        self.assertIn(cowork.label, str(ctx.exception))
        self.assertIn("claude-code", str(ctx.exception))


class TestInstallSettingsGemini(unittest.TestCase):
    """The fork: a harness that keeps its rules in a file of their own gets
    that file, read back at once, beside a settings.json with no Claude Code
    base in it. Real registry, real mappings, real policies."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        patcher = patch.object(paths, "AGENTS_STATE", Path(self.tmpdir.name) / "state")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _install(self, policies=(), specialties=()):
        inst = _inst(policies=tuple(REGISTRY.policies[n] for n in policies),
                     specialties=tuple(REGISTRY.specialties[n] for n in specialties))
        install_settings(inst, REGISTRY, GEMINI_CLI)
        rules_file = inst.state_dir / REGISTRY.harnesses["gemini-cli"].policy_file
        return inst, json.loads((inst.state_dir / "settings.json").read_text()), tomllib.loads(rules_file.read_text())

    def test_rules_land_in_the_rules_file_not_the_settings(self):
        _, settings, rules = self._install(policies=["no-git"])
        self.assertEqual(settings, {})   # no base yet (step 4), no mode demanded
        commands = {rule.get("commandPrefix") for rule in rules["rule"]}
        self.assertEqual(commands, {"sudo", "git"})   # the always-on <-su>, then the selection

    def test_a_demanded_mode_lands_in_the_settings(self):
        _, settings, _ = self._install(policies=["plan-first"])
        self.assertEqual(settings, {"general": {"defaultApprovalMode": "plan"}})

    def test_project_starters_allows_never_reach_plan_mode(self):
        # project-starter carries <!plan> and <+all>. Gemini's plan mode is
        # enforced by default-tier rules, so an allow ranked above them in
        # plan mode would lift it: every allow must name its modes, and
        # never plan. The day someone widens the scope, this names the agent
        # it breaks.
        build = load_lego(AGENTS_DIR / "project-starter.lego")
        _, settings, rules = self._install(policies=build.policies)
        self.assertEqual(settings["general"]["defaultApprovalMode"], "plan")
        allows = [rule for rule in rules["rule"] if rule["decision"] == "allow"]
        self.assertTrue(allows)
        for rule in allows:
            with self.subTest(tools=rule["toolName"]):
                self.assertEqual(rule["modes"], ["default", "autoEdit", "yolo"])

    def test_the_rules_dir_is_made_host_side_and_takes_new_files(self):
        # Staging mounts the FILE read-only; its dir must exist before docker
        # would create it root-owned, and must take a new file — the CLI
        # writes its auto-saved policies there through a temp file.
        inst, _, _ = self._install()
        rules_dir = (inst.state_dir / REGISTRY.harnesses["gemini-cli"].policy_file).parent
        self.assertTrue(rules_dir.is_dir())
        (rules_dir / "auto-saved.toml.probe.tmp").write_text("")

    def test_an_unreadable_rules_file_stops_the_launch(self):
        with patch("launch.agents_crud.rules_file_text", return_value="[[rule]\n"), \
                self.assertRaises(TagError) as ctx:
            self._install()
        self.assertIn("does not parse", str(ctx.exception))


class TestInvalidTagsReport(unittest.TestCase):
    """invalid_tags_report — the fix-it message for an instance whose store
    entry names tags that no longer resolve. Built against the real registry
    so the listed alternatives are the shipped ones."""

    def setUp(self):
        self.reg = scan_all(paths.AGENTS_DIR)

    def _instance(self, build: AgentBuild) -> Instance:
        clean, problems = self.reg.resolve_store_build(build, scope="solo")
        return Instance(agent="refactorer", md_path=Path("/x.md"), session="s",
                        workspace="/tmp", is_brand_new=False, invalid_tags=tuple(problems),
                        **resolve_build(clean, "refactorer", self.reg))

    def test_startable_flag_tracks_invalid_tags(self):
        self.assertTrue(self._instance(AgentBuild(professions=("code",))).is_startable)
        self.assertFalse(self._instance(AgentBuild(professions=("web",))).is_startable)

    def test_report_says_where_a_forbidden_tag_goes_and_offers_no_alternatives(self):
        # {clstr} is a real specialty a solo build cannot carry: the report
        # points at clusters and does NOT offer the specialty list — which
        # would offer the very tag back (bug-investigator, gate tag-scopes).
        report = invalid_tags_report(self._instance(AgentBuild(specialties=("cluster",))))
        self.assertIn("a solo instance cannot carry", report)
        self.assertIn("clusters only: cluster creation applies it", report)
        self.assertNotIn("replace it with", report)

    def test_report_lists_same_kind_alternatives_only(self):
        # A bad policy name lists policy tags — never professions/specialties.
        report = invalid_tags_report(self._instance(AgentBuild(policies=("nope",))))
        self.assertIn("<nope>", report)
        self.assertIn("web-research", report)   # a real policy
        self.assertNotIn("code", report)         # a profession — wrong kind, not offered
        self.assertNotIn("firewall", report)     # a specialty — wrong kind, not offered

    def test_report_names_the_renamed_profession_alternative(self):
        # The web→webdev rename case: 'web' is gone, 'webdev' is the fix.
        report = invalid_tags_report(self._instance(AgentBuild(professions=("web",))))
        self.assertIn("[web]", report)
        self.assertIn("webdev", report)
        self.assertIn("F2", report)              # the picker fix path is mentioned

    def test_report_explains_wrong_axis(self):
        # A real tag on the wrong axis reads differently from a typo.
        report = invalid_tags_report(self._instance(AgentBuild(specialties=("no-sudo",))))
        self.assertIn("policy tag", report)      # no-sudo is really a policy

    def test_report_does_not_leak_absolute_home_path(self):
        # The store location is shown ~-relative, never as a raw /home/<user> path.
        report = invalid_tags_report(self._instance(AgentBuild(policies=("nope",))))
        self.assertIn("~/", report)
        self.assertNotIn(str(Path.home()), report)


# ============================================================
# compute_resume_flag — resume vs fresh, and the big-transcript warning
# ============================================================


class TestComputeResumeFlag(unittest.TestCase):
    """`--continue` only when a transcript exists — and a WARNING once that
    transcript is big enough to be at risk: claude silently dropped the
    history of a ~92 MB one at launch (plans/ISSUES.md, 2026-08-29), so the
    launch must state the risk instead of letting the operator discover it."""

    def _instance(self, *, transcript_bytes: int | None,
                  brand_new: bool = False) -> Instance:
        registry = scan_all(paths.AGENTS_DIR)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        if transcript_bytes is not None:
            sessions = root / "projects" / "-workspace"
            sessions.mkdir(parents=True)
            # truncate, not write: st_size is what the check reads, and a
            # sparse 60 MB costs nothing.
            with open(sessions / "session.jsonl", "wb") as jsonl:
                jsonl.truncate(transcript_bytes)
        return Instance(agent="refactorer",
                        md_path=paths.AGENTS_DIR / "refactorer.md",
                        session="proj", workspace="/w", is_brand_new=brand_new,
                        state_dir_override=root,
                        **resolve_build(AgentBuild(), "refactorer", registry))

    def _printed(self, mock) -> str:
        return " ".join(str(call.args[0]) for call in mock.call_args_list)

    def test_brand_new_starts_fresh_without_touching_disk_or_printing(self):
        with patch("builtins.print") as printed:
            flag = compute_resume_flag(self._instance(transcript_bytes=1024,
                                                      brand_new=True))
        self.assertEqual(flag, [])
        printed.assert_not_called()

    def test_no_transcript_prints_the_notice_and_starts_fresh(self):
        # `--continue` against history-only state crashes claude with
        # 'No conversation found' — the notice is the difference between a
        # deliberate fresh start and a mystery.
        with patch("builtins.print") as printed:
            flag = compute_resume_flag(self._instance(transcript_bytes=None))
        self.assertEqual(flag, [])
        self.assertIn("starting fresh", self._printed(printed))

    def test_a_small_transcript_resumes_silently(self):
        with patch("builtins.print") as printed:
            flag = compute_resume_flag(self._instance(transcript_bytes=1024))
        self.assertEqual(flag, ["--continue"])
        printed.assert_not_called()

    def test_resume_takes_the_harnesss_whole_argument_list(self):
        # Gemini CLI resumes with `--resume latest`, a flag AND a value; a
        # reader that took one flag would emit a broken command line.
        from launch import agents_crud
        from launch.ai import GEMINI_CLI
        with patch.object(agents_crud, "active_adapter", return_value=GEMINI_CLI), \
             patch("builtins.print"):
            flag = compute_resume_flag(self._instance(transcript_bytes=1024))
        self.assertEqual(flag, ["--resume", "latest"])

    def test_a_huge_transcript_still_resumes_but_says_the_risk_out_loud(self):
        # Still resumes — the operator asked to continue, and most launches
        # survive it — but the warning names the observed failure and the size.
        size = RESUME_SIZE_WARN_BYTES + 2**20
        with patch("builtins.print") as printed:
            flag = compute_resume_flag(self._instance(transcript_bytes=size))
        self.assertEqual(flag, ["--continue"])
        text = self._printed(printed)
        self.assertIn("WARNING", text)
        self.assertIn("51 MB", text)
        self.assertIn("ISSUES.md", text)


if __name__ == "__main__":
    unittest.main()
