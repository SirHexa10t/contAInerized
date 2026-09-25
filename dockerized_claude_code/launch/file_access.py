"""The launcher's file-access layer — every disk-touching primitive the
launcher uses, with caching where it earns its keep. Other modules
delegate to this one for reads, writes, scans, and stat calls; they don't
do direct file I/O themselves (except for narrowly-scoped operations like
state-dir lifecycle in agents_crud, where the file ops are inseparable
from the domain logic).

Grouped by section in this file:
  - Agent file lookup (agent_md_index [cached]) — the name → md-path index
    (stem = agent name; an agent's axes live in `<name>.lego`, parsed by
    the tags package).
  - (The per-instance axis store — instances.toml — lives in tags.store, and
    the session-transcript readers in `launch/transcripts.py`; both are built
    on this module's read/write primitives rather than opening files
    themselves.)
  - Optional credentials (present_optional_cred_services [cached],
    optional_cred_tokens)
  - User firewall whitelist (user_firewall_whitelist_lines [cached, self-plants
    from FIREWALL_WHITELIST_TEMPLATE on first read]). The first-launch plant
    of firewall_whitelist.txt + the always-on plant of optional_creds_readme.txt
    live in user_additions.plant_user_extras so they happen at the right point
    in run.py's launch flow.

Caching strategy:
  - `agent_md_index` / `present_optional_cred_services` /
    `user_firewall_whitelist_lines` are LRU-cached for the launcher process
    lifetime (each `python3 run.py` invocation is a fresh process).

No build/composition logic (that's tag_handlers), no identity dataclasses
(that's tags/identity.py), no arg formatting for docker (that's docker_config).
Imports paths + utils only — kept leaf-shaped so the tags package can depend on
it without pulling in heavier modules. tag_handlers, agents_crud,
audit, the tags package, user_additions, and run.py all import from here.
"""

import glob
import json
import os
import re
import shutil
import time
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any, IO, Literal

from .ai.adapter import Adapter, AuthFile
from .paths import (
    credentials_dir,
    AGENTS_DIR,
    FIREWALL_WHITELIST_FILE,
    FIREWALL_WHITELIST_TEMPLATE, OPTIONAL_CREDS_MOUNTS,
    OPTIONAL_CREDS_TOKEN_ENV_VARS, clusters_dir, instances_dir,
    optional_creds_service_path, optional_creds_token_path,
    quickie_communal_workspace, quickie_dir,
)
from .utils import shell_returncode

# ============================================================
# Filesystem primitives — every disk-touching syscall flows through this file
# ============================================================
# Other modules call these wrappers instead of `.mkdir()` / `.unlink()` /
# `.read_text()` / `.write_text()` / `.exists()` / `.is_dir()` / etc. directly,
# so the "who can touch the filesystem" rule is locally enforceable. The
# sudo-escalation policy used for Docker-bind-mount leftovers lives in
# `force_remove` further down — it wraps `remove_path` with the fallback
# logic so callers don't have to thread sudo handling themselves.
#
# Inside this module, Path methods are called directly (we ARE the file-access
# layer); the wrappers exist for external callers.

# --- Mutations ---

def ensure_dir(path: Path) -> None:
    """`mkdir -p` equivalent: create the directory and any missing parents
    if absent; no-op if already present."""
    path.mkdir(parents=True, exist_ok=True)


