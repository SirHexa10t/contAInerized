"""Structural integrity: assert the files the launcher relies on actually
exist at the paths it computes. Catches "renamed/moved a file but missed a
reference" regressions immediately. Repo-hygiene invariants that no single
module owns live here too — no stray root Dockerfiles, one definition of the
quality gate.

For each chain tag with launch-side machinery, this also verifies the
required `_apply_<name>` handler is in place — a missing artifact fails
here at test time rather than at runtime.
The tag tree itself (Dockerfiles, tag.info shapes, `.lego` references) is
validated by TestTagTreeDiscovery / TestAgentLegoFiles below via scan_all."""

import re
import unittest

from launch import paths, tag_handlers
from launch.tags import load_lego, scan_all

# Chain tags with host-side launch behavior must have an _apply_ handler;
# the rest are data-only ({auto} = claude_args + wants; [webdev]'s playwright
# cache rides [code]'s ~/.cache mount).
HANDLER_TAGS = ["code", "dood", "firewall"]
DATA_ONLY_TAGS = ["webdev", "auto", "read-only"]   # read-only's effects (workspace :ro + claimed policy fragment) are data, not a handler


class TestRepoLayout(unittest.TestCase):
    def test_dockerized_root_exists(self):
        self.assertTrue(paths.DOCKERIZED_CLAUDE_ROOT.is_dir())

    def test_agents_dir_exists(self):
        self.assertTrue(paths.AGENTS_DIR.is_dir())

    def test_settings_dir_exists(self):
        self.assertTrue(paths.SETTINGS_DIR.is_dir())

    def test_template_files_dir_exists(self):
        self.assertTrue(paths.TEMPLATE_FILES_DIR.is_dir())


class TestBaseDockerArtifacts(unittest.TestCase):
    def test_base_dockerfile_exists(self):
        self.assertTrue(paths.BASE_DOCKERFILE.is_file())

    def test_base_dockerfile_carries_firewall_prerequisites(self):
        # {firewall} has no image layer — iptables + the scoped sudoers entry
        # ride the base image (inert without the specialty's mounted script).
        text = paths.BASE_DOCKERFILE.read_text()
        self.assertIn("iptables", text)
        self.assertIn("init-firewall.sh", text)   # the sudoers NOPASSWD line


class TestTagHandlerArtifacts(unittest.TestCase):
    """Tags with host-side launch behavior must keep their `_apply_<name>`
    handler; data-only tags must NOT grow one silently (their behavior is
    declared in tag.info / tag.docker, and an unexpected handler would mean
    logic crept out of the data). Dockerfile/tag.docker presence is checked
    against the discovered tree in TestTagTreeDiscovery."""

    def test_handler_tags_have_apply_handler(self):
        for tag in HANDLER_TAGS:
            with self.subTest(tag=tag):
                handler = getattr(tag_handlers, f"_apply_{tag}", None)
                self.assertTrue(callable(handler),
                                f"tag_handlers is missing _apply_{tag}()")

    def test_data_only_tags_have_no_handler(self):
        for tag in DATA_ONLY_TAGS:
            with self.subTest(tag=tag):
                self.assertFalse(hasattr(tag_handlers, f"_apply_{tag}"),
                                 f"_apply_{tag} exists — {tag} is meant to be data-only")

    def test_root_has_no_layer_dockerfiles(self):
        # Layer Dockerfiles live in the agents/ tree; a stray root
        # Dockerfile.<x> would be dead weight (or worse, shadow one).
        strays = list(paths.DOCKERIZED_CLAUDE_ROOT.glob("Dockerfile.*"))
        self.assertEqual(strays, [])

    def test_base_has_no_apply_handler(self):
        # base has no side effects beyond being the starting image — no handler.
        self.assertFalse(hasattr(tag_handlers, "_apply_base"))


class TestUserExtrasTemplates(unittest.TestCase):
    """Template files that the launcher plants into ~/.claude-agents/user_extras/
    on first launch."""

    def test_firewall_whitelist_template_exists(self):
        self.assertTrue(paths.FIREWALL_WHITELIST_TEMPLATE.is_file())

    def test_optional_creds_readme_template_exists(self):
        self.assertTrue(paths.OPTIONAL_CREDS_README_TEMPLATE.is_file())


