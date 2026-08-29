# group_hosting — adaptation plan

Design record of the cross-agent cowork feature — written as the forward-looking
implementation plan and kept as the WHY behind the code. The mechanism survey and
its evidence live in `agent_cross_comm_propositions.md`; this file is the design
that followed from it. Everything below is now implemented (see the next
paragraph for the inventory); where implementation taught us something the plan
did not know, the section says so inline.

Implemented and tested so far: the `{cowork}` and `{manager}` tags (nested,
with their addendums), the `_cowork` settings fragment (Stop-hook capture,
`dontAsk`, the read-write allowlist), the per-participant mount,
`docker_config.docker_attach_inject`, the repo-root `cowork.py` entry script, and
every `launch/cowork/` module: `group`, `mailbox`, `journal`, `sync`, `relay`,
`roster`, `lifecycle`, `control`, `cli` — including the hub's full lifecycle
(`run.py` ensures a detached hub on every `{manager}` launch; the hub exits once
no manager has been running for a grace period). The `poc/` scripts have been
**deleted** — everything proven there now lives in the modules above. The audit
covers the tree (`orphan_group` / `bad_session` / `rejected` / `stale_pid`), and
the docs are refreshed (README features + CLI + state layout; `.claude_summary`;
`.claude_dev_guidelines` module roles + the paths-as-builders lesson). Nothing
from this plan remains unbuilt.

---

## The rename

`town_square/` becomes **`group_hosting/`**, so a path states its own role:

```
~/.claude-agents/group_hosting/
```

"Host" is used in the hosting-a-session sense, not a machine: the `{manager}`
instance hosts a group, and its id forms half the group key.

The rename touches the container-side path baked into `{cowork}`'s Stop hook
(`/cowork/outbox/` under the new mount), so **participants must be
relaunched** — a running instance keeps the `settings.json` it was launched with,
and a hub reading the new path while an old instance wrote to the old one would
silently capture nothing. Rename the fragment, the hub constant, and relaunch as
one step. No migration code is needed: `install_settings` rewrites
`settings.json` every launch.

---

## One launcher change: a per-participant mount

**Mount `group_hosting/<instance-id>/` → `/cowork/`** for any `{cowork}` instance.
`set_container_mounts` gains one tag-conditional block. The host dir is owned by the
user, so although docker creates the `/cowork` mountpoint as root, the mount itself
carries host ownership and the container's `claude` user can write it.

### What "relaunch" does and does not cost

Precisely, because it matters:

- **A container's mounts are fixed at creation.** Nothing can add `/cowork` to an
  already-running container. So instances running *today* must be relaunched once,
  when the feature lands. That part is unavoidable.
- **The mount is generic per instance** — derived from the instance's own id, with no
  group knowledge — so **joining a group never requires a relaunch.** A participant
  launched once can be recruited into any number of groups afterwards.
- **Becoming a coworker requires a launch anyway**, independent of the mount: the
  `{cowork}` tag arrives via `settings.json`, which `install_settings` writes at
  launch, and that file carries the Stop hook the hub depends on. So the relaunch is
  the moment of opting in, not an extra tax the mount imposes.

That last point is why an earlier draft's "zero launcher changes" was a false
economy: it avoided the mount by squatting the already-mounted state dir
(`instances/<id>/cowork/` → `/home/claude/.claude/cowork/`, verified `rw=true`), but
bought nothing, since no untagged running instance could have participated regardless.

*If* recruiting arbitrary untagged instances ever becomes desirable, the mount and
the Stop hook would both have to move into the always-on base configuration — at the
cost of every instance writing a capture file on every turn, forever. Not proposed;
noted so the option is not rediscovered from scratch.

### Why this beats the state-dir squat

- **The whole feature lives in one root**, `~/.claude-agents/group_hosting/`, instead
  of scattering across `instances/*/cowork/`.
- **No squatting in Claude Code's own config namespace** (`~/.claude/` holds
  `projects/`, `skills/`, `commands/`, `todos/`), so no risk of a future version
  caring what is in there — and the hub never writes near `settings.json`,
  `CLAUDE.md`, or the transcripts.
- **The hub is host-side, so it just writes files.** `docker cp` is not *bad*, it is
  simply unnecessary once both ends are host paths: the hub writes to
  `group_hosting/<id>/…` and the participant sees it through the mount. One less
  moving part than either `docker cp` or `docker exec` staging.
- **Captures are durable**, because the hook writes into the mounted dir.

Docker is then needed for exactly one thing: **injection**, the wake. Every byte of
data moves as ordinary files.

---

## Directory layout

One root, one dir per participant, mounted as `/cowork` in that participant's
container. There is no separate `groups/` namespace: a group's canonical state
lives in its **manager's** copy of the group dir.

The group key is `<manager-id>-<project-title>`, used identically in every tree so
one string names the group everywhere.

```
~/.claude-agents/group_hosting/
  hub.state.json                              # hub-only — deliberately outside every mount

  <instance-id>/                              # mounted -> /cowork
    outbox/                                   # the Stop hook's drop point
    <manager-id>-<project-title>/             # a group this instance takes part in
      session.json                            #   ...present ONLY in the manager's copy
      conversation.md                         #   ...the group log, likewise
    <manager-id>-<project-title>@<sender-id>/     # an inbox: what <sender-id> sent
```

Every participant has the same two shapes — the group dir it writes, and one inbox
per correspondent, written only by the hub. In a manager's tree an inbox holds a
coworker's submission; in a coworker's tree it holds what the manager handed over.

**Group discovery is a scan, not a registry.** Walk `group_hosting/*/*/`; a dir
containing `session.json` is a group, and its parent dir names the manager. Inbox
dirs hold no `session.json`, and their `@` cannot occur in a group name, so the two
are distinguishable by name as well — belt and braces, since they are siblings. A
dir whose name starts with its parent's own instance id is one that parent *hosts*;
otherwise the parent is a guest in it.

Two consequences worth having:

- **The manager can read its own group log.** `conversation.md` sits inside the
  manager's mounted dir, so no hub copy is needed for the host to review the
  discussion. Coworkers still cannot see it — the hub can copy it into their group
  dir on request.
