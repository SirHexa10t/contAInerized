"""Tests for launch.cluster_work_protocol — the schema's vocabulary rules
(the fold above all), the loud config, the queue's total order under
concurrency, the `cluster-chat` CLI members actually type, and the package's
standalone constraint (it must import without its launch/ parent, because
containers mount the directory alone)."""

import contextlib
import fcntl
import importlib
import io
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import paths
from launch.cluster_work_protocol import (
    Message, ProtocolError, Queue, load_config,
)
from launch.cluster_work_protocol import gates, wake
from launch.cluster_work_protocol.cli import main as cli_main
from launch.cluster_work_protocol.config import ProtocolConfig

SEEDED_CONFIG = paths.SETTINGS_DIR / "cluster_protocol.toml"


class TestMessageSchema(unittest.TestCase):
    def _msg(self, **over):
        base = dict(seq=1, ts="2026-08-31T00:00:00+00:00", member="tester",
                    kind="stance", body="fine by me", iteration="gate-1",
                    stance=8)
        base.update(over)
        return Message(**base)

    def test_round_trips_through_a_line(self):
        message = self._msg()
        self.assertEqual(Message.from_line(message.to_line()), message)

    def test_none_fields_are_omitted_not_null(self):
        line = self._msg(kind="free", iteration=None, stance=None).to_line()
        self.assertNotIn("null", line)
        self.assertNotIn("stance", line)

    def test_the_fold_carries_nothing(self):
        # THE rule (operator, 2026-08-31): a nop is poker's fold — the intent
        # not to participate, with no assessment data riding along.
        with self.assertRaisesRegex(ProtocolError, "fold"):
            self._msg(kind="nop", stance=3).validate()
        self._msg(kind="nop", stance=None).validate()   # the legal nop

    def test_a_stance_needs_a_value_in_range(self):
        with self.assertRaisesRegex(ProtocolError, "needs `stance`"):
            self._msg(stance=None).validate()
        for bad in (-1, 11):
            with self.assertRaisesRegex(ProtocolError, "outside"):
                self._msg(stance=bad).validate()
        for edge in (0, 10):
            self._msg(stance=edge).validate()

    def test_gate_kinds_need_their_iteration(self):
        for kind in ("open", "nop", "hold", "timeout", "close"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ProtocolError, "iteration"):
                    self._msg(kind=kind, stance=None, iteration=None).validate()
        self._msg(kind="free", stance=None, iteration=None).validate()

    def test_unknown_kind_and_unknown_fields_fail_loud(self):
        with self.assertRaisesRegex(ProtocolError, "unknown kind"):
            self._msg(kind="veto").validate()
        with self.assertRaisesRegex(ProtocolError, "unknown fields"):
            Message.from_line('{"seq": 1, "surprise": true}')


