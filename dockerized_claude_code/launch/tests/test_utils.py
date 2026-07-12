"""Tests for launch.utils — pure formatting / sorting / parsing helpers."""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from launch.utils import (
    ordering_index_or_end, parse_agent_name, parse_stem, plural,
    relative_time, split_host_port,
)


class TestPlural(unittest.TestCase):
    def test_singular(self):
        self.assertEqual(plural(1), "")

    def test_zero(self):
        self.assertEqual(plural(0), "s")

    def test_two(self):
        self.assertEqual(plural(2), "s")

    def test_large(self):
        self.assertEqual(plural(100), "s")

    def test_negative(self):
        # negative isn't "exactly one" — plural
        self.assertEqual(plural(-1), "s")


class TestRelativeTime(unittest.TestCase):
    """relative_time builds its delta from datetime.now() — patch the bound
    name in launch.utils so tests are time-independent."""

    def _mtime_n_ago(self, **kwargs):
        return (datetime(2026, 5, 15, 12, 0, 0) - timedelta(**kwargs)).timestamp()

    def setUp(self):
        self.fake_now = datetime(2026, 5, 15, 12, 0, 0)
        self.patcher = patch("launch.utils.datetime")
        mock_dt = self.patcher.start()
        mock_dt.now.return_value = self.fake_now
        mock_dt.fromtimestamp.side_effect = datetime.fromtimestamp

    def tearDown(self):
        self.patcher.stop()

    def test_just_now(self):
        self.assertEqual(relative_time(self._mtime_n_ago(seconds=5)), "just now")

    def test_one_minute(self):
        self.assertEqual(relative_time(self._mtime_n_ago(minutes=1)), "1 minute ago")

    def test_many_minutes(self):
        self.assertEqual(relative_time(self._mtime_n_ago(minutes=42)), "42 minutes ago")

    def test_one_hour(self):
        self.assertEqual(relative_time(self._mtime_n_ago(hours=1)), "1 hour ago")

    def test_many_hours(self):
        self.assertEqual(relative_time(self._mtime_n_ago(hours=5)), "5 hours ago")

    def test_one_day(self):
        self.assertEqual(relative_time(self._mtime_n_ago(days=1)), "1 day ago")

    def test_many_days(self):
        self.assertEqual(relative_time(self._mtime_n_ago(days=14)), "14 days ago")

    def test_future_mtime_clamps_to_just_now(self):
        # Clock skew / NTP jump / copied file can put an mtime in the future.
        # The negative timedelta normalizes to days=-1 + positive seconds,
        # which used to render nonsense like "23 hours ago" — clamp instead.
        self.assertEqual(relative_time(self._mtime_n_ago(hours=-2)), "just now")

    def test_slightly_future_mtime_clamps_to_just_now(self):
        self.assertEqual(relative_time(self._mtime_n_ago(seconds=-1)), "just now")


class TestOrderingIndexOrEnd(unittest.TestCase):
    def test_first(self):
        self.assertEqual(ordering_index_or_end("a", ["a", "b", "c"]), 0)

    def test_middle(self):
        self.assertEqual(ordering_index_or_end("b", ["a", "b", "c"]), 1)

    def test_last(self):
        self.assertEqual(ordering_index_or_end("c", ["a", "b", "c"]), 2)

    def test_missing_sinks_to_end(self):
        self.assertEqual(ordering_index_or_end("z", ["a", "b", "c"]), 3)

    def test_empty_ordering(self):
        self.assertEqual(ordering_index_or_end("anything", []), 0)

    def test_works_with_tuple(self):
        # ordering accepted as any sequence (used with InstanceModifiers.tag_values()
        # which is a tuple)
        self.assertEqual(ordering_index_or_end("b", ("a", "b", "c")), 1)