class TestDefaultAgentConf(unittest.TestCase):
    def test_default_conf_exists(self):
        self.assertTrue(paths.DEFAULT_CONF.is_file())


class TestQualityGate(unittest.TestCase):
    """check.sh is the ONE definition of what a passing tree means; the CI
    workflow builds an environment and calls it. The point of that split is
    that a developer's local run and CI can't check different things, so these
    tests exist to keep the definition from being copied into the workflow —
    which is how the two would silently drift."""

    # The three checks, as the fragments each must appear as somewhere.
    GATE_COMMANDS = ["unittest discover", "ruff check", "mypy launch/"]

    def setUp(self):
        root = paths.DOCKERIZED_CLAUDE_ROOT
        self.script = root / "check.sh"
        self.workflow = root / ".github" / "workflows" / "ci.yml"

    def test_gate_script_exists(self):
        self.assertTrue(self.script.is_file(), "check.sh is the gate — it must exist")

    def test_gate_script_runs_all_three_checks(self):
        text = self.script.read_text()
        for command in self.GATE_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(command, text)

    def test_gate_script_does_not_stop_at_the_first_failure(self):
        # `set -e` would abort at the first failing check, hiding the other
        # two — the opposite of the "complete picture in one pass" contract in
        # the script's header. Asserting on the flag stands in for a behavioral
        # test, which would have to run the gate (and so this suite) recursively.
        # re.MULTILINE matters: without it `^` anchors to the start of the file,
        # which is the shebang, and the assertion can never fail.
        text = self.script.read_text()
        self.assertNotRegex(text, re.compile(r"^set -[a-z]*e", re.MULTILINE),
                            "check.sh must not be fail-fast")

    def test_workflow_delegates_to_the_gate_script(self):
        self.assertTrue(self.workflow.is_file())
        self.assertIn("bash check.sh", self.workflow.read_text())

    def test_workflow_does_not_restate_the_checks(self):
        # The drift-catcher. A check inlined here would run in CI but not
        # locally (or worse, run differently), and nothing else would notice.
        text = self.workflow.read_text()
        for command in self.GATE_COMMANDS:
            with self.subTest(command=command):
                self.assertNotIn(command, text,
                                 f"{command!r} belongs in check.sh, not the workflow")

    def test_project_skill_delegates_to_the_gate_script(self):
        # /ai_project-test is the third surface that could restate a check —
        # it already drifted once (its mypy line predated two entry points).
        # Like the workflow, it may only repair the environment and call the
        # script; a check defined there would run for agents but not CI.
        # It lives with the other tag-granted commands ([self] declares it),
        # not in .claude/commands/ — command files ride tags now.
        skill = paths.AGENTS_COMMANDS_DIR / "ai_project-test.md"
        self.assertTrue(skill.is_file())
        text = skill.read_text()
        self.assertIn("bash check.sh", text)
        for command in self.GATE_COMMANDS:
            with self.subTest(command=command):
                self.assertNotIn(command, text,
                                 f"{command!r} belongs in check.sh, not the skill")

    def test_workflow_matrix_covers_the_declared_python_floor(self):
        # Bump requires-python and CI must follow, or the floor is a claim
        # nothing tests. Pinned against pyproject rather than a literal so
        # there is still only one source for the number.
        import tomllib
        with (paths.DOCKERIZED_CLAUDE_ROOT / "pyproject.toml").open("rb") as handle:
            requires = tomllib.load(handle)["project"]["requires-python"]
        floor = requires.lstrip(">=~^ ")
        self.assertIn(f'"{floor}"', self.workflow.read_text(),
                      f"requires-python is {requires} — add {floor} to the CI matrix")


