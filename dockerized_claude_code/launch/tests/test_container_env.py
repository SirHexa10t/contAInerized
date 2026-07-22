"""Tests for launch.container_env — env-var taxonomy, formatters, accumulator."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from launch import paths
from launch.container_env import (
    CONTAINER_ENV_FORWARDS, ContainerEnvKey, _container_env,
    conf_env_args, container_env_args, stage_container_env,
    install_creds_flags, staged_env, token_env_dict, toolkit_install_flags,
)
from launch.tags import Profession


# ============================================================
# ContainerEnvKey enum
# ============================================================


class TestContainerEnvKey(unittest.TestCase):
    def test_members_match_value(self):
        # str-subclass: member acts like its value in equality / dict keys
        self.assertEqual(ContainerEnvKey.DOCKER_GID, "DOCKER_GID")
        self.assertEqual(ContainerEnvKey.WHITELIST_ADDRESSES, "WHITELIST_ADDRESSES")

    def test_str_emits_value_not_enum_repr(self):
        # Critical for f-string flag emission in container_env_args /
        # docker_config's build_arg_flags.
        self.assertEqual(f"{ContainerEnvKey.DOCKER_GID}", "DOCKER_GID")

    def test_each_member_str_is_uppercase(self):
        for m in ContainerEnvKey:
            with self.subTest(key=m.name):
                self.assertEqual(str(m), str(m).upper())
                self.assertEqual(m.value, m.name)   # name == value by design

    def test_works_as_dict_key(self):
        d = {ContainerEnvKey.DOCKER_GID: "999"}
        # Lookup by enum member AND by raw string — both succeed because of str-subclass.
        self.assertEqual(d[ContainerEnvKey.DOCKER_GID], "999")
        self.assertEqual(d["DOCKER_GID"], "999")


# ============================================================
# stage_container_env + staged_env (accumulator)
# ============================================================


class ContainerEnvFixture(unittest.TestCase):
    """Snapshot + clear the module-level accumulator around each test."""

    def setUp(self):
        self._snapshot = dict(_container_env)
        _container_env.clear()

    def tearDown(self):
        _container_env.clear()
        _container_env.update(self._snapshot)


class TestStageContainerEnv(ContainerEnvFixture):
    def test_staged_value_visible_in_staged_env(self):
        stage_container_env(ContainerEnvKey.DOCKER_GID, "988")
        self.assertEqual(staged_env()["DOCKER_GID"], "988")

    def test_non_string_value_coerced_at_boundary(self):
        # The accumulator can hold Path/int; staged_env coerces to str.
        stage_container_env(ContainerEnvKey.BASH_ENV, Path("/home/claude/.bashrc"))
        self.assertEqual(staged_env()["BASH_ENV"], "/home/claude/.bashrc")

    def test_overwrite_replaces_prior_value(self):
        stage_container_env(ContainerEnvKey.DOCKER_GID, "first")
        stage_container_env(ContainerEnvKey.DOCKER_GID, "second")
        self.assertEqual(staged_env()["DOCKER_GID"], "second")

    def test_staged_env_holds_only_staged_entries(self):
        # No os.environ overlay — flags are emitted explicitly, so the host
        # env never leaks into build args / -e values by accident.
        stage_container_env(ContainerEnvKey.DOCKER_GID, "999")
        self.assertEqual(set(staged_env()), {"DOCKER_GID"})


# ============================================================
# Container-side emission lists + container_env_args
# ============================================================


class TestContainerEnvForwards(unittest.TestCase):
    def test_forwards_contains_agent_status_line(self):
        self.assertIn(ContainerEnvKey.AGENT_STATUS_LINE, CONTAINER_ENV_FORWARDS)

    def test_forwards_contains_bash_env(self):
        self.assertIn(ContainerEnvKey.BASH_ENV, CONTAINER_ENV_FORWARDS)

    def test_forwards_contains_each_token_env_var(self):
        for env_var in paths.OPTIONAL_CREDS_TOKEN_ENV_VARS.values():
            with self.subTest(env_var=env_var):
                self.assertIn(env_var, CONTAINER_ENV_FORWARDS)

    def test_whitelist_addresses_not_always_forwarded(self):
        # WHITELIST_ADDRESSES travels via {firewall}'s [run] env_forward —
        # unconditional emission would leak the address list into every
        # launch that happened to stage it.
        self.assertNotIn(ContainerEnvKey.WHITELIST_ADDRESSES, CONTAINER_ENV_FORWARDS)

    def test_container_emits_returns_only_flagged_members(self):
        for member in ContainerEnvKey.container_emits():
            with self.subTest(member=member.name):
                self.assertTrue(member.container_emit)
        for member in ContainerEnvKey:
            if not member.container_emit:
                with self.subTest(member=member.name):
                    self.assertNotIn(member, ContainerEnvKey.container_emits())


class TestContainerEnvArgs(ContainerEnvFixture):
    def test_forwarded_value_emitted_when_staged(self):
        stage_container_env(ContainerEnvKey.AGENT_STATUS_LINE, "STATUS")
        args = container_env_args()
        self.assertTrue(any(a == "AGENT_STATUS_LINE=STATUS" for a in args))

    def test_forwarded_value_skipped_when_not_staged(self):
        # CONTAINER_ENV_FORWARDS includes BASH_ENV + JIRA_API_TOKEN, but if
        # they aren't staged (set_container_env not called, no jira/token file),
        # the `-e` flag is silently dropped.
        args = container_env_args()
        self.assertFalse(any("BASH_ENV" in a for a in args))
        self.assertFalse(any("JIRA_API_TOKEN" in a for a in args))

    def test_args_alternate_flag_and_value(self):
        # Each entry is two adjacent strings: "-e" then "KEY=VALUE".
        stage_container_env(ContainerEnvKey.AGENT_STATUS_LINE, "S")
        stage_container_env(ContainerEnvKey.BASH_ENV, "/b")
        args = container_env_args()
        for i in range(0, len(args), 2):
            self.assertEqual(args[i], "-e")
            self.assertIn("=", args[i + 1])


# ============================================================
# install_creds_flags / token_env_dict
# ============================================================


class TestToolkitInstallFlags(unittest.TestCase):
    """toolkit_install_flags(professions) -> {INSTALL_<TOOL>: '1'|'0'} from
    each configurable profession's toolkit profile alone (manifest defaults
    when the profile file is missing). Service CLIs are NOT here — they're
    creds-driven via install_creds_flags below; test_essential_files guards
    that the two key sets stay disjoint."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        toolkit_path = Path(self.tmp.name) / "template.form"
        toolkit_path.write_text(
            '[rust]\ndescription = "d"\nrun_command = "cargo"\nlanguage = "c"\napprox_size_mb = 1\ndefault = true\nbuild_arg = "INSTALL_RUST"\n'
            '[cmake]\ndescription = "d"\nrun_command = "cmake"\nlanguage = "c"\napprox_size_mb = 1\ndefault = false\nbuild_arg = "INSTALL_CMAKE"\n'
        )
        self.profession = Profession(name="code", path=Path(self.tmp.name), toolkit_path=toolkit_path)
        self.bare_profession = Profession(name="web", path=Path(self.tmp.name))   # no template.form
        # Deliberately a path that's never written — load_profile falls back
        # to manifest defaults, so tests never touch the real
        # ~/.claude-agents/code_profile.toml.
        self.profile_path = Path(self.tmp.name) / "code_profile.toml"
        self._patch = patch("launch.container_env.toolkit_profile_path", lambda name: self.profile_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_no_profile_file_uses_manifest_defaults(self):
        flags = toolkit_install_flags([self.profession])
        self.assertEqual(flags, {"INSTALL_RUST": "1", "INSTALL_CMAKE": "0"})

    def test_explicit_profile_values_override_defaults(self):
        self.profile_path.write_text("rust = false\ncmake = true\n")
        flags = toolkit_install_flags([self.profession])
        self.assertEqual(flags, {"INSTALL_RUST": "0", "INSTALL_CMAKE": "1"})

    def test_profession_without_toolkit_contributes_nothing(self):
        self.assertEqual(toolkit_install_flags([self.bare_profession]), {})

    def test_multiple_professions_merge(self):
        flags = toolkit_install_flags([self.profession, self.bare_profession])
        self.assertEqual(set(flags), {"INSTALL_RUST", "INSTALL_CMAKE"})


class TestInstallCredsFlags(unittest.TestCase):
    """install_creds_flags(present_services) → {INSTALL_<TOOL>: '1'|'0'} for
    the service CLIs — creds-presence is the ONLY driver (the toolkit form
    deliberately does not offer these). One entry per OPTIONAL_CREDS_MOUNTS
    service with a cli_name; config-only entries (npmrc, pypirc) and the
    `home/` contents-mount get no flag."""

    def _installable_services(self):
        return [n for n, (_, cli) in paths.OPTIONAL_CREDS_MOUNTS.items() if cli is not None]

    def test_no_creds_all_zero(self):
        flags = install_creds_flags(set())
        for name in self._installable_services():
            with self.subTest(service=name):
                self.assertEqual(flags[f"INSTALL_{name.upper()}"], "0")

    def test_one_cred_flips_only_its_flag(self):
        flags = install_creds_flags({"gh"})
        self.assertEqual(flags["INSTALL_GH"], "1")
        self.assertEqual(flags["INSTALL_AWS"], "0")
        self.assertEqual(flags["INSTALL_KUBE"], "0")

    def test_cli_less_entries_get_no_flag(self):
        # npmrc / pypirc have cli=None — no INSTALL_<NAME> entry; the
        # `home/` contents-mount entry also has cli=None (and its slash
        # would make an invalid env-var name — the filter doubles as guard).
        flags = install_creds_flags({"npmrc", "pypirc", "home/"})
        self.assertNotIn("INSTALL_NPMRC", flags)
        self.assertNotIn("INSTALL_PYPIRC", flags)
        self.assertNotIn("INSTALL_HOME/", flags)

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
