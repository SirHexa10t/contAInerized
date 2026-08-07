"""Tests for launch.paths — path constants, dict shapes, and path-builder lambdas.

These tests assert *structural* invariants (shapes, suffixes, naming
conventions) rather than absolute paths, since absolute paths depend on the
host's home directory and the repo's checkout location."""

import unittest
from pathlib import Path
from unittest.mock import patch

from launch import paths


class TestHomeDerivedConstants(unittest.TestCase):
    def test_home_matches_path_home(self):
        self.assertEqual(paths._HOME, Path.home())

    def test_agents_state_under_home(self):
        self.assertEqual(paths.AGENTS_STATE, Path.home() / ".claude-agents")

    def test_user_extras_under_agents_state(self):
        self.assertEqual(paths.USER_EXTRAS_DIR, paths.AGENTS_STATE / "user_extras")

    def test_optional_creds_dir_under_user_extras(self):
        self.assertEqual(paths.OPTIONAL_CREDS_DIR, paths.USER_EXTRAS_DIR / "optional_creds")

    def test_cache_root_under_agents_state(self):
        self.assertEqual(paths.CACHE_ROOT, paths.AGENTS_STATE / "cache")

    def test_firewall_whitelist_file_location(self):
        self.assertEqual(paths.FIREWALL_WHITELIST_FILE, paths.USER_EXTRAS_DIR / "firewall_whitelist.txt")

    def test_firewall_caches_grouped_under_firewall_cache_dir(self):
        # Both {firewall} host caches live in firewall_cache/ (flat), not loose
        # at the AGENTS_STATE root.
        self.assertEqual(paths.FIREWALL_CACHE_DIR, paths.AGENTS_STATE / "firewall_cache")
        self.assertEqual(paths.RESOLVED_DOMAINS_CACHE_FILE,
                         paths.FIREWALL_CACHE_DIR / "resolved_domains.txt")
        self.assertEqual(paths.cdn_ranges_cache_path("cloudflare"),
                         paths.FIREWALL_CACHE_DIR / "cloudflare.txt")


class TestRepoDerivedConstants(unittest.TestCase):
    def test_dockerized_root_resolves(self):
        # DOCKERIZED_CLAUDE_ROOT is one above launch/, which contains paths.py
        self.assertEqual(paths.DOCKERIZED_CLAUDE_ROOT, Path(paths.__file__).resolve().parent.parent)

    def test_agents_dir(self):
        self.assertEqual(paths.AGENTS_DIR, paths.DOCKERIZED_CLAUDE_ROOT / "agents")

    def test_base_dockerfile_at_root(self):
        self.assertEqual(paths.BASE_DOCKERFILE, paths.DOCKERIZED_CLAUDE_ROOT / "Dockerfile")

    def test_settings_dir(self):
        self.assertEqual(paths.SETTINGS_DIR, paths.DOCKERIZED_CLAUDE_ROOT / "settings")

    def test_template_files_dir(self):
        self.assertEqual(paths.TEMPLATE_FILES_DIR, paths.DOCKERIZED_CLAUDE_ROOT / "launch" / "template_files")


class TestContainerConstants(unittest.TestCase):
    def test_claude_home_in_container(self):
        self.assertEqual(paths.CLAUDE_HOME_IN_CONTAINER, Path("/home/claude"))

    def test_claude_config_in_container(self):
        self.assertEqual(paths.CLAUDE_CONFIG_IN_CONTAINER, Path("/home/claude/.claude"))

    def test_skills_in_container(self):
        self.assertEqual(paths.SKILLS_IN_CONTAINER, Path("/home/claude/.claude/skills"))

    def test_claude_summary_in_container(self):
        self.assertEqual(paths.CLAUDE_SUMMARY_IN_CONTAINER, Path("/workspace/.claude_summary"))