class TestTagTreeDiscovery(unittest.TestCase):
    """Integration checkpoint (tags rewrite): the real agents/ tree is
    discovered + validated by the tags package. Guards every migration step —
    a mis-shaped tag dir (missing tag.info, stray dir, orphan _layer, dangling
    reference) fails scan_all here rather than at launch. Kinds whose subtrees
    aren't migrated yet are simply absent (empty), which scan_all allows."""

    def setUp(self):
        self.reg = scan_all(paths.AGENTS_DIR)

    def test_engines_discovered(self):
        self.assertLessEqual(
            {"default", "golem", "poet", "thinker", "researcher", "breakthrough", "reliable", "quick"},
            set(self.reg.engines),
        )

    def test_engine_conf_resolves_through_new_tree(self):
        self.assertIn("haiku", self.reg.engines["golem"].conf_map.get("ANTHROPIC_MODEL", ""))
        self.assertIn("opus", self.reg.engines["reliable"].conf_map.get("ANTHROPIC_MODEL", ""))
        self.assertIn("sonnet", self.reg.engines["quick"].conf_map.get("ANTHROPIC_MODEL", ""))
        self.assertTrue(self.reg.engines["breakthrough"].conf_map.get("ANTHROPIC_MODEL"))

    def test_professions_discovered_with_nesting_requires(self):
        self.assertLessEqual({"code", "webdev"}, set(self.reg.professions))
        self.assertEqual(self.reg.professions["webdev"].requires, frozenset({"code"}))

    def test_specialties_discovered_with_layer_requires(self):
        self.assertLessEqual({"auto", "dood"}, set(self.reg.specialties))
        self.assertEqual(self.reg.specialties["dood"].requires, frozenset({"code"}))  # via _dood layer
        self.assertTrue(self.reg.specialties["auto"].warn)

    def test_cluster_nests_inside_muxer_which_owns_the_layer(self):
        # A cluster IS multiplexing, so ticking {cluster} must bring {muxer} —
        # the same tree mechanism that makes {manager} require {cowork}. The
        # multiplexer install rides {muxer}'s hidden layer, at the profession
        # ROOT so it carries no [code] requirement.
        muxer, cluster = self.reg.specialties["muxer"], self.reg.specialties["cluster"]
        self.assertEqual(cluster.requires, frozenset({"muxer"}))
        self.assertEqual(muxer.requires, frozenset())
        self.assertIsNotNone(muxer.layer)
        self.assertEqual(muxer.layer.path.name, "_muxer")
        self.assertEqual(muxer.layer.requires, frozenset())
        # {cluster} has no layer YET — the message-queue will bring `_cluster`.
        self.assertIsNone(cluster.layer)

    def test_neither_muxer_nor_cluster_warns(self):
        # `warn` marks AUTHORITY an instance would not otherwise have ({dood}'s
        # host-root, {auto}'s bypassed prompts). {muxer} adds panes for the human.
        # {cluster} multiplies an exposure every agent already carries toward its
        # own workspace and state rather than introducing a new kind — so an
        # earlier `warn = true` on it was revoked.
        self.assertFalse(self.reg.specialties["muxer"].warn)
        self.assertFalse(self.reg.specialties["cluster"].warn)

    def test_the_muxer_layer_installs_a_multiplexer_and_nothing_else(self):
        # Thin on purpose: swapping the backend (herdr is the watched candidate)
        # should be this file plus cluster/tmux.py, not a tag-tree rebuild.
        text = (self.reg.specialties["muxer"].layer.path / "Dockerfile").read_text()
        self.assertIn("tmux", text)
        self.assertNotIn("docker-ce", text)

    def test_cluster_addendum_teaches_the_member_its_own_identity(self):
        # A member shares its persona with any sibling built from the same agent,
        # so the env vars are the only way it can tell which one it is.
        _, body = self.reg.specialties["cluster"].addendum
        for var in ("CLUSTER_MEMBER", "CLUSTER_ROLE", "CLUSTER_SESSION"):
            self.assertIn(var, body)
        # And the trust rule cowork learned the hard way.
        self.assertIn("no authority", body.lower())

    def test_muxer_addendum_does_not_promise_a_session_that_may_not_exist(self):
        # {muxer} installs the multiplexer; nothing in the SOLO launch path starts
        # one (run_container execs claude directly). An addendum asserting "this
        # container runs a multiplexer" would therefore be false for every solo
        # instance carrying the tag — so it must describe availability, and tell
        # the agent how to check.
        _, body = self.reg.specialties["muxer"].addendum
        self.assertIn("installed", body)
        self.assertIn("list-sessions", body)

    def test_manager_nests_inside_cowork(self):
        # The role tag: shipped inside cowork/, so recruiting power implies
        # recruitability, and the form auto-ticks {cowork} under {manager}.
        self.assertLessEqual({"cowork", "manager"}, set(self.reg.specialties))
        self.assertEqual(self.reg.specialties["manager"].requires,
                         frozenset({"cowork"}))
        self.assertTrue(self.reg.specialties["manager"].warn)

    def test_cowork_wants_its_grants_and_a_firewall(self):
        # All three probed live (perm_probe): without the grants a coworker is
        # auto-denied every script run and all web access; WITH them and no
        # {firewall} it reached arbitrary hosts and installed packages
        # unsupervised. Wants are advisory — the red form/banner warning, never
        # a hard cascade — because a read-only reviewer coworker is a
        # legitimate build.
        wants = self.reg.specialties["cowork"].wants_map
        self.assertLessEqual({"free-bash", "web-research", "firewall"}, set(wants))
        self.assertIn("DENIED", wants["free-bash"])

    def test_autonomy_tags_all_want_a_firewall(self):
        # The shared rule: any tag that lets an agent act without a human
        # watching each turn asks for a network boundary. {auto} bypasses the
        # prompt engine; {cowork} is driven by a manager. Same argument, so a
        # future autonomy tag should join this list rather than rediscover it.
        for tag in ("auto", "cowork"):
            with self.subTest(tag=tag):
                self.assertIn("firewall", self.reg.specialties[tag].wants_map)

    def test_all_actions_is_the_union_of_the_grant_pair(self):
        # <+all>'s description says "pick this OR that pair — they overlap
        # completely"; this pins the claim so neither side can drift under it.
        def allows(name):
            return set(self.reg.policies[name].load_fragment()["permissions"]["allow"])
        umbrella = allows("all-actions")
        self.assertLessEqual(allows("free-bash") | allows("web-research"), umbrella)
        # And it covers every denial the probe surfaced.
        self.assertLessEqual({"Bash", "WebFetch", "WebSearch"}, umbrella)
        self.assertEqual(self.reg.policies["all-actions"].label, "<+all>")

    def test_cowork_addendum_warns_that_only_the_last_message_survives(self):
        # The capture is `last_assistant_message` — a coworker that narrates
        # incrementally loses everything but its closing chunk, silently, and
        # neither side can tell. Observed live; the addendum bullet is the only
        # mitigation, so it must not be edited away.
        _, body = self.reg.specialties["cowork"].addendum
        self.assertIn("one final message", body.lower())
        self.assertIn("silently lost", body)

    def test_manager_declares_the_cowork_command(self):
        # `/cowork` used to live in the shared `custom_commands/`, which mounts
        # into EVERY container — so every instance carried a command only a manager
        # can use. It belongs to the tag: declared by NAME in tag.info, with the
        # file central in agents/_commands/ so another tag could share it.
        #
        # It needs NO tag.docker: a per-tag mount over ~/.claude/commands/<name>.md
        # is impossible, because that directory is itself a read-only mount and
        # docker cannot create a mountpoint inside one (`mount: read-only file
        # system`, which killed every {manager} launch). The launcher assembles the
        # dir instead — see agents_crud.install_commands.
        self.assertEqual(self.reg.specialties["manager"].commands, ("cowork",))
        self.assertTrue((paths.AGENTS_COMMANDS_DIR / "cowork.md").is_file())
        self.assertIsNone(self.reg.specialties["manager"].docker,
                          "{manager} needs no tag.docker — the dir is assembled")

    def test_the_commands_dir_is_assembled_not_mounted_from_the_repo(self):
        # The shared dir must NOT be an always-on mount any more: mounting it
        # read-only is what made a nested per-tag mount impossible.
        from launch.paths import DOCKER_BASE_MOUNTS, SHARED_COMMANDS_DIR
        self.assertNotIn(SHARED_COMMANDS_DIR, DOCKER_BASE_MOUNTS)

    def test_a_manager_instance_gets_the_command_and_a_plain_one_does_not(self):
        # The behaviour the whole move exists for, end to end.
        import tempfile
        from pathlib import Path

        from launch.agents_crud import install_commands
        from launch.paths import state_commands_dir
        from launch.tags import AgentBuild, Instance, resolve_build

        def commands_for(specialties: tuple[str, ...]) -> set[str]:
            with tempfile.TemporaryDirectory() as tmp:
                inst = Instance(
                    agent="refactorer", md_path=paths.AGENTS_DIR / "refactorer.md",
                    session="s", workspace="/w", is_brand_new=False,
                    state_dir_override=Path(tmp),
                    **resolve_build(AgentBuild(specialties=specialties),
                                    "refactorer", self.reg))
                install_commands(inst)
                return {p.name for p in state_commands_dir(inst.state_dir).glob("*.md")}

        managerial = commands_for(("cowork", "manager"))
        plain = commands_for(())
        self.assertIn("cowork.md", managerial)
        self.assertNotIn("cowork.md", plain)
        # Both still get the shared ones.
        self.assertLessEqual({"refactor.md", "write-summary.md"}, plain)

    def test_a_tag_command_shadowing_a_shared_one_aborts_loudly(self):
        # Same NAME from two different files means somebody's command silently
        # replaces somebody else's in ~/.claude/commands — whichever won, one
        # author's file would not be what instances run. (Two tags declaring
        # the same command is fine: one file, no shadowing.)
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from launch import agents_crud
        from launch.tags import AgentBuild, Instance, TagError, resolve_build

        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            shared.mkdir()
            (shared / "cowork.md").write_text("---\ndescription: impostor\n---\n")
            inst = Instance(
                agent="refactorer", md_path=paths.AGENTS_DIR / "refactorer.md",
                session="s", workspace="/w", is_brand_new=False,
                state_dir_override=Path(tmp) / "state",
                **resolve_build(AgentBuild(specialties=("cowork", "manager")),
                                "refactorer", self.reg))
            with patch.object(agents_crud, "SHARED_COMMANDS_DIR", shared), \
                 self.assertRaisesRegex(TagError, "collision.*cowork"):
                agents_crud.install_commands(inst)

    def test_the_shared_commands_dir_no_longer_carries_cowork(self):
        # Two homes for one command would mean every instance gets it anyway.
        shared = paths.DOCKERIZED_CLAUDE_ROOT / "custom_commands"
        self.assertFalse((shared / "cowork.md").exists())

    def test_the_central_dir_is_the_only_underscore_commands_dir(self):
        # `_commands` appears exactly once: the central dir at the agents/ root
        # (underscored as the project's "internal asset, not a tag" marker).
        # A per-TAG `_commands/` — the retired mechanism — must not reappear:
        # nothing reads one any more, so it would be SILENTLY inert, its
        # command granted to nobody with no error anywhere. Declare in
        # tag.info; put the file in the central dir.
        self.assertEqual(list(paths.AGENTS_DIR.rglob(paths.COMMANDS_DIR_NAME)),
                         [paths.AGENTS_COMMANDS_DIR])

    def test_every_central_command_is_claimed_by_at_least_one_tag(self):
        # agents/_commands/ is the one place specialized commands live; a file
        # there that no tag declares is granted to nobody — dead weight that
        # LOOKS shipped. (The reverse — a declared name with no file — aborts
        # scan_all, so it needs no test here.)
        claimed = {name for tag in self.reg.get_all() for name in tag.commands}
        on_disk = {p.stem for p in paths.AGENTS_COMMANDS_DIR.glob("*.md")}
        self.assertEqual(on_disk - claimed, set())

    def test_every_central_command_describes_itself(self):
        # The legend and Claude Code's /help both show the frontmatter
        # description; a command without one renders as a blank cell.
        from launch.gui.menu_picker import _frontmatter_description
        for md in paths.AGENTS_COMMANDS_DIR.glob("*.md"):
            with self.subTest(command=md.name):
                self.assertTrue(_frontmatter_description(md))

    def test_self_extends_code_and_grants_the_project_commands(self):
        # [self] = develops this launcher. A profession, not a specialty, on the
        # kind's own definition: it earns an IMAGE LAYER (the gate's toolchain,
        # fixing the deps-vanish-on-relaunch churn ISSUES.md records) and names a
        # domain of work — nothing about its running conditions is exceptional.
        # Nested under code/ because the gate also needs ruff, which [code] bakes.
        self_prof = self.reg.professions["self"]
        self.assertEqual(self_prof.requires, frozenset({"code"}))
        self.assertEqual(self_prof.commands,
                         ("ai_project-test", "ai_project-update-models"))
        dockerfile = (self_prof.path / "Dockerfile").read_text()
        for dep in ("prompt_toolkit", "python-dotenv", "rich", "mypy"):
            self.assertIn(dep, dockerfile)

    def test_the_self_addendum_names_the_gate_and_the_tracker(self):
        # The two entry points a launcher-developing agent must know; plus the
        # baked toolchain stated as AVAILABLE, with the why (runtime installs
        # don't survive) — so a session neither reinstalls out of habit nor
        # wonders why the image carries what it carries.
        _, body = self.reg.professions["self"].addendum
        self.assertIn("check.sh", body)
        self.assertIn("ISSUES.md", body)
        self.assertIn("already available", body)
        self.assertIn("don't survive", body)

    def test_the_legend_lists_every_declared_command(self):
        # Read from the declarations, not any directory — so it covers every
        # kind ([self] is a profession, {manager} a specialty).
        from launch.gui.menu_picker import _tag_commands
        found = {(tag.name, command) for tag, command, _ in _tag_commands(self.reg)}
        self.assertLessEqual({("manager", "/cowork"),
                              ("self", "/ai_project-test"),
                              ("self", "/ai_project-update-models")}, found)

    def test_a_tag_commands_description_comes_from_its_frontmatter(self):
        # So the legend cannot drift from what the command says about itself.
        from launch.gui.menu_picker import _tag_commands
        descriptions = {command: text for _, command, text in _tag_commands(self.reg)}
        self.assertTrue(descriptions["/cowork"],
                        "/cowork's description: frontmatter is missing or unparsed")

    def test_the_legend_renders_a_tag_commands_section(self):
        import re
        from launch.gui.menu_picker import _build_composition_legend
        plain = re.sub(r"\x1b\[[0-9;]*m", "", _build_composition_legend(self.reg))
        # "Tag Commands", not the earlier "Specialty Commands" — a profession
        # grants commands too now.
        self.assertIn("Tag Commands", plain)
        self.assertNotIn("Specialty Commands", plain)
        self.assertIn("/cowork", plain)
        self.assertIn("/ai_project-test", plain)

    def test_manager_addendum_teaches_the_control_channel(self):
        # The addendum IS the protocol documentation an agent gets; if the
        # control dir, a verb, or the quiet flag is renamed, this must fail.
        from launch.cowork.control import (
            CONTROL_SUBDIR, QUIET_FLAG, REPLIES_SUBDIR, _VERBS,
        )
        addendum = self.reg.specialties["manager"].addendum
        self.assertIsNotNone(addendum)
        _, body = addendum
        self.assertIn(f"/cowork/{CONTROL_SUBDIR}/", body)
        self.assertIn(f"{CONTROL_SUBDIR}/{REPLIES_SUBDIR}/", body)
        self.assertIn(QUIET_FLAG, body)
        for verb in _VERBS:
            self.assertIn(f"`{verb}", body)

    def test_code_installs_ruff_unconditionally(self):
        # Not a template.form toggle and not ARG-gated: Python is a given for
        # [code], and the linter a code agent runs on its own output should not
        # be re-installed per container. Kept in the failure-log shape so a
        # transient network fault warns instead of aborting the build.
        text = (self.reg.professions["code"].path / "Dockerfile").read_text()
        self.assertIn("uv tool install ruff", text)
        self.assertNotIn("ARG INSTALL_RUFF", text)     # unconditional by design
        self.assertNotIn("ruff", self.reg.professions["code"].load_toolkit())

    def test_professions_have_dockerfile(self):
        for name in ("code", "webdev"):
            self.assertTrue((self.reg.professions[name].path / "Dockerfile").is_file())

    def test_policies_discovered(self):
        self.assertLessEqual({"web-research", "no-sudo", "no-git"}, set(self.reg.policies))
        self.assertEqual(self.reg.policies["web-research"].label, "<+qry>")
        self.assertEqual(self.reg.policies["vcs-safe"].label, "<-gpush>")

    def test_no_git_denies_the_whole_family_not_just_push(self):
        # <-git> forbids ALL git via Bash — stage, commit, push, everything —
        # in both pattern spellings the engine honours (<-su> sets the shape).
        # <-gpush> stays the lighter option that still allows local commits.
        fragment = self.reg.policies["no-git"].load_fragment()
        self.assertEqual(fragment["permissions"]["deny"],
                         ["Bash(git *)", "Bash(git:*)"])
        self.assertEqual(self.reg.policies["no-git"].label, "<-git>")

    def test_the_doer_agents_carry_the_grant_umbrella(self):
        # The lego-level defaults chosen deliberately (TODO - cowork follow-up):
        # agents whose job is autonomous DOING get <+all> (or bash alone for the
        # web-less mathematician); watch-their-hands agents (refactorer,
        # strict-reviewer) and voice/persona agents (poet) deliberately do NOT —
        # a grant appearing on one of those is a decision, not a drift.
        expected = {"golem": ["all-actions"],
                    "bug-investigator": ["all-actions"],
                    "researcher": ["all-actions"],
                    "project-starter": ["plan-first", "all-actions"],
                    "mathematician": ["free-bash"],
                    "refactorer": ["vcs-safe"],
                    "strict-reviewer": [],
                    "poet": []}
        for agent, policies in expected.items():
            with self.subTest(agent=agent):
                build = load_lego(paths.AGENTS_DIR / f"{agent}.lego")
                self.assertEqual(list(build.policies), policies)

    def test_no_sudo_is_the_always_on_static_policy(self):
        # <-su> applies to every instance (install_settings merges it from
        # the registry); nothing else ships always-on, and no .lego lists it
        # (validate_build would reject that — covered by TestAgentLegoFiles).
        always_on = {n for n, p in self.reg.policies.items() if p.always_on}
        self.assertEqual(always_on, {"no-sudo"})