def remove_path(path: Path) -> None:
    """Remove `path` — file, symlink, or directory. No-op if absent. Dispatches
    on what's at `path` so callers don't need a parallel ladder of is_dir /
    is_symlink checks. For paths that may be root-owned (Docker bind-mount
    leftovers), use `force_remove` instead — it wraps this one with a
    sudo-escalation policy."""
    if not path.exists() and not path.is_symlink():   # broken symlinks: `.exists()` returns False, so we also check `.is_symlink()` to catch them
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def force_remove(path: Path, *, name: str | None = None) -> bool:
    """Best-effort removal of `path` (file, symlink, or directory). Logs what's
    being removed, falls back to `sudo rm -rf` for root-owned artifacts (Docker
    bind-mount leftovers), and follows up with `sudo -k` so cached credentials
    don't linger past this single operation.

    `name` is an optional human-friendly identifier — when provided, the path
    is treated as user-initiated removal (no "stale" descriptor in the log).
    This layer only prints what happened; interactive gating on failure is
    the caller's job (agents_crud.delete_instance holds the picker open with
    prompt_keypress so the user can read the failure) — file access shouldn't
    block on user input.

    Returns True on success (including "already absent"); False if even sudo
    couldn't remove the path."""
    if not path.exists() and not path.is_symlink():
        return True

    kind = "symlink" if path.is_symlink() else ("dir" if path.is_dir() else "file")
    descriptor = "" if name else "stale "
    print(f"  Removing {descriptor}{kind}: {path}")

    try:
        remove_path(path)
        return True
    except FileNotFoundError:
        return True   # raced with another removal; consider it done
    except (PermissionError, OSError):
        pass          # fall through to sudo escalation

    print("\n  Permission denied — root-owned (Docker bind-mount artifact). Elevating with sudo...")
    ret = shell_returncode("sudo", "rm", "-rf", str(path))
    shell_returncode("sudo", "-k")   # clear cached credentials
    if ret == 0:
        return True

    print(f"\n  sudo cleanup failed (exit {ret}).")
    print(f"  Manual cleanup:  sudo rm -rf '{path}'")
    return False


def move_path(src: Path, dst: Path) -> None:
    """Rename `src` to `dst`. Works for both files and directories.
    Used by agents_crud.modify_instance to relocate state dirs after a
    session-suffix rename."""
    src.rename(dst)


def write_text(path: Path, content: str) -> None:
    """Write `content` to `path` as text (overwriting if present) — atomically:
    content lands in a same-directory temp file first, then `os.replace`s over
    `path`. A Ctrl+C / crash mid-write can therefore never truncate existing
    state, and concurrent readers — including the in-container view of a
    bind-mounted state dir — only ever observe the old or the new content,
    never a partial file.
    (The temp file lives next to `path` so the rename stays on one filesystem;
    the pid suffix keeps two launcher processes from colliding.)
    Auto-creates the parent directory tree if missing, so callers don't need a
    separate ensure_dir call. Use ensure_dir directly only for non-write
    reasons (creating a directory that's a bind-mount target / mount point,
    etc.)."""
    ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(content)
        os.replace(tmp, path)
    except BaseException:            # incl. KeyboardInterrupt — the exact torn-write scenario this guards
        tmp.unlink(missing_ok=True)
        raise


def append_text(path: Path, content: str) -> None:
    """Append `content` to `path`, creating the file and its parents if missing.

    Deliberately NOT the write_text temp-and-replace dance: an append-only log
    must not be rewritten wholesale, and a single small append in "a" mode is
    written at the current end-of-file, so a concurrent reader (`tail -f`, or an
    agent reading through a bind-mount) sees whole lines rather than a truncated
    file. Used for the {cowork} per-group conversation log."""
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(content)


def open_binary_append(path: Path) -> IO[bytes]:
    """Open `path` for binary append, creating its parents — for handing a real
    OS-level file handle to subprocess (a Popen redirect needs a file object,
    which the content-shaped helpers here rightly never expose). The caller
    owns closing it; a Popen child keeps its own duplicate of the descriptor,
    so closing the parent's copy right after spawning is the normal pattern.
    Used for the {cowork} hub's detached-stdout log."""
    ensure_dir(path.parent)
    return path.open("ab")


