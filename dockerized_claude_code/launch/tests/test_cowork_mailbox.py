"""Tests for launch.cowork.mailbox — staging out, attributing in.

The transcript fixtures here mirror shapes observed in real Claude Code
transcripts, notably that a single turn carries many user-role `tool_result`
entries alongside the one thing a human actually typed. Attribution is only
correct if those are excluded, so they are present in the fixtures rather than
idealised away.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from launch.cowork import mailbox as mb
from launch.cowork.mailbox import (
    Capture, attribute, consume, consume_staged, container_group_path,
    group_from_prompt, host_transcript_path, next_seq, pointer_prompt,
    prompt_text, read_captures, stage_message, tag_message,
)


def _transcript(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def _user(prompt_id: str, text: str, **over) -> dict:
    return {"promptId": prompt_id,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            **over}


def _tool_result(prompt_id: str) -> dict:
    """A user-role entry that is the harness echoing tool output, not a prompt.
    Real turns contain ten or more of these."""
    return {"promptId": prompt_id,
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "content": "ok"}]}}


class TestGroupTag(unittest.TestCase):
    """The tag is what makes a reply attributable, and its absence is what
    identifies a human-typed turn."""

    def test_round_trip(self):
        # tag_message writes `manager::project`; attribution must hand back the
        # group KEY, which is the same pair joined by `-`.
        self.assertEqual(group_from_prompt(tag_message("m__1", "p", "hello")), "m__1-p")

    def test_tag_shows_the_halves_not_the_key(self):
        # The format the operator asked for: `[cowork task <manager>::<project>]`.
        self.assertTrue(tag_message("m__1", "p", "hi")
                        .startswith("[cowork task m__1::p] "))

    def test_hyphenated_names_round_trip(self):
        # Both halves may contain `-` (agent names do; the key separator is also
        # `-`), which is exactly why the tag needs `::` — the first `::` is
        # unambiguous because group.py rejects `:` in either half.
        self.assertEqual(
            group_from_prompt(tag_message("bug-hunter__a-b", "fix-pass", "go")),
            "bug-hunter__a-b-fix-pass")

    def test_untagged_is_none(self):
        self.assertIsNone(group_from_prompt("just a question someone typed"))

    def test_tag_must_lead(self):
        # A tag mentioned mid-prompt is discussion, not routing metadata.
        self.assertIsNone(group_from_prompt("see [cowork task m__1::p] for context"))

    def test_leading_whitespace_tolerated(self):
        self.assertEqual(group_from_prompt("  [cowork task m__1::p] hi"), "m__1-p")

    def test_old_format_still_attributes(self):
        # Staged messages and transcripts outlive a hub upgrade: a prompt tagged
        # `[cowork <group-key>]` by the previous hub must still route after the
        # format change, or every in-flight reply would drain as unsolicited.
        self.assertEqual(group_from_prompt("[cowork m__1-p] hi"), "m__1-p")

    def test_body_survives_tagging(self):
        self.assertIn("the actual message", tag_message("g", "p", "the actual message"))


class TestPromptRecovery(unittest.TestCase):
    """prompt_text joins a capture's prompt_id back to the one thing typed."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_finds_the_prompt_among_tool_results(self):
        t = _transcript(self.dir / "t.jsonl", [
            _user("p1", "[cowork m__1-p] do the thing"),
            *[_tool_result("p1") for _ in range(10)],
            {"promptId": "p1", "message": {"role": "assistant", "content": "done"}},
        ])
        self.assertEqual(prompt_text(t, "p1"), "[cowork m__1-p] do the thing")

    def test_ignores_other_turns(self):
        t = _transcript(self.dir / "t.jsonl", [
            _user("p1", "first"), _user("p2", "second")])
        self.assertEqual(prompt_text(t, "p2"), "second")

    def test_ignores_sidechain_entries(self):
        # Sidechain traffic is a subagent's, not this conversation's.
        t = _transcript(self.dir / "t.jsonl", [
            _user("p1", "subagent prompt", isSidechain=True),
            _user("p1", "the real prompt")])
        self.assertEqual(prompt_text(t, "p1"), "the real prompt")

    def test_plain_string_content(self):
        t = _transcript(self.dir / "t.jsonl",
                        [{"promptId": "p1", "message": {"role": "user", "content": "typed"}}])
        self.assertEqual(prompt_text(t, "p1"), "typed")

    def test_unknown_prompt_id(self):
        t = _transcript(self.dir / "t.jsonl", [_user("p1", "x")])
        self.assertIsNone(prompt_text(t, "nope"))

    def test_missing_transcript(self):
        self.assertIsNone(prompt_text(self.dir / "absent.jsonl", "p1"))

    def test_malformed_lines_are_skipped(self):
        p = self.dir / "t.jsonl"
        p.write_text("not json\n" + json.dumps(_user("p1", "survived")) + "\n")
        self.assertEqual(prompt_text(p, "p1"), "survived")


