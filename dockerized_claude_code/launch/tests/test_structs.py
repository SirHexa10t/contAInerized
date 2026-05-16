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
        self.assertEqual(names, ["BASE", "TAG_PROG", "MODE_AUTO", "MODE_DOOD"])

    def test_base_value(self):
        self.assertEqual(InstanceModifiers.BASE.value, "base")

    def test_tag_prog_value(self):
        self.assertEqual(InstanceModifiers.TAG_PROG.value, "prog")

    def test_mode_auto_value(self):
        self.assertEqual(InstanceModifiers.MODE_AUTO.value, "auto")

    def test_mode_dood_value_preserves_case(self):
        # DooD's canonical form is mixed-case (CamelCase abbreviation)
        self.assertEqual(InstanceModifiers.MODE_DOOD.value, "DooD")

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
            [InstanceModifiers.MODE_AUTO, InstanceModifiers.MODE_DOOD],
        )

    def test_base_excluded_from_tags(self):
        self.assertNotIn(InstanceModifiers.BASE, InstanceModifiers.tags())

    def test_base_excluded_from_modes(self):
        self.assertNotIn(InstanceModifiers.BASE, InstanceModifiers.modes())

    def test_tag_values(self):
        self.assertEqual(InstanceModifiers.tag_values(), ("prog",))

    def test_mode_values(self):
        self.assertEqual(InstanceModifiers.mode_values(), ("auto", "DooD"))


class TestInstanceModifiersSlug(unittest.TestCase):
    def test_base_slug(self):
        self.assertEqual(InstanceModifiers.BASE.slug, "base")

    def test_tag_prog_slug(self):
        self.assertEqual(InstanceModifiers.TAG_PROG.slug, "prog")

    def test_mode_auto_slug(self):
        self.assertEqual(InstanceModifiers.MODE_AUTO.slug, "auto")

    def test_mode_dood_slug_lowercases(self):
        # slug lowercases the canonical value — DooD → dood
        self.assertEqual(InstanceModifiers.MODE_DOOD.slug, "dood")


class TestInstanceModifiersLabel(unittest.TestCase):
    def test_tag_label_uses_brackets(self):
        self.assertEqual(InstanceModifiers.TAG_PROG.label, "[prog]")

    def test_mode_label_uses_braces(self):
        self.assertEqual(InstanceModifiers.MODE_AUTO.label, "{auto}")

    def test_mode_dood_label_preserves_case(self):
        self.assertEqual(InstanceModifiers.MODE_DOOD.label, "{DooD}")

    def test_base_label_is_bare_value(self):
        # BASE has no decorative wrapping — it's never user-facing, but label
        # is reachable via the labels dict comprehension in format_prefix, so
        # it shouldn't render with misleading mode-style braces.
        self.assertEqual(InstanceModifiers.BASE.label, "base")


