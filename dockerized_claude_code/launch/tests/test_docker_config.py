"""Tests for launch.docker_config — chain naming + compose-file selection +
set_container_mounts (workspace fallback).

Env-formatter tests (install_creds_flags, token_env_dict, etc.) live in
test_compose_env.py since that's where the formatters were moved."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from launch import paths
from launch.docker_config import chain_compose_files, chain_image_tag, set_container_mounts


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


class TestSetContainerMountsWorkspaceFallback(unittest.TestCase):
    """Regression: set_container_mounts must never try to bind-mount a None
    workspace. If inst_id.workspace is None (stale workspace-map entry that
    slipped past resolve_target's re-prompt), fall back to DEFAULT_WORKSPACE."""

    def _capture_mounts(self, inst_id):
        """Drive set_container_mounts through a patched add_docker_mount that
        records every (source, target) pair. Returns the list of pairs in
        call order."""
        recorded = []
        with patch("launch.docker_config.add_docker_mount", side_effect=lambda s, t: recorded.append((str(s), str(t)))):
            set_container_mounts(inst_id)
        return recorded

    def test_workspace_set_uses_provided_path(self):
        inst_id = SimpleNamespace(workspace="/some/host/path", state_dir="/tmp/state")
        mounts = self._capture_mounts(inst_id)
        workspace_pair = next(p for p in mounts if p[1] == "/workspace")
        self.assertEqual(workspace_pair[0], "/some/host/path")

    def test_workspace_none_falls_back_to_default(self):
        inst_id = SimpleNamespace(workspace=None, state_dir="/tmp/state")
        mounts = self._capture_mounts(inst_id)
        workspace_pair = next(p for p in mounts if p[1] == "/workspace")
        self.assertEqual(workspace_pair[0], str(paths.DEFAULT_WORKSPACE))

    def test_workspace_empty_string_falls_back_to_default(self):
        # `or` covers None AND empty string — both treated as "no workspace".
        inst_id = SimpleNamespace(workspace="", state_dir="/tmp/state")
        mounts = self._capture_mounts(inst_id)
        workspace_pair = next(p for p in mounts if p[1] == "/workspace")
        self.assertEqual(workspace_pair[0], str(paths.DEFAULT_WORKSPACE))


if __name__ == "__main__":
    unittest.main()
