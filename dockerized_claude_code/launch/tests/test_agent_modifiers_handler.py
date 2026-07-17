"""Tests for launch.agent_modifiers_handler — compose_chain return shape and
handler dispatch. (Tag selection + dangerous-combination warnings live in
menu_picker's checkbox form — tested in test_menu_picker.)

compose_chain has side effects via _apply_* handlers (filesystem caches,
DNS resolution, docker GID lookup) — tests patch each handler to verify
dispatch without actually running it. The Instance stand-in only needs a
`.chain` attribute: compose_chain reads that and passes the object through
to each handler untouched."""

import unittest
from unittest.mock import patch

from launch import agent_modifiers_handler
from launch.agent_modifiers_handler import compose_chain


class _FakeInst:
    """Duck-typed Instance — compose_chain only touches `.chain` (the real
    property derives it from professions + specialties; here it's given)."""
    def __init__(self, chain):
        self.chain = list(chain)


class HandlersPatchedTestCase(unittest.TestCase):
    """Shared fixture: the three real handlers patched to mocks."""

    def setUp(self):
        self.mocks = {}
        for name in ("code", "auto", "dood"):
            patcher = patch.object(agent_modifiers_handler, f"_apply_{name}")
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)


class TestComposeChainReturn(HandlersPatchedTestCase):
    """compose_chain returns the chain it was given, as a list."""

    def test_base_only_chain(self):
        self.assertEqual(compose_chain(_FakeInst(["base"])), ["base"])

    def test_code_chain(self):
        self.assertEqual(compose_chain(_FakeInst(["base", "code"])), ["base", "code"])

    def test_full_chain_passthrough(self):
        chain = ["base", "code", "auto", "dood"]
        self.assertEqual(compose_chain(_FakeInst(chain)), chain)


class TestComposeChainDispatch(HandlersPatchedTestCase):
    """Handlers fire exactly when the matching tag is in the chain."""

    def test_no_handlers_fired_for_base_only(self):
        # "base" itself has no _apply_base — deliberately: the base image has
        # no launch-side side effects beyond existing.
        compose_chain(_FakeInst(["base"]))
        for mock in self.mocks.values():
            mock.assert_not_called()

    def test_code_handler_fires_for_code(self):
        compose_chain(_FakeInst(["base", "code"]))
        self.mocks["code"].assert_called_once()
        self.mocks["auto"].assert_not_called()
        self.mocks["dood"].assert_not_called()

    def test_handlers_receive_the_instance(self):
        # Uniform handler signature: every _apply_* gets the Instance;
        # _apply_auto reads .state_dir off it for the status-file location.
        inst = _FakeInst(["base", "auto"])
        compose_chain(inst)
        self.mocks["auto"].assert_called_once_with(inst)

    def test_all_three_fire_when_all_active(self):
        compose_chain(_FakeInst(["base", "code", "auto", "dood"]))
        for mock in self.mocks.values():
            mock.assert_called_once()

    def test_handlers_fire_in_chain_order(self):
        order = []
        for name, mock in self.mocks.items():
            mock.side_effect = lambda inst, name=name: order.append(name)
        compose_chain(_FakeInst(["base", "code", "auto", "dood"]))
        self.assertEqual(order, ["code", "auto", "dood"])

    def test_code_handler_not_fired_for_auto_only(self):
        # No [code] → no programming-toolchain caches are mounted, no
        # programming-image layer side effects. Critical guarantee: a
        # non-[code] agent never inherits [code]'s side effects.
        compose_chain(_FakeInst(["base", "auto"]))
        self.mocks["code"].assert_not_called()

    def test_auto_handler_not_fired_when_auto_inactive(self):
        # Symmetric: a [code] agent without {auto} doesn't trigger the
        # firewall resolve.
        compose_chain(_FakeInst(["base", "code"]))
        self.mocks["auto"].assert_not_called()


class TestHandlerlessTags(HandlersPatchedTestCase):
    """Tags without an `_apply_<name>` are a NO-OP by design — data-only tags
    ([web]'s playwright cache rides [code]'s ~/.cache mount) need no code in
    this module, and a future tree-added tag must not crash the launcher."""

    def test_web_has_no_handler(self):
        self.assertFalse(hasattr(agent_modifiers_handler, "_apply_web"))

    def test_handlerless_tag_is_skipped_silently(self):
        chain = ["base", "code", "web"]
        self.assertEqual(compose_chain(_FakeInst(chain)), chain)
        self.mocks["code"].assert_called_once()

    def test_unknown_future_tag_is_skipped_silently(self):
        self.assertEqual(compose_chain(_FakeInst(["base", "quantum"])), ["base", "quantum"])


if __name__ == "__main__":
    unittest.main()
