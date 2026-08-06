# Cross-instance agent communication — mechanism survey

Design notes for a prospective feature: letting several running agent instances
collaborate (e.g. a worker, a reviewer, a manager) instead of working alone.

This file surveys **how one process can hand a prompt to another running agent**
and wake it up. It is a decision record, not a plan — nothing here is
implemented. The PoC for the recommended option lives in `poc/wake_poc.py`.

Verified against **Claude Code 2.1.215**. Findings are tagged:

- **[tested]** — empirically confirmed by running the mechanism.
- **[docs]** — quoted from official Claude Code documentation.
- **[open]** — neither; needs verification before being relied on.

Re-verify after a Claude Code upgrade: three of these mechanisms are
undocumented or only partly documented, so they can move without notice.

---

## The actual problem

Transport is easy — containers can share a bind-mounted directory or a unix
socket (the `{dood}` specialty already bind-mounts `docker.sock`, so the pattern
is established and needs no network, which matters because `{firewall}` blocks
egress by default).

The hard part is the **last inch**: getting a delivered message into an agent
that is already running, and making it *act*. A Claude Code session is
turn-based — it is a process blocked waiting for input, not an event loop. So
"the worker writes a file and the reviewer notices" does not happen on its own.
Something has to originate a turn.

That is the axis the table below sorts on: **which mechanisms can originate a
turn**, versus which only move bytes.

---

## Options

| # | Mechanism | Where the instructions-giver sits | Means of delivery | Agent receives it as | Wakes an idle agent? | Status |
|---|-----------|-----------------------------------|-------------------|----------------------|----------------------|--------|
| 1 | **Persistent `--input-format stream-json` process** ⭐ | Any process holding the agent process's stdin — a daemon, a queue consumer, or a peer agent | Write one newline-delimited JSON message to the **live process's stdin** | A normal user turn | **Effectively yes** — the process is permanently parked on stdin; feeding it *is* the wake | **[tested]** Supported; multi-turn, tool use, `--resume`, and a control protocol all confirmed |
| 2 | pty injection (tmux `send-keys` style) | A daemon writing into the container's pty | Writes prompt bytes + Enter into the running session's controlling terminal, as if a human typed | A typed user turn | **Yes** | **[tested]** Works, but **unsupported** — requires scraping the TUI for output |
| 3 | `Stop` hook returning `decision: "block"` | A peer writes a mailbox file; the hook reads it | Hook returns `block` + `reason` when the agent tries to end its turn | Hook feedback that continues the current turn | **Only at a turn boundary** — a session already sitting idle never fires it | **[docs]** *"Prevents Claude from stopping, continues the conversation."* |
| 4 | Channels (`--channels`) | A local channel — an MCP server you author, spawned as a stdio subprocess | Server emits `notifications/claude/channel` with `{content, meta}` | Structured `<channel source="…">` event with metadata | **[open]** — undocumented either way | **[docs]** for payload/transport; idle-wake unverified |
| 5 | Host orchestrator → one-shot `claude -p` per message | A host-side loop | A fresh process per message; `--continue` resumes the thread | A normal user turn | N/A — no live session exists between turns | **[tested]** Supported (this is how `quick_question.py` already works) |
| 6 | MCP tool the agent pulls (`read_inbox`) | Nobody pushes | The agent calls the tool itself, mid-turn | A tool result | **No** | **[docs]** Transport only — MCP is pull-model |
| 7 | Shared mailbox files, read at turn start | Any writer to the shared volume | The agent reads the mailbox when it next takes a turn | File contents it chose to read | **No** | Transport only |
| 8 | `FileChanged` hook | A peer writes a watched file | Hook fires with `file_path` + `change_type` | **Nothing** | **No** | **[docs]** Exists, but *"no decision control. Exit code and output are ignored."* |
| 9 | Agent SDK harness | Your own Python/TS process | Programmatic streaming input into a hosted loop | A stream message | **Yes** | **[docs]** Supported, but option 1 provides the same thing without leaving the CLI |

### What does *not* exist

Ruled out by documentation, so no design should assume them:

- No built-in cross-process messaging between separate `claude` instances.
  **Agent Teams** is the closest feature, but its mailboxes
  (`~/.claude/teams/<team>/inboxes/<agent>.json`) are single-machine and scoped
  to one lead session — its *layout* is worth copying, the mechanism is not
  reusable across containers.
