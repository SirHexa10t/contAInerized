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


class TestFindTurns(unittest.TestCase):
    """find_turns — the `--find` corpus read. What it must get right: only
    SPOKEN turns match (the term in a tool call or a tool result is not
    something anyone said), sub-agent transcripts DO count while they are
    excluded everywhere else in this module, and a hit carries enough to
    quote itself without a second search."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.state = Path(self.tmpdir.name)
        self.tx = self.state / "projects" / "-workspace"
        self.tx.mkdir(parents=True)

    def _write(self, name, events):
        path = self.tx / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    @staticmethod
    def _turn(speaker, content, ts, **extra):
        return {"type": speaker, "message": {"role": speaker, "content": content},
                "timestamp": ts, **extra}

    def test_finds_the_term_in_both_speakers_oldest_first(self):
        self._write("s.jsonl", [
            self._turn("assistant", "the WIDGET is ready", "2026-01-02T00:00:00.000Z"),
            self._turn("user", "what about the widget", "2026-01-01T00:00:00.000Z"),
        ])
        hits = transcripts.find_turns(self.state, "widget")
        self.assertEqual([hit.speaker for hit in hits], ["user", "assistant"])

    def test_match_is_case_insensitive_and_offset_points_at_it(self):
        self._write("s.jsonl", [self._turn("user", "about the Widget now",
                                           "2026-01-01T00:00:00.000Z")])
        (hit,) = transcripts.find_turns(self.state, "WIDGET")
        self.assertEqual(hit.text[hit.where:hit.where + 6], "Widget")

    def test_a_term_only_in_a_tool_result_is_not_a_hit(self):
        # The echo is typed "user" and the word is right there in the file —
        # but nobody said it, and a raw grep would report it.
        self._write("s.jsonl", [self._turn(
            "user", [{"type": "tool_result", "content": "widget"}],
            "2026-01-01T00:00:00.000Z")])
        self.assertEqual(transcripts.find_turns(self.state, "widget"), [])

    def test_bookkeeping_lines_are_not_hits(self):
        # About half a real transcript is these; `last-prompt` even carries a
        # copy of the prompt text, which would double every hit.
        self._write("s.jsonl", [
            {"type": "last-prompt", "lastPrompt": "the widget", "sessionId": "x"},
            {"type": "ai-title", "aiTitle": "widget work", "sessionId": "x"},
        ])
        self.assertEqual(transcripts.find_turns(self.state, "widget"), [])

    def test_subagent_transcripts_count_and_are_labelled(self):
        # Every line a sub-agent writes is flagged isSidechain, which every
        # other reader here rejects. A search keeps them and says so.
        self._write("s.jsonl", [self._turn("user", "parent asks about widgets",
                                           "2026-01-01T00:00:00.000Z")])
        self._write("s/subagents/agent-1.jsonl",
                    [self._turn("assistant", "sub-agent found the widget",
                                "2026-01-01T00:01:00.000Z", isSidechain=True)])
        hits = transcripts.find_turns(self.state, "widget")
        self.assertEqual([hit.sidechain for hit in hits], [False, True])
        self.assertEqual(hits[1].source.name, "agent-1.jsonl")

    def test_the_source_file_rides_the_hit(self):
        self._write("a.jsonl", [self._turn("user", "widget", "2026-01-01T00:00:00.000Z")])
        (hit,) = transcripts.find_turns(self.state, "widget")
        self.assertEqual(hit.source, self.tx / "a.jsonl")

    def test_malformed_lines_cost_only_their_own_hits(self):
        (self.tx / "s.jsonl").write_text(
            'not json — a widget\n'
            + json.dumps(self._turn("user", "a widget", "2026-01-01T00:00:00.000Z")) + "\n")
        self.assertEqual(len(transcripts.find_turns(self.state, "widget")), 1)

    def test_no_transcripts_finds_nothing(self):
        bare = Path(self.tmpdir.name) / "bare"
        bare.mkdir()
        self.assertEqual(transcripts.find_turns(bare, "widget"), [])


if __name__ == "__main__":
    unittest.main()