def copy_file(src: Path, dest: Path, overwrite_if_changed: bool = False) -> None:
    """Copy `src` to `dest` (content + permissions + metadata, via shutil.copy2).
    Default behaviour: no-op when `dest` already exists — preserves user edits.
    With `overwrite_if_changed=True`, reads both files and rewrites only when
    they differ — for launcher-owned templates the user shouldn't be editing
    (e.g. optional_creds_readme.txt: regenerated when the template moves on,
    but no needless rewrite + mtime bump when nothing changed). Auto-creates
    dest's parent directory tree, matching write_text's convention."""
    if dest.exists() and (not overwrite_if_changed or src.read_bytes() == dest.read_bytes()):
        return
    ensure_dir(dest.parent)
    shutil.copy2(src, dest)


def create_exclusive(path: Path, content: str) -> bool:
    """Create `path` holding `content`, but ONLY if it does not already exist.
    True when we created it, False when someone else got there first.

    The opposite of write_text's replace-what's-there: this is an atomic *claim*.
    `O_EXCL` makes create-or-fail a single syscall, so two processes racing for the
    same lock cannot both believe they won — which a `path_exists()` check followed
    by a write cannot promise, however small the window looks.

    Used for the {cowork} hub's singleton pidfile, where two hubs draining the same
    outboxes would each consume half the captures."""
    ensure_dir(path.parent)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as handle:
        handle.write(content)
    return True


def files_differ(left: Path, right: Path) -> bool:
    """True when the two paths do not hold identical bytes.

    A missing (or unreadable) side counts as differing, because absence is
    exactly the difference callers care about — a file one tree has and the other
    doesn't. Compares content rather than size/mtime, so a file rewritten with
    the same bytes reads as unchanged.

    Sits next to copy_file because it is the predicate copy_file's
    `overwrite_if_changed` describes; kept separate so a caller can ask the
    question without performing the copy ({cowork}'s sync reports which of a
    coworker's submitted files actually differ from the manager's copy)."""
    try:
        return left.read_bytes() != right.read_bytes()
    except OSError:
        return True


def enforce_ssh_dir_perms(ssh_dir: Path) -> None:
    """Apply SSH's strict permission requirements to a directory: 700 on the
    dir itself, 600 on every regular file inside EXCEPT `*.pub` (public keys)
    and `*_hosts` / `*_hosts2` (`known_hosts`, `known_hosts2`) which get 644. ssh refuses
    to read private keys whose perms aren't 600 — and refuses to load any
    config from a dir whose perms aren't 700 — so this is what the user
    would otherwise have to set by hand. No-op if `ssh_dir` doesn't exist
    or isn't a directory. Top-level only (subdirs aren't traversed). The
    `optional_creds/ssh/` dir is expected to hold *copies* (or fresh keys)
    rather than symlinks to the user's everyday ~/.ssh; chmodding here
    isn't expected to mutate their host setup."""
    if not ssh_dir.is_dir():
        return
    ssh_dir.chmod(0o700)
    for entry in ssh_dir.iterdir():
        if not entry.is_file():
            continue
        relaxed = entry.suffix == ".pub" or entry.name.endswith(("_hosts", "_hosts2"))
        entry.chmod(0o644 if relaxed else 0o600)


# --- Existence + kind queries ---
# All accept Path or str (internally `Path(path)`-coerced) so callers don't
# have to think about which they're holding.

def path_exists(path: Path | str) -> bool:
    """True iff something exists at `path` (file, dir, or symlink-to-anything)."""
    return Path(path).exists()


def is_dir(path: Path | str) -> bool:
    """True iff `path` exists and is a directory."""
    return Path(path).is_dir()


def is_file(path: Path | str) -> bool:
    """True iff `path` exists and is a regular file (not a directory or symlink-to-dir)."""
    return Path(path).is_file()


def is_symlink(path: Path | str) -> bool:
    """True iff `path` is a symlink (regardless of what — or nothing — it points to)."""
    return Path(path).is_symlink()


# --- Listing + searching ---

