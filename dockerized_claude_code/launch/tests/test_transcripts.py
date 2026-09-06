"""Tests for launch.transcripts — reading Claude Code's session records.

The format is not ours and is not versioned, so these tests are mostly about
what the reader must NOT do: mistake a tool-result echo for a human prompt,
read somebody else's sidechain, or raise on a line it cannot parse. Moved
here with the module 2026-09-03 (they were file_access's).

Every case writes real JSONL into a tmp state dir rather than mocking the
read — the parsing IS the behaviour under test."""

import json
import tempfile
from datetime import datetime, timezone
import unittest
from pathlib import Path

from launch import transcripts


class TestLastPromptInState(unittest.TestCase):
    """last_prompt_in_state pulls the most-recent human question (plus its
    epoch time) out of a state dir's projects/-workspace transcript, skipping
    assistant turns, tool-result echoes, sidechains, and junk lines."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state = Path(self.tmpdir.name)
        self.tx = self.state / "projects" / "-workspace"
        self.tx.mkdir(parents=True)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write(self, name, events):
        (self.tx / name).write_text("\n".join(json.dumps(e) for e in events) + "\n")

    def _user(self, content, ts, **extra):
        return {"type": "user", "message": {"role": "user", "content": content},
                "timestamp": ts, **extra}

    def test_returns_latest_user_prompt_and_epoch(self):
        self._write("s.jsonl", [
            self._user("first question", "2026-01-01T00:00:00.000Z"),
            {"type": "assistant", "message": {"role": "assistant", "content": "an answer"},
             "timestamp": "2026-01-01T00:00:01.000Z"},
            self._user("second question", "2026-01-02T00:00:00.000Z"),
        ])
        prompt, when = transcripts.last_prompt_in_state(self.state)
        self.assertEqual(prompt, "second question")
        self.assertEqual(when, datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp())

    def test_latest_is_by_timestamp_not_file_order(self):
        # A tool_result echo carries a later timestamp than the real question;
        # it must not win (it isn't a human prompt), and a sidechain is ignored.
        self._write("s.jsonl", [
            self._user("real question", "2026-01-01T00:00:00.000Z"),
            self._user([{"type": "tool_result", "content": "x"}], "2026-06-01T00:00:00.000Z"),
            self._user("sidechain noise", "2026-07-01T00:00:00.000Z", isSidechain=True),
        ])
        prompt, _ = transcripts.last_prompt_in_state(self.state)
        self.assertEqual(prompt, "real question")

    def test_text_block_list_content_joined(self):
        self._write("s.jsonl", [self._user(
            [{"type": "text", "text": "block one"}, {"type": "text", "text": "block two"}],
            "2026-01-01T00:00:00.000Z")])
        prompt, _ = transcripts.last_prompt_in_state(self.state)
        self.assertEqual(prompt, "block one block two")

    def test_malformed_and_promptless_lines_skipped(self):
        (self.tx / "s.jsonl").write_text(
            'not json\n{"type": "user"}\n'
            + json.dumps(self._user("survivor", "2026-01-01T00:00:00.000Z")) + "\n")
        prompt, _ = transcripts.last_prompt_in_state(self.state)
        self.assertEqual(prompt, "survivor")

    def test_scans_across_multiple_transcript_files(self):
        self._write("a.jsonl", [self._user("older", "2026-01-01T00:00:00.000Z")])
        self._write("b.jsonl", [self._user("newer", "2026-03-01T00:00:00.000Z")])
        prompt, _ = transcripts.last_prompt_in_state(self.state)
        self.assertEqual(prompt, "newer")

    def test_no_transcript_dir_returns_none(self):
        bare = Path(self.tmpdir.name) / "bare"
        bare.mkdir()
        self.assertIsNone(transcripts.last_prompt_in_state(bare))

    def test_no_human_prompt_returns_none(self):
        self._write("s.jsonl", [{"type": "assistant",
                                  "message": {"role": "assistant", "content": "hi"},
                                  "timestamp": "2026-01-01T00:00:00.000Z"}])
        self.assertIsNone(transcripts.last_prompt_in_state(self.state))


class TestLastAnswerInState(unittest.TestCase):
    """last_answer_in_state pulls the latest ASSISTANT text turn (the answer),
    ignoring the user's questions and the assistant's redacted thinking blocks."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state = Path(self.tmpdir.name)
        self.tx = self.state / "projects" / "-workspace"
        self.tx.mkdir(parents=True)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write(self, events):
        (self.tx / "s.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")

    def test_returns_latest_assistant_text_not_the_question(self):
        self._write([
            {"type": "user", "message": {"role": "user", "content": "the question"},
             "timestamp": "2026-01-01T00:00:00.000Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": ""},          # redacted — no readable text
                {"type": "text", "text": "the answer"}]},
             "timestamp": "2026-01-01T00:00:05.000Z"},
        ])
        found = transcripts.last_answer_in_state(self.state)
        self.assertEqual(found[0], "the answer")

    def test_none_when_only_a_question(self):
        self._write([{"type": "user", "message": {"role": "user", "content": "q"},
                      "timestamp": "2026-01-01T00:00:00.000Z"}])
        self.assertIsNone(transcripts.last_answer_in_state(self.state))


if __name__ == "__main__":
    unittest.main()