class TestFormatPrefix(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(InstanceModifiers.format_prefix([]), "")

    def test_single_tag(self):
        self.assertEqual(InstanceModifiers.format_prefix(["prog"]), "[prog] ")

    def test_single_mode(self):
        self.assertEqual(InstanceModifiers.format_prefix(["auto"]), "{auto} ")

    def test_tag_then_mode(self):
        self.assertEqual(InstanceModifiers.format_prefix(["prog", "auto"]), "[prog] {auto} ")

    def test_preserves_input_order(self):
        # Output reflects the input sequence, not enum declaration order.
        self.assertEqual(InstanceModifiers.format_prefix(["auto", "prog"]), "{auto} [prog] ")

    def test_unknown_falls_back_to_tag_style(self):
        # The docstring says unknowns get `[v]` rendering — handles typo'd filename tags
        self.assertEqual(InstanceModifiers.format_prefix(["typo"]), "[typo] ")

    def test_base_value_falls_back_to_unknown(self):
        # BASE isn't in the labels dict (excluded from format_prefix's
        # tag/mode-only construction), so "base" routes through the fallback.
        self.assertEqual(InstanceModifiers.format_prefix(["base"]), "[base] ")

    def test_mode_dood_label(self):
        self.assertEqual(InstanceModifiers.format_prefix(["DooD"]), "{DooD} ")


# ============================================================
# Identity dataclasses
# ============================================================


class TestSessionSep(unittest.TestCase):
    def test_separator(self):
        self.assertEqual(SESSION_SEP, "__")


class TestInstanceIdentityHelpers(unittest.TestCase):
    """Tests focused on properties/methods that don't require filesystem
    access (find_md_for_agent, etc.)."""

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
        self.assertEqual(self._sess(["prog"], []).chain, ("base", "prog"))

    def test_mode_appended(self):
        self.assertEqual(self._sess([], ["auto"]).chain, ("base", "auto"))

    def test_full_chain(self):
        self.assertEqual(
            self._sess(["prog"], ["auto", "DooD"]).chain,
            ("base", "prog", "auto", "DooD"),
        )

    def test_order_follows_declaration_not_input(self):
        # Even if modes come in as ("DooD", "auto"), chain enforces declaration order.
        self.assertEqual(
            self._sess(["prog"], ["DooD", "auto"]).chain,
            ("base", "prog", "auto", "DooD"),
        )

    def test_base_always_first(self):
        chain = self._sess(["prog"], ["auto"]).chain
        self.assertEqual(chain[0], "base")

    # --- validation ---

    def test_unknown_tag_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _ = self._sess(["typo"], []).chain
        self.assertIn("Unknown tag", str(ctx.exception))
        self.assertIn("typo", str(ctx.exception))

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _ = self._sess([], ["badmode"]).chain
        self.assertIn("Unknown mode", str(ctx.exception))
        self.assertIn("badmode", str(ctx.exception))

    def test_base_as_input_tag_is_rejected(self):
        # BASE is implicit — passing "base" as a tag is treated as unknown.
        with self.assertRaises(ValueError):
            _ = self._sess(["base"], []).chain

    def test_chain_returns_tuple(self):
        # Tuple (immutable) — signals "don't mutate this".
        self.assertIsInstance(self._sess(["prog"], ["auto"]).chain, tuple)


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
    Bad values (typo'd mode names, sundry case mistakes, dup entries) need to
    surface as a loud failure rather than a silent half-build.

    Uses a SessionIdentity subclass that overrides .tags so we don't need a
    real agent .md on disk — same pattern as TestSessionIdentityChain above."""

    def _sess(self, modes, tags=()):
        class _Sess(SessionIdentity):
            @property
            def tags(self):
                return tuple(tags)
        return _Sess(agent="x", session="s", workspace="/tmp",
                     is_brand_new=False, modes=tuple(modes))

    def test_unknown_mode_raises_with_listed_value(self):
        # The ValueError message should name the offending mode so the user can
        # find it in their modes map.
        with self.assertRaises(ValueError) as ctx:
            _ = self._sess(["badmode"]).chain
        self.assertIn("badmode", str(ctx.exception))

    def test_case_mismatch_is_caught(self):
        # `auto` is canonical; `Auto` / `AUTO` are unknown. Case sensitivity is
        # intentional — DooD's mixed-case name relies on case being meaningful.
        with self.assertRaises(ValueError):
            _ = self._sess(["Auto"]).chain
        with self.assertRaises(ValueError):
            _ = self._sess(["AUTO"]).chain

    def test_mixed_valid_and_unknown_still_raises(self):
        # One typo shouldn't slip through just because another mode is valid.
        with self.assertRaises(ValueError) as ctx:
            _ = self._sess(["auto", "typo"]).chain
        self.assertIn("typo", str(ctx.exception))

    def test_duplicate_modes_are_idempotent(self):
        # Duplicates in the input collapse via the set-based chain construction.
        # Same as if they appeared once.
        chain = self._sess(["auto", "auto"]).chain
        self.assertEqual(chain.count("auto"), 1)

    def test_empty_modes_list_yields_base_only_chain(self):
        # No modes ⇒ no mode-driven blocks; chain is just BASE.
        self.assertEqual(self._sess([]).chain, ("base",))


if __name__ == "__main__":
    unittest.main()