def iter_conversation_dirs() -> Iterator[Path]:
    """Every state dir on this host that can hold a conversation, in no
    promised order: one per instance, one per CLUSTER MEMBER, one per quickie
    thread. `--find` searches exactly this set.

    Three roots because the launcher parks three kinds of conversation in
    three places, and a search that knew only about `instances/` would answer
    "never discussed" about work a cluster did. Nothing here parses
    `cluster.toml` or the instance store: a conversation dir is a DIRECTORY
    FACT, and keeping it one keeps the disk layer free of tag and TOML
    knowledge (the caller labels a dir by which root it came from).

    Not cached, unlike `agent_md_index`: that index is read per row per
    render, while this is walked once per search, and a cache would hide an
    instance created since the launcher started."""
    yield from iter_subdirs(instances_dir())
    for cluster in iter_subdirs(clusters_dir()):
        yield from iter_subdirs(cluster / "members")
    communal = quickie_communal_workspace().name
    yield from (thread for thread in iter_subdirs(quickie_dir())
                if thread.name != communal)     # the shared drop-box is not a thread


def iter_subdirs(parent: Path) -> Iterator[Path]:
    """Yield immediate subdirectories of `parent` (filesystem order). No-op
    on a missing parent so callers don't need a `path_exists` guard before
    iterating — matches the "missing dir == empty listing" intent every
    caller wants. Callers wanting all entries (not just dirs) should call
    parent.iterdir() — but no caller currently does, so the filter is folded
    in here."""
    if not parent.exists():
        return
    for entry in parent.iterdir():
        if entry.is_dir():
            yield entry


def iter_files(parent: Path, suffix: str | None = None) -> Iterator[Path]:
    """Yield immediate *files* of `parent`, name-sorted, optionally filtered to
    one suffix. Sister to iter_subdirs (which yields dirs, unsorted): sorted here
    because every caller so far reads a spool directory whose filenames encode
    their order. No-op on a missing parent, same "missing dir == empty listing"
    intent."""
    if not parent.exists():
        return
    for entry in sorted(parent.iterdir(), key=lambda p: p.name):
        if entry.is_file() and (suffix is None or entry.suffix == suffix):
            yield entry


def iter_tree_files(parent: Path) -> Iterator[Path]:
    """Yield every regular file anywhere under `parent`, as paths RELATIVE to it,
    depth-first and sorted.

    Relative rather than absolute because both callers of a recursive walk want
    to rebase onto a second tree (`destination / relative`) or report a name a
    human recognises — an absolute path would just be split apart again. Sorted
    so a copy manifest reads the same on every run. No-op on a missing parent,
    same "missing dir == empty listing" intent as its siblings above.

    Distinct from iter_file_stats, which is also recursive but yields absolute
    paths bundled with size + mtime for the cache-pruning walk."""
    if not parent.exists():
        return
    for entry in sorted(parent.rglob("*")):
        if entry.is_file():
            yield entry.relative_to(parent)


def tab_complete_paths(text_prefix: str) -> list[str]:
    """Host filesystem glob for readline tab-completion. Returns list of
    string matches (~-expanded), each with `os.sep` appended if it's a
    directory. Used by menu_picker._path_completer."""
    matches = glob.glob(os.path.expanduser(text_prefix) + "*")
    return [m + os.sep if os.path.isdir(m) else m for m in matches]


# --- Stats ---

def file_mtime(path: Path | str) -> float | None:
    """Mtime of `path` as epoch seconds, or None if it doesn't exist or
    can't be stat'd. Single point of stat-call truth so callers don't deal
    with `path.stat().st_mtime` and the OSError surface directly."""
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def iter_file_stats(parent: Path) -> Iterator[tuple[Path, int, float]]:
    """Yield `(path, size, mtime)` for every regular file under `parent`.
    Used by tag_handlers.prune_caches for the size+age cache walk —
    bundling the rglob + is_file filter + stat call so the caller doesn't
    juggle three filesystem operations."""
    for f in parent.rglob("*"):
        if f.is_file():
            s = f.stat()
            yield f, s.st_size, s.st_mtime


