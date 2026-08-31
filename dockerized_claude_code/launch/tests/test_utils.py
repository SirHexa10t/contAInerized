"""Tests for launch.utils — pure formatting / sorting / parsing helpers."""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from launch.utils import (
    ordering_index_or_end, plural,
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
        # ordering accepted as any sequence (used with registry-derived name lists
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


class TestResetTerminal(unittest.TestCase):
    """reset_terminal — the repair for escape-sequence modes a dead
    full-screen TUI leaves set on the terminal EMULATOR (beyond termios's
    reach): mouse tracking's `35;77;15M` report garbage is the observed
    symptom, endless when a container died out from under its terminal."""

    def test_the_reset_covers_the_reported_failure_modes(self):
        # Mouse tracking (all grades + the SGR encoding that produces the
        # `35;77;15M` shape), the alternate screen, and the hidden cursor —
        # each in its explicit "off/normal" spelling.
        from launch.utils import TERMINAL_MODE_RESET
        for sequence in ("\x1b[?1000l", "\x1b[?1002l", "\x1b[?1003l",
                         "\x1b[?1006l", "\x1b[?1049l", "\x1b[?25h"):
            self.assertIn(sequence, TERMINAL_MODE_RESET)

    def test_non_tty_stdout_gets_no_escape_bytes(self):
        # quickie pipes stdout — captured output must never contain the
        # repair sequences.
        from launch.utils import reset_terminal
        from unittest.mock import patch
        import io
        with patch("sys.stdout", io.StringIO()) as out:
            reset_terminal(drain_input=True)
        self.assertEqual(out.getvalue(), "")

    def test_a_tty_gets_the_reset_and_the_input_drain(self):
        from launch.utils import TERMINAL_MODE_RESET, reset_terminal
        from unittest.mock import MagicMock, patch
        out = MagicMock()
        out.isatty.return_value = True
        with patch("sys.stdout", out), \
             patch("termios.tcflush") as flush, \
             patch("sys.stdin") as stdin:
            stdin.fileno.return_value = 0
            reset_terminal(drain_input=True)
        out.write.assert_called_once_with(TERMINAL_MODE_RESET)
        flush.assert_called_once()


if __name__ == "__main__":
    unittest.main()