- No socket, pipe, or control file for injecting a prompt into a running
  **interactive** session (which is why option 2 has to impersonate a keyboard).
- No hook that can spontaneously originate a turn. Option 3 is the only
  hook-based continuation, and it only fires when a turn *ends*.

---

## Why option 1 is the recommendation

It is the only mechanism that is simultaneously supported, structured, and
capable of waking an agent — and it matches the intuition that a daemon should
just message a session that is already running. One long-lived process per
agent, parked on stdin, holding its own conversation.

Confirmed by test:

- **One process, many messages.** Two consecutive turns served by a single
  process, still alive after both. The CLI calls this *"realtime streaming
  input"*.
- **It is a real agent, not an echo.** It used the `Write` tool to create a file
  with exact contents, then recalled what it had done on the following turn.
- **Sessions are attachable.** A *fresh* persistent process launched with
  `--resume <session_id>` recalled work from the earlier process — so a
  collaboration session can be picked up later, including by a human.
- **A control protocol exists** alongside user messages:
  `control_request` / `control_response` / `control_cancel_request`,
  `interrupt`, `can_use_tool`, `set_permission_mode`, `hook_callback`.

Two consequences worth designing around:

- **`can_use_tool` routes permission decisions out over the stream**, which
  makes "the reviewer approves the worker's writes" an enforced boundary rather
  than a convention.
- **`interrupt` can cancel a peer mid-turn** — no other option here offers that.

Relevant flags: `--permission-mode` (`acceptEdits`, `auto`, `bypassPermissions`,
`manual`, `dontAsk`, `plan`) lets a worker edit without the blanket bypass that
`{auto}` grants; `--replay-user-messages` echoes delivered messages back on
stdout as a delivery acknowledgement.

### Costs and open questions

- **The instance is headless.** No human-attachable TUI while it runs — the only
  reason options 2 and 4 remain interesting. Mitigated by `--resume`.
- **Container plumbing needs stdin as a pipe.** `docker_stream_subprocess`
  currently passes `stdin=DEVNULL`; a persistent agent needs `PIPE`.
- **[open] The `control_request` wire format is undocumented.** Interrupt and
  `can_use_tool` would need to be reverse-engineered against the Agent SDK
  before either can be depended on.
- **[open] Long-run behaviour.** Context growth and compaction across a long
  collaboration are untested.
- **[open] Concurrency.** Two processes must never drive one session's history —
  interleaved writes corrupt the transcript JSONL. One live process per
  instance, or separate session ids / `--fork-session`.

---

## Topology: the relay is also the bus

The topology is not a free choice. **Only the process holding an agent's stdin
can prompt it**, so every message physically passes through the host-side relay —
there is no peer-to-peer path. That constraint is welcome: it forces a star, and
the hub is exactly where round caps, logging, and "done" detection want to live.

The same relay carries both directions. It holds each agent's **stdin**
(delivery) and parses each agent's **stdout** `result` events (that is how the
PoC knows a turn finished), so agent→agent messaging needs no new channel: the
relay reads A's reply and writes it into B's stdin.

```
            HOST                                     CONTAINERS
  ┌───────────────────────┐   stdin (JSON line)   ┌─────────────────┐
  │                       │ ────────────────────► │ worker  (rw ws) │
  │   relay daemon        │ ◄──────────────────── │                 │
  │   - routing policy    │   stdout (result ev)  └─────────────────┘
  │   - round cap, "done" │
  │   - journal → inboxes/│   stdin               ┌─────────────────┐
  │                       │ ────────────────────► │ reviewer  {ro}  │
  └───────────────────────┘ ◄──────────────────── │                 │
                              stdout               └─────────────────┘
                    shared volume: workspace (worker rw, reviewer ro)
                                 + inboxes/ journal (relay is sole writer)
```

Containers must be started with `docker run -i` and **no `-t`** — `-i` keeps
stdin open as a pipe, while a TTY would reintroduce echo and CRLF mangling into
the JSON stream. The relay's lifetime is the collaboration's lifetime: if it
dies, the pipes close and the agents see EOF and wind down. Transcripts survive,
and `--resume <session_id>` re-attaches, so that is recoverable — but it makes
the relay the de-facto session object.

### What the message between agents should be