class TestHostTranscriptPath(unittest.TestCase):
    """The hook records a CONTAINER path; a host-side hub must rebase it."""

    def test_rebases_onto_the_instance_state_dir(self):
        from launch.paths import instance_state_dir_path
        host = host_transcript_path("poet__a", "/home/claude/.claude/projects/-workspace/s.jsonl")
        self.assertEqual(host, instance_state_dir_path("poet__a") / "projects/-workspace/s.jsonl")

    def test_path_outside_the_mount_is_rejected(self):
        # A host-run agent, or a layout change — better None than the wrong file.
        self.assertIsNone(host_transcript_path("poet__a", "/tmp/elsewhere.jsonl"))


class TestCapturePayload(unittest.TestCase):
    def _payload(self, **over):
        base = {"last_assistant_message": "the reply", "prompt_id": "p1",
                "session_id": "s1", "transcript_path": "/home/claude/.claude/projects/x.jsonl"}
        return {**base, **over}

    def test_parses(self):
        cap = Capture.from_payload("poet__a", Path("/x.json"), self._payload())
        self.assertEqual((cap.answer, cap.prompt_id), ("the reply", "p1"))

    def test_empty_answer_is_dropped(self):
        # An empty turn has nothing to forward.
        self.assertIsNone(Capture.from_payload("poet__a", Path("/x.json"),
                                               self._payload(last_assistant_message="  ")))
        self.assertIsNone(Capture.from_payload("poet__a", Path("/x.json"),
                                               self._payload(last_assistant_message=None)))

    def test_missing_ids_become_none_not_crashes(self):
        cap = Capture.from_payload("poet__a", Path("/x.json"),
                                   {"last_assistant_message": "hi"})
        self.assertIsNone(cap.prompt_id)
        self.assertIsNone(cap.transcript_path)


