"""Tests for launch.quickie — the `q` one-shot question tool. Covers the
docker-free parts: CLI parsing/dispatch, fixed-build Instance construction,
the empty-question guards, and the --history listing (formatting + walk). The
container run itself is the shared run_container (covered in test_docker_config's
TestRunContainerModes); the transcript read is test_file_access's
TestLastPromptInState."""

import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import paths
from launch.ai import ADAPTERS
from launch.paths import AGENTS_DIR, quickie_communal_workspace, quickie_state_dir_path
from launch.quickie import cli
from launch.quickie.ask import RESEARCH, TRIVIA, _gibberish, ask, build_quickie_instance
from launch.quickie.history import collect_history, format_time, one_line, print_answer
from launch.quickie.render import render_stream
from launch.tags import scan_all

REGISTRY = scan_all(AGENTS_DIR)


class TestBuildQuickieInstance(unittest.TestCase):
    """build_quickie_instance resolves the default (QUICK) _quickie lego into an
    Instance parked under quickie/ with the communal workspace."""

    def setUp(self):
        self.inst = build_quickie_instance(REGISTRY, "abc123")

    def test_default_build_is_quick_engine(self):
        self.assertEqual(self.inst.engine.name, "quick")
        self.assertEqual([p.name for p in self.inst.policies], ["web-research"])
        self.assertEqual(self.inst.professions, ())   # base image — no [code] toolchain

    def test_workspace_is_the_communal_dir(self):
        self.assertEqual(self.inst.workspace, str(quickie_communal_workspace()))

    def test_state_dir_parked_under_quickie_not_instances(self):
        self.assertEqual(self.inst.state_dir, quickie_state_dir_path("abc123"))
        self.assertIn("quickie", self.inst.state_dir.parts)
        self.assertNotIn("instances", self.inst.state_dir.parts)

    def test_instance_id_labelled_quickie(self):
        self.assertEqual(self.inst.instance, "quickie__abc123")


class TestAiChoice(unittest.TestCase):
    """`--ai <name>` swaps the lego's AI; the harness follows the AI's own
    default, because the lego's harness runs the lego's AI, not this one."""

    def test_the_default_is_the_legos_ai_claude(self):
        inst = build_quickie_instance(REGISTRY, "s")
        self.assertEqual((inst.ai.name, inst.harness.name), ("claude", "claude-code"))

    def test_another_ai_brings_its_own_default_cli(self):
        inst = build_quickie_instance(REGISTRY, "s", ai="gemini")
        self.assertEqual((inst.ai.name, inst.harness.name), ("gemini", REGISTRY.ais["gemini"].harness))
        self.assertEqual(inst.engine.name, "quick")          # the agent's build otherwise unchanged

    def test_the_choices_are_the_trees_ais_and_the_help_says_who_can_answer(self):
        names, runnable = cli.ai_choices(REGISTRY)
        self.assertEqual(names, sorted(REGISTRY.ais))
        self.assertEqual(runnable, ["claude"])                # the one adapted CLI today
        text = " ".join(cli.build_parser(REGISTRY).format_help().split())   # argparse wraps at the terminal width
        for name in names:
            self.assertIn(name, text)
        self.assertIn("can answer today: claude", text)
        self.assertIn("default: claude", text)


class TestResumeBuild(unittest.TestCase):
    """--resume reuses build_quickie_instance with is_brand_new=False so the
    shared compute_resume_flag can offer --continue against the pinned dir."""

    def test_default_is_brand_new(self):
        self.assertTrue(build_quickie_instance(REGISTRY, "abc123").is_brand_new)

    def test_resume_marks_not_brand_new_on_the_pinned_dir(self):
        inst = build_quickie_instance(REGISTRY, "abc123", is_brand_new=False)
        self.assertFalse(inst.is_brand_new)
        self.assertEqual(inst.state_dir, quickie_state_dir_path("abc123"))


