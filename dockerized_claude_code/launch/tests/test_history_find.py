"""Tests for launch.history_find — `--find`'s corpus layer: which
conversation a hit belongs to, what it is called, and how the result reads.

Every case writes real transcripts into a real (temporary) state root and
runs the real walk. The three roots are the point of the module — an answer
that covered `instances/` alone would say "never discussed" about a term a
whole cluster worked on — so the fixture always builds all three.
"""

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from launch import history_find, paths
from launch.history_find import (
    HITS_SHOWN, ConversationFinds, conversation_label, find_in_history,
    print_findings, quote,
)


def _turn(speaker, text, stamp, **extra):
    return {"type": speaker, "message": {"role": speaker, "content": text},
            "timestamp": stamp, **extra}


class _StateRoot(unittest.TestCase):
    """A temporary ~/.ai-agents with the three conversation shapes in it."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        patcher = patch.object(paths, "AGENTS_STATE", Path(self.tmpdir.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, state_dir: Path, name: str, events: list[dict]) -> Path:
        path = state_dir / "projects" / "-workspace" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
        return path

    def instance(self, instance_id):
        return paths.instances_dir() / instance_id

    def member(self, session, member_id):
        return paths.cluster_member_dir(session, member_id)

    def thread(self, thread_id):
        return paths.quickie_state_dir_path(thread_id)


class TestConversationLabel(_StateRoot):
    """A conversation is named by the root it sits under, never by reading
    anything inside it — no cluster.toml, no instance store."""

    def test_an_instance_is_its_id(self):
        self.assertEqual(conversation_label(self.instance("golem__myproj")),
                         "golem__myproj")

    def test_a_cluster_member_names_both_halves(self):
        # "refactorer" alone is ambiguous across clusters, and the cluster
        # alone cannot say which member said it.
        self.assertEqual(conversation_label(self.member("devteam", "refactorer")),
                         "devteam / refactorer")

    def test_a_quickie_thread_is_marked_as_one(self):
        # Thread ids are gibberish by design; the `q /` is what makes the row
        # legible next to an instance id.
        self.assertEqual(conversation_label(self.thread("abc123")), "q / abc123")

    def test_a_dir_under_no_known_root_still_names_itself(self):
        # A layout that changed under us prints a path rather than crashing
        # or, worse, mislabelling it as an instance.
        stray = Path("/somewhere/else/entirely")
        self.assertEqual(conversation_label(stray), str(stray))


class TestFindInHistory(_StateRoot):
    """The search across all three roots: what matches, what is dropped, and
    in what order the answer comes back."""

    def test_finds_the_term_in_every_root(self):
        self.write(self.instance("golem__myproj"), "s.jsonl",
                   [_turn("user", "add a widget", "2026-09-10T09:00:00Z")])
        self.write(self.member("devteam", "refactorer"), "s.jsonl",
                   [_turn("assistant", "moved the widget code", "2026-09-19T10:00:00Z")])
        self.write(self.thread("abc123"), "s.jsonl",
                   [_turn("user", "what is a widget", "2026-09-01T08:00:00Z")])
        labels = [found.label for found in find_in_history("widget")]
        self.assertEqual(sorted(labels),
                         ["devteam / refactorer", "golem__myproj", "q / abc123"])

    def test_ranked_by_the_newest_MATCH_not_the_newest_conversation(self):
        # The stale conversation is the one still being used; what ranks is
        # when the term was last said, which is what the operator is after.
        busy = self.instance("golem__busy")
        self.write(busy, "s.jsonl", [
            _turn("user", "widget talk, long ago", "2026-01-01T00:00:00Z"),
            _turn("user", "unrelated, this morning", "2026-09-19T09:00:00Z")])
        self.write(self.instance("golem__recent"), "s.jsonl",
                   [_turn("user", "widget talk, yesterday", "2026-09-18T00:00:00Z")])
        self.assertEqual([found.label for found in find_in_history("widget")],
                         ["golem__recent", "golem__busy"])

    def test_conversations_without_a_hit_are_dropped_entirely(self):
        self.write(self.instance("golem__quiet"), "s.jsonl",
                   [_turn("user", "nothing of the sort", "2026-09-10T09:00:00Z")])
        self.assertEqual(find_in_history("widget"), [])

    def test_the_communal_quickie_workspace_is_not_a_conversation(self):
        # It is the shared drop-box the threads sit beside, and a file the
        # user dropped there is not something anyone said.
        communal = paths.quickie_communal_workspace()
        self.write(communal, "s.jsonl",
                   [_turn("user", "widget", "2026-09-10T09:00:00Z")])
        self.assertEqual(find_in_history("widget"), [])

    def test_subagent_turns_count_and_carry_their_label(self):
        member = self.member("devteam", "refactorer")
        self.write(member, "s.jsonl",
                   [_turn("user", "look into widgets", "2026-09-19T10:00:00Z")])
        self.write(member, "s/subagents/agent-1.jsonl",
                   [_turn("assistant", "the widget is in the picker",
                          "2026-09-19T10:05:00Z", isSidechain=True)])
        (found,) = find_in_history("widget")
        self.assertEqual([hit.sidechain for hit in found.hits], [False, True])

    def test_one_conversation_keeps_every_hit_in_time_order(self):
        self.write(self.instance("golem__myproj"), "s.jsonl", [
            _turn("assistant", "widget, later", "2026-09-10T10:00:00Z"),
            _turn("user", "widget, earlier", "2026-09-10T09:00:00Z")])
        (found,) = find_in_history("widget")
        self.assertEqual([hit.speaker for hit in found.hits], ["user", "assistant"])

    def test_case_insensitive_across_the_corpus(self):
        self.write(self.instance("golem__myproj"), "s.jsonl",
                   [_turn("user", "the Widget", "2026-09-10T09:00:00Z")])
        self.assertEqual(len(find_in_history("WIDGET")), 1)


class TestQuote(unittest.TestCase):
    """What a result row shows of a long turn."""

    @staticmethod
    def _hit(text, where):
        return history_find.TranscriptHit(
            speaker="user", text=text, when=0.0, where=where,
            source=Path("/x.jsonl"), sidechain=False)

    def test_a_match_at_the_start_is_not_ellipsised(self):
        self.assertEqual(quote(self._hit("widget first", 0)), "widget first")

    def test_a_late_match_keeps_its_run_up(self):
        text = "x" * 500 + "widget"
        quoted = quote(self._hit(text, 500))
        self.assertTrue(quoted.startswith("…"))
        self.assertIn("widget", quoted)

    def test_newlines_never_survive_into_a_row(self):
        # A turn is paragraphs; a row is a row.
        quoted = quote(self._hit("one\n\ntwo widget three", 10))
        self.assertNotIn("\n", quoted)


class TestPrintFindings(_StateRoot):
    """The printed report. Content assertions only — the colours and the
    exact padding are rich's, and pinning them would break on a restyle."""

    def _printed(self, term):
        # The real Console captured BEFORE the patch — a lambda that reached
        # back through the module would call itself. Width pinned wide so the
        # one-row-per-hit rule doesn't truncate the text being asserted.
        console_cls = history_find.Console
        out = StringIO()
        with patch.object(history_find, "Console",
                          lambda *a, **k: console_cls(file=out, width=200,
                                                      force_terminal=False)), \
             patch("sys.stdout", out):
            print_findings(term, find_in_history(term))
        return out.getvalue()

    def test_nothing_found_says_so_with_the_term(self):
        self.assertIn('No conversation mentions "widget"', self._printed("widget"))

    def test_a_report_names_the_conversation_and_quotes_the_turn(self):
        self.write(self.instance("golem__myproj"), "s.jsonl",
                   [_turn("user", "can we add a widget", "2026-09-10T09:00:00Z")])
        printed = self._printed("widget")
        self.assertIn("golem__myproj", printed)
        self.assertIn("can we add a widget", printed)
        self.assertIn("1 turn in 1 conversation", printed)

    def test_the_counts_are_pluralised_over_both_axes(self):
        self.write(self.instance("golem__a"), "s.jsonl", [
            _turn("user", "widget one", "2026-09-10T09:00:00Z"),
            _turn("user", "widget two", "2026-09-10T09:01:00Z")])
        self.write(self.instance("golem__b"), "s.jsonl",
                   [_turn("user", "widget three", "2026-09-11T09:00:00Z")])
        self.assertIn("3 turns in 2 conversations", self._printed("widget"))

    def test_a_flood_of_hits_is_capped_and_the_rest_counted(self):
        # A term someone worked on all day would otherwise bury every other
        # conversation in the answer.
        self.write(self.instance("golem__busy"), "s.jsonl",
                   [_turn("user", f"widget {n}", f"2026-09-10T09:{n:02}:00Z")
                    for n in range(HITS_SHOWN + 4)])
        printed = self._printed("widget")
        self.assertIn("+ 4 more", printed)
        self.assertNotIn(f"widget {HITS_SHOWN + 3}", printed)

    def test_a_subagent_hit_says_it_was_a_subagent(self):
        member = self.member("devteam", "refactorer")
        self.write(member, "s/subagents/agent-1.jsonl",
                   [_turn("assistant", "found the widget", "2026-09-19T10:00:00Z",
                          isSidechain=True)])
        self.assertIn("(sub-agent)", self._printed("widget"))


class TestConversationFinds(unittest.TestCase):
    def test_when_is_the_newest_hit(self):
        hits = [history_find.TranscriptHit("user", "a", 100.0, 0, Path("/x"), False),
                history_find.TranscriptHit("user", "b", 300.0, 0, Path("/x"), False),
                history_find.TranscriptHit("user", "c", 200.0, 0, Path("/x"), False)]
        self.assertEqual(ConversationFinds("l", Path("/d"), hits).when, 300.0)


if __name__ == "__main__":
    unittest.main()
