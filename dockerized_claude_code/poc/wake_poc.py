#!/usr/bin/env python3
"""PoC: wake a LIVE agent process by messaging it (option 1 of the survey).

Proves the mechanism the cross-instance collaboration feature would rest on:
ONE long-lived `claude` process, parked on stdin, woken by an external "daemon"
that writes to it. No new process per turn, no pty tricks, no TUI scraping.

    agent process:  claude -p --input-format stream-json --output-format stream-json
    the daemon:     this script
    the wake:       one newline-delimited JSON message written to the live stdin

See `agent_cross_comm_propositions.md` for the mechanism survey. Throwaway
experiment — lives outside `launch/` on purpose, never imported by the launcher.

Usage
-----
Prompt an existing instance by name. Runs IN A CONTAINER (no `claude` on the
host), mounts that instance's real workspace and real state dir, and passes
--continue — so the prompt lands in that agent's own conversation and it still
has its memory:

    python3 poc/wake_poc.py --instance refactorer__dockerized_claude_code \\
                            --prompt "how old is the moon?"

Add --host to run on the host instead (needs `claude` on PATH, no sandbox).

Scripted self-demo (three prompts with idle gaps, proves the pid never changes):

    python3 poc/wake_poc.py --demo

Mailbox mode — park an agent and feed it by hand from any other shell. This is
the "a human sends manual prompts through the hub" case: the daemon owns the
pipe, so your prompt reaches a live agent without a TTY of its own.

    python3 poc/wake_poc.py
    # elsewhere:
    echo 'What is 2+2? Answer with just the number.' > /tmp/agent_mailbox/01.txt

Requires `docker` plus the built image (or `claude` on PATH with --host), and
working credentials. Ctrl-C to stop.

IMPORTANT — this LAUNCHES a container for the instance; it does not attach to a
session already running in a terminal. There is no supported way to inject a
prompt into a live interactive `claude`, so an instance can only be driven this
way if it is not already up. The script refuses when the launcher's own
container for that instance is running, because two agents sharing one state dir
interleave writes and corrupt the transcript.

Caution: with --instance the agent writes to that instance's REAL history and
memory (that is the point), and works in its REAL workspace. Use --readonly to
mount the workspace read-only when you only want to ask it something.
"""

import argparse
import shutil
import sys
import time
import tomllib
from pathlib import Path

from live_agent import (
    LAUNCHER_PREFIX, LiveAgent, docker_command, host_command, log, running_instances,
)

AGENTS_STATE = Path.home() / ".claude-agents"
INSTANCES_FILE = AGENTS_STATE / "instances.toml"
INSTANCES_DIR = AGENTS_STATE / "instances"
# Scratch history for the no---instance case; a real instance uses its OWN state dir.
SCRATCH_STATE_ROOT = AGENTS_STATE / "town_square" / "poc_sessions"

DEFAULT_MAILBOX = Path("/tmp/agent_mailbox")
POLL_SECONDS = 0.4          # mailbox scan interval
HEARTBEAT_SECONDS = 5.0     # how often to report "still idle, still parked"
DEMO_GAP_SECONDS = 6.0      # idle gap between demo messages, to show parking

DEMO_MESSAGES = (
    "Reply with exactly: FIRST_WAKE",
    "Use the Write tool to create wake_poc_proof.txt containing PEER_OK, then say WROTE_IT.",
    "Without using any tools, what filename did you create a moment ago?",
)