class TestAgentLegoFiles(unittest.TestCase):
    """Every agent has a `.lego`, and each references only real tags on the
    right axes. Validated against the discovered registry — a typo'd
    engine/profession name, or a tag listed on the wrong axis, fails here
    rather than at launch."""

    def setUp(self):
        self.reg = scan_all(paths.AGENTS_DIR)

    def test_every_agent_has_a_lego(self):
        for md in paths.AGENTS_DIR.glob("*.md"):
            name = md.stem
            with self.subTest(agent=name):
                self.assertTrue((paths.AGENTS_DIR / f"{name}.lego").is_file(),
                                f"agent {name!r} ({md.name}) has no {name}.lego")

    def test_every_lego_validates_against_registry(self):
        legos = sorted(paths.AGENTS_DIR.glob("*.lego"))
        self.assertTrue(legos, "no .lego files found")
        for lego in legos:
            with self.subTest(lego=lego.name):
                self.reg.validate_build(load_lego(lego), lego)   # raises on any bad reference


class TestFirewallSpecialtyArtifacts(unittest.TestCase):
    """{firewall} bind-mounts init-firewall.sh + firewall-entrypoint.sh into
    the container (declared in its tag.docker; sources existence-checked at
    scan time, but guard the shipped tree explicitly too)."""

    def setUp(self):
        self.fw = scan_all(paths.AGENTS_DIR).specialties["firewall"]

    def test_mount_sources_resolve_to_real_files(self):
        self.assertTrue(self.fw.docker.mounts)
        for source, _target in self.fw.docker.mounts:
            with self.subTest(path=str(source)):
                self.assertTrue(source.is_file(), f"mount source missing: {source}")

    def test_env_forward_carries_whitelist_and_selftest_addr(self):
        # init-firewall.sh consumes both: the whitelist via env (sudoers
        # env_keep) and the self-test address via $1 (entrypoint hand-off).
        self.assertEqual(tuple(self.fw.docker.env_forward),
                         ("WHITELIST_ADDRESSES", "FIREWALL_SELFTEST_ADDR"))

    def test_entrypoint_and_cap_declared(self):
        self.assertEqual(self.fw.docker.entrypoint, "firewall-entrypoint.sh")
        self.assertIn("NET_ADMIN", self.fw.docker.cap_add)

    def test_no_image_layer(self):
        # The firewall is run-time-only — iptables lives in the base image.
        self.assertIsNone(self.fw.layer)

    def test_auto_wants_firewall(self):
        auto = scan_all(paths.AGENTS_DIR).specialties["auto"]
        self.assertIn("firewall", dict(auto.wants))
        self.assertIn("--dangerously-skip-permissions", auto.claude_args)


