"""Helpers for the /write-summary slash command.

Two operations, dispatched via the first CLI arg:
  diff   compare current /workspace contents against the manifest in .claude_summary;
         print one line per NEW / CHANGED / DELETED file.
  save   replace the manifest block in .claude_summary with the current listing.
         Refuses to run unless a '### File Manifest' heading is followed by
         begin/end markers, each on its own line.

Invoked by the bash wrappers `summary_diff` and `summary_save_manifest` defined in
settings/bashrc.sh. `.claude_summary` is intentionally excluded from the listing —
since `save` writes to it, tracking it would always show a CHANGED loop.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path("/workspace")
SUMMARY = ROOT / ".claude_summary"

EXCLUDED_NAMES = {".claude_summary"}

# Used only when ROOT isn't a git repo (the primary path delegates to
# `git ls-files --cached --others --exclude-standard`, which handles
# .gitignore — including nested ones — natively). Generous list of dirs
# that virtually no project tracks: build outputs, dep caches, tool caches.
# IDE-state dirs live in NOISE_DIR_NAMES below — they need to filter even
# when git DOES track them (accidentally-committed personal state).
FALLBACK_EXCLUDED_DIR_NAMES = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".next", "dist", "build", "target", ".cache",
    ".venv", "venv",
}

# Directory-name segments that should be filtered from the manifest even
# when git tracks them — these are editor / IDE per-user state that
# someone accidentally committed instead of .gitignoring. Matches against
# any path segment, so `frontend/android/.idea/foo.xml` is caught by `.idea`.
NOISE_DIR_NAMES = {
    ".idea",        # JetBrains family — IntelliJ, PyCharm, WebStorm, GoLand, RustRover, RubyMine, PhpStorm, …
    ".vscode",      # VS Code (sometimes intentional — debug configs / tasks; remove from this set if your team shares them)
    ".vs",          # Visual Studio (Windows)
    ".cursor",      # Cursor (AI IDE)
    ".windsurf",    # Windsurf (AI IDE)
    ".fleet",       # JetBrains Fleet
    ".zed",         # Zed
    ".atom",        # Atom (legacy, still around in old repos)
    ".history",     # VS Code's "Local History" extension
}

# Exact basenames the scanner always skips — auto-generated dependency
# snapshots, build wrappers, and OS metadata files that DO get committed
# (for reproducibility, or accidentally) but say nothing about the code.
NOISE_FILENAMES = {
    # JS / TS lockfiles
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    # Rust / Python / PHP / Go
    "Cargo.lock", "Pipfile.lock", "poetry.lock", "uv.lock", "composer.lock", "go.sum",
    # Gradle wrapper (Capacitor / Android / pure-Gradle projects)
    "gradlew", "gradlew.bat", "gradle-wrapper.properties",
    # OS metadata
    ".DS_Store", "Thumbs.db", "desktop.ini",
}

# File extensions the summary scanner skips regardless of whether the file
# is tracked. Catches binary assets that ARE legitimately in the repo
# (launcher icons, fonts, sample audio, compiled blobs, etc.) but contribute
# nothing to an AI session's understanding of the project's CODE. Applied
# after the git/fallback listing — so a file like `frontend/android/.../
# ic_launcher_foreground.png` is dropped here even though git tracks it.
#
# SVG is deliberately omitted: SVGs are often icons-as-code (Heroicons,
# inline illustrations) that DO carry meaningful content. The same logic
# applies to .json / .xml / .toml etc., which are config / data and stay.
BINARY_ASSET_EXTENSIONS = {
    # Raster images
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".tiff", ".tif",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Audio / video
    ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac",
    ".mp4", ".avi", ".mov", ".webm", ".mkv",
    # Archives
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar",
    # Compiled / build outputs
    ".o", ".a", ".so", ".dylib", ".dll", ".exe", ".class", ".jar", ".wasm", ".pyc",
    # Databases / misc binary documents
    ".db", ".sqlite", ".sqlite3", ".pdf",
}

BEGIN_TAG = "<!-- manifest:begin -->"
END_TAG   = "<!-- manifest:end -->"


def _git_tracked_and_untracked():
    """Path strings (relative to ROOT) for every file git considers project-relevant
    — tracked OR newly-untracked-but-not-ignored. Returns None when ROOT isn't a
    git repo, when git isn't installed, or when the command fails for any reason
    (caller falls back to the directory-name blocklist below).

    Flags:
      --cached            — tracked files
      --others            — untracked files (newly created, not `git add`ed yet)
      --exclude-standard  — apply .gitignore / .git/info/exclude / global excludes
                            to the --others set (handles nested .gitignore natively)
      -z                  — NUL-separated output so paths with unusual chars (spaces,
                            unicode, newlines) come through verbatim instead of
                            C-style quoted.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return [p for p in result.stdout.decode("utf-8", errors="replace").split("\0") if p]


