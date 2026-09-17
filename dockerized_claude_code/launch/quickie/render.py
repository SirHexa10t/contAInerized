"""Rendering for quickie's `claude -p --output-format stream-json` events: a
live `⋯ thinking… (Ns)` note on stderr while the model reasons, then the answer
streamed to stdout as `text_delta` events arrive.

Current models redact extended-thinking text in headless mode (`display:
omitted` — thinking blocks stream a signature but no readable text), so the
reasoning CONTENT can't be shown; the ticker just signals that thinking is
happening and how long it's taken. Answer text goes to stdout and the progress
note to stderr, so `q "..." > file` captures only the answer.

A `result` that is an error — `is_error: true`, whatever its `subtype` (a
"Not logged in · Please run /login" run ends with subtype `success` and
is_error true) — is reported on stderr WITH the CLI's own words, and so is a
run that ended with no answer at all: until 2026-09-16 the renderer printed
only streamed `text_delta`s and flagged only non-success subtypes, so the
login failure showed nothing after the docker output and could only be read
back with `q --answer` (the transcript had recorded it as the assistant's
turn — the CLI delivers such a message as a whole `assistant` event, never as
deltas). An `assistant` event's text is held back and printed to stdout only
when the run succeeds without having streamed (a harness that does not send
deltas), so an error's text never lands on the answer channel.

Unknown or unparseable event lines are skipped, so a change to Claude Code's
stream-json schema degrades to 'answer only' rather than crashing the question."""

import json
import sys
import threading
import time
from collections.abc import Iterable
from typing import Any


LOGIN_HINT = ("log in once through a normal launch (`ai <agent>`, then /login inside the container) — "
              "the quickie reuses that login")


def render_stream(lines: Iterable[str], *, tick: bool = True) -> None:
    """Consume Claude Code stream-json lines: start the thinking ticker when a
    thinking block opens, stop it and stream the answer once `text_delta`s
    arrive, hold an `assistant` event's text for a run that streams nothing,
    and report an error `result` — or a run that ended without any answer —
    on stderr in the CLI's own words. `tick=False` disables the background
    ticker for deterministic tests (answer rendering is unaffected)."""
    ticker = _Ticker() if tick else None
    answer_started = False
    held: list[str] = []          # an assistant event's text, printed only if the run succeeds without streaming
    concluded = False             # a `result` event arrived and was reported
    try:
        for raw in lines:
            event = _parse(raw)
            if event is None:
                continue
            if event.get("type") == "assistant" and not answer_started:
                text = _message_text(event)
                if text:
                    held.append(text)
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
            elif event.get("type") == "result":
                concluded = True
                if ticker:
                    ticker.stop()
                failed = bool(event.get("is_error")) or event.get("subtype") != "success"
                said = event.get("result") if isinstance(event.get("result"), str) else ""
                if failed:
                    _report_failure(event.get("subtype") or "error", said or " ".join(held))
                elif not answer_started and held:
                    print("\n".join(held), end="", flush=True)   # a success that never streamed: the message is the answer; the closing newline follows below
                    answer_started = True
    finally:
        if ticker:
            ticker.stop()
    if answer_started:
        print()   # close the streamed answer with a newline
    elif not concluded:
        # No answer and no verdict: the stream ended early (the container died,
        # the CLI crashed before its result). Never silent.
        print("\n[quickie] claude ended without an answer" + (f": {' '.join(held)}" if held else "."),
              file=sys.stderr, flush=True)


def _report_failure(subtype: str, said: str) -> None:
    """The stderr report for a run that did not answer: the CLI's own words
    (`Not logged in · Please run /login`) and, for a login failure, how the
    quickie gets one — it never asks itself, being a headless `-p` run."""
    detail = f": {said}" if said else "."
    print(f"\n[quickie] claude ended without an answer ({subtype}){detail}", file=sys.stderr, flush=True)
    if "not logged in" in said.lower() or "/login" in said:
        print(f"[quickie] {LOGIN_HINT}", file=sys.stderr, flush=True)


def _message_text(event: dict[str, Any]) -> str:
    """The joined text blocks of an `assistant` event's message, or ""."""
    content = (event.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(block["text"] for block in content
                         if isinstance(block, dict) and block.get("type") == "text"
                         and isinstance(block.get("text"), str)).strip()
    return ""


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
