"""Tests for launch.agent_modifiers_handler — compose_chain return shape and
warn_if_dangerous_modes gate.

compose_chain has side effects via _apply_* handlers (filesystem caches,
DNS resolution, docker GID lookup) — tests patch each handler to verify
dispatch without actually running it."""

import unittest
from pathlib import Path
from unittest.mock import patch

from launch import agent_modifiers_handler
from launch.agent_modifiers_handler import compose_chain
from launch.structs import InstanceModifiers, InstanceIdentity


class _FakeInst(InstanceIdentity):
    """InstanceIdentity subclass overriding `tags` + `state_dir` so we don't
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
    """compose_chain returns inst_id.chain as a list. We patch the three
    _apply_* handlers so they don't actually mount anything or fire DNS."""

    def setUp(self):
        self.patches = [
            patch.object(agent_modifiers_handler, "_apply_code"),
            patch.object(agent_modifiers_handler, "_apply_auto"),
            patch.object(agent_modifiers_handler, "_apply_dood"),
        ]
        self.mocks = [p.start() for p in self.patches]
        self.mock_code, self.mock_auto, self.mock_dood = self.mocks

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_base_only_chain(self):
        sess = _FakeInst.make([], [], Path("/tmp/state"))
        self.assertEqual(compose_chain(sess), ["base"])

    def test_code_chain(self):
        sess = _FakeInst.make([InstanceModifiers.TAG_CODE], [], Path("/tmp/state"))
        self.assertEqual(compose_chain(sess), ["base", "code"])

    def test_full_chain_order(self):
        sess = _FakeInst.make([InstanceModifiers.TAG_CODE], [InstanceModifiers.MODE_WARN_AUTO, InstanceModifiers.MODE_WARN_DOOD], Path("/tmp/state"))
        self.assertEqual(compose_chain(sess), ["base", "code", "auto", "DooD"])

    def test_chain_order_independent_of_input(self):
        sess = _FakeInst.make([InstanceModifiers.TAG_CODE], [InstanceModifiers.MODE_WARN_DOOD, InstanceModifiers.MODE_WARN_AUTO], Path("/tmp/state"))
        self.assertEqual(compose_chain(sess), ["base", "code", "auto", "DooD"])


class TestComposeChainDispatch(unittest.TestCase):
    """Handlers fire exactly when the matching modifier is active."""

    def setUp(self):
        self.patches = [
            patch.object(agent_modifiers_handler, "_apply_code"),
            patch.object(agent_modifiers_handler, "_apply_auto"),
            patch.object(agent_modifiers_handler, "_apply_dood"),
        ]
        self.mocks = [p.start() for p in self.patches]
        self.mock_code, self.mock_auto, self.mock_dood = self.mocks

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_no_handlers_fired_for_base_only(self):
        compose_chain(_FakeInst.make([], [], Path("/tmp/state")))
        self.mock_code.assert_not_called()
        self.mock_auto.assert_not_called()
        self.mock_dood.assert_not_called()

    def test_code_handler_fires_for_code_tag(self):
        compose_chain(_FakeInst.make([InstanceModifiers.TAG_CODE], [], Path("/tmp/state")))
        self.mock_code.assert_called_once()
        self.mock_auto.assert_not_called()
        self.mock_dood.assert_not_called()

    def test_auto_handler_receives_state_dir(self):
        state = Path("/tmp/some-state")
        compose_chain(_FakeInst.make([], [InstanceModifiers.MODE_WARN_AUTO], state))
        self.mock_auto.assert_called_once_with(state)

    def test_dood_handler_fires_for_dood_mode(self):
        compose_chain(_FakeInst.make([], [InstanceModifiers.MODE_WARN_DOOD], Path("/tmp/state")))
        self.mock_dood.assert_called_once()

    def test_all_three_fire_when_all_active(self):
        compose_chain(_FakeInst.make([InstanceModifiers.TAG_CODE], [InstanceModifiers.MODE_WARN_AUTO, InstanceModifiers.MODE_WARN_DOOD], Path("/tmp/state")))
        self.mock_code.assert_called_once()
        self.mock_auto.assert_called_once()
        self.mock_dood.assert_called_once()

    def test_code_handler_not_fired_for_auto_only(self):
        # No [code] tag → no programming-toolchain caches are mounted, no
        # programming-image layer is selected. Critical guarantee: a non-[code]
        # agent never inherits the [code] tag's side effects.
        compose_chain(_FakeInst.make([], [InstanceModifiers.MODE_WARN_AUTO], Path("/tmp/state")))
        self.mock_code.assert_not_called()

    def test_code_handler_not_fired_for_dood_only(self):
        compose_chain(_FakeInst.make([], [InstanceModifiers.MODE_WARN_DOOD], Path("/tmp/state")))
        self.mock_code.assert_not_called()

    def test_auto_handler_not_fired_when_auto_inactive(self):
        # Symmetric: a [code] agent without {auto} doesn't trigger the firewall
        # resolve.
        compose_chain(_FakeInst.make([InstanceModifiers.TAG_CODE], [], Path("/tmp/state")))
        self.mock_auto.assert_not_called()

    def test_dood_handler_not_fired_when_dood_inactive(self):
        compose_chain(_FakeInst.make([InstanceModifiers.TAG_CODE], [InstanceModifiers.MODE_WARN_AUTO], Path("/tmp/state")))
        self.mock_dood.assert_not_called()


