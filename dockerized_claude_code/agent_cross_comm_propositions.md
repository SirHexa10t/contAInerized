# Cross-instance agent communication — mechanism survey

Design notes for a prospective feature: letting several running agent instances
collaborate (e.g. a worker, a reviewer, a manager) instead of working alone.

This file surveys **how one process can hand a prompt to another running agent**
and wake it up. It is a decision record. Since it was written, the `{cowork}`
feature implemented option 2 (pty injection) as the hub's ingress; the `poc/`
scripts that validated these options were removed after serving their purpose.

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

Ruled out by documentation **as of Claude Code 2.1.215**, so no design should
assume them. ⚠️ **Two of these stopped being true in v2.1.224** — see the dated
addendum below; kept as written because the options table above was evaluated
against this state of the world.

- No built-in cross-process messaging between separate `claude` instances.
  **Agent Teams** is the closest feature, but its mailboxes
  (`~/.claude/teams/<team>/inboxes/<agent>.json`) are single-machine and scoped
  to one lead session — its *layout* is worth copying, the mechanism is not
  reusable across containers.
- No socket, pipe, or control file for injecting a prompt into a running
  **interactive** session (which is why option 2 has to impersonate a keyboard).
- No hook that can spontaneously originate a turn. Option 3 is the only
  hook-based continuation, and it only fires when a turn *ends*.

### Addendum (2026-08-11): cross-session messaging, v2.1.224

Anthropic shipped **cross-session messaging** in the Claude Code CLI
(`SendMessage`/`ListAgents` across independent sessions; docs:
<https://code.claude.com/docs/en/cross-session-messaging>), which supersedes the
first two "does not exist" bullets: separate `claude` instances CAN now message
each other, and every non-`--bare` session binds a **per-session inbox Unix
socket** whose path is exported as `CLAUDE_CODE_MESSAGING_SOCKET` (to hooks and
Bash, before `SessionStart` runs) — a supported ingress "when you want a script
or hook to post into a session."

What it changes for THIS launcher — researched by a cowork peer, full sourced
findings in the closed group's
`group_hosting/refactorer__dockerized_claude_code/refactorer__dockerized_claude_code-agent_comms_research/findings.md`:

- **Not a transport replacement.** Same-machine discovery = shared registration
  files + socket, so container-per-agent breaks it (docs say so explicitly);
  the cross-machine path routes through Anthropic servers.
- **The socket IS a candidate replacement for option 2** (pty injection): a
  `SessionStart` hook inside each container could publish the socket path to a
  mounted volume for the hub to write to. Researched further on 2026-08-11
  (socket_protocol group): **the wire format is undocumented — a checked null
  result — so the verdict is WAIT** for a payload spec or official client.
  Also settled: the kill-switch env vars are sticky (unset, not `=0`);
  `crossSessionInbound` is a strictness ratchet (project/local `accept` is
  ignored; user scope — which our merged settings.json is — works, and an
  explicit `accept` moots the PID-1 own-child failure); Linux sandboxes offer
  only the all-or-nothing `allowAllUnixSockets`. Host-write acceptance remains
  untested. Details: ISSUES.md "Socket delivery" entry.
- **Sprung trap, verified live in this tree:** the base image sets
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (Dockerfile), which disables
  feature-flag evaluation and with it this whole feature — no
  `CLAUDE_CODE_MESSAGING_SOCKET` appears in a 2.1.226 container today. Any
  socket experiment starts by lifting that for the instance under test.
- Their design independently converges with this survey's recommendations
  (named addressing, per-recipient inboxes, text-only payloads, loop caps), and
  their delivery-outcome notices (held/denied/expired) prompted the hub's
  stand-down notices (see ISSUES.md).

---

## Outcome (2026-08-11) — the survey concluded; the feature shipped

Everything below the options survey used to be design sections (the option-1
recommendation, the relay/town_square service, the `{cowork}` specialty draft,
the closing design cautions). They are deleted, not archived — no longer
propositions:

- **The feature shipped as `{cowork}`/`{manager}` + the host-side hub.**
  `group_hosting_plan.md` is the design record; `launch/cowork/` is the
  implementation; ISSUES.md tracks what is still open.
- **Option 2 (pty injection) is the shipped wake, not the starred option 1.**
  The hub design reduced docker's role to "exactly one thing: injection, the
  wake", and injection needed no change to how containers are started or owned.
  Option 1 (persistent `--input-format stream-json` stdin) remains the cleaner
  ingress IF the launcher ever owns every agent's stdin — a structural change.
  Its one implementation prerequisite, preserved from the deleted constraints
  section: `tag.docker` accepts no run-flag keys and silently drops unknown
  ones, so option 1 needs a new intent-shaped key (e.g. `stdin = "pipe"`)
  driving BOTH the `-i` flag and the Python-side `stdin=PIPE`, or the two
  drift.
- **The closing design cautions are now enforced or taught by the shipped
  feature** — self-contained messages, round budgets, no shared read-write
  workspace, `{ro}` for reviewer coworkers — via the `{cowork}`/`{manager}`
  addenda and the hub's own limits.
- **Live validation of the shipped machinery** (two end-to-end engagements,
  new tag format, `+quiet`, queue consumption): see "Live validation
  (2026-08-11)" in `group_hosting_plan.md`.