def is_file_recent(path: Path | str, max_age_seconds: float) -> bool:
    """True iff `path` exists and its mtime is within the last `max_age_seconds`.
    Missing / unreadable / stale all → False, so callers can use this as a
    single truthy 'use this cache?' gate. Backs the {firewall} resolved-domains
    cache TTL gate in the firewall resolver."""
    mtime = file_mtime(path)
    return mtime is not None and time.time() - mtime <= max_age_seconds


# --- Host paths ---

def resolved_path(p: Path | str) -> Path:
    """Path(p) with symlinks resolved and ~ expanded."""
    return Path(p).resolve()


def resolved_cwd() -> Path:
    """Path.cwd() with symlinks resolved — what the launcher really thinks
    its working dir is, used by the picker for cwd/workspace matching."""
    return Path.cwd().resolve()


def home_dir() -> Path:
    """User's home directory as a Path."""
    return Path.home()


def home_relative(path: Path) -> str:
    """`path` with the home dir collapsed to `~` for display (e.g. in
    user-facing messages), leaving paths outside home untouched. Keeps the
    launcher from printing an operator's absolute home path verbatim."""
    home = home_dir()
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def expand_user_path(s: str) -> str:
    """Expand `~` in `s` and make it absolute; returns a string. For user-typed
    workspace paths where we want the literal expanded form (not symlink-resolved)."""
    return os.path.abspath(os.path.expanduser(s))


# --- Reads ---

def read_text(path: Path) -> str:
    """Read the entire contents of `path` as a string. Raises
    FileNotFoundError if absent (no missing_ok concept here — callers
    that expect the file might not exist should either .exists()-check
    first or use one of the higher-level readers below, which have their
    own missing-file semantics)."""
    return path.read_text()


def parse_lines(path: Path) -> Iterator[str]:
    """Iterate non-empty, non-comment-only lines from `path`, with inline
    `#` comments stripped and surrounding whitespace trimmed. Suits plain
    one-token-per-line config files (e.g. the firewall whitelist).

    Raises FileNotFoundError if the file is absent — callers must ensure
    the file exists first (typically via a paired `ensure_*` helper that
    creates it from a template). The launcher's only caller goes through
    user_firewall_whitelist_lines below, which handles ensure + cache."""
    for line in read_text(path).splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            yield line


def read_json_field(path: Path | str, *keys: str) -> Any:
    """Walk `keys` into the JSON document at `path` and return the value, or
    None on any failure: file missing, unreadable, malformed JSON, missing
    key, or a non-dict mid-walk. Callers wanting an optional field handle
    None as 'not found' rather than catching exceptions themselves."""
    try:
        cur = json.loads(read_text(Path(path)))
        for k in keys:
            cur = cur[k]
        return cur
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


def _agent_md_index(agents_dir: Path) -> dict[str, Path]:
    """Build the {agent name: md path} index for `agents_dir` — a sorted glob
    where the stem IS the agent name (axes live in `<name>.lego`). `_`-prefixed
    stems are EXCLUDED: they're hidden agents (e.g. `_quickie`, driven by their
    own entry point) that shouldn't surface in the picker, CLI target
    resolution, or the audit's orphan check. A hidden agent's own launcher
    loads its `.md` by path, not through this index."""
    return {p.stem: p for p in sorted(agents_dir.glob("*.md")) if not p.stem.startswith("_")}


@lru_cache(maxsize=None)
def agent_md_index() -> dict[str, Path]:
    """Snapshot of every agent .md in AGENTS_DIR, indexed by agent name
    (= filename stem). Cached for the launcher process lifetime — agents/ is
    hand-populated and stable across a single `run.py` invocation, and the
    lazy read keeps module import free of disk I/O. Iterate `.values()` for
    the path list, membership-check `.keys()` for the name set. The returned
    dict is SHARED across callers and must not be mutated."""
    return _agent_md_index(AGENTS_DIR)