- **`hub.state.json` stays at the root**, outside every mount, so no agent can
  reach the hub's own bookkeeping even though `session.json` is deliberately
  agent-readable.

A coworker in several teams keeps them in sibling directories, so nothing mixes and
nothing is duplicated.

**On `outbox/` placement** — see the attribution gap below; the short version is
that a static hook cannot pick a per-group destination on its own, so it drops here
and the hub files the attributed record into the group dir.

**Every outbox is drained, not just those of active participants.** The `{cowork}`
Stop hook fires on EVERY turn for the whole life of a tagged instance — group or no
group — so an instance between groups (its normal state most of the time) still
drops a capture per turn. Draining by group membership would leave those
accumulating without bound, and would strand any capture that arrived just as its
group closed. So the hub iterates `group_hosting/*/outbox/` and resolves each
capture on its own merits; an unattributable one is reported as unsolicited and
consumed, which is what bounds the directory for every tagged instance.

### hub.state.json

```json
{
  "schema": 1,
  "groups": {
    "<manager-id>-<project-title>": {
      "manager": "<manager-id>",
      "coworkers": ["<coworker-id>", "..."],
      "round_budget": 6,
      "rounds_used": 3,
      "status": "active",
      "created_at": "...", "updated_at": "..."
    }
  },
  "participants": {
    "<instance-id>": {
      "groups": ["<group-key>", "..."],
      "last_transcript_entry": "<uuid of the newest turn already processed>",
      "outstanding_send": { "group": "<group-key>", "sent_at": "..." }
    }
  }
}
```

`last_transcript_entry` is the high-water mark that stops a turn being processed
twice across hub restarts. `outstanding_send` is what makes a capture attributable
and lets an undelivered message be retried — see the attribution gap below. Written
after every change, read on startup.

### File exchange: the inbox / pull-request model

One rule covers every participant, whatever its role:

> `<group>/` is yours to write. `<group>@<someone>/` is an **inbox** — what
> someone sent you, written only by the hub.

So both directions are the same operation, and `sync._deliver` is the only one
there is: copy the sender's working copy into the recipient's inbox-from-sender.
`hand_over` and `submit` just name the two directions.

1. Hub hands material over — copies the manager's working copy into the
   coworker's `<coworker-id>/<group>@<manager-id>/` — then injects a prompt.
2. The coworker takes up its inbox, then works **only inside its own**
   `<group>/` (in-container: `/cowork/<group>/`).
3. On turn completion (the Stop hook is the exact signal) the hub copies the
   coworker's files into `<manager-id>/<group>@<coworker-id>/`.
4. The hub notifies the recipient that an inbox has arrived.
5. The recipient diffs the inbox against its own copy and decides what to merge.

**Nothing the hub copies ever lands on a dir its owner writes.** That is what
makes the handover non-destructive in *both* directions: a coworker's unsubmitted
edit survives a fresh hand-over, and a manager's canonical copy survives a
submission. An earlier draft wrote a hand-over straight into the coworker's own
`<group>/`, which silently destroyed work in progress; the symmetric inbox removes
the failure mode rather than documenting around it, and costs the coworker one
merge step in exchange for a diff of what actually moved upstream.

The separator is `@`, not `-`: group names are `<manager>-<project>`, so `-` made
an inbox name ambiguous with the group dir of a project whose title happened to
end in `-<sender>`. Nothing composes a group name with `@`, so no inbox name can
collide with a group name — the two are siblings in one directory, which is why it
matters. (Discovery still keys on `session.json` presence, not on the name.)

**Staleness replaces loss, and is only partly detectable.** A recipient that
ignores an inbox works on old material. `Delivery.changed` — what differs from the
recipient's own copy, computed at delivery time — is the reliable signal and the
one to announce. Afterwards, only *absence* can be judged honestly:
`sync.not_taken_up` reports files the recipient never picked up at all. A
difference check cannot work, as the round-trip test showed — the moment a
recipient takes a file up and improves it, its copy differs from the inbox by
definition, so "differs" fires on every healthy round. Telling a considered
revision from ignored material needs a merge base, i.e. version control, which is
outside what a copy plane should become.

A plain `diff -r` is **not** the right command, as testing the round trip showed:
a working copy legitimately holds `session.json`, `conversation.md` and
`messages/`, none of which are ever sent, so every round reports three phantom
`Only in ...` entries around the one real change. The exclusions belong
with the set that defines them — `sync.review_command` builds the invocation
(`diff -r -x conversation.md -x messages -x session.json <working> <inbox>`) from
`sync.HUB_OWNED`, and the notification quotes it verbatim so the manager never
composes it by hand.

It emits **container** paths, not the host paths the hub copies between: the
command is addressed to an agent inside a container, where a host path does not
resolve at all. The translation is exact rather than a guess — a working copy and
an inbox are both direct children of the participant's dir, which that participant
sees as `/cowork` — but it is easy to miss, because every other path in the file
plane is a host path and the bug is invisible until an agent runs the command.

`diff`, `diff3`, `patch`, `cmp`, and `git` are **already in the base image** (GNU
diffutils 3.10), so no `_cowork` image layer is needed for this. `rsync` is
absent, but the hub runs host-side and uses plain copies.

**Inbox retention is the recipient's call.** The hub neither auto-deletes nor
auto-versions an inbox — different tasks want different handling, so the policy
lives with the reasoning agent. BOTH addendums should *encourage clearing an inbox
once merged*, because a stale inbox is indistinguishable from a fresh one on the
next round and is the obvious source of future confusion.

The canonical copy is therefore only ever written by the manager, deliberately.
Two coworkers submitting the same file land in two separate inboxes rather than
clobbering each other, and the merge decision stays with a reasoning agent rather
than a copy rule.

**Not covered, by design:** simultaneous editing of one file by two participants,
and a coworker seeing the manager's tree change mid-turn. Handing a file out while
a previous version is still being edited is a protocol matter for the manager —
sequence the handovers — not an architectural one.

