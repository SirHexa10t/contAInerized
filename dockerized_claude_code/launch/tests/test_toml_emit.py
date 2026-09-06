"""Tests for launch.toml_emit — the escaping rules both hand-written TOML
stores (`tags/store.py`, `cluster/state.py`) share.

The contract these tests hold the module to is not "emits this exact string"
but "emits something `tomllib` reads back unchanged" — that round trip is the
only property the stores actually depend on, and it stays true through any
future change of quoting style."""

import tomllib
import unittest

from launch import toml_emit


class TestKeyQuoting(unittest.TestCase):
    """TOML allows bare keys of letters/digits/underscore/dash; anything else
    has to be quoted or the file will not parse."""

    def test_ordinary_instance_and_member_ids_stay_bare(self):
        # The overwhelmingly common case — quoting these would churn every
        # line of both state files for nothing.
        for name in ("researcher", "researcher__primary", "web-dev", "a1_2-3"):
            with self.subTest(name=name):
                self.assertEqual(toml_emit.key(name), name)

    def test_anything_a_bare_key_cannot_hold_gets_quoted(self):
        for name in ("with.dot", "with space", "wîth-unicode", "with\"quote", ""):
            with self.subTest(name=name):
                emitted = toml_emit.key(name)
                self.assertNotEqual(emitted, name)
                self.assertTrue(emitted.startswith('"') and emitted.endswith('"'))

    def test_a_quoted_key_still_round_trips_to_the_original(self):
        # The point of quoting: the reader must get the name back verbatim.
        for name in ("with.dot", "with space", 'with"quote', "with\\backslash"):
            with self.subTest(name=name):
                parsed = tomllib.loads(f"[{toml_emit.key(name)}]\nx = 1\n")
                self.assertEqual(list(parsed), [name])


class TestStringEscaping(unittest.TestCase):
    def test_values_needing_escapes_round_trip(self):
        for value in ('has "quotes"', "has\\backslash", "has\ttab",
                      "has\nnewline", "héllo", "/plain/path", ""):
            with self.subTest(value=value):
                parsed = tomllib.loads(f"v = {toml_emit.string(value)}\n")
                self.assertEqual(parsed["v"], value)

    def test_a_plain_value_is_emitted_as_a_plain_basic_string(self):
        # Not a behavioural requirement, but the files are user-readable and
        # `project = "/home/u/proj"` should look like that.
        self.assertEqual(toml_emit.string("/home/u/proj"), '"/home/u/proj"')


class TestStringList(unittest.TestCase):
    """The `axis = [...]` line — the shape both stores use for tag axes."""

    def test_an_empty_axis_renders_as_an_empty_list_not_an_absent_key(self):
        # Load-bearing: an axis the user emptied must round-trip as empty.
        # An absent key means "legacy file" to both readers, which then fall
        # back to defaults — so omitting it would silently restore tags.
        self.assertEqual(toml_emit.string_list("policies", []), "policies = []")
        self.assertEqual(tomllib.loads(toml_emit.string_list("policies", []))["policies"], [])

    def test_values_come_back_in_order(self):
        line = toml_emit.string_list("specialties", ["muxer", "cluster", "cluster-cowork"])
        self.assertEqual(tomllib.loads(line)["specialties"],
                         ["muxer", "cluster", "cluster-cowork"])

    def test_it_accepts_any_iterable_not_just_lists(self):
        # Callers pass `entry.get(axis, [])`, tuples off a frozen build, and
        # generator expressions interchangeably.
        self.assertEqual(toml_emit.string_list("professions", ("code",)),
                         'professions = ["code"]')
        self.assertEqual(toml_emit.string_list("professions", (c for c in ("a", "b"))),
                         'professions = ["a", "b"]')

    def test_a_value_needing_escapes_is_escaped_inside_the_list(self):
        line = toml_emit.string_list("names", ['odd"name', "with space"])
        self.assertEqual(tomllib.loads(line)["names"], ['odd"name', "with space"])


class TestBothStoresShareOneDefinition(unittest.TestCase):
    """The regression this module exists to prevent: two byte-identical
    copies of the quoting rules, one edit away from two files that quote
    differently — a divergence neither file's own reader would notice."""

    def test_neither_store_defines_its_own_escaping_helpers(self):
        from launch.cluster import state
        from launch.tags import store
        for module in (store, state):
            with self.subTest(module=module.__name__):
                for name in ("_toml_key", "_toml_str", "_BARE_KEY_RE"):
                    self.assertFalse(hasattr(module, name),
                                     f"{module.__name__}.{name} is back — "
                                     f"use launch.toml_emit instead")


if __name__ == "__main__":
    unittest.main()
