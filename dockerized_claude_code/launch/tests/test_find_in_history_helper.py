"""Tests for settings/_find_in_history.py — the in-container half of
`--find`, behind the muxer's alt+f and the `find_in_history` shell function.

Not part of the launch package (it is bind-mounted into containers), so it
is loaded by file path like `_summary.py`'s tests do. The import itself is
part of the contract: the script reaches the launcher's own parser through
the file mounted BESIDE it, and must keep working where no `launch` package
exists — which is why the loader below registers that parser under its
CONTAINER name (`_transcript_format`) instead of letting the script find
`launch.transcript_format`, which would only ever resolve here.
"""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _ROOT / "settings" / "_find_in_history.py"
_PARSER = _ROOT / "launch" / "transcript_format.py"


def _load_helper():
    """The script as the container loads it: the parser importable under its
    MOUNTED name, with no `launch` package in sight."""
    spec = importlib.util.spec_from_file_location("_transcript_format", _PARSER)
    parser = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered BEFORE exec: the parser's frozen dataclass resolves its own
    # annotations through sys.modules, and `from __future__ import
    # annotations` makes that lookup happen while the module is still being
    # executed. The container gets this for free by importing normally.
    sys.modules["_transcript_format"] = parser
    spec.loader.exec_module(parser)
    spec = importlib.util.spec_from_file_location("find_in_history_helper", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


HELPER = _load_helper()


def _turn(speaker, text, stamp, **extra):
    return {"type": speaker, "message": {"role": speaker, "content": text},
            "timestamp": stamp, **extra}


class _Container(unittest.TestCase):
    """A tmp tree shaped like a container's config dir."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)

    def transcripts(self, projects: Path, name: str, events: list[dict]) -> Path:
        path = projects / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
        return path


class TestTranscriptsRoot(_Container):
    """Where the script looks — the launcher's staged env, never a guess."""

    def test_the_staged_env_wins(self):
        with patch.dict("os.environ", {"AGENT_TRANSCRIPTS_DIR": "/cluster/members/x/projects"}):
            self.assertEqual(HELPER.transcripts_root(),
                             Path("/cluster/members/x/projects"))

    def test_without_the_env_it_falls_back_to_the_solo_default(self):
        # Only reachable in a shell started outside a launcher container.
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(HELPER.transcripts_root().name, "projects")


class TestSearchRoots(_Container):
    """What a container may search: itself, or the whole cluster it is in."""

    def test_a_solo_instance_searches_only_itself(self):
        projects = self.root / ".claude" / "projects"
        projects.mkdir(parents=True)
        self.assertEqual(HELPER.search_roots(projects), [("this session", projects)])

    def test_a_cluster_member_searches_every_sibling(self):
        # The question alt+f exists for is "did anyone handle this", and in a
        # cluster that includes the seats next to you.
        members = self.root / "members"
        for name in ("refactorer", "poet", "golem"):
            (members / name / "projects").mkdir(parents=True)
        roots = HELPER.search_roots(members / "refactorer" / "projects")
        self.assertEqual([label for label, _ in roots], ["golem", "poet", "refactorer"])

    def test_the_walk_never_climbs_above_the_members_dir(self):
        # /cluster is mounted whole; the search must still stay inside the
        # cluster's own member dirs and reach nothing else on the filesystem.
        members = self.root / "members"
        (members / "refactorer" / "projects").mkdir(parents=True)
        (self.root / "secrets").mkdir()
        for _, path in HELPER.search_roots(members / "refactorer" / "projects"):
            with self.subTest(path=path):
                self.assertTrue(path.is_relative_to(members))

    def test_a_members_shaped_name_that_is_not_a_dir_is_not_a_cluster(self):
        projects = self.root / "members" / "projects"   # too shallow to be a member
        projects.mkdir(parents=True)
        (label, _), = HELPER.search_roots(projects)
        self.assertEqual(label, "this session")


class TestFind(_Container):
    """The search itself — the same rules the host applies, because it is
    the same parser."""

    def setUp(self):
        super().setUp()
        self.projects = self.root / ".claude" / "projects"
        self.roots = [("this session", self.projects)]

    def test_finds_a_spoken_turn(self):
        self.transcripts(self.projects, "-workspace/s.jsonl",
                         [_turn("user", "about the widget", "2026-09-10T09:00:00Z")])
        ((label, hits),) = HELPER.find("widget", self.roots)
        self.assertEqual(label, "this session")
        self.assertEqual(len(hits), 1)

    def test_a_tool_result_is_not_something_anyone_said(self):
        # The shared parser's rule, exercised here so the CONTAINER path is
        # known to have it too — a look-alike reader would grep the line.
        self.transcripts(self.projects, "-workspace/s.jsonl",
                         [_turn("user", [{"type": "tool_result", "content": "widget"}],
                                "2026-09-10T09:00:00Z")])
        self.assertEqual(HELPER.find("widget", self.roots), [])

    def test_subagent_transcripts_are_searched_too(self):
        self.transcripts(self.projects, "-workspace/s/subagents/agent-1.jsonl",
                         [_turn("assistant", "the widget is here",
                                "2026-09-10T09:00:00Z", isSidechain=True)])
        ((_, hits),) = HELPER.find("widget", self.roots)
        self.assertTrue(hits[0].sidechain)

    def test_case_insensitive(self):
        self.transcripts(self.projects, "-workspace/s.jsonl",
                         [_turn("user", "the Widget", "2026-09-10T09:00:00Z")])
        self.assertEqual(len(HELPER.find("WIDGET", self.roots)), 1)

    def test_an_unreadable_file_costs_only_its_own_hits(self):
        good = self.transcripts(self.projects, "-workspace/a.jsonl",
                                [_turn("user", "widget", "2026-09-10T09:00:00Z")])
        bad = self.transcripts(self.projects, "-workspace/b.jsonl", [])
        bad.chmod(0o000)
        self.addCleanup(bad.chmod, 0o644)
        ((_, hits),) = HELPER.find("widget", self.roots)
        self.assertEqual(hits[0].source, good)

    def test_missing_projects_dir_finds_nothing(self):
        self.assertEqual(HELPER.find("widget", [("gone", self.root / "nope")]), [])


class TestReport(_Container):
    """The popup's text. It must say what it searched — a member seeing
    'nothing found' has to know whether that covered the whole cluster."""

    def _printed(self, term, roots, found):
        out = io.StringIO()
        with redirect_stdout(out):
            HELPER.report(term, roots, found)
        return out.getvalue()

    def test_the_scope_is_stated_for_one_root(self):
        printed = self._printed("widget", [("this session", Path("/p"))], [])
        self.assertIn("/p", printed)
        self.assertIn('Nothing said "widget"', printed)

    def test_the_scope_names_the_member_count_in_a_cluster(self):
        roots = [("a", Path("/a")), ("b", Path("/b")), ("c", Path("/c"))]
        self.assertIn("3 cluster members", self._printed("widget", roots, []))

    def test_hits_are_capped_and_the_rest_counted(self):
        self.projects = self.root / ".claude" / "projects"
        self.transcripts(self.projects, "-workspace/s.jsonl",
                         [_turn("user", f"widget {n}", f"2026-09-10T09:{n:02}:00Z")
                          for n in range(HELPER.HITS_SHOWN + 2)])
        roots = [("this session", self.projects)]
        printed = self._printed("widget", roots, HELPER.find("widget", roots))
        self.assertIn("+ 2 more", printed)


class TestMain(_Container):
    def test_a_term_on_the_command_line_needs_no_prompt(self):
        with patch.dict("os.environ", {"AGENT_TRANSCRIPTS_DIR": str(self.root / "projects")}), \
             patch("builtins.input", side_effect=AssertionError("must not prompt")), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(HELPER.main(["widget"]), 0)

    def test_no_term_asks_for_one(self):
        with patch.dict("os.environ", {"AGENT_TRANSCRIPTS_DIR": str(self.root / "projects")}), \
             patch("builtins.input", return_value="widget"), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(HELPER.main([]), 0)

    def test_an_empty_answer_exits_without_searching(self):
        with patch("builtins.input", return_value="  "), redirect_stdout(io.StringIO()):
            self.assertEqual(HELPER.main([]), 1)

    def test_ctrl_c_at_the_prompt_is_not_a_traceback(self):
        with patch("builtins.input", side_effect=KeyboardInterrupt), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(HELPER.main([]), 1)


if __name__ == "__main__":
    unittest.main()