---

## `{cowork}` changes

In place and tested: the Stop-hook capture, `permissions.defaultMode: "dontAsk"`,
a read-write allowlist, and the 11-bullet protocol addendum.

### `dontAsk` makes the allowlist exhaustive — fixed

Found the hard way: a `{cowork}`-tagged agent was **unable to edit any file**,
because `dontAsk` turns `permissions.allow` from a convenience list into the
*complete* set of permitted tools — anything unlisted is auto-denied rather than
prompted. The first allowlist was read-only (chosen to stop permission *prompts*,
with a reviewer in mind), so it became a hard ceiling for every participant, and
it would have made the inbox/`diff`/merge flow above impossible: a manager cannot
merge without writing.

Read-only-ness belongs to `{ro}`, which exists for exactly that. `{cowork}` must
not impose it.

- **Done:** `Write`, `Edit`, `Bash(mkdir:*)`, `Bash(cp:*)`, `Bash(mv:*)`, and
  `Bash(diff:*)` added to `_cowork`.
- **Do not put `{ro}` on an ordinary coworker.** A coworker has its own workspace
  and its own `cowork/<group>/`, and may well want to draft or experiment with
  files before answering — `{ro}`'s `deny` is *global*, not path-scoped, so it
  would block that and also block submitting anything at all.
- **`{cowork}` + `{ro}` is a distinct, narrower role: a silent reviewer.** It can
  read and reply but cannot produce a single file, so it never submits to an
  inbox. Verified: `Write` lands in both lists and `deny` wins. Useful for pure
  review; wrong as the default.
- **Remember each `Bash` subcommand is matched independently** — Claude Code
  splits on `&&`, `||`, `;`, `|`, `|&`, `&`, and newlines, so a pipeline needs
  every part allowed. This is why the protocol addendum steers agents to
  `Glob`/`Grep` over shell pipelines.

### How fragments compose (verified)

- **Multiple policies merge.** `policies` is a list axis: every selected policy's
  fragment merges, plus every `always_on` policy, plus each specialty-claimed
  fragment. Nothing is one-at-most.
- **A specialty claims at most ONE fragment, matched by its own name.**
  `policy_fragments.get(tag_dir.name)`, so `{cowork}` claims `policy/_cowork` and
  `{ro}` claims `policy/_read-only`. `{ro}` cannot also claim `_cowork` — but it
  does not need to, because ticking both tags merges both fragments.
- **A future `silent-reviewer` role** is therefore best done as a nested specialty
  (`agents/specialty/cowork/reviewer/`) claiming its own `policy/_reviewer`
  fragment that denies writes — one tag to tick instead of two, once the nesting
  change lands.

### Other changes

1. **Hook path** → `/cowork/outbox/` (drop the `mkdir -p`; the new
   per-participant mount provides it, and this makes captures durable).
2. **One mount to add** — `group_hosting/<id>/` -> `/cowork/`.
3. **Addendum** → document `/cowork/<group>/` as this agent's working
   copy for a group, that it is the *only* place it may write group files, that
   submission happens automatically on turn completion, and that
   `conversation.md` can be re-read to recover context after a restart. Keep the
   existing "never write your reply into a file" rule.

---

## `{manager}` — full description

**Kind:** specialty **nested inside** `{cowork}` — `agents/specialty/cowork/manager/`.

**Decided: real specialty→specialty inheritance.** `requires` is *derived*, never
declared (`base.py`: "There is no `requires` key in any manifest"), and today
`Specialty.scan` is a flat `sorted(root.iterdir())`, so nesting is currently
ignored. The change is small because the helper already exists: `walk_tag_tree`
yields `(tag_dir, ancestor_names)` depth-first and already handles `_`-dir
skipping and the strict "bare dir without `tag.info` raises" rule — professions
use it. `Specialty.scan` adopts it and sets `requires=frozenset(ancestors)`,
roughly a three-line change to one function.

Nothing assumes specialties are flat: the form and registry iterate
`specialties.values()` and consume `requires` generically, which is how nested
professions like `webdev` already work. The form's check-cascade then comes free —
ticking `{manager}` auto-ticks `{cowork}`, unticking `{cowork}` unticks
`{manager}`. Every future role (`tester`, `bugfixer`, `recruiter`) inherits the
same way.

**What the tag grants.** `{manager}` is the *host* of a group: its instance id
supplies the `<manager-id>` half of the group key, and it owns the canonical
working copy plus one inbox per coworker. It is the only participant whose
control requests the hub honours.

**Roles live in the group's state files, not in mount topology.** `session.json`
records who is the manager and who the coworkers — that one file is the roster, so
a manager can list its past cowork sessions and re-establish one with the same
participants. (An earlier draft had a separate team-data file beside it; there is
nothing for it to hold that `session.json` does not.) The hub reads that state plus
the requester's tags when deciding whether to honour a control request. Tag as
marker, hub as gate.

**The manager names the project.** It supplies `<project-title>` — a
space-free identifier such as `edge_case_tests` — which the hub combines with the
manager's instance id into the group key. The same participants may therefore be
convened again as a *separate* team for a different task, distinguished purely by
that title, with its own directories and its own log.

**Control verbs** — implemented in `cowork/control.py`. A request is a file in
the manager's own `/cowork/control/` (first line = command, remaining lines =
body); the hub consumes it, writes a reply under `control/replies/`, and injects
an UNTAGGED pointer at it (untagged so the acknowledgement drains as unsolicited
instead of being routed). Every group name is explicit, because a manager can
host several groups and `control/` is one dir at the participant root:

| Verb | Meaning |
|---|---|
| `roster` | list recruitable peers, asker excluded |
| `recruit <project> <peer>...` | create or extend the group `<asker>-<project>`; body = the task |
| `send <group-key> <peer> [+files]` | deliver the body; `+files` hands the working copy over first. Consumes a round |
| `release <group-key> <peer>` | drop a peer; its dirs stay on disk |
| `done <group-key>` | close the group; files and conversation.md are kept |