| Carrier | How A's message reaches B | Verdict |
|---------|---------------------------|---------|
| **In-band: A's turn-reply *is* the message; the relay routes it** | Relay takes A's `result` text, wraps it (`Message from worker: …`), writes it to B's stdin | **Start here.** No agent-side convention to forget or malform; it is the path already proven end to end |
| Mailbox files the agents write with tools | A writes `outbox/reviewer/001.md`; the relay watches and delivers | Defer. Adds a convention the model must remember every turn plus silent failure modes (wrong dir, malformed file), while the relay still does the same routing |
| MCP `send_message` tool | A calls a real tool; the MCP server hands off to the relay | The right *upgrade*, not the start — schema-enforced addressing, mid-turn and multi-recipient sends. Worth it once more than two or three agents are involved |

Two moves keep the in-band carrier sufficient:

1. **Split control from data.** Messages carry only control ("done, changed
   `foo.py` and `bar.py` — please review"); the work product travels through the
   **shared workspace volume**. Code and diffs never bloat the message stream,
   and the no-two-read-write-writers rule holds by construction (worker `rw`,
   reviewer the same path under `{ro}`).
2. **Keep the mailbox layout — as the relay's journal.** The relay logs every
   routed message to `inboxes/<agent>/NNN.md` (Agent Teams' layout). That gives
   the durable, replayable paper trail of the file-based approach with a *single
   writer* and no agent-side convention.

Routing stays dumb policy in the relay to begin with: worker's reply → reviewer;
reviewer's verdict → worker; an approval marker (or the round cap) ends the run.
Each agent learns its role from its per-instance CLAUDE.md addendum — plumbing
the launcher already has (`install_latest_md`) — including the one line that
makes the in-band carrier work: *"your reply is forwarded verbatim to your peer,
so make it self-contained."* A manager doing real judgement-routing later joins
as a third spoke whose replies the relay reads as routing decisions; the star
does not change.

---

## The town_square service

The relay gets a name and a home: a middle service owning
`~/.claude-agents/town_square/` — a sibling of `instances/`, `cache/`,
`firewall_cache/`, and `quickie/`, declared in `paths.py`.

**What it does and does not solve.** A directory cannot hold a pipe. The write
end of a container's stdin belongs to whichever process called `docker run -i`,
and nothing on disk can substitute for that. So the directory alone does not
solve the piping problem — but a *service that spawns the agents* does, because
it is then the parent process holding every pipe. `town_square/` is that
service's journal and coordination surface, not its mechanism.

**The control plane stays out of the containers.** With the in-band carrier the
agents never read or write `town_square/` themselves — the service does, on the
host. So it needs no mount, exactly like `firewall_cache/`, and the only
puncture of per-instance isolation remains the shared *workspace*. (A later move
to a mailbox or MCP carrier would change that: those need the agents to reach the
bus, via a mount or a bind-mounted socket.)

**Durable state, ephemeral pipes.** `town_square/` survives a service restart;
the pipes do not. If the service dies the agents see EOF and wind down, but the
transcripts and journal remain and `--resume <session_id>` re-attaches each one.

**How it should spawn agents.** Preferably by calling the launcher's own
`run_container` with a persistent mode added, so tag resolution, mounts, and
engine-conf assembly are reused rather than reimplemented (what
`poc/relay_poc.py` does by hand, and why it duplicates the credential mounts).

### Relay and carrier are layers, not alternatives

Worth stating plainly, because the table above invites the confusion:

- The **relay** is the *wake* mechanism. It holds stdin and is the only thing
  that can originate a turn in an idle agent. It is never optional.
- The **carrier** is what a message *is* — in-band reply text (v1), mailbox
  files, or an MCP `send_message` tool (the upgrade).

Adopting MCP replaces the carrier only. An MCP tool is pull-model, so it still
cannot wake an idle peer; the relay remains underneath it either way.

### Verified constraints (Claude Code 2.1.215, this tree)

- **`tag.docker` cannot express persistent mode.** It accepts exactly
  `build_arg_forward`, `cap_add`, `entrypoint`, `mounts`, `env_forward`; there is
  no generic run-flag key, and unknown keys are *silently dropped* (a
  `tag.docker` containing `stdin = "pipe"` parses to an empty contribution, no
  error). Adding one intent-shaped key (`stdin = "pipe"`) would be the clean
  extension — it should drive **both** `-i` and `stdin=PIPE`, since specifying
  only the flag leaves the two free to drift.
- **`-i` alone is not enough.** It only keeps the container's stdin open; what
  stdin is *connected to* comes from the parent. `docker_subprocess` inherits the
  launcher's stdin and `docker_stream_subprocess` passes
  `stdin=subprocess.DEVNULL`, so a relay-driven agent needs
  `stdin=subprocess.PIPE` — a Python argument, not a docker flag.
- **A hidden layer may sit at the profession root.** `discover_layers` sets
  `requires = frozenset(ancestors)`, so `agents/profession/_cowork/` would carry
  no dependency at all (unlike `code/_dood`, which inherits `{code}`).
- **Hidden asset dirs cannot contain tags.** `discover_layers` rejects any
  `tag.info` beneath a `_`-dir, so role tags cannot nest inside `_cowork`.
- **Specialties are flat.** `Specialty.scan` does a single `iterdir()` with no
  recursion, so there is no specialty→specialty inheritance. A specialty's
  `requires` comes from the *layer it claims* — that is how `{dood}` acquires
  `{code}` — so tree position lives on the profession side.

---

## The `{cowork}` specialty

Decided and added (`agents/specialty/cowork/tag.info`). It grants **capability
only**: the instance may be recruited into a collaboration, and nothing about a
normal interactive launch changes. The headless, relay-driven session mode is
chosen by the relay when it starts the group — *not* baked into the instance.

That split is deliberate. A tag that forced headless mode would poison the
instance: tag it once and it could never be opened interactively again. As it
stands a coworking instance stays fully usable on its own, and stays inspectable
mid-collaboration via `claude --resume <session_id>`.

What it deliberately does **not** carry:

- **No `claude_args`.** The stream-json flags belong to the relay's launch, and
  keeping them next to the pipe wiring stops the two from drifting apart.
- **No `workspace_readonly`.** That is role-specific — the reviewer wants `{ro}`,
  the worker must not have it.
- **No role.** Worker / reviewer / manager behaviour comes from the agent's own
  `.md` persona. Encoding roles in tags would give `{cowork-worker}`,
  `{cowork-reviewer}`, and so on — combinatorics for no gain.

It carries `warn = true`, because coworking punctures the per-instance isolation
the launcher otherwise provides: peers share a workspace, so a peer's mistakes
can reach your files.

**Recognition is by tag name**, decided deliberately in preference to adding a
`tag.info` schema field. The trade accepted with it: the launcher will name
`cowork` in its own logic, so renaming the tag later means editing code — unlike
the rest of the tag system, which is field-driven and never special-cases names.

### Still to build

1. **Persistent launch mode.** `run_container` passes `["-it"] if interactive
   else []`, so a non-interactive launch currently gets *neither* `-i` nor `-t`;
   and `docker_stream_subprocess` hardwires `stdin=DEVNULL`. Relay-driven agents
   need `-i` (no `-t`) plus `stdin=PIPE`.
2. **The relay daemon**, promoted from `poc/relay_poc.py`: N agents, routing
   policy, round cap, journal.
3. **Role briefings** as per-instance CLAUDE.md addenda, via the existing
   `install_latest_md` plumbing (the PoC's `WORKER_BRIEF` / `REVIEWER_BRIEF`).

Until (1) and (2) land, `{cowork}` is inert by design — which is exactly what a
capability-only tag should be on a solo launch.

---

## Design cautions (independent of mechanism)

- **The protocol is the easy part; the scheduler is not.** Who runs next, what
  wakes them, and when the group stops determine everything else.
- **Never let two writers share a code workspace read-write.** Edits are
  read-then-write with no locking, so concurrent writers silently lose work.
  Share a *mailbox*; give the reviewer the workspace read-only via `{ro}`
  (which denies the edit tools *and* mounts `/workspace` read-only).
- **Cap the rounds and define "done" explicitly**, or agents ping-pong
  indefinitely at multiplied token cost.
- **Messages must be self-contained.** Peers have separate contexts and never
  saw each other's reasoning.
- **Multi-agent work deliberately punctures the per-instance isolation** this
  project exists to provide. Be deliberate about combining it with `{auto}` or
  `{dood}`.
