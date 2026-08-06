"""Shared plumbing for the cross-agent communication PoCs.

One long-lived `claude` process, parked on stdin, woken by writing a
newline-delimited JSON message to it. Extracted here because both
`wake_poc.py` (one agent, mailbox-driven) and `relay_poc.py` (two agents, relay
routed) need exactly the same wrapper.

Throwaway experiment code — deliberately outside `launch/`, never imported by
the launcher. See `agent_cross_comm_propositions.md` for the mechanism survey.
"""

import json
import queue
import subprocess
import threading
import time
from pathlib import Path

# Container-side paths, mirroring launch/paths.py. Duplicated rather than
# imported so these PoCs stay standalone (runnable from a checkout without the
# launcher's dependencies installed).
CONTAINER_HOME = "/home/claude"
# The launcher's per-launch container name prefix (docker_config.CONTAINER_NAME_PREFIX),
# duplicated rather than imported so these PoCs stay standalone.
LAUNCHER_PREFIX = "claude-code_"
CONTAINER_CONFIG = f"{CONTAINER_HOME}/.claude"
DEFAULT_IMAGE = "claude-agents:base"

# The flags that make a `claude` process a persistent, relay-drivable agent.
# Kept together because they are one mechanism: stream-json in/out is what lets
# the relay both deliver messages and detect turn completion.
STREAM_FLAGS = (
    "-p",
    "--input-format", "stream-json",
    "--output-format", "stream-json",
    "--verbose",
)

TURN_TIMEOUT = 300.0        # give up waiting for one turn's `result` event


def log(tag: str, msg: str) -> None:
    """Timestamped line so idle gaps and wake latency are visible in output."""
    print(f"[{time.strftime('%H:%M:%S')}] {tag:<10} {msg}", flush=True)


def encode_user_message(text: str) -> str:
    """One stream-json input line — the envelope a live agent accepts on stdin.

    This exact shape is what makes the mechanism work; a bare string is
    rejected. Defined once so the wire format has a single definition."""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }) + "\n"


def host_command(permission_mode: str) -> list[str]:
    """Argv for an agent running directly on the host (no container)."""
    return ["claude", *STREAM_FLAGS, "--permission-mode", permission_mode]


def running_containers() -> set[str]:
    """Names of the containers currently up, or an empty set if docker can't say.

    Used to refuse driving an instance the launcher is already running: two
    `claude` processes sharing one state dir interleave writes into the same
    transcript JSONL and corrupt it."""
    try:
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.strip() for line in r.stdout.splitlines() if line.strip()}


def running_instances() -> set[str]:
    """Instance ids the launcher currently has running — the same signal the
    picker uses to mark a row "(RUNNING)": list containers, then match the
    prefix in Python (docker's `--filter name=` is a substring match, so it
    would also catch an unrelated `my-claude-code_x`)."""
    return {name.removeprefix(LAUNCHER_PREFIX) for name in running_containers()
            if name.startswith(LAUNCHER_PREFIX)}


def docker_command(name: str, workspace: Path, *, readonly: bool,
                   permission_mode: str, image: str = DEFAULT_IMAGE,
                   state_dir: Path | None = None, resume: bool = False) -> list[str]:
    """Argv for an agent running in a container.

    `-i` WITHOUT `-t` is the crux: `-i` keeps stdin open as a pipe, while a TTY
    would reintroduce echo and CRLF mangling into the JSON stream. The image's
    ENTRYPOINT is `claude`, so the flags simply append.

    Mount layout mirrors the launcher's own (`set_container_mounts`): the whole
    state dir lands on `~/.claude` read-write, which is what carries the
    conversation transcript, memory, and CLAUDE.md — mounting only a subpath
    would leave the agent amnesiac. The credentials file nests inside that mount
    (docker resolves nested targets by depth, same as the launcher relies on),
    and is read-write because Claude Code refreshes the token in place.

    `resume` adds `--continue`, so the agent picks up that state dir's most
    recent conversation instead of starting a fresh one."""
    agents_state = Path.home() / ".claude-agents"
    mounts = ["-v", f"{workspace}:/workspace{':ro' if readonly else ''}"]
    if state_dir is not None:
        state_dir.mkdir(parents=True, exist_ok=True)
        mounts += ["-v", f"{state_dir}:{CONTAINER_CONFIG}"]
    mounts += [
        "-v", f"{agents_state / '.claude.json'}:{CONTAINER_HOME}/.claude.json",
        "-v", f"{agents_state / '.credentials.json'}:{CONTAINER_CONFIG}/.credentials.json",
    ]
    return [
        "docker", "run", "-i", "--rm", "--name", name,
        *mounts, image,
        *STREAM_FLAGS, "--permission-mode", permission_mode,
        *(["--continue"] if resume else []),
    ]


class LiveAgent:
    """A single long-lived agent process that stays parked on stdin.

    Binds the three moving parts — the child process, the reader thread draining
    its stdout, and the turn-completion queue — to one lifetime, so `close()`
    cannot forget one of them.
    """

    def __init__(self, label: str, argv: list[str], cwd: Path | None = None) -> None:
        self.label = label
        self.session_id: str | None = None
        self._turns: queue.Queue[str] = queue.Queue()
        log("SPAWN", f"{label}: {' '.join(argv)}")
        self.proc = subprocess.Popen(
            argv, cwd=str(cwd) if cwd else None,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        log("SPAWN", f"{label}: alive as pid {self.proc.pid} — this pid must never change")

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def _drain_stdout(self) -> None:
        """Parse the event stream; a `result` event ends one turn.

        Non-JSON lines are skipped rather than fatal — the stream is a debugging
        surface and one odd line shouldn't kill the experiment."""
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if self.session_id is None and event.get("session_id"):
                self.session_id = str(event["session_id"])
            if event.get("type") == "result":
                self._turns.put(str(event.get("result", "")))

    def _drain_stderr(self) -> None:
        """Surface auth/startup failures instead of letting the caller hang."""
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            if line.strip():
                log("AGENT-ERR", f"{self.label}: {line.rstrip()}")

    def deliver(self, text: str) -> str | None:
        """Wake this parked agent with one message; return its reply text.

        None means the agent died or the turn exceeded TURN_TIMEOUT."""
        if not self.alive:
            log("ERROR", f"{self.label}: exited (code {self.proc.returncode}) — cannot deliver")
            return None
        assert self.proc.stdin is not None
        started = time.monotonic()
        log("WAKE →", f"{self.label} (pid {self.proc.pid}): {text[:64]}")
        try:
            self.proc.stdin.write(encode_user_message(text))
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            log("ERROR", f"{self.label}: stdin closed — agent is gone")
            return None
        try:
            reply = self._turns.get(timeout=TURN_TIMEOUT)
        except queue.Empty:
            log("ERROR", f"{self.label}: no result within {TURN_TIMEOUT:.0f}s")
            return None
        log("TURN ✓", f"{self.label} replied in {time.monotonic() - started:.1f}s: {reply[:64]}")
        return reply

    def close(self) -> None:
        if self.alive:
            try:
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
            except (BrokenPipeError, ValueError):
                pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
