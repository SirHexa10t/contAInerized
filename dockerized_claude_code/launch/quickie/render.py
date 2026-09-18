"""Rendering for quickie's `claude -p --output-format stream-json` events: a
live `⋯ thinking… (Ns)` note on stderr while the model reasons, then the answer
streamed to stdout as `text_delta` events arrive.

**A terminal gets MARKDOWN; a pipe gets the source.** Models answer in
markdown, so on a TTY the answer is rendered as it arrives — headings, bold,
lists, syntax-highlighted code — by a rich `Live` that re-renders the growing
document (verified 2026-09-18: a document taller than the screen still prints
each line exactly once, so nothing duplicates in the scrollback). Redirected
or piped, the raw text streams exactly as the model wrote it: `q … > notes.md`
must stay valid markdown, and a script parsing the answer must not meet ANSI
escapes. One rule, `_wants_markdown`, decides.

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

from rich.console import Console                                          # dep — declared in pyproject.toml [project]
from rich.live import Live
from rich.markdown import Markdown

# How often the growing answer is re-rendered. Fast enough to read as
# streaming, slow enough that a long answer is not re-laid-out per token.
REFRESH_PER_SECOND = 8


LOGIN_HINT = ("log in once through a normal launch (`ai <agent>`, then /login inside the container) — "
              "the quickie reuses that login")


def _wants_markdown(markdown: bool | None = None) -> bool:
    """Whether to RENDER the answer rather than emit its source: only when
    stdout is a terminal, because a redirect or a pipe must receive exactly
    what the model wrote. `markdown` overrides the detection (the tests pin
    both paths without a pty)."""
    if markdown is not None:
        return markdown
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):   # a closed or exotic stdout is not a terminal
        return False


def print_markdown(text: str) -> None:
    """Print `text` to stdout as rendered markdown on a terminal, or as its
    own source anywhere else — the one spelling the streamed answer and
    `q --answer` share, so a reprint looks like the answer did."""
    if _wants_markdown():
        Console().print(Markdown(text))
    else:
        print(text)


class _RawAnswer:
    """The answer as the model wrote it, streamed straight to stdout — what a
    redirect or a pipe receives."""

    def __init__(self) -> None:
        self.started = False

    def write(self, text: str) -> None:
        self.started = True
        print(text, end="", flush=True)

    def close(self) -> None:
        if self.started:
            print()   # close the streamed answer with a newline

    def whole(self, text: str) -> None:
        """A complete answer that never streamed (a harness that sends no
        deltas) — the same channel, one write."""
        self.write(text)


class _PrettyAnswer:
    """The answer RENDERED as markdown while it arrives: a rich `Live` that
    re-renders the growing document, so the reader sees it build and ends
    with headings, lists and highlighted code rather than raw asterisks.

    The Live opens on the first text, never before — an answer that never
    comes must leave the terminal untouched for the error report."""

    def __init__(self) -> None:
        self.started = False
        self._text = ""
        self._live: Live | None = None

    def write(self, text: str) -> None:
        self.started = True
        self._text += text
        if self._live is None:
            self._live = Live(console=Console(), refresh_per_second=REFRESH_PER_SECOND,
                              vertical_overflow="visible")
            self._live.start()
        self._live.update(Markdown(self._text))

    def close(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def whole(self, text: str) -> None:
        """A complete answer that never streamed — rendered in one pass, with
        no Live at all: there is nothing to watch grow."""
        self.started = True
        Console().print(Markdown(text))


def render_stream(lines: Iterable[str], *, tick: bool = True, markdown: bool | None = None,
                  login_hint: str = LOGIN_HINT) -> None:
    """Consume Claude Code stream-json lines: start the thinking ticker when a
    thinking block opens, stop it and show the answer once `text_delta`s
    arrive, hold an `assistant` event's text for a run that streams nothing,
    and report an error `result` — or a run that ended without any answer —
    on stderr in the CLI's own words. The answer is rendered as markdown on a
    terminal and emitted as its source anywhere else (`markdown` overrides).
    `tick=False` disables the background ticker for deterministic tests
    (answer rendering is unaffected). `login_hint` is what to print when the
    CLI says it is not logged in — the caller supplies it because only the
    caller knows which credentials it mounted."""
    answer: _PrettyAnswer | _RawAnswer = _PrettyAnswer() if _wants_markdown(markdown) else _RawAnswer()
    ticker = _Ticker() if tick else None
    answer_started = False
    held: list[str] = []          # an assistant event's text, printed only if the run succeeds without streaming
    stray: list[str] = []         # stdout lines that are not stream-json at all — a CLI refusing BEFORE it starts the stream prints plain text, and dropping it is how a run went silent
    concluded = False             # a `result` event arrived and was reported
    try:
        for raw in lines:
            event = _parse(raw)
            if event is None:
                if raw.strip():
                    stray.append(raw.strip())
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
                                ticker.stop()   # the ticker owns the line until the answer takes it
                        answer.write(delta["text"])
            elif event.get("type") == "result":
                concluded = True
                if ticker:
                    ticker.stop()
                failed = bool(event.get("is_error")) or event.get("subtype") != "success"
                said = event.get("result") if isinstance(event.get("result"), str) else ""
                if failed:
                    _report_failure(event.get("subtype") or "error", said or " ".join(held) or _tail(stray), login_hint)
                elif not answer_started and held:
                    answer.whole("\n".join(held))   # a success that never streamed: the message is the answer
                    answer_started = True
    finally:
        if ticker:
            ticker.stop()
        answer.close()      # a Live must be stopped even if the stream raised
    if not answer_started and not concluded:
        # No answer and no verdict: the stream ended early (the container died,
        # the CLI refused before opening the stream). Never silent, and never
        # without what it said — a plain-text refusal on stdout is not noise
        # when nothing else arrived (operator, 2026-09-18: a "Not logged in"
        # run printed nothing and could only be read back with `q --answer`).
        said = " ".join(held) or _tail(stray)
        print("\n[quickie] claude ended without an answer" + (f": {said}" if said else "."),
              file=sys.stderr, flush=True)
        if said:
            _login_hint_if_needed(said, login_hint)


def _report_failure(subtype: str, said: str, login_hint: str = LOGIN_HINT) -> None:
    """The stderr report for a run that did not answer: the CLI's own words
    (`Not logged in · Please run /login`) and, for a login failure, how the
    quickie gets one — it never asks itself, being a headless `-p` run."""
    detail = f": {said}" if said else "."
    print(f"\n[quickie] claude ended without an answer ({subtype}){detail}", file=sys.stderr, flush=True)
    _login_hint_if_needed(said, login_hint)


def _login_hint_if_needed(said: str, login_hint: str = LOGIN_HINT) -> None:
    """The one hint a headless run cannot act on for itself."""
    if "not logged in" in said.lower() or "/login" in said:
        print(f"[quickie] {login_hint}", file=sys.stderr, flush=True)


def _tail(lines: list[str], keep: int = 5) -> str:
    """The last few non-JSON stdout lines, joined — what the CLI said when it
    said it outside the event stream. Capped: a crash can spill a lot, and the
    point is the message, not the dump."""
    return " ".join(lines[-keep:])


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