Two gates, layered: the requester must be `{manager}`-tagged (a non-manager's
request is parked unread — answering would invite probing), and the group-scoped
verbs additionally require the requester to HOST the named group, or a
manager-tagged peer recruited into someone else's group could spend that group's
rounds. A just-modified request file is left for the next pass
(`is_file_recent`), since an agent's Write is not atomic and half a command must
not be parsed as a whole one.

**Addendum** (in `manager/tag.info`, composed after `{cowork}`'s by chain order):
teaches the request format, every verb, the working-copy and inbox layout, and
the two protocol duties — clear a merged inbox, sequence handovers. A drift test
(`test_manager_addendum_teaches_the_control_channel`) fails if the documented
paths or verbs fall out of sync with `control.py`.

---

## Hub changes

1. **Root rename** to `group_hosting`, per-group subdirectories keyed
   `<manager-id>-<project-title>`.
2. **Plain file IO instead of `docker exec`** for all data movement — the
   per-participant mount makes both ends host-side paths. Injection stays as the only
   docker dependency, and it is the wake, not the transport.
3. **Durable state — `hub.state.json`.** Today the roster, pairings, round
   counter, and message sequence live only in memory and CLI args, so a hub
   restart forgets everything mid-session. Persist:
   - known groups and their participants
   - per-group round counter and budget
   - message sequence number
   - last-drained marker per participant
   Written after each change, read on startup, so a restarted hub rejoins its
   groups instead of starting blank.
4. **Roster service** — answer `roster` from the same signal the picker uses for
   `(RUNNING)`: list containers, match the `claude-code_` prefix in Python, then
   confirm each carries the `{cowork}` hook. Two requirements follow from
   `{manager}` nesting inside `{cowork}`:
   - **Other `{manager}` instances are listed as candidate coworkers.** They are
     `{cowork}`-capable by inheritance, and a manager-tagged peer is one of the
     most capable coworkers available — filtering by role would forbid the best
     candidates.
   - **Self-exclusion belongs here, and needs the asker's identity.** The
     requesting manager passes its own id and the roster omits it. That is why
     `Session.with_coworker` treats a self-recruit as a silent no-op rather than
     an error: the roster is the layer that prevents it being offered, so the
     state layer only has to refuse to record it.
5. **Round budget per group**, read from `session.json`, so a runaway exchange
   stops without the hub being restarted.
6. **`conversation.md` per group** rather than one global log.
7. **Tag-gated control requests** — honour `roster` / `recruit` / `done` only from
   an instance whose tags include `{manager}`; ignore them from a plain
   `{cowork}` peer.
8. **File exchange** — hand over into the coworker's `cowork/<group>@<manager-id>/`
   inbox, submit back into the manager's `cowork/<group>@<coworker-id>/` inbox on
   turn completion. Neither direction writes where its recipient works.
9. **Membership is enforced on every send and every file move.** The hub carries
   traffic only between members of a group. Without it a typo'd recipient wakes an
   uninvolved instance, stages a message into a tree with no business holding one,
   records the traffic as group history, and burns a round — all reported as
   success. The file plane matters more than the message plane here: a hand-over to
   a non-member copies the manager's working copy into that instance's `/cowork`
   mount, **which it can read**, and refusing the message afterwards does not take
   it back. So the check is consulted BEFORE any file moves
   (`relay.membership_problem`), and `sync._deliver` additionally RAISES on a
   non-member as the invariant of its own layer.

---

## The flow

**Phase A — copy-based exchange, one launcher change.**
Each `{cowork}` participant is launched with its own
`group_hosting/<id>/` mounted at `/cowork/`. Because that
mount is generic per instance, a participant can then join any number of groups
without relaunching again.

1. Human launches a `{manager}` instance (which implies `{cowork}`) and one or
   more `{cowork}` peers — or simply uses ones already running.
2. Manager writes a `roster` request into its own `control/` dir → the hub
   injects the list of online cowork-capable peers.
3. Manager writes `recruit <peer>` → the hub records the group in
   `hub.state.json`, creates `groups/<group>/`, and seeds each participant's
   `<group>/` dir.
4. Manager sends work. The hub writes the message into the peer's `<group>/` dir
   (plus any checked-out files) and injects a one-line pointer, tagged with the
   group key so the reply can be attributed.
5. The peer works in its own `<group>/` dir. Its Stop hook fires on turn
   completion; the hub reads the capture, pairs it with its prompt via the
   transcript, appends both sides to `conversation.md`, copies submitted files into
   the manager's `<group>-<peer>/` inbox, and notifies the manager.
6. Round budget or a `done` request ends the session; the hub marks it closed in
   `session.json` and appends a closing entry to `conversation.md`.

**Phase B — hub launches participants (true shared workspace).**
Phase A's file exchange is copy-based, so two participants cannot edit one file
at once. Genuine co-editing needs the *same* directory mounted into several
containers, and a mount is launch-time only — so the hub must start the
participant itself, reusing `run_container` so tags, engine conf, and mounts come
from the launcher rather than being re-implemented. That also lets `recruit` wake
an instance that was not previously running.

Phase A is worth shipping first because it needs **no launcher change at all**,
while Phase B turns the hub into a launcher client.

---

## Resilience

| Survives a restart | How |
|---|---|
| Instance transcript + memory | the per-instance state dir; a relaunch resumes via `--continue` (confirmed in testing) |
| Discussion log | `conversation.md`, host-side and append-only |
| Joint work product | every participant's `<group>/` dir under `group_hosting/` |
| Group membership, rounds | `hub.state.json` |
| Task statement | `session.json` |

Does **not** survive: an in-flight injection, and the pty attach itself. A
message being typed when the hub dies is lost — the hub should mark a message
delivered only after the injection returns, so a restart can retry.

A relaunched participant keeps its working copy, because it lives on the host under
`group_hosting/`, not in the container. For discussion context the hub
can copy `conversation.md` into that dir on request; `groups/` itself is not mounted,
since it is hub-owned.

---

## Hub lifecycle

`run.py` ensures the hub is up when it launches a `{manager}` instance. But the
hub **must not be a child of `run.py`**, and one edge case settles that on its own:

> Manager A is running, so the hub is up. Manager B launches. If the hub were A's
> child, closing A would kill B's routing mid-session.

`run_container` blocks for the container's whole lifetime (`shell_returncode` →
`subprocess.run`), so `run.py`'s lifetime *is* the instance's lifetime. A child
hub would therefore die with whichever manager happened to start it. So:

**The hub is a detached, global singleton** — one per host, not one per manager,
not one per group. Two hubs draining the same outboxes would each consume half the
captures, so singleton-ness is a correctness requirement, not tidiness.

### Rules

- **Ensure-running, not start.** `run.py` checks a pidfile at
  `group_hosting/hub.pid` *and* whether that pid is alive (a stale pidfile after a
  crash or reboot must not block startup), then spawns only if needed — after the
  image build, right before `docker run`, so the hub's managerless grace only has
  to cover seconds of container startup rather than a whole build.
- **Detach it — via `sh`/`nohup`, reparented to init, NOT a plain
  `start_new_session` Popen.** Implementation found the direct-child version has a
  dark corner: a child the launcher never waits on becomes a ZOMBIE when it dies,
  and `os.kill(pid, 0)` — the liveness probe — succeeds on zombies, so a crashed
  hub read as "already serving" for as long as the manager's run.py lived, with no
  new hub startable. The spawn therefore goes through a short-lived `sh` that
  backgrounds the hub (`nohup … & echo $!`) and exits, reparenting the hub to
  init, which reaps it the moment it dies. `nohup` rather than `setsid` because
  macOS has no `setsid` binary. Output appends to `group_hosting/hub.log`
  (spawned with `python -u`, or block-buffering would leave `tail -f` — the
  watch-the-team view — trailing kilobytes behind reality).
- **It exits when the last manager goes.** It does not die with *a* manager (that
  would break the two-manager case), but it does not linger either:
  `lifecycle.ManagerWatch` polls `roster.running_managers` each pass and the hub
  shuts down — removing its pidfile, printing the reason — once no manager has
  been running for `MANAGERLESS_GRACE_SECONDS` (60s). Time-based rather than
  pass-counted so tuning the poll interval cannot change its meaning; a
  docker-unreachable probe resets the clock rather than counting as "all gone",
  and the grace also covers close-and-relaunch thrash. `run.py` respawns the hub
  on the next manager launch, so exiting is free. `--once` drains skip the watch.
- **Skip on `--dry-run`.** Dry-run projects a launch; it must not start a daemon.
- **Trigger on `{manager}` only.** A lone coworker has nothing to route to, and
  because outboxes are durable its captures simply wait. Starting on any
  `{cowork}` launch would spin up a hub with no group to serve.

### Edge cases and what happens

| Situation | Behaviour |
|---|---|
| User closes a manager, relaunches later | Hub stays up. Group state persists in `hub.state.json` / `session.json`; the relaunched instance resumes its own conversation via `--continue` and the hub keeps routing. Pending inboxes persist in the state dir. |
| Two managers at once | One hub serves both. Closing one leaves the other's routing intact. |
| Hub crashes or the host reboots | **Nothing is lost but in-flight injections.** Captures accumulate in the (now durable) outboxes; on next start the hub reads `hub.state.json` and drains the backlog. This is the payoff for moving the hook output into the state dir. |
| Last manager closed, coworkers still up | Hub exits and clears its pidfile — there is nothing left to route to. Coworker captures keep accumulating durably and are drained whenever a hub next runs. |
| Two `run.py` calls race to start it | Atomic pidfile creation (`O_EXCL`); the loser exits without complaint. |
| Hub alive but stale pidfile from an old crash | Liveness-checked, so the file alone never blocks a restart. |

A message being typed when the hub dies is the one real loss — so the hub should
mark a message delivered only *after* the injection returns, letting a restart
retry rather than silently drop it.

---

## Code layout: `launch/cowork/`

Where the `poc/` scripts land once promoted. Modelled on `launch/quickie/`: a thin
root entry script delegating to a package, one concern per module.

### What must stay OUTSIDE the package

- **`launch/docker_config.py`** — gains `docker_attach_inject(container, text, …)`
  (the pty-attach injection, `container_tty_size`, the `TIOCSWINSZ` sizing). That
  module documents itself as the single home for *every* docker-CLI touchpoint, and
  `docker_running_instances_subprocess` already lives there. Injection is a docker
  call, so it belongs there, not in `cowork/`.
- **`launch/paths.py`** — gains the path builders (`group_hosting_dir`,
  `instance_cowork_dir`, `group_key`, `cowork_outbox_dir`, `hub_state_file`),
  alongside the existing `instance_state_dir_path` family. Paths are that module's
  job; no other module should compose them by hand.
- **`agents/specialty/cowork/`**, its nested `manager/`, and
  `agents/policy/_cowork/` — the tags and the settings fragment.
- **`cowork.py`** at the root — the entry script (mirrors `quick_question.py` →
  `launch.quickie.main`), so `launch/cowork/` is never run directly.
- **`launch/tests/test_cowork.py`** — mirroring `test_quickie.py`.

### The package

| Module | Owns | Notes |
|---|---|---|
| `__init__.py` | `main(argv)` entry + the public surface | same shape as `launch/quickie/__init__.py` |
| `cli.py` | argparse, subcommands, dispatch | `serve` (the hub loop), `send`, `roster`, `status`. `-h` is this tool's help, not `claude`'s |
| `roster.py` | discovery — who is cowork-capable, reachable, and how committed | `agents_crud.instance_from_store` for tags + `docker_running_instances_subprocess` for liveness; no docker calls of its own. Takes the asker's id so it can omit it, and names running instances that would qualify after a relaunch |
| `group.py` | group identity and durable state | `group_key`, `session.json`, `hub.state.json` load/save. The only writer of hub state |
| `mailbox.py` | the message plane | stage an inbound message into a participant's `cowork/<group>/`; drain Stop-hook captures from `cowork/outbox/` |
| `sync.py` | the file plane | one symmetric `_deliver`; `hand_over` and `submit` name the directions. Also owns `review_command` — the `diff` invocation the notification quotes, since the exclusions must track this module's `HUB_OWNED` set |
| `journal.py` | `conversation.md` | append-only, both sides, one file per group |
| `relay.py` | the hub loop and routing policy | round budget, whose turn is next, done-detection. Calls every module above; contains no IO of its own beyond orchestration |

