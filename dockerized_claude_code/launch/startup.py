"""Every entry point's FIRST step — one function, so no entry can skip what
the others do.

`run.py`, the quickie (`q`) and the cluster CLI (`cluster.py`) each open the
launcher the same way: bring the host state up to date
(`tags/migrations.ensure_migrated` — the state dir's name, the credentials'
home, the retired store format), then scan the tag tree. The order matters and
is the same everywhere: a launch reads and creates files under the state dir
(the store, the login blanks docker binds), so the migrations must have run
before anything else touches it.

Why a module for two lines: on 2026-09-15 the cluster CLI launched a cluster
without the migrations the other two entries ran. It created blank login files
at the credentials' new home, mounted those into every member, and the real
files stayed at the old place; the next solo launch found a non-blank file the
members' CLI had written and would not overwrite it — the operator was asked
to log in again. Three entry points, three spellings of "start up" — now one.
"""

from __future__ import annotations

from pathlib import Path

from .paths import AGENTS_DIR
from .tags import Registry, migrations, scan_all


def open_launcher(agents_dir: Path = AGENTS_DIR) -> Registry:
    """Migrate the host state, then scan the tag tree. Raises `TagError` for a
    broken tree — every entry wraps it the same way (`call_or_exit`, or the
    cluster CLI's refusal), never a traceback."""
    migrations.ensure_migrated()
    return scan_all(agents_dir)
