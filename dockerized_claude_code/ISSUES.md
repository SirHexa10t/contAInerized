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

## Known issues — launcher

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

## Known issues — docs

- **`TODO.txt` lists at least one already-fixed defect** (the
  `None<instancename>` credentials banner bug, repaired during the tags
  rewrite). It is an archive of a completed pass, so it is stale by nature —
  but it reads as a task list, which misleads. Worth a header stating it is
  historical, or a prune.