class TestAgentSpecs(unittest.TestCase):
    """--explain / --research swap the persona+build; the default is QUICK."""

    def test_explain_uses_trivia_on_reliable(self):
        inst = build_quickie_instance(REGISTRY, "s", agent=TRIVIA)
        self.assertEqual(inst.engine.name, "reliable")
        self.assertEqual(inst.instance, "trivia__s")
        self.assertEqual([p.name for p in inst.policies], ["web-research"])

    def test_research_is_lean_researcher_without_code(self):
        inst = build_quickie_instance(REGISTRY, "s", agent=RESEARCH)
        self.assertEqual(inst.engine.name, "researcher")
        self.assertEqual(inst.professions, ())            # lean: base image, no [code]
        self.assertEqual([p.name for p in inst.policies], ["web-research"])
        self.assertEqual(inst.instance, "research__s")


class TestGibberish(unittest.TestCase):
    def test_hex_and_unique(self):
        a, b = _gibberish(), _gibberish()
        self.assertNotEqual(a, b)
        self.assertTrue(all(c in "0123456789abcdef" for c in a))


class TestAskGuard(unittest.TestCase):
    def test_a_lego_in_a_harness_without_an_adapter_exits_before_any_docker_work(self):
        unadapted = next(h for h in REGISTRY.harnesses.values() if h.name not in ADAPTERS)
        inst = dataclasses.replace(build_quickie_instance(REGISTRY, "abc123"), harness=unadapted)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        # An old-layout login pair at the state root: the CLI's first step is
        # the shared startup, so it is migrated before the parse, the refusal,
        # or any docker work.
        (state / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "REAL"}}')
        with patch.object(paths, "AGENTS_STATE", state), \
             patch("launch.quickie.ask.build_quickie_instance", return_value=inst), \
             patch("launch.quickie.ask.require_docker"), \
             patch("launch.quickie.ask.ensure_image") as image, \
             contextlib.redirect_stdout(io.StringIO()), \
             self.assertRaises(SystemExit) as caught:
            cli.main(["why?"])
        self.assertIn(unadapted.label, str(caught.exception))
        image.assert_not_called()
        self.assertTrue((state / "credentials" / "claude-code" / ".credentials.json").is_file())
        self.assertFalse((state / ".credentials.json").exists())

    def test_an_ai_whose_cli_has_no_adapter_is_refused_naming_the_flag(self):
        # `--ai gemini`: the lego's harness runs claude, so the AI's own default
        # CLI takes over — Gemini CLI, unadapted — and the refusal says which
        # AI answers through which CLI, since the user named the AI.
        with patch("launch.quickie.ask.require_docker"), \
             patch("launch.quickie.ask.ensure_image") as image, \
             patch.object(paths, "AGENTS_STATE", Path(tempfile.mkdtemp())), \
             contextlib.redirect_stdout(io.StringIO()), \
             self.assertRaises(SystemExit) as caught:
            ask("why?", REGISTRY, ai="gemini")
        message = str(caught.exception)
        self.assertIn("--ai gemini", message)
        self.assertIn(REGISTRY.harnesses["gemini-cli"].label, message)
        self.assertIn("no adapter", message)
        image.assert_not_called()

    def test_the_quickie_is_staged_like_every_other_shape(self):
        # Evidence (2) of the 2026-09-15 unification: the quickie mounted a
        # commands dir it never installed, so docker root-created the source.
        # Now the one staging installs it and mounts it read-only.
        from launch import docker_config
        from launch.container_env import _container_env
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        snapshot = dict(_container_env)
        self.addCleanup(lambda: (_container_env.clear(), _container_env.update(snapshot)))
        docker_config._docker_mounts.clear()
        self.addCleanup(docker_config._docker_mounts.clear)
        with patch.object(paths, "AGENTS_STATE", Path(tmp.name)), \
             patch("launch.quickie.ask._gibberish", return_value="abc123"), \
             patch("launch.quickie.ask.require_docker"), \
             patch("launch.quickie.ask.ensure_image", return_value="img"), \
             patch("launch.quickie.ask.run_container") as run, \
             contextlib.redirect_stdout(io.StringIO()):
            ask("why?", REGISTRY)
            state = quickie_state_dir_path("abc123")   # while AGENTS_STATE is still redirected
        run.assert_called_once()
        self.assertTrue((state / "commands").is_dir())
        self.assertIn((str(state / "commands"), "/home/claude/.claude/commands:ro"), docker_config.staged_mounts())
        self.assertIn((str(state / "settings.json"), "/home/claude/.claude/settings.json:ro"), docker_config.staged_mounts())
        self.assertIn("BASH_ENV", {str(k) for k in _container_env})
        self.assertEqual(str(_container_env["CLAUDE_AGENT_INSTANCE"]), "quickie__abc123")

    def test_empty_question_exits_before_any_docker_work(self):
        # The empty/whitespace guard is the first thing ask() does — no
        # scan_all, no require_docker, so it's safe to exercise here.
        with self.assertRaises(SystemExit):
            ask("   ", REGISTRY)

    def test_empty_followup_names_the_resume_command(self):
        # Same first-thing guard on the resume path, with a resume-specific hint.
        with self.assertRaises(SystemExit) as cm:
            ask("  ", REGISTRY, resume_session="abc123")
        self.assertIn("--resume abc123", str(cm.exception))


