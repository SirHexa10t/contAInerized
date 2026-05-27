"""Tests for launch.user_additions — `optional_creds_mounts` covers both
key shapes (whole-mount + trailing-`/` contents-mount).

Each test isolates the docker_config._docker_mounts accumulator and patches
optional_creds_service_path so the real ~/.claude-agents state is never
touched."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import docker_config, user_additions


class _UserAdditionsBase(unittest.TestCase):
    """Shared setup: tmp `optional_creds/` root + cleared mount accumulator."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.optional_creds = Path(self.tmpdir.name) / "optional_creds"
        self.optional_creds.mkdir()
        docker_config._docker_mounts.clear()
        # `optional_creds_service_path` joins OPTIONAL_CREDS_DIR with `name`;
        # patching it lets us redirect lookups into our tmp dir. We also
        # strip any trailing `/` so the contents-mount key "home/" resolves
        # to `<tmp>/home`.
        self._patch_path = patch(
            "launch.user_additions.optional_creds_service_path",
            lambda name: self.optional_creds / name.rstrip("/"),
        )
        self._patch_path.start()

    def tearDown(self):
        self._patch_path.stop()
        docker_config._docker_mounts.clear()
        self.tmpdir.cleanup()

    def _staged(self) -> dict[str, str]:
        return dict(docker_config._docker_mounts)


# ============================================================
# optional_creds_mounts — trailing-`/` contents-mount entry (home/)
# ============================================================


class TestOptionalCredsMountsHomeOverlay(_UserAdditionsBase):
    """Keys ending with `/` mount each top-level child of the source dir
    individually into the target dir. Top-level only — subdirs aren't
    walked. Clashes with previously-staged mounts raise RuntimeError."""

    def setUp(self):
        super().setUp()
        self.home = self.optional_creds / "home"
        self.home.mkdir()
        # Patch the OPTIONAL_CREDS_MOUNTS dict to ONLY contain the home/ entry
        # for these tests, so we don't accidentally pull in real services.
        self._patch_mounts = patch.dict(
            "launch.user_additions.OPTIONAL_CREDS_MOUNTS",
            {"home/": ("/home/claude/", None)},
            clear=True,
        )
        self._patch_mounts.start()
        self._patch_present = patch(
            "launch.user_additions.present_optional_cred_services",
            return_value=frozenset({"home/"}),
        )
        self._patch_present.start()

    def tearDown(self):
        self._patch_mounts.stop()
        self._patch_present.stop()
        super().tearDown()

    def test_top_level_file_becomes_file_mount(self):
        gitconfig = self.home / ".gitconfig"
        gitconfig.write_text("[user]\n  name = X\n")
        names = user_additions.optional_creds_mounts()
        self.assertEqual(names, ["home"])   # trailing `/` stripped for banner
        self.assertEqual(self._staged(), {str(gitconfig): "/home/claude/.gitconfig"})

    def test_top_level_dir_becomes_dir_mount(self):
        gnupg = self.home / ".gnupg"
        gnupg.mkdir()
        (gnupg / "pubring.kbx").write_text("")
        user_additions.optional_creds_mounts()
        self.assertEqual(self._staged(), {str(gnupg): "/home/claude/.gnupg"})

    def test_subdir_files_not_walked(self):
        # home/.config/git/config exists, but only `.config` (the top-level
        # entry) gets mounted — as a whole dir. The git config below stays
        # under the .config/ dir mount, not as a separate file mount.
        config_dir = self.home / ".config"
        (config_dir / "git").mkdir(parents=True)
        (config_dir / "git" / "config").write_text("")
        user_additions.optional_creds_mounts()
        self.assertEqual(self._staged(), {str(config_dir): "/home/claude/.config"})

    def test_clash_with_existing_mount_raises(self):
        # A launcher-side mount at /home/claude/.bashrc is already staged.
        docker_config.add_docker_mount("/host/settings/bashrc.sh", "/home/claude/.bashrc")
        (self.home / ".bashrc").write_text("")
        with self.assertRaises(RuntimeError) as ctx:
            user_additions.optional_creds_mounts()
        self.assertIn(".bashrc", str(ctx.exception))
        self.assertIn("/home/claude/.bashrc", str(ctx.exception))


# ============================================================
# optional_creds_mounts — regular (whole-mount) entries
# ============================================================


class TestOptionalCredsMountsWholeMount(_UserAdditionsBase):
    """Regular entries (no trailing `/` on the key) mount the source dir / file
    as a whole at the target path declared in OPTIONAL_CREDS_MOUNTS. Tests use
    the real OPTIONAL_CREDS_MOUNTS table (not a patched one) so the
    source→target dispatch we actually ship is exercised."""

    def _stage(self, services):
        """Helper: create tmp src dirs/files for each present service, patch
        present_optional_cred_services to report them, run the mount stager."""
        for name in services:
            src = self.optional_creds / name
            # Real npmrc/pypirc are files; everything else is a directory.
            # Match for realism — present_optional_cred_services only checks
            # existence (not kind), so either works for the lookup itself.
            if name in {"npmrc", "pypirc"}:
                src.write_text("")
            else:
                src.mkdir()
        with patch("launch.user_additions.present_optional_cred_services",
                   return_value=frozenset(services)):
            return user_additions.optional_creds_mounts()

    def test_dir_service_mounted_at_declared_target(self):
        from launch.paths import OPTIONAL_CREDS_MOUNTS
        names = self._stage({"aws"})
        target = OPTIONAL_CREDS_MOUNTS["aws"][0]
        self.assertEqual(self._staged(), {str(self.optional_creds / "aws"): target})
        self.assertEqual(names, ["aws"])

    def test_file_service_mounted_at_declared_target(self):
        # `npmrc` is a file-mount entry (target is a file path, not a dir).
        from launch.paths import OPTIONAL_CREDS_MOUNTS
        names = self._stage({"npmrc"})
        target = OPTIONAL_CREDS_MOUNTS["npmrc"][0]
        self.assertEqual(self._staged(), {str(self.optional_creds / "npmrc"): target})
        self.assertEqual(names, ["npmrc"])

    def test_multiple_services_all_staged(self):
        from launch.paths import OPTIONAL_CREDS_MOUNTS
        names = self._stage({"aws", "gh", "kube"})
        staged = self._staged()
        for svc in ("aws", "gh", "kube"):
            self.assertEqual(staged[str(self.optional_creds / svc)],
                             OPTIONAL_CREDS_MOUNTS[svc][0])
        self.assertEqual(set(names), {"aws", "gh", "kube"})

    def test_absent_services_not_staged(self):
        names = self._stage({"aws"})   # only aws present
        self.assertEqual(len(self._staged()), 1)
        self.assertEqual(names, ["aws"])


# ============================================================
# optional_creds_mounts — the ssh perm-enforcement hook
# ============================================================


class TestOptionalCredsMountsSshPerms(_UserAdditionsBase):
    """`optional_creds_mounts` runs `enforce_ssh_dir_perms` on the ssh entry
    before staging the mount. We patch present_optional_cred_services so the
    tmp dir's ssh/ subdir counts as present."""

    def test_ssh_entry_perms_get_fixed(self):
        ssh = self.optional_creds / "ssh"
        ssh.mkdir(mode=0o755)   # deliberately wrong
        key = ssh / "id_ed25519"
        key.write_text("")
        key.chmod(0o644)        # deliberately too loose
        with patch("launch.user_additions.present_optional_cred_services",
                   return_value=frozenset({"ssh"})):
            user_additions.optional_creds_mounts()
        self.assertEqual(ssh.stat().st_mode & 0o777, 0o700)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
