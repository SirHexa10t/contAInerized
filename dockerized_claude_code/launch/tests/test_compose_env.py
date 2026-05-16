"""Tests for launch.compose_env — env-var taxonomy, formatters, accumulator."""

import os
import unittest
from unittest.mock import patch

from launch import paths
from launch.compose_env import (
    CONTAINER_ENV_FIXED, CONTAINER_ENV_FORWARDS, ComposeEnvKey, _compose_env,
    conf_env_args, container_env_args, install_creds_flags, stage_compose_env,
    subprocess_env, token_env_dict,
)


# ============================================================
# ComposeEnvKey enum
# ============================================================


class TestComposeEnvKey(unittest.TestCase):
    def test_members_match_value(self):
        # str-subclass: member acts like its value in equality / dict keys
        self.assertEqual(ComposeEnvKey.TARGET_IMAGE, "TARGET_IMAGE")
        self.assertEqual(ComposeEnvKey.AGENT_NAME, "AGENT_NAME")

    def test_str_emits_value_not_enum_repr(self):
        # Critical for f-string -e KEY=VALUE emission in container_env_args.
        self.assertEqual(f"{ComposeEnvKey.TARGET_IMAGE}", "TARGET_IMAGE")

    def test_each_member_str_is_uppercase(self):
        for m in ComposeEnvKey:
            with self.subTest(key=m.name):
                self.assertEqual(str(m), str(m).upper())
                self.assertEqual(m.value, m.name)   # name == value by design

    def test_works_as_dict_key(self):
        d = {ComposeEnvKey.TARGET_IMAGE: "claude-agents:base"}
        # Lookup by enum member AND by raw string — both succeed because of str-subclass.
        self.assertEqual(d[ComposeEnvKey.TARGET_IMAGE], "claude-agents:base")
        self.assertEqual(d["TARGET_IMAGE"], "claude-agents:base")


# ============================================================
# stage_compose_env + subprocess_env (accumulator)
# ============================================================


class TestStageComposeEnv(unittest.TestCase):
    def setUp(self):
        # Snapshot + clear; tests mutate this module-level dict.
        self._snapshot = dict(_compose_env)
        _compose_env.clear()

    def tearDown(self):
        _compose_env.clear()
        _compose_env.update(self._snapshot)

    def test_staged_value_visible_in_subprocess_env(self):
        stage_compose_env(ComposeEnvKey.TARGET_IMAGE, "claude-agents:prog")
        env = subprocess_env()
        self.assertEqual(env["TARGET_IMAGE"], "claude-agents:prog")

    def test_non_string_value_coerced_at_subprocess_boundary(self):
        # The accumulator can hold Path/int; subprocess_env coerces to str.
        from pathlib import Path
        stage_compose_env(ComposeEnvKey.DOCKERIZED_CLAUDE_ROOT, Path("/repo/root"))
        self.assertEqual(subprocess_env()["DOCKERIZED_CLAUDE_ROOT"], "/repo/root")

    def test_overwrite_replaces_prior_value(self):
        stage_compose_env(ComposeEnvKey.TARGET_IMAGE, "first")
        stage_compose_env(ComposeEnvKey.TARGET_IMAGE, "second")
        self.assertEqual(subprocess_env()["TARGET_IMAGE"], "second")

    def test_subprocess_env_overlays_os_environ(self):
        # Host env's keys are preserved (anything in os.environ also appears).
        stage_compose_env(ComposeEnvKey.AGENT_NAME, "poet")
        env = subprocess_env()
        self.assertEqual(env["AGENT_NAME"], "poet")
        # PATH is virtually always set
        self.assertIn("PATH", env)

    def test_compose_env_keys_win_over_host(self):
        # If a key is in both os.environ and _compose_env, the staged value wins
        with patch.dict(os.environ, {"TARGET_IMAGE": "from-host"}):
            stage_compose_env(ComposeEnvKey.TARGET_IMAGE, "from-launcher")
            self.assertEqual(subprocess_env()["TARGET_IMAGE"], "from-launcher")


# ============================================================
# Container-side emission lists + container_env_args
# ============================================================


