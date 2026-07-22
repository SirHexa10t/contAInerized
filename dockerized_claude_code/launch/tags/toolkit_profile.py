"""Per-profession toolkit profiles — the user's install toggles for a
configurable profession's optional tools, persisted at
`~/.claude-agents/<profession>_profile.toml` (`paths.toolkit_profile_path`).

    # ~/.claude-agents/code_profile.toml
    rust = true
    node = true
    cmake = true
    gh = false
    ...

One flat bool per `ToolkitEntry` in the profession's `template.form`
(`Profession.load_toolkit()`) — global, not per-instance: it configures the
one shared image every instance of that profession builds from, same as
`instances.toml` is per-instance but this is per-profession. Edited via the
picker's "Edit Toolkits" menu (menu_picker._edit_profession_toolkit); a hand
edit works too, since load() re-reads the file fresh each call.

Schema evolution: a key present in the manifest but missing from the user's
file (a tool added after the file was first generated) falls back to that
entry's `default`; a key present in the file but no longer in the manifest
(a tool removed) is silently dropped on the next save — the file always
reflects the CURRENT manifest, never stale cruft from an older one.

Reading goes through stdlib `tomllib`; writing through the small emitter
below — the schema is flatter than instances.toml's (bool-only, one level),
so the emitter is a few lines. Deliberately cache-free, like store.py, for
the same reason: trivial to test, no staleness across the picker's reads.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from ..file_access import path_exists, read_text, write_text
from .profession import ToolkitEntry


def _toggleable(entries: dict[str, ToolkitEntry]) -> dict[str, ToolkitEntry]:
    """Just the user-configurable entries — locked ones (informational, e.g.
    Python) carry no toggle, so they never reach the profile file or the
    build-arg flags."""
    return {key: entry for key, entry in entries.items() if not entry.locked}


def default_profile(entries: dict[str, ToolkitEntry]) -> dict[str, bool]:
    """A fresh profile straight from the manifest's own defaults — what a
    profession gets before the user has ever touched its toolkit form. Locked
    entries are omitted (they aren't toggles)."""
    return {key: entry.default for key, entry in _toggleable(entries).items()}


def load_profile(path: Path, entries: dict[str, ToolkitEntry]) -> dict[str, bool]:
    """The on-disk profile as `{key: bool}`, missing/removed keys reconciled
    against `entries` (see module docstring). `{}` when nothing is toggleable
    (no template.form, or only locked entries). A missing or empty file
    yields `default_profile(entries)`."""
    toggleable = _toggleable(entries)
    if not toggleable:
        return {}
    if not path_exists(path):
        return default_profile(entries)
    data: dict[str, Any] = tomllib.loads(read_text(path))
    return {key: bool(data[key]) if key in data else entry.default
            for key, entry in toggleable.items()}


def save_profile(path: Path, values: dict[str, bool], entries: dict[str, ToolkitEntry]) -> None:
    """Persist `values` (as returned by the toolkit form) to `path`, one
    commented `key = bool` line per TOGGLEABLE entry — the comment carries the
    description + size ballpark so a hand-editor sees them without cross-
    referencing template.form. Locked entries and keys outside `entries` (a
    stale prior file after a manifest change) are dropped: the file always
    matches the CURRENT toggleable manifest."""
    lines = [
        "# Toolkit install toggles — edit via the picker's \"Edit Toolkits\" menu,",
        "# or by hand (booleans only). Re-launch to apply: a changed value feeds a",
        "# fresh --build-arg, so only that tool's Docker layer rebuilds.",
        "",
    ]
    for key, entry in sorted(_toggleable(entries).items()):
        lines.append(f"# {entry.description} (~{entry.approx_size_mb}MB)")
        lines.append(f"{key} = {'true' if values.get(key, entry.default) else 'false'}")
        lines.append("")
    write_text(path, "\n".join(lines).rstrip("\n") + "\n")