class TestSplitHostPort(unittest.TestCase):
    def test_bare_host(self):
        self.assertEqual(split_host_port("example.com"), ("example.com", ""))

    def test_host_with_port(self):
        self.assertEqual(split_host_port("example.com:443"), ("example.com", "443"))

    def test_ipv4(self):
        self.assertEqual(split_host_port("1.2.3.4"), ("1.2.3.4", ""))

    def test_ipv4_with_port(self):
        self.assertEqual(split_host_port("1.2.3.4:80"), ("1.2.3.4", "80"))

    def test_cidr(self):
        self.assertEqual(split_host_port("10.0.0.0/8"), ("10.0.0.0/8", ""))

    def test_cidr_with_port(self):
        self.assertEqual(split_host_port("10.0.0.0/8:443"), ("10.0.0.0/8", "443"))

    def test_empty(self):
        self.assertEqual(split_host_port(""), ("", ""))

    def test_trailing_colon(self):
        # rpartition still splits on the colon — port empty string
        self.assertEqual(split_host_port("host:"), ("host", ""))

    def test_leading_colon(self):
        self.assertEqual(split_host_port(":80"), ("", "80"))


class TestParseStem(unittest.TestCase):
    def test_name_only(self):
        self.assertEqual(parse_stem("poet"), ("poet", [], None))

    def test_name_with_tag(self):
        self.assertEqual(parse_stem("poet[code]"), ("poet", ["code"], None))

    def test_name_with_parent(self):
        self.assertEqual(parse_stem("poet(thinker)"), ("poet", [], "thinker"))

    def test_name_with_tag_then_parent(self):
        self.assertEqual(parse_stem("poet[code](thinker)"), ("poet", ["code"], "thinker"))

    def test_name_with_parent_then_tag(self):
        # Order is free — same result either way
        self.assertEqual(parse_stem("poet(thinker)[code]"), ("poet", ["code"], "thinker"))

    def test_multiple_tags_accumulate_in_order(self):
        self.assertEqual(parse_stem("poet[a][b][c]"), ("poet", ["a", "b", "c"], None))

    def test_repeated_parent_last_wins(self):
        self.assertEqual(parse_stem("poet(a)(b)"), ("poet", [], "b"))

    def test_complex_combo(self):
        self.assertEqual(parse_stem("name[a](parent)[b]"), ("name", ["a", "b"], "parent"))


class TestParseStemMalformed(unittest.TestCase):
    """Malformed stems raise ValueError instead of silently dropping the
    malformed parts — the old lenient behavior meant a typo'd filename like
    `poet[code.md` launched the agent without its [code] toolchain and
    nothing ever said so."""

    def test_unclosed_tag_bracket_raises(self):
        with self.assertRaises(ValueError):
            parse_stem("poet[code")

    def test_unclosed_parent_paren_raises(self):
        with self.assertRaises(ValueError):
            parse_stem("poet(thinker")

    def test_stray_text_between_groups_raises(self):
        with self.assertRaises(ValueError):
            parse_stem("poet[a]junk[b]")

    def test_trailing_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_stem("poet[code]x")

    def test_empty_tag_group_raises(self):
        with self.assertRaises(ValueError):
            parse_stem("poet[]")

    def test_empty_parent_group_raises(self):
        with self.assertRaises(ValueError):
            parse_stem("poet()")

    def test_leading_bracket_raises(self):
        # Stem must start with a name, not a group.
        with self.assertRaises(ValueError):
            parse_stem("[code]poet")

    def test_empty_stem_raises(self):
        with self.assertRaises(ValueError):
            parse_stem("")

    def test_error_message_names_the_stem(self):
        # The warning path in paths._agent_md_index prints this message —
        # it must identify the offending file for the user.
        with self.assertRaises(ValueError) as ctx:
            parse_stem("poet[code")
        self.assertIn("poet[code", str(ctx.exception))


class TestParseAgentName(unittest.TestCase):
    def test_extracts_name_dropping_suffixes(self):
        # Asserts the name half of parse_stem's tuple — the wrapper that
        # AGENT_MD_BY_NAME's comprehension uses to index the dict.
        self.assertEqual(parse_agent_name("poet"), "poet")
        self.assertEqual(parse_agent_name("poet[code]"), "poet")
        self.assertEqual(parse_agent_name("poet(thinker)"), "poet")
        self.assertEqual(parse_agent_name("poet[code](thinker)"), "poet")


if __name__ == "__main__":
    unittest.main()
