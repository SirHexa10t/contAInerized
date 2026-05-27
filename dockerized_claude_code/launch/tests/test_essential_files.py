"""Structural integrity: assert the files the launcher relies on actually
exist at the paths it computes. Catches "renamed/moved a file but missed a
reference" regressions immediately.

For each InstanceModifier modifier (except BASE), this also verifies the
modifier's Dockerfile, compose layer, and `_apply_<value>` handler are all
in place — so adding a new modifier requires all three to land at once
(otherwise the build will fail at runtime; here it fails at test time)."""

import unittest

from launch import agent_composition, paths
from launch.structs import InstanceModifiers


class TestRepoLayout(unittest.TestCase):
    def test_dockerized_root_exists(self):
        self.assertTrue(paths.DOCKERIZED_CLAUDE_ROOT.is_dir())

    def test_agents_dir_exists(self):
        self.assertTrue(paths.AGENTS_DIR.is_dir())

    def test_docker_dir_exists(self):
        self.assertTrue(paths.DOCKER_DIR.is_dir())

    def test_settings_dir_exists(self):
        self.assertTrue(paths.SETTINGS_DIR.is_dir())

    def test_templates_dir_exists(self):
        self.assertTrue(paths.TEMPLATES_DIR.is_dir())


class TestBaseDockerArtifacts(unittest.TestCase):
    def test_base_dockerfile_exists(self):
        self.assertTrue((paths.DOCKER_DIR / "Dockerfile").is_file())

    def test_base_compose_yml_exists(self):
        self.assertTrue(paths.COMPOSE_FILE_PATH.is_file())


class TestModifierArtifactsPresent(unittest.TestCase):
    """For every non-BASE modifier, the three files that compose its image
    layer must all exist: Dockerfile.<value>, compose.<value>.yml, and an
    `_apply_<value>` callable in agent_composition — all lowercased
    (matters for {DooD}, whose canonical value preserves the mixed case)."""

    def _non_base_modifiers(self):
        return [m for m in InstanceModifiers if m is not InstanceModifiers.BASE]

    def test_each_modifier_has_dockerfile(self):
        for m in self._non_base_modifiers():
            with self.subTest(modifier=m.value):
                df = paths.DOCKER_DIR / f"Dockerfile.{m.value.lower()}"
                self.assertTrue(df.is_file(), f"missing Dockerfile for {m.value}: {df}")

    def test_each_modifier_has_compose_layer(self):
        for m in self._non_base_modifiers():
            with self.subTest(modifier=m.value):
                layer = paths.compose_layer_path(m.value)
                self.assertTrue(layer.is_file(), f"missing compose layer for {m.value}: {layer}")

    def test_each_modifier_has_apply_handler(self):
        for m in self._non_base_modifiers():
            with self.subTest(modifier=m.value):
                handler_name = f"_apply_{m.value.lower()}"
                self.assertTrue(
                    hasattr(agent_composition, handler_name),
                    f"agent_composition is missing {handler_name}() for {m.value}",
                )
                self.assertTrue(
                    callable(getattr(agent_composition, handler_name)),
                    f"agent_composition.{handler_name} is not callable",
                )

    def test_base_has_no_dockerfile_suffix(self):
        # BASE is the un-suffixed base Dockerfile. Defensive: confirm there's no
        # `Dockerfile.base` (which would shadow the intended base layer).
        self.assertFalse((paths.DOCKER_DIR / "Dockerfile.base").exists())

    def test_base_has_no_apply_handler(self):
        # BASE has no side effects beyond being the starting image — no handler.
        self.assertFalse(hasattr(agent_composition, "_apply_base"))


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


class TestAutoModeAuxiliaryScripts(unittest.TestCase):
    """{auto} mode bind-mounts init-firewall.sh + auto-entrypoint.sh into the
    container (via DOCKER_AUTO_MOUNTS). Both must exist on disk on the host."""

    def test_auto_mounts_resolve_to_real_files(self):
        for host_path in paths.DOCKER_AUTO_MOUNTS:
            with self.subTest(path=str(host_path)):
                self.assertTrue(
                    host_path.is_file(),
                    f"DOCKER_AUTO_MOUNTS source missing: {host_path}",
                )


if __name__ == "__main__":
    unittest.main()
