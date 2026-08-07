"""Tests for launch.file_access — the agent-md index, the OAuth file
ensure step, and the small helpers in this module.

Filesystem-touching tests use tmpdir + targeted patches so they don't depend
on the host's actual launcher state."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from launch import file_access


# ============================================================
# agent_md_index — agents/ name → md-path index
# ============================================================


class TestAgentMdIndex(unittest.TestCase):
    """Post-rewrite the stem IS the agent name — the index is a plain sorted
    glob (the `[tag](parent)` filename grammar is retired; axes live in the
    agent's `.lego`)."""

    def _index(self, filenames):
        with tempfile.TemporaryDirectory() as d:
            for fn in filenames:
                (Path(d) / fn).write_text("# stub\n")
            return file_access._agent_md_index(Path(d))

    def test_stems_index_verbatim(self):
        index = self._index(["golem.md", "researcher.md", "kid.md"])
        self.assertEqual(set(index), {"golem", "researcher", "kid"})

    def test_non_md_files_ignored(self):
        index = self._index(["golem.md", "golem.lego", "notes.txt"])
        self.assertEqual(set(index), {"golem"})

    def test_underscore_prefixed_agents_excluded(self):
        # `_`-prefixed agents (e.g. _quickie) are hidden — driven by their own
        # entry point, never surfaced in the picker / CLI resolve / audit.
        index = self._index(["golem.md", "_quickie.md"])
        self.assertEqual(set(index), {"golem"})

    def test_empty_dir_yields_empty_index(self):
        self.assertEqual(self._index([]), {})

    def test_cached_accessor_returns_same_object(self):
        # agent_md_index is lru_cached — repeated calls share one dict (and
        # one disk scan) per process.
        self.assertIs(file_access.agent_md_index(), file_access.agent_md_index())

    def test_real_agents_dir_indexes_every_visible_md(self):
        # The repo's shipped agents/ must all be well-formed — one index entry
        # per .md file, minus the `_`-prefixed hidden agents (e.g. _quickie).
        from launch.paths import AGENTS_DIR
        visible = [p for p in AGENTS_DIR.glob("*.md") if not p.stem.startswith("_")]
        self.assertEqual(len(file_access.agent_md_index()), len(visible))


# ============================================================
# write_text — atomic replace semantics
# ============================================================


class TestWriteTextAtomic(unittest.TestCase):
    """write_text goes through a same-directory temp file + os.replace so an
    interrupt mid-write can never truncate existing state (the JSON maps were
    previously corruptible by a Ctrl+C landing inside the write)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "sub" / "out.txt"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_writes_content_and_creates_parents(self):
        file_access.write_text(self.path, "hello")
        self.assertEqual(self.path.read_text(), "hello")

    def test_overwrites_existing_content(self):
        file_access.write_text(self.path, "first")
        file_access.write_text(self.path, "second")
        self.assertEqual(self.path.read_text(), "second")

    def test_no_temp_litter_after_write(self):
        file_access.write_text(self.path, "hello")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["out.txt"])

    def test_failed_replace_preserves_original_and_cleans_temp(self):
        # The atomicity contract: if anything goes wrong before the final
        # rename, the original file is untouched and no temp file lingers.
        file_access.write_text(self.path, "original")
        with patch("launch.file_access.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                file_access.write_text(self.path, "partial")
        self.assertEqual(self.path.read_text(), "original")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["out.txt"])


# ============================================================
# installed_cred_clis — derived from present_optional_cred_services
# ============================================================


class TestInstalledCredClis(unittest.TestCase):
    """installed_cred_clis returns space-joined CLI names for services that
    are (a) present on host AND (b) have a non-None CLI in OPTIONAL_CREDS_MOUNTS."""

    def setUp(self):
        # Clear the lru_cache before each test so we control what
        # present_optional_cred_services returns via patching.
        file_access.present_optional_cred_services.cache_clear()

    def tearDown(self):
        file_access.present_optional_cred_services.cache_clear()

    def _with_present(self, present):
        return patch.object(
            file_access, "present_optional_cred_services",
            return_value=frozenset(present),
        )

    def test_no_creds_empty_string(self):
        with self._with_present(set()):
            self.assertEqual(file_access.installed_cred_clis(), "")

    def test_single_cred_with_cli(self):
        with self._with_present({"gh"}):
            self.assertEqual(file_access.installed_cred_clis(), "gh")

    def test_multiple_creds_space_joined(self):
        with self._with_present({"gh", "aws"}):
            # Order follows OPTIONAL_CREDS_MOUNTS declaration (aws appears before gh)
            self.assertEqual(file_access.installed_cred_clis(), "aws gh")

    def test_kube_renders_as_kubectl(self):
        # service name `kube` → CLI binary `kubectl`
        with self._with_present({"kube"}):
            self.assertEqual(file_access.installed_cred_clis(), "kubectl")

    def test_cli_less_services_excluded(self):
        # npmrc/pypirc have cli=None — they don't appear in the output even
        # when present on host.
        with self._with_present({"npmrc", "pypirc"}):
            self.assertEqual(file_access.installed_cred_clis(), "")

    def test_mix_with_and_without_clis(self):
        with self._with_present({"gh", "npmrc"}):
            self.assertEqual(file_access.installed_cred_clis(), "gh")

    def test_unknown_service_silently_skipped(self):
        # A service that's "present" but not in OPTIONAL_CREDS_MOUNTS at all
        # is just ignored — iteration is keyed off OPTIONAL_CREDS_MOUNTS.items().
        with self._with_present({"bogus", "gh"}):
            self.assertEqual(file_access.installed_cred_clis(), "gh")


# ============================================================
# Small helpers
# ============================================================


class TestParseLines(unittest.TestCase):
    """parse_lines yields non-blank, non-comment lines."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "list.txt"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_skips_blank_lines(self):
        self.path.write_text("a\n\nb\n\n\nc\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), ["a", "b", "c"])

    def test_skips_comment_lines(self):
        self.path.write_text("a\n# comment\nb\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), ["a", "b"])

    def test_strips_each_line(self):
        self.path.write_text("  spacy  \n\ttab\t\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), ["spacy", "tab"])

    def test_missing_file_raises(self):
        # Documented behavior: parse_lines requires the file to exist —
        # callers must ensure it first (typically via a template plant).
        missing = Path(self.tmpdir.name) / "nope.txt"
        with self.assertRaises(FileNotFoundError):
            list(file_access.parse_lines(missing))

    def test_inline_comment_stripped(self):
        self.path.write_text("value  # trailing comment\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), ["value"])


class TestParseLinesBadInput(unittest.TestCase):
    """parse_lines is the entry point for the firewall whitelist parser too —
    a user-edited file with weird content shouldn't crash the launcher; bad
    entries pass through and fail downstream at DNS-resolution time (where
    they're recorded in the status file as `failed:`)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "whitelist.txt"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_garbage_line_passes_through(self):
        # Random text that looks nothing like a hostname / IP / CIDR — parser
        # doesn't validate, just yields. The downstream resolver will fail.
        self.path.write_text("this is just garbage text\nfoo.com\n")
        self.assertEqual(
            list(file_access.parse_lines(self.path)),
            ["this is just garbage text", "foo.com"],
        )

    def test_inline_comment_trims_to_useful_part(self):
        # `foo.com  # added 2025-01-15` → just `foo.com`
        self.path.write_text("foo.com  # added recently\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), ["foo.com"])

    def test_line_that_is_just_comment_skipped(self):
        self.path.write_text("# header\nfoo.com\n# trailer\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), ["foo.com"])

    def test_leading_and_trailing_whitespace_stripped(self):
        self.path.write_text("   foo.com   \n\t bar.com\t\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), ["foo.com", "bar.com"])

    def test_duplicate_entries_returned_as_given(self):
        # parse_lines doesn't dedupe — that's the firewall resolver's job (it dedupes
        # across BUILTIN + user lists into a single set).
        self.path.write_text("foo.com\nfoo.com\nbar.com\n")
        self.assertEqual(
            list(file_access.parse_lines(self.path)),
            ["foo.com", "foo.com", "bar.com"],
        )

    def test_malformed_cidr_passes_through(self):
        # Parsing layer doesn't know IPv4 CIDR — it just yields strings.
        # `10.0.0.0/99` is invalid but won't crash the launcher; iptables will
        # reject it downstream.
        self.path.write_text("10.0.0.0/99\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), ["10.0.0.0/99"])

    def test_unicode_passes_through(self):
        # Non-ASCII char in a hostname — exotic but legal in some contexts.
        # Parser doesn't enforce ASCII; downstream resolver handles or fails.
        self.path.write_text("héllo.example\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), ["héllo.example"])

    def test_whitespace_only_lines_skipped(self):
        # Pure-whitespace lines look like comments after strip — yield nothing.
        self.path.write_text("\n   \n\t\nfoo.com\n   \t\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), ["foo.com"])

    def test_completely_empty_file(self):
        self.path.write_text("")
        self.assertEqual(list(file_access.parse_lines(self.path)), [])

    def test_only_comments_file_returns_nothing(self):
        self.path.write_text("# header\n# more comments\n# yet more\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), [])


class TestIsFileRecent(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "file.txt"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_missing_file_is_not_recent(self):
        self.assertFalse(file_access.is_file_recent(self.path, 60))

    def test_just_written_file_is_recent(self):
        self.path.write_text("x")
        self.assertTrue(file_access.is_file_recent(self.path, 60))

    def test_stale_file_is_not_recent(self):
        import os
        import time
        self.path.write_text("x")
        # Backdate mtime to 2 hours ago
        old = time.time() - 7200
        os.utime(self.path, (old, old))
        self.assertFalse(file_access.is_file_recent(self.path, 60))


# ============================================================
# ensure_shared_oauth_files — idempotent touch of shared OAuth state files
# ============================================================


class TestEnsureSharedOauthFiles(unittest.TestCase):
    """Each test patches launch.file_access.ACCOUNT_FILE / CREDENTIALS_FILE to
    a temp path so the real launcher state under AGENTS_STATE isn't touched."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.account_path = Path(self.tmpdir.name) / "account.json"
        self.creds_path = Path(self.tmpdir.name) / "creds.json"
        self.patches = [
            patch.object(file_access, "ACCOUNT_FILE", self.account_path),
            patch.object(file_access, "CREDENTIALS_FILE", self.creds_path),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmpdir.cleanup()

    def test_creates_both_files_when_absent(self):
        self.assertFalse(self.account_path.exists())
        self.assertFalse(self.creds_path.exists())
        file_access.ensure_shared_oauth_files()
        self.assertTrue(self.account_path.is_file())
        self.assertTrue(self.creds_path.is_file())

    def test_initial_contents_are_empty_json_object(self):
        file_access.ensure_shared_oauth_files()
        self.assertEqual(self.account_path.read_text(), "{}")
        self.assertEqual(self.creds_path.read_text(), "{}")

    def test_existing_account_file_left_alone(self):
        # Pre-existing OAuth state must NOT be clobbered — Claude Code's
        # actual tokens live in these files.
        self.account_path.write_text('{"real": "data"}')
        file_access.ensure_shared_oauth_files()
        self.assertEqual(self.account_path.read_text(), '{"real": "data"}')

    def test_existing_creds_file_left_alone(self):
        self.creds_path.write_text('{"token": "abc"}')
        file_access.ensure_shared_oauth_files()
        self.assertEqual(self.creds_path.read_text(), '{"token": "abc"}')

    def test_creates_only_missing_file_when_other_exists(self):
        # Mixed state: one file exists, the other doesn't. Existing one stays
        # untouched; missing one gets created.
        self.account_path.write_text('{"existing": true}')
        file_access.ensure_shared_oauth_files()
        self.assertEqual(self.account_path.read_text(), '{"existing": true}')
        self.assertEqual(self.creds_path.read_text(), "{}")

    def test_idempotent_across_repeated_calls(self):
        file_access.ensure_shared_oauth_files()
        first_mtime = self.account_path.stat().st_mtime
        # Repeated call — must not rewrite the file (mtime stable).
        import time
        time.sleep(0.01)
        file_access.ensure_shared_oauth_files()
        self.assertEqual(self.account_path.stat().st_mtime, first_mtime)


# ============================================================
# enforce_ssh_dir_perms
# ============================================================


class TestEnforceSshDirPerms(unittest.TestCase):
    """SSH demands 700 on the dir + 600 on private keys; the launcher should
    apply that so users don't have to. *.pub and *_hosts files are non-secret
    and get the relaxed 644."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ssh = Path(self.tmpdir.name) / "ssh"
        self.ssh.mkdir(mode=0o755)   # deliberately wrong perms

    def tearDown(self):
        self.tmpdir.cleanup()

    def _mode(self, p: Path) -> int:
        return p.stat().st_mode & 0o777

    def _make_file(self, name: str, initial_mode: int = 0o644) -> Path:
        p = self.ssh / name
        p.write_text("content")
        p.chmod(initial_mode)
        return p

    def test_dir_perms_set_to_700(self):
        file_access.enforce_ssh_dir_perms(self.ssh)
        self.assertEqual(self._mode(self.ssh), 0o700)

    def test_private_key_chmod_600(self):
        key = self._make_file("id_ed25519", 0o644)
        file_access.enforce_ssh_dir_perms(self.ssh)
        self.assertEqual(self._mode(key), 0o600)

    def test_ssh_config_chmod_600(self):
        cfg = self._make_file("config", 0o644)
        file_access.enforce_ssh_dir_perms(self.ssh)
        self.assertEqual(self._mode(cfg), 0o600)

    def test_pub_chmod_644(self):
        pub = self._make_file("id_ed25519.pub", 0o600)
        file_access.enforce_ssh_dir_perms(self.ssh)
        self.assertEqual(self._mode(pub), 0o644)

    def test_known_hosts_chmod_644(self):
        kh = self._make_file("known_hosts", 0o600)
        file_access.enforce_ssh_dir_perms(self.ssh)
        self.assertEqual(self._mode(kh), 0o644)

    def test_known_hosts2_chmod_644(self):
        # "known_hosts2" doesn't literally end with "_hosts" — the endswith
        # check needs the ("_hosts", "_hosts2") pair or ssh's secondary
        # known-hosts file silently lands in the 600 bucket.
        kh2 = self._make_file("known_hosts2", 0o600)
        file_access.enforce_ssh_dir_perms(self.ssh)
        self.assertEqual(self._mode(kh2), 0o644)

    def test_non_directory_silently_skipped(self):
        # Missing or non-dir input → no-op (no exception).
        file_access.enforce_ssh_dir_perms(Path("/does/not/exist"))
        file_access.enforce_ssh_dir_perms(self.ssh / "id_ed25519")   # a file

    def test_subdir_entries_skipped(self):
        # Only top-level files get chmod'd; subdirs aren't touched.
        subdir = self.ssh / "sub"
        subdir.mkdir(mode=0o755)
        file_access.enforce_ssh_dir_perms(self.ssh)
        self.assertEqual(self._mode(subdir), 0o755)   # unchanged


# ============================================================
# last_prompt_in_state — most-recent human question in a transcript
# ============================================================


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
        prompt, when = file_access.last_prompt_in_state(self.state)
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
        prompt, _ = file_access.last_prompt_in_state(self.state)
        self.assertEqual(prompt, "real question")

    def test_text_block_list_content_joined(self):
        self._write("s.jsonl", [self._user(
            [{"type": "text", "text": "block one"}, {"type": "text", "text": "block two"}],
            "2026-01-01T00:00:00.000Z")])
        prompt, _ = file_access.last_prompt_in_state(self.state)
        self.assertEqual(prompt, "block one block two")

    def test_malformed_and_promptless_lines_skipped(self):
        (self.tx / "s.jsonl").write_text(
            'not json\n{"type": "user"}\n'
            + json.dumps(self._user("survivor", "2026-01-01T00:00:00.000Z")) + "\n")
        prompt, _ = file_access.last_prompt_in_state(self.state)
        self.assertEqual(prompt, "survivor")

    def test_scans_across_multiple_transcript_files(self):
        self._write("a.jsonl", [self._user("older", "2026-01-01T00:00:00.000Z")])
        self._write("b.jsonl", [self._user("newer", "2026-03-01T00:00:00.000Z")])
        prompt, _ = file_access.last_prompt_in_state(self.state)
        self.assertEqual(prompt, "newer")

    def test_no_transcript_dir_returns_none(self):
        bare = Path(self.tmpdir.name) / "bare"
        bare.mkdir()
        self.assertIsNone(file_access.last_prompt_in_state(bare))

    def test_no_human_prompt_returns_none(self):
        self._write("s.jsonl", [{"type": "assistant",
                                  "message": {"role": "assistant", "content": "hi"},
                                  "timestamp": "2026-01-01T00:00:00.000Z"}])
        self.assertIsNone(file_access.last_prompt_in_state(self.state))


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
        found = file_access.last_answer_in_state(self.state)
        self.assertEqual(found[0], "the answer")

    def test_none_when_only_a_question(self):
        self._write([{"type": "user", "message": {"role": "user", "content": "q"},
                      "timestamp": "2026-01-01T00:00:00.000Z"}])
        self.assertIsNone(file_access.last_answer_in_state(self.state))


class TestAppendText(unittest.TestCase):
    """append_text backs the {cowork} conversation log: it must extend a file
    rather than replace it, and must not need the caller to pre-create dirs."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_creates_file_and_parents(self):
        target = self.dir / "deep" / "nested" / "log.md"
        file_access.append_text(target, "first\n")
        self.assertEqual(target.read_text(), "first\n")

    def test_appends_rather_than_replacing(self):
        target = self.dir / "log.md"
        file_access.append_text(target, "one\n")
        file_access.append_text(target, "two\n")
        self.assertEqual(target.read_text(), "one\ntwo\n")

    def test_leaves_no_temp_files_behind(self):
        # Unlike write_text, there is no temp-and-replace step to leak.
        target = self.dir / "log.md"
        file_access.append_text(target, "x")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["log.md"])


class TestIterFiles(unittest.TestCase):
    """iter_files is the wrapper spool readers use; sorted, files only."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_missing_parent_yields_nothing(self):
        self.assertEqual(list(file_access.iter_files(self.dir / "absent")), [])

    def test_yields_files_sorted_and_skips_dirs(self):
        (self.dir / "b.json").write_text("{}")
        (self.dir / "a.json").write_text("{}")
        (self.dir / "subdir").mkdir()
        self.assertEqual([p.name for p in file_access.iter_files(self.dir)],
                         ["a.json", "b.json"])

    def test_suffix_filter(self):
        (self.dir / "keep.json").write_text("{}")
        (self.dir / "skip.txt").write_text("x")
        self.assertEqual([p.name for p in file_access.iter_files(self.dir, suffix=".json")],
                         ["keep.json"])


class TestIterTreeFiles(unittest.TestCase):
    """iter_tree_files is the recursive walk {cowork}'s sync copies trees with —
    relative paths, so a caller can rebase onto a second tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _plant(self, relative: str) -> None:
        path = self.dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")

    def test_missing_parent_yields_nothing(self):
        self.assertEqual(list(file_access.iter_tree_files(self.dir / "absent")), [])

    def test_empty_dir_yields_nothing(self):
        self.assertEqual(list(file_access.iter_tree_files(self.dir)), [])

    def test_paths_are_relative_to_the_parent(self):
        self._plant("pkg/deep/mod.py")
        self.assertEqual(list(file_access.iter_tree_files(self.dir)),
                         [Path("pkg/deep/mod.py")])

    def test_yields_nested_and_top_level_together_sorted(self):
        self._plant("b.py")
        self._plant("a/inner.py")
        self.assertEqual(list(file_access.iter_tree_files(self.dir)),
                         [Path("a/inner.py"), Path("b.py")])

    def test_directories_are_not_yielded(self):
        (self.dir / "empty_subdir").mkdir()
        self.assertEqual(list(file_access.iter_tree_files(self.dir)), [])

    def test_dotfiles_are_included(self):
        # rglob("*") skips nothing by name; a dotfile is still work product.
        self._plant(".hidden_config")
        self.assertEqual(list(file_access.iter_tree_files(self.dir)),
                         [Path(".hidden_config")])


class TestFilesDiffer(unittest.TestCase):
    """files_differ answers the question copy_file's overwrite_if_changed asks,
    without performing the copy."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.left = self.dir / "left"
        self.right = self.dir / "right"

    def test_identical_content_does_not_differ(self):
        self.left.write_text("same")
        self.right.write_text("same")
        self.assertFalse(file_access.files_differ(self.left, self.right))

    def test_different_content_differs(self):
        self.left.write_text("one")
        self.right.write_text("two")
        self.assertTrue(file_access.files_differ(self.left, self.right))

    def test_a_missing_counterpart_differs(self):
        self.left.write_text("only here")
        self.assertTrue(file_access.files_differ(self.left, self.right))

    def test_two_missing_paths_differ_rather_than_raise(self):
        self.assertTrue(file_access.files_differ(self.left, self.right))

    def test_a_directory_differs_rather_than_raising(self):
        self.left.write_text("x")
        self.right.mkdir()
        self.assertTrue(file_access.files_differ(self.left, self.right))

    def test_compares_content_not_mtime(self):
        # A file rewritten with the same bytes must read as unchanged, or every
        # sync round would report the whole tree as changed.
        self.left.write_text("same")
        self.right.write_text("same")
        os.utime(self.right, (0, 0))
        self.assertFalse(file_access.files_differ(self.left, self.right))

    def test_two_empty_files_do_not_differ(self):
        self.left.write_text("")
        self.right.write_text("")
        self.assertFalse(file_access.files_differ(self.left, self.right))


if __name__ == "__main__":
    unittest.main()