### Where the PoC pieces went

`poc/` is gone. Recorded because the mapping explains why several modules are
shaped the way they are:

| From the deleted `poc/` | Landed in |
|---|---|
| `inject_poc.inject`, `container_tty_size`, `_set_winsize` | `docker_config.docker_attach_inject` / `container_tty_size` / `_match_container_winsize` |
| `town_square_hub.drain_outbox` (minus the `docker exec`) | `mailbox.read_captures` |
| `town_square_hub.Conversation` | `journal.py` |
| `town_square_hub.serve` + routing | `relay.serve` / `relay.poll_once` |
| `live_agent.running_instances` | already in `docker_config.docker_running_instances_subprocess`; `roster.py` consumes it |
| `town_square_hub.parse_request`, argparse | still to build, in `cli.py` |
| `live_agent.LiveAgent` + `STREAM_FLAGS` | **dropped** — only Phase B (hub-launched agents) would want it, and there `run_container` supersedes it |
| `wake_poc.py`, `relay_poc.py` | dropped; their cases are covered by `relay` plus injection |

`_put_file`, the `docker exec` draining, and the pointer-staging machinery all
disappear with the per-participant mount — they exist only to move bytes across a
container boundary that turns out already to be bridged.

---

## Human "watch the team" view

Skip tmux. Its capture path would be a **downgrade**: `capture-pane` yields
ANSI-laden full-screen redraws with no turn boundaries, whereas the Stop hook
already gives clean text with exact boundaries, `session_id`, and timestamps —
the hook *is* the log. The same objection applies to `script`.

Instead: `conversation.md` is append-only, so `tail -f` is already a live view.
If that wants polish, a small optional `--follow` renderer that pretty-prints it
costs a few lines against tmux's image layer plus changed entrypoint.

tmux keeps two narrow advantages worth revisiting only if they start to matter:
`send-keys` avoids sharing the human's TTY (today's injection shares it, so a
human typing at the same moment can interleave), and multiple panes would let one
watch several agents live.

---

## Gaps found reviewing this plan

### 1. Capture attribution — why the hook cannot bind to a group by itself

A Stop-hook capture carries `session_id`, `cwd`, `transcript_path`,
`last_assistant_message` — and **nothing identifying a group**. `cwd` is
`/workspace` for every participant.

To be clear on the channel question: **the hook already is one channel per instance**
— each participant has its own outbox in its own mounted dir. The ambiguity is
*within* an instance: a coworker in two groups has one Claude session, one Stop hook,
one outbox, and every turn lands there unlabelled.

**Can the hook be given the project title so it writes straight into
`<instance-id>/<manager-id>-<project-title>/`?** Not at launch, for a concrete
reason: the hook command is a fixed string baked into `settings.json` by
`install_settings` *at launch*, and **groups do not exist yet at that point** — they
form later, when a manager recruits. Rewriting `settings.json` mid-session does not
help either, since Claude Code reads settings at startup.

It *can* be done at runtime, because the hook command is a shell command and can
compute its destination:

```sh
G=$(cat /cowork/.active_group 2>/dev/null || echo outbox); rm -f /cowork/.active_group
mkdir -p "/cowork/$G" && cat > "/cowork/$G/capture-$(date +%s%N).json"
```

The hub writes `.active_group` before injecting; the hook consumes it. Consuming
rather than merely reading is what makes it safe-ish: a later unsolicited turn finds
no marker and falls back to `outbox/`. Combined with the hub serialising sends per
peer — one outstanding send at a time, which the protocol's no-interleaving rule
already wants — this routes correctly for hub-driven turns.

**When would the hub write it?** Immediately before injecting — after staging the
group's files, before typing the prompt — and atomically (temp file plus `rename`)
so the hook can never read a half-written marker.

But the write moment is not where the risk lives. The marker is **live for the whole
turn**, which can run minutes, and any turn that completes in that window consumes
it. What actually matters is whether a turn was *already in flight* when the hub
wrote it:

- Human types `Q`; the agent starts on it. Hub then writes the marker and injects
  `P`, which queues behind `Q`. `Q` finishes first and takes the marker, so `Q` is
  filed as a group reply and `P`'s reply is left unattributed. **Both wrong.**
- Human types *during* the hub's turn: harmless. Prompts queue and complete in order
  (verified), so the hub's turn consumes the marker correctly and the human's turn
  finds none.

The hub can partly guard by reading the transcript before injecting — if the newest
entry is a user turn with no assistant reply yet, a turn is in flight, so wait. That
check is itself racy (the human can type in the gap), which is the point: the marker
is best-effort by nature.

Two residual holes therefore remain, and they are why the marker is an optimisation
rather than the mechanism:

- **Human-typed turns.** Confirmed by observation: the test instance had two captures
  waiting before the hub sent it anything — replies to prompts the user typed. If a
  human turn completes while a marker is pending, it consumes the marker and is filed
  as a group reply.
- **Queued prompts.** If two sends do overlap, whichever turn finishes first takes
  the marker.

**So attribution is settled by pairing the reply with its prompt, via the
transcript — and the pairing is an exact join, not a positional guess.** Verified
against a real transcript: entries carry `promptId`, `uuid`, `parentUuid`, and
`timestamp`, and **the Stop-hook payload's `prompt_id` matches the transcript's
`promptId`**. One promptId groups the prompt, every tool-result entry, and every
assistant entry of that turn — so the hub looks up `prompt_id`, takes the user entry
that is not a tool-result echo, and reads the group tag out of its text. No walking
the chain, no matching on reply text, no ordering assumption anywhere.

Two field-level notes from that check:

- **`isSidechain` marks subagent turns**, so they can be excluded — which
  `_transcript_turn` already does.