# ============================================================
# Shared OAuth state files
# ============================================================
# Both files live in AGENTS_STATE and are bind-mounted into each container
# at launch. Claude Code refreshes the token in .credentials.json in place,
# and .claude.json holds the OAuth account info — so they must exist on the
# host before docker mounts them, or docker auto-creates them as root-owned
# directories instead of writable files.

def ensure_auth_files(adapter: Adapter) -> None:
    """Idempotently create the harness's auth files under
    `credentials/<harness>/` with the adapter's blank contents, so docker's
    bind-mount finds writable host FILES (and doesn't auto-create root-owned
    dirs on first launch). No-op when they exist — their contents are the
    CLI's, written at login and refreshed by it, never by the launcher."""
    for f in adapter.auth_files:
        path = credentials_dir(adapter.key) / f.name
        if not path_exists(path):
            ensure_dir(path.parent)
            write_text(path, f.blank)
            make_private(path)   # the CLI fills it with tokens IN PLACE, so it keeps the mode it was born with — 0644 under a default umask would leave bearer tokens world-readable


LoginState = Literal["absent", "none", "recorded", "unreadable"]


def login_state(path: Path, auth_file: AuthFile) -> LoginState:
    """What the harness's auth file at `path` holds — the one reading the
    migration, the launch notice and the audit share:
      absent      — no file, or the launcher's blank;
      none        — a JSON object without the file's `login_key` (a CLI that
                    was never logged in wrote its startup state into the blank
                    — the 2026-09-15 incident); for a file without a
                    `login_key`, never returned: anything but the blank counts;
      recorded    — the `login_key` is there (or, without one, any content);
      unreadable  — empty or not JSON. A CLI refreshing a token IN PLACE
                    leaves the file empty or partial for an instant, so this
                    is not "no login": nothing may replace such a file
                    (bug-investigator, gate one-startup).
    `login_key` names a PRIVATE shape of the CLI's (Claude Code documents
    neither `claudeAiOauth` nor `oauthAccount`), so callers that destroy or
    replace keep a `.bak` — a renamed key must cost a copy, not a login."""
    if not path_exists(path):
        return "absent"
    text = read_text(path).strip()
    if text == auth_file.blank.strip():
        return "absent"
    if not text:
        return "unreadable"   # the launcher never writes an empty file; a CLI that truncates before it rewrites does
    if auth_file.login_key is None:
        return "recorded"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "unreadable"
    return "recorded" if isinstance(data, dict) and auth_file.login_key in data else "none"


def login_recorded(path: Path, auth_file: AuthFile) -> bool:
    """`login_state(path, auth_file) == "recorded"`."""
    return login_state(path, auth_file) == "recorded"


def make_private(path: Path | str) -> None:
    """chmod 600 — a file that holds a secret (a key file, an OAuth blob)."""
    os.chmod(path, 0o600)


def file_mode(path: Path | str) -> int | None:
    """The permission bits of `path` (e.g. 0o600), or None when it is missing."""
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return None


_ENV_LINE = re.compile(r"[A-Z_][A-Z0-9_]*=\S+")


def key_file_problems(path: Path, key_env: str) -> list[str]:
    """What is wrong with a `credentials/keys/<ai>.env`, as messages that name
    variables and line numbers but NEVER a value — the one place the launcher
    reads a key file (the audit and the launch preflight both call it; docker
    itself is what consumes the file). Docker's `--env-file` grammar, not
    dotenv: a line is `NAME=value` taken VERBATIM (quotes stay in the value —
    a quoted key fails authentication with a bare 401), `#` comments only at
    the start of a line, no `export`, and a bare `NAME` line would import the
    HOST's variable into the container. The file must define the vendor's
    variable `key_env` and nothing else — every line lands in the container's
    environment, so a stray one could override a launcher setting."""
    problems: list[str] = []
    names: list[str] = []
    # Raw bytes, not `read_text`: universal newlines would hide the \r docker sees.
    text = Path(path).read_bytes().decode("utf-8", errors="replace")
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw
        if line.endswith("\r"):
            problems.append(f"line {number}: Windows line ending — docker would keep the \\r in the value")
            line = line.rstrip("\r")
        if not line.strip() or line.startswith("#"):
            continue
        if _ENV_LINE.fullmatch(line):
            name = line.partition("=")[0]
            if name in names:
                problems.append(f"{name} is defined twice")
            names.append(name)
            if name != key_env:
                problems.append(f"{name} is not the vendor's key variable ({key_env}) — a key file defines only that")
            if line.partition("=")[2][0] in "'\"":
                problems.append(f"line {number}: {name} is quoted — docker keeps the quotes in the value")
            continue
        if line.startswith("export "):
            problems.append(f"line {number}: `export` prefix — docker takes the line verbatim, write NAME=value")
        elif "=" not in line:
            problems.append(f"line {number}: a bare name imports the HOST's variable into the container — write NAME=value")
        else:
            problems.append(f"line {number}: not NAME=value (an upper-case name, no spaces)")
    if key_env not in names:
        problems.append(f"does not define {key_env}")
    return problems