def _walk_with_fallback_exclusions():
    """Recursive walk with FALLBACK_EXCLUDED_DIR_NAMES applied — used only when
    ROOT isn't a git repo. Prints a one-line stderr notice so the user knows
    which strategy is in play (silently using a less-precise list would surprise
    debugging later)."""
    print(
        "  (no git repo at /workspace — using built-in dir-name blocklist; "
        f"adopting any .gitignore would require `git init`.)",
        file=sys.stderr,
    )
    paths = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in FALLBACK_EXCLUDED_DIR_NAMES for part in rel.parts):
            continue
        paths.append(str(rel))
    return paths


def list_files():
    """Map every project-relevant regular file under ROOT to its int mtime.

    Primary path: ask git what's in the project (tracked + untracked-but-not-
    ignored). This adopts ALL .gitignore files automatically — including nested
    ones, and including the standard build-output / dep-cache exclusions that
    .gitignore captures in repos like NextJS / Cargo / Maven projects.

    Fallback: if ROOT isn't a git repo, walk with a hardcoded common-bloat
    blocklist (FALLBACK_EXCLUDED_DIR_NAMES)."""
    rel_paths = _git_tracked_and_untracked()
    if rel_paths is None:
        rel_paths = _walk_with_fallback_exclusions()
    out = {}
    for rel in rel_paths:
        if rel in EXCLUDED_NAMES:
            continue
        parts = Path(rel).parts
        if any(p in NOISE_DIR_NAMES for p in parts):
            continue
        if parts[-1] in NOISE_FILENAMES:
            continue
        if Path(rel).suffix.lower() in BINARY_ASSET_EXTENSIONS:
            continue
        full = ROOT / rel
        try:
            if not full.is_file():
                continue
            out[rel] = int(full.stat().st_mtime)
        except OSError:
            continue   # racing fs (file removed between listing and stat) — drop silently
    return out


def find_manifest_block():
    """Read SUMMARY and locate the manifest block. Returns (text, begin, end) — the
    full file text plus the indices spanning the content between the markers.
    Returns None if SUMMARY doesn't exist, the heading is missing, or either marker
    is missing.

    The `### File Manifest` heading must appear on its own line, and both markers
    must appear on their own lines *after* that heading. This anchoring keeps the
    parser from latching onto incidental mentions of the marker strings elsewhere
    in the document (e.g., in prose explaining the manifest format)."""
    if not SUMMARY.exists():
        return None
    text = SUMMARY.read_text()
    header = re.search(r"^### File Manifest$", text, re.MULTILINE)
    if header is None:
        return None
    begin = re.search(r"^" + re.escape(BEGIN_TAG) + r"$", text[header.end():], re.MULTILINE)
    if begin is None:
        return None
    begin_pos = header.end() + begin.end()
    end = re.search(r"^" + re.escape(END_TAG) + r"$", text[begin_pos:], re.MULTILINE)
    if end is None:
        return None
    end_pos = begin_pos + end.start()
    return text, begin_pos, end_pos


def parse_manifest():
    """Read SUMMARY's manifest block into {path: epoch_int}; {} if missing or no markers."""
    found = find_manifest_block()
    if found is None:
        return {}
    text, begin, end = found
    out = {}
    for line in text[begin:end].splitlines():
        m = re.match(r"^\s*(\d+)\s+(.+?)\s*$", line)
        if m:
            out[m.group(2)] = int(m.group(1))
    return out


def _classify():
    """Yield (kind, path) for each file differing between SUMMARY's manifest and the
    current /workspace listing. kind ∈ {'NEW', 'CHANGED', 'DELETED'}."""
    prev = parse_manifest()
    curr = list_files()
    for path in sorted(set(prev) | set(curr)):
        p, c = prev.get(path), curr.get(path)
        if   p is None: yield "NEW", path
        elif c is None: yield "DELETED", path
        elif p != c:    yield "CHANGED", path


def cmd_diff():
    for kind, path in _classify():
        print(f"{kind:<10}{path}")


def cmd_files_to_check():
    """Print one path per line for files needing the AI's attention (NEW + CHANGED)."""
    for kind, path in _classify():
        if kind != "DELETED":
            print(path)


def cmd_save():
    found = find_manifest_block()
    if found is None:
        if not SUMMARY.exists():
            raise SystemExit(f"{SUMMARY} does not exist; create it first.")
        raise SystemExit(
            f"Refusing to save: missing manifest block in {SUMMARY}. "
            f"Expected a '### File Manifest' heading followed by '{BEGIN_TAG}' "
            f"and '{END_TAG}' — each on its own line."
        )
    text, begin, end = found
    items = sorted(list_files().items(), key=lambda kv: (-kv[1], kv[0]))
    new_block = "\n" + "\n".join(f"{e} {p}" for p, e in items) + "\n"
    SUMMARY.write_text(text[:begin] + new_block + text[end:])
    print(f"Wrote {len(items)} entries between '{BEGIN_TAG}' and '{END_TAG}' in {SUMMARY}.")


COMMANDS = {"diff": cmd_diff, "files": cmd_files_to_check, "save": cmd_save}

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    cmd = COMMANDS.get(mode)
    if cmd is None:
        raise SystemExit(f"Unknown mode: {mode!r}; expected one of {list(COMMANDS)}.")
    cmd()