- **`promptSource` records how a prompt arrived, but does not help here:** an
  injected prompt is literally typed, so it reads `typed` exactly like a human's.
  The group tag in the prompt text is therefore still what distinguishes hub traffic
  from human traffic.

Deterministic rather than inferential:

- A human-typed turn carries no tag → recognised as unsolicited, logged, not routed.
- Overlapping prompts still pair correctly, because the ordering comes from the
  transcript itself rather than from the hub's bookkeeping.
- `last_transcript_entry` in `hub.state.json` is the high-water mark, so a restart
  neither reprocesses nor skips.

**This is not FIFO, even though the outbox is one channel.** FIFO would mean
"the Nth capture answers the Nth message I sent" — an *inference* from ordering, which
a single human-typed turn breaks by inserting a capture the hub never sent and
shifting everything after it. Transcript pairing makes no ordering assumption at all:
each capture carries `transcript_path`, and the group is read out of the paired user
turn's own text. Every capture resolves independently.

The clearest way to hold it: **the hub advances through the transcript, not through
the outbox.** The outbox is a doorbell plus a pointer — "a turn finished, here is
where to look" — and the transcript is the ordered, self-describing record. So the
outbox needing no per-group channels is not a compromise; its ordering simply carries
no meaning. Two turns completing between polls attribute correctly whichever capture
the hub happens to read first, because each transcript entry has its own preceding
prompt. `last_transcript_entry` is a transcript position, not a capture count.

The machinery already exists: `file_access._last_text_turn` / `_transcript_turn` read
exactly these JSONL transcripts, skipping tool-result echoes, sidechains, and
malformed lines.

Given that, the simplest build is **hook drops into `outbox/`; hub attributes, then
files the record into the group dir.** The `.active_group` marker stays available as
a later optimisation if having the hook pre-sort ever proves worth the two caveats.

`outstanding_send` remains useful, but for delivery rather than attribution: a send
with no matching turn after a timeout means the injection did not land, so retry or
alert.

### 2. Context bleed across groups

File separation is clean (sibling `cowork/<group>/` dirs), but a coworker has **one
conversation thread**. Two groups therefore share its context even though their
files do not — group B's discussion is visible while it works on group A. Options:
have the message prefix name the group (cheap, partial), limit a coworker to one
active group at a time (simple, restrictive), or accept the bleed and document it.
Not decided.

### 3. Atomic writes

The hub writes into `cowork/<group>/` while the agent may be reading it — a
half-written file can be read as truncated. All hub writes should go to a temp name
in the same directory and be `rename()`d into place; same filesystem, so the rename
is atomic. Cheap, and invisible to add later only if remembered.

### 4. Write confinement

With the dedicated mount the hub no longer writes anywhere near `settings.json`,
`CLAUDE.md`, or the transcripts — a real gain over the state-dir squat. Still worth
a guard, since group keys and instance ids come from disk: every hub write should
assert its resolved path is under
`group_hosting/<id>/`, and never composed from unvalidated input.

### 5. Deleting a participant mid-group

`delete_instance` removes the instance's state dir and store entry. With the
dedicated mount its group work now *survives* under `group_hosting/`,
which is better than losing it — but it leaves an orphan directory for an instance
that no longer exists, and a group referencing a dead participant. The hub must
tolerate a vanished participant, the delete flow should warn when the instance
belongs to an active group, and the audit should report the orphan. Note the
existing running-instance guard does not cover this: a *stopped* instance can be
deleted while still a group member.

### 6. Audit integration

`python -m launch.audit` is this project's convention for reporting state
inconsistencies, and the new on-disk state deserves the same treatment: groups
whose `session.json` names instances that no longer exist, inboxes for departed
coworkers, a stale `hub.pid`, and captures left undrained because no hub ran.

### 7. Test plan

Nothing here says how this gets tested, in a project with 784 passing tests.
Pure and therefore unit-testable: path builders, group key composition,
`session.json` / `hub.state.json` round-trips, roster filtering, inbox naming,
journal formatting, request parsing, FIFO attribution (including the unsolicited
case). Needing a fake-docker shim, as `test_docker_config` already does: injection
argv assembly and roster liveness. Genuinely untestable without a live agent: that
the TUI accepts an injected keystroke, and that the Stop hook fires — both already
verified manually and worth re-verifying after any Claude Code upgrade.

### 8. Smaller notes

- **Cost visibility.** Each hop is a full turn; a 3-participant group with a
  6-round budget is 18+ turns. The capture payload has no usage figures, but it
  does carry `transcript_path` — already needed for attribution — so the hub can read per-turn usage from there and
  record it in the journal.
- **No migration needed for the rename.** `install_settings` rewrites
  `settings.json` on every launch, so relaunching a participant picks up the new
  hook path automatically — no `migrations.py` entry required.
- **Docs.** `README.md`, `.claude_summary`, and `.claude_dev_guidelines` all need
  updating when this lands; the guidelines especially, since `launch/cowork/`
  introduces a new layer whose boundaries future contributors will need.

---

## Decisions still needed

Settled: `{manager}` nests inside `{cowork}` with real specialty→specialty
`requires`; the name is `{manager}`; Phase A ships first.

Also settled: the manager chooses `<project-title>` (space-free); role and roster
data live in the manager's own `session.json`, which is what lets it re-establish a
past session. Inbox retention is the manager's call, with the addendum encouraging
it to clear a merged inbox. The hub is started by `run.py` (ensure-running) when a
`{manager}` launches, and exits when the last manager goes.

Still open:

1. **Context-bleed mitigation** (gap 2): rely on the protocol's no-interleaving rule
   alone, or additionally cap a coworker at one active group at a time? `roster`
   now reports each candidate's active-group count and sorts the least-committed
   first, so this can be settled with evidence rather than guessed — and a manager
   already has what it needs to avoid overloading a peer unaided.

