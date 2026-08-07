"""Multi-agent group hosting for `{cowork}` instances (launch/cowork).

A host-side hub lets several running instances collaborate: it delivers prompts
by injecting into a live session, reads each reply from the `{cowork}` Stop-hook
capture, routes messages between participants, and exchanges files through each
participant's `group_hosting/<instance-id>/` dir (bind-mounted at `/cowork`).

Layering: this package consumes the launcher's core (paths, file_access, tags,
agents_crud, docker_config) and is consumed by the `cowork.py` entry script. It
owns no docker-CLI calls of its own — every `docker` touchpoint lives in
`docker_config`, which is that module's stated invariant.

Modules:

  group     group identity and durable state — `session.json` per group,
            `hub.state.json` for the hub's own bookkeeping, and
            discovery-by-scan. The only writer of hub state.
  mailbox   the message plane — stage a message into a recipient's group dir and
            build the one-line pointer that announces it; read and attribute the
            Stop-hook captures coming back, pairing each reply to its prompt via
            `prompt_id` so a human-typed turn is never mistaken for a reply.
  journal   the per-group `conversation.md` — append-only, both directions, in
            the manager's group dir so it survives restarts and can be read back
            to recover a thread.
  sync      the file plane — one symmetric transfer in both directions. Every
            participant writes only `<group>/` and receives only into
            `<group>@<sender>/`, so no copy ever lands on a dir its owner is
            working in, and no transfer may leave the group-hosting tree.
  roster    discovery — which instances are cowork-capable, reachable, and how
            many groups they are already in, with the asker excluded. Also names
            running instances that would qualify after a relaunch, since "nobody
            is available" and "nobody is tagged yet" are different problems.
  relay     the hub loop and its routing policy — deliver and wake, drain every
            outbox, attribute each capture, log, hand files on, notify. Rounds are
            counted on hub-to-coworker sends only, and a manager's own reply is
            never forwarded (that would close a notify/reply loop). Orchestration
            only: every mechanism it uses belongs to a module above.
  control   the agent-facing channel — a `{manager}` writes a request file into
            its own `/cowork/control/`; the hub gates on the requester's TAGS
            (the only gate possible: any cowork instance can write there),
            dispatches roster / recruit / send / release / done, and answers
            with a reply file plus an injected pointer. Group-scoped verbs
            additionally require the requester to HOST the group.
  lifecycle the singleton guard — one hub per host, enforced with a
            liveness-checked pidfile, because two hubs draining the same outboxes
            would each consume about half the captures.
  cli       argparse front end and dispatch — `roster`, `recruit`, `send`,
            `status`, `serve`, `close`. The repo-root `cowork.py` is its entry
            point, so this package is never run directly.

See `group_hosting_plan.md` at the repo root for the full design, and
`agent_cross_comm_propositions.md` for the mechanism survey it rests on.
"""

from .cli import main

__all__ = ["main"]
