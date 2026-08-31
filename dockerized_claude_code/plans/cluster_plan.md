# cluster — cohabiting agents (design sketch)

A prospective **second collaboration mode**, distinct from `{cowork}`. Where
cowork routes messages between *isolated* agents in separate containers through
a host-side hub, a **cluster** puts N agents in **one container**, working the
**same project**, able to talk to each other **directly** — either via
Anthropic's cross-session messaging (`SendMessage`/`ListAgents`) or via an
in-container message-queue, which turn out to be complementary rather than
alternatives (see Communications).

Status: **PoC-0 built** (2026-08-12) — the model, the tags, and the multiplexer
layer exist and are tested; nothing launches a container yet. This file remains
the decision record, like `group_hosting_plan.md` and
`agent_cross_comm_propositions.md`: annotations are tagged **DECIDED**,
**PROPOSAL** (a suggested change to the idea), **OBJECTION** (a risk I think is
load-bearing), and **OPEN** (a question needing an answer before code).

What exists today:

- `agents/devteam.legoset` — the PoC template (5 members, 2 of them researchers).
- `agents/specialty/muxer/` + `agents/profession/_muxer/` — `{mux}`, which
  installs the multiplexer; **verified live against tmux 3.5a** (see the run
  model section).
- `agents/specialty/muxer/cluster/` — `{clstr}`, nested so it requires `{muxer}`;
  forced onto every member at creation.
- `launch/cluster/` — member / legoset / state / worktree / tmux / launch_plan /
  cli, plus a root `cluster.py` entry (`create`, `list`, `plan`, `script`,
  `destroy`).

What does NOT exist yet: the image build + `docker run` for a cluster (the
integration step, which `docker_config` owns), the per-member CLONE mechanism
(the decided writer-safety model — PoC-0 ships shared workspaces), the creation
form, and the message-queue.

Framing captured from the originating conversation (2026-08-11): "cohabit"
rather than "cowork"; every launch starts N instances on one project, in one
container, managed through tmux; start members "equal" and let roles/instructions
shape their dynamics, evolving the program from experiment.

---

## Why this topology, and why now

The socket research (see `agent_cross_comm_propositions.md` and ISSUES.md's
"Socket delivery" entry) concluded cross-session messaging **cannot** cross our
container barrier — same-machine discovery needs the sessions to *see the same
registration files and socket*, and container-per-agent breaks that. Every
blocker was a container-barrier problem.

A cluster **deletes the barrier**: one container, one filesystem, and the docs
sanction it verbatim — *"Two sessions inside the same container can still
message each other, including on a self-hosted runner."* So the feature we
shelved for cowork becomes usable here, on its **supported surface**: real
Claude sessions calling `SendMessage`/`ListAgents` natively, so the undocumented
socket wire format (the thing that made us say WAIT for cowork) never enters the
picture. This is the one place the feature fits.

---

## Vocabulary and on-disk layout

A new session kind sits beside instances, not inside them.

```
~/.claude-agents/
  instances.toml                      # unchanged — solo instances
  instances/<agent>__<session>/       # unchanged — one solo instance's state
  clusters/
    <session_name>/                   # ONE cluster
      cluster.toml                    #   members + per-member tag selections (parallels instances.toml)
      <agent>__<role>/                #   ONE member — a full instance state dir
      <agent>__<role>/                #   another member (role disambiguates duplicates)
      ...
```

- A **member id** is `<agent>__<role>` *within* its cluster; globally it is
  `clusters/<session>/<agent>__<role>`.
- `role` disambiguates duplicates (two `researcher`s) and names intent
  (`researcher__security`, `researcher__perf`).

> **OBJECTION — role can't be optional if duplicates are allowed.** Two
> `researcher`s with no role both resolve to `researcher__` → one dir, one
> `--name`, a collision. Fix: role is *optional only when a member is unique in
> the cluster*; the moment a second of a type is added, both need a role (or the
> launcher auto-suffixes `-1`/`-2`). Cleaner rule: **role always present**,
> defaulting to the agent name for the first of a type. It also becomes the
> messaging `--name`, so it must be unique per cluster regardless.

> **PROPOSAL — each member keeps its OWN state dir, and this matters twice.**
> Members must NOT share `~/.claude`: separate transcripts, separate memory,
> separate composed CLAUDE.md (own persona + `{clstr}` addendum), separate
> engine/model. The layout above already implies this. See the discovery
> tension in *Communications* — messaging pulls the other way, and reconciling
> the two is OPEN #3.

---

## The member is (mostly) just an instance

A cluster member reuses the whole existing agent stack — do **not** reinvent it:

- an **agent** (`agents/<name>.md` + `<name>.lego`) supplies persona, engine,
  and default tags, exactly as for a solo instance;
- a member's per-cluster tag selections live in `cluster.toml`, the direct
  analogue of `instances.toml` (`tags/store.py`), and are edited the same way —
  through the picker's tag form, post-creation;
- resolution to an `Instance` (`tags/identity.py`) is unchanged: chain,
  build steps, docker contributions, conf, claude_args.

What a cluster adds on top: the container is shared, the run model is tmux, and
one forced tag (`{clstr}`).

> **PROPOSAL — code home.** Cowork lives in `launch/cowork/` as a leaf consumer
> of the core. Cluster logic gets the sibling `launch/cluster/` — durable state
> (`cluster.toml` load/save, discovery), the member-set model, and the tmux
> launch assembly. It imports the tag/identity core and `docker_config`; nothing
> in `launch/` imports it back except `run.py`. The one genuinely shared concern
> — assembling a `docker run` and handing over a terminal — stays in
> `docker_config.py`, which already owns `run_container`; the cluster variant is
> a new function there, not a reimplementation in the cluster package.

---

## `.legoset` — a cluster template

Beside each agent's `.lego`, a cluster template names its default membership:

```toml
# agents/devteam.legoset
members = ["project-starter", "refactorer", "researcher", "bug-investigator"]
```

Each entry is a bare agent name (the `.md`/`.lego` stem, no `__session`). A
member inherits that agent's `.lego` as its starting tags, plus the forced
`{clstr}`.

**DECIDED — a legoset expresses multiplicity and default roles.** A template can
ship "2 researchers by default", each with a default role, and those defaults
appear pre-filled in the creation form where the user can change them:

```toml
# agents/devteam.legoset
members = [
  { agent = "project-starter" },
  { agent = "refactorer" },
  { agent = "researcher", role = "primary" },
  { agent = "researcher", role = "adversarial" },
  { agent = "bug-investigator" },
]
```

