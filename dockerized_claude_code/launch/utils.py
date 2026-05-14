"""Domain-neutral helpers shared across launch/ modules. Time-side helpers —
relative_time for picker-side display, is_file_recent for freshness gating
on cache-style files. Disk-touching helpers (read/write/stat wrappers) live
in file_access.py per the "all file I/O lives in one place" rule; the stat
call here is a freshness *check*, not a read/write of the contents.

Leaf module: imports nothing from sibling launch/ modules — kept pull-able
from anywhere without circular-import risk.
"""

import time
from datetime import datetime


def relative_time(mtime):
    """Human-readable relative time from an epoch mtime (e.g. '3 days ago',
    '5 minutes ago'). Display-only — used by the picker's Cont preview for
    the 'Last used' line."""
    delta = datetime.now() - datetime.fromtimestamp(mtime)
    if delta.days >= 1:
        return f"{delta.days} day{'s' if delta.days != 1 else ''} ago"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    minutes = delta.seconds // 60
    return f"{minutes} minute{'s' if minutes != 1 else ''} ago" if minutes else "just now"


def is_file_recent(path, max_age_seconds):
    """True iff `path` exists and its mtime is within the last `max_age_seconds`.
    False for missing files, stale files, or anything that fails to stat (so
    callers can use a single truthy check as a 'use cache?' gate). Used by the
    {auto}-mode resolved-domains cache to decide whether to reuse previously-
    resolved IPs instead of running a fresh DNS pass."""
    try:
        return time.time() - path.stat().st_mtime <= max_age_seconds
    except OSError:
        return False