Settled since: **roster breadth**. `roster.survey` enumerates every
cowork-capable instance from `instances.toml` and marks each `running`, rather
than filtering to online-only. Whether a manager is shown stopped peers is a
PRESENTATION decision — `roster.reachable` filters when a caller wants only what
the hub can wake right now — and the two answers genuinely differ: "recruit this
one now" versus "this one exists, start it first". Discovery reports; it does not
decide. `liveness_known` is carried separately so a failed `docker ps` cannot be
misread as "everyone is offline".

### Build order

The first slice is **not** divisible: the mount and the hook path must land together.
A hook writing to `/cowork/outbox/` with no mount writes into the container's
ephemeral layer; the mount with the old `town_square` hook path captures nothing. So
slice one is: the mount in `set_container_mounts`, the hook path in `_cowork`, the
`paths.py` builders, and a relaunch — verified against a live instance before
anything else is built on it.

Then, in order: `group.py` state and discovery-by-`session.json`; attribution via
`prompt_id`; `journal.py`; `sync.py`; `relay.py`; the `{manager}` tag last, since its
`Specialty.scan` change is the one edit that touches **every** specialty and wants
the 784-test suite green before and after, in isolation.

### Planned, sequenced after the `{auto}` pass

**A `/cowork [<instance-id>]` slash command, available to managers only.** Lists
cowork-capable instances so the user can pick a peer, or see that none are
available and a new one is needed.

**Manager-only scoping needs no new mechanism.** The shared `custom_commands/` dir
is mounted into *every* container, so a command placed there would be visible to
all instances. Instead `{manager}` ships its own via `tag.docker`, exactly as
`{firewall}` ships its scripts — relative mount sources resolve against the tag's
own directory (verified):

```toml
# agents/specialty/cowork/manager/tag.docker
[run]
mounts = ["commands/cowork.md -> /home/claude/.claude/commands/cowork.md:ro"]
```

The file lives in the tag, so the command exists only for instances carrying the
tag. Docker resolves the nested target inside the existing `~/.claude/commands`
mount by depth — the same nesting the launcher already relies on when it mounts
`settings.json` inside the instance state-dir mount.

**Where its content comes from.** A slash command is a prompt template, not a
program, and the agent has no docker access — so it cannot enumerate containers
itself. The hub therefore maintains a roster file in each manager's `cowork/` dir,
and `cowork.md` instructs the agent to read and present it. Agent stays
docker-free; the hub stays the single source of truth about who is online.

Deliberately sequenced *after* the `{auto}` issue below: a roster command is much
less useful if the permission model underneath it is still untested.

### Known issue, deliberately deferred

**`{auto}` masks the permission model.** Its `--dangerously-skip-permissions` is a
CLI flag, and CLI beats settings, so an instance carrying both `{auto}` and
`{cowork}` ignores `dontAsk` and the allowlist entirely — every tool is permitted.
That is convenient while building (it is how the plan's own edits got made) but it
means the permission design is untested in practice, and any gap in the allowlist
stays hidden until `{auto}` comes off. Needs a pass without `{auto}` before the
feature is called done. Not urgent.

## Live validation (2026-08-11)

What the shipped machinery has PROVEN in real use — recorded here because the
circumstances will change (this is the pre-socket-era baseline: Claude Code
**2.1.226**, pty injection as the only ingress, hub on the host, coworkers in
containers). Everything below was observed on disk or in transcripts, not
inferred.

**Two complete manager→coworker engagements, one round each.** A `{manager}`
instance (this repo's workspace) recruited a researcher instance in a DIFFERENT
workspace, twice (groups `agent_comms_research`, `socket_protocol`). Each ran:
`roster` → fit/resource gate → `recruit` → one self-contained brief → a single
complete reply with a sourced artifact file → inbox merge → `done`. Both briefs
survived the no-shared-context constraint (the coworker needed nothing beyond
the message text), and both groups closed having used **1 of 6 rounds** — the
budget's headroom went unused because the protocol's "make each message carry
its weight" bullet did its job.

**The mechanics, each observed live:**

- **Attribution** by `prompt_id` → transcript `promptId` join, under the
  current `[cowork task <manager>::<project>]` marker — including across the
  format migration (messages staged by the old hub still attributed after the
  restart, per the compat regex).
- **Queue consumption**: both groups' `messages/` dirs are empty on disk after
  the replies routed — a handled message leaves the queue with no agent
  holding delete rights.
- **`+quiet` control requests**: four issued (recruit/send/done), zero
  redundant wake prompts; the reply files appeared under `control/replies/`
  and were read by polling. (Against the pre-flag hub, `+quiet` degraded
  safely: `roster` ignored it, and the send-form would have been refused —
  which is why the flag was only used once the new hub was confirmed live by
  its own tag format.)
- **File submission**: coworker working-copy → manager inbox on turn end, with
  the notification quoting the exact `diff -r` review command; merge + clear
  behaved as the addendum prescribes.
- **The grant model**: the coworker's `all-actions` policy made
  WebFetch/WebSearch/Bash run unprompted under `{cowork}`'s `dontAsk` floor —
  the same instance shape that had every research tool auto-denied before the
  grant policies existed (perm_probe group, same day).

**Specialization through the wire — a two-sided result.** A persona's
*procedure* survives cowork intact: the researcher's method (raw-markdown doc
fetching, per-claim URLs, verified-vs-inference separation, sourced null
results) expressed fully in both engagements. A persona's *voice* does not
survive competing imperatives: the poet coworker (perm_probe) answered
protocol-first and style-free under format-constrained prompts — its persona
was present in context (composition verified in `agents_crud.install_latest_md`)
but outweighed, not overwritten.

**Closes the "deliberately deferred" issue above:** the permission model has
now had its pass without `{auto}` — the perm_probe engagement characterised the
`dontAsk` floor on a grant-less coworker (unlisted tools denied; the built-in
default-allow set mapped), and the first-person probes settled
deny-beats-bypass and subagent inheritance. See ISSUES.md's cowork section for
the durable findings.

**Implemented and test-pinned but not yet exercised live** (honesty line):
the one-time `[cowork-notice]` stand-down notices, the non-member guard, and
park-on-error — all landed the same day with crash-reproduction tests, but no
real straggler has triggered them yet.
