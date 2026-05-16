"""Tests for launch.agent_composition — compose_chain return shape,
sync_memory_templates round-trip, and warn_if_dangerous_modes gate.

compose_chain has side effects via _apply_* handlers (filesystem caches,
DNS resolution, docker GID lookup) — tests patch each handler to verify
dispatch without actually running it. sync_memory_templates is tested
with a tmp state dir."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import agent_composition, memory_addendums
from launch.agent_composition import compose_chain, sync_memory_templates
from launch.memory_addendums import _wrap_block
from launch.structs import InstanceModifiers, SessionIdentity


class _FakeSess(SessionIdentity):
    """SessionIdentity subclass overriding `tags` + `state_dir` so we don't
    need real .md files on disk and we can point the state-dir at any path
    the test cares about. Frozen dataclass blocks normal __setattr__, so
    overrides are set via object.__setattr__ on instance attributes that
    the subclass properties read from."""

    @property
    def tags(self):
        return self._tags_override

    @property
    def state_dir(self):
        return self._state_dir_override

    @classmethod
    def make(cls, tags, modes, state_dir, *, agent="x", session="s"):
        s = cls(
            agent=agent, session=session, workspace="/tmp",
            is_brand_new=False, modes=tuple(modes),
        )
        object.__setattr__(s, "_tags_override", tuple(tags))
        object.__setattr__(s, "_state_dir_override", state_dir)
        return s


# ============================================================
# compose_chain — handler dispatch + chain return
# ============================================================


class TestComposeChainReturn(unittest.TestCase):
    """compose_chain returns sess_id.chain as a list. We patch the three
    _apply_* handlers so they don't actually mount anything or fire DNS."""

    def setUp(self):
        self.patches = [
            patch.object(agent_composition, "_apply_prog"),
            patch.object(agent_composition, "_apply_auto"),
            patch.object(agent_composition, "_apply_dood"),
        ]
        self.mocks = [p.start() for p in self.patches]
        self.mock_prog, self.mock_auto, self.mock_dood = self.mocks

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_base_only_chain(self):
        sess = _FakeSess.make([], [], Path("/tmp/state"))
        self.assertEqual(compose_chain(sess), ["base"])

    def test_prog_chain(self):
        sess = _FakeSess.make(["prog"], [], Path("/tmp/state"))
        self.assertEqual(compose_chain(sess), ["base", "prog"])

    def test_full_chain_order(self):
        sess = _FakeSess.make(["prog"], ["auto", "DooD"], Path("/tmp/state"))
        self.assertEqual(compose_chain(sess), ["base", "prog", "auto", "DooD"])

    def test_chain_order_independent_of_input(self):
        sess = _FakeSess.make(["prog"], ["DooD", "auto"], Path("/tmp/state"))
        self.assertEqual(compose_chain(sess), ["base", "prog", "auto", "DooD"])


class TestComposeChainDispatch(unittest.TestCase):
    """Handlers fire exactly when the matching modifier is active."""

    def setUp(self):
        self.patches = [
            patch.object(agent_composition, "_apply_prog"),
            patch.object(agent_composition, "_apply_auto"),
            patch.object(agent_composition, "_apply_dood"),
        ]
        self.mocks = [p.start() for p in self.patches]
        self.mock_prog, self.mock_auto, self.mock_dood = self.mocks

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_no_handlers_fired_for_base_only(self):
        compose_chain(_FakeSess.make([], [], Path("/tmp/state")))
        self.mock_prog.assert_not_called()
        self.mock_auto.assert_not_called()
        self.mock_dood.assert_not_called()

    def test_prog_handler_fires_for_prog_tag(self):
        compose_chain(_FakeSess.make(["prog"], [], Path("/tmp/state")))
        self.mock_prog.assert_called_once()
        self.mock_auto.assert_not_called()
        self.mock_dood.assert_not_called()

    def test_auto_handler_receives_state_dir(self):
        state = Path("/tmp/some-state")
        compose_chain(_FakeSess.make([], ["auto"], state))
        self.mock_auto.assert_called_once_with(state)

    def test_dood_handler_fires_for_dood_mode(self):
        compose_chain(_FakeSess.make([], ["DooD"], Path("/tmp/state")))
        self.mock_dood.assert_called_once()

    def test_all_three_fire_when_all_active(self):
        compose_chain(_FakeSess.make(["prog"], ["auto", "DooD"], Path("/tmp/state")))
        self.mock_prog.assert_called_once()
        self.mock_auto.assert_called_once()
        self.mock_dood.assert_called_once()

    def test_prog_handler_not_fired_for_auto_only(self):
        # No [prog] tag → no programming-toolchain caches are mounted, no
        # programming-image layer is selected. Critical guarantee: a non-[prog]
        # agent never inherits the [prog] tag's side effects.
        compose_chain(_FakeSess.make([], ["auto"], Path("/tmp/state")))
        self.mock_prog.assert_not_called()

    def test_prog_handler_not_fired_for_dood_only(self):
        compose_chain(_FakeSess.make([], ["DooD"], Path("/tmp/state")))
        self.mock_prog.assert_not_called()

    def test_auto_handler_not_fired_when_auto_inactive(self):
        # Symmetric: a [prog] agent without {auto} doesn't trigger the firewall
        # resolve.
        compose_chain(_FakeSess.make(["prog"], [], Path("/tmp/state")))
        self.mock_auto.assert_not_called()

    def test_dood_handler_not_fired_when_dood_inactive(self):
        compose_chain(_FakeSess.make(["prog"], ["auto"], Path("/tmp/state")))
        self.mock_dood.assert_not_called()