def instance_paths(name: str) -> tuple[Path, Path]:
    """That instance's (workspace, state_dir) — its real ones.

    The state dir is what makes a prompt a CONTINUATION: mounted at
    `~/.claude` it carries the transcript, memory, and CLAUDE.md, so the agent
    resumes the conversation and remembers what it knows.

    Parsed with stdlib tomllib rather than importing the launcher, so this PoC
    keeps running from a bare checkout with no dependencies installed. Exits
    with the known instance names when the lookup fails — a typo in a long
    `<agent>__<session>` id is the likeliest way to get here."""
    if not INSTANCES_FILE.is_file():
        raise SystemExit(f"error: {INSTANCES_FILE} not found — launch an agent normally first")
    entries = tomllib.loads(INSTANCES_FILE.read_text(encoding="utf-8"))
    entry = entries.get(name)
    if entry is None:
        known = "\n  ".join(sorted(entries)) or "(none)"
        raise SystemExit(f"error: no instance '{name}' in {INSTANCES_FILE}\nknown instances:\n  {known}")
    workspace = entry.get("workspace")
    if not workspace or not Path(workspace).is_dir():
        raise SystemExit(f"error: instance '{name}' has no usable workspace: {workspace!r}")
    state_dir = INSTANCES_DIR / name
    if not state_dir.is_dir():
        raise SystemExit(f"error: instance '{name}' has no state dir at {state_dir} — "
                         "launch it normally once so its history exists")
    return Path(workspace), state_dir


def wait_for_message(mailbox: Path, agent: LiveAgent) -> str | None:
    """Block until a *.txt appears in the mailbox; return its text.

    Stands in for the real hub (a queue consumer, or a peer agent). Consumed
    files move to `done/` so each prompt fires once. None if the agent dies."""
    done_dir = mailbox / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    idle_since = time.monotonic()
    next_beat = idle_since + HEARTBEAT_SECONDS
    while True:
        if not agent.alive:
            return None
        for path in sorted(mailbox.glob("*.txt")):
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            shutil.move(str(path), str(done_dir / path.name))
            if text:
                log("MAILBOX", f"picked up {path.name} after {time.monotonic() - idle_since:.1f}s idle")
                return text
        now = time.monotonic()
        if now >= next_beat:
            log("IDLE ⏸", f"pid {agent.proc.pid} parked on stdin, {now - idle_since:.0f}s — nothing to do")
            next_beat = now + HEARTBEAT_SECONDS
        time.sleep(POLL_SECONDS)


def run_one_shot(agent: LiveAgent, prompt: str) -> None:
    """Deliver a single prompt and print the reply — the --prompt path."""
    reply = agent.deliver(prompt)
    if reply is not None:
        print()
        print(reply)


def run_demo(agent: LiveAgent) -> None:
    """Drive the agent unattended so the mechanism is visible in one command."""
    for i, text in enumerate(DEMO_MESSAGES, start=1):
        log("DEMO", f"message {i}/{len(DEMO_MESSAGES)} — simulating a peer sending work")
        if agent.deliver(text) is None:
            return
        if i < len(DEMO_MESSAGES):
            log("IDLE ⏸", f"sleeping {DEMO_GAP_SECONDS:.0f}s — agent stays parked, pid unchanged")
            time.sleep(DEMO_GAP_SECONDS)


def run_mailbox(agent: LiveAgent, mailbox: Path) -> None:
    """Park the agent and serve whatever anyone drops in the mailbox."""
    log("READY", f"drop prompts as *.txt into {mailbox}")
    log("READY", f"e.g.  echo 'Say hello' > {mailbox / '01.txt'}")
    while True:
        text = wait_for_message(mailbox, agent)
        if text is None:
            log("ERROR", "agent is no longer running — stopping")
            return
        if agent.deliver(text) is None:
            return


