"""Rendering the TOML the launcher writes BY HAND — key quoting, string
escaping, and the string-list line.

Reading TOML is `tomllib`'s job (stdlib, read-only). Writing it is ours, and
two modules do it: `tags/store.py` (instances.toml) and `cluster/state.py`
(cluster.toml). Both carried byte-identical copies of the quoting rules,
which is one edit away from two files that quote differently — and the
failure mode is quiet, because each file's own reader keeps parsing until a
key finally needs escaping.

Deliberately not `tomli-w`: what the launcher emits is two fixed shapes
(optional strings and string lists), and this module is the whole of it.

Leaf module: stdlib only, imports nothing from launch/ — pullable from any
layer without circular-import risk.
"""

import json
import re
from collections.abc import Iterable

# TOML bare keys: letters/digits/underscore/dash. Anything else (a future
# dotted agent name, say) gets basic-string quoting so the file stays valid.
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def key(name: str) -> str:
    """`name` as a TOML key: bare when it can be, quoted when it must be."""
    return name if _BARE_KEY_RE.match(name) else json.dumps(name)


def string(value: str) -> str:
    """`value` as a TOML basic string. JSON string escaping is a subset of
    TOML's (`\\"` `\\\\` `\\n` `\\t` `\\uXXXX` …), so json.dumps emits a
    valid TOML string."""
    return json.dumps(value)


def string_list(name: str, values: Iterable[str]) -> str:
    """A `name = ["a", "b"]` line — the shape both stores use for tag axes.
    An empty `values` renders `name = []` rather than omitting the key: an
    axis the user emptied must round-trip as empty, not as absent (absent is
    what a legacy file means, and that falls back to defaults)."""
    return f"{name} = [{', '.join(string(v) for v in values)}]"
