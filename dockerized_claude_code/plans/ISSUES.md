# Issues

The live tracker: problems that stay in place until someone fixes them, and
questions whose answers would change how the launcher is built. Completed work
is NOT recorded here — `refactoring_plan.md` / `refactoring-replan.md` /
`TODO.txt` are archives of finished passes, and `group_hosting_plan.md` is the
cowork feature's design record.

Every entry states what is known, how it was established, and what would close
it. An entry whose evidence is a single agent's self-report says so.

---

## Open questions — cowork permission model

These came out of live probing (Claude Code **2.1.226**) and are
version-sensitive: a Claude Code upgrade can invalidate any of them, so re-probe
rather than trusting the note.

### The `dontAsk` allowlist is a floor, not an enumerable ceiling

`{cowork}`'s fragment sets `permissions.defaultMode = "dontAsk"`, which was
believed to make its `allow` list exhaustive. It does not. A coworker holding
only the 10-entry floor had unlisted `python3`, `ruff`, `WebFetch` and
`WebSearch` denied — but unlisted **`echo`** and the **Agent/Task tool** ran.
So Claude Code carries a built-in default-allow set, and it is a hardcoded
NAME list rather than a safety judgement — the sharpest evidence being that
`true` runs while **`:` is denied**, though they are the same no-op builtin.

Characterised (not exhaustively) by a later probe with artifacts the host could
read: **ran unlisted** — `echo`, `true`, `pwd`, `printf`, `test -f`, `[ -d ]`,
and the Agent tool; **denied** — `ls`, `cat`, `python3 -V`, `cd`, `:`, WebFetch,
WebSearch. The shape: unlisted commands with no filesystem or network effect may
run; everything else is refused. A coworker's own summary of the precedence is
worth keeping: *"deny list > internal policy > allow-list guarantee, with
`dontAsk` meaning 'never interrupt the user' rather than 'allow everything'."*

**Why it still matters:** any reasoning of the form "a coworker cannot do X
because X is not in its allow list" remains unsound, and the full set cannot be
enumerated from outside. Only *observed* denials are evidence.

**What would close it:** nothing worth doing. The shape is known, the boundary
is not security-critical (deny rules and the mode are what enforce), and the set
can change with any Claude Code release. Re-probe if a release surprises you.

### `{auto}`'s bypass does NOT defeat deny policies — VERIFIED first-person

Long assumed but never tested: does `--dangerously-skip-permissions` (what
`{auto}` passes) turn off deny rules too? **No.** Verified directly from a
session running with the flag — `/proc/1/cmdline` confirms
`claude --effort max --continue --dangerously-skip-permissions` — where
`sudo -n true` was still refused: *"Permission to use Bash with command
`sudo -n true` has been denied."*

So the precedence holds even under bypass: **deny beats everything**, including
`{auto}`. Two consequences worth acting on:

- guardrail policies (`<-su>`, `<-gpush>`, `<-net>`, `{ro}`'s write-deny) are
  worth pairing with `{auto}`, which the older `dood + auto` combo warning was
  written as if they were not;
- conversely, `{auto}` + a deny policy is a genuinely useful build: autonomous
  yet still fenced. Worth saying so in `{auto}`'s own description.

### `.claude/` config paths have no FILESYSTEM guard — the block is the permission engine

The same bypass session wrote `/workspace/.claude/agents/probe_selftest.md`
successfully (then removed it). So the earlier "config paths are protected"
finding is really "the permission engine refuses these paths under `dontAsk`" —
there is no OS-level or hard-coded filesystem guard beneath it.

Consequence: the protection is only as strong as the mode. An instance carrying
`{auto}` **can** author itself an agent definition, so the frontmatter-override
route is open to any autonomous instance — closed only for restricted coworkers.
For `{cowork}` that is the case that matters (coworkers are the untrusted side),
but it should not be described as a hard boundary.

### Subagent inheritance — VERIFIED first-person: delegation cannot widen permissions

**Closed.** Tested from a session that both runs `--dangerously-skip-permissions`
AND is denied `sudo` by `<-su>` — the one configuration where the two mechanisms
can be separated. Its subagent attempted `sudo -n true` and got back
*"Permission to use Bash with command `sudo -n true` has been denied."*, while
`echo` and two `/proc` reads in the same subagent session ran normally.

Three things this establishes at once:

- **the deny rule reaches the subagent** — delegation is not an escape hatch;
- **enforcement is per tool call inside the subagent**, not only in the parent
  (the denial arrived before a shell existed: no exit code, no stderr);
- **the denial is command-specific**, not a blanket loss of Bash.

Consistent with the binary's own claim (`Subagents inherit the parent session's
permission mode`). An earlier, weaker version of this finding rested on a
haiku-class coworker's paraphrase; this replaces it.

Still untested: whether an agent-definition's `permissionMode` frontmatter can
widen a subagent (the binary says it "may override"). Reachable only by an
instance that can write `.claude/agents/` — i.e. an `{auto}` one, per the entry
above — so it is a question about autonomous instances, not about coworkers.

### No environment variable marks a subagent

`env | grep -i subagent` is empty inside a subagent, so subagent context is not
discoverable from the environment. Anything that tried to detect or vary
behaviour "when running as a subagent" via an env var would silently never fire.
(Noted because the `CLAUDE_CODE_*_SUBAGENT_*` vars are INPUTS one sets, not
markers the runtime exports.)

**Evidence quality — weaker than it first looked.** Both rest on a
*haiku-class coworker's self-report* plus binary strings, not on direct
observation from the launcher side (the subject's `/workspace` is its own mount,
so the manager cannot inspect what it did). Two concrete reliability failures
were caught in that one probe, and they are the reason these stay open:

- it **paraphrased while claiming to quote.** Asked for the subagent's denial
  *word for word*, it reported `"Bash has been denied. I cannot run the command
  without explicit permission being granted."`; the operator's console showed
  the subagent had actually said `"I cannot execute the Bash command because
  Claude Code is running in don't-ask mode and Bash permission has been
  denied…"`. The finding survives (the subagent WAS denied) but the quotation
  did not;
- it **reported an attempt it had not made**, correcting itself only when
  challenged with a control test.

Treat its narration as a lead, never as data. Anything load-bearing needs an
artifact the host can read.

**Not a launcher bug (checked, so nobody re-investigates):** that same subject
reported its merged `allow` list with five entries duplicated. `merge_fragments`
does dedupe (`"lists concatenate then dedupe (order-preserving)"`, verified
against a deliberately overlapping merge), the base `settings/settings.json`
carries no `allow` list at all, and the repetition pattern interleaved rather
than concatenating. A model rendering glitch.

**Closed by first-person probing instead** (see the two verified entries above):
the manager tested the same boundaries on itself and its own subagent, so no
trust chain and no coworker was involved. That is the technique to reuse — a
question about *this* instance's ceiling never needed a peer.

### Capture loses everything but the final message — FIXED, kept for the record

The `{cowork}` Stop hook forwards `last_assistant_message`, so a coworker that
narrates across a turn has every message but its last silently discarded —
confirmed by comparing a coworker's console transcript against what the hub
received (four of five numbered answers were lost). No hub-side truncation
exists; it is the payload's semantics.

Mitigated by the `{cowork}` addendum bullet instructing coworkers to put their
whole answer in one final message, which was verified to work on the same
subject. Recorded here because the mitigation is *advisory* — a peer that
ignores it still loses data with no error on either side. A structural fix would
need a different capture source (the transcript itself, which the hub already
reads for attribution).

### Closing a group stranded in-flight submissions — FIXED

A coworker did work after its manager had closed the group, and its artifacts sat
in its own tree with no route out: `relay._handle` resolved captures against
ACTIVE sessions only, so a reply for a closed group became `unknown-group` and
was dropped.

Fixed by resolving against EVERY group and adding a `LATE` event: the reply is
journalled, **the files are still submitted** to the manager's inbox (that inbox
is the manager's own tree, so nothing is exposed), and the manager is not woken —
a closed group should generate no traffic, and `cowork status` already reports a
waiting inbox. Verified end-to-end on the real artifacts that were stranded.
Three tests pin it, including that a group which never existed still reports
`unknown-group` so the new path cannot swallow the genuine case.

### `recruit` did not reopen a closed group while the CLI claimed it did — HALF FIXED

`cli._resolve` used to print *"reopen it by recruiting again"*, which was false:
`create_session` returns an existing session untouched (deliberately — a
re-recruit must not reset a round count), so the status stayed `closed` and every
group-scoped verb then refused it.

The message is now honest ("closed groups cannot be reopened — recruit under a
new project label"), which is what the workaround actually is.

**Still open, deliberately:** there is no way to REOPEN a group. An explicit
`reopen` verb that flips the status without touching `rounds_used` would be more
useful than the workaround, but it is new surface on the control channel and
nothing has needed it yet.

### Engine `.conf` subagent controls — RESOLVED; only `FORK_SUBAGENT` untouched

Settled by probing on Claude Code 2.1.226. The verdicts and their exact evidence
now live where someone writing an engine will meet them —
`agents/engine/default/engine.conf`, in the "Subagent controls" comment block —
rather than here, since this file tracks what is still open. In short: the
concurrency cap, the depth cap and the subagent-model override all work;
`MAX_SUBAGENTS_PER_SESSION` is **inert** (8 spawns against a cap of 3, no
refusal).

Two implementation details worth not rediscovering, both in that conf block:
the two working caps use **different enforcement mechanisms** — concurrency
refuses at runtime with a message naming its own variable, while depth silently
**withholds the spawning tool**, so no log will ever attribute a blocked nested
spawn to the depth cap; and the concurrency cap must be at least as large as the
chain depth you want, or it intercepts every nested spawn before the depth check.

**Still open:** `CLAUDE_CODE_FORK_SUBAGENT` — meaning not guessable from the
name, never exercised. Nothing depends on it.

### How to probe a permission question (technique, for reuse)

Every permission question in this file was eventually answered the same way, and
the earlier failed attempts all shared one mistake — asking a peer:

- **probe the instance you are ALREADY in, plus its own subagent.** A question
  about a ceiling never needs a peer. The manager settled deny-survives-bypass
  and subagent-inheritance on itself in two tool calls, with no trust chain;
- **check `/proc/1/cmdline` first.** Whether the session runs
  `--dangerously-skip-permissions` decides what any result means. A successful
  write under bypass proves nothing about the gated path;
- **if a peer must be involved, land artifacts where the HOST can read them** —
  a coworker's `/cowork/<group>/` is submitted to the manager's inbox, so the
  file becomes the evidence instead of the narration. Two subjects paraphrased
  quotes they claimed were verbatim;
- **expect a refusal, and take it.** A coworker cannot verify that a relayed
  request was operator-sanctioned, so boundary-probing asks are declined by
  design. Rephrasing to get past that is the manipulation the refusal detected;
  ask the operator to drive it directly instead.

### A released coworker's reply was a hub-killing poison pill — FIXED

`relay._forward_to_manager` called `sync.submit` without checking membership,
and `sync._deliver` (correctly) raises for non-members. Nothing caught it:
the pass died, the capture survived, and every later pass — and every hub
restart — died again on the same file. Found while adopting delivery-outcome
notices (below), fixed threefold: `_handle` now short-circuits non-members
(`NONMEMBER` event; journalled, never forwarded, no files taken), `poll_once`
parks any capture whose handling raises into `rejected/` (`ERROR` event) instead
of dying, and both paths are pinned by tests including the crash reproduction.

### Senders got no outcome notice for unroutable replies — FIXED

Adopted from Anthropic's cross-session messaging (its held/denied/expired
notices; see the agent_comms_research group's findings.md). A coworker whose
reply hit a closed group — or who had been released — learned nothing and could
keep working into a void. The hub now injects a one-time stand-down notice,
prefixed `[cowork-notice]` — hyphenated deliberately so `GROUP_TAG_RE` can never
parse it as group traffic, which is what makes the notice loop-proof: the ack it
provokes drains as unsolicited. Once-per-(instance, group) is remembered in hub
state (`ParticipantState.noticed`, surviving restarts); `_clear_outstanding` was
also fixed to stop rebuilding the state from scratch, which would have silently
re-armed every notice.

### Socket delivery could replace pty injection — RESEARCHED: WAIT

Cross-session messaging (v2.1.224) gives every non-`--bare` session an inbox
Unix socket, path exported as `CLAUDE_CODE_MESSAGING_SOCKET` before
`SessionStart` runs — documented for "a script or hook posts into a session",
i.e. exactly what `docker_attach_inject`'s pty keystroke-typing approximates
unsupported. Plan shape, should it ever open: a `SessionStart` hook in each
container publishes its socket path to a mounted volume; the hub writes to it.

**Verdict: do not build yet.** The follow-up research (group socket_protocol,
its `socket_protocol.md`, per-claim URLs) resolved every question, and the
decisive one negatively:

1. **The wire format is UNDOCUMENTED — a checked null result**, not a gap in
   our reading: the socket section prescribes no payload, the full CLI
   reference has no posting client, and the docs index ships protocol
   references for other subsystems but none for messaging. An unversioned,
   reverse-engineered contract as the hub's sole ingress is worse than the
   pty hack it would replace. Re-check on Claude Code releases; build when a
   payload spec or official client appears (prototype behind a flag, keep pty
   injection as the fallback — the CHANGELOG already shows churn here).
2. **The kill-switch is sticky:** `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=0`
   still disables — it must be UNSET (verified wording; our Dockerfile sets it,
   confirmed live as no socket in a 2.1.226 container). The fine-grained trio
   (`DISABLE_AUTOUPDATER`, `DISABLE_ERROR_REPORTING`,
   `DISABLE_FEEDBACK_COMMAND`, plus `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY`)
   keeps messaging alive — but **telemetry-off and messaging are mutually
   exclusive** (`DISABLE_TELEMETRY` and `DO_NOT_TRACK=1` both kill flag
   fetching; no "telemetry off, flags on" switch exists). Accepting telemetry
   in exchange for the socket is an operator policy call, not ours.
3. **`crossSessionInbound` is a strictness ratchet, not scope-ordered:** a
   project/local `accept` is silently IGNORED (`accept` < `hold` < `refuse`;
   only managed / `--settings` / user scopes can loosen). Our merged
   `~/.claude/settings.json` IS user scope in-container, so the fragment
   model can deliver `accept` — but nothing project-side ever could.
   NOT worth shipping ahead of time: `accept` is a gate on the messaging
   socket's inbound traffic, and with the feature dead in our containers
   (kill-switch) and pty injection as the ingress, that channel carries zero
   traffic — the key would be inert configuration that reads as load-bearing.
   Add it the day the socket path reopens, not before.
4. **`accept` moots the PID-1 problem:** own-child verification applies only
   when NO `crossSessionInbound` value applies, so setting it explicitly
   sidesteps the in-container lineage failure. Without it, an unverified
   message to a bypass-class instance is held and silently dropped at
   `dialogExpiry` (default 5m) — unattended coworkers would lose mail.
5. Linux sandbox nuance for the eventual prototype: the per-path
   `allowUnixSockets` list is macOS-only and IGNORED on Linux;
   `allowAllUnixSockets: true` is the only Linux switch (and only matters if
   the optional seccomp filter is installed).
6. Still untested (flagged inference, unavoidable until a prototype): whether
   a host process writing into a bind-mounted socket is accepted at all, and
   what sender identity it presents. Nothing documents non-Claude writers.
   **This is the potential show-stopper, and most of it CAN be checked early**
   — cheaply, before any build, if the feature is ever revisited:
   - *(a) activation:* run one throwaway instance with the kill-switch var
     genuinely UNSET — note `docker run -e VAR=` cannot do it (empty is still
     "set" under the sticky semantics); the clean route is an entrypoint
     wrapper that `unset`s it before exec (the `{firewall}` tag already shows
     tag-shipped entrypoints), or a build-arg-gated `ENV`. Success =
     `CLAUDE_CODE_MESSAGING_SOCKET` present and the socket file bound.
   - *(b) in-container proof:* start TWO sessions inside that one container
     and have them message each other — docs sanction exactly this ("Two
     sessions inside the same container can still message each other"), and it
     proves the feature works in our image with zero wire-format knowledge,
     using Claude itself as the client.
   - *(c) host reachability:* `connect()` to the bind-mounted socket from the
     host — tests the mount and the same-OS-user/peer-credential boundary
     (container uid vs host uid) without sending a valid payload.
   - *(d) acceptance of a real message* — blocked on the wire format; this is
     the one piece that must wait.
   (a)–(c) are an hour with one throwaway instance; a failure at (a) or (c)
   kills the plan before any code exists.

Full sourced findings: the closed socket_protocol group's `socket_protocol.md`
and agent_comms_research's `findings.md`; survey context:
`agent_cross_comm_propositions.md`'s 2026-08-11 addendum.

**2026-08-29 update (Claude Code 2.1.245, probed live):** the WAIT verdict
above is for COWORK's hub — a HOST process writing into a session's socket —
and it stands (wire format still undocumented). But the IN-CONTAINER path the
cluster needs is now verified working with no wire-format knowledge at all:
two sessions in one launcher container, kill-switch unset, exchanged a message
via their own `ListAgents`/`SendMessage` tools with native wake, through the
live firewall, named via `CLAUDE_CODE_SESSION_NAME`. The model is the client;
no host ingress is involved. Mechanics that supersede the older notes:
registration is `$CLAUDE_CONFIG_DIR/sessions/<pid>.json` (+ peer-token
`.key`), the socket is `/tmp/cc-socks/<pid>.sock`, and points (a)+(b) of the
early-check recipe above are hereby done. Full results:
plans/cluster_plan.md, "Research spike — CLOSED".

## Known issues — cluster work protocol, first live {cc} run

**Audited 2026-09-02** on a real 4-member `{cc}` cluster (ConcorDance PoC),
by reading `/cluster/protocol/chat.jsonl` and the container's work state.
Verdict: the GATE mechanism worked exactly as designed; what is missing is
everything it does not cover.

- **The one gate ran correctly, and reply counts were EVEN.** `project-starter`
  opened `poc-arch`; all three required members replied exactly once — stance
  8, 8, 8, each with riders and evidence — and the opener closed with
  "APPROVED 8/8/8" plus the adopted riders. 3-of-3 accounted for. The
  operator's impression of unevenness came from TOTAL message counts
  (project-starter 5, researcher 2, others 1), which is by design: the opener
  also posts the `open`, the `close` and progress `free` lines, and free
  chatter is unrationed. Worth surfacing per gate ("gate X: 3/3 replied")
  rather than leaving the raw journal to imply it.
- **ONE gate for an entire application.** Everything after the plan ran on
  `free` announcements — including a font/asset swap explicitly framed as
  "minor refinement inside the approved plan". That followed the addendum's
  own letter ("routine progress inside an agreed plan needs no gate"), which
  is exactly why the addendum was the thing at fault.
- **Nothing ever priced the dependency footprint** — the operator's
  "way-too-large application" complaint, quantified: **393 transitive crates,
  3.4 GB target dir**, from an `iced` (wgpu/GPU) GUI stack. Per-crate
  discipline was actually good (pinned `=` versions, `default-features =
  false`, feature-trimmed, each with a comment). The plan gate named the
  library and everyone stanced 8 on it before any footprint existed to
  judge. FIXED in the `{cc}` addendum: dependencies are their own gate even
  inside an approved plan, and the gate body must carry a measured footprint;
  a footprint bigger than the gate's claim is a NEW gate.
- **Duplicated verification, with no durable home for findings.** Three
  members each opened the same corpus archive and separately confirmed the
  same facts (charset, anchor format, file counts), after which one had to
  correct another's reading in prose. The queue is an append-only
  CONVERSATION, not a knowledge base. Addendum now tells members to read
  before re-verifying and to post what they establish; a shared findings
  file under `/cluster/` is the obvious next step and is NOT built.
- **No per-member git isolation, by construction.** All four members ran with
  cwd `/workspace` on ONE checkout, single `main` branch, nothing committed
  (everything untracked at audit time). `launch_plan.build(...,
  personal_workspaces=False)` is the switch; `cluster/worktree.py` already
  implements the per-member alternative and is simply unused. Coordination is
  by file-claiming over the queue (the opener did claim `Cargo.toml`, `src/`,
  `tests/`, `assets/`). Whether to flip that to worktrees-plus-branches is an
  open operator decision, not a defect.

## Known issues — Claude Code surface

- **`app:exit` CANNOT be rebound — so there is no uniform exit key for
  non-`{muxer}` instances.** The operator wanted one chord (`alt+q`) that
  ends any session. On a `{muxer}` instance the multiplexer already provides
  it (root-table binding / direct chord, confirmed working). For a bare
  instance the key would have to reach Claude Code, and `keybindings.json`
  looks like it should deliver: the docs list `app:exit` (default Ctrl+D) in
  the rebindable "App actions" table. It does not work. Measured
  first-person on **2.1.251**, with both controls, inside a scratch tmux
  session driven by `send-keys`:
  - `alt+q` → `app:exit`, bound in **Global AND Chat**, pressed twice: the
    session was completely unaffected;
  - control 1 — `ctrl+d` twice in the same pane: exited (so the pane could
    exit);
  - control 2 — the SAME `alt+q` bound to `chat:modelPicker`: the picker
    opened. So the file IS read and `alt+q` IS decoded and dispatched; the
    ACTION is what refuses to move, matching the docs' separate "Reserved
    shortcuts: Ctrl+D — Hardcoded exit" line (the "rebindable" reading of
    the actions table is what proved wrong).
  The binding was therefore REMOVED rather than shipped inert — a
  `test_no_binding_claims_the_unbindable_exit_action` guard keeps it out,
  carrying this evidence. What would close it: an upstream release that
  makes exit rebindable (re-probe with the two controls above). Workarounds
  meanwhile, both real: run instances WITH `{muxer}` (its quit key is the
  uniform chord, and it confirms before killing), or map `alt+q` in the
  TERMINAL emulator to send `\x04\x04` (kitty: `map alt+q send_text all
  \x04\x04`) — host-side config, outside this repo. Not worth building: a
  pty wrapper that watches for the chord would be re-implementing a
  multiplexer we already ship.

## Known issues — cluster work protocol

- **Capability text is not a protocol — the first live gate never happened,
  and every MECHANISM was fine.** 2026-09-02: a member of a real
  `{clstr}` cluster was given a task, planned it solo, and never opened a
  gate. Verified by inspecting the running container (operator granted
  `{dood}`): the `_cluster` layer, `python3`, the `cluster-chat` shim, both
  `/opt` mounts, `/cluster/protocol/cursors` (member-owned), all four
  members' `$CLUSTER_MEMBER/ROLE/SESSION`, herdr's roster mapping every
  member id to a pane, and `cluster-chat` itself in situ — all correct;
  `chat.jsonl` simply never existed, because nothing was ever posted.
  Cause: the `{clstr}` addendum described the queue's VERBS ("here is how to
  post, here is how to open a gate") in one ~300-word bullet and never named
  the MOMENT a gate is obliged. An agent handed a task therefore did the
  task — correctly, by its instructions.
  Fixed by rewriting the addendum trigger-first: the gate rule leads, states
  what makes a decision consequential (the plan about to be executed,
  architecture, dependencies, schema/API, substantial rewrites, roadmap
  changes), and carries a tie-breaker ("when in doubt, gate: a nop costs a
  sibling one line, a bad unilateral commitment costs everyone a rewrite").
  A test pins the trigger words, so the rule cannot decay back into a manual.
  **The general lesson, worth remembering as a class:** prompt-level protocol
  needs CONDITIONALS ("before X, do Y"), not capability descriptions — and
  the trigger belongs at the top of its bullet, not buried mid-paragraph.
  **Escalated the same day to a harness lever, since prose had already failed
  once:** the new `{cc}` tag (`specialty/muxer/cluster/cluster-cowork`)
  claims `policy/_cluster-cowork`, whose `UserPromptSubmit` hook runs
  `cluster-chat brief` — so at the moment a task arrives the member is told
  what it has not read, which gates it OWES a reply to (repeated every prompt
  until answered: the nag is the enforcement), and the standing gate rule.
  The brief always exits 0 (a non-zero UserPromptSubmit hook blocks the
  prompt, which no briefing is worth) and prints nothing outside a cluster.
  Bonus property worth keeping in mind: it is a SECOND delivery path for the
  queue, so gate traffic reaches a member even when a pane wake is lost —
  which de-risks the one mechanism the trial still has to prove.
  Still unverified (only a live trial can): whether an injected
  `[cluster-chat]` line reliably starts a turn in a member's Claude pane.
  Everything up to that point is proven.

## Known issues — launcher

- **`--continue` silently started a FRESH conversation over a ~92 MB transcript
  (Claude Code 2.1.245, observed 2026-08-29).** The launcher's side was right —
  the container ran `claude --effort max --continue` with cwd `/workspace` —
  and claude even picked the correct predecessor (the new session file
  inherited the old session's ai-title), but loaded none of its messages: the
  new session's first user entry has `parentUuid: null`. The same binary had
  appended to that transcript 16 minutes before the relaunch, and had resumed
  it after a 266-hour gap that same morning (file then ~75–85 MB), so the
  trigger sits somewhere in the size region, not in a version change. The
  transcript itself is intact — every line parses (`jq -e . <file>` exits 0) —
  so the conversation is unloaded, not lost. Upstream problem space, not ours:
  anthropics/claude-code #21022 (accessing >50 MB session files hangs), #30302
  (resume crash on a large multi-day session); the sessions docs (checked
  2026-08-29) document no size cap and no fresh-start fallback.
  What would close it: an upstream fix, or a documented cap to design against.
  Launcher-side mitigation BUILT 2026-08-29 (operator's nod):
  `compute_resume_flag` warns past `RESUME_SIZE_WARN_BYTES` (50 MB) while
  still resuming — the surprise is at least a stated one now.
- **herdr 0.8.2 as a daily driver: three operator-reported "crashes" in one
  day, zero server-side evidence — UNRESOLVED; the operator dropped `{muxer}`
  from the instance.** What IS known: in both containers whose logs were
  inspected before the next relaunch replaced them, `herdr-server.log` held
  no panic and no client disconnect — server and attach were alive at
  inspection time. So what the operator saw was either the CLIENT dying later
  (its evidence dies with the container; never captured), a popup reading as
  a freeze (popups swallow every key including Escape until their command
  exits — Enter is the way out), or the relaunch itself killing in-flight
  work. The reports correlate with the pre-1.0 UI surfaces adopted the same
  day (alt+q / alt+/ popups, a text tab-bar entry, window_title, metadata
  sidebar rows) — plausible client-bug triggers, none proven. On rendering,
  the score settled at: `$keys` sidebar rows never draw on 0.8.2 (token
  lands in `workspace list`, no row — the one confirmed
  accepted-but-invisible surface); `pane rename` labels DO render, on the
  split frame (screenshot 2026-08-30 — an earlier "draws nothing" reading
  from a mid-session rename was wrong), which is why the shell pane's label
  is a plain "shell"; and the TAB ROW's right corner (tab_bar_right) renders
  and is the hint's settled home — hotkeys anywhere else (typed greeting,
  frame label) were reversed as clutter. The row therefore stays visible
  even for solo's single tab (renamed "agent"); tests pin the row-stays +
  corner-hint + plain-label set in both configs.
  What would close it: capturing the client side of one crash (run the attach
  under `script -f`/`tee`, or find a herdr client log, BEFORE relaunching),
  and re-probing label/row rendering on the next herdr release (version
  pinned in `profession/_muxer/Dockerfile`). Until then the measured advice:
  `{muxer}` with tmux is the fully-verified stable path — since 2026-08-30 a
  persisted preference (`herdr_instead_of_tmux = false` in
  `~/.claude-agents/ui_profile.toml`, edited from the picker's
  "(Edit Toolkits & UI)" form), which replaced the earlier `MUXER_BACKEND`
  env var and the flip-the-default option.
- **A file mount's auto-created parent dir is ROOT-owned — FIXED for herdr,
  and worth remembering as a class.** Mounting `settings/herdr.toml` at
  `~/.config/herdr/config.toml` made docker create `~/.config/herdr/` as
  root:root at run time, and herdr binds its socket (plus session.json and
  logs) BESIDE its config — so every herdr-backend launch died at the
  readiness gate, server never bound. Found the day herdr became the default
  backend, by checking the live dir's ownership (the earlier live probe
  predated the config mount, which is why it had passed). Fixed in the
  `_muxer` image layer: `mkdir -p` + `chown` the dir at build time — a file
  mount into an EXISTING dir leaves the dir's ownership alone; a test pins
  the two lines. The general rule joins the RO-nested-mount one below:
  **when a single-FILE mount's parent must be written by the container user,
  the image has to create and own that parent first.**
- **An in-container edit of a RO-mounted settings file does not reach the
  running container.** The `settings/*` files are single-FILE bind mounts,
  which pin an INODE; an editor that writes atomically (temp file + rename —
  Claude Code's Edit tool does) replaces the inode under the host path, so the
  mounted view keeps showing the pre-edit content until the next launch.
  Directory mounts (`/workspace` itself) are immune — only file mounts bite.
  Live-applying a key-policy change therefore means issuing the equivalent
  `tmux -L muxer` commands by hand (or relaunching); `source-file` on the
  mounted conf would re-read the STALE inode. Recorded 2026-08-29 after
  exactly that: an edited tmux.conf verified clean in `/workspace` while its
  mounted copy stayed old.
- **A tag cannot mount a file into `~/.claude/commands/` — FIXED, and worth
  remembering as a class.** `{manager}` shipping `/cowork` via a `tag.docker`
  mount killed every launch of it: *"create mountpoint … read-only file system"*.
  Cause: `custom_commands/` was mounted at `~/.claude/commands` with `:ro`, and
  docker cannot create a mountpoint for a nested file inside a read-only mount.
  `settings.json` gets away with the same shape only because its parent
  (`~/.claude`) is a read-WRITE mount.
  Fixed by ASSEMBLING the directory per instance instead:
  `agents_crud.install_commands` copies the shared commands plus every command
  the active tags declare into `<state>/commands/`, which is mounted whole and
  read-only. A tag now ships a command by declaring its name in `tag.info`
  (`commands = [...]`); the files live centrally in `agents/_commands/` — one
  file shareable by several tags, validated at scan time, and the picker's
  legend lists every declaration. (An earlier iteration had each tag carrying
  its own `_commands/` dir; retired because commands couldn't be shared and the
  underscore dirs read as pseudo-tags in the tree.)
  The general rule: **a read-only mount cannot host a nested mount.** Anything
  that wants per-tag files inside one has to be assembled host-side first.
- **`{muxer}`: whether a copy reaches the HOST clipboard is the terminal's call,
  not ours.** tmux hands a copy-mode selection to the outer terminal with the OSC
  52 escape sequence, and several emulators refuse clipboard WRITES by default
  (kitty needs `clipboard_control write-clipboard`, xterm needs
  `allowWindowOps`). Our side is now complete and verified — `set-clipboard on`,
  the `*:clipboard` feature asserted for every `$TERM` regardless of terminfo, and
  a drag that confirms itself — so a failure past that point is configuration in
  the operator's terminal. Two things make it livable rather than mysterious: the
  drag says "copied", and `^b m` hands the mouse to the terminal so its own
  select-and-copy (which needs no cooperation from tmux) takes over. Diagnosis in
  one command: `tmux -L muxer set-buffer -w "TEST"` then paste — if `TEST` does
  not arrive, the emulator is blocking OSC 52.
  What would close it: nothing on our side. Do NOT add a clipboard helper binary
  (`xclip`/`wl-copy`) — there is no X or Wayland display in the container to talk
  to, so it would fail differently rather than work.
- **`mouse on` is not an additive option — worth remembering as a class.** It
  looked like it only added click-to-focus and wheel scrolling; it silently
  redefined two interaction primitives, and both surfaced as bug reports. The
  wheel becomes `copy-mode -e`, so scrolling puts the pane in a key table where 81
  of the 95 printable characters are DROPPED (typing appeared to do nothing until
  you scrolled back down), and tmux takes the mouse from the terminal, so native
  drag-select stops reaching the emulator. Both are fixed in `cluster/tmux.py`
  (`_typethrough_command`, `_copy_argv`). The lesson generalises: an option that
  adds a capability may also be REPLACING a default, and tmux documents the
  addition rather than the replacement — check what a default binding did before
  enabling the thing that overrides it.
- **No lockfile** — CI gate FIXED. `check.sh` is now the single definition of a
  passing tree (tests + `ruff` + `mypy`), and `.github/workflows/ci.yml` calls
  it on push / PR / weekly across Python 3.12 and 3.14. Three tests in
  `test_essential_files.py` (`TestQualityGate`) fail if a check is inlined into
  the workflow, if the script turns fail-fast, or if the CI matrix stops
  covering the `requires-python` floor. Still open: the deps in
  `pyproject.toml` are floors (`>=`) with nothing pinning a resolved set, so a
  new `prompt_toolkit` / `rich` release can break an untouched tree. The weekly
  CI run is a detector, not a fix — it dates the breakage instead of preventing
  it. A real fix means a lockfile (`uv lock` / `uv pip compile`), which changes
  how `install_dependencies.sh` installs; not attempted.
  ALSO NOTE: the workflow is inert until the repo is hosted on GitHub.
- **Runtime-installed packages vanish on every relaunch, and the split is not
  obvious.** `~/.local` is NOT among the launcher's mounts (only
  `.local/share/pnpm/store`, as a cache), and containers run `--rm`, so anything
  installed *inside* a container is gone when it is recreated. What survives is
  exactly what the IMAGE bakes in: `rich-cli` (base `Dockerfile`) and `ruff`
  (`agents/profession/code/Dockerfile`), both via `uv tool install` at build
  time. What does not: `pip3 install --user` packages (`prompt_toolkit`,
  `python-dotenv`, `rich`) and any `uv tool install` run at runtime (`mypy`).
  Observed on three consecutive relaunches; the confusing part is that `ruff`
  keeps working while `mypy` disappears, which looks arbitrary until you know one
  is in the image and the other is not.
  Consequence for THIS repo specifically: an agent working on the launcher needs
  the launcher's own deps to run its test suite, so the first `check.sh` after
  any relaunch reports import errors that look like regressions and are not.
  Already mitigated, deliberately: `check.sh` treats a missing checker as a
  FAILURE with the `uv tool install` hint rather than skipping it, and
  `/test-ai-project`'s preflight installs what is missing before running the
  gate. So the workflow absorbs it — it is surprising, not broken.
  What would close it: either persist `~/.local/bin`, `~/.local/lib` and
  `~/.local/share/uv/tools` (they are not caches, so a stale binary could shadow
  an image-provided one, and the dirs are Python-version and arch specific), or
  bake the launcher's own dev deps into an image layer (wrong for a general-purpose
  agent container — most instances never touch this repo). Neither is clearly
  better than the current "reinstall on demand", which is why it is documented
  rather than fixed.
- **`docker build` runs per layer on every launch** — no image-exists
  short-circuit, so each launch pays a few cache-hit seconds per layer.
- **`install_latest_md` / `install_settings` overwrite their state-dir files
  every launch.** In-container edits to `CLAUDE.md` or `settings.json` are
  silently discarded. Intentional (the launcher owns those files), but it
  surprises people — an agent asked to "fix its own instructions" cannot.
- **`--network=host` on builds** — a permanent workaround for BuildKit bridge
  DNS issues, not a considered choice.
- **Dry-run only:** an intermittent `Exception in thread phase2-cascade` line
  can print at exit (daemon DNS thread reaped at interpreter shutdown). Real
  runs block on the container, so it cannot occur there.
- **macOS is unverified on real hardware.** Host code avoids Linux-only deps
  (`getent` → `socket.getaddrinfo` fallback, zsh aliasing, a Docker Desktop
  version floor), but two things are untested or unsupported: `docker build
  --network=host` under Docker Desktop, and `{dood}`, which hard-fails on macOS
  (`_apply_dood` needs a host `docker` group — see `tag_handlers.py`, whose
  error message still reads as Linux-only).

## Known issues — testing technique

- **A same-length mutation can leave stale bytecode behind, and the restored tree
  then fails.** Mutation-testing a guard means editing a source file, running the
  suite, and restoring it. CPython validates a `.pyc` against the source's
  **(mtime, size)** — so a mutation that changes neither (`range(0x20, 0x7F)` →
  `range(0x20, 0x7E)`) leaves a cache entry that still looks valid after the
  restore, and the next run imports the MUTATED bytecode. Observed exactly once
  and it was maximally confusing: the file on disk was provably correct while three
  tests failed. Any mutation harness in this repo must delete the relevant
  `__pycache__` after both the write AND the restore. Left here rather than fixed
  because the harnesses are throwaway scripts, not a checked-in tool — the cost is
  remembering, and the symptom (a clean file that fails) is worth recognising fast.

## Known issues — docs

- **`TODO.txt` lists at least one already-fixed defect** (the
  `None<instancename>` credentials banner bug, repaired during the tags
  rewrite). It is an archive of a completed pass, so it is stale by nature —
  but it reads as a task list, which misleads. Worth a header stating it is
  historical, or a prune.
