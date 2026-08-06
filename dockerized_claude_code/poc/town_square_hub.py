#!/usr/bin/env python3
"""PoC: the town_square hub — routes messages between running instances.

Puts the two verified mechanisms together into one service:

  delivery  pty injection into a live session (inject_poc) — full fidelity,
            because it is the instance's own launcher-started session
  capture   the {cowork} Stop hook, read out of each container's outbox

The hub is the only thing that needs docker. Callers do NOT: to send a prompt
you drop a text file into `<root>/requests/`, so a client with no docker access
(and no DooD) can drive the whole thing.

    # terminal 1 — the hub
    python3 poc/town_square_hub.py --instance feature-identifier__test_space

    # anywhere else — no docker needed, just a file
    printf 'to: feature-identifier__test_space\\nhow many .py files are here?\\n' \\
        > ~/.claude-agents/town_square/requests/ask.txt

Request file format: an optional `to: <instance>` first line (defaulted when only
one instance is tracked), then the prompt. Consumed files move to
`requests/done/`.

Both sides of every exchange are appended to `<root>/conversation.txt`.

Pair two instances with `--pair A B` and each one's reply is forwarded to the
other as its next prompt, bounded by `--rounds` — that is two agents talking.

Throwaway experiment — outside `launch/`, never imported by the launcher.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from inject_poc import inject
from live_agent import LAUNCHER_PREFIX, log, running_instances

DEFAULT_ROOT = Path.home() / ".claude-agents" / "town_square"
OUTBOX_IN_CONTAINER = "/home/claude/town_square/outbox"   # where the {cowork} Stop hook writes
POLL_SECONDS = 2.0
CONVERSATION_FILE = "conversation.txt"


def _exec(container: str, script: str) -> tuple[int, str]:
    """Run a shell snippet inside `container`; return (returncode, stdout)."""
    try:
        r = subprocess.run(["docker", "exec", container, "sh", "-c", script],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        log("ERROR", f"docker exec failed on {container}: {e}")
        return 1, ""
    return r.returncode, r.stdout


def drain_outbox(instance: str) -> list[dict[str, object]]:
    """Read and CONSUME every capture waiting in that instance's outbox.

    Deleting as we go gives at-most-once delivery and stops the directory
    growing one file per turn forever. Oldest first, so a burst of queued turns
    is routed in the order the agent answered them."""
    container = f"{LAUNCHER_PREFIX}{instance}"
    rc, listing = _exec(container, f"ls -1tr {OUTBOX_IN_CONTAINER} 2>/dev/null")
    if rc != 0 or not listing.strip():
        return []
    payloads: list[dict[str, object]] = []
    for name in listing.split():
        rc, body = _exec(container, f"cat {OUTBOX_IN_CONTAINER}/{name}")
        if rc == 0 and body.strip():
            try:
                payloads.append(json.loads(body))
            except json.JSONDecodeError:
                log("WARN", f"{instance}: unparseable capture {name}, dropping")
        _exec(container, f"rm -f {OUTBOX_IN_CONTAINER}/{name}")
    return payloads


class Conversation:
    """Append-only human-readable log of both sides, in `<root>/conversation.txt`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, arrow: str, who: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        body = "\n".join(f"    {line}" for line in text.strip().splitlines()) or "    (empty)"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {arrow} {who}\n{body}\n\n")


def parse_request(path: Path, default_to: str | None) -> tuple[str, str] | None:
    """(instance, prompt) from a request file, or None if it names no target."""
    text = path.read_text(encoding="utf-8", errors="replace")
    target, body = default_to, text
    first, _, rest = text.partition("\n")
    if first.lower().startswith("to:"):
        target, body = first[3:].strip(), rest
    prompt = body.strip()
    if not target or not prompt:
        log("WARN", f"{path.name}: needs a 'to: <instance>' line and a prompt — skipping")
        return None
    return target, prompt


def send(instance: str, prompt: str, convo: Conversation, live: set[str]) -> bool:
    """Inject one prompt, logging it as outbound. False if the instance is down."""
    if instance not in live:
        log("ERROR", f"{instance} is not running — cannot deliver")
        convo.write("!!", instance, f"UNDELIVERED (instance not running): {prompt}")
        return False
    convo.write("→", instance, prompt)
    return inject(f"{LAUNCHER_PREFIX}{instance}", prompt, enter_delay=0.4, watch=0.0) == 0


def serve(instances: list[str], pair: tuple[str, str] | None, root: Path,
          max_rounds: int) -> None:
    """Poll requests and outboxes until interrupted.

    One loop does both directions: inbound requests become injections, and
    captures become log entries plus (when paired) the peer's next prompt."""
    requests_dir = root / "requests"
    done_dir = requests_dir / "done"
    for d in (requests_dir, done_dir):
        d.mkdir(parents=True, exist_ok=True)
    convo = Conversation(root / CONVERSATION_FILE)
    default_to = instances[0] if len(instances) == 1 else None
    rounds = 0

    log("READY", f"watching {requests_dir} for *.txt")
    log("READY", f"logging both sides to {root / CONVERSATION_FILE}")
    log("READY", f"tracking: {', '.join(instances)}" + (f"  |  paired: {pair[0]} <-> {pair[1]}" if pair else ""))

    while True:
        live = running_instances()

        for path in sorted(requests_dir.glob("*.txt")):
            parsed = parse_request(path, default_to)
            path.rename(done_dir / path.name)          # consume either way
            if parsed is None:
                continue
            instance, prompt = parsed
            log("REQUEST", f"{path.name} -> {instance}")
            send(instance, prompt, convo, live)

        for instance in instances:
            for payload in drain_outbox(instance):
                answer = str(payload.get("last_assistant_message") or "").strip()
                if not answer:
                    continue
                log("CAPTURE", f"{instance}: {answer[:70]}")
                convo.write("←", instance, answer)
                if pair is None:
                    continue
                peer = pair[1] if instance == pair[0] else pair[0] if instance == pair[1] else None
                if peer is None:
                    continue
                if rounds >= max_rounds:
                    log("STOP", f"round cap {max_rounds} reached — not forwarding to {peer}")
                    convo.write("!!", peer, f"round cap {max_rounds} reached; not forwarded")
                    continue
                rounds += 1
                log("ROUTE", f"round {rounds}/{max_rounds}: {instance} -> {peer}")
                send(peer, f"Message from {instance}:\n\n{answer}", convo, live)

        time.sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="town_square_hub.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instance", action="append", default=[], metavar="NAME",
                        help="instance to watch; repeat for several")
    parser.add_argument("--pair", nargs=2, metavar=("A", "B"),
                        help="forward each of these two instances' replies to the other")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help=f"town_square directory (default: {DEFAULT_ROOT})")
    parser.add_argument("--rounds", type=int, default=6,
                        help="max forwarded messages before the hub stops relaying (default: 6)")
    args = parser.parse_args(argv)

    if shutil.which("docker") is None:
        print("error: `docker` not found on PATH — the hub needs it", file=sys.stderr)
        return 2
    instances = list(dict.fromkeys(args.instance + (list(args.pair) if args.pair else [])))
    if not instances:
        parser.error("give at least one --instance (or --pair A B)")

    live = running_instances()
    for name in instances:
        log("SETUP", f"{name}: {'running' if name in live else 'NOT RUNNING — requests to it will fail'}")

    try:
        serve(instances, (args.pair[0], args.pair[1]) if args.pair else None,
              args.root, args.rounds)
    except KeyboardInterrupt:
        print()
        log("STOP", "interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
