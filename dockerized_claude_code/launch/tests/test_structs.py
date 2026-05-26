"""Tests for launch.structs — InstanceModifiers taxonomy + identity dataclasses."""

import tempfile
import unittest
from pathlib import Path

from launch.structs import (
    InstanceIdentity, InstanceModifiers, SESSION_SEP, SessionIdentity,
)


class TestInstanceModifiersMembers(unittest.TestCase):
    """Members + their canonical attributes."""

    def test_expected_members(self):
        names = [m.name for m in InstanceModifiers]
        self.assertEqual(names, ["BASE", "TAG_PROG", "MODE_WARN_AUTO", "MODE_WARN_DOOD", "MODE_WEB"])

    def test_base_value(self):
        self.assertEqual(InstanceModifiers.BASE.value, "base")

    def test_tag_prog_value(self):
        self.assertEqual(InstanceModifiers.TAG_PROG.value, "prog")

    def test_mode_auto_value(self):
        self.assertEqual(InstanceModifiers.MODE_WARN_AUTO.value, "auto")

    def test_mode_dood_value_preserves_case(self):
        # DooD's canonical form is mixed-case (CamelCase abbreviation)
        self.assertEqual(InstanceModifiers.MODE_WARN_DOOD.value, "DooD")

    def test_each_has_description(self):
        for m in InstanceModifiers:
            self.assertTrue(m.description, f"{m.name} should have a non-empty description")


class TestInstanceModifiersSubsetViews(unittest.TestCase):
    """tags() / modes() / *_values() — BASE is excluded from both subset views."""

    def test_tags_only_prog(self):
        self.assertEqual(list(InstanceModifiers.tags()), [InstanceModifiers.TAG_PROG])

    def test_modes_in_declaration_order(self):
        self.assertEqual(
            list(InstanceModifiers.modes()),
            [InstanceModifiers.MODE_WARN_AUTO, InstanceModifiers.MODE_WARN_DOOD, InstanceModifiers.MODE_WEB],
        )

    def test_base_excluded_from_tags(self):
        self.assertNotIn(InstanceModifiers.BASE, InstanceModifiers.tags())

    def test_base_excluded_from_modes(self):
        self.assertNotIn(InstanceModifiers.BASE, InstanceModifiers.modes())

    def test_tag_values(self):
        self.assertEqual(InstanceModifiers.tag_values(), ("prog",))

    def test_mode_values(self):
        self.assertEqual(InstanceModifiers.mode_values(), ("auto", "DooD", "web"))


class TestInstanceModifiersSlug(unittest.TestCase):
    def test_base_slug(self):
        self.assertEqual(InstanceModifiers.BASE.slug, "base")

    def test_tag_prog_slug(self):
        self.assertEqual(InstanceModifiers.TAG_PROG.slug, "prog")

    def test_mode_auto_slug(self):
        self.assertEqual(InstanceModifiers.MODE_WARN_AUTO.slug, "auto")

    def test_mode_dood_slug_lowercases(self):
        # slug lowercases the canonical value — DooD → dood
        self.assertEqual(InstanceModifiers.MODE_WARN_DOOD.slug, "dood")


class TestInstanceModifiersLabel(unittest.TestCase):
    def test_tag_label_uses_brackets(self):
        self.assertEqual(InstanceModifiers.TAG_PROG.label, "[prog]")

    def test_mode_label_uses_braces(self):
        self.assertEqual(InstanceModifiers.MODE_WARN_AUTO.label, "{auto}")

    def test_mode_dood_label_preserves_case(self):
        self.assertEqual(InstanceModifiers.MODE_WARN_DOOD.label, "{DooD}")

    def test_base_label_is_bare_value(self):
        # BASE has no decorative wrapping — it's never user-facing, but label
        # is reachable via the labels dict comprehension in format_prefix, so
        # it shouldn't render with misleading mode-style braces.
        self.assertEqual(InstanceModifiers.BASE.label, "base")


