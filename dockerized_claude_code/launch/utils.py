"""Generic helpers shared across launch/ modules. Domain-neutral — nothing
here knows about agents, modes, paths, or docker.

Imports nothing from sibling launch/ modules — kept as a leaf so it can be
pulled in anywhere without circular-import risk."""

import json
from pathlib import Path


def parse_lines(path):
    """Iterate non-empty, non-comment-only lines from `path`, with inline `#`
    comments stripped and surrounding whitespace trimmed. Yields nothing if the
    file is missing. Suits plain one-token-per-line config files (e.g. the
    firewall whitelist) — see agent_composition.load_conf for `KEY=VALUE` style."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            yield line


def read_json_field(path, *keys):
    """Walk `keys` into the JSON document at `path` and return the value, or
    None on any failure: file missing, unreadable, malformed JSON, missing key,
    or a non-dict mid-walk. Callers wanting an optional field handle None as
    "not found" rather than catching exceptions themselves."""
    try:
        cur = json.loads(Path(path).read_text())
        for k in keys:
            cur = cur[k]
        return cur
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