class CoworkTmpRoot(unittest.TestCase):
    """Redirect the group-hosting builders into a tmpdir."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        for p in (patch.object(mb, "cowork_group_path",
                               lambda instance, key: self.root / instance / key),
                  patch.object(mb, "cowork_outbox_path",
                               lambda instance: self.root / instance / "outbox"),
                  patch.object(mb, "instance_state_dir_path",
                               lambda instance: self.root / "state" / instance)):
            p.start()
            self.addCleanup(p.stop)


class TestStaging(CoworkTmpRoot):
    def test_writes_body_and_returns_container_path(self):
        returned = stage_message("golem__a", "m__1-p", "m__1", "the body", seq=1)
        written = self.root / "golem__a" / "m__1-p" / "messages" / "001-from-m__1.md"
        self.assertTrue(written.is_file())
        self.assertIn("the body", written.read_text())
        # The pointer must name the path as the RECIPIENT sees it, not the host's.
        self.assertEqual(returned, container_group_path("m__1-p") / "messages" / "001-from-m__1.md")

    def test_sequence_numbers_do_not_overwrite(self):
        stage_message("golem__a", "m__1-p", "m__1", "first", seq=1)
        stage_message("golem__a", "m__1-p", "m__1", "second", seq=2)
        names = sorted(p.name for p in (self.root / "golem__a" / "m__1-p" / "messages").iterdir())
        self.assertEqual(names, ["001-from-m__1.md", "002-from-m__1.md"])

    def test_pointer_prompt_is_single_line_and_tagged(self):
        # Injection types keystrokes: a newline would submit a fragment.
        prompt = pointer_prompt("m__1", "p", Path("/cowork/m__1-p/messages/001-from-m__1.md"))
        self.assertNotIn("\n", prompt)
        self.assertEqual(group_from_prompt(prompt), "m__1-p")
        self.assertIn("/cowork/m__1-p/messages/001-from-m__1.md", prompt)

    def test_consume_staged_removes_exactly_the_handled_message(self):
        stage_message("golem__a", "m__1-p", "m__1", "first", seq=1)
        stage_message("golem__a", "m__1-p", "m__1", "second", seq=2)
        consume_staged("golem__a", "m__1-p", "001-from-m__1.md")
        remaining = [p.name for p in
                     (self.root / "golem__a" / "m__1-p" / "messages").iterdir()]
        self.assertEqual(remaining, ["002-from-m__1.md"])

    def test_consume_staged_tolerates_a_missing_file(self):
        # Consumption retries alongside its capture; the second pass must not
        # crash the hub over a file the first pass already removed.
        consume_staged("golem__a", "m__1-p", "001-from-m__1.md")

    def test_next_seq_never_reuses_a_number_after_consumption(self):
        # 001 handled and consumed while 002 still waits: count+1 would mint a
        # second 002 and overwrite the unread message. Max+1 cannot.
        stage_message("golem__a", "m__1-p", "m__1", "first", seq=1)
        stage_message("golem__a", "m__1-p", "m__1", "second", seq=2)
        consume_staged("golem__a", "m__1-p", "001-from-m__1.md")
        self.assertEqual(next_seq("golem__a", "m__1-p"), 3)

    def test_pointer_prompt_does_not_restate_the_sender(self):
        # The staged file's name and header already carry the sender, and the
        # protocol has senders introduce themselves in the body — naming them in
        # the pointer too was noise the operator asked to drop.
        prompt = pointer_prompt("m__1", "p", Path("/cowork/m__1-p/messages/002-from-golem__a.md"))
        before_path = prompt.split("/cowork/", 1)[0]
        self.assertNotIn("golem__a", before_path)
        self.assertIn("A message is at", prompt)


class TestReadCaptures(CoworkTmpRoot):
    def _drop(self, instance, name, payload):
        outbox = self.root / instance / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        path = outbox / name
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return path

    def test_missing_outbox_is_empty_not_an_error(self):
        self.assertEqual(read_captures("nobody__x"), [])

    def test_reads_oldest_first(self):
        self._drop("poet__a", "200-1.json", {"last_assistant_message": "second"})
        self._drop("poet__a", "100-1.json", {"last_assistant_message": "first"})
        self.assertEqual([c.answer for c in read_captures("poet__a")], ["first", "second"])

    def test_unparseable_capture_is_parked_not_deleted(self):
        # Destroying state the hub failed to understand is the wrong default.
        self._drop("poet__a", "100-1.json", "{{{ not json")
        self.assertEqual(read_captures("poet__a"), [])
        parked = self.root / "poet__a" / "outbox" / "rejected" / "100-1.json"
        self.assertTrue(parked.is_file())

    def test_parked_capture_is_not_reread(self):
        self._drop("poet__a", "100-1.json", "garbage")
        read_captures("poet__a")
        self.assertEqual(read_captures("poet__a"), [])   # rejected/ is not rescanned

    def test_empty_answer_is_parked_too(self):
        self._drop("poet__a", "100-1.json", {"last_assistant_message": ""})
        self.assertEqual(read_captures("poet__a"), [])

    def test_consume_removes_the_file(self):
        path = self._drop("poet__a", "100-1.json", {"last_assistant_message": "hi"})
        (capture,) = read_captures("poet__a")
        self.assertTrue(path.is_file())      # reading alone must not destroy it
        consume(capture)
        self.assertFalse(path.is_file())

    def test_non_json_files_are_ignored(self):
        outbox = self.root / "poet__a" / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "notes.txt").write_text("hello")
        self.assertEqual(read_captures("poet__a"), [])


class TestAttribute(CoworkTmpRoot):
    """End-to-end attribution: capture -> transcript -> group, or None."""

    def _capture(self, prompt: str | None, *, prompt_id="p1",
                 transcript_path="/home/claude/.claude/projects/-workspace/s.jsonl"):
        if prompt is not None:
            _transcript(self.root / "state" / "poet__a" / "projects/-workspace/s.jsonl",
                        [_user(prompt_id, prompt), *[_tool_result(prompt_id) for _ in range(3)]])
        return Capture(instance="poet__a", source=Path("/x.json"), prompt_id=prompt_id,
                       session_id="s", transcript_path=transcript_path, answer="a reply")

    def test_tagged_prompt_attributes_to_its_group(self):
        result = attribute(self._capture("[cowork m__1-p] please review"))
        self.assertEqual(result.group, "m__1-p")
        # Not a pointer prompt — there is no staged file to consume.
        self.assertIsNone(result.message_file)

    def test_pointer_prompt_names_the_file_the_turn_answered(self):
        # The queue contract's linchpin: the reply's paired prompt carries the
        # staged file's path, so the hub can consume exactly the message that
        # was handled — no FIFO guess, no delete rights for the recipient.
        prompt = pointer_prompt("m__1", "p", Path("/cowork/m__1-p/messages/003-from-m__1.md"))
        result = attribute(self._capture(prompt))
        self.assertEqual(result.group, "m__1-p")
        self.assertEqual(result.message_file, "003-from-m__1.md")

    def test_old_format_pointer_still_names_its_file(self):
        # Messages staged before the format change are answered after it.
        result = attribute(self._capture(
            "[cowork m__1-p] A message from m__1 is at "
            "/cowork/m__1-p/messages/002-from-m__1.md — read that file and reply to it."))
        self.assertEqual(result.group, "m__1-p")
        self.assertEqual(result.message_file, "002-from-m__1.md")

    def test_human_typed_turn_is_unattributed(self):
        # The whole point: a typed turn cannot carry a tag the hub never wrote.
        self.assertIsNone(attribute(self._capture("what does this function do?")))

    def test_missing_prompt_id_is_unattributed(self):
        cap = self._capture("[cowork g] x")
        self.assertIsNone(attribute(Capture(**{**cap.__dict__, "prompt_id": None})))

    def test_transcript_outside_the_mount_is_unattributed(self):
        self.assertIsNone(attribute(self._capture("[cowork g] x",
                                                 transcript_path="/tmp/nope.jsonl")))

    def test_unreadable_transcript_is_unattributed(self):
        self.assertIsNone(attribute(self._capture(None)))

    def test_two_turns_attribute_independently(self):
        # No ordering assumption: each capture resolves on its own contents,
        # which is what distinguishes this from FIFO.
        _transcript(self.root / "state" / "poet__a" / "projects/-workspace/s.jsonl", [
            _user("pA", "[cowork m__1-alpha] first"),
            _user("pB", "typed by a human"),
            _user("pC", "[cowork m__1-beta] second"),
        ])
        def cap(pid):
            return Capture(instance="poet__a", source=Path("/x.json"), prompt_id=pid,
                           session_id="s",
                           transcript_path="/home/claude/.claude/projects/-workspace/s.jsonl",
                           answer="r")
        self.assertEqual(attribute(cap("pC")).group, "m__1-beta")
        self.assertIsNone(attribute(cap("pB")))
        self.assertEqual(attribute(cap("pA")).group, "m__1-alpha")


if __name__ == "__main__":
    unittest.main()
