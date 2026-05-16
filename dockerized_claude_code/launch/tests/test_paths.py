"""Tests for launch.paths — path constants, dict shapes, and path-builder lambdas.

These tests assert *structural* invariants (shapes, suffixes, naming
conventions) rather than absolute paths, since absolute paths depend on the
host's home directory and the repo's checkout location."""

import unittest
from pathlib import Path

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

    def test_resolved_domains_cache_at_agents_state_root(self):
        # The DNS cache lives at the AGENTS_STATE root (not under user_extras/).
        self.assertEqual(paths.RESOLVED_DOMAINS_CACHE_FILE, paths.AGENTS_STATE / "resolved_domains.txt")


class TestRepoDerivedConstants(unittest.TestCase):
    def test_dockerized_root_resolves(self):
        # DOCKERIZED_CLAUDE_ROOT is one above launch/, which contains paths.py
        self.assertEqual(paths.DOCKERIZED_CLAUDE_ROOT, Path(paths.__file__).resolve().parent.parent)

    def test_agents_dir(self):
        self.assertEqual(paths.AGENTS_DIR, paths.DOCKERIZED_CLAUDE_ROOT / "agents")

    def test_docker_dir(self):
        self.assertEqual(paths.DOCKER_DIR, paths.DOCKERIZED_CLAUDE_ROOT / "docker")

    def test_settings_dir(self):
        self.assertEqual(paths.SETTINGS_DIR, paths.DOCKERIZED_CLAUDE_ROOT / "settings")

    def test_templates_dir(self):
        self.assertEqual(paths.TEMPLATES_DIR, paths.DOCKERIZED_CLAUDE_ROOT / "launch" / "templates")


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

    def test_ssh_has_no_cli(self):
        # ssh contributes config to the system ssh — no separate CLI install.
        _, cli = paths.OPTIONAL_CREDS_MOUNTS["ssh"]
        self.assertIsNone(cli)

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
    def test_state_md_path(self):
        d = Path("/tmp/state")
        self.assertEqual(paths.state_md_path(d), d / paths.INSTANCE_CLAUDE_MD_FILENAME)

    def test_state_memory_path(self):
        d = Path("/tmp/state")
        self.assertEqual(paths.state_memory_path(d), d / paths.INSTANCE_MEMORY_FILE_RELPATH)

    def test_state_projects_path(self):
        d = Path("/tmp/state")
        self.assertEqual(paths.state_projects_path(d), d / paths.INSTANCE_PROJECTS_RELPATH)

    def test_state_skill_subdir_path(self):
        d = Path("/tmp/state")
        self.assertEqual(
            paths.state_skill_subdir_path(d, "my_skill"),
            d / paths.INSTANCE_SKILLS_RELPATH / "my_skill",
        )

    def test_state_domain_resolve_status_path(self):
        d = Path("/tmp/state")
        self.assertEqual(
            paths.state_domain_resolve_status_path(d),
            d / paths.DOMAINS_PENDING_RESOLVE_FILENAME,
        )

    def test_state_domain_resolve_status_for_container_path(self):
        # Same lambda works for the in-container claude-config dir; this is
        # how memory_addendums builds the in-container status file path.
        self.assertEqual(
            paths.state_domain_resolve_status_path(paths.CLAUDE_CONFIG_IN_CONTAINER),
            Path("/home/claude/.claude/domains_pending_resolve.yml"),
        )

    def test_instance_state_dir_path(self):
        self.assertEqual(
            paths.instance_state_dir_path("poet__draft"),
            paths.AGENTS_STATE / "poet__draft",
        )

    def test_workspace_skills_path(self):
        self.assertEqual(
            paths.workspace_skills_path("/my/workspace"),
            Path("/my/workspace") / paths.WORKSPACE_SKILLS_DIRNAME,
        )

    def test_optional_creds_service_path(self):
        self.assertEqual(
            paths.optional_creds_service_path("aws"),
            paths.OPTIONAL_CREDS_DIR / "aws",
        )

    def test_optional_creds_token_path(self):
        self.assertEqual(
            paths.optional_creds_token_path("jira"),
            paths.OPTIONAL_CREDS_DIR / "jira" / paths.OPTIONAL_CREDS_TOKEN_FILENAME,
        )

    def test_compose_layer_path_lowercases(self):
        # DooD mode → compose.dood.yml (lowercased)
        self.assertEqual(
            paths.compose_layer_path("DooD"),
            paths.DOCKER_DIR / "compose.dood.yml",
        )

    def test_compose_layer_path_already_lowercase(self):
        self.assertEqual(
            paths.compose_layer_path("prog"),
            paths.DOCKER_DIR / "compose.prog.yml",
        )

    def test_agent_conf_path(self):
        self.assertEqual(
            paths.agent_conf_path("poet"),
            paths.AGENTS_DIR / f"poet{paths.CONF_EXT}",
        )


if __name__ == "__main__":
    unittest.main()
