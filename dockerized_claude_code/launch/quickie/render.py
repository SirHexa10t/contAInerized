"""Rendering for quickie's `claude -p --output-format stream-json` events: a
live `⋯ thinking… (Ns)` note on stderr while the model reasons, then the answer
streamed to stdout as `text_delta` events arrive.

Current models redact extended-thinking text in headless mode (`display:
omitted` — thinking blocks stream a signature but no readable text), so the
reasoning CONTENT can't be shown; the ticker just signals that thinking is
happening and how long it's taken. Answer text goes to stdout and the progress
note to stderr, so `q "..." > file` captures only the answer.

Unknown or unparseable event lines are skipped, so a change to Claude Code's
stream-json schema degrades to 'answer only' rather than crashing the question."""

import json
import sys
import threading
import time
from collections.abc import Iterable
from typing import Any


def render_stream(lines: Iterable[str], *, tick: bool = True) -> None:
    """Consume Claude Code stream-json lines: start the thinking ticker when a
    thinking block opens, stop it and stream the answer once `text_delta`s
    arrive, and note a non-success `result` on stderr. `tick=False` disables the
    background ticker for deterministic tests (answer rendering is unaffected)."""
    ticker = _Ticker() if tick else None
    answer_started = False
    try:
        for raw in lines:
            event = _parse(raw)
            if event is None:
                continue
            if event.get("type") == "stream_event":
                inner = event.get("event") or {}
                itype = inner.get("type")
                if itype == "content_block_start" and (inner.get("content_block") or {}).get("type") == "thinking":
                    if ticker:
                        ticker.start()
                elif itype == "content_block_delta":
                    delta = inner.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        if not answer_started:
                            answer_started = True
                            if ticker:
                                ticker.stop()
                        print(delta["text"], end="", flush=True)
            elif event.get("type") == "result" and event.get("subtype") != "success":
                if ticker:
                    ticker.stop()
                print(f"\n[quickie] claude ended without an answer ({event.get('subtype') or 'error'}).",
                      file=sys.stderr, flush=True)
    finally:
        if ticker:
            ticker.stop()
    if answer_started:
        print()   # close the streamed answer with a newline


def _parse(line: str) -> dict[str, Any] | None:
    """A stream-json line as a dict, or None for a blank / non-JSON / non-object line."""
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


class _Ticker:
    """A background `⋯ thinking… (Ns)` line on stderr while the model reasons —
    elapsed time is the only progress signal, since the reasoning text itself is
    redacted. start()/stop() are idempotent; stop() clears the line and joins
    the daemon thread."""

    _INTERVAL = 0.5

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        start = time.monotonic()
        while not self._stop.wait(self._INTERVAL):
            print(f"\r⋯ thinking… ({int(time.monotonic() - start)}s)", end="", file=sys.stderr, flush=True)

    def stop(self) -> None:
        if self._thread is None or self._stop.is_set():
            return
        self._stop.set()
        self._thread.join(timeout=1)
        print("\r" + " " * 40 + "\r", end="", file=sys.stderr, flush=True)   # clear the ticker line
