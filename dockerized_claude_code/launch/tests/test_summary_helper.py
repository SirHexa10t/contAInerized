"""Tests for settings/_summary.py — the in-container helper behind
/write-summary's `summary_diff` / `summary_save_manifest` bash wrappers.

The module isn't part of the launch package (it's bind-mounted into
containers), so it's loaded here by file path via importlib. Its module
globals ROOT / SUMMARY are repointed at a tmp tree per test — the fallback
(non-git) walk is what these fixtures exercise, plus the manifest
parse/save/classify core."""

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_SUMMARY_PY = Path(__file__).resolve().parent.parent.parent / "settings" / "_summary.py"


def _load_summary_module():
    spec = importlib.util.spec_from_file_location("summary_helper", _SUMMARY_PY)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


MANIFEST_TEMPLATE = """## Project Summary

Prose about the project.

### File Manifest

<!-- manifest:begin -->
{body}<!-- manifest:end -->
"""


class _SummaryBase(unittest.TestCase):
    def setUp(self):
        self.mod = _load_summary_module()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.summary = self.root / ".claude_summary"
        self._patches = [
            patch.object(self.mod, "ROOT", self.root),
            patch.object(self.mod, "SUMMARY", self.summary),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmpdir.cleanup()

    def _write_summary(self, manifest_body=""):
        self.summary.write_text(MANIFEST_TEMPLATE.format(body=manifest_body))


class TestListFiles(_SummaryBase):
    """list_files maps project-relevant files to int mtimes, filtering noise.
    The tmp root isn't a git repo, so the fallback dir-blocklist walk runs
    (it prints a one-line stderr notice — silenced here)."""

    def _list(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return self.mod.list_files()

    def test_regular_files_listed_with_int_mtimes(self):
        (self.root / "main.py").write_text("x")
        out = self._list()
        self.assertEqual(set(out), {"main.py"})
        self.assertIsInstance(out["main.py"], int)

    def test_summary_file_itself_excluded(self):
        self._write_summary()
        (self.root / "main.py").write_text("x")
        self.assertEqual(set(self._list()), {"main.py"})

    def test_lockfiles_and_os_noise_excluded(self):
        for name in ("package-lock.json", "uv.lock", ".DS_Store", "kept.py"):
            (self.root / name).write_text("x")
        self.assertEqual(set(self._list()), {"kept.py"})

    def test_ide_dirs_excluded_anywhere_in_path(self):
        (self.root / ".idea").mkdir()
        (self.root / ".idea" / "misc.xml").write_text("x")
        (self.root / "sub" / ".vscode").mkdir(parents=True)
        (self.root / "sub" / ".vscode" / "launch.json").write_text("x")
        (self.root / "sub" / "real.py").write_text("x")
        self.assertEqual(set(self._list()), {"sub/real.py"})

    def test_binary_assets_excluded_but_svg_kept(self):
        for name in ("logo.png", "font.woff2", "icon.svg"):
            (self.root / name).write_text("x")
        self.assertEqual(set(self._list()), {"icon.svg"})

    def test_dep_cache_dirs_excluded_by_fallback_walk(self):
        (self.root / "node_modules" / "pkg").mkdir(parents=True)
        (self.root / "node_modules" / "pkg" / "index.js").write_text("x")
        (self.root / "app.js").write_text("x")
        self.assertEqual(set(self._list()), {"app.js"})


class TestManifestParsing(_SummaryBase):
    def test_parse_manifest_reads_epoch_and_path(self):
        self._write_summary("1700000000 main.py\n1700000001 sub/app.js\n")
        self.assertEqual(self.mod.parse_manifest(),
                         {"main.py": 1700000000, "sub/app.js": 1700000001})

    def test_missing_summary_yields_empty(self):
        self.assertEqual(self.mod.parse_manifest(), {})

    def test_summary_without_markers_yields_empty(self):
        self.summary.write_text("## Project Summary\n\nno manifest here\n")
        self.assertEqual(self.mod.parse_manifest(), {})

    def test_marker_mention_in_prose_not_latched(self):
        # The begin/end tags must be on their own lines AFTER the heading —
        # inline mentions in prose (e.g. docs about the format) don't count.
        self.summary.write_text(
            "Text mentioning <!-- manifest:begin --> inline.\n\n"
            "### File Manifest\n\n<!-- manifest:begin -->\n1 a.py\n<!-- manifest:end -->\n"
        )
        self.assertEqual(self.mod.parse_manifest(), {"a.py": 1})


class TestClassify(_SummaryBase):
    def test_new_changed_deleted_partition(self):
        self._write_summary("111 old_gone.py\n222 changed.py\n333 same.py\n")
        listing = {"changed.py": 999, "same.py": 333, "brand_new.py": 444}
        with patch.object(self.mod, "list_files", return_value=listing):
            kinds = {path: kind for kind, path in self.mod._classify()}
        self.assertEqual(kinds, {
            "old_gone.py": "DELETED",
            "changed.py": "CHANGED",
            "brand_new.py": "NEW",
        })


class TestSave(_SummaryBase):
    def test_save_refuses_without_markers(self):
        self.summary.write_text("## Project Summary\n\nno markers\n")
        with self.assertRaises(SystemExit):
            self.mod.cmd_save()

    def test_save_refuses_when_summary_missing(self):
        with self.assertRaises(SystemExit):
            self.mod.cmd_save()

    def test_save_rewrites_block_and_roundtrips(self):
        self._write_summary("1 stale.py\n")
        (self.root / "real.py").write_text("x")
        with contextlib.redirect_stderr(io.StringIO()), patch("builtins.print"):
            self.mod.cmd_save()
        self.assertEqual(set(self.mod.parse_manifest()), {"real.py"})
        # Prose outside the markers survives the rewrite.
        self.assertIn("Prose about the project.", self.summary.read_text())


if __name__ == "__main__":
    unittest.main()
