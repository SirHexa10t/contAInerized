"""Tests for launch.docker_config — chain naming, compose-file selection,
and the optional-creds → INSTALL_<TOOL> / env-var maps."""

import unittest

from launch import paths
from launch.docker_config import (
    chain_compose_files, chain_image_tag, install_creds_flags, token_env_dict,
)


class TestChainImageTag(unittest.TestCase):
    def test_base_only(self):
        self.assertEqual(chain_image_tag(["base"]), "claude-agents:base")

    def test_single_layer(self):
        self.assertEqual(chain_image_tag(["base", "prog"]), "claude-agents:prog")

    def test_two_layers_joined_with_dot(self):
        self.assertEqual(chain_image_tag(["base", "prog", "auto"]), "claude-agents:prog.auto")

    def test_dood_lowercased(self):
        # DooD's value is mixed-case; the tag form is lowercased to match
        # compose / Dockerfile filename conventions.
        self.assertEqual(
            chain_image_tag(["base", "prog", "auto", "DooD"]),
            "claude-agents:prog.auto.dood",
        )

    def test_just_dood_with_base(self):
        self.assertEqual(chain_image_tag(["base", "DooD"]), "claude-agents:dood")


class TestChainComposeFiles(unittest.TestCase):
    def test_base_only_uses_compose_yml(self):
        result = chain_compose_files(["base"])
        self.assertEqual(result, ["-f", str(paths.COMPOSE_FILE_PATH)])

    def test_appends_one_layer(self):
        result = chain_compose_files(["base", "prog"])
        self.assertEqual(result, [
            "-f", str(paths.COMPOSE_FILE_PATH),
            "-f", str(paths.compose_layer_path("prog")),
        ])

    def test_appends_multiple_layers_in_order(self):
        result = chain_compose_files(["base", "prog", "auto", "DooD"])
        self.assertEqual(result, [
            "-f", str(paths.COMPOSE_FILE_PATH),
            "-f", str(paths.compose_layer_path("prog")),
            "-f", str(paths.compose_layer_path("auto")),
            "-f", str(paths.compose_layer_path("DooD")),   # compose_layer_path lowercases internally
        ])

    def test_dood_layer_uses_lowercased_filename(self):
        # Spot-check: compose_layer_path("DooD") → compose.dood.yml, not compose.DooD.yml.
        result = chain_compose_files(["base", "DooD"])
        self.assertIn(str(paths.DOCKER_DIR / "compose.dood.yml"), result)


class TestInstallCredsFlags(unittest.TestCase):
    """install_creds_flags(present_services) → {INSTALL_<TOOL>: '1'|'0'} build-args.
    One entry per OPTIONAL_CREDS_MOUNTS service; '1' when in `present_services`."""

    def test_no_creds_all_zero(self):
        flags = install_creds_flags(set())
        # Every service represented; every value is '0'
        for name in paths.OPTIONAL_CREDS_MOUNTS:
            with self.subTest(service=name):
                self.assertEqual(flags[f"INSTALL_{name.upper()}"], "0")

    def test_one_cred_flips_only_its_flag(self):
        flags = install_creds_flags({"gh"})
        self.assertEqual(flags["INSTALL_GH"], "1")
        # Spot-check that an unrelated flag stayed at 0
        self.assertEqual(flags["INSTALL_AWS"], "0")
        self.assertEqual(flags["INSTALL_KUBE"], "0")

    def test_multiple_creds_set_independently(self):
        flags = install_creds_flags({"aws", "kube"})
        self.assertEqual(flags["INSTALL_AWS"], "1")
        self.assertEqual(flags["INSTALL_KUBE"], "1")
        self.assertEqual(flags["INSTALL_GH"], "0")

    def test_keys_are_uppercased_service_names(self):
        flags = install_creds_flags(set())
        for name in paths.OPTIONAL_CREDS_MOUNTS:
            self.assertIn(f"INSTALL_{name.upper()}", flags)

    def test_unknown_services_dont_create_flags(self):
        # Even if `present_services` contains something not in OPTIONAL_CREDS_MOUNTS,
        # only known services produce flags.
        flags = install_creds_flags({"bogus_service"})
        self.assertNotIn("INSTALL_BOGUS_SERVICE", flags)


class TestTokenEnvDict(unittest.TestCase):
    """token_env_dict({service: token}) → {env_var: token}, translating each
    service via OPTIONAL_CREDS_TOKEN_ENV_VARS."""

    def test_empty(self):
        self.assertEqual(token_env_dict({}), {})

    def test_jira_token(self):
        result = token_env_dict({"jira": "xyz123"})
        self.assertEqual(result, {"JIRA_API_TOKEN": "xyz123"})

    def test_unknown_service_dropped(self):
        # service not in OPTIONAL_CREDS_TOKEN_ENV_VARS → silently skipped
        result = token_env_dict({"aws": "shouldnt-appear"})
        self.assertEqual(result, {})

    def test_mix_known_and_unknown(self):
        result = token_env_dict({"jira": "good", "aws": "ignored"})
        self.assertEqual(result, {"JIRA_API_TOKEN": "good"})


if __name__ == "__main__":
    unittest.main()