class TestComposeChainValidation(unittest.TestCase):
    """Validation lives in InstanceIdentity.chain — compose_chain surfaces it
    by accessing inst_id.chain before any handler dispatch."""

    def setUp(self):
        for name in ("_apply_code", "_apply_auto", "_apply_dood"):
            patcher = patch.object(agent_modifiers_handler, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_unknown_tag_unrepresentable(self):
        # Tags are typed enum members — AgentIdentity.tags converts each
        # filename string via from_value at the property boundary, which
        # raises ValueError on unknowns. By the time compose_chain accesses
        # inst_id.tags, every tag is a valid member; the chain itself no
        # longer needs a runtime tag-validation block.
        with self.assertRaises(ValueError):
            InstanceModifiers.from_value("typo")

    def test_unknown_mode_unrepresentable(self):
        # Modes are typed enum members — the JSON-load boundary
        # (InstanceModifiers(s)) raises ValueError before a InstanceIdentity
        # with an unknown mode can be constructed. The chain-level mode
        # validation block is therefore gone; only tag validation remains.
        with self.assertRaises(ValueError):
            InstanceModifiers("bogus")


# ============================================================
# warn_if_dangerous_modes — no-op unless {auto}+{DooD}
# ============================================================


class TestWarnIfDangerousModes(unittest.TestCase):
    def test_no_warning_for_empty_modes(self):
        # No interactive prompt — function returns immediately
        from launch.agent_modifiers_handler import warn_if_dangerous_modes
        warn_if_dangerous_modes([])   # would block waiting for keypress if it triggered

    def test_no_warning_for_auto_alone(self):
        from launch.agent_modifiers_handler import warn_if_dangerous_modes
        warn_if_dangerous_modes([InstanceModifiers.MODE_WARN_AUTO])

    def test_no_warning_for_dood_alone(self):
        from launch.agent_modifiers_handler import warn_if_dangerous_modes
        warn_if_dangerous_modes([InstanceModifiers.MODE_WARN_DOOD])

    def test_warning_fires_for_auto_plus_dood(self):
        # warn_if_dangerous_modes delegates to utils.prompt_keypress for each
        # matching MODIFIER_NOTICE_PROMPTS entry. We patch prompt_keypress so
        # we don't depend on tty / termios behaviour and can confirm dispatch.
        with patch("launch.agent_modifiers_handler.prompt_keypress") as mock_kp:
            from launch.agent_modifiers_handler import warn_if_dangerous_modes
            warn_if_dangerous_modes([InstanceModifiers.MODE_WARN_AUTO, InstanceModifiers.MODE_WARN_DOOD])
        mock_kp.assert_called_once()


if __name__ == "__main__":
    unittest.main()
