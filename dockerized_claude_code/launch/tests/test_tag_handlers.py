"""Tests for launch.tag_handlers — apply_tags' two passes (declarative
tag.docker mount staging + dynamic handler dispatch) and its chain return.
(Tag selection + dangerous-combination warnings live in menu_picker's
checkbox form — tested in test_menu_picker.)

apply_tags has side effects via _apply_* handlers (filesystem caches,
DNS resolution, docker GID lookup) — tests patch each handler to verify
dispatch without actually running it. The Instance stand-in only needs
`.chain` + `.docker_contributions`: apply_tags reads those and passes the
object through to each handler untouched."""

import unittest
from pathlib import Path
from unittest.mock import patch

from launch import tag_handlers
from launch.tag_handlers import apply_tags
from launch.tags import DockerContribution


class _FakeInst:
    """Duck-typed Instance — apply_tags only touches `.chain` (the real
    property derives it from professions + specialties; here it's given)
    and `.docker_contributions` (tag.docker records; empty by default)."""
    def __init__(self, chain, contributions=()):
        self.chain = list(chain)
        self.docker_contributions = list(contributions)


class HandlersPatchedTestCase(unittest.TestCase):
    """Shared fixture: the three real handlers patched to mocks."""

    def setUp(self):
        self.mocks = {}
        for name in ("code", "dood", "firewall"):
            patcher = patch.object(tag_handlers, f"_apply_{name}")
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)


class TestApplyTagsReturn(HandlersPatchedTestCase):
    """apply_tags returns the chain it was given, as a list."""

    def test_base_only_chain(self):
        self.assertEqual(apply_tags(_FakeInst(["base"])), ["base"])

    def test_code_chain(self):
        self.assertEqual(apply_tags(_FakeInst(["base", "code"])), ["base", "code"])

    def test_full_chain_passthrough(self):
        chain = ["base", "code", "dood", "firewall"]
        self.assertEqual(apply_tags(_FakeInst(chain)), chain)


class TestApplyTagsDispatch(HandlersPatchedTestCase):
    """Handlers fire exactly when the matching tag is in the chain."""

    def test_no_handlers_fired_for_base_only(self):
        # "base" itself has no _apply_base — deliberately: the base image has
        # no launch-side side effects beyond existing.
        apply_tags(_FakeInst(["base"]))
        for mock in self.mocks.values():
            mock.assert_not_called()

    def test_code_handler_fires_for_code(self):
        apply_tags(_FakeInst(["base", "code"]))
        self.mocks["code"].assert_called_once()
        self.mocks["dood"].assert_not_called()
        self.mocks["firewall"].assert_not_called()

    def test_handlers_receive_the_instance(self):
        # Uniform handler signature: every _apply_* gets the Instance;
        # _apply_firewall reads .state_dir off it for the status-file location.
        inst = _FakeInst(["base", "firewall"])
        apply_tags(inst)
        self.mocks["firewall"].assert_called_once_with(inst)

    def test_all_three_fire_when_all_active(self):
        apply_tags(_FakeInst(["base", "code", "dood", "firewall"]))
        for mock in self.mocks.values():
            mock.assert_called_once()

    def test_handlers_fire_in_chain_order(self):
        order = []
        for name, mock in self.mocks.items():
            mock.side_effect = lambda inst, name=name: order.append(name)
        apply_tags(_FakeInst(["base", "code", "dood", "firewall"]))
        self.assertEqual(order, ["code", "dood", "firewall"])

    def test_code_handler_not_fired_for_firewall_only(self):
        # No [code] → no programming-toolchain caches are mounted, no
        # programming-image layer side effects. Critical guarantee: a
        # non-[code] agent never inherits [code]'s side effects.
        apply_tags(_FakeInst(["base", "firewall"]))
        self.mocks["code"].assert_not_called()

    def test_firewall_handler_not_fired_when_inactive(self):
        # Symmetric: a [code] agent without {firewall} doesn't trigger the
        # whitelist resolve.
        apply_tags(_FakeInst(["base", "code"]))
        self.mocks["firewall"].assert_not_called()


class TestDeclarativeMountStaging(HandlersPatchedTestCase):
    """apply_tags' first pass stages every tag.docker mount before any
    handler runs — the declarative half of a tag's launch contribution."""

    def test_contribution_mounts_staged(self):
        contribution = DockerContribution(
            mounts=((Path("/host/init-firewall.sh"), "/usr/local/bin/init-firewall.sh:ro"),),
        )
        staged = []
        with patch.object(tag_handlers, "add_docker_mount",
                          side_effect=lambda s, t: staged.append((str(s), str(t)))):
            apply_tags(_FakeInst(["base"], [contribution]))
        self.assertEqual(staged, [("/host/init-firewall.sh", "/usr/local/bin/init-firewall.sh:ro")])

    def test_no_contributions_stage_nothing(self):
        with patch.object(tag_handlers, "add_docker_mount") as mock_add:
            apply_tags(_FakeInst(["base", "code"]))
        mock_add.assert_not_called()   # [code]'s cache mounts come from its (patched) handler, not tag.docker


class TestHandlerlessTags(HandlersPatchedTestCase):
    """Tags without an `_apply_<name>` are a NO-OP by design — data-only tags
    ({auto} = claude_args + wants; [web]'s playwright cache rides [code]'s
    ~/.cache mount) need no code in this module, and a future tree-added tag
    must not crash the launcher."""

    def test_web_has_no_handler(self):
        self.assertFalse(hasattr(tag_handlers, "_apply_web"))

    def test_auto_has_no_handler(self):
        # Post-firewall-split, {auto} is pure data: its skip-permissions flag
        # rides Instance.claude_args and its firewall request rides [wants].
        self.assertFalse(hasattr(tag_handlers, "_apply_auto"))

    def test_handlerless_tag_is_skipped_silently(self):
        chain = ["base", "code", "web", "auto"]
        self.assertEqual(apply_tags(_FakeInst(chain)), chain)
        self.mocks["code"].assert_called_once()

    def test_unknown_future_tag_is_skipped_silently(self):
        self.assertEqual(apply_tags(_FakeInst(["base", "quantum"])), ["base", "quantum"])


if __name__ == "__main__":
    unittest.main()