The bare-string form (`"researcher"`) stays as sugar for the common
`{ agent = "x" }` case. Default roles are a *starting point*, not a lock — the
form lets the user rename, add, or remove members before creation, and the
picker edits them after. Validation mirrors `.lego`: every named agent must
exist in the registry (reuse `Registry.validate_build`'s shape).

---

## The creation form — full vision, and the PoC cut

**Full vision** (later phase): like the instance form, ask project location and
session name first. Then a member-picker: the legoset's members pre-listed,
each cancellable; picking an agent adds *another* of the same type; a text field
per member for its `role`. Per-member *tag* editing is deferred to the picker,
post-creation — too tedious to set every member's fine detail up front.

**PoC cut (build this first): no form at all.** `devteam.legoset` is
instantiated with its preset members and default roles. The user gets a tmux
GUI to switch between members in one window. That is the whole PoC surface.

> **OBJECTION — the member-picker's "add another on pick" interaction is novel
> and fiddly.** The existing tag form (`gui/tag_form.py`) toggles a fixed set;
> a growing list with per-row text fields is a different widget. It is a real
> chunk of UI work and rightly sits *after* the PoC — but flag it as its own
> milestone, not a footnote to "add a form".

---

## The run model — one container, tmux, N sessions

This is the largest new piece, because today's run model is single-headed.

**Today (`docker_config.run_container`):** the launcher execs one foreground
`docker run --rm -it --name … <image> claude …` and hands the terminal to that
one `claude`. One container, one TTY, one session.

**Cluster:** one container, one image, an **entrypoint that starts tmux** with
one window per member, each window running that member's
`claude --name <role> …` with its own engine env and state-dir mount. The
launcher attaches the user's terminal to the tmux session; window-switching
moves between members.

```
docker run -it --name <cluster-container> <image>  \
    → entrypoint: tmux new-session -d -s cluster \
                  ; tmux new-window -n <role_1> 'claude --name <role_1> ...' \
                  ; tmux new-window -n <role_2> 'claude --name <role_2> ...' \
                  ; ... ; tmux attach
```

- **Bottom banner = the multiplexer's status bar**, which lists windows/tabs
  natively — that *is* "list the available agents", styled. Distinct from the
  in-session Claude Code statusline (`claude_code_config.build_status_line`),
  which each member's own pane still shows; that could be extended to read "you
  are `<role>`; siblings: …". Two banners, two jobs.

### Multiplexer options — RESEARCHED (2026-08-12)

Researched by the researcher coworker (group `multiplexer_research`; full sourced
findings in that closed group's `multiplexer.md`, GitHub-API-measured health, raw
files grepped for features). The verdict: **tmux now, herdr as a flagged
parallel prototype to watch.** The decisive axis turned out to be per-pane env
(quality b) and a scriptable banner (quality c), plus dependency health (g).

| Option | Category | Verdict |
|---|---|---|
| **tmux** ✅ | in-container multiplexer (C, ISC) | **Ship for PoC-0.** Wins the two hard requirements: `-e` sets per-pane env *natively* (`tmux new-window -e ANTHROPIC_MODEL=… 'claude'` — no shell-quoting), and `status-right` takes `#(shell-command)` interpolation, so our banner is any script reading hub-owned state. ~1 MB, healthy (48.5k★, 3.8 in dev), reference-grade PTY. Agent-awareness (h) is the one thing it lacks — but we own the agent state, so a programmable status line is the *better* primitive. |
| **herdr** 👁 | in-container multiplexer, **agent-native** (Rust, Apache-2.0) | **Prototype behind a flag; don't make it the sole path yet.** The only candidate that solves (h) natively — per-pane working/blocked/idle state, a *documented* NDJSON-over-Unix-socket API (pane/agent lifecycle, `agent.wait-for-state`, event subscriptions, per-launch `env`), and injected `HERDR_*` vars letting a pane drive its own mux. Risk is maturity, not design: pre-1.0 (v0.8.0), ~4.5 months old, **bus factor ≈1** (~90% of human commits one person), 144 open issues, weekly preview churn. Untested as PID 1 in a container — must verify before adoption. |
| **zellij** | in-container multiplexer (Rust, MIT) | Third. Good and healthy, but **no native per-pane env** (wrap in `sh -c`, with a documented *silent* cwd-failure trap), and the status bar is behind a Wasm-plugin boundary vs tmux's one-line `#(…)`. Pick only for its UX defaults. |
| **WezTerm** | emulator **+** multiplexer (Rust) | Two paths, don't confuse them. `wezterm-mux-server` in-container is architecturally valid but the heaviest footprint and **forces WezTerm on the user's host** (version-coupled). Its `ExecDomain`/`docker exec` path is client-side with no in-container server → **fails persistence (d)**; not an option for a durable cluster. License shows `NOASSERTION` — audit the LICENSE file. |
| **ghostty** ❌ | terminal *emulator* (Zig) | Excluded — wrong category. No in-container server, no persistence. Fine as the host terminal to attach *with*; cannot be the multiplexer. (My prior, confirmed.) |
| **tailscale** ❌ | WireGuard mesh **VPN** | Excluded — red herring. Solves *reaching* a machine, not *switching between processes on it*. (My suspicion, confirmed.) |
| **dvtm + abduco** | multiplexer + detach layer (C) | Only under a hard minimalism mandate. **Dormant since 2020** — bad trade for a base image we must audit. |

### Verified live against tmux 3.5a (2026-08-12)

The multiplexer layer stopped being assembly-only: this instance was relaunched
carrying `{muxer}`, so the real generated entrypoint script was executed and the
resulting session inspected. What that confirmed, and the one bug it caught:

- **Per-window env works, and it is the property that chose tmux.** Each member's
  pane process carries its own `CLUSTER_MEMBER` / model — checked at
  `/proc/<pane_pid>/environ`, not by trusting the flag. Note `tmux
  show-environment` is NOT a valid check: it reports the SESSION environment and
  showed one value for every window.
- **BUG FOUND AND FIXED — `new-session -e` leaks into the session environment.**
  Only that first call does it; `new-window -e` does not. Consequence observed:
  the free shell window inherited `CLUSTER_MEMBER=<first member>`, so the
  operator's own shell claimed to be a cluster member, and any variable a later
  member did not override would have inherited member one's value. Fixed by
  emitting `set-environment -u <key>` for each of the first member's variables
  straight after the session is created — verified that the first member's
  already-exec'd process keeps its own copy. Pinned by three tests.
- **The generated script runs clean** end to end under `set -eu`: 5 members in
  template order (windows 0–4) plus the free `shell` last, each in its own cwd.
- **The status line renders as designed** in a real attached client (captured
  from a pty): highlighted current member, the member list, the banner file's
  content interpolated via `#(cat …)`, and the `^b Q=quit` hint. A missing banner
  file prints nothing, as intended. Caveat for future probes: `display-message
  -p` does not evaluate `#()`, so it cannot be used to check the banner.
- **`remain-on-exit on` keeps a dead member visible** (`pane_dead=1`, window
  still listed) — so a crashed member looks crashed rather than un-started, which
  is what makes the explicit quit binding necessary.
- **The quit binding registers** as `bind-key -T prefix Q confirm-before …
  kill-session`.
- **`{muxer}`'s image layer builds and composes**: base → code → muxer, with
  `ensure_image` passing `PARENT_IMAGE` explicitly, and tmux 3.5a present in the
  running container.

### The solo split, and four tmux traps it walked into (2026-08-12)

A SOLO `{muxer}` instance gets ONE window with TWO panes — agent on top at full
width, free shell below at 22% — chosen from a four-way live comparison the
operator screenshotted. Stacked beat side-by-side because at 53 columns a code
block wrapped mid-identifier while the same line rendered intact at full width.
Clusters keep window-per-member (members need the height); a test pins that the
cluster path never splits.

Everything below was found by rendering it, not by reading docs. Each is now a
comment at its call site and a regression test:

1. **Pane percentages do not survive a resize.** A detached session starts at
   80x24, so `split-window -l 22%` sizes against 24 rows; when a client attaches,
   tmux adds the new rows **equally to both panes**, not proportionally —
   measured, 24→58 rows added 17 to each, turning 22% into 39%. Asking for 15%
   and 22% both landed near 35% on a tall terminal. Fixed with `client-attached`
   and `client-resized` hooks that re-apply the ratio; the resize one must be
   DEFERRED ~0.4s because the hook fires before the relayout finishes.
2. **A bare `;` argv element does not chain into a binding.** tmux ends the
   `bind-key` command there and runs the remainder immediately, so the key got
   the layout change while the resize fired once at setup. Chained commands must
   arrive as ONE argument.
3. **`{bottom}` / `{right}` collide with command-BLOCK syntax** inside such a
   string: tmux reads the braces as a block and fails with `unknown command:
   bottom`. `.1` says the same thing for a two-pane window.
4. **`list-keys` prints the key in the FOURTH field**, so a "which keys are free"
   check that reads the third gets nonsense: `/` (describe-key) and `-`
   (delete-buffer) both looked free and are not. `K` and `|` are genuinely
   unbound; `-` is knowingly overridden.

Also: `display-message -p` does NOT evaluate `#()`, so the banner cannot be
checked that way — only a real attached client draws it. And `tmux
show-environment` reports the SESSION environment, so it cannot verify per-window
env; `/proc/<pane_pid>/environ` can.

**Recommendation (adopted):** bake **tmux** into the base image for PoC-0, driven
from the entrypoint (`new-session -d -e …` + `new-window -e …` per member),
`status-right` pointed at a hub-owned banner file. Keep a **herdr** variant behind
a tag/flag and evaluate it on a real cluster; revisit as primary once it hits 1.0
or gains a second sustained maintainer. Both are single in-container deps driven
from the same entrypoint script, so they are not mutually exclusive. The earlier
"custom prompt_toolkit switcher" idea is effectively **superseded by herdr**,
which is a far better-resourced version of "an agent-aware multiplexer with a
socket API" than we would build.

> **PRIOR ART — herdr is a partial sibling of this whole feature.** Its own
> tagline is *"the runtime your coding agents live on"*: N agents in panes, marked
> working/blocked/idle, able to `prompt` each other and `wait-for-state` over a
> documented socket. That is a chunk of the cluster vision, already built. Two
> implications: (1) it *validates* the direction — someone bet on exactly this;
> (2) it is a **buy-vs-build** candidate for more than multiplexing (its
> event-subscription API could back the message-queue's wake, OPEN #4, instead of
> our own daemon). Weigh it alongside Agent Teams before building the protocol
> layer. The bus-factor-1 risk applies to any such dependence.

> **OBJECTION — heterogeneous env per window.** Each member has its own engine
> (model), so each `claude` needs its own env block (ANTHROPIC_MODEL, effort,
> the messaging vars). tmux windows inherit the entrypoint's env; per-window
> overrides go via `tmux new-window` with an env-prefixed command or a small
> per-member launch script written into the container. Not hard, but it means
> the per-instance "conf → env" mapping that `run_container` does once must now
> be done N times, one per window. The cluster launch assembly owns that loop.

> **OBJECTION — image identity.** All members share ONE image, so its toolchain
> is the union of what the members need. If a devteam mixes `[code]` and a
> web-only member, the image carries both layers. Acceptable, but it means
> **the cluster's image is built from the union of members' tags**, and two
> members that disagree on a *container-level* setting (see firewall, below)
> cannot both be honoured. Per-member differences must be limited to what lives
> in a session's own env/settings, not the image or container flags.

> **OPEN #1 — detach/reattach and lifecycle.** tmux makes N long-lived
> processes. What happens on detach (does the cluster keep running?), on the
> user quitting one member's `claude`, on re-launching the same cluster
> (`--continue` per member)? The solo model's `--rm` + resume-flag logic
> (`agents_crud.compute_resume_flag`) needs a per-member analogue.

---

## The `{clstr}` tag

A specialty, shipped like `{cowork}`: `agents/specialty/clstr/` with a
`tag.info` (its addendum) and a `policy.json` fragment.

- **Forced and non-removable on every member.** There is no existing
  "irremovable tag" mechanism — the nearest is `{manager}` auto-ticking
  `{cowork}`. **PROPOSAL:** the cluster creation path applies `{clstr}` to every
  member, and the per-member picker tag-form filters it out of the toggleable
  set (shown, greyed, not removable). The reason to force it is exactly yours:
  it carries the addendum that teaches a member it is one of several.
- **Its policy fragment is where the messaging prerequisites live** (see below):
  `crossSessionInbound: "accept"`, and whatever settings key the feature needs.
  This is legitimate because the merged in-container `~/.claude/settings.json`
  is **user scope**, the one non-managed scope the `crossSessionInbound`
  strictness-ratchet respects (per the socket research).
- **Addendum content:** you are member `<role>` of cluster `<session>`; your
  siblings are `…`; how to address one (`/address` or the SendMessage tool); the
  turn-taking protocol in force; and — same as cowork's hard-won lesson — that a
  message from a sibling carries **no user authority** (can't approve
  permissions, can't change config; verified first-person, see ISSUES.md).

> **OBJECTION — `{clstr}` and `{cowork}` are different comms substrates; don't
> mix them in the PoC.** Cowork talks through the host hub across containers;
> cluster talks through Anthropic's feature inside one container. An agent could
> in principle carry both, but the semantics (who can wake me, over what) would
> be confusing. Keep them orthogonal; PoC members are `{clstr}`-only.

---

## Communications

**Two substrates, and they are complementary — not a pick-one.** The
conversation surfaced a second mechanism beside Anthropic's feature, and it
turns out to cover exactly what the feature does not:

| | Anthropic cross-session messaging | Our in-container message-queue |
|---|---|---|
| Shape | point-to-point (`SendMessage` to one named session) | ordered broadcast — one push, every member sees it |
| Wake ("last inch") | **native** — idle receiver starts a new turn on delivery | needs its own wake (see below) — the hard part |
| Ordering | none guaranteed | total order (it's a queue) |
| Shared log | none — each session sees only its own transcript | the queue **is** the log |
| Cost to us | consume the supported tool surface; activation quirks | we build it; but no wire format, no telemetry, no feature-flag dependency |

So: **Anthropic's feature is the clean fit for directed 1:1** ("ask the
researcher X"), and **the queue is the natural substrate for the team-room /
round-table** ("does anyone know why we're not using an enum here?"). They can
layer — the feature's native wake could even be what nudges a member to read the
queue. Neither is committed yet; the phasing below starts without either.

### The in-container message-queue (your idea, expanded)

A file in the shared cluster mount (see OPEN #3), append-only and ordered.
Every push is visible to **all** members; a member reads new entries, and
**usually nops** — stays quiet — which is how a real group works: most remarks
aren't for you. This gives three things the point-to-point feature can't:
message *order*, *broadcast*, and a *shared conversation log* for free.

- **The team dynamic it enables** (the goal): a member asks the room "why aren't
  we using an enum for `<thing>`?"; any member who has touched that code answers
  ("it's so the values can carry metadata" / "good catch — it *would* be
  better, I'll switch it"); members with nothing to add stay silent.
- **All-nop handling (needed):** if a broadcast question goes unanswered — every
  member nopped — it must not vanish. Silence must be *decided*, not accidental.
  For the PoC, keep it blunt: a round-robin "you're up" rule obliges one member
  to answer or explicitly say "no one here knows".

  > **DEFERRED IDEA — nop-as-election, an emergent social protocol (not a PoC
  > concern; recorded so it isn't lost).** Instead of a designated chair,
  > *every* member elects to nop unless it has high confidence it has something
  > intelligent to say **immediately**; who remains after the nops is visible to
  > all (the message-queue again), and that visibility could itself create
  > "social pressure" — leaning harder on those who haven't nopped out yet. The
  > all-nop fallback would then draw on **local culture / social norms** rather
  > than a fixed rule:
  > - simplest norm: prioritise whoever has spoken least so far (a fairness /
  >   turn-balancing rule);
  > - richer factors to weight the step-up: peers' past approval of a member
  >   (track record for quality and for actually resolving issues), fitness of a
  >   member's *model* to the problem at hand, and **partial confidence** — "it's
  >   probably X" or "the direction *has* to be Y" is worth surfacing over a flat
  >   "no idea", so the norm should reward hedged-but-directional over silent.
  >
  > The appeal is that turn-taking *emerges* from confidence + norms instead of
  > being dictated, which suits the flat/equal topology. The cost is that
  > "confidence" is self-reported and models are unreliable narrators of their
  > own certainty (this session has live examples of a peer over-claiming) — so
  > any real version needs the confidence signal grounded in something checkable,
  > not taken at face value. Revisit alongside the protocol phase.
- **The unsolved hard part is the wake.** A queue is just a file; an idle member
  won't notice a push on its own (the whole "last inch" problem from
  `agent_cross_comm_propositions.md`). Options carry over: the messaging
  feature's native wake, a tiny in-container daemon doing `tmux send-keys` to
  the right window (local pty injection — simpler than the cowork hub because
  there's no container boundary), or a `Stop`-hook poll. **OPEN #4** — which
  wake backs the queue is undecided and is the queue's make-or-break.

### Directed address via the feature

**`/address <role> <message>`** — a slash command shipped via `{clstr}`'s mount
(exactly as `{manager}` ships `/cowork`) that instructs the current member to
`SendMessage` a named sibling. No hub, no wire format, no host process — the
model is the client. This is the clean adoption of the feature, and its native
wake means the addressed sibling actually stirs.

**Prerequisites for the feature (established by the socket research; here they
are per-cluster, not per-launcher):**

- **The kill-switch must be truly UNSET for cluster containers.** The base image
  sets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (Dockerfile), which disables
  feature-flag evaluation and with it messaging; `=0` does **not** rescue it
  (sticky). The cluster entrypoint must `unset` it before launching members.
  The fine-grained vars (`DISABLE_AUTOUPDATER`, `DISABLE_ERROR_REPORTING`,
  `DISABLE_FEEDBACK_COMMAND`, `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY`) keep
  auto-update and error-reporting off without killing messaging.

> **Telemetry re-admitted — ACCEPTED (knowingly).** There is no "telemetry off,
> flags on" switch, so enabling the *feature* means accepting Statsig flag/usage
> traffic for cluster containers. The user has accepted this per-mode cost;
> state it at cluster-creation time so it isn't a surprise, and keep solo
> instances on the kill-switch (unaffected). NOTE: this cost is the feature's
> alone — **the in-container message-queue needs no flag evaluation and so no
> telemetry**, which is a point in the queue's favour for a privacy-minded
> setup.

> **OPEN #2 — `{firewall}` interaction.** Same-container messaging is a **local
> Unix socket — no egress**, so it is firewall-compatible. BUT feature-flag
> *evaluation* at startup needs to reach Anthropic's flag endpoint once. Under
> `{firewall}` that endpoint must be whitelisted, or messaging silently stays
> off. Confirm the exact host and add it to the built-in whitelist for cluster
> images. (Firewall is container-level, so it is one setting for all members —
> another reason per-member container-level differences can't exist.)

> **OPEN #3 — discovery vs isolation, with a chosen direction.** Messaging
> discovery needs siblings to see the same registration files; isolation wants
> each member to keep its own `~/.claude` (separate memory/transcript/persona).
> **Direction (from the conversation):** mount a **shared cluster directory** —
> `~/.claude-agents/clusters/<session>/` — into every member, giving them a
> common surface with direct access, while the rest of `~/.claude` stays
> per-member. That shared dir is also where the **message-queue** lives, so one
> mount serves both. Residual question for the researcher: whether the
> *feature's* registration dir can be relocated into that shared mount (env /
> config for the registration path), because if it can't be split from the
> per-member state dir, the feature-path forces "shared `~/.claude`" (members
> lose separate memory) — whereas **the queue path has no such constraint**, it
> only needs the shared mount we already control. Another reason the queue is
> attractive: it sidesteps the one question that could block the feature.

### Research spike — CLOSED (2026-08-29, empirical, Claude Code 2.1.245)

Every blocking question above was answered by probing inside a live
launcher-built container (this instance's own — `{firewall}` active, disposable
sibling sessions on the agent's scratch tmux server), not from docs:

- **Kill-switch recipe CONFIRMED.** A sibling launched with
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` genuinely UNSET had `ListAgents`
  loaded and `SendMessage` working; the control sibling (var still set) had no
  `ListAgents` at all. The planned entrypoint `unset` (already supported via
  `tmux.script`'s `unset_env`) is exactly the right mechanism.
- **OPEN #2 (firewall) CLOSED — no whitelist change needed.** Activation and a
  full exchange worked under the live firewall. The transport is a local unix
  socket (no egress), and whatever remote fetch activation performed
  (`remote-settings.json` appeared) passed the existing whitelist.
- **Exchange + native wake PROVEN** (the docs' "two sessions inside the same
  container" case). `SendMessage` delivered into the sibling's pane as a prompt
  (`@ workspace-e4❯ ping from labA…`) and the IDLE sibling woke and processed
  the turn. No inbound hold fired: local same-uid peers exchanged with zero
  `crossSessionInbound` configuration on 2.1.245 defaults.
- **OPEN #3 (discovery vs isolation) — mechanism mapped.** Registration is
  per-CONFIG-DIR: `$CLAUDE_CONFIG_DIR/sessions/<pid>.json` plus a
  `<pid>.<hash>.key` peer-token file; the socket itself is machine-scoped
  (`/tmp/cc-socks/<pid>.sock`). Shared config dir → discovery proven live.
  Separate per-member config dirs → separate registries BY CONSTRUCTION — so
  the cluster entrypoint symlinks every member's `sessions/` to one shared
  dir (`/cluster/sessions/`) before launching members. Symlink-follow is plain
  POSIX path resolution and should be invisible to the app; it is the one
  residual to verify at the first real cluster launch. The message-queue's
  shared-mount direction above is unchanged and still queue-sufficient.
- **Member naming SOLVED: `CLAUDE_CODE_SESSION_NAME=<member-id>`.** Verified:
  a sibling launched with `…=researcher__primary` appears in a peer's
  `ListAgents` as `researcher__primary [f0b12a]`. Naming is therefore one more
  per-window `-e` variable — the exact property that chose tmux. `/rename`
  exists for live changes; auto-derived names (`workspace-e4`) are the
  fallback.
- **Bonus, better than the plan hoped:** every session (messaging-enabled or
  not) self-registers `name`, `status: idle|busy`, and its OWN TMUX PANE ID
  (`"tmux":"labC:@2.%2"`) in `sessions/<pid>.json`, and peers advertise a
  `notify_idle` feature. Claude Code natively maintains the per-member state
  herdr was being watched for — the banner and the queue's wake (OPEN #4) can
  READ this instead of us building telemetry. Stale records from dead sessions
  are filtered by `ListAgents` (procStart validation), so no cleanup falls on
  us.
- One probe was fenced, and the fence is itself a finding: an agent cannot
  copy `.credentials.json` even to test an isolated config dir (permission
  engine). Harmless to the design — the LAUNCHER places credentials host-side
  into every member state dir, as it already does for solo instances.

---

## Turn-taking and the shared "chat"

The feature wakes receivers; it imposes **no** speaking order. Turn-taking is
therefore a **prompt-level protocol** (like the cowork addenda — which this
session proved real agents follow to the letter), not something the harness
enforces. Later-phase patterns, in rough order of ambition:

1. **Directed address** (PoC-1): `/address <role>` — one-to-one, user-initiated.
2. **Moderator/chair:** one member (later the `team-lead`) grants turns; others
   speak only when addressed. Maps onto the dev-team vision.
3. **Shared chat / round-table:** N members "in a room" where each may chime in
   — the philosophers-in-discussion / design-review-concerns image. The
   in-container **message-queue** (see Communications) is its natural substrate:
   broadcast by construction, ordered, self-logging. The behavioural spec — the
   **team dynamic**, all-nop handling, and the step-up protocol for an
   unanswered question — is written up there; it applies whatever substrate
   carries the words.

The stop rule matters as much as the start rule: a round-table needs a way to
*end* a thread (a resolution line, a chair calling it, or the loop cap below),
or N members politely acknowledging each other never terminates.

Harness-level levers that DO exist and back the protocol up:

- `permissions.deny: ["SendMessage"]` structurally mutes a member (a listen-only
  reviewer) — hard, not advisory. Documented side effect: also disables that
  member's subagent messaging. (Only bites the *feature* substrate; the queue
  would need its own mute — a per-member read-only flag on the queue file.)
- Built-in **loop throttling** (repeat-drop, unread caps) means a *broken*
  feature-protocol degrades to silence, not an infinite spend loop — better
  than cowork's budget exhaustion. The queue would need an equivalent cap.

> **OBJECTION — a shared transcript is required for the round-table, and the
> queue gives it for free.** With the feature alone each member sees only its
> own conversation, so a discussion has no log — debugging it is blind. This is
> a concrete point for building the round-table *on the queue* (which is
> self-logging) rather than on point-to-point feature messages, or else adding a
> chair that journals to a file (the existing Stop-hook capture works unchanged
> in-container).

> **PROPOSAL — evaluate Agent Teams before building the protocol layer.**
> Anthropic's **agent teams** is a productized lead+teammates-in-one-session
> with a **shared task list, file-locked claiming, and dependency unblocking** —
> i.e. turn coordination made structural rather than conversational, which is
> most of what `team-lead` wants. It is experimental, flag-gated, fixed-topology
> (one lead, one team per session). If your end state is *visible N panes to
> watch*, cross-session messaging fits better; if it is *coordinated work*,
> teams may deliver it with no protocol for us to build. An evening's evaluation
> before the team-lead phase is well spent.

---

## "Equal" members, and writer safety — two separate axes

The conversation clarified that these are **different questions** that were
getting conflated:

**Axis 1 — topology. "Equal" = flat, no manager/coworker asymmetry.** This is
what the user means by equal: unlike cowork (where a `{manager}` hosts and
recruits `{cowork}` subordinates), cluster members are peers — none owns the
group, none spends another's budget. **DECIDED: flat topology for the PoC.** A
chair/`team-lead` may later be *elected by role*, but it's a role, not a
privileged tag tier. Equality here says nothing about files.

**Axis 2 — file writes. The real hazard, addressed by placement + protocol.**
The launcher's edits are read-then-write with no locking, so two members editing
the same file silently lose work. The intended posture makes this rare —
**different members work on totally different things** — and two mechanisms back
it up:

- **DECIDED in principle: each member gets its own checkout; the shared project
  is the upstream everyone syncs with.** Members integrate through git rather
  than racing one working tree.

  **MEASURED (git 2.47.3, 2026-08-12): worktrees DO isolate files, but they are
  NOT container-portable. Clones are. That settles the mechanism: CLONES.**

  What the probe showed, in order:
  1. *Files are genuinely separate* — different inodes, and an edit in one
     worktree leaves the others and the main checkout untouched. So the
     impression that worktrees are for exactly this is correct as far as the
     working tree goes; nothing is shared at the file level.
  2. *But a worktree's `.git` is a FILE holding an ABSOLUTE path* —
     `gitdir: /host/path/main/.git/worktrees/<name>`. Mount only the worktree
     into a container (which is precisely our model: `/workspaces/<member-id>`)
     and git dies outright: **`fatal: not a git repository`**. Making it work
     would mean also mounting the parent repo at its identical host path inside
     the container — leaking host paths into every cluster container — or
     running `git worktree repair` inside, per member, after every mount.
  3. *`--relative-paths` does not rescue it here*: that is git 2.48+, and this
     toolchain is 2.47.3. Even with it, the relative layout would have to be
     preserved by the mount, which `/workspaces/<id>` does not do.
  4. *Two worktrees cannot share a branch* — `fatal: 'alpha' is already used by
     worktree at …`. Fine for our design (each member wants its own branch), but
     it is a hard constraint rather than a convention.
  5. *A clone survives the same test* — with the origin path hidden entirely,
     `git status` and `git log` work, and `git remote -v` still names the
     upstream. Self-contained, mountable anywhere, and it already has the
     "remote everyone syncs with" shape the intent called for.

  So the trade is not "cheap vs familiar", it is **"host-only vs
  container-ready"**. PoC-0's `--worktrees` remains useful as a host-side
  convenience and its tests still pass, but it is NOT the in-container mechanism
  and must not be promoted to one. Clone costs stand (N copies of history;
  `--reference` would claw disk back at the price of reintroducing exactly the
  parent-repo coupling that disqualified worktrees) and the protocol gains an
  explicit `push`/`pull` step — arguably a feature, since "publish my work"
  becomes a reviewable moment rather than an implicit merge.

  **Open sub-decision — where a personal workspace lives on disk.** Candidates
  raised: `clusters/<session>/<member-id>/` (beside the member's state dir) or
  `clusters/<session>/workspace__<member-id>/` (a sibling, name-tagged). Today's
  code uses `clusters/<session>/worktrees/<member-id>/`, which is a third shape —
  a dedicated subdir keeping checkouts out of the state namespace entirely.
  Worth settling together with the mechanism, since a clone's layout may want its
  own root. **Needs a conversation, not a default.**

  **PoC-0 therefore ships SHARED workspaces and no parallel file work** — every
  member sees the one project at `/workspace`, `create` says so in plain words
  every time, and `--worktrees` is available meanwhile for anyone who wants
  isolation before the decision lands.
- **Lock-files in the protocol, as a LAST resort.** When members genuinely must
  touch the same file, a claim/lock convention in the shared mount lets them
  avoid collision — but this is the exception the worktree default keeps rare,
  not the primary mechanism. (This is also what agent-teams does structurally;
  see the buy-vs-build note.)
- **`{ro}` for a member that should never write** (a pure reviewer) — denies the
  edit tools and mounts `/workspace` read-only. A per-member option, not the
  cluster default.

> **The distinction to preserve:** "flat/equal topology" (axis 1, decided) and
> "how members avoid clobbering each other" (axis 2, worktrees + last-resort
> locks) are independent. Equal members can still be worktree-isolated; that is
> the intended combination, and it dissolves the "equal + shared read-write"
> hazard the earlier draft flagged.

---

## Explicitly accepted tradeoffs

State these at the top of the eventual feature so nobody reads them as bugs:

- **Cohabitation punctures the per-instance isolation this project exists to
  provide.** One container means one crash, one rogue `rm -rf`, or one bad actor
  takes all N members. Accepted, in exchange for direct messaging and a shared
  project view.
- **Resource multiplication.** N members are N live Claude processes. Idle
  sessions don't burn tokens, but a chatty protocol multiplies spend and can hit
  rate limits. The loop caps bound the worst case.
- **Telemetry on** for cluster containers IF the Anthropic feature is used
  (accepted); the message-queue substrate avoids it.

---

## Roadmap

1. ~~**Research spike**~~ **CLOSED (2026-08-29)** — done empirically in a live
   container rather than delegated; full results under "Research spike —
   CLOSED" in Communications. Kill-switch recipe confirmed, OPEN #2 closed (no
   whitelist change), exchange + native wake proven, OPEN #3 mechanism mapped
   (per-config-dir `sessions/` registry; shared symlink target for members),
   naming solved (`CLAUDE_CODE_SESSION_NAME`). One residual rides PoC-1: verify
   the sessions-dir symlink is followed.
2. **PoC-0 — BUILT (2026-08-12).** `agents/devteam.legoset`, the
   `launch/cluster/` package (member / legoset / state / worktree / tmux /
   launch_plan / cli), and a root `cluster.py` entry. 131 tests. What is
   verified live: git worktrees (real repo, real isolation) and the whole
   create → plan → script → destroy cycle. What is assembly-only: the tmux
   commands (tmux is not installed in the dev container, and the flags rest on
   the researched docs), and the container itself — no image build or
   `docker run`, which is the integration step `docker_config` owns.
   Two bugs the build itself surfaced, both fixed and pinned:
   - the tmux status line first named the HOST banner path, which inside the
     container `cat`s nothing and renders empty (same class as `{cowork}`'s
     early review-command bug) — hence `LaunchPlan.container_banner`;
   - `git symbolic-ref` prints `master` on an UNBORN head and exits 0, so it
     cannot detect an empty repo; worse, `git worktree add -b` on an empty repo
     SUCCEEDS by inferring `--orphan`, so five members would each have got an
     empty checkout of a project full of files. `has_commits` (via
     `rev-parse --verify`) is the correct probe and the refusal now says why.
3. **PoC-1:** `{clstr}` tag + addendum + `/address`; native sibling messaging
   via the feature; prerequisites wired. Prove two members exchange a message
   in-container. (OR: prototype the message-queue instead, if the spike says the
   feature path is blocked or the queue is preferred for privacy.)
4. **The form — CREATION HALF BUILT (2026-08-29).** The picker now carries one
   cyan-tab row per `.legoset`; selecting it prompts project + session name,
   then opens the membership form (`gui/cluster_form.py`): every agent listed,
   **Space adds another of the focused agent** (×N chip), Backspace removes its
   last, a live panel previews the exact member ids, Enter persists via
   `legoset.assemble` + `state.save`. One deliberate change from the sketch
   above: **no role fields** — decided with the operator that per-member
   editing mid-flow is noise during setup. Roles auto-derive instead
   (`legoset.auto_roles`): template roles verbatim; a unique unroled pick stays
   bare; duplicates ALL get numbered (`golem__1`/`golem__2` — a bare `golem`
   beside a `golem__2` would read senior), numbering skipping claimed values;
   the live id panel is what makes the invisible rule acceptable. A broken
   `.legoset` renders as an unselectable red row naming the fault rather than
   crashing the picker. **EDITING HALF BUILT TOO (same day):** existing
   clusters appear beneath the template rows (`▸ Clstr` line, members nested
   with their tag labels); **F2 on a member opens the ordinary tag form** and
   persists via `Cluster.with_build`, which re-applies the forced
   `{muxer}`/`{cluster}` (the form lets anyone untick them; no path may
   produce a member unaware it is one); Del removes a member (last member
   guarded — destroy instead) or destroys the cluster (via `state.destroy`,
   extracted from the CLI so both share one teardown; worktree removal only
   runs when worktrees exist, so a shared-workspace cluster never shells git).
   STILL PENDING from this milestone: nothing — what remains is LAUNCH (the
   docker integration; Enter on a cluster row explains that today).
5. **LAUNCH — BUILT (2026-08-29).** `launch/cluster/launching.py` (assembly) +
   `docker_config.run_cluster_container` (execution, per the recorded
   split). Enter on a picker cluster row and `cluster.py launch <session>
   [--dry-run]` both run it: members resolve to ordinary Instances (personas /
   settings / commands installed into `clusters/<s>/members/<id>` by the
   normal agents_crud pipeline), the UNION image builds via the ordinary
   `ensure_image`, and the generated entrypoint (`/cluster/cluster-start.sh`)
   bakes in the spike's recipes — kill-switch unset (messaging ON; the
   accepted telemetry cost is announced at every launch), per-member
   `CLAUDE_CONFIG_DIR=/cluster/members/<id>` riding the one /cluster mount,
   `sessions/` symlinked to shared `/cluster/sessions/`, skills/keybindings
   symlinked from ~/.claude, `CLAUDE_CODE_SESSION_NAME=<member-id>` per
   window. Per-member engine conf and effort/`--continue`/claude_args ride
   each WINDOW's env/argv (`launch_plan` grew `command_for`). Credentials
   mount per member — mounts are (source, target) PAIRS because the shared
   credential file is the source of N mounts (a source-keyed dict silently
   served only the last member; caught by its test). Members whose tags carry
   container-level docker features ({firewall}, {dood}) are REFUSED by name
   rather than launched degraded. Cycling: tmux native (^b n/p, numbers, ^b w,
   click the status-bar name), now listed in the help popup. Verified: full
   CLI dry-run end-to-end (base→code→muxer builds, the run command, the
   script's recipes) + 6 mutation-checked guards. NOT yet verified: a real
   `docker run` on a docker-equipped host — first live boot checks the
   sessions-symlink follow and CLAUDE_CONFIG_DIR account-file relocation.
5. **Protocol + shared chat:** the team dynamic on the queue — broadcast
   questions, any-who-knows answers, all-nop handling, the step-up protocol, and
   a stop rule; moderator first, then round-table; the queue is the journal.
6. **`agent_writer`, then `team-lead`:** `agent_writer` is a persona that
   follows a plan / improvises and **routes tasks to the fitting member** —
   project-starter when starting a project or feature, refactorer for
   `/refactor` and `/unspaghettify`, and so on. `team-lead` is built *after* it
   and manages development flow using that routing sense. Evaluate Agent Teams
   first (PROPOSAL above) — it may supply the lead + shared-task-list structure.
7. **More member types:** tester, security-oriented, creative, reporter.

---

## Decisions needed

**Resolved in conversation (recorded so they don't reopen):**

- **Topology:** flat/equal — no manager/coworker asymmetry among members.
- **Writer safety:** git worktrees per member (default); lock-files in the
  protocol as a last resort for genuine same-file work; `{ro}` for pure
  reviewers. Posture: different members on different things.
- **Telemetry** for the feature path: accepted (the queue path avoids it).
- **`.legoset`:** expresses multiplicity + default roles, user-editable at
  creation.
- **Buy-vs-build:** note Agent Teams; evaluate before the team-lead phase; do
  not implement early.
- **Multiplexer:** tmux for the PoC (zellij as ergonomic fallback; prompt_toolkit
  switcher as the long-term "our UI" path).
- **Member/window order (2026-08-29): DERIVED, never authored.** No `order`
  field in cluster.toml (legacy files' key is ignored); `state.picker_order`
  — the picker's own agent-row sorting, same-agent members id-grouped — is
  the one ordering every consumer uses (windows, `^b 1..9`, picker rows,
  previews, summaries, the form's panel). Template file order and form pick
  order carry no meaning (pick sequence only drives duplicate numbering).
  Rationale, verbatim: "The user has enough small decisions to make, this
  doesn't need to be one of those." Accepted cost: a template cannot choose
  its landing window (devteam opens on bug-investigator, not
  project-starter).

**Queued, not yet done (2026-08-12):**

- ~~Move `/cowork` under `{manager}`~~ **DONE.** It lives at
  `specialty/cowork/manager/_commands/cowork.md`, mounted by that tag's
  `tag.docker`, so only manager-tagged instances carry it. Note the dir is
  `_commands/` — the tag tree's strict rule reserves un-prefixed subdirs for
  nested tags, and `commands/` raised a TagError. Same pattern for `/cluster`.
- ~~Add a "Specialty Commands" legend section~~ **DONE**, and DISCOVERED rather
  than listed: any tag with `_commands/*.md` appears, with the description read
  from the command's own `description:` frontmatter so the legend cannot drift
  from what the command says about itself. Adding a command to a tag is one file
  *(2026-08-13 — both bullets superseded: per-tag `_commands/` dirs are retired.
  A tag now DECLARES its commands in `tag.info` (`commands = [...]`) and the
  files live centrally in `agents/_commands/`, shareable across tags; the legend
  section is "Tag Commands" since professions grant commands too. The mount
  mentioned above never survived either — a read-only mount cannot host a
  nested mount, so the dir is assembled per instance; see ISSUES.md.)*
  plus one mount, with nothing to register in the picker.
- ~~Per-area mouse scrolling in the picker~~ **DONE.** prompt_toolkit delivers a
  mouse event only to the control under the pointer, so "scroll whichever side
  the mouse is over" needed no hit-testing — one `_ScrollingControl` on each side.
  The list moves one ROW per notch (so wheel and arrows cannot disagree about the
  selection) while the preview moves three LINES and carries a scrollbar. Moving
  rows resets the preview offset, or the next row's preview would open part-way
  down. **The bug that made it look unimplemented:** prompt_toolkit's
  `mouse_support` defaults to False, so the terminal was never put into
  mouse-reporting mode and NO control received any event — the handlers were
  correct and simply never called, with nothing erroring. Now set explicitly and
  asserted against the constructed Application. Trade-off it brings: while the
  picker is open the terminal's own click-drag selection is suppressed (Shift
  bypasses it in most terminals).
- **Dragging the preview scrollbar is NOT possible** as things stand:
  prompt_toolkit's `ScrollbarMargin` exposes only `create_margin` and
  `get_width` — no mouse handler — and margins sit outside the area whose events
  reach a control. The bar is an indicator; the wheel is the control. A draggable
  bar would mean a custom margin plus event routing of our own.
- **Split the oversized addendums.** A tag may carry SEVERAL addendums, one per
  topic, and `{cowork}`'s and `{cluster}`'s are each doing multiple jobs in one
  block (protocol + trust + economy). One topic per addendum reads better and
  lets a reader skip what does not apply.
- ~~**Probe herdr in a container before more tmux polish.**~~ **PROBED AND
  IMPLEMENTED (2026-08-29, v0.8.2, live in a launcher container).** Every
  recorded blocker fell: the server runs HEADLESS and holds the PTYs (panes
  built, driven, and read with no client ever attached — the PID-1 worry is
  gone; state even persists across server restarts via session.json);
  `tab create --env/--cwd/--label` reaches the tab's shell (echoed from
  inside), matching tmux's decisive per-window env; and `agent start <name>
  --kind claude` gets the member DETECTED — `agent list` reports it by
  member id with live idle/working state, the native version of the banner
  roster. Shipped as `launch/cluster/herdr.py` (script assembly, tested),
  selected per launch with `MUXER_BACKEND=herdr` (tmux stays default until a
  real cluster launch vouches); binary version- AND sha256-pinned in the
  `_muxer` layer (pre-1.0, bus factor ≈1 — exact pins non-negotiable);
  `settings/herdr.toml` is the user-editable config at herdr's default path.
  Keys: same ctrl+b prefix; prefix+n/p cycle members, prefix+1..9 jump,
  prefix+b sidebar, prefix+? help, **detach is prefix+q**; the deliberate
  way out is `herdr server stop`. One bug only LIVE EXECUTION of the
  generated script caught: `herdr status server` exits 0 whether or not the
  server runs (it reports, it doesn't probe) — exit-code loops made the
  readiness gate a no-op and the stopped container immortal; both loops now
  grep "status: running", pinned by test.

**Still open:**

0. **Tag-data isolation — audited 2026-08-12.** Every profession already ships a
   `tag.docker` (`code`, `webdev`), and `_dood` carries one for its build-arg
   forward; `_muxer` needs none (its Dockerfile takes no ARG beyond the
   automatically-passed `PARENT_IMAGE`). The specialties without one genuinely
   need none — `{auto}` is `claude_args` in tag.info, `{ro}` is a tag.info flag
   plus a claimed policy fragment. Two things the audit DID surface:
   - **`/cowork` is shipped globally, not by `{manager}`.** `custom_commands/` is
     mounted into every container, so every instance gets a command only a manager
     can use. `group_hosting_plan.md` specified the opposite — a
     `specialty/cowork/manager/tag.docker` mounting `commands/cowork.md` — and
     that is the correct home. It degrades gracefully today (the command's first
     gate checks for `/cowork/`), so this is tidiness, not breakage.
   - **Two name-based checks remain in code by necessity**, and both are UI, not
     container config: `user_additions` plants the firewall whitelist template,
     and `claude_code_config` prints the whitelist count in the launch banner.
     A `tag.info` key (`user_extras_template = …`) could drive the first
     generically, but with one caller that is a premature abstraction. The
     `{cowork}` mount must stay in code too — its source path is per-instance,
     which static `tag.docker` mounts cannot express.

1. **Isolate more of `{muxer}` into the tag definition.** The entrypoint now lives
   in `agents/specialty/muxer/tag.docker` (so "what a muxer container runs" is
   data, routed through the same `entrypoint_flags` every tag uses). What remains
   in Python splits cleanly in two, and only one half has to:
   - *Genuinely dynamic* — session name, the agent's argv, the host project
     label, and the ratio hooks (which must be session-targeted). A static asset
     cannot carry these.
   - *Static, and a candidate for a mounted `agents/specialty/muxer/muxer.tmux.conf`*
     — every `set-option`, every `bind-key`, and the help popup text. Moving it
     would let the look be tweaked without touching Python, exactly as
     `{firewall}` ships `init-firewall.sh` as a tag asset, and the generated
     script would shrink to: create session, split, `source-file`, attach, wait.
   - **The blocker to check first:** the ratio hooks cannot become global (`-g`)
     hooks in that conf, because a cluster container ALSO carries `{muxer}` (it
     nests inside) and its windows are unsplit — a global `resize-pane -t .1`
     would fire there against a pane that does not exist. So the conf can hold
     options and bindings, but hooks stay generated.

   **`{muxer}` + `{firewall}` — DONE (2026-08-12).** They now compose as a CHAIN:
   `entrypoint_chain` makes the first claimant docker's entrypoint and hands every
   later one to it as arguments, so each wrapper does its job and `exec "$@"`s the
   next. `{firewall}`'s tail changed from `exec claude "$@"` to `exec "$@"` (it no
   longer knows what it wraps), and `run_container` names `claude` explicitly
   whenever a wrapper is active — the image's ENTRYPOINT is bypassed then.
   `{muxer}` is the TERMINAL link: the agent's argv is baked into its generated
   script, so nothing follows it. Order comes from the existing contribution order
   and is pinned by a test (firewall before muxer), because iptables must be
   applied and `sudo -k` run before the agent starts. The refusal that existed
   only to explain the old incompatibility is deleted.

1. **Role required or optional?** — rec: always present, defaulting to the agent
   name, unique per cluster (it's the messaging `--name` and the dir suffix).
2. **PoC-0 (tmux only) as the first deliverable, before any messaging?** — rec:
   yes; it de-risks the new run model on its own.
3. **Feature vs message-queue as the first comms substrate** — depends on the
   spike (OPEN #3/#4). The queue avoids telemetry and the registration-dir
   question but owns the wake problem; the feature has a native wake but the
   telemetry cost and the relocation question.
4. **OPEN #1** (detach/reattach + per-member resume lifecycle),
   **OPEN #2** (firewall flag-endpoint whitelist), **OPEN #3** (registration
   relocation), **OPEN #4** (queue wake) — the technical unknowns above.