class TestCodeToolkitManifest(unittest.TestCase):
    """[code]'s template.form — the configurable-installs manifest behind the
    "Edit Toolkits" menu. Guards the scope decisions: the form offers the
    LANGUAGE TOOLCHAINS only (all default on, matching the pre-toggle
    unconditional installs); the service CLIs are creds-driven and never
    manifest entries; every Dockerfile INSTALL_* ARG is owned by exactly one
    of the two mechanisms."""

    CREDS_CLI_KEYS = frozenset(
        name.upper() for name, (_, cli) in paths.OPTIONAL_CREDS_MOUNTS.items() if cli is not None
    )

    def setUp(self):
        self.code = scan_all(paths.AGENTS_DIR).professions["code"]

    def test_template_form_present(self):
        self.assertIsNotNone(self.code.toolkit_path)
        self.assertEqual(self.code.toolkit_path.name, "template.form")

    def test_offers_the_toolchains_plus_locked_python(self):
        self.assertEqual(set(self.code.load_toolkit()),
                         {"python", "rust", "node", "cmake", "go", "java", "kotlin", "ruby"})

    def test_python_is_locked_and_argless(self):
        # Python ships in the base image — shown so users know it's there,
        # but un-toggleable and not gated by any build-arg.
        python = self.code.load_toolkit()["python"]
        self.assertTrue(python.locked)
        self.assertEqual(python.build_arg, "")
        self.assertTrue(python.default)

    def test_pretoggle_toolchains_default_on_new_languages_off(self):
        # rust/node/cmake were unconditional before the form existed — their
        # defaults preserve that image; the languages added WITH the form
        # default off so nobody's image silently grows by ~1GB.
        entries = self.code.load_toolkit()
        for key in ("rust", "node", "cmake"):
            with self.subTest(tool=key):
                self.assertTrue(entries[key].default)
        for key in ("go", "java", "kotlin", "ruby"):
            with self.subTest(tool=key):
                self.assertFalse(entries[key].default)

    def test_manifest_disjoint_from_creds_clis(self):
        # A build-arg claimed by both would make two mechanisms fight over it.
        manifest_args = {e.build_arg for e in self.code.load_toolkit().values() if e.build_arg}
        self.assertFalse(manifest_args & {f"INSTALL_{k}" for k in self.CREDS_CLI_KEYS})

    def test_every_install_arg_owned_by_exactly_one_mechanism(self):
        # The drift-catcher: an INSTALL_* ARG that neither the manifest nor
        # creds-presence stages would silently keep its Dockerfile default
        # (never install); a manifest entry whose build_arg has no ARG would
        # no-op the toggle. Both must fail loud here, not surface as "I
        # toggled it and nothing changed" at launch time. (Locked entries
        # like python carry no build_arg — excluded.)
        dockerfile = (self.code.path / "Dockerfile").read_text()
        arg_names = set(re.findall(r"^ARG (INSTALL_\w+)=", dockerfile, re.MULTILINE))
        manifest_args = {e.build_arg for e in self.code.load_toolkit().values() if e.build_arg}
        creds_args = {f"INSTALL_{k}" for k in self.CREDS_CLI_KEYS}
        self.assertEqual(manifest_args | creds_args, arg_names)


if __name__ == "__main__":
    unittest.main()
