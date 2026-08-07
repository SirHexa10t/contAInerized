"""Tests for launch.cowork.journal — the per-group conversation log.

Bodies here deliberately contain markdown (headings, rules, fences) and blank
lines, because the log quotes agent prose the hub does not control: the format
has to survive a body that would otherwise restructure the document it lands in.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from launch.cowork import group as grp
from launch.cowork.journal import (
    Direction, append, format_entry, journal_path, read_journal,
)
from launch.cowork.group import Session


def _session(**over) -> Session:
    fields = dict(manager="m__1", project="p", task="t")
    return Session(**{**fields, **over})


class TestFormatEntry(unittest.TestCase):
    """Pure formatting — no disk. `tail -f` is the intended reader, so these
    assert on the raw text rather than on rendered markdown."""

    def test_header_carries_glyph_and_participant(self):
        entry = format_entry(Direction.TO, "golem__a", "hello", now=0)
        self.assertIn("→ golem__a", entry.splitlines()[0])

    def test_body_is_indented(self):
        entry = format_entry(Direction.FROM, "golem__a", "the reply", now=0)
        self.assertIn("    the reply", entry)

    def test_body_markdown_cannot_restructure_the_document(self):
        # A heading or rule in agent prose must not become a heading or rule of
        # the log itself — that is what the indent buys.
        entry = format_entry(Direction.FROM, "golem__a", "# Heading\n---\n```code```", now=0)
        for line in entry.splitlines()[1:]:
            self.assertFalse(line.startswith(("#", "---", "```")))

    def test_blank_lines_inside_a_body_stay_blank(self):
        # Indenting a blank line would leave trailing whitespace; keep it empty.
        entry = format_entry(Direction.FROM, "golem__a", "one\n\ntwo", now=0)
        self.assertIn("\n\n    two", entry)

    def test_entries_are_separated_by_a_blank_line(self):
        self.assertTrue(format_entry(Direction.TO, "a", "x", now=0).endswith("\n\n"))

    def test_empty_body_does_not_crash(self):
        self.assertIn("→ golem__a", format_entry(Direction.TO, "golem__a", "   ", now=0))

    def test_each_direction_has_a_distinct_glyph(self):
        glyphs = {d.glyph for d in Direction}
        self.assertEqual(len(glyphs), len(list(Direction)))


class TestJournalOnDisk(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # journal resolves the group dir through group.cowork_group_path.
        p = patch.object(grp, "cowork_group_path",
                         lambda instance, key: self.root / instance / key)
        p.start()
        self.addCleanup(p.stop)

    def test_path_is_in_the_managers_group_dir(self):
        s = _session()
        self.assertEqual(journal_path(s), self.root / "m__1" / s.key / "conversation.md")

    def test_append_creates_the_file(self):
        s = _session()
        append(s, Direction.TO, "golem__a", "first", now=0)
        self.assertTrue(journal_path(s).is_file())

    def test_appends_accumulate_in_order(self):
        s = _session()
        append(s, Direction.TO, "golem__a", "the ask", now=0)
        append(s, Direction.FROM, "golem__a", "the answer", now=1)
        text = read_journal(s)
        self.assertLess(text.index("the ask"), text.index("the answer"))

    def test_append_never_rewrites_earlier_entries(self):
        # The log is the durable record; a later write must not be able to
        # truncate what came before.
        s = _session()
        for i in range(5):
            append(s, Direction.FROM, "golem__a", f"entry {i}", now=i)
        text = read_journal(s)
        for i in range(5):
            self.assertIn(f"entry {i}", text)

    def test_read_journal_empty_before_anything_is_logged(self):
        self.assertEqual(read_journal(_session()), "")

    def test_separate_groups_keep_separate_logs(self):
        # A coworker in two groups must not see them interleaved.
        one, two = _session(project="alpha"), _session(project="beta")
        append(one, Direction.FROM, "golem__a", "alpha work", now=0)
        append(two, Direction.FROM, "golem__a", "beta work", now=0)
        self.assertNotIn("beta work", read_journal(one))
        self.assertNotIn("alpha work", read_journal(two))

    def test_log_lives_with_the_manager_not_the_coworker(self):
        s = _session(coworkers=("golem__a",))
        append(s, Direction.FROM, "golem__a", "x", now=0)
        self.assertFalse((self.root / "golem__a").exists())


if __name__ == "__main__":
    unittest.main()
