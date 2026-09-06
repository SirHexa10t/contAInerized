"""Tests for launch.container_inject — typing a prompt into a live session.

No docker: `docker attach` is replaced by a fake that holds the pty's slave
end open, so the real pty plumbing (raw mode, the refusal probe, the winsize
stamp, the Enter keystroke) runs exactly as it does in production. That
plumbing had NO direct coverage while it lived inside `docker_config` —
every cowork test stubs `docker_attach_inject` wholesale — and each of its
details is there because of a bug that reached a user: a blanked TUI, an
EPIPE with no explanation, an Enter that submitted a fragment."""

import os
import select
import struct
import termios
import unittest
from unittest.mock import patch

import fcntl

from launch import container_inject


class _FakeAttach:
    """Stands in for the `docker attach` child process.

    Duplicates the slave fd because `docker_attach_inject` closes its own copy
    the moment Popen returns ("the child owns it now") — without the dup the
    pty would collapse and the writes under test would fail for the wrong
    reason."""

    def __init__(self, argv, *, stdin, stdout, stderr, close_fds,
                 alive=True, emit=b""):
        self.argv = argv
        self.slave = os.dup(stdin)
        self._alive = alive
        self.terminated = False
        self.killed = False
        self._typed = b""
        if emit:
            os.write(self.slave, emit)

    def poll(self):
        return None if self._alive else 1

    def terminate(self):
        # Read the typed bytes HERE, not from the test body: `terminate()` is
        # the last thing called while the master fd is still open, and closing
        # a pty's master discards whatever the slave has not read yet. Draining
        # afterwards reads an empty buffer no matter what was typed.
        self.terminated = True
        self._typed = self._drain()

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True

    def _drain(self, seconds=0.25):
        chunks = []
        while select.select([self.slave], [], [], seconds)[0]:
            try:
                data = os.read(self.slave, 4096)
            except OSError:         # master gone
                break
            if not data:
                break
            chunks.append(data)
            seconds = 0.05          # first read waits, the rest just drain
        return b"".join(chunks)

    def typed(self):
        """Whatever the launcher typed at us, as bytes."""
        return self._typed


class InjectionCase(unittest.TestCase):
    def setUp(self):
        self.attaches: list[_FakeAttach] = []
        # The refusal probe is a 1s watch in production; nothing in a test
        # needs to wait that long for a decision the fake has already made.
        self.enterExit(patch.object(container_inject, "INJECT_ATTACH_PROBE", 0.05))

    def enterExit(self, ctx):
        value = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        return value

    def inject(self, prompt="hello", *, alive=True, emit=b"", size=(30, 100)):
        def popen(argv, **kwargs):
            fake = _FakeAttach(argv, alive=alive, emit=emit, **kwargs)
            self.attaches.append(fake)
            self.addCleanup(os.close, fake.slave)
            return fake

        with patch.object(container_inject.subprocess, "Popen", popen), \
             patch.object(container_inject, "container_tty_size", return_value=size), \
             patch("builtins.print") as printed:
            landed = container_inject.docker_attach_inject(
                "researcher__proj", prompt, enter_delay=0)
        self.printed = " ".join(str(c.args[0]) for c in printed.call_args_list)
        return landed


class TestTheHappyPath(InjectionCase):
    def test_the_prompt_is_typed_and_followed_by_enter(self):
        self.assertTrue(self.inject("what is the status?"))
        typed = self.attaches[0].typed()
        self.assertEqual(typed, b"what is the status?" + b"\r")

    def test_enter_is_carriage_return_not_newline(self):
        # "\n" is swallowed by the input widget — the TUI reads Enter as \r,
        # so a newline here would leave the prompt sitting unsubmitted.
        self.inject("x")
        self.assertTrue(self.attaches[0].typed().endswith(b"\r"))
        self.assertNotIn(b"\n", self.attaches[0].typed())

    def test_it_attaches_to_the_prefixed_container_without_proxying_signals(self):
        # --sig-proxy=false: a Ctrl-C in the hub's terminal must not reach the
        # agent's session.
        self.inject()
        self.assertEqual(self.attaches[0].argv, [
            "docker", "attach", "--sig-proxy=false",
            f"{container_inject.CONTAINER_NAME_PREFIX}researcher__proj"])

    def test_the_attach_is_terminated_rather_than_left_running(self):
        self.inject()
        self.assertTrue(self.attaches[0].terminated)
        self.assertFalse(self.attaches[0].killed)   # it exited on request


class TestARefusedAttach(InjectionCase):
    """docker rejects a non-TTY attach immediately; the first write would then
    die with a bare EPIPE. The probe exists so the caller sees docker's own
    complaint instead."""

    def test_a_dead_child_reports_docker_s_complaint_and_types_nothing(self):
        self.assertFalse(self.inject(emit=b"Error: No such container: x\r\n",
                                     alive=False))
        self.assertIn("No such container", self.printed)
        self.assertEqual(self.attaches[0].typed(), b"")

    def test_ansi_noise_is_stripped_from_the_reported_detail(self):
        self.assertFalse(self.inject(emit=b"\x1b[31mError: denied\x1b[0m\r\n",
                                     alive=False))
        self.assertIn("Error: denied", self.printed)
        self.assertNotIn("\x1b", self.printed)

    def test_a_silent_death_still_says_something(self):
        self.assertFalse(self.inject(emit=b"", alive=False))
        self.assertIn("docker printed nothing", self.printed)


class TestWindowSize(unittest.TestCase):
    """The stamp that keeps a woken agent's TUI from redrawing into nothing:
    `docker attach` propagates the CLIENT's terminal size to the container, so
    an unsized pty (often 0x0) resizes the agent's terminal on every
    injection."""

    def _stamped(self, probed):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        with patch.object(container_inject, "container_tty_size", return_value=probed):
            container_inject._match_container_winsize(master, "claude-code_x")
        packed = fcntl.ioctl(master, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        return rows, cols

    def test_the_containers_own_size_is_stamped_onto_the_pty(self):
        self.assertEqual(self._stamped((45, 172)), (45, 172))

    def test_an_unreadable_size_falls_back_to_a_conventional_terminal(self):
        # Better a plausible 24x80 than the pty default: 0x0 is what blanks
        # the display until the human resizes their window.
        self.assertEqual(self._stamped(None), container_inject.FALLBACK_TTY_SIZE)


class TestDrainPty(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = os.openpty()
        self.addCleanup(os.close, self.master)

    def test_it_returns_what_the_stream_emitted(self):
        os.write(self.slave, b"docker said this\r\n")
        self.addCleanup(os.close, self.slave)
        self.assertIn("docker said this", container_inject._drain_pty(self.master, 0.2))

    def test_silence_within_the_window_returns_empty(self):
        self.addCleanup(os.close, self.slave)
        self.assertEqual(container_inject._drain_pty(self.master, 0.1), "")

    def test_it_stops_early_when_the_far_end_closes(self):
        # No waiting out the full window once there is nothing more coming —
        # this is what keeps the 1s production probe from being a 1s floor on
        # every failed injection.
        os.write(self.slave, b"bye")
        os.close(self.slave)
        self.assertIn("bye", container_inject._drain_pty(self.master, 30))

    def test_undecodable_bytes_do_not_raise(self):
        # The stream is whatever docker printed; a truncated multibyte
        # sequence must not turn a diagnostic into a traceback.
        os.write(self.slave, b"\xff\xfe bad")
        self.addCleanup(os.close, self.slave)
        self.assertIn("bad", container_inject._drain_pty(self.master, 0.2))


if __name__ == "__main__":
    unittest.main()
