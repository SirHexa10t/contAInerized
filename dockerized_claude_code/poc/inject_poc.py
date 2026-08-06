#!/usr/bin/env python3
"""PoC: prompt an ALREADY-RUNNING instance by pty injection (option 2).

The counterpart to `wake_poc.py`. That script LAUNCHES a headless agent it owns
the stdin of; this one talks to an instance you already have open in a terminal,
by typing into its terminal for you. The prompt and the reply appear in that
instance's own Claude Code UI, which is the only way to get a visible
conversation — the headless stream-json mode has no UI at all.

    python3 poc/inject_poc.py --instance strict-reviewer__dockerized_claude_code \\
                              --prompt "how many files are in the project?"

Why `docker attach`: the launcher starts instances with `docker run -it`, so
attaching connects to that TTY and bytes written land in claude's input exactly
as if typed. The alternatives do not work — writing to `/proc/<pid>/fd/0` writes
to the terminal's OUTPUT side rather than its input queue, and the `TIOCSTI`
ioctl that would inject into the input queue is disabled by default on modern
kernels. `tmux send-keys` would be cleaner but needs the session to have been
STARTED under tmux, which cannot be retrofitted onto a running container.

Because this rides the instance's normal launch, it is full fidelity for free —
engine/model, tags, permissions, memory, instructions, network policy, and image
layers are all whatever the launcher gave it. That is the one thing `wake_poc.py`
cannot offer.

UNSUPPORTED, and the trade-offs are real:

  * Claude Code exposes no injection channel; this impersonates a keyboard, so a
    future TUI change can break it.
  * The reply is NOT parseable here. Output is a shared TTY stream of ANSI
    redraws with no turn-complete marker, so read the answer in the instance's
    own window. `--watch` dumps the raw stream only as a smoke signal.
  * You share the input stream with the human at that terminal. If they are
    mid-keystroke your text interleaves with theirs.
  * A prompt starting with `/`, `!`, or `@` is interpreted by the TUI as a
    slash-command, bash mode, or file mention rather than plain text.

Throwaway experiment — outside `launch/`, never imported by the launcher.
"""

import argparse
import fcntl
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
import tty

from live_agent import LAUNCHER_PREFIX, log, running_instances

ENTER = "\r"                 # what the TUI reads as Enter (as in the verified pty test)
DEFAULT_ENTER_DELAY = 0.4    # settle time between the text and Enter, so the TUI sees a full line
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][A-Z0-9]|\x1b[=>]|\r")


def find_container(instance: str) -> str:
    """The running container for `instance`, or exit with what IS running.

    Uses the same signal the picker uses to mark a row "(RUNNING)" — hence the
    inverse guard to wake_poc.py: that script refuses when the instance is up,
    this one refuses when it is down."""
    live = running_instances()
    if instance in live:
        return f"{LAUNCHER_PREFIX}{instance}"
    known = "\n  ".join(sorted(live)) or "(none — no agent containers are up)"
    raise SystemExit(f"error: instance '{instance}' is not running.\n"
                     f"pty injection needs a live session to type into; "
                     f"use wake_poc.py to drive a stopped instance.\n"
                     f"currently running:\n  {known}")


