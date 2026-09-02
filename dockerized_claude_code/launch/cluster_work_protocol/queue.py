"""The queue itself — the ONLY module that touches chat.jsonl.

One append-only JSONL file per cluster session (the recorded MQ-type
recommendation: no broker, no daemon, no database — at cluster scale a queue
is a coordination LOG). Everything that makes it a queue lives here:

- **Total order:** `seq` is assigned under an exclusive flock from a counter
  file, so two members appending "simultaneously" serialize — the serial
  chain, mechanically.
- **Atomic appends:** the line is written whole while the lock is held; a
  reader never sees a torn line.
- **Broadcast reads:** `read_since(seq)` and per-member cursor files (one
  int each) give every member its own "what's new to ME" without anyone
  consuming anything.

The lock is `flock(2)` on a sidecar `.lock` file. flock binds to the open
file description, so every `append()` opening the lock file fresh serializes
against every other appender — other processes AND other threads alike. The
acquisition wait is bounded (`lock_wait_seconds`, config): a wedged holder
surfaces as a loud ProtocolError, not a hang.

STANDALONE CONSTRAINT (see __init__): stdlib only, no imports beyond this
package.
"""

from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Callable, Iterator, TypeVar

from .schema import Message, ProtocolError

T = TypeVar("T")

# The protocol's home inside a cluster container. Under /cluster (the shared
# per-session mount) so the queue file persists with the session and every
# member sees one file — but created by the ENTRYPOINT's setup commands
# (mkdir -p), never as a docker mountpoint parent: those arrive root-owned
# (the recorded herdr lesson) and this dir must be member-writable.
PROTOCOL_DIR_IN_CONTAINER = Path("/cluster/protocol")
CHAT_FILENAME = "chat.jsonl"
CURSORS_DIRNAME = "cursors"
_LOCK_FILENAME = ".lock"
_SEQ_FILENAME = ".seq"
_LOCK_POLL_SECONDS = 0.05


class Queue:
    """One cluster session's message queue, rooted at `root` (in-container:
    PROTOCOL_DIR_IN_CONTAINER; tests: any tmp dir). Instantiation is cheap
    and stateless — every operation opens, locks, and closes; nothing is
    cached between calls (the toolkit-profile stance: staleness bugs cost
    more than reopening small files)."""

    def __init__(self, root: Path, lock_wait_seconds: int) -> None:
        self.root = root
        self.lock_wait_seconds = lock_wait_seconds

    @property
    def chat_path(self) -> Path:
        return self.root / CHAT_FILENAME

    def _cursor_path(self, member: str) -> Path:
        return self.root / CURSORS_DIRNAME / member

    @contextmanager
    def _locked(self) -> Iterator[IO[bytes]]:
        """Exclusive flock on the sidecar, bounded by lock_wait_seconds.
        Non-blocking attempts in a poll loop rather than LOCK_EX, so a
        wedged holder becomes a named error instead of a silent hang."""
        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.lock_wait_seconds
        handle = open(self.root / _LOCK_FILENAME, "wb")
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ProtocolError(
                            f"could not take the queue lock within "
                            f"{self.lock_wait_seconds}s — is another append "
                            f"wedged? ({self.root / _LOCK_FILENAME})") from None
                    time.sleep(_LOCK_POLL_SECONDS)
            yield handle
        finally:
            handle.close()    # closing releases the flock

    def _next_seq_locked(self) -> int:
        """Advance the counter file. Caller holds the lock."""
        seq_path = self.root / _SEQ_FILENAME
        current = 0
        if seq_path.exists():
            current = int(seq_path.read_text() or 0)
        seq_path.write_text(str(current + 1))
        return current + 1

    def append(self, member: str, kind: str, body: str = "", *,
               iteration: str | None = None,
               stance: int | None = None) -> Message:
        """Validate, assign the next seq, and append one line — all under
        the lock, so the seq a message carries IS its position."""
        message, _ = self.append_with(member, kind, body,
                                      iteration=iteration, stance=stance)
        return message

    def append_with(self, member: str, kind: str, body: str = "", *,
                    iteration: str | None = None,
                    stance: int | None = None,
                    guard: "Callable[[list[Message]], None] | None" = None,
                    check: "Callable[[list[Message]], T] | None" = None,
                    ) -> "tuple[Message, T | None]":
        """`append` with a pre-write `guard` and a post-write `check`, BOTH
        evaluated over the whole journal inside ONE lock hold — the gate
        machinery's atomicity primitive:

        - `guard` (journal WITHOUT the new message) raises ProtocolError to
          refuse the append — reply caps and closed-gate checks cannot race
          a concurrent post;
        - `check` (journal WITH the new message) is the completion detector:
          of N racing repliers exactly ONE sees the count hit its threshold,
          because seq assignment, the write, and the read share the flock
          (the plan's 'detected under the same flock as the append')."""
        with self._locked():
            journal = self._read_all_unlocked()
            if guard is not None:
                guard(journal)
            message = Message(
                seq=self._next_seq_locked(),
                ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                member=member, kind=kind, body=body,
                iteration=iteration, stance=stance)
            message.validate()
            with open(self.chat_path, "a", encoding="utf-8") as chat:
                chat.write(message.to_line())
            result = check(journal + [message]) if check is not None else None
        return message, result

    def _read_all_unlocked(self) -> list[Message]:
        if not self.chat_path.exists():
            return []
        return [Message.from_line(line)
                for line in self.chat_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def read_all(self) -> list[Message]:
        """Every message, in order. Unparseable lines raise — the file is
        launcher-written under a lock, so a bad line is a real defect, not
        noise to skim past. (Reads take no lock: a line is written whole and
        flushed while the writer holds it, so a reader never sees a torn
        one.)"""
        return self._read_all_unlocked()

    def read_since(self, seq: int) -> list[Message]:
        """Messages strictly after `seq`, in order."""
        return [message for message in self.read_all() if message.seq > seq]

    def cursor(self, member: str) -> int:
        """The member's read position — 0 (nothing read) when unset."""
        path = self._cursor_path(member)
        if not path.exists():
            return 0
        return int(path.read_text() or 0)

    def read_new(self, member: str, *, advance: bool = True) -> list[Message]:
        """What `member` hasn't seen yet; by default the cursor advances to
        the last returned message (pass advance=False to peek)."""
        fresh = self.read_since(self.cursor(member))
        if advance and fresh:
            path = self._cursor_path(member)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(fresh[-1].seq))
        return fresh