class TestCli(unittest.TestCase):
    """cli.main opens the launcher (the one startup — stubbed here to the
    scanned tree), parses argv and routes: a bare question → a fresh QUICK
    ask on the lego's AI; --explain/--research pick the agent; --ai picks the
    AI; --resume continues; --history/--answer are standalone displays.
    argparse owns -h and rejects --explain+--research."""

    def setUp(self):
        opener = patch.object(cli, "open_launcher", return_value=REGISTRY)
        opener.start()
        self.addCleanup(opener.stop)

    def test_bare_question_joined_and_asked_fresh(self):
        with patch.object(cli, "ask") as ask_mock:
            cli.main(["why", "the", "sky"])
        ask_mock.assert_called_once_with("why the sky", REGISTRY, resume_session=None, agent=cli.QUICK, ai=None)

    def test_ai_flag_reaches_the_ask_and_an_unknown_ai_is_refused_by_the_parser(self):
        with patch.object(cli, "ask") as ask_mock:
            cli.main(["--ai", "gemini", "why"])
        ask_mock.assert_called_once_with("why", REGISTRY, resume_session=None, agent=cli.QUICK, ai="gemini")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--ai", "hal9000", "why"])

    def test_the_startup_runs_before_the_parse_so_every_verb_sees_the_migrated_state(self):
        calls = []
        with patch.object(cli, "open_launcher", side_effect=lambda: calls.append("opened") or REGISTRY), \
             patch.object(cli, "print_history", side_effect=lambda: calls.append("history")):
            cli.main(["--history"])
        self.assertEqual(calls, ["opened", "history"])

    def test_resume_routes_to_ask_with_session(self):
        with patch.object(cli, "quickie_state_dir_path") as thread, patch.object(cli, "ask") as ask_mock:
            thread.return_value.is_dir.return_value = True      # "abc123" names a thread on disk
            cli.main(["--resume", "abc123", "and", "clouds?"])
        ask_mock.assert_called_once_with("and clouds?", REGISTRY, resume_session="abc123", agent=cli.QUICK, ai=None)

    def test_explain_selects_trivia(self):
        with patch.object(cli, "ask") as ask_mock:
            cli.main(["--explain", "how do rainbows work"])
        ask_mock.assert_called_once_with("how do rainbows work", REGISTRY, resume_session=None, agent=cli.TRIVIA, ai=None)

    def test_research_selects_research(self):
        with patch.object(cli, "ask") as ask_mock:
            cli.main(["--research", "latest on fusion"])
        ask_mock.assert_called_once_with("latest on fusion", REGISTRY, resume_session=None, agent=cli.RESEARCH, ai=None)

    def test_resume_combines_with_agent_flag(self):
        with patch.object(cli, "quickie_state_dir_path") as thread, patch.object(cli, "ask") as ask_mock:
            thread.return_value.is_dir.return_value = True
            cli.main(["--resume", "id1", "--explain", "more"])
        ask_mock.assert_called_once_with("more", REGISTRY, resume_session="id1", agent=cli.TRIVIA, ai=None)

    def test_explain_and_research_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--explain", "--research", "q"])

    def test_history_lists_and_never_asks(self):
        with patch.object(cli, "print_history") as hist, patch.object(cli, "ask") as ask_mock:
            cli.main(["--history"])
        hist.assert_called_once_with()
        ask_mock.assert_not_called()

    def test_answer_shows_saved_answer_and_never_asks(self):
        with patch.object(cli, "print_answer") as ans, patch.object(cli, "ask") as ask_mock:
            cli.main(["--answer", "abc123"])
        ans.assert_called_once_with("abc123")
        ask_mock.assert_not_called()

    def test_history_rejects_other_args(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--history", "--resume", "x"])

    def test_answer_rejects_a_trailing_question(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--answer", "abc", "extra question"])

    def test_dash_h_is_our_help_not_claudes(self):
        # argparse consumes -h → prints q's own help → exits 0, so it never
        # reaches run_container / claude.
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(buf):
            cli.main(["-h"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("--explain", buf.getvalue())
        self.assertIn("--answer", buf.getvalue())
        # argparse may hyphen-wrap the path across lines; compare whitespace-stripped.
        self.assertIn("~/.ai-agents/quickie/communal/", "".join(buf.getvalue().split()))


class TestHistoryOneLine(unittest.TestCase):
    """one_line squeezes a prompt to a single 180-char line for the listing."""

    def test_short_prompt_unchanged(self):
        self.assertEqual(one_line("why is the sky blue?"), "why is the sky blue?")

    def test_newlines_and_whitespace_runs_collapsed(self):
        self.assertEqual(one_line("a\n\nb   c\td"), "a b c d")

    def test_exactly_180_not_truncated(self):
        p = "y" * 180
        self.assertEqual(one_line(p), p)

    def test_over_180_cut_with_ellipsis(self):
        out = one_line("x" * 250)
        self.assertEqual(out[:180], "x" * 180)
        self.assertTrue(out.endswith("…"))
        self.assertEqual(len(out), 181)   # 180 kept + the ellipsis


class TestFormatTime(unittest.TestCase):
    def test_shape_is_local_date_hh_mm(self):
        self.assertRegex(format_time(1_800_000_000.0), r"^\d{4}-\d\d-\d\d \d\d:\d\d$")


def _sev(inner):
    return json.dumps({"type": "stream_event", "event": inner})


def _text_delta(text):
    return _sev({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})


def _thinking_start():
    return _sev({"type": "content_block_start", "content_block": {"type": "thinking"}})


def _result(subtype="success", **extra):
    return json.dumps({"type": "result", "subtype": subtype, **extra})


def _assistant(text):
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})


class TestRenderStream(unittest.TestCase):
    """render_stream turns Claude Code stream-json into stdout answer text +
    stderr progress. tick=False disables the timer thread for determinism."""

    def _render(self, lines, tick=False, markdown=False):
        # markdown=False by default: these tests pin the SOURCE the model
        # wrote, which is what a pipe or a redirect receives. The rendered
        # path has its own class below.
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            render_stream(iter(lines), tick=tick, markdown=markdown)
        return out.getvalue(), err.getvalue()

    def test_streams_answer_text_deltas_to_stdout(self):
        out, _ = self._render([_thinking_start(), _text_delta("Hello, "), _text_delta("world."), _result()])
        self.assertEqual(out, "Hello, world.\n")

    def test_no_answer_means_no_stdout(self):
        out, _ = self._render([_thinking_start(), _result()])
        self.assertEqual(out, "")

    def test_malformed_and_blank_lines_skipped(self):
        out, _ = self._render(["not json", "", _text_delta("ok"), "{bad", _result()])
        self.assertEqual(out, "ok\n")

    def test_result_error_noted_on_stderr_not_stdout(self):
        out, err = self._render([_thinking_start(), _result("error_max_turns")])
        self.assertEqual(out, "")
        self.assertIn("error_max_turns", err)

    def test_a_not_logged_in_run_is_reported_in_the_clis_words_with_the_way_to_log_in(self):
        # THE 2026-09-16 report: the CLI delivers the failure as a whole
        # `assistant` event plus a `result` with subtype "success" and
        # is_error true — no text_delta, a "success" subtype — so nothing was
        # printed and the words could only be read back with `q --answer`.
        said = "Not logged in · Please run /login"
        out, err = self._render([_assistant(said), _result("success", is_error=True, result=said)])
        self.assertEqual(out, "")                       # never on the answer channel
        self.assertIn(said, err)
        self.assertIn("log in once through a normal launch", err)

    def test_an_error_result_without_its_own_words_falls_back_to_the_assistants(self):
        _, err = self._render([_assistant("Rate limited"), _result("error_during_execution", is_error=True)])
        self.assertIn("Rate limited", err)
        self.assertNotIn("log in once", err)

    def test_a_success_that_never_streamed_prints_the_assistants_message(self):
        out, err = self._render([_assistant("The answer."), _result("success")])
        self.assertEqual(out, "The answer.\n")
        self.assertEqual(err, "")

    def test_a_streamed_answer_is_not_printed_twice_by_its_snapshot(self):
        out, _ = self._render([_text_delta("Hel"), _text_delta("lo."), _assistant("Hello."), _result("success")])
        self.assertEqual(out, "Hello.\n")

    def test_a_stream_that_ends_without_a_result_is_never_silent(self):
        out, err = self._render([_thinking_start()])
        self.assertEqual(out, "")
        self.assertIn("ended without an answer", err)

    def test_tick_true_still_renders_answer(self):
        # Exercises the real ticker start/stop path without asserting its
        # (timing-dependent) stderr output — the answer must be unaffected.
        out, _ = self._render([_thinking_start(), _text_delta("hi"), _result()], tick=True)
        self.assertEqual(out, "hi\n")


class TestCollectHistory(unittest.TestCase):
    """collect_history walks ~/.ai-agents/quickie/, reads each thread's last
    question from its transcript, skips the communal workspace and threads with
    no question, and returns them ascending by date."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.patcher = patch.object(paths, "AGENTS_STATE", Path(self.tmpdir.name))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmpdir.cleanup()

    def _thread(self, name, prompt=None, ts=None):
        tx = quickie_state_dir_path(name) / "projects" / "-workspace"
        tx.mkdir(parents=True)
        if prompt is not None:
            event = {"type": "user", "message": {"role": "user", "content": prompt},
                     "timestamp": ts}
            (tx / "s.jsonl").write_text(json.dumps(event) + "\n")

    def test_sorted_ascending_skipping_communal_and_promptless(self):
        quickie_communal_workspace().mkdir(parents=True)     # the shared workspace, not a thread
        self._thread("newer", "second", "2026-02-01T00:00:00.000Z")
        self._thread("older", "first", "2026-01-01T00:00:00.000Z")
        self._thread("blank")                                # no question yet → skipped
        result = collect_history()
        self.assertEqual([(tid, prompt) for _, tid, prompt in result],
                         [("older", "first"), ("newer", "second")])
        self.assertLess(result[0][0], result[1][0])          # ascending by timestamp

    def test_empty_when_no_threads(self):
        self.assertEqual(collect_history(), [])


class TestPrintAnswer(unittest.TestCase):
    """print_answer prints a thread's saved answer, or exits with a note."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.patcher = patch.object(paths, "AGENTS_STATE", Path(self.tmpdir.name))
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmpdir.cleanup()

    def _thread_with_answer(self, name, answer):
        tx = quickie_state_dir_path(name) / "projects" / "-workspace"
        tx.mkdir(parents=True)
        events = [
            {"type": "user", "message": {"role": "user", "content": "q"},
             "timestamp": "2026-01-01T00:00:00.000Z"},
            {"type": "assistant", "message": {"role": "assistant",
             "content": [{"type": "text", "text": answer}]}, "timestamp": "2026-01-01T00:00:05.000Z"},
        ]
        (tx / "s.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")

    def test_prints_saved_answer(self):
        self._thread_with_answer("t1", "Because physics.")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            print_answer("t1")
        self.assertEqual(out.getvalue().strip(), "Because physics.")

    def test_unknown_thread_exits(self):
        with self.assertRaises(SystemExit):
            print_answer("nope")

    def test_thread_without_answer_exits(self):
        (quickie_state_dir_path("t2") / "projects" / "-workspace").mkdir(parents=True)
        with self.assertRaises(SystemExit):
            print_answer("t2")


if __name__ == "__main__":
    unittest.main()


def _plain(text: str) -> str:
    """ANSI escapes stripped — the rendered answer's SHAPE without asserting
    on colour, which varies with the terminal rich detects."""
    import re
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)


class TestMarkdownAnswer(unittest.TestCase):
    """A terminal gets the answer RENDERED — headings, bold, lists, code — and
    a pipe gets the source, because `q … > notes.md` must stay valid markdown
    and a script parsing the answer must not meet ANSI escapes."""

    SOURCE = "# Title\n\nSome **bold** and `code`.\n\n- first\n- second\n"

    def _render(self, lines, markdown):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            render_stream(iter(lines), tick=False, markdown=markdown)
        return _plain(out.getvalue()), err.getvalue()

    def _answer_lines(self):
        return [_text_delta(self.SOURCE), _result()]

    def test_a_terminal_gets_it_rendered(self):
        out, _ = self._render(self._answer_lines(), markdown=True)
        self.assertIn("Title", out)
        self.assertNotIn("# Title", out)        # the hash is a heading, not text
        self.assertNotIn("**bold**", out)       # the asterisks are emphasis, not text
        self.assertIn("bold", out)
        self.assertIn("•", out)                 # the dashes are a list

    def test_a_pipe_gets_exactly_what_the_model_wrote(self):
        out, _ = self._render(self._answer_lines(), markdown=False)
        self.assertEqual(out, self.SOURCE + "\n")

    def test_an_answer_that_never_streamed_is_rendered_too(self):
        # A harness that sends no deltas delivers one assistant event; the
        # reader should not be able to tell which arrived.
        out, _ = self._render([_assistant(self.SOURCE), _result("success")], markdown=True)
        self.assertIn("Title", out)
        self.assertNotIn("# Title", out)

    def test_a_failed_run_renders_nothing_on_stdout_either_way(self):
        # The Live must never open for an answer that does not come: the
        # error report owns the terminal then.
        for markdown in (True, False):
            with self.subTest(markdown=markdown):
                out, err = self._render([_result("success", is_error=True, result="Not logged in")], markdown=markdown)
                self.assertEqual(out, "")
                self.assertIn("Not logged in", err)

    def test_the_rule_is_stdout_being_a_terminal(self):
        from launch.quickie import render
        self.assertTrue(render._wants_markdown(True))
        self.assertFalse(render._wants_markdown(False))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(render._wants_markdown())          # a StringIO is not a terminal
        with patch.object(render.sys, "stdout", object()):      # nor is a stdout with no isatty
            self.assertFalse(render._wants_markdown())

    def test_the_saved_answer_reprints_the_way_it_was_shown(self):
        from launch.quickie import history, render
        state = tempfile.mkdtemp()
        with patch.object(history, "quickie_state_dir_path", return_value=Path(state)), \
             patch.object(history, "last_answer_in_state", return_value=(self.SOURCE, 0.0)), \
             patch.object(render, "_wants_markdown", return_value=True), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            history.print_answer("t1")
        self.assertNotIn("# Title", _plain(out.getvalue()))
        self.assertIn("Title", _plain(out.getvalue()))


class TestTheCliSaysWhatHappened(unittest.TestCase):
    """A run that produced no answer must SAY so when it happens, whatever
    shape the refusal took — the 2026-09-18 report: a `Not logged in` quickie
    printed nothing after the docker output and the message could only be read
    back with `q --answer`."""

    def _render(self, lines, login_hint="HINT"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            render_stream(iter(lines), tick=False, markdown=False, login_hint=login_hint)
        return out.getvalue(), err.getvalue()

    def test_a_plain_text_refusal_on_stdout_is_reported_not_swallowed(self):
        # THE bug: a CLI that refuses before opening the event stream prints
        # plain text, and every non-JSON line used to be dropped as noise.
        out, err = self._render(["Not logged in · Please run /login"])
        self.assertEqual(out, "")                       # not an answer
        self.assertIn("Not logged in", err)             # but not lost either
        self.assertIn("HINT", err)                      # with the caller's login hint

    def test_a_json_error_result_is_reported_with_its_words(self):
        out, err = self._render([_result("success", is_error=True, result="Not logged in · Please run /login")])
        self.assertEqual(out, "")
        self.assertIn("Not logged in", err)
        self.assertIn("HINT", err)

    def test_stray_output_is_tailed_not_dumped(self):
        noisy = [f"line {i}" for i in range(40)]
        _, err = self._render(noisy)
        self.assertIn("line 39", err)
        self.assertNotIn("line 0 ", err)                # capped at the last few
        self.assertLess(len(err), 400)

    def test_a_successful_answer_is_unaffected_by_stray_lines(self):
        out, err = self._render(["a warm-up line the CLI printed", _text_delta("the answer"), _result()])
        self.assertEqual(out, "the answer\n")
        self.assertEqual(err, "")

    def test_the_hint_names_what_the_launcher_mounted(self):
        # So the NEXT report says which credential state produced the refusal.
        # `launch.quickie.ask` is the FUNCTION (the package re-exports it), so
        # the module is imported by path.
        from launch.quickie.ask import _mounted_credentials
        inst = build_quickie_instance(REGISTRY, "abc123")
        with patch.object(paths, "AGENTS_STATE", Path(tempfile.mkdtemp())):
            line = _mounted_credentials(inst)
        self.assertIn(".credentials.json", line)
        self.assertIn(".claude.json", line)
        self.assertIn("API key file", line)


class TestAnswerDefaultsToTheLatest(unittest.TestCase):
    """`q --answer` with no id prints the answer to the last question —
    typing a gibberish id to read what you just asked was the friction."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(paths, "AGENTS_STATE", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _thread(self, session, prompt, answer, when):
        import json as _json
        state = quickie_state_dir_path(session)
        transcript = state / "projects" / "-workspace" / "s.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(
            _json.dumps({"type": "user", "timestamp": when, "message": {"role": "user", "content": prompt}}) + "\n"
            + _json.dumps({"type": "assistant", "timestamp": when, "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}}) + "\n")

    def test_no_id_prints_the_newest_threads_answer(self):
        from launch.quickie import history
        self._thread("older", "first?", "the older answer", "2026-09-01T10:00:00Z")
        self._thread("newer", "second?", "the newer answer", "2026-09-18T10:00:00Z")
        self.assertEqual(history.latest_thread(), "newer")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            history.print_answer()
        self.assertIn("the newer answer", out.getvalue())

    def test_an_id_still_wins(self):
        from launch.quickie import history
        self._thread("older", "first?", "the older answer", "2026-09-01T10:00:00Z")
        self._thread("newer", "second?", "the newer answer", "2026-09-18T10:00:00Z")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            history.print_answer("older")
        self.assertIn("the older answer", out.getvalue())

    def test_no_threads_at_all_says_how_to_make_one(self):
        from launch.quickie import history
        with self.assertRaises(SystemExit) as caught:
            history.print_answer()
        self.assertIn("No quickie threads yet", str(caught.exception))

    def test_the_flag_takes_an_optional_id_and_routes_both_ways(self):
        with patch.object(cli, "open_launcher", return_value=REGISTRY), \
             patch.object(cli, "print_answer") as answer, patch.object(cli, "ask") as ask_mock:
            cli.main(["--answer"])
            cli.main(["--answer", "abc123"])
        self.assertEqual([c.args[0] for c in answer.call_args_list], ["", "abc123"])
        ask_mock.assert_not_called()

    def test_it_still_refuses_to_be_combined_with_a_question(self):
        with patch.object(cli, "open_launcher", return_value=REGISTRY), \
             self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--answer", "--history"])


class TestResumeDefaultsToTheLatest(unittest.TestCase):
    """`q --resume "follow-up"` continues the last thread — the same default
    `--answer` takes. The catch argparse cannot solve: `--resume` takes an
    OPTIONAL id, so it swallows the question as the id; the disk decides."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(paths, "AGENTS_STATE", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.parser_registry = REGISTRY

    def _thread(self, session, prompt, when):
        import json as _json
        transcript = quickie_state_dir_path(session) / "projects" / "-workspace" / "s.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(_json.dumps(
            {"type": "user", "timestamp": when, "message": {"role": "user", "content": prompt}}) + "\n")

    def _split(self, argv):
        return cli.resume_and_question(cli.build_parser(self.parser_registry).parse_args(argv))

    def test_a_question_after_the_flag_is_not_mistaken_for_an_id(self):
        # THE trap: argparse gives "and" to --resume and the rest to the
        # question. No thread is named "and", so it is the question's first word.
        self.assertEqual(self._split(["--resume", "and", "their", "trunks?"]),
                         ("", "and their trunks?"))

    def test_a_real_id_is_taken_as_the_thread(self):
        quickie_state_dir_path("a1b2c3d4e5f6").mkdir(parents=True)
        self.assertEqual(self._split(["--resume", "a1b2c3d4e5f6", "more", "please"]),
                         ("a1b2c3d4e5f6", "more please"))

    def test_the_bare_flag_means_the_latest_and_no_question(self):
        self.assertEqual(self._split(["--resume"]), ("", ""))
        self.assertEqual(self._split(["why", "the", "sky"]), (None, "why the sky"))

    def test_ask_resolves_the_empty_id_to_the_newest_thread(self):
        self._thread("older", "first?", "2026-09-01T10:00:00Z")
        self._thread("newer", "second?", "2026-09-18T10:00:00Z")
        with patch("launch.quickie.ask.require_docker"), \
             patch("launch.quickie.ask.ensure_image", return_value="img"), \
             patch("launch.quickie.ask.run_container"), \
             patch("launch.quickie.ask.build_quickie_instance", wraps=build_quickie_instance) as built, \
             contextlib.redirect_stdout(io.StringIO()):
            ask("and then?", REGISTRY, resume_session="")
        self.assertEqual(built.call_args.args[1], "newer")
        self.assertFalse(built.call_args.kwargs["is_brand_new"])   # a continuation, so --continue is offered

    def test_resuming_with_no_threads_says_how_to_start_one(self):
        # require_docker is patched out: the docker gate runs first by design
        # (nothing can run without it), and this test is about what follows.
        with patch("launch.quickie.ask.require_docker"), self.assertRaises(SystemExit) as caught:
            ask("and then?", REGISTRY, resume_session="")
        self.assertIn("No quickie threads yet", str(caught.exception))

    def test_an_empty_follow_up_names_the_command_without_a_gap(self):
        with self.assertRaises(SystemExit) as caught:
            ask("  ", REGISTRY, resume_session="")
        self.assertIn('q --resume "your question"', str(caught.exception))
        with self.assertRaises(SystemExit) as named:
            ask("  ", REGISTRY, resume_session="abc123")
        self.assertIn('q --resume abc123 "your question"', str(named.exception))

    def test_history_still_rejects_a_resume_beside_it(self):
        with patch.object(cli, "open_launcher", return_value=REGISTRY), \
             self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--history", "--resume"])