class TestProtocolConfig(unittest.TestCase):
    def test_the_seeded_file_loads_with_the_recorded_seeds(self):
        config = load_config(SEEDED_CONFIG)
        self.assertEqual(config.nudge_after_seconds, 180)
        self.assertEqual(config.close_after_seconds, 480)
        self.assertEqual(config.reply_cap, 1)
        self.assertEqual(config.lock_wait_seconds, 7)   # the operator's 7s, re-homed
        self.assertEqual(config.loop_cap, 200)

    def test_the_scale_is_complete_and_says_what_the_plan_says(self):
        scale = load_config(SEEDED_CONFIG).scale
        self.assertEqual(sorted(scale), list(range(11)))
        self.assertIn("hard no", scale[0])
        self.assertIn("torn", scale[5])          # informed conflict...
        self.assertIn("never ignorance", scale[5])   # ...never "I don't know"
        self.assertIn("resounding yes", scale[10])

    def _write(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        self.addCleanup(Path(tmp.name).unlink)
        tmp.write(text)
        tmp.close()
        return Path(tmp.name)

    def test_a_missing_tunable_is_a_loud_stop(self):
        crippled = SEEDED_CONFIG.read_text().replace(
            "nudge_after_seconds = 180\n", "")
        with self.assertRaisesRegex(ProtocolError, "nudge_after_seconds"):
            load_config(self._write(crippled))

    def test_a_hole_in_the_scale_is_a_loud_stop(self):
        crippled = SEEDED_CONFIG.read_text().replace(
            '7 = "probably yes — leaning yes; would welcome a check, '
            "wouldn't block\"\n", "")
        with self.assertRaisesRegex(ProtocolError, "'7'"):
            load_config(self._write(crippled))

    def test_a_nonpositive_tunable_is_a_loud_stop(self):
        crippled = SEEDED_CONFIG.read_text().replace(
            "lock_wait_seconds = 7", "lock_wait_seconds = 0")
        with self.assertRaisesRegex(ProtocolError, "positive"):
            load_config(self._write(crippled))


class TestQueue(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue = Queue(Path(self._tmp.name), lock_wait_seconds=2)

    def test_appends_assign_dense_seqs_and_read_back_in_order(self):
        self.queue.append("mgr", "open", "assess the plan", iteration="g1")
        self.queue.append("tester", "stance", "add tests", iteration="g1",
                          stance=7)
        self.queue.append("security", "nop", iteration="g1")
        messages = self.queue.read_all()
        self.assertEqual([m.seq for m in messages], [1, 2, 3])
        self.assertEqual([m.kind for m in messages], ["open", "stance", "nop"])

    def test_read_since_is_strictly_after(self):
        for _ in range(3):
            self.queue.append("a", "free", "hi")
        self.assertEqual([m.seq for m in self.queue.read_since(1)], [2, 3])

    def test_cursors_give_each_member_its_own_new(self):
        self.queue.append("a", "free", "one")
        self.assertEqual(len(self.queue.read_new("b")), 1)
        self.assertEqual(self.queue.read_new("b"), [])      # advanced
        self.queue.append("a", "free", "two")
        peeked = self.queue.read_new("b", advance=False)
        self.assertEqual([m.body for m in peeked], ["two"])
        self.assertEqual(len(self.queue.read_new("b")), 1)  # peek didn't advance

    def test_an_invalid_message_never_reaches_the_file(self):
        with self.assertRaises(ProtocolError):
            self.queue.append("a", "nop", iteration="g1", stance=3)
        self.assertEqual(self.queue.read_all(), [])

    def test_concurrent_appenders_serialize_into_one_dense_order(self):
        # The design's founding requirement: no order discrepancy between
        # members. 8 writers × 25 appends must yield seqs 1..200 exactly,
        # every line parseable.
        def hammer(name: str) -> None:
            for _ in range(25):
                self.queue.append(name, "free", "x")
        threads = [threading.Thread(target=hammer, args=(f"m{i}",))
                   for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        seqs = [m.seq for m in self.queue.read_all()]
        self.assertEqual(sorted(seqs), list(range(1, 201)))

    def test_a_wedged_lock_is_a_named_error_not_a_hang(self):
        holder = open(Path(self._tmp.name) / ".lock", "wb")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX)
        with self.assertRaisesRegex(ProtocolError, "queue lock"):
            Queue(Path(self._tmp.name), lock_wait_seconds=1).append(
                "a", "free", "x")


class GateFixture(unittest.TestCase):
    """Shared plumbing for gate-flavored tests: a tmp protocol root, a
    members dir (the roster), the seeded config, and every wake path mocked
    into a recording — wake's own subprocess behavior has its own tests."""

    MEMBERS = ("golem", "security", "tester")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "protocol"
        self.members_dir = Path(self._tmp.name) / "members"
        for member in self.MEMBERS:
            (self.members_dir / member).mkdir(parents=True)
        self.queue = Queue(self.root, lock_wait_seconds=2)
        self.config = load_config(SEEDED_CONFIG)
        self.pings: list[tuple[tuple[str, ...], str]] = []

        def record(members, text):
            self.pings.append((tuple(members), text))
            return []

        for target in ("launch.cluster_work_protocol.gates.ping_members",
                       "launch.cluster_work_protocol.cli.ping_members"):
            patcher = patch(target, side_effect=record)
            patcher.start()
            self.addCleanup(patcher.stop)
        timer = patch("launch.cluster_work_protocol.gates._spawn_timer")
        self.timers = timer.start()
        self.addCleanup(timer.stop)

    def pinged(self, text_fragment: str) -> list[tuple[str, ...]]:
        return [members for members, text in self.pings
                if text_fragment in text]


class TestGates(GateFixture):
    """The liturgy end to end, plus the atomicity the design demands."""

    def open(self, iteration: str = "g1", opener: str = "golem") -> list[str]:
        return gates.open_gate(self.queue, self.config, iteration=iteration,
                               body="adopt plan X?", opener=opener,
                               members_dir=self.members_dir,
                               config_path=SEEDED_CONFIG, timers=False)

    def reply(self, member: str, kind: str = "nop", *, stance=None,
              body: str = "", iteration: str = "g1"):
        return gates.post_reply(self.queue, self.config, member=member,
                                kind=kind, body=body, iteration=iteration,
                                stance=stance, members_dir=self.members_dir)

    def test_open_pings_everyone_but_the_opener(self):
        self.open()
        (members,) = self.pinged("gate g1 is open")
        self.assertEqual(members, ("security", "tester"))

    def test_a_duplicate_gate_id_is_refused(self):
        self.open()
        with self.assertRaisesRegex(ProtocolError, "already exists"):
            self.open()

    def test_only_the_completing_reply_pings_the_opener(self):
        self.open()
        self.reply("security")
        self.assertEqual(self.pinged("all replies are in"), [])
        _, report = self.reply("tester", "stance", stance=7, body="fine")
        self.assertEqual(self.pinged("all replies are in"), [("golem",)])
        self.assertTrue(any("last reply" in line for line in report))

    def test_the_reply_cap_holds(self):
        self.open()
        self.reply("security")
        with self.assertRaisesRegex(ProtocolError, "already replied"):
            self.reply("security", "stance", stance=3, body="second thoughts")

    def test_the_loop_cap_degrades_to_silence(self):
        tight = ProtocolConfig(nudge_after_seconds=180,
                               close_after_seconds=480, reply_cap=5,
                               lock_wait_seconds=2, loop_cap=2,
                               scale=self.config.scale)
        self.open()   # thread message #1
        gates.post_reply(self.queue, tight, member="security", kind="nop",
                         body="", iteration="g1", stance=None,
                         members_dir=self.members_dir)   # thread message #2
        with self.assertRaisesRegex(ProtocolError, "loop cap"):
            gates.post_reply(self.queue, tight, member="tester", kind="nop",
                             body="", iteration="g1", stance=None,
                             members_dir=self.members_dir)

    def test_replies_need_an_open_gate(self):
        with self.assertRaisesRegex(ProtocolError, "no gate"):
            self.reply("tester")
        self.open()
        self.reply("security")
        self.reply("tester")
        gates.close_gate(self.queue, member="golem", iteration="g1",
                         resolution="adopted", members_dir=self.members_dir)
        with self.assertRaisesRegex(ProtocolError, "closed"):
            self.reply("tester", iteration="g1")

    def test_only_the_opener_closes(self):
        self.open()
        with self.assertRaisesRegex(ProtocolError, "only the opener"):
            gates.close_gate(self.queue, member="tester", iteration="g1",
                             resolution="I say so", members_dir=self.members_dir)

    def test_check_gate_nudges_only_stragglers_before_the_deadline(self):
        self.open()
        self.reply("security")
        report = gates.check_gate(self.queue, self.config, iteration="g1",
                                  members_dir=self.members_dir)
        self.assertEqual(self.pinged("reminder"), [("tester",)])
        self.assertTrue(any("nudged 1" in line for line in report))

    def test_check_gate_after_the_deadline_records_timeouts_idempotently(self):
        from datetime import datetime, timedelta, timezone
        self.open()
        self.reply("security")
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        with patch("launch.cluster_work_protocol.gates.datetime") as clock:
            clock.fromisoformat = datetime.fromisoformat
            clock.now.return_value = future
            gates.check_gate(self.queue, self.config, iteration="g1",
                             members_dir=self.members_dir)
        rows = [m for m in self.queue.read_all() if m.kind == "timeout"]
        self.assertEqual([(m.member, m.iteration) for m in rows],
                         [("tester", "g1")])
        self.assertEqual(self.pinged("timed out waiting on tester"),
                         [("golem",)])
        # A re-run (the other fork, or a retry) adds nothing: the timeout
        # made the gate COMPLETE, so the opener just gets the standard ping.
        gates.check_gate(self.queue, self.config, iteration="g1",
                         members_dir=self.members_dir)
        self.assertEqual(
            len([m for m in self.queue.read_all() if m.kind == "timeout"]), 1)
        self.assertEqual(self.pinged("all replies are in"), [("golem",)])

    def test_check_gate_on_a_closed_gate_is_silent(self):
        self.open()
        self.reply("security")
        self.reply("tester")
        gates.close_gate(self.queue, member="golem", iteration="g1",
                         resolution="done", members_dir=self.members_dir)
        self.assertEqual(gates.check_gate(self.queue, self.config,
                                          iteration="g1",
                                          members_dir=self.members_dir), [])

    def test_open_plants_both_timers_with_the_config_seeds(self):
        self.timers.reset_mock()
        gates.open_gate(self.queue, self.config, iteration="g2",
                        body="?", opener="golem",
                        members_dir=self.members_dir,
                        config_path=SEEDED_CONFIG, timers=True)
        delays = [call.args[0] for call in self.timers.call_args_list]
        self.assertEqual(delays, [180, 480])
        command = self.timers.call_args_list[0].args[1]
        self.assertIn("check-gate g2", command)
        self.assertIn(str(self.root), command)
        # EVERY path rides the fork's argv — a default-path fork against a
        # custom-rooted gate died silently in the first host-side smoke.
        self.assertIn(str(self.members_dir), command)

    def test_mentions_name_real_other_members_only(self):
        found = gates.mentions("ask @tester and @golem, not @me or @nobody",
                               self.MEMBERS, author="golem")
        self.assertEqual(found, ("tester",))


class TestWake(unittest.TestCase):
    """wake.inject's backend ladder — herdr's roster first, tmux fallback,
    warnings (never exceptions) when both fail."""

    HERDR_LIST = ('{"result": {"agents": [{"name": "tester", '
                  '"pane_id": "w1:p3"}]}}')

    def _completed(self, rc: int = 0, stdout: str = ""):
        from types import SimpleNamespace
        return SimpleNamespace(returncode=rc, stdout=stdout)

    def test_herdr_route_uses_the_detected_pane(self):
        calls = []

        def fake_run(argv):
            calls.append(argv)
            if argv[:3] == ["herdr", "agent", "list"]:
                return self._completed(0, self.HERDR_LIST)
            return self._completed(0)

        with patch.object(wake, "_run", side_effect=fake_run):
            self.assertTrue(wake.inject("tester", "gate g1 is open"))
        self.assertEqual(calls[1][:4], ["herdr", "pane", "run", "w1:p3"])
        self.assertTrue(calls[1][4].startswith("[cluster-chat]"))

    def test_tmux_fallback_targets_the_member_window(self):
        def fake_run(argv):
            if argv[0] == "herdr":
                return None                      # no herdr here
            return self._completed(0)

        with patch.object(wake, "_run", side_effect=fake_run), \
             patch.dict("os.environ", {"CLUSTER_SESSION": "team"}):
            self.assertTrue(wake.inject("tester", "hello"))

    def test_both_backends_failing_is_a_warning_not_an_error(self):
        with patch.object(wake, "_run", return_value=None), \
             patch.dict("os.environ", {}, clear=False):
            os.environ.pop("CLUSTER_SESSION", None)
            warnings = wake.ping_members(("tester",), "hello")
        self.assertEqual(len(warnings), 1)
        self.assertIn("could not wake tester", warnings[0])


class TestCli(unittest.TestCase):
    """cluster-chat — what a member actually types. Identity rides
    $CLUSTER_MEMBER only; $CLUSTER_CHAT_READONLY is the queue-side mute;
    refusals are one plain line and exit code 2 (the message lands in an
    agent's context)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "protocol"
        self.members_dir = Path(self._tmp.name) / "members"
        for member in ("golem", "tester"):
            (self.members_dir / member).mkdir(parents=True)
        env = patch.dict("os.environ", {"CLUSTER_MEMBER": "tester"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLUSTER_CHAT_READONLY", None)
        self.pings: list[tuple[tuple[str, ...], str]] = []

        def record(members, text):
            self.pings.append((tuple(members), text))
            return []

        for target in ("launch.cluster_work_protocol.gates.ping_members",
                       "launch.cluster_work_protocol.cli.ping_members"):
            patcher = patch(target, side_effect=record)
            patcher.start()
            self.addCleanup(patcher.stop)
        timer = patch("launch.cluster_work_protocol.gates._spawn_timer")
        timer.start()
        self.addCleanup(timer.stop)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli_main(["--root", str(self.root),
                             "--config", str(SEEDED_CONFIG),
                             "--members-dir", str(self.members_dir), *argv])
        return code, out.getvalue()

    def as_member(self, member: str):
        return patch.dict("os.environ", {"CLUSTER_MEMBER": member})

    def test_post_and_read_round_trip(self):
        code, _ = self.run_cli("post", "free", "hello room")
        self.assertEqual(code, 0)
        code, out = self.run_cli("read", "--all")
        self.assertEqual(code, 0)
        self.assertIn("#1", out)
        self.assertIn("tester free: hello room", out)

    def test_a_stance_renders_its_number_and_gate(self):
        with self.as_member("golem"):
            self.run_cli("open", "g1", "adopt plan X?")
        self.run_cli("post", "stance", "add tests first", "--gate", "g1",
                     "--stance", "7")
        _, out = self.run_cli("read", "--all")
        self.assertIn("stance(7) [gate g1]: add tests first", out)

    def test_the_full_gate_flow_end_to_end(self):
        # golem opens; tester (the only other member) replies — which
        # completes the gate and pings golem; golem closes; tester's late
        # reply is refused. The journal tells the whole story.
        with self.as_member("golem"):
            code, out = self.run_cli("open", "g1", "adopt plan X?")
            self.assertEqual(code, 0)
            self.assertIn("1 replies expected", out)
        code, out = self.run_cli("post", "stance", "yes, with tests",
                                 "--gate", "g1", "--stance", "8")
        self.assertEqual(code, 0)
        self.assertIn("last reply", out)
        self.assertEqual([m for m, t in self.pings if "all replies" in t],
                         [("golem",)])
        with self.as_member("golem"):
            code, out = self.run_cli("close", "g1", "adopted — tests first")
            self.assertEqual(code, 0)
        code, out = self.run_cli("post", "nop", "--gate", "g1")
        self.assertEqual(code, 2)
        self.assertIn("closed", out)

    def test_only_the_opener_may_close_via_the_cli_too(self):
        with self.as_member("golem"):
            self.run_cli("open", "g1", "?")
        code, out = self.run_cli("close", "g1", "I decide")
        self.assertEqual(code, 2)
        self.assertIn("only the opener", out)

    def test_a_gate_reply_without_the_gate_flag_is_refused(self):
        code, out = self.run_cli("post", "nop")
        self.assertEqual(code, 2)
        self.assertIn("--gate", out)

    def test_a_mention_pings_the_named_member(self):
        self.run_cli("post", "free", "thoughts, @golem?")
        self.assertEqual([m for m, t in self.pings if "mentioned you" in t],
                         [("golem",)])

    def test_a_nop_with_a_body_is_refused(self):
        # The fold carries nothing — not even prose.
        code, out = self.run_cli("post", "nop", "well actually…", "--gate", "g1")
        self.assertEqual(code, 2)
        self.assertIn("carries nothing", out)

    def test_readonly_members_can_read_but_not_post(self):
        self.run_cli("post", "free", "before the mute")
        with patch.dict("os.environ", {"CLUSTER_CHAT_READONLY": "1"}):
            code, out = self.run_cli("post", "free", "muted")
            self.assertEqual(code, 2)
            self.assertIn("listen-only", out)
            code, out = self.run_cli("read", "--all")
            self.assertEqual(code, 0)
            self.assertIn("before the mute", out)

    def test_missing_identity_is_a_named_refusal(self):
        os.environ.pop("CLUSTER_MEMBER")
        code, out = self.run_cli("post", "free", "who am I?")
        self.assertEqual(code, 2)
        self.assertIn("CLUSTER_MEMBER", out)

    def test_default_read_advances_the_cursor_and_peek_does_not(self):
        self.run_cli("post", "free", "one")
        _, first = self.run_cli("read")
        self.assertIn("one", first)
        _, second = self.run_cli("read")
        self.assertIn("nothing new", second)
        self.run_cli("post", "free", "two")
        _, peeked = self.run_cli("read", "--peek")
        self.assertIn("two", peeked)
        _, again = self.run_cli("read")
        self.assertIn("two", again)      # the peek left the cursor alone

    def test_scale_prints_every_meaning(self):
        code, out = self.run_cli("scale")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 11)
        self.assertIn("5 — torn", out)


class TestStandaloneConstraint(unittest.TestCase):
    """Containers mount the package directory ALONE (no launch/ parent), so
    it must be stdlib-only and import as a top-level package."""

    PACKAGE_DIR = Path(__file__).resolve().parent.parent / "cluster_work_protocol"

    def test_no_module_reaches_past_the_package(self):
        # Real IMPORT statements only (ast, not substrings — the package's
        # own docs quote the forbidden pattern as prose): relative imports
        # may go one level (`from .config …`), never two (`from ..paths …`),
        # and absolute imports may not name the launcher.
        import ast
        for source in self.PACKAGE_DIR.glob("*.py"):
            tree = ast.parse(source.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    with self.subTest(module=source.name, line=node.lineno):
                        self.assertLessEqual(node.level, 1,
                                             "parent-package escape")
                        self.assertFalse((node.module or "").startswith("launch"))
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        with self.subTest(module=source.name, line=node.lineno):
                            self.assertFalse(alias.name.startswith("launch"))

    def test_the_package_imports_as_a_top_level_name(self):
        # Exactly what the in-container shim does: the package's PARENT dir
        # on sys.path, imported without the launch. prefix.
        sys.path.insert(0, str(self.PACKAGE_DIR.parent))
        self.addCleanup(sys.path.remove, str(self.PACKAGE_DIR.parent))
        for name in list(sys.modules):
            if name == "cluster_work_protocol" or name.startswith("cluster_work_protocol."):
                del sys.modules[name]
        module = importlib.import_module("cluster_work_protocol")
        self.addCleanup(sys.modules.pop, "cluster_work_protocol", None)
        self.assertTrue(hasattr(module, "Queue"))


if __name__ == "__main__":
    unittest.main()