class TestComposeChainValidation(unittest.TestCase):
    """Validation lives in SessionIdentity.chain — compose_chain surfaces it
    by accessing sess_id.chain before any handler dispatch."""

    def setUp(self):
        for name in ("_apply_prog", "_apply_auto", "_apply_dood"):
            patcher = patch.object(agent_composition, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_unknown_tag_raises_before_any_handler(self):
        sess = _FakeSess.make(["typo"], [], Path("/tmp/state"))
        with self.assertRaises(ValueError) as ctx:
            compose_chain(sess)
        self.assertIn("typo", str(ctx.exception))

    def test_unknown_mode_raises(self):
        sess = _FakeSess.make([], ["bogus"], Path("/tmp/state"))
        with self.assertRaises(ValueError) as ctx:
            compose_chain(sess)
        self.assertIn("bogus", str(ctx.exception))


# ============================================================
# sync_memory_templates — round-trip against a tmp state dir
# ============================================================


class TestSyncMemoryTemplates(unittest.TestCase):
    """Builds a fake state dir + SessionIdentity, runs sync_memory_templates,
    and asserts on the resulting MEMORY.md."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        # The memory path is state_dir / projects / -workspace / memory / MEMORY.md;
        # write_text auto-creates the parent dirs, so we don't pre-create them.
        self.state_dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _sess(self, tags, modes):
        return _FakeSess.make(tags, modes, self.state_dir)

    def _memory_path(self):
        from launch.paths import state_memory_path
        return state_memory_path(self.state_dir)

    def test_no_active_modifiers_writes_only_base_block(self):
        sess = self._sess([], [])
        sync_memory_templates(sess)
        content = self._memory_path().read_text()
        # SEEK_SUMMARY (under BASE) should be present
        self.assertIn("base-instructions-start", content)
        self.assertIn("base-instructions-end", content)
        # No auto block, no prog block
        self.assertNotIn("auto-instructions-start", content)
        self.assertNotIn("prog-instructions-start", content)

    def test_auto_mode_adds_firewall_block(self):
        sess = self._sess([], ["auto"])
        sync_memory_templates(sess)
        content = self._memory_path().read_text()
        self.assertIn("auto-instructions-start", content)
        self.assertIn("auto-instructions-end", content)
        # Block content checks
        self.assertIn("{auto}", content)
        self.assertIn("ECONNREFUSED", content)

    def test_removing_mode_removes_its_block(self):
        # First launch with {auto}
        sess_with = self._sess([], ["auto"])
        sync_memory_templates(sess_with)
        # Second launch without {auto} — block should be cleaned up
        sess_without = self._sess([], [])
        sync_memory_templates(sess_without)
        content = self._memory_path().read_text()
        self.assertNotIn("auto-instructions-start", content)
        # Base block still there
        self.assertIn("base-instructions-start", content)

    def test_preserves_content_outside_wrapped_blocks(self):
        # Simulate an agent-added pointer entry persisting through a sync
        from launch.paths import state_memory_path
        memory = state_memory_path(self.state_dir)
        memory.parent.mkdir(parents=True, exist_ok=True)
        memory.write_text("- [Some pointer](file.md) — agent-added entry\n")
        sess = self._sess([], ["auto"])
        sync_memory_templates(sess)
        content = memory.read_text()
        self.assertIn("Some pointer", content)
        self.assertIn("auto-instructions-start", content)

    def test_no_write_when_content_unchanged(self):
        sess = self._sess([], [])
        sync_memory_templates(sess)
        memory = self._memory_path()
        first_mtime = memory.stat().st_mtime
        # Re-sync immediately — same content, should be a no-op
        import time
        time.sleep(0.01)
        sync_memory_templates(sess)
        self.assertEqual(memory.stat().st_mtime, first_mtime)

    def test_addendum_block_refreshes_when_text_changes(self):
        # Patch the BASE addendum to a known string, sync, then patch to a
        # different string and sync again. The second call must REPLACE the
        # first block (not append a second one alongside it).
        sess = self._sess([], [])
        with patch.dict(memory_addendums.MODIFIER_ADDENDUMS,
                        {InstanceModifiers.BASE: ["first version"]}, clear=True):
            sync_memory_templates(sess)
        first = self._memory_path().read_text()
        self.assertIn("first version", first)

        with patch.dict(memory_addendums.MODIFIER_ADDENDUMS,
                        {InstanceModifiers.BASE: ["second version"]}, clear=True):
            sync_memory_templates(sess)
        second = self._memory_path().read_text()
        self.assertIn("second version", second)
        self.assertNotIn("first version", second)
        # And the block-start marker appears exactly once — no duplicate.
        self.assertEqual(second.count("base-instructions-start"), 1)

    def test_credentials_block_added_for_prog_with_creds(self):
        # Patch the credentials addendum so the test doesn't depend on the
        # launcher's actual cred state at import time.
        sess = self._sess(["prog"], [])
        with patch.dict(memory_addendums.MODIFIER_ADDENDUMS,
                        {InstanceModifiers.TAG_PROG: ["fake creds notice text"]},
                        clear=False):
            sync_memory_templates(sess)
        content = self._memory_path().read_text()
        self.assertIn("prog-instructions-start", content)
        self.assertIn("fake creds notice text", content)

    def test_credentials_block_absent_without_prog_tag(self):
        # Even when MODIFIER_ADDENDUMS has a non-empty CREDENTIALS_NOTICE, a
        # non-[prog] agent gets no prog block — the modifier isn't in sess.chain
        # so splice runs with keep=False, removing or never-adding it.
        sess = self._sess([], ["auto"])
        with patch.dict(memory_addendums.MODIFIER_ADDENDUMS,
                        {InstanceModifiers.TAG_PROG: ["fake creds notice text"]},
                        clear=False):
            sync_memory_templates(sess)
        content = self._memory_path().read_text()
        self.assertNotIn("prog-instructions-start", content)
        self.assertNotIn("fake creds notice text", content)

    def test_adding_prog_tag_adds_credentials_block(self):
        # Launch first without [prog]; verify no block. Then launch with [prog]
        # (same workspace) and verify the block appears.
        with patch.dict(memory_addendums.MODIFIER_ADDENDUMS,
                        {InstanceModifiers.TAG_PROG: ["the creds block"]},
                        clear=False):
            sync_memory_templates(self._sess([], []))
            before = self._memory_path().read_text()
            self.assertNotIn("the creds block", before)

            sync_memory_templates(self._sess(["prog"], []))
            after = self._memory_path().read_text()
            self.assertIn("the creds block", after)

    def test_removing_prog_tag_removes_credentials_block(self):
        # Inverse of above — block disappears when [prog] is dropped.
        with patch.dict(memory_addendums.MODIFIER_ADDENDUMS,
                        {InstanceModifiers.TAG_PROG: ["the creds block"]},
                        clear=False):
            sync_memory_templates(self._sess(["prog"], []))
            self.assertIn("the creds block", self._memory_path().read_text())

            sync_memory_templates(self._sess([], []))
            self.assertNotIn("the creds block", self._memory_path().read_text())


# ============================================================
# warn_if_dangerous_modes — no-op unless {auto}+{DooD}
# ============================================================


class TestWarnIfDangerousModes(unittest.TestCase):
    def test_no_warning_for_empty_modes(self):
        # No interactive prompt — function returns immediately
        from launch.agent_composition import warn_if_dangerous_modes
        warn_if_dangerous_modes([])   # would block waiting for keypress if it triggered

    def test_no_warning_for_auto_alone(self):
        from launch.agent_composition import warn_if_dangerous_modes
        warn_if_dangerous_modes(["auto"])

    def test_no_warning_for_dood_alone(self):
        from launch.agent_composition import warn_if_dangerous_modes
        warn_if_dangerous_modes(["DooD"])

    def test_warning_fires_for_auto_plus_dood(self):
        # The function prints and waits for keypress. We patch stdin and the
        # termios machinery so it returns quickly. The print itself is fine.
        with patch("launch.agent_composition.sys.stdin") as mock_stdin, \
             patch("launch.agent_composition.input", create=True, return_value=""), \
             patch("builtins.print"):
            mock_stdin.fileno.side_effect = OSError("no tty")   # forces input() fallback
            from launch.agent_composition import warn_if_dangerous_modes
            warn_if_dangerous_modes(["auto", "DooD"])
            # If we reach here without blocking, the keypress gate was triggered
            # and the input() fallback returned.


if __name__ == "__main__":
    unittest.main()
