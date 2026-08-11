---
description: Drive this instance's cowork side — groups + recruitable peers overview, inbox review, hub verbs — or hand it a quoted task and it assembles and manages the crew itself. Usage — /cowork [status|inbox|<verb> …|"<task>"]
argument-hint: [status | inbox | roster | recruit <project> <peer>… | send <group> <peer> <message…> | release <group> <peer> | done <group> | "<task for a crew>"]
---

The user invoked `/cowork $ARGUMENTS`.

Dispatch on the first word: a subcommand below runs as described; no arguments means `status`; **anything else is a task brief** — handled by the last section. Inspect with Read/Glob/Grep where available, single plain commands otherwise.

**First, one gate for everything:** if `/cowork/` does not exist, this instance was not launched with the `{cowork}` tag — say so, mention that the operator can relaunch it with `{cowork}` (or `{manager}` to host groups) from the picker's tag form, and stop.

## `status` (default)

Two halves — what exists, then who could be recruited — ending with the user's options.

1. **Groups** — each `/cowork/<group>/` directory that has a `session.json`: its status, coworkers, and rounds used/left, straight from that file. `session.json` and `conversation.md` are the hub's records — read them, never edit them.
2. **Message backlog** — files under a group's `messages/` subdir are *unhandled by definition*: the hub consumes a message once the reply to it routes, queue-style, so nothing already answered is still there. List what remains oldest-first; an old entry deserves an explanation, not just a mention.
3. **Waiting inboxes** — each non-empty `/cowork/<group>@<sender>/`: who sent it and what files it holds. An inbox left populated reads as not-yet-processed, so flag these as action items.
4. **Recruitable peers** — file a `roster +quiet` request through the control channel (your CLAUDE.md's **"Hosting cowork groups"** section is the protocol; `+quiet` because you will poll `/cowork/control/replies/` for the answer yourself, and the hub's wake would arrive as a redundant extra turn) and present the reply in full: every cowork-capable instance with its state — running, stopped (cannot be woken), or running-but-untagged (needs a `{cowork}` relaunch to take part). If your CLAUDE.md has no such section, you are a coworker: show halves 1–3 and note that the roster and all recruiting run through a `{manager}` instance.
5. **Close with the choices**: message a peer in an active group, recruit, release, `done` a finished group — or hand `/cowork` a quoted task to have this instance assemble the crew itself.

## `inbox`

Show the contents of every `/cowork/<group>@<sender>/` directory in detail (or of the one matching the second argument). Remind at the end: copy what you take up into your own working copy (`/cowork/<group>/`) before editing — the inbox is overwritten by the next delivery — then clear the inbox so the next delivery is distinguishable.

## `roster`, `recruit`, `send`, `release`, `done`

These are hub verbs. Your own CLAUDE.md is the protocol document — follow its **"Hosting cowork groups"** section verbatim (request files into `/cowork/control/`, replies under `/cowork/control/replies/`) rather than improvising the mechanics; that section, not this command, is the source of truth for the request format.

- If your CLAUDE.md has no such section, you are a coworker, not a manager — the hub gates these verbs on the `{manager}` tag. Say so and suggest `status`/`inbox` instead; don't file a request that will be refused.
- Append `+quiet` to the command line and poll `/cowork/control/replies/` for the answer (you are reporting to the user in this turn, so the hub's wake would be a redundant extra one). Quote the reply file's content back to the user.
- Before `send`, remind the user (one line) that each send to a coworker consumes one round of the group's budget.
- For `done`, confirm with the user first if any inbox for that group is still populated — closing strands nothing, but unreviewed work deserves a look while its sender can still answer questions.

## A task in quotes — assemble and manage the crew

Anything that isn't a subcommand is a brief, e.g. `/cowork "remove <feature> and check <other-feature> still works"`. The user delegated the *who* and the *how* to you — you are the manager:

1. **Roster first.** Pick the coworkers yourself: match each candidate's persona and workspace to the task, prefer instances that are RUNNING (a stopped one cannot be woken by the hub), and take as many as the task needs and no more — one is often right.
2. **Refuse to start under-resourced.** If what a *proper* plan needs is lacking — no fitting coworker, the fitting one stopped or untagged, a candidate whose permissions or workspace don't match its part, too few rounds in the budget for the shape of the work — do NOT run a degraded version and report it as the task. Lay out the plan you would run, name each gap and the user action that closes it (start this instance, relaunch that one with `{cowork}` or a permission tag, raise the budget), and stop there. Recruit only once the plan is properly resourced — a poor result delivered confidently is worse than a request for intervention.
3. **Recruit** under a short project label derived from the task.
4. **Brief each coworker per the protocol**: introduce yourself, restate the task in full (a peer shares none of your context), name exact paths, and say what you expect back. Sends are finite — every one consumes a round, so make each message carry its weight.
5. **Manage the work**: review every reply and submission, iterate where it falls short, keep at most two requests outstanding to any peer, and release a coworker whose part is finished.
6. **Land it**: when the work holds together, `done` the group (inbox check first) and report the outcome to the user — what was done, by whom, and what you merged from each inbox.

Mid-task judgment stays with you: redirect, re-scope, or swap coworkers as the work demands. The user gave you the goal, not the roster picks.