class TestOptionalCredsMounts(unittest.TestCase):
    """OPTIONAL_CREDS_MOUNTS values are `(container_path, cli_name_or_None)` tuples."""

    def test_all_values_are_pairs(self):
        for name, value in paths.OPTIONAL_CREDS_MOUNTS.items():
            with self.subTest(service=name):
                self.assertIsInstance(value, tuple)
                self.assertEqual(len(value), 2, f"{name}'s value should be (mount, cli)")

    def test_known_services_present(self):
        expected = {"aws", "gcloud", "kube", "ssh", "gh", "glab", "jira", "vercel", "railway", "npmrc", "pypirc"}
        self.assertTrue(expected.issubset(paths.OPTIONAL_CREDS_MOUNTS.keys()))

    def test_kube_maps_to_kubectl_cli(self):
        _, cli = paths.OPTIONAL_CREDS_MOUNTS["kube"]
        self.assertEqual(cli, "kubectl")

    def test_ssh_maps_to_ssh_cli(self):
        # cli_name is the agent-facing binary name (what shows up in the
        # CREDENTIALS_NOTICE addendum). Presence of the ssh cred dir also
        # triggers INSTALL_SSH=1 → Dockerfile.code apt-installs the
        # openssh-client package (the package name is hardcoded in the
        # Dockerfile, separate from this field).
        _, cli = paths.OPTIONAL_CREDS_MOUNTS["ssh"]
        self.assertEqual(cli, "ssh")

    def test_npmrc_has_no_cli(self):
        _, cli = paths.OPTIONAL_CREDS_MOUNTS["npmrc"]
        self.assertIsNone(cli)

    def test_pypirc_has_no_cli(self):
        _, cli = paths.OPTIONAL_CREDS_MOUNTS["pypirc"]
        self.assertIsNone(cli)

    def test_mounts_target_container_paths(self):
        for name, (mount, _) in paths.OPTIONAL_CREDS_MOUNTS.items():
            with self.subTest(service=name):
                self.assertTrue(
                    str(mount).startswith("/home/claude"),
                    f"{name}'s mount should be under /home/claude/, got {mount}",
                )


class TestOptionalCredsTokenEnvVars(unittest.TestCase):
    def test_keys_subset_of_mount_keys(self):
        # Each token-service must have a matching mount entry — both maps are
        # keyed by the same service name.
        self.assertTrue(
            set(paths.OPTIONAL_CREDS_TOKEN_ENV_VARS.keys()).issubset(paths.OPTIONAL_CREDS_MOUNTS.keys())
        )

    def test_jira_token_var(self):
        self.assertEqual(paths.OPTIONAL_CREDS_TOKEN_ENV_VARS["jira"], "JIRA_API_TOKEN")


# ============================================================
# Lambda path-builders
# ============================================================


class TestPathBuilderLambdas(unittest.TestCase):
    def test_state_workspace_jsonls_missing_dir_yields_empty(self):
        # Documented behaviour: glob on a missing dir returns an empty iterator,
        # which is what lets has_continuable_jsonl skip the is_dir() guard.
        self.assertEqual(list(paths.state_workspace_jsonls(Path("/tmp/definitely-missing"))), [])

    def test_state_workspace_jsonls_yields_jsonls_under_workspace_subdir(self):
        # Concrete-path assertion: the lambda must look under projects/-workspace/
        # specifically (not just projects/, and not the state dir root where
        # history.jsonl actually lives), and must filter by `.jsonl` extension.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            target = state / "projects" / "-workspace"
            target.mkdir(parents=True)
            (target / "abc-uuid.jsonl").touch()
            (target / "def-uuid.jsonl").touch()
            (target / "ignore.txt").touch()
            (state / "history.jsonl").touch()                              # actual location — must NOT be picked up
            (state / "projects" / "other-project").mkdir()
            (state / "projects" / "other-project" / "sneaky.jsonl").touch() # different project — must NOT be picked up

            found = {p.name for p in paths.state_workspace_jsonls(state)}
            self.assertEqual(found, {"abc-uuid.jsonl", "def-uuid.jsonl"})

    def test_state_domain_resolve_status_for_container_path(self):
        # Same lambda works for the in-container claude-config dir; this is
        # how memory_addendums builds the in-container status file path.
        self.assertEqual(
            paths.state_domain_resolve_status_path(paths.CLAUDE_CONFIG_IN_CONTAINER),
            Path("/home/claude/.claude/domains_pending_resolve.yml"),
        )

    def test_instances_dir(self):
        self.assertEqual(paths.instances_dir(), paths.AGENTS_STATE / "instances")

    def test_quickie_dirs(self):
        self.assertEqual(paths.quickie_dir(), paths.AGENTS_STATE / "quickie")
        self.assertEqual(paths.quickie_communal_workspace(), paths.AGENTS_STATE / "quickie" / "communal")
        self.assertEqual(paths.quickie_state_dir_path("abc"), paths.AGENTS_STATE / "quickie" / "abc")

    def test_instance_state_dir_path(self):
        self.assertEqual(
            paths.instance_state_dir_path("poet__draft"),
            paths.AGENTS_STATE / "instances" / "poet__draft",
        )

    def test_optional_creds_service_path(self):
        self.assertEqual(
            paths.optional_creds_service_path("aws"),
            paths.OPTIONAL_CREDS_DIR / "aws",
        )