class TestFormatPrefix(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(InstanceModifiers.format_prefix([]), "")

    def test_single_tag(self):
        self.assertEqual(InstanceModifiers.format_prefix([InstanceModifiers.TAG_PROG]), "[prog] ")

    def test_single_mode(self):
        self.assertEqual(InstanceModifiers.format_prefix([InstanceModifiers.MODE_WARN_AUTO]), "{auto} ")

    def test_tag_then_mode(self):
        self.assertEqual(
            InstanceModifiers.format_prefix([InstanceModifiers.TAG_PROG, InstanceModifiers.MODE_WARN_AUTO]),
            "[prog] {auto} ",
        )

    def test_preserves_input_order(self):
        # Output reflects the input sequence, not enum declaration order.
        self.assertEqual(
            InstanceModifiers.format_prefix([InstanceModifiers.MODE_WARN_AUTO, InstanceModifiers.TAG_PROG]),
            "{auto} [prog] ",
        )

    def test_mode_dood_label(self):
        self.assertEqual(InstanceModifiers.format_prefix([InstanceModifiers.MODE_WARN_DOOD]), "{DooD} ")


# ============================================================
# Identity dataclasses
# ============================================================


class TestSessionSep(unittest.TestCase):
    def test_separator(self):
        self.assertEqual(SESSION_SEP, "__")


class TestInstanceIdentityHelpers(unittest.TestCase):
    """Tests focused on properties/methods that don't require filesystem
    access or AGENT_MD_BY_NAME lookups."""

    def test_instance_name_static(self):
        self.assertEqual(InstanceIdentity.instance_name("poet", "draft"), "poet__draft")

    def test_state_dir_for_static(self):
        # Validates the path-building flow (state_dir_for → instance_name →
        # instance_state_dir_path); doesn't assert the host-specific path
        # prefix, just that the leaf is the instance id.
        path = InstanceIdentity.state_dir_for("poet", "draft")
        self.assertEqual(path.name, "poet__draft")

    def test_instance_property(self):
        inst = InstanceIdentity(agent="poet", session="draft", workspace="/tmp", is_brand_new=True)
        self.assertEqual(inst.instance, "poet__draft")

    def test_with_modes_returns_session_identity(self):
        inst = InstanceIdentity(agent="poet", session="draft", workspace="/tmp", is_brand_new=False)
        sess = inst.with_modes(["auto"])
        self.assertIsInstance(sess, SessionIdentity)
        self.assertEqual(sess.modes, ("auto",))
        # Carries through the InstanceIdentity fields
        self.assertEqual(sess.agent, "poet")
        self.assertEqual(sess.session, "draft")
        self.assertEqual(sess.workspace, "/tmp")
        self.assertFalse(sess.is_brand_new)


# ============================================================
# SessionIdentity.chain — the central validation + ordering property
# ============================================================


class TestSessionIdentityChain(unittest.TestCase):
    """SessionIdentity.chain validates tags/modes against the modifier taxonomy
    and returns them in InstanceModifiers declaration order, with BASE first.
    Constructed with AgentIdentity.tags overridden via a subclass since the
    real `tags` property reads the .md file — these tests don't touch disk."""

    def _sess(self, tags, modes):
        """Build a SessionIdentity whose `.tags` returns the given iterable
        instead of reading from a filename. Uses a tiny subclass override
        rather than mocking the underlying file-access path."""
        class _Sess(SessionIdentity):
            @property
            def tags(self):
                return tuple(tags)
        return _Sess(
            agent="x", session="s", workspace="/tmp",
            is_brand_new=False, modes=tuple(modes),
        )

    # --- chain composition ---

    def test_empty_chain_is_just_base(self):
        self.assertEqual(self._sess([], []).chain, ("base",))

    def test_tag_appended(self):
        self.assertEqual(self._sess([InstanceModifiers.TAG_PROG], []).chain, ("base", "prog"))

    def test_mode_appended(self):
        self.assertEqual(self._sess([], [InstanceModifiers.MODE_WARN_AUTO]).chain, ("base", "auto"))

    def test_full_chain(self):
        self.assertEqual(
            self._sess([InstanceModifiers.TAG_PROG], [InstanceModifiers.MODE_WARN_AUTO, InstanceModifiers.MODE_WARN_DOOD]).chain,
            ("base", "prog", "auto", "DooD"),
        )

    def test_order_follows_declaration_not_input(self):
        # Even if modes come in as (DOOD, AUTO), chain enforces declaration order.
        self.assertEqual(
            self._sess([InstanceModifiers.TAG_PROG], [InstanceModifiers.MODE_WARN_DOOD, InstanceModifiers.MODE_WARN_AUTO]).chain,
            ("base", "prog", "auto", "DooD"),
        )

    def test_base_always_first(self):
        chain = self._sess([InstanceModifiers.TAG_PROG], [InstanceModifiers.MODE_WARN_AUTO]).chain
        self.assertEqual(chain[0], "base")

    # --- validation moved to the property boundaries ---

    def test_unknown_tag_unrepresentable(self):
        # Tags are typed enum members — the property AgentIdentity.tags
        # converts each filename-grammar string via InstanceModifiers.from_value,
        # which raises ValueError on unknowns before SessionIdentity.chain
        # ever sees a "typo'd" tag. Same fail-fast contract as modes.
        with self.assertRaises(ValueError):
            InstanceModifiers.from_value("typo")

    def test_unknown_mode_unrepresentable(self):
        with self.assertRaises(ValueError):
            InstanceModifiers.from_value("badmode")

    def test_base_value_not_a_tag_or_mode(self):
        # BASE is implicit — looking it up via from_value succeeds (it IS a
        # valid InstanceModifiers value), but it's neither a tag nor a mode,
        # so chain construction excludes it from the tag/mode membership checks
        # and only emits it via the always-on first slot.
        self.assertIs(InstanceModifiers.from_value("base"), InstanceModifiers.BASE)
        self.assertEqual(self._sess([InstanceModifiers.BASE], []).chain, ("base",))

    def test_chain_returns_tuple(self):
        # Tuple (immutable) — signals "don't mutate this".
        self.assertIsInstance(self._sess([InstanceModifiers.TAG_PROG], [InstanceModifiers.MODE_WARN_AUTO]).chain, tuple)


# ============================================================
# validate_workspace — misconfigured workspace-map entries
# ============================================================


class TestValidateWorkspace(unittest.TestCase):
    """validate_workspace exits cleanly when a stored workspace path no longer
    resolves to a real directory (stale workspace-map entry from a deleted /
    renamed project). None passes through silently — resolve_target uses that
    to detect the missing-entry case and re-prompt."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _inst(self, workspace):
        return InstanceIdentity(agent="x", session="s", workspace=workspace, is_brand_new=False)

    def test_valid_directory_passes_silently(self):
        # Workspace is an existing directory → no exit.
        valid_dir = self.tmp / "valid"
        valid_dir.mkdir()
        self._inst(str(valid_dir)).validate_workspace()   # would raise SystemExit if invalid

    def test_none_workspace_passes_silently(self):
        # None is the "missing map entry" sentinel — caller (resolve_target)
        # decides to re-prompt; validate_workspace mustn't treat it as an error.
        self._inst(None).validate_workspace()

    def test_nonexistent_path_exits(self):
        nonexistent = self.tmp / "does-not-exist"
        with self.assertRaises(SystemExit):
            self._inst(str(nonexistent)).validate_workspace()

    def test_path_to_file_exits(self):
        # Workspace must be a directory — pointing it at a regular file is wrong.
        file_path = self.tmp / "a-file"
        file_path.write_text("not a directory")
        with self.assertRaises(SystemExit):
            self._inst(str(file_path)).validate_workspace()

    def test_empty_string_passes_silently(self):
        # Empty string is treated like None — passes through silently so the
        # downstream DEFAULT_WORKSPACE fallback (in set_container_mounts) can
        # take over. NOT a SystemExit, despite `Path("").is_dir()` spuriously
        # returning True (Python resolves empty path to cwd, which we
        # definitely don't want to bind-mount).
        self._inst("").validate_workspace()


# ============================================================
# Modes misconfiguration — additional edge cases beyond test_chain
# ============================================================


class TestModesMisconfiguration(unittest.TestCase):
    """Modes come from agent_modes_map.json — a JSON file the user can hand-edit.
    Bad string values surface as a loud failure at the JSON-load boundary
    (InstanceModifiers(s) raises ValueError on unknowns) rather than being
    silently absorbed into SessionIdentity.modes. These tests pin that contract.

    The chain-level mode validation block is gone (modes are typed enum members
    at construction time, so chain doesn't need to revalidate). Tag misconfig
    still validates at chain access time — that path is exercised by
    TestSessionIdentityChain above."""

    def test_unknown_mode_raises_with_listed_value(self):
        # InstanceModifiers("badmode") raises ValueError; the message names the
        # offending mode so the user can find it in their modes map.
        with self.assertRaises(ValueError) as ctx:
            InstanceModifiers("badmode")
        self.assertIn("badmode", str(ctx.exception))

    def test_case_mismatch_is_caught(self):
        # `auto` is canonical; `Auto` / `AUTO` are unknown. Case sensitivity is
        # intentional — DooD's mixed-case name relies on case being meaningful.
        with self.assertRaises(ValueError):
            InstanceModifiers("Auto")
        with self.assertRaises(ValueError):
            InstanceModifiers("AUTO")

    def test_mixed_valid_and_unknown_still_raises(self):
        # The typical load boundary: converting each string in turn — one bad
        # string raises mid-iteration, even if other strings are valid modes.
        with self.assertRaises(ValueError) as ctx:
            list(InstanceModifiers(s) for s in ["auto", "typo"])
        self.assertIn("typo", str(ctx.exception))

    def test_duplicate_modes_are_idempotent_in_chain(self):
        # Duplicates in the modes tuple collapse via set membership in chain
        # construction. Same as if they appeared once.
        class _Sess(SessionIdentity):
            @property
            def tags(self):
                return ()
        sess = _Sess(agent="x", session="s", workspace="/tmp", is_brand_new=False,
                     modes=(InstanceModifiers.MODE_WARN_AUTO, InstanceModifiers.MODE_WARN_AUTO))
        self.assertEqual(sess.chain.count("auto"), 1)

    def test_empty_modes_tuple_yields_base_only_chain(self):
        # No modes ⇒ no mode-driven blocks; chain is just BASE.
        class _Sess(SessionIdentity):
            @property
            def tags(self):
                return ()
        sess = _Sess(agent="x", session="s", workspace="/tmp", is_brand_new=False,
                     modes=())
        self.assertEqual(sess.chain, ("base",))


if __name__ == "__main__":
    unittest.main()