class TestContainerEnvForwardsAndFixed(unittest.TestCase):
    def test_forwards_contains_agent_status_line(self):
        self.assertIn(ComposeEnvKey.AGENT_STATUS_LINE, CONTAINER_ENV_FORWARDS)

    def test_forwards_contains_each_token_env_var(self):
        for env_var in paths.OPTIONAL_CREDS_TOKEN_ENV_VARS.values():
            with self.subTest(env_var=env_var):
                self.assertIn(env_var, CONTAINER_ENV_FORWARDS)

    def test_fixed_sets_bash_env_to_bashrc(self):
        bash_env = CONTAINER_ENV_FIXED[ComposeEnvKey.BASH_ENV]
        self.assertEqual(str(bash_env), "/home/claude/.bashrc")


class TestContainerEnvArgs(unittest.TestCase):
    def setUp(self):
        self._snapshot = dict(_compose_env)
        _compose_env.clear()

    def tearDown(self):
        _compose_env.clear()
        _compose_env.update(self._snapshot)

    def test_fixed_entries_always_emitted(self):
        args = container_env_args()
        # BASH_ENV is in CONTAINER_ENV_FIXED — always present.
        self.assertIn("-e", args)
        self.assertTrue(any(a.startswith("BASH_ENV=") for a in args))

    def test_forwarded_value_emitted_when_staged(self):
        stage_compose_env(ComposeEnvKey.AGENT_STATUS_LINE, "STATUS")
        args = container_env_args()
        self.assertTrue(any(a == "AGENT_STATUS_LINE=STATUS" for a in args))

    def test_forwarded_value_skipped_when_not_staged(self):
        # CONTAINER_ENV_FORWARDS includes JIRA_API_TOKEN (via the token env-vars
        # set), but if no jira/token file exists, the value isn't staged and
        # the `-e` flag is silently dropped.
        args = container_env_args()
        self.assertFalse(any("JIRA_API_TOKEN" in a for a in args))

    def test_args_alternate_flag_and_value(self):
        # Each entry is two adjacent strings: "-e" then "KEY=VALUE".
        args = container_env_args()
        for i in range(0, len(args), 2):
            self.assertEqual(args[i], "-e")
            self.assertIn("=", args[i + 1])


# ============================================================
# install_creds_flags / token_env_dict (moved from test_docker_config.py)
# ============================================================


class TestInstallCredsFlags(unittest.TestCase):
    """install_creds_flags(present_services) → {INSTALL_<TOOL>: '1'|'0'} build-args.
    One entry per OPTIONAL_CREDS_MOUNTS service; '1' when in `present_services`."""

    def test_no_creds_all_zero(self):
        flags = install_creds_flags(set())
        for name in paths.OPTIONAL_CREDS_MOUNTS:
            with self.subTest(service=name):
                self.assertEqual(flags[f"INSTALL_{name.upper()}"], "0")

    def test_one_cred_flips_only_its_flag(self):
        flags = install_creds_flags({"gh"})
        self.assertEqual(flags["INSTALL_GH"], "1")
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
        flags = install_creds_flags({"bogus_service"})
        self.assertNotIn("INSTALL_BOGUS_SERVICE", flags)


class TestTokenEnvDict(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(token_env_dict({}), {})

    def test_jira_token(self):
        result = token_env_dict({"jira": "xyz123"})
        self.assertEqual(result, {"JIRA_API_TOKEN": "xyz123"})

    def test_unknown_service_dropped(self):
        result = token_env_dict({"aws": "shouldnt-appear"})
        self.assertEqual(result, {})

    def test_mix_known_and_unknown(self):
        result = token_env_dict({"jira": "good", "aws": "ignored"})
        self.assertEqual(result, {"JIRA_API_TOKEN": "good"})


# ============================================================
# conf_env_args
# ============================================================


class TestConfEnvArgs(unittest.TestCase):
    def test_empty_conf(self):
        self.assertEqual(conf_env_args({}), [])

    def test_one_entry(self):
        self.assertEqual(conf_env_args({"ANTHROPIC_MODEL": "claude-opus-4-7"}),
                         ["-e", "ANTHROPIC_MODEL=claude-opus-4-7"])

    def test_multiple_entries_pair_each_key(self):
        result = conf_env_args({"A": "1", "B": "2"})
        # Each conf key produces "-e KEY=VALUE" pair, in any order — verify shape.
        self.assertEqual(len(result), 4)
        self.assertEqual(result.count("-e"), 2)
        self.assertIn("A=1", result)
        self.assertIn("B=2", result)


if __name__ == "__main__":
    unittest.main()