class TestGroupHostingPaths(unittest.TestCase):
    """The {cowork} group-hosting builders. `group_key` is the one string that
    names a group in EVERY participant's tree, so its shape is load-bearing:
    the manager's own dir, each coworker's copy, and the inbox all derive from
    it, and group discovery scans for `session.json` rather than parsing names."""

    def test_the_root_follows_a_patched_state_dir(self):
        # The reason it is a builder rather than a constant: a module that imported
        # a constant would keep writing to the real state dir under test, silently.
        with patch.object(paths, "AGENTS_STATE", Path("/tmp/elsewhere")):
            self.assertEqual(paths.group_hosting_dir(), Path("/tmp/elsewhere/group_hosting"))
            self.assertEqual(paths.cowork_dir_path("poet__a").parent,
                             Path("/tmp/elsewhere/group_hosting"))

    def test_root_sits_under_agents_state(self):
        # Derived, not literal: a rename of the leaf still passes, while moving
        # the root out of the launcher's state dir still fails.
        self.assertEqual(paths.group_hosting_dir().parent, paths.AGENTS_STATE)

    def test_hub_state_is_outside_every_participant_dir(self):
        # Deliberate: agents may read their own session.json, never hub state.
        self.assertEqual(paths.hub_state_path().parent, paths.group_hosting_dir())

    def test_per_instance_dir_and_outbox(self):
        self.assertEqual(paths.cowork_dir_path("poet__a").parent, paths.group_hosting_dir())
        self.assertEqual(paths.cowork_outbox_path("poet__a").parent,
                         paths.cowork_dir_path("poet__a"))

    def test_group_key_composition(self):
        # The one assertion that SHOULD be literal: the `<manager>-<project>`
        # shape is load-bearing, because a manager identifies the groups it hosts
        # by its own id prefixing the dir name. Changing it must fail loudly.
        self.assertEqual(paths.group_key("planner__x", "edge_case_tests"),
                         "planner__x-edge_case_tests")

    def test_same_key_names_the_group_in_every_participant_tree(self):
        key = paths.group_key("planner__x", "edge_case_tests")
        self.assertEqual(paths.cowork_group_path("planner__x", key).name,
                         paths.cowork_group_path("golem__b", key).name)

    def test_inbox_is_a_sibling_of_the_group_dir(self):
        # Shares the group dir's prefix — which is why discovery keys on
        # session.json's presence instead of on the directory name.
        key = paths.group_key("planner__x", "proj")
        inbox = paths.cowork_inbox_path("planner__x", key, "golem__b")
        self.assertEqual(inbox.parent, paths.cowork_group_path("planner__x", key).parent)
        self.assertEqual(inbox.name, f"{key}{paths.INBOX_SEPARATOR}golem__b")

    def test_an_inbox_name_can_never_equal_a_group_name(self):
        # The two are siblings in one directory, so a collision would merge a
        # participant's own working copy with something it was sent. Group keys
        # are composed without the separator, inbox names always carry it —
        # which makes the collision structurally impossible rather than unlikely.
        key = paths.group_key("planner__x", "proj")
        self.assertNotIn(paths.INBOX_SEPARATOR, key)
        self.assertIn(paths.INBOX_SEPARATOR,
                      paths.cowork_inbox_path("planner__x", key, "golem__b").name)

    def test_the_same_builder_serves_both_directions(self):
        # Args are (owner, group, sender): the owner is whoever's tree the inbox
        # sits in, so one builder covers the manager's and the coworker's.
        key = paths.group_key("planner__x", "proj")
        to_manager = paths.cowork_inbox_path("planner__x", key, "golem__b")
        to_coworker = paths.cowork_inbox_path("golem__b", key, "planner__x")
        self.assertNotEqual(to_manager, to_coworker)
        self.assertEqual(to_manager.parent.name, "planner__x")
        self.assertEqual(to_coworker.parent.name, "golem__b")

    def test_group_state_files_live_in_the_group_dir(self):
        group_dir = paths.cowork_group_path("planner__x", paths.group_key("planner__x", "proj"))
        self.assertEqual(paths.group_session_path(group_dir).parent, group_dir)
        self.assertEqual(paths.group_conversation_path(group_dir).parent, group_dir)

    def test_container_target_is_outside_claude_config(self):
        # The design decision, not the spelling: the group dir must not squat
        # Claude Code's own ~/.claude namespace (projects/, skills/, todos/).
        self.assertTrue(paths.COWORK_IN_CONTAINER.is_absolute())
        self.assertNotIn(str(paths.CLAUDE_CONFIG_IN_CONTAINER), str(paths.COWORK_IN_CONTAINER))


if __name__ == "__main__":
    unittest.main()