def spawn(label: str, workspace: Path, state_dir: Path, *, on_host: bool, image: str,
          permission_mode: str, readonly: bool, resume: bool) -> LiveAgent:
    """One persistent agent on `workspace`, containerised (default) or host-side.

    `readonly` is container-only — it is the kernel refusing writes to the
    mount, which has no host-side equivalent (there the workspace is merely the
    process's cwd, not a mount)."""
    if on_host:
        return LiveAgent(label, host_command(permission_mode), cwd=workspace)
    return LiveAgent(label, docker_command(
        f"wake_poc_{label}", workspace, readonly=readonly,
        permission_mode=permission_mode, image=image,
        state_dir=state_dir, resume=resume))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wake_poc.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--instance", metavar="NAME",
                        help="existing instance id (e.g. refactorer__dockerized_claude_code); "
                             "its workspace is read from instances.toml")
    parser.add_argument("--prompt", metavar="TEXT",
                        help="send this one prompt, print the reply, exit")
    parser.add_argument("--demo", action="store_true",
                        help="send a scripted sequence with idle gaps instead of watching the mailbox")
    parser.add_argument("--host", action="store_true",
                        help="run the agent directly on the host instead of in a container "
                             "(needs `claude` on PATH; no sandbox)")
    parser.add_argument("--image", default="claude-agents:base", help="image for --docker")
    parser.add_argument("--readonly", action="store_true",
                        help="mount the workspace read-only (container mode only) — the kernel "
                             "refuses writes, so a peer editing the same tree can't be clobbered")
    parser.add_argument("--mailbox", type=Path, default=DEFAULT_MAILBOX,
                        help=f"directory watched for *.txt prompts (default: {DEFAULT_MAILBOX})")
    parser.add_argument("--workdir", type=Path, default=None,
                        help="workspace for the agent; ignored when --instance is given")
    parser.add_argument("--permission-mode", default="acceptEdits",
                        help="claude --permission-mode value; acceptEdits lets the agent write files")
    args = parser.parse_args(argv)

    if args.prompt and args.demo:
        parser.error("--prompt and --demo are different modes; pick one")
    if args.readonly and args.host:
        parser.error("--readonly is container-only; host mode has no mount to make read-only")
    needed = "claude" if args.host else "docker"
    if shutil.which(needed) is None:
        print(f"error: `{needed}` not found on PATH", file=sys.stderr)
        return 2
    if not args.host:
        # A bind-mount of a missing host file makes docker CREATE it as a
        # directory, which breaks auth and litters ~/.claude-agents. Fail loudly.
        missing = [p for p in (AGENTS_STATE / ".claude.json", AGENTS_STATE / ".credentials.json")
                   if not p.is_file()]
        if missing:
            print("error: these OAuth files must exist before --docker "
                  "(docker would otherwise create them as directories):", file=sys.stderr)
            for p in missing:
                print(f"  {p}", file=sys.stderr)
            return 2

    args.mailbox.mkdir(parents=True, exist_ok=True)
    resume = False
    if args.instance:
        label = args.instance
        workspace, state_dir = instance_paths(label)
        resume = True                      # continue that instance's own conversation
        # Two claude processes sharing one state dir interleave writes into the
        # same transcript and corrupt it, so never drive an instance the
        # launcher is already running.
        if label in running_instances():
            raise SystemExit(
                f"error: '{label}' is already running under the launcher "
                f"(container {LAUNCHER_PREFIX}{label}).\n"
                "Two agents on one state dir corrupt the transcript — stop it first.")
        log("SETUP", f"instance  {label}  (continuing its own conversation + memory)")
        log("SETUP", f"state     {state_dir} -> ~/.claude")
    else:
        label = "agent"
        workspace = args.workdir or (args.mailbox / "workspace")
        state_dir = SCRATCH_STATE_ROOT / label
    workspace.mkdir(parents=True, exist_ok=True)
    log("SETUP", f"workspace {workspace}{' (read-only)' if args.readonly else ''}")
    if not args.prompt and not args.demo:
        log("SETUP", f"mailbox   {args.mailbox}")

    agent = spawn(label, workspace, state_dir, on_host=args.host, image=args.image,
                  permission_mode=args.permission_mode, readonly=args.readonly,
                  resume=resume)
    try:
        if args.prompt:
            run_one_shot(agent, args.prompt)
        elif args.demo:
            run_demo(agent)
        else:
            run_mailbox(agent, args.mailbox)
    except KeyboardInterrupt:
        print()
        log("STOP", "interrupted")
    finally:
        if agent.session_id:
            log("RESULT", f"reattach later with:  claude --resume {agent.session_id}")
        agent.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