def container_tty_size(container: str) -> tuple[int, int] | None:
    """(rows, cols) of the container's OWN terminal, or None if unknowable.

    Read fresh on every injection, because the human may have resized or moved
    their window since the last prompt — a stale size is exactly what blanks the
    TUI. pid 1 in the container is `claude` (the image's ENTRYPOINT), so its fd 0
    is the pty we are about to attach to; `stty size` reports that pty's winsize.
    Run without `-t` so the exec doesn't allocate a pty of its own."""
    try:
        r = subprocess.run(["docker", "exec", container, "sh", "-c", "stty size < /proc/1/fd/0"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    parts = r.stdout.split()
    if r.returncode != 0 or len(parts) != 2:
        return None
    try:
        rows, cols = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return (rows, cols) if rows > 0 and cols > 0 else None


def _fallback_tty_size() -> tuple[int, int]:
    """Our own terminal's size, else a conventional 24x80."""
    try:
        size = os.get_terminal_size()
        return size.lines, size.columns
    except OSError:
        return 24, 80


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Stamp a window size onto our pty.

    `docker attach` PROPAGATES the client terminal's size to the container, so
    without this the pty's default (often 0x0) resizes the agent's terminal and
    the TUI redraws into nothing until the human resizes and triggers SIGWINCH.
    Matching the container's current size makes the propagated resize a no-op."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _drain(master: int, seconds: float) -> str:
    """Read whatever the attach stream emits for `seconds`, as text."""
    chunks: list[bytes] = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([master], [], [], 0.2)
        if not ready:
            continue
        try:
            data = os.read(master, 65536)
        except OSError:                  # slave side closed — nothing more coming
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks).decode(errors="replace")


def inject(container: str, prompt: str, *, enter_delay: float, watch: float) -> int:
    """Type `prompt` into the container's TTY via `docker attach`, then detach.

    The attach runs against a pty we allocate, not a plain pipe: the launcher
    starts instances with `-t`, and the docker CLI refuses to attach a non-TTY
    stdin to a TTY container (it exits immediately, and the first write then dies
    with EPIPE). The pty makes our side a terminal, which is also what the
    original injection test used. Bytes go out with os.write so no TextIOWrapper
    is left holding unflushed data at interpreter shutdown.

    `--sig-proxy=false` keeps our Ctrl-C out of the agent's session, and we
    terminate the attach rather than closing its stdin — with a TTY the other
    attachers keep the master open, so claude should never see an EOF."""
    size = container_tty_size(container)
    if size is None:
        size = _fallback_tty_size()
        log("WARN", f"could not read the container's tty size; using {size[0]}x{size[1]} — "
                    "if the window blanks, that mismatch is why")
    else:
        log("SIZE", f"container terminal is {size[0]}x{size[1]}; matching it so attach won't resize")
    master, slave = pty.openpty()
    _set_winsize(master, *size)
    # Raw mode so the terminal's line discipline leaves our bytes alone: without
    # it ICRNL rewrites the Enter (\r -> \n) and ECHO bounces the prompt back
    # into the output we read. Real docker sets raw itself, but doing it here
    # makes the injection deterministic either way.
    tty.setraw(master)
    argv = ["docker", "attach", "--sig-proxy=false", container]
    log("ATTACH", " ".join(argv))
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)                      # the child owns it now
    try:
        # If docker rejects the attach it dies within moments; its complaint
        # arrives on the pty, so surface that instead of a bare EPIPE.
        early = _drain(master, 1.0)
        if proc.poll() is not None:
            detail = ANSI_RE.sub("", early).strip() or "(docker printed nothing)"
            log("ERROR", f"docker attach exited with code {proc.returncode}: {detail}")
            return 1

        log("INJECT", f"typing {len(prompt)} chars into {container}")
        os.write(master, prompt.encode())
        time.sleep(enter_delay)          # let the TUI register the line before Enter
        os.write(master, ENTER.encode())
        log("INJECT", "Enter sent — the prompt should now appear in that instance's window")

        if watch > 0:
            log("WATCH", f"raw TTY output for {watch:.0f}s (ANSI stripped; not the real answer)")
            for line in _drain(master, watch).splitlines():
                cleaned = ANSI_RE.sub("", line).strip()
                if cleaned:
                    print(f"    │ {cleaned[:160]}", flush=True)
    except OSError as e:
        log("ERROR", f"could not write to the attach stream: {e}")
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.close(master)
    log("DONE", "detached; the instance keeps running")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="inject_poc.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instance", required=True, metavar="NAME",
                        help="a RUNNING instance id (e.g. strict-reviewer__dockerized_claude_code)")
    parser.add_argument("--prompt", required=True, metavar="TEXT",
                        help="the text to type into that instance's session")
    parser.add_argument("--watch", type=float, default=0.0, metavar="SECONDS",
                        help="after injecting, dump the raw TTY stream this long as a smoke signal "
                             "(default 0 — read the answer in the instance's own window)")
    parser.add_argument("--enter-delay", type=float, default=DEFAULT_ENTER_DELAY,
                        metavar="SECONDS", help=f"pause between text and Enter (default {DEFAULT_ENTER_DELAY})")
    args = parser.parse_args(argv)

    if shutil.which("docker") is None:
        print("error: `docker` not found on PATH", file=sys.stderr)
        return 2
    if args.prompt.startswith(("/", "!", "@")):
        log("WARN", f"prompt starts with {args.prompt[0]!r} — the TUI will read it as a "
                    "command/mention, not plain text")

    container = find_container(args.instance)
    log("SETUP", f"instance  {args.instance} (running as {container})")
    log("SETUP", "note      full fidelity — this is the instance's own session, tags and all")
    return inject(container, args.prompt, enter_delay=args.enter_delay, watch=args.watch)


if __name__ == "__main__":
    sys.exit(main())
