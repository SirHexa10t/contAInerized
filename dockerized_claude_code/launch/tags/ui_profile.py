"""The launcher-UI profile — profession-INDEPENDENT preferences, persisted at
`~/.claude-agents/ui_profile.toml` (`paths.ui_profile_path`), manifested by
`settings/ui.form`.

    # ~/.claude-agents/ui_profile.toml
    herdr_instead_of_tmux = true

The toolkit profiles' sibling (tags/toolkit_profile.py), with two deliberate
differences:

- the MANIFEST has its own parser and entry type (`UiEntry`), not
  profession.py's: template.form entries gate Dockerfile build-args and that
  parser's strictness protects the image pipeline — these entries gate
  launcher behavior only, so the fields differ (`body` panel text instead of
  run_command/language; no sizes, no args);
- the FORM read (`load_ui_profile`) reconciles like a toolkit profile — a
  missing key falls back to its manifest default, so the form always opens —
  while the LAUNCH read (`muxer_backend`) is STRICT by the operator's spec: a
  missing FILE is first-launch normal and generated from the manifest's
  defaults, but a file that exists WITHOUT the field is a loud stop naming
  it, with the fix in the message (rename the file away; the next launch
  regenerates it). Never a silent fallback: a hand-edit that loses the field
  must not quietly flip the muxer.

Reading goes through stdlib `tomllib`; writing through the same tiny
commented-bool emitter shape as the toolkit profiles. Cache-free like them.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..file_access import path_exists, read_text, write_text
from ..paths import UI_FORM, ui_profile_path
from .base import TagError, read_toml

# The field `cluster.backend()` launches by. Named for what its CHECKBOX
# asks; a future third backend would outgrow the bool and become a radio
# group (the form machinery already has them) plus a migrations.py entry —
# deliberately not pre-built.
MUXER_FIELD = "herdr_instead_of_tmux"


@dataclass(frozen=True)
class UiEntry:
    """One row of `settings/ui.form` — a launcher preference shown in the
    "(Edit Preferences)" form's UI section. `body` is the focused row's
    panel text (the tradeoff the toggle decides); `default` seeds a fresh
    ui_profile.toml, after which the user's file wins."""
    key: str
    description: str
    default: bool
    body: str


def load_ui_form(path: Path = UI_FORM) -> dict[str, UiEntry]:
    """Parse the UI manifest into `{key: UiEntry}` — fail loud on a missing
    field, exactly like the toolkit manifest parser: a manifest typo should
    surface at form/launch time as a named error, never as a silently absent
    toggle."""
    out: dict[str, UiEntry] = {}
    for key, fields in read_toml(path).items():
        try:
            out[key] = UiEntry(key=key, description=fields["description"],
                               default=bool(fields["default"]),
                               body=fields["body"])
        except KeyError as exc:
            raise TagError(
                f"{path}: entry '{key}' missing required field {exc}") from exc
    return out


def load_ui_profile(path: Path, entries: dict[str, UiEntry]) -> dict[str, bool]:
    """The on-disk profile as `{key: bool}` for the FORM — a missing file or
    missing keys reconcile to manifest defaults (the lenient read; the launch
    path's strict read is `muxer_backend`, see the module docstring)."""
    if not path_exists(path):
        return {key: entry.default for key, entry in entries.items()}
    data = tomllib.loads(read_text(path))
    return {key: bool(data[key]) if key in data else entry.default
            for key, entry in entries.items()}


def save_ui_profile(path: Path, values: dict[str, bool],
                    entries: dict[str, UiEntry]) -> None:
    """Persist the UI section's checkboxes, one commented `key = bool` line
    per manifest entry — keys outside the CURRENT manifest are dropped, the
    same always-current rule as the toolkit profiles."""
    lines = [
        '# Launcher UI preferences — edit via the picker\'s "(Edit Preferences)"',
        "# menu, or by hand (booleans only). Read fresh at every launch.",
        "",
    ]
    for key, entry in sorted(entries.items()):
        lines.append(f"# {entry.description}")
        lines.append(f"{key} = {'true' if values.get(key, entry.default) else 'false'}")
        lines.append("")
    write_text(path, "\n".join(lines).rstrip("\n") + "\n")


def muxer_backend() -> str:
    """`"herdr"` or `"tmux"` — THE launch-path read of ui_profile.toml.

    Strict by the operator's spec: a missing FILE is first-launch normal
    (generated here from manifest defaults, so the user finds it editable);
    a file WITHOUT the field is a loud stop with the fix in the message."""
    entries = load_ui_form()
    if MUXER_FIELD not in entries:
        raise TagError(f"{UI_FORM} must define '{MUXER_FIELD}' — the launcher "
                       f"cannot pick a multiplexer backend without it")
    path = ui_profile_path()
    if not path_exists(path):
        save_ui_profile(path, {}, entries)      # defaults, per entry
        return "herdr" if entries[MUXER_FIELD].default else "tmux"
    data = tomllib.loads(read_text(path))
    if MUXER_FIELD not in data:
        raise SystemExit(
            f"{path} has no '{MUXER_FIELD}' — the launcher cannot pick a "
            f"multiplexer backend. Rename or delete that file and relaunch: "
            f"a fresh one is generated from settings/ui.form defaults (other "
            f"UI toggles will need re-picking).")
    return "herdr" if data[MUXER_FIELD] else "tmux"
