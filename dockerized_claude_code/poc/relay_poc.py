#!/usr/bin/env python3
"""PoC: two agents collaborating through a host-side relay.

Demonstrates the topology from `agent_cross_comm_propositions.md`: only the
process holding an agent's stdin can prompt it, so every message passes through
a host-side relay. There is no peer-to-peer path — the relay IS the bus.

    worker    ── reply ──►  relay  ── wrapped as a prompt ──►  reviewer
    reviewer  ── verdict ─►  relay  ── wrapped as a prompt ──►  worker
                             └── journals every hop to inboxes/<agent>/NNN.md

The message carrier is IN-BAND: an agent's turn-reply *is* the message, and the
relay routes it. Nothing is written by the agents themselves, so there is no
convention they can forget or malform. The work product travels separately, over
the shared workspace volume (worker read-write, reviewer read-only).

Usage
-----
Host-side (no containers, no docker needed — proves the relay logic):

    python3 poc/relay_poc.py --task "Create hello.py that prints Hello, then say READY."

Containerised (the real topology — needs the claude-agents:base image built):

    python3 poc/relay_poc.py --docker --task "..."

Add --rounds N to change the cap. The run ends when the reviewer says APPROVED
or the cap is hit — never on its own, which is the point of the cap.

Throwaway experiment — deliberately outside `launch/`, not imported by the
launcher.
"""

import argparse
import shutil
import sys
from pathlib import Path

from live_agent import LiveAgent, docker_command, host_command, log

DEFAULT_ROOT = Path("/tmp/agent_relay")
DEFAULT_TASK = ("Create a file greet.py containing a function greet(name) that returns "
                "a greeting string. Then briefly say what you did.")
APPROVAL_MARKER = "APPROVED"
DEFAULT_ROUNDS = 3

# The role briefings. In the real feature these become per-instance CLAUDE.md
# addenda (the launcher already installs those via install_latest_md). The line
# about replies being forwarded verbatim is what makes the in-band carrier work:
# peers have separate contexts and never saw each other's reasoning.
WORKER_BRIEF = """You are the WORKER in a two-agent collaboration.
Your peer is a REVIEWER who can read your files but cannot edit them.
The workspace is your current directory.
Your reply is forwarded verbatim to the reviewer, so make it self-contained:
say what you changed and name the files. Keep it under 100 words.
Task: {task}"""

REVIEWER_BRIEF = """You are the REVIEWER in a two-agent collaboration.
Your peer is a WORKER who just did some work in the shared workspace.
Your workspace is READ-ONLY: inspect files, but never try to edit them.
Read the actual files before judging — do not trust the worker's summary alone.
Your reply is forwarded verbatim to the worker, so make it self-contained.
If the work is correct and complete, reply with the single word {marker}
followed by one short sentence. Otherwise list precisely what must change.
The worker reported: {report}"""


class Relay:
    """Routes messages between peers and journals every hop.

    Sole writer of the journal — that is what buys the durable, replayable paper
    trail of a file-based mailbox without any agent-side convention."""

    def __init__(self, journal_root: Path) -> None:
        self.journal_root = journal_root
        self.hops = 0

    def route(self, sender: str, recipient: LiveAgent, text: str) -> str | None:
        """Journal a message, then wake the recipient with it."""
        self.hops += 1
        inbox = self.journal_root / "inboxes" / recipient.label
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{self.hops:03d}.md").write_text(
            f"# from: {sender}\n# to: {recipient.label}\n\n{text}\n", encoding="utf-8")
        log("RELAY", f"hop {self.hops}: {sender} → {recipient.label} (journalled)")
        return recipient.deliver(text)