# ============================================================
# Optional credentials (~/.ai-agents/user_extras/optional_creds/)
# ============================================================

@lru_cache(maxsize=None)
def present_optional_cred_services() -> frozenset[str]:
    """Frozenset of OPTIONAL_CREDS_MOUNTS service names whose host dir is
    present. LRU-cached for the launcher process lifetime — both
    user_additions.optional_creds_mounts and container_env.set_container_env
    (via toolkit_install_flags) consume it, so without dedupe each would
    independently stat-check every service (~11 × 2 = ~22 stats per launch)."""
    return frozenset(
        name for name in OPTIONAL_CREDS_MOUNTS
        if optional_creds_service_path(name).exists()
    )


def installed_cred_clis() -> str:
    """Space-joined CLI names for present optional-cred services that install
    a CLI in Dockerfile.code (cli != None in OPTIONAL_CREDS_MOUNTS). Order
    follows the OPTIONAL_CREDS_MOUNTS declaration so the addendum reads in a
    stable order across launches. Used to render the body of
    tags/addendums.py's CREDENTIALS_NOTICE — a no-creds environment collapses
    the body to '' and composed_addendum drops the sub-section entirely."""
    present = present_optional_cred_services()
    return " ".join(
        cli for name, (_, cli) in OPTIONAL_CREDS_MOUNTS.items()
        if cli is not None and name in present
    )


def optional_cred_tokens() -> dict[str, str]:
    """Return `{service_name: token_string}` for every service in
    OPTIONAL_CREDS_TOKEN_ENV_VARS that has a non-empty `<service>/token`
    file on the host. Tokens are stripped of leading/trailing whitespace
    (the file is expected to hold just the secret). Empty files and absent
    files are silently skipped."""
    out = {}
    for name in OPTIONAL_CREDS_TOKEN_ENV_VARS:
        token_file = optional_creds_token_path(name)
        if token_file.is_file():
            value = read_text(token_file).strip()
            if value:
                out[name] = value
    return out


# ============================================================
# User firewall whitelist (~/.ai-agents/user_extras/firewall_whitelist.txt)
# ============================================================

@lru_cache(maxsize=None)
def user_firewall_whitelist_lines() -> tuple[str, ...]:
    """Return the user's firewall_whitelist.txt parsed lines as a tuple,
    self-planting from FIREWALL_WHITELIST_TEMPLATE on first read so parse_lines
    never sees a missing file. (copy_file is a no-op when the destination
    already exists, so this stays cheap on subsequent launches; the
    user_additions.plant_user_extras call in setup_state also lands on it
    idempotently.) Cached for the launcher process lifetime, so the two
    callers — network.start_whitelist_resolution and the launch-banner count
    in menu_picker.print_launch_banner — share one read + parse instead of
    doing it independently."""
    copy_file(FIREWALL_WHITELIST_TEMPLATE, FIREWALL_WHITELIST_FILE)
    return tuple(parse_lines(FIREWALL_WHITELIST_FILE))
