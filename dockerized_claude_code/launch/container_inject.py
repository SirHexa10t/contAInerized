"""Typing a prompt into a live session's TTY — the group-hosting doorbell.

The one thing `{cowork}` needs docker for beyond launching: waking a running
instance. Every byte of cowork DATA moves as ordinary files through the
per-participant mount; this module only rings the bell.

It has to be a pty rather than a pipe. The launcher starts instances with
`-t`, and the docker CLI refuses to attach non-TTY stdin to a TTY container —
it exits at once, and the first write then dies with EPIPE. Two further
details were each found the hard way and are load-bearing, so they are
commented where they happen: raw mode (in `docker_attach_inject`) and window
size (`_match_container_winsize`).

Split out of `docker_config` 2026-09-03. It was never orchestration: nothing
in the launch path calls it, and `cowork/control.py` + `cowork/relay.py`
importing one function used to drag the whole 848-line build-and-run module —
and its eight tty/pty stdlib imports, which only this code ever used — into
the group-hosting layer. Nothing else in the tree writes to a container's
terminal, so `container_probe` (read one live container) and this (write to
one) are the pair.
"""

import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import termios
import time
import tty

from .container_probe import CONTAINER_NAME_PREFIX, container_tty_size

ENTER_KEY = "\r"                    # what the TUI reads as Enter; "\n" is swallowed by the input widget
INJECT_ENTER_DELAY = 0.4            # settle time between the text and Enter, so the TUI registers a full line
INJECT_ATTACH_PROBE = 1.0           # how long to watch for docker refusing the attach before typing
FALLBACK_TTY_SIZE = (24, 80)        # conventional terminal, used only when the container's size is unreadable
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][A-Z0-9]|\x1b[=>]|\r")


def docker_attach_inject(instance: str, prompt: str,
                         *, enter_delay: float = INJECT_ENTER_DELAY) -> bool:
    """Type `prompt` into `instance`'s live session and press Enter. True if it
    landed.

    Deliberately no liveness pre-check: callers polling several participants
    already hold a `docker_running_instances_subprocess` snapshot, and a second
    probe per injection would buy nothing — attaching to a container that is gone
    fails cleanly and reports docker's own complaint.

    `--sig-proxy=false` keeps a Ctrl-C in the hub's terminal out of the agent's
    session. The attach is terminated rather than having its stdin closed: with a
    TTY the other attachers hold the master open, so `claude` never sees an EOF
    and the human's own session is left untouched.

    A single line only — `prompt` is TYPED, so an embedded newline reads as Enter
    and submits a fragment. Callers send a pointer to a file for anything longer
    (see cowork.mailbox.pointer_prompt)."""
    container = f"{CONTAINER_NAME_PREFIX}{instance}"
    master, slave = pty.openpty()
    _match_container_winsize(master, container)
    # Raw mode so the line discipline leaves our bytes alone: without it ICRNL
    # rewrites the Enter (\r -> \n) and ECHO bounces the prompt back into the
    # stream we read. Real docker sets raw itself; doing it here makes the
    # injection behave the same whether or not it gets that far.
    tty.setraw(master)
    proc = subprocess.Popen(["docker", "attach", "--sig-proxy=false", container],
                            stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)                          # the child owns it now
    try:
        # A rejected attach dies within moments and complains onto the pty, so
        # surface that rather than letting the first write fail with a bare EPIPE.
        early = _drain_pty(master, INJECT_ATTACH_PROBE)
        if proc.poll() is not None:
            detail = _ANSI_RE.sub("", early).strip() or "(docker printed nothing)"
            print(f"  Injection into '{instance}' failed: {detail}")
            return False
        os.write(master, prompt.encode())
        time.sleep(enter_delay)              # let the TUI register the line before Enter
        os.write(master, ENTER_KEY.encode())
        return True
    except OSError as e:
        print(f"  Injection into '{instance}' failed writing to the attach stream: {e}")
        return False
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.close(master)


def _match_container_winsize(fd: int, container: str) -> None:
    """Stamp the container's current window size onto our pty.

    `docker attach` PROPAGATES the client terminal's size to the container, so
    without this the pty's default (often 0x0) resizes the agent's terminal and
    its TUI redraws into nothing until the human resizes and triggers SIGWINCH.
    Matching the container's size makes the propagated resize a no-op.

    Read fresh on every injection, because the human may have resized or moved
    their window since the last prompt — a stale size is exactly what blanks the
    display."""
    size = container_tty_size(container) or FALLBACK_TTY_SIZE
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))


def _drain_pty(master: int, seconds: float) -> str:
    """Read whatever the attach stream emits for `seconds`, as text. Used to
    catch docker's own refusal before anything is typed."""
    chunks: list[bytes] = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([master], [], [], 0.2)
        if not ready:
            continue
        try:
            data = os.read(master, 65536)
        except OSError:                      # slave side closed — nothing more coming
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks).decode(errors="replace")