def build_agents(root: Path, use_docker: bool, image: str) -> tuple[LiveAgent, LiveAgent]:
    """Spawn worker (read-write) and reviewer (read-only) on the same workspace.

    The read-only reviewer is the containerised half of what `{ro}` does in the
    launcher: the kernel refuses writes, so the reviewer cannot 'fix' what it is
    reviewing even if it tries."""
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    if use_docker:
        worker = LiveAgent("worker", docker_command(
            "relay_poc_worker", workspace, readonly=False,
            permission_mode="acceptEdits", image=image, state_dir=root / "state_worker"))
        reviewer = LiveAgent("reviewer", docker_command(
            "relay_poc_reviewer", workspace, readonly=True,
            permission_mode="acceptEdits", image=image, state_dir=root / "state_reviewer"))
        return worker, reviewer
    # Host-side: one shared directory, no kernel-level read-only enforcement
    # (the reviewer is only asked not to write). Proves the relay, not isolation.
    worker = LiveAgent("worker", host_command("acceptEdits"), cwd=workspace)
    reviewer = LiveAgent("reviewer", host_command("acceptEdits"), cwd=workspace)
    return worker, reviewer


def collaborate(worker: LiveAgent, reviewer: LiveAgent, relay: Relay,
                task: str, max_rounds: int) -> str:
    """Run worker→reviewer rounds until approval or the cap. Returns an outcome."""
    report = relay.route("relay", worker, WORKER_BRIEF.format(task=task))
    if report is None:
        return "worker failed on the opening task"

    for round_no in range(1, max_rounds + 1):
        log("ROUND", f"{round_no}/{max_rounds} — sending the worker's report to the reviewer")
        verdict = relay.route("worker", reviewer,
                              REVIEWER_BRIEF.format(marker=APPROVAL_MARKER, report=report))
        if verdict is None:
            return "reviewer failed"
        if APPROVAL_MARKER in verdict:
            return f"approved by the reviewer in round {round_no}"
        log("ROUND", f"{round_no}/{max_rounds} — changes requested, back to the worker")
        report = relay.route("reviewer", worker,
                             f"Your reviewer replied:\n\n{verdict}\n\n"
                             "Address the feedback, then briefly report what you changed.")
        if report is None:
            return "worker failed on a revision"
    return f"round cap ({max_rounds}) reached without approval"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="relay_poc.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docker", action="store_true",
                        help="run each agent in a container (the real topology) instead of host-side")
    parser.add_argument("--image", default="claude-agents:base", help="image for --docker")
    parser.add_argument("--task", default=DEFAULT_TASK, help="what the worker should do")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                        help=f"max worker↔reviewer rounds (default: {DEFAULT_ROUNDS})")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help=f"scratch root for workspace + journal (default: {DEFAULT_ROOT})")
    args = parser.parse_args(argv)

    needed = "docker" if args.docker else "claude"
    if shutil.which(needed) is None:
        print(f"error: `{needed}` not found on PATH", file=sys.stderr)
        return 2

    if args.docker:
        # Bind-mounting a host path that doesn't exist makes docker CREATE it as a
        # DIRECTORY — which would both break auth inside the container and litter
        # ~/.claude-agents with junk dirs. Fail loudly instead.
        agents_state = Path.home() / ".claude-agents"
        missing = [p for p in (agents_state / ".claude.json", agents_state / ".credentials.json")
                   if not p.is_file()]
        if missing:
            print("error: these OAuth files must exist before --docker "
                  "(docker would otherwise create them as directories):", file=sys.stderr)
            for p in missing:
                print(f"  {p}", file=sys.stderr)
            print("Launch any agent normally once to populate them.", file=sys.stderr)
            return 2

    args.root.mkdir(parents=True, exist_ok=True)
    log("SETUP", f"root      {args.root}")
    log("SETUP", f"workspace {args.root / 'workspace'}  (worker rw, reviewer {'ro' if args.docker else 'rw — host mode'})")
    log("SETUP", f"journal   {args.root / 'inboxes'}")

    worker, reviewer = build_agents(args.root, args.docker, args.image)
    relay = Relay(args.root)
    try:
        outcome = collaborate(worker, reviewer, relay, args.task, args.rounds)
        log("OUTCOME", outcome)
        log("OUTCOME", f"{relay.hops} message hops journalled under {args.root / 'inboxes'}")
        for agent in (worker, reviewer):
            if agent.session_id:
                log("OUTCOME", f"reattach {agent.label}:  claude --resume {agent.session_id}")
    except KeyboardInterrupt:
        print()
        log("STOP", "interrupted")
    finally:
        worker.close()
        reviewer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
