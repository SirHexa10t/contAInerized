# Claude Code Agents

A launcher for pre-made Claude Code agents — each persona ships its own model,
parameters, and instructions, but all share OAuth credentials, workspace
configuration, slash commands, and toolchain caches. Each launch boots an
isolated Docker container with persistent per-instance state.

## Features

- **Pre-made agent personas** — drop `agents/<name>.md` (the agent's
  `CLAUDE.md`) plus an optional `agents/<name>.lego` build file naming its
  default tags; the picker shows it on the next launch.
- **Multiple sessions per agent** — every launch is an instance keyed by
  `<agent>__<session>`, with its own conversation history and workspace.
- **Resume conversations** — picking a "Cont." row auto-passes `--continue`
  to `claude` so the conversation state is restored.
- **CLI shortcuts** — `python3 run.py poet` skips the picker and sets up a
  fresh `poet` instance; `python3 run.py poet__myproject` continues that
  specific instance directly.
- **`q` — one-shot quick questions** — a separate launcher (`quick_question.py`; run
  `install_rc_alias.sh` to install the `q` alias) for asking a single
  direct question without the picker or tag form:
  `q "why do elephants have big ears?"` (quote the whole question). It runs a
  hidden, fixed-build agent in print mode (`claude -p`): a live `⋯ thinking…`
  timer runs while it reasons, then the answer streams in as it's generated,
  rendered as markdown — headings, bold, lists, highlighted code — while it
  arrives. Redirected or piped it stays the raw source instead, so
  `q "…" > notes.md` is valid markdown and a script never meets an escape
  code; `q --answer` reprints by the same rule. A run that ends without an
  answer says so on stderr in the CLI's own words — including a plain-text
  refusal printed outside the event stream, which used to be discarded as
  noise — and a "not logged in" adds what the launcher mounted, so the next
  report says which credential state caused it.
  (The model's own reasoning *text* isn't shown — current models redact it in
  headless mode — so the timer is the progress signal during long thinks.) The
  default uses the `quick` engine; **`--explain`** answers with a `trivia`
  agent that draws out connections and related tidbits (on the `reliable`
  engine), and
  **`--research`** uses a source-checking research agent (mutually exclusive
  with `--explain`). The question goes to **claude** unless **`--ai <name>`**
  names another AI member of `agents/ai/` (`q -h` lists them and says which
  can answer today — the ones whose CLI has an adapter; the others are
  refused with what an adapter needs). If the run ends without an answer —
  Claude Code not logged in, a rate limit — the CLI's own words print on
  stderr, with how to log in (once, through a normal launch). Each question's thread is saved under
  `~/.ai-agents/quickie/`, sharing one `communal/` workspace you can drop
  files into. **`q --history`** lists past threads (grey timestamp, id, last
  question; oldest first); **`q --answer`** reprints the LATEST thread's saved
  answer, or a named one with `q --answer <id>`; **`q --resume "follow-up"`** continues the latest thread (name one with
  `q --resume <id> "follow-up"`); **`q -h`** prints
  the tool's own help (a real arg-parser, so flags don't reach `claude`).
- **Multi-agent group hosting (`{cowork}` / `{manager}`)** — running
  instances can work together, coordinated by a host-side hub. Tag instances
  `{cowork}` to make them recruitable (each gets its own
  `~/.ai-agents/group_hosting/<id>/` mounted at `/cowork`); tag one
  `{manager}` (nested inside `{cowork}`) to let it convene groups: it asks the
  hub for a roster, recruits peers, hands work out, reviews what comes back in
  per-coworker inboxes, and closes the group — all by writing request files
  into its own `/cowork/control/`. Launching a `{manager}` auto-starts the hub
  (detached; it exits on its own once no manager has run for a minute), and the
  `cowork` CLI (`python3 cowork.py`) drives the same machinery by hand:
  `roster` / `recruit` / `send` / `status` / `serve` / `close`. Messages are
  injected into live sessions; files move as reviewed copies (never into a dir
  its owner is working in); every group keeps an append-only
  `conversation.md`. Watch a team live with
  `tail -f ~/.ai-agents/group_hosting/hub.log`.
- **Interactive picker** — full-screen TUI with type-to-filter, Del to
  delete an instance, F2 to modify its session name and/or workspace.
- **Workspace-aware** — `$PWD` is the default workspace (unless `$PWD` is in
  `DEFAULTING_DIRS` — `$HOME`, `~/Desktop`, `~/Downloads`, `~/Pictures`,
  `~/Videos`, `~/.ssh`, `/tmp`, `/var/tmp`, `/` — in which case it falls back
  to `/ai_workspace`). Picker rows show a workspace hint when applicable:
  `(CURRENT DIR)` (yellow) for rows whose workspace matches `$PWD`,
  `(DEFAULT DIR)` (yellow) when `$PWD` is one of `DEFAULTING_DIRS` and the
  row's workspace is the fallback target, or `(INVALID DIR)` (red) when the
  stored workspace path no longer exists or isn't a directory — hit F2 to
  repoint it.
- **A six-kind tag system, discovered from the file tree** — every agent
  instance composes from members of `agents/{ai,harness,engine,profession,specialty,policy}/`:
  - `⟪AI⟫` — which AI runs it: `⟪Claude⟫`, `⟪Gemini⟫`, `⟪ChatGPT⟫`, `⟪Grok⟫`, each
    coloured after its logo. A member dir holds the vendor, its agent CLI's
    name, its TIER for each of the launcher's CAPABILITY STANDARDS — a model
    and an effort (`efforts.tiers`; the dated standards themselves are the
    kind's shared `agents/ai/capability.standards`). One member is the default;
    each names its default harness.
  - `⟦Harness⟧` — which agent CLI wraps the AI: the vendors' own `⟦ClaudeCode⟧`,
    `⟦GeminiCLI⟧`, `⟦CodexCLI⟧`, `⟦GrokBuild⟧`, and the open multi-model
    `⟦OpenCode⟧`, `⟦OpenClaw⟧`, `⟦Hermes⟧`. A member dir names the vendor, the AIs
    the CLI runs, its binary and its package, and carries the launcher's budget
    words in that CLI's settings (`knobs.mapping`); an instance runs in its
    AI's default harness unless it picks another that runs that AI. Only `⟦ClaudeCode⟧` has
    an adapter in the launcher today, so instances in the other harnesses can
    be described and stored, not yet launched. Picking a harness the AI's
    vendor does not let its SUBSCRIPTION into warns in the form: the price per
    token is the same everywhere (no vendor prices by client), but a plan's
    quota is spendable only in the clients that vendor allows, so elsewhere
    the same work runs on an API key at metered rates. Vendors differ —
    Anthropic offers Pro/Max in Claude Code alone, while OpenAI and xAI let
    their sign-ins into some third-party harnesses — so the permitted set is
    per-AI data (`plan_harnesses` in `agents/ai/<name>/tag.info`), and
    `plans/credentials.md` carries the evidence for maintainers. For
    `⟪Claude⟫` the warning LEADS with a field report of far higher token use
    outside `⟦ClaudeCode⟧`, quoted as the tree states it, hedged and dated;
    the second line names the one published mechanism that could explain a
    gap that size, a harness that does not reuse the prompt cache, so the
    harness can be checked before the model is blamed.
  - `(engine)` — how hard it thinks: a `tag.budget` in the launcher's OWN
    words (a step such as `high`, switches such as `memory = false`, amounts
    such as `max_output_tokens = 36000`) — no AI's key names; the instance's
    AI translates it.
  - `[profession]` — tools it can use: a Dockerfile image layer (`[code]`
    adds Rust + Node + uv; `[webdev]` adds the playwright CLI and the site
    toolbox — `dig`, `whois`, `openssl`).
  - `{specialty}` — exceptional access or running conditions: `{auto}` skips
    permission prompts, `{firewall}` applies an iptables outbound whitelist,
    `{dood}` bind-mounts the host's Docker socket, `{ro}` mounts the
    workspace read-only (and denies the edit tools) for reviewers.
  - `<policy>` — what it's permitted to do: a Claude Code settings fragment
    (`<+qry>` allows WebSearch/WebFetch, `<-su>` denies sudo, `<!plan>`
    mandates plan mode), merged and mounted read-only so the agent can't
    redefine its limits. Colored by stance: orange grants, blue denies,
    white demands. A policy marked `always_on = true` in its tag.info is a
    STATIC tag — applied to every instance unconditionally, shown grayed and
    locked in the form, and never listed in `.lego` files or
    `instances.toml` (`<-su>` ships that way: sudo is denied everywhere).

  Adding a member is a folder with a `tag.info` (and optionally a
  `tag.budget` / `Dockerfile` / `tag.docker` / `policy.json`) — no launcher code. Tree
  position encodes requirements: `profession/code/webdev/` means `[webdev]`
  requires `[code]`. Selections are made in a kind-sectioned form at
  create/modify time — the AI, then the engines, as radio groups up top,
  checkboxes for the rest (requirements auto-check; risky picks and unmet
  companion requests warn in red) — and persist per instance.
- **Shared toolchain caches** — Cargo, npm, pnpm, etc. live under
  `~/.ai-agents/cache/` and bind-mount into `[code]`-tagged containers
  (the base image has no compilers to use them). One agent's downloads
  benefit every later launch. Files older than 7 days are pruned from any
  cache that grows past 5 GB (skipped while a container is running).
- **`{firewall}` — an outbound whitelist the user can extend** — traffic
  from `{firewall}` containers is restricted to a curated built-in
  domain list (~130 entries: Anthropic + GitHub + package registries, plus
  language docs, cloud docs, dev-tooling sites, web standards, ML / data /
  databases). Drop extra entries, one per line, into
  `~/.ai-agents/user_extras/firewall_whitelist.txt` — domains, raw IPv4
  addresses, CIDR ranges (`10.0.0.0/8`), and `*.wildcards` (honored via
  known-CDN-provider ranges — Cloudflare / Fastly / GitHub / CloudFront /
  Google — fetched live from each provider's published list and cached for
  a few days; no address space is baked into the source) are all accepted.
  The launcher resolves entries on the host at launch, keeps re-resolving
  them every few minutes for the container's lifetime (so VPN-exit swaps
  and CDN rotation heal without a relaunch), and the in-container firewall
  self-tests and refuses to launch the agent if enforcement isn't actually
  working. The self-test probes a launcher-resolved address directly
  (`curl --resolve`), so slow or broken container DNS can't fail a healthy
  firewall; the critical Anthropic hosts are additionally whitelisted by
  their registered IP block, not just the momentary A record. Firewall
  startup also probes each nameserver in the container's resolv.conf and
  reorders a dead one off the front (a VPN kill-switch commonly kills
  container→LAN DNS, which otherwise costs ~5s per lookup for the whole
  session). IPv6 egress is denied outright — the whitelist pipeline is
  IPv4-only.
- **Custom slash commands** — drop a markdown file in `custom_commands/` and
  it's available as `/<filename>` inside every agent.
- **Project-wide key bindings** — `settings/keybindings.json` is mounted into
  every agent. Current entries add `Shift+Enter` as newline-without-submit
  alongside the defaults (`Enter` submits, `Ctrl+J` newline, `Alt+Enter`
  newline in most terminals). Edits propagate live, no restart required.
- **Per-workspace skills / commands / CLAUDE.md** — Claude Code auto-discovers
  anything you check into `<workspace>/.claude/` natively (`.claude/skills/<name>/SKILL.md`
  for skills, `.claude/commands/<name>.md` for slash commands, `CLAUDE.md` at the
  repo root for project instructions). No launcher mount step needed — see
  [tips/project_claude_files.md](tips/project_claude_files.md) for the layout,
  priority order, and quirks.
- **Per-workspace prompts** — `@<path>` works on any file to inline its
  contents into a message; the `.prompts/` folder is a discovery convention,
  so the in-container `man` command surfaces those files as ready-to-paste
  `@.prompts/<file>` lines. Absent folder just means `man` shows nothing
  under "Custom prompts".
- **`{dood}` (Docker-out-of-Docker)** — opt in per-instance in the tag
  form. Bind-mounts `/var/run/docker.sock` so the agent can drive the
  host's Docker daemon (run sub-containers, build images). ⚠ effective
  host-root; unticked by default; the form and the launch banner warn in
  red when an instance combines `{dood}` with `{auto}`.
- **Optional credential passthrough** — drop your `~/.aws` (or `gcloud`,
  `kube`, `ssh`, `gh`, `.npmrc`, `.pypirc`) under
  `~/.ai-agents/user_extras/optional_creds/` and the matching CLI
  inside the container picks it up automatically. See *Optional Host
  Mounts* below for the full table.
- **"(Edit Preferences)" menu** — a row in the picker (above the delete menu)
  for professions that expose a configurable install set (today: `[code]`).
  Toggle language toolchains on or off: Rust, Node, and CMake (on by
  default — they were always part of `[code]`), plus opt-in Go, Java,
  Kotlin, and Ruby (off by default; sizes shown in the form). Python is
  shown too but grayed out and un-toggleable — it ships in the base image,
  so it's always available. The focused row's panel names how you run the
  tool and what kind of language it is. Selections persist in
  `~/.ai-agents/code_profile.toml`, which the launcher reads on every
  `[code]` build; each tool's defaults, size, run command, language blurb,
  and the Dockerfile build-arg it drives live in
  `agents/profession/code/template.form`, beside the Dockerfile that
  consumes it. Service CLIs (gh, gcloud, aws, ...) are not part of the form
  — they install on creds-presence, as ever. Global, not per-instance: it's
  the one shared `claude-agents:code` image every `[code]` launch reuses.
  Toggling a value only rebuilds that tool's Docker layer on the next launch.
- **Stale-tag safety** — if an instance's saved tags in `instances.toml`
  name something that no longer exists (a typo, or a tag renamed/removed
  since the instance was set up), the picker flags it on the instance's row
  in a red alert style and refuses to start it, printing which names are bad
  and the valid tags of that kind to pick instead. Fix it by editing
  `instances.toml`, or press F2 on the row to re-pick against the current
  tag set.
- **State auditor** — `python3 -m launch.audit` reports tag-tree faults,
  orphaned state dirs, ghost `instances.toml` entries, bad workspaces,
  entries referencing unknown tags, missing or empty OAuth files, instances
  without a `history.jsonl` trace, and stray instance dirs still at the old
  `~/.ai-agents/` root (they belong under `instances/`).
  Since 2026-09-14 it also flags instances, cluster members and agents that
  name no `ai` / `harness`: they run the tree's defaults, and anything older
  than the two kinds is most likely meant for `claude` in `claude-code`.
  It checks the credentials too: each adapted harness's login files (present,
  valid JSON, mode 600) and each AI's key file (mode 600, docker's grammar,
  the vendor's variable and nothing else).

## Tech Stack & Setup

Host requirements:

- **Docker Engine ≥ 20.10** (plain `docker build` / `docker run` — no compose;
  the install script enforces the floor, and newer is better). On macOS this is
  Docker Desktop (which bundles a recent engine).
- **Python 3.12+**
- Two Python packages: **`prompt_toolkit`** (picker UI) and **`rich`** (markdown
  rendering for agent previews); every launcher-authored file is TOML, read by the stdlib
  — the canonical list lives in `pyproject.toml`'s `[project]` table

Inside the container, the runtime image is built incrementally as a chain of
layers. The root `Dockerfile` (the **base** stage) installs `uv` + ripgrep +
python3 + iptables — what every agent needs whatever CLI it runs in. On top
of that, each image-bearing tag supplies its own Dockerfile from the agents/
tree (`agents/profession/code/Dockerfile` adds `build-essential`, Rust, Node;
`agents/profession/code/webdev/Dockerfile` adds the playwright CLI plus
`dig` / `whois` / `openssl`;
`agents/profession/code/_dood/Dockerfile` is `{dood}`'s layer), plus an
optional `tag.docker` declaring its build-args, mounts, capabilities, and
entrypoint. Run-only specialties (`{auto}`, `{firewall}`) contribute
container config without an image layer. LAST comes the **harness** layer
(`agents/harness/claude-code/Dockerfile`): the agent CLI itself, its env
switches and the image's `ENTRYPOINT`, plus the weekly OS security upgrade —
last so the weekly refresh rebuilds that one layer and nothing beneath it
(until 2026-09-16 the CLI lived in the base and every layer rebuilt weekly).
Images are tagged `claude-agents:base`, `claude-agents:code`,
`claude-agents:code.dood.claude-code`, etc., so common prefixes are cached;
a layer that changes (a rebuilt parent, a toggled toolchain) leaves its old
image untagged — `docker image prune` reclaims those. Nothing else needs to
be on the host for the agents themselves.

### Install — Linux

Run the installer from the project root:

```bash
bash install_dependencies.sh
```

It uses uv to set up a Python venv at `~/pydev` with all three pip deps,
installs Docker via the official convenience script, and adds your user to
the `docker` group. Idempotent —
safe to re-run; if `~/pydev` already exists, it's reused as-is, and the
pip-install step always re-runs so any newly-added dep gets picked up.

### Install — macOS

The same installer works on macOS — it detects Darwin and uses Homebrew to
install Docker Desktop, while the Python side uses uv (same as Linux):

```bash
bash install_dependencies.sh
```

If you don't have Homebrew, the script will print the Docker Desktop
download URL so you can install it manually, then re-run to set up Python.

### Install — Windows

Install [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop)
and [Python 3.12+](https://www.python.org/downloads/) from their official
sites, then `pip install prompt_toolkit rich`
(mirrors `pyproject.toml`'s `[project]` dependencies) in your shell of choice.

Or — recommended — install Docker Desktop on the Windows host and run the
launcher from inside WSL2; once you're at a WSL2 bash prompt, the Linux
installer above takes over.

### Manual install

If you'd rather not use the installer, the *Host requirements* list above is
exhaustive — pick whatever Python/Docker route fits your setup.

### Verify

Activate the venv the installer created so `python3` resolves to it:

```bash
source ~/pydev/bin/activate
```

Then confirm the toolchain:

```bash
docker version
python3 -c "import prompt_toolkit, rich; print('ok')"
```

If either errors out, fix it before proceeding — `run.py` exits early with a
clear message if `docker` isn't on `$PATH`, but the Python packages will only
fail at the picker step.

### Shortcut aliases (`ai` and `q`)

Optional, but ergonomic. With the toolchain in place (`install_dependencies.sh`
must have created the `~/pydev` venv first), install the launcher aliases:

```bash
bash install_rc_alias.sh
```

It adds `ai` → `run.py` (the interactive launcher) and `q` → `quick_question.py`
(the one-shot question tool), writing them into **every shell rc that exists
among `~/.bashrc` and `~/.zshrc`** (macOS defaults to zsh; if neither exists it
creates the one for your login shell). Both aliases call the `~/pydev` venv
python by absolute path, so they work with no venv activation. Any alias you
already have is left untouched and reported as skipped, so it's safe to re-run.
Once activated (open a new shell, or `source` the rc it printed):

```bash
ai                                    # opens the picker
ai poet                               # new instance of poet, no picker
q "why do elephants have big ears?"   # one-shot question (quote it)
q --explain "how do rainbows form?"   # answer + connections & related tidbits (trivia, Opus)
q --research "latest on <topic>?"     # deeper, source-checked (research agent)
q --ai gemini "how old is the moon?"  # ask another AI (q -h lists them; today only claude's CLI can answer)
q --history                           # list past question threads (grey timestamp, id, question)
q --answer                            # reprint the last answer (add an id for an older thread)
q --resume "and their trunks?"        # continue the last thread (add an id for an older one)
```

### Workspace location

Every container bind-mounts a host directory at `/workspace`. By default:

1. `$AI_WORKSPACE` if set in the host environment, else
2. `$PWD`, except when `$PWD` is one of `DEFAULTING_DIRS` (`$HOME`,
   `~/Desktop`, `~/Downloads`, `~/Pictures`, `~/Videos`, `~/.ssh`, `/tmp`,
   `/var/tmp`, `/`) — in which case it falls back to `/ai_workspace` (so
   launching from a "neutral" directory doesn't accidentally share something
   like your entire `$HOME` with the container).

You can also pick the workspace interactively when the launcher prompts. The
form you type (with `~` expanded but symlinks preserved) is stored verbatim in
the instance's `~/.ai-agents/instances.toml` entry.

## How to Run

From the project root:

```bash
python3 run.py                                          # opens the picker
python3 run.py poet                                     # skip picker; new instance of `poet`
python3 run.py poet__myproject                          # skip picker; continue that instance
python3 run.py poet__myproject --dry-run                # run every setup stage; docker build/run print instead of executing (smoke-test the pipeline)
python3 run.py poet__myproject --refresh-installs       # force-retry every optional-CLI install (recover from a transient failure flagged in a prior launch)
```

(`run.py` has a shebang; `chmod +x run.py` once and you can run `./run.py …`
directly.)

The `cowork` tool drives multi-agent groups by hand (a `{manager}` agent issues
the same verbs itself through its `/cowork/control/` channel — see the Features
list):

```bash
python3 cowork.py status                                # hub + every group, rounds, waiting inboxes
python3 cowork.py roster --as boss__proj                # who boss__proj could recruit
python3 cowork.py recruit boss__proj widget golem__api  # create/extend group boss__proj-widget
python3 cowork.py send boss__proj-widget golem__api "please review retry.py" --with-files
python3 cowork.py serve                                 # run the hub loop (run.py auto-starts one per {manager} launch)
python3 cowork.py close boss__proj-widget               # end a group; its files + conversation.md are kept
```

If you installed the shortcut aliases (see *Shortcut aliases* above), `ai …`
is equivalent to `python3 run.py …`, and `q "…"` runs the quick-question tool.

Two ways in (plans/credentials.md). An API key: put the vendor's variable in
`~/.ai-agents/credentials/keys/<ai>.env` (`ANTHROPIC_API_KEY=…`, one line,
docker's `--env-file` grammar — no quotes, no `export`; the launcher fixes the
mode to 600 and refuses a line docker would mis-read) and every harness that
runs that AI receives it. Or a login: on first launch the CLI walks you through
its OAuth onboarding inside the container, and the resulting files land under
`~/.ai-agents/credentials/<harness>/` (Claude Code: `.credentials.json` and
`.claude.json`), bind-mounted read-write into every later container, so you
never re-authenticate per agent. Both present: the key wins — Claude Code reads
`ANTHROPIC_API_KEY` before its stored login and asks once whether to use it —
so a subscription user keeps no key file; the launcher says so before it runs.
A key in the container's environment is readable by every process in it,
every cluster member, and `docker inspect`; prefer the login where the vendor
allows it. A cluster's members likewise share the operator's optional
credentials (`user_extras/optional_creds/`), exactly as every solo instance
does — one container, one set of mounts, named in the launch banner.

A successful launch prints a banner summarising the resolved agent (chain,
skills, optional creds, whitelist), then builds each chain step incrementally,
then drops you into Claude Code:

```
  Agent definition: agents/researcher.md
  Engine:           (researcher) — agents/engine/researcher
  Professions:      [code]
  Specialties:      {auto} {frwl}
  User whitelist:   3 domains (from ~/.ai-agents/user_extras/firewall_whitelist.txt)
  Building base → claude-agents:base...
  Building code → claude-agents:code...
[docker build output]
init-firewall.sh: testing enforcement...
[Claude Code starts; status line shows: ● Researcher - Myproject ( /path/to/workspace )]
[meanwhile the launcher streams the rest of the whitelist into iptables as
 hosts resolve, then keeps re-resolving every few minutes for the session]
```

Lines for each tag axis, optional creds, and whitelist are conditional —
they only appear when the relevant feature is in play. Advisory warnings
(an unmet companion request like `{auto}` without `{firewall}`) print in
red under the banner.

### Picker controls

| Key | Action |
|-----|--------|
| ↑ / ↓ | Move between rows |
| (any printable character) | Filter rows by substring. The rules between clusters stay put, so members of two clusters that match the same word never read as one roster |
| Backspace | Edit the filter |
| Enter | Select — launch the highlighted instance or cluster, or create from an agent / cluster template. Inert on a cluster member: members launch with their cluster |
| Del | Delete the highlighted row (with confirmation): an instance and its state dir, a cluster and its members, or one member out of its cluster |
| F2 | Redefine the highlighted row in one form: an instance's project path, name and tags; a cluster's tags, then its name, project and membership; a member's own tags (the cluster's show locked and marked `(from the cluster)`; a tag the row's scope cannot carry — `{dood}` per member, `{clstr}` on a solo instance — is greyed with the reason, e.g. `cluster-wide only: F2 on the cluster row`) |
| F12 | Hide / show the preview pane. While it is hidden the list takes the full width and nothing is computed for the pane — no transcript is read for the highlighted row |
| F8 | Toggle the composition legend — overlays one table per kind (AIs / harnesses / engines / professions / specialties / policies) in the preview pane, explaining each tag. Esc closes it without leaving the picker. |
| Esc / Ctrl-C | Cancel and exit |

Rows for existing things — instances, clusters, cluster members — share one
anatomy: tags, name, then the workspace path in a column, tagged
`(CURRENT DIR)` when it is where you launched from, `(DEFAULT DIR)` when it
is the default workspace and you launched from a neutral dir like `$HOME`,
or `(INVALID DIR)` when the stored path no longer exists. The right-hand
pane previews the highlighted row: for an instance, a cluster or a member
it lists the same facts — workspace, engine, state dir, when it was last
used (a cluster goes by its latest member) and, for instances and members,
the last prompt typed into it — then every active tag expanded to its full
name and description. A member's pane marks the tags it inherits from its
cluster `(cluster-wide)`; a cluster's pane lists its members with their
own tags and last use.

### Audit

Inspect persistent state for issues:

```bash
python3 -m launch.audit
```

Reports tag-tree faults (malformed `tag.info`, dangling references),
orphans (state dirs without an agent .md), stray instance dirs still at the
`~/.ai-agents/` root (they now live under `instances/`), ghost
`instances.toml` entries (entry without a state dir), bad workspaces (entry points nowhere),
entries referencing unknown tags or the wrong axis, missing/empty OAuth
files, names the launcher's label rule refuses (an instance's session or a
cluster's directory name — such a cluster is skipped by discovery and never
shows in the picker), clusters whose `cluster.toml` fails to load, and
instances with no `history.jsonl` (the file the picker uses for the "Last
used" hint). Prints `All clear. N instance(s)…` when nothing is
wrong. It's read-only. `python3 -m launch.audit -h` prints the full check list.

## Adding an Agent

1. Create `agents/<name>.md`. The first line becomes the picker label; the
   whole file becomes the agent's `CLAUDE.md` inside the container.
2. Create `agents/<name>.lego` — the agent's default tag selections, all
   fields optional:

   ```toml
   engine = "thinker"          # agents/engine/<name>/ — omit to use an engine named after the agent, else "default"
   professions = ["code"]      # image layers the agent starts with
   specialties = []            # pre-ticked specialties (users usually opt in per instance)
   policies = []               # pre-ticked settings fragments
   ```

   The `.lego` only sets the form's *starting point* — every instance can
   deviate at create/modify time, and the chosen set persists per instance
   in `instances.toml`.
3. (Optional) Give the agent its own engine: `agents/engine/<name>/` with a
   `tag.info` (description) and a `tag.budget` in the launcher's own words —
   `effort_tier` (`cheapest`, `best`, or a dated quarter from
   `agents/ai/capability.standards` — the frontier's level then, e.g. `2025Q4`),
   switches (`thinking`, `memory`, `background_agents`, `telemetry`,
   `tool_search`) and amounts (`max_output_tokens`, `tool_output_tokens`,
   `compact_at_percent`). Every `agents/ai/*/efforts.tiers` says which model
   and effort meet that tier on that AI, and the harness's `knobs.mapping`
   translates the rest into that CLI's settings. Nested
   engine folders overlay their parent's budget key-by-key.
4. Re-run `python3 run.py` — the new agent appears in the picker, grouped by
   profession set and sorted by its engine's capability standard (most
   capable first), the same order whatever AI runs.

### Adding a tag

Every tag kind is discovered from the tree — a new member is a folder, not
launcher code:

- **AI**: `agents/ai/<name>/{tag.info, efforts.tiers}` — `tag.info`
  adds `vendor`, `harness` (the key of its default `agents/harness/` member),
  `default` (exactly one member says true) and the tag's own colours `fg` /
  `bg` as hex; `efforts.tiers`
  answers every capability standard (`[cheapest]`, each dated quarter of
  `agents/ai/capability.standards`, `[best]` — a `model` and an `effort` each,
  the AI's `[scale]` of effort words) with the Artificial Analysis index and
  the token cost as comments.
- **Harness**: `agents/harness/<name>/{tag.info, knobs.mapping}` — `tag.info`
  adds `vendor`, `ais` (the AI members the CLI runs: an AI's default harness
  must list it, and an instance pairing a harness with an AI it cannot run
  falls back to the AI's own), `binary` and `package`; `knobs.mapping` maps the
  budget purposes (`[model]`, `[effort]`, `[thinking.on]`, `[memory.off]`,
  `[max_output_tokens]` …) to that CLI's native settings as `{value}` templates,
  with `{value/100}` and `{value*4}` for unit conversions and `{provider}` (from
  a `[providers]` table, AI → the CLI's provider slug) where a multi-model CLI
  spells `anthropic/<model>`. A purpose the CLI cannot express is left out;
  rendering reports it as unmapped. Members do not nest. Launching in one needs
  an adapter in `launch/ai/`, keyed by the member's name.
- **Engine**: `agents/engine/<name>/{tag.info, tag.budget}` — the budget in the
  launcher's own words (above). The descriptions name the TIER (cheap,
  everyday, dependable), never a model: the tag form shows the engine's own
  capability standard beside them (`2026Q2`, `cheapest`, `best`), and the F8
  legend, the preview pane and the launch banner show the model the AI runs
  for it.
- **Profession**: `agents/profession/<name>/{tag.info, Dockerfile}` (+
  optional `tag.docker` naming the build-args its Dockerfile consumes).
  Nest it under another profession to declare a requirement
  (`profession/code/webdev/` ⇒ `[webdev]` requires `[code]`).
- **Specialty**: `agents/specialty/<name>/tag.info` (fields: `description`,
  `warn`, `claude_args`, `workspace_readonly`, `forbid_on`, `[wants]`). If it needs an image layer, add a
  hidden `_<name>/Dockerfile` under the profession it depends on (that tree
  position supplies the requirement — see `_dood` under `code/`). Static
  container config (mounts, `cap_add`, `entrypoint`, env forwards) goes in
  `tag.docker`; `agents/specialty/combos.info` holds warnings for risky
  multi-tag combinations.
- **Policy**: `agents/policy/<name>/{tag.info, policy.json}` — the JSON is a
  Claude Code settings fragment. Fragments merge (lists concatenate; a
  scalar conflict aborts the launch naming both policies) on top of
  `settings/settings.json`, and the result is mounted read-only.

**Where a tag may not go — `forbid_on`.** A build lives in one of three
scopes: a **solo** instance (an agent's `.lego` is its defaults), a
**cluster**'s shared tag set (every member inherits it), or a **member**'s own
additions. `forbid_on = ["member"]` in a tag's `tag.info` keeps it out of a
scope, and that one declaration is what every surface reads: the tag form
greys the row with the reason, the F8 legend shows `not on: …`, cluster
creation and launch refuse with the same words, and the audit reports it as
`forbidden_tag`. Shipped: `{dood}` and `{ro}` forbid `member` — one container
has one docker socket and one workspace mount, so they are set on the whole
cluster (the cluster's tag form) and every member inherits them; `{frwl}`
forbids `member` and `cluster` (wrapping a cluster's entrypoint with the
firewall chain is not built); `{clstr}` and `{cc}` forbid `solo` (cluster
creation applies them). The scan insists every tag with container reach (a
`tag.docker` capability, mount, env forward or entrypoint; `workspace_readonly`)
forbids `member`, so the declaration cannot lag the physics. It covers
container features, not image layers: a layer-claiming tag joins the cluster's
union image, cluster-wide in effect but not impossible per member.

`tag.info` is TOML: `short_description` (a few words, shown right next to
the form row and in the F8 legend), `full_description` (the focused row's
body panel — rendered after the tag's underlined `fullname`, which defaults
to the folder name and exists for expansions like dood →
`Docker-outside-of-Docker`), an optional `shortname` (what renders inside the kind's
punctuation — `firewall` displays as `{frwl}`, `web-research` as
`<+qry>`), and an optional `[addendum]` table (`title` + `body`) injected
into CLAUDE.md while the tag is active — bodies may use the launcher
placeholders published in `launch/tags/addendums.py`. Policies also carry
`stance` (`"allow"` / `"deny"` / `"demand"` → orange / blue / white);
specialties carry `warn`, `claude_args`, and `workspace_readonly` (the
`{ro}` flag). A specialty can also claim a hidden `policy/_<name>` settings
fragment — that's how `{ro}` bundles the workspace `:ro` mount with a
Write/Edit/NotebookEdit tool-deny in one tag (the policy-tree analogue of
how `{dood}` claims its `_dood` image layer).

**Editor association:** the launcher's own config files are all TOML —
`*.lego`, `tag.info`, `tag.docker`, `combos.info`, `template.form`, and
`instances.toml`.
Point your editor at the TOML grammar for those extensions/filenames to get
syntax highlighting (e.g. in VS Code, `"files.associations": {"*.lego":
"toml", "*.info": "toml", "*.docker": "toml"}`). An engine's `tag.budget`, an AI's `efforts.tiers` and a harness's `knobs.mapping` are TOML too (map `*.budget`, `*.tiers` and `*.mapping` to `toml` as well).json` is JSON.

## Persistent State Layout

```
~/.ai-agents/
  credentials/                       # two axes (plans/credentials.md): API keys are the AI's, logins are the harness's
    keys/<ai>.env                    # an API key: one 0600 file per AI, docker --env-file grammar (NAME=value verbatim, no quotes, no export) defining the vendor's variable the AI's tag.info names — passed to whichever harness runs that AI
    <harness>/                       # a harness's login state, the files its adapter names, mounted read-write where the CLI keeps them (claude-code/: .credentials.json + .claude.json)
  instances.toml                     # per-instance tag selections + workspace — one table per <agent>__<session> (launcher-owned; the picker's F2 form is the supported editor)
  cache/                             # shared toolchain caches (cargo, npm, …); mounted into [code] agents only
  firewall_cache/                    # {firewall} host-side caches — resolved DNS + per-provider CDN ranges (host-only, TTL'd, rebuilt when stale)
  user_extras/                       # hand-edited, non-project-specific user configuration
    firewall_whitelist.txt           # user-managed extra domains for {firewall} (auto-created with a template preamble; comments + one domain per line)
    optional_creds/                  # opt-in passthrough creds; see "Optional Host Mounts" below
  group_hosting/                     # {cowork} multi-agent state — one dir per participant, mounted at /cowork in its container
    hub.pid / hub.log / hub.state.json   # the hub's singleton pidfile, event log (`tail -f` = watch the team), and private bookkeeping — outside every mount
    <instance-id>/                   # a participant's tree: its per-group working copies, inboxes (<group>@<sender>/), outbox/, control/
  quickie/                           # the `q` tool's state (segregated from the main instances/)
    communal/                        # one shared workspace for quick questions — drop files here
    <gibberish>/                     # one throwaway conversation thread per question
  instances/                         # all instance state dirs live here (keeps the root uncluttered)
    <agent>__<session>/              # one per instance
      CLAUDE.md                      # rewritten each launch: source agent .md + active-tag addendums (project summary pointer, privacy rules, credentials notice, {firewall} guidance — composed by tags/addendums.py)
      settings.json                  # rewritten each launch: settings/settings.json + the instance's policy fragments; mounted READ-ONLY over ~/.claude/settings.json in-container
      projects/-workspace/memory/MEMORY.md   # Claude Code's auto-memory file, agent-owned (the launcher doesn't touch it)
      projects/-workspace/...        # claude's per-project state, incl. history.jsonl
```

(A `<agent>__<session>` dir found directly at the `~/.ai-agents/` root is a
leftover from the pre-`instances/` layout — the launcher only looks in
`instances/` now, so move it there; `python -m launch.audit` flags any stray.)

(Upgrading from before 2026-09-14? The state dir was `~/.claude-agents/`; the
first launch renames it to `~/.ai-agents/` in place — one move, nothing copied.
If both exist, the launcher says so and touches neither. Upgrading from the
pre-tags layout? The first launch folds `agent_workspace_map.json` +
`agent_modes_map.json` into `instances.toml` automatically and renames the
originals `*.pre-rewrite.bak`.)

## Optional Host Mounts

Beyond the always-on bind-mounts (workspace, agent state, OAuth credentials,
shared commands/settings/skills), several paths are **conditional** — applied
only when the relevant host-side resource exists or when the instance opts in.
Each is independent; nothing here is required for a basic launch.

| Mount | Source | Container path | Trigger |
|---|---|---|---|
| Workspace prompts | `<workspace>/.prompts/` | (left in-place at `/workspace/.prompts/`; surfaced by the in-container `man`) | dir present in workspace |
| Toolchain caches | `~/.ai-agents/cache/<rel>` | `/home/claude/<rel>` (cargo/registry, .npm, .cache, …) | instance has the `[code]` profession |
| Firewall scripts | `agents/specialty/firewall/{init-firewall,firewall-entrypoint}.sh` | `/usr/local/bin/` (ro) | instance has `{firewall}` (declared in its `tag.docker`) |
| Docker socket | `/var/run/docker.sock` (host) | `/var/run/docker.sock` | instance has `{dood}` (declared in `_dood/tag.docker`) |
| Optional creds | `~/.ai-agents/user_extras/optional_creds/<service>/` | varies by service (see below) | path exists on host |

### Optional credentials (recognized services)

Drop a directory or file under `~/.ai-agents/user_extras/optional_creds/`
and it gets bind-mounted into the container at the matching default location, so
the corresponding CLI just works. Read-write (cloud CLIs need to refresh
tokens, write cache, etc.). Anything not in this list is ignored — extend
`OPTIONAL_CREDS_MOUNTS` in `launch/paths.py` to recognize more tools.

| `optional_creds/` entry | Container path | CLI | Auto-installed in `[code]` |
|---|---|---|---|
| `aws/`     | `/home/claude/.aws/`                       | `aws`     | ✓ via `uv tool install awscli` |
| `gcloud/`  | `/home/claude/.config/gcloud/`             | `gcloud`, `gsutil` | ✓ via apt (Google Cloud apt repo) — heavy install (~400-500MB) |
| `kube/`    | `/home/claude/.kube/`                      | `kubectl` | ✓ static binary into `~/.local/bin` |
| `ssh/`     | `/home/claude/.ssh/`                       | `ssh`, `git` over ssh | ✓ via apt (`openssh-client`). Launcher also fixes host-side perms before mounting — `700` on the dir, `600` on each file except `*.pub` / `*_hosts` which get `644`. Treat the contents here as *copies* of your everyday keys (or fresh agent-only keys); symlinking from `~/.ssh` propagates the chmod back to the originals. |
| `gh/`      | `/home/claude/.config/gh/`                 | `gh`      | ✓ via apt (GitHub apt repo) |
| `glab/`    | `/home/claude/.config/glab-cli/`           | `glab`    | ✓ via apt (GitLab packagecloud repo) |
| `jira/`    | `/home/claude/.config/.jira/`              | `jira` (ankitpokhrel/jira-cli) | ✓ static binary from GitHub releases. Drop `.config.yml` (server/login) here; put the API key in a plain-text file named `jira/token` — the launcher reads it and forwards as `$JIRA_API_TOKEN` (jira-cli's default token env var). Your Jira host (`<org>.atlassian.net`) needs to be in the {firewall} whitelist for the CLI to reach it. |
| `vercel/`  | `/home/claude/.local/share/com.vercel.cli/`| `vercel`  | ✓ via `npm install -g vercel` |
| `railway/` | `/home/claude/.config/railway/`            | `railway` | ✓ via `npm install -g @railway/cli` |
| `npmrc`    | `/home/claude/.npmrc`                      | `npm` (auth tokens) | — `npm` is in the code image |
| `pypirc`   | `/home/claude/.pypirc`                     | `twine` / pip uploads | — install yourself: `uv tool install twine` |
| `home/`    | each top-level entry → `/home/claude/<name>` | (catch-all loose dotfiles — `.gitconfig`, `.git-credentials`, `.gnupg/`, `.tmux.conf`, etc.) | — Trailing-`/` key in `OPTIONAL_CREDS_MOUNTS` signals "mount the contents of this dir, each at the matching `/home/claude/<name>`". Files become file mounts; subdirectories become whole-dir mounts. Subdirs of `home/` itself are NOT walked. The launcher refuses to shadow a target already mounted by something else (e.g. `home/.bashrc` colliding with the bundled `settings/bashrc.sh`) and halts with a clear message. |

**Auto-install:** for entries marked ✓, dropping the credentials dir on the
host also flips an `INSTALL_<TOOL>=1` build-arg, and the `[code]`
Dockerfile (`agents/profession/code/Dockerfile`) installs the CLI on the
next `[code]` build. Each tool gets its own ARG, so adding a new credential
only invalidates that tool's layer (downstream layers re-run as no-ops).
Creds-presence is the only driver for these CLIs — the "(Edit Preferences)" form
covers the language toolchains, a disjoint set.
Removing a credential reverses it on the next build. Auto-install only
happens for `[code]` agents; others get the credentials passthrough but no
CLI (those agents probably don't need cloud tools anyway).

**Token files** (`<service>/token`): for services that authenticate via an
env-var token (currently `jira` → `$JIRA_API_TOKEN`), put the secret in a
plain-text file at `~/.ai-agents/user_extras/optional_creds/<service>/token`. The
launcher reads its trimmed contents at launch and forwards as the matching
env var (the CLI in the container picks it up the same way it does on your
host). Service→env-var mapping lives in `OPTIONAL_CREDS_TOKEN_ENV_VARS`
(`launch/paths.py`); to add a new tokened service, add one entry there —
the launcher forwards every present token as a `-e` flag on `docker run`.

To enable AWS in any `[code]` agent, for example:

```bash
mkdir -p ~/.ai-agents/user_extras/optional_creds
ln -s ~/.aws ~/.ai-agents/user_extras/optional_creds/aws    # symlink so host edits propagate
# or:  cp -r ~/.aws ~/.ai-agents/user_extras/optional_creds/aws
```

Next launch of any `[code]` agent: the code image rebuilds with `awscli`
installed, and the mounted creds make it ready to use.

### Resilient installs + `--refresh-installs`

Each `INSTALL_<TOOL>` RUN block in `agents/profession/code/Dockerfile` wraps its install
steps in a `{ … } || { echo <tool> >> /var/log/claude-agents/install_failures.log; }`
guard. A failed install (curl 403, apt repo unreachable, GitHub API rate
limit, etc.) appends the tool's name to the failure log and the RUN exits
0 — **the build never aborts on a single optional install going sideways**.
After `ensure_image` completes, the launcher reads the log from the just-
built image and, if it's non-empty, surfaces a press-any-key warning with
the failed tool names + the exact retry command:

```
  ⚠ Failed installs: jira
  To retry the installation, re-run with --refresh-installs:
    python3 run.py poet__myproject --refresh-installs

  [press any key to continue]
```

The keypress gate sits between the build and `docker run`, so the warning
isn't immediately clobbered when Claude Code's TUI takes over.

`--refresh-installs` (on `run.py` and on `cluster.py launch`, where the union
image gets the same prompt with `python3 cluster.py launch <session>
--refresh-installs` as its retry line) re-pulls the Debian base and busts both
cache-buster build-args (`SOFTWARE_STACK_REFRESH` and `FORCE_INSTALLS_REFRESH`)
with a per-launch timestamp, forcing every install layer to rebuild —
already-installed tools fast-path through their package manager's no-op
(`apt install -y` on a present package, `npm install -g` on a present global,
etc.); previously-failed installs get a fresh shot. Successful installs strip
their own name from the failure log so the warning clears once a retry
actually works.

### What rebuilds when — the cache cadence

Three cadences, each on its own build-arg, so an ordinary launch hits the
cache everywhere:

- **Weekly** (`SOFTWARE_STACK_REFRESH`, `%Y-W%W`): the harness layer only —
  the agent CLI reinstalls (its autoupdater is off, so this is its only
  update path) and `apt-get upgrade` patches every OS package in the image,
  whatever layer installed it. It is the LAST layer, so nothing else rebuilds.
- **On demand** (`FORCE_INSTALLS_REFRESH`, via `--refresh-installs`): `uv`,
  `rich-cli`, every `[code]` toolchain and service CLI, `ruff`, playwright,
  `[self]`'s dev deps — and the Debian base is re-pulled, so the whole stack
  rebuilds from a fresh image.
- **Never on their own**: the apt layers of the base and the professions,
  cached from their first build until a Dockerfile edit or a refresh above.

The first launch after 2026-09-16 rebuilds the stack once (the base
Dockerfile changed); after that, expect a week boundary to rebuild exactly
one layer per chain.

## Project Layout

```
run.py                               # entry point + staged launch() orchestrator (scan tags → migrate store → resolve → resume? → persist → apply tags → setup → build → run). --find TERM searches every past conversation instead of launching (the transcripts on disk, so it reaches what scrolled away). --stop opens a multi-select of RUNNING instances/clusters and stops the picked containers ({mux} highlighted on instance rows — a sticky instance is the one that outlives its terminal; every cluster is sticky by construction), and says so when nothing is running
quick_question.py                    # entry point for the `q` quickie tool → launch.quickie.main (argparse; --explain/--research/--history/--answer/--resume; print-mode one-shot)
Dockerfile                           # base image — uv + python3 + ripgrep + iptables/sudo ({firewall} prerequisites); the agent CLI is the harness layer's (agents/harness/<name>/Dockerfile, built last); built with `network: host` to dodge BuildKit DNS issues
cluster.py                           # entry point for CLUSTER mode (PoC) — N cohabiting agents in one container, switched between in one multiplexer (herdr or tmux — the ui_profile.toml preference, editable from the picker's "(Edit Preferences)" form) → launch.cluster.cli. Design record: cluster_plan.md
check.sh                             # the quality gate — see "Quality gate" below
.github/workflows/ci.yml             # CI — sets up an environment and calls check.sh
launch/
  paths.py                           # centralised path constants — host (AGENTS_STATE, INSTANCES_FILE, USER_EXTRAS_DIR, OPTIONAL_CREDS_MOUNTS, OPTIONAL_CREDS_TOKEN_ENV_VARS, DEFAULTING_DIRS), container (CLAUDE_HOME_IN_CONTAINER, CLAUDE_CONFIG_IN_CONTAINER, SKILLS_IN_CONTAINER), bind-mount dicts (DOCKER_BASE_MOUNTS, CACHE_MOUNTS), path-builder lambdas. Import root: zero internal deps.
  utils.py                           # domain-neutral helpers — plural, relative_time, ordering_index_or_end, split_host_port, prompt_keypress, call_or_exit. No disk access. Leaf module.
  ai/                                # the code half of "which harness runs" — LEAF package: catalog.py (DEFAULT_HARNESS_KEY + the call-time active_harness_key() / set_active_harness()), adapter.py (Adapter: an agent CLI's names — binary, flags, config-root files, env vars, hosts, and its AuthFile list: the login files under credentials/<harness>/, each with its container anchor and mount mode), claude_code.py (the one adapter), __init__ (ADAPTERS keyed by the harness member, adapter_for / active_adapter — a LookupError for a harness without one — refusal_for, adopt)
  file_access.py                     # every disk-touching call routes through here — agent_md_index, atomic write_text, force_remove (sudo + `sudo -k` fallback), per-instance state-dir probes, optional-creds discovery, iter_conversation_dirs (every state dir that can hold a conversation: instances, cluster members, quickie threads).
  transcripts.py                     # reading Claude Code's session records out of a state dir — is there anything --continue could load and how big, when was it last used, what was said last, and find_turns (every spoken turn containing a term, sub-agent transcripts included).
  transcript_format.py               # ONE transcript LINE, parsed — pure, stdlib-only, no intra-package imports, because this same FILE is RO-mounted into containers as `_transcript_format.py` so the in-container search (`alt+f`) reads a transcript by exactly the rules the host's `--find` does.
  history_find.py                    # `--find TERM`: labels each conversation dir, groups the hits, ranks by newest MATCH, prints them. No index — a parsed scan of a 36 MB corpus measured 0.22 s.
  tags/                              # the tag system — kinds as classes, members discovered from agents/
    base.py                          #   Tag record + DockerContribution + tag.info/tag.docker parsing + the STRICT tree rule + TagError
    ai.py, engine.py, profession.py, #   the five kind classes, each with its own scanner; ai renders an engine's budget in its settings
    specialty.py, policy.py          #   (Ai.render); profession discovers hidden `_<name>` layers; specialty adds combos.info; policy adds merge_fragments
    budget.py                        #   the engine budget vocabulary — Budget (tag.budget), the capability-standard spelling and order (cheapest < YYYYQn < best), the switches and amounts every AI's knobs.mapping translates
    registry.py                      #   scan_all(agents_dir) → Registry: discover + cross-validate + look up
    lego.py                          #   AgentBuild + `.lego` loading (an agent's default tag selections)
    identity.py                      #   Agent (pickable) + Instance (fully-resolved launch: ai, chain, build_steps, docker_contributions, conf = the engine's budget rendered by the AI, model, effort, claude_args, unmet_wants)
    store.py                         #   instances.toml load/save (stdlib tomllib in; small TOML emitter out)
    toolkit_profile.py               #   per-profession <profession>_profile.toml — "(Edit Preferences)" install toggles ([code]); same tomllib-in / emitter-out shape as store
    migrations.py                    #   ISOLATED one-shot conversions from retired on-disk formats (legacy two-map JSON → instances.toml)
    addendums.py                     #   chain-keyed CLAUDE.md addendum copy + compose()
  container_env.py                   # env staging — ContainerEnvKey enum, the staged-value accumulator, `-e`/build-arg formatters, set_container_env orchestrator.
  docker_config.py                   # plain-docker orchestration — ensure_image (base + per-layer `docker build`), run_container (`docker run` assembly), tag.docker flag emitters (build_arg_flags / env_forward_flags / entrypoint_flags), bind-mount accumulator, docker CLI wrappers, dry-run gate, prompt_install_failures.
  tag_handlers.py                    # apply_tags(instance): stages declarative tag.docker mounts, then dispatches per-tag `_apply_<name>` handlers (code cache prep/prune, dood GID staging, firewall DNS kickoff). Tags without a handler are data-only no-ops.
  firewall/                          # {firewall} subsystem (package): __init__ facade + resolver.py (two-phase DNS resolution — sync Phase 1 → streaming Phase 2 via docker exec iptables -I, CDN widening, cross-launch resolved-IP cache; getent on Linux, socket.getaddrinfo fallback where absent e.g. macOS) + whitelist.py (entry expansion) + status.py (agent-visible domains_pending_resolve.yml). Host caches live in ~/.ai-agents/firewall_cache/; curated domain list in template_code/firewall_domains.py.
  agents_crud.py                     # instance-state CRUD — instances.toml writers (persist/delete/modify), install_latest_md + install_settings (state-dir CLAUDE.md + merged settings.json), resolve_pick, picker-entry factories, engine sort keys.
  user_additions.py                  # optional_creds mounts + plant_user_extras (readme always; firewall_whitelist.txt under {firewall}).
  gui/                               # TUI subpackage (sole prompt_toolkit importer; run.py uses its __init__ re-exports). Nine modules, one role each, imported strictly one way — styles.py (the style system + tag colours every surface draws with) -> form_core.py (FormOption/TextField/FormResult, the confirm gate, and run_form: the ONE scaffold both full-screen forms run on; checkbox_form is the multi-select row model on it) -> forms.py (the instance + cluster-wide tag forms, the merged "(Edit Preferences)" form) & cluster_form.py (the membership form: its agent rows + add/remove keys on the scaffold) -> picker_previews.py (session_preview — the one pane instances, cluster members and clusters render through — + the child-process transcript read) & picker_prompts.py (line prompts, inline dialogs, field validators) -> picker_flows.py (what each picker key MEANS: cluster create/edit, member re-tag, removals) & picker_widget.py (the reusable picker: row models incl. WorkspaceView / ContEntry / MemberEntry, the off-thread preview loader, the selection loop) -> menu_picker.py (the launcher's menus: select_agent, the deletion submenu, --stop's selector, the F8 legend; the shared Cont-row anatomy every existing thing wears).
  cowork/                            # {cowork}/{manager} multi-agent group hosting — leaf consumer of the core: group (durable state) + mailbox (messages + capture attribution) + sync (file plane) + journal + roster + control (agent-facing verbs) + lifecycle (hub singleton) + relay (the loop) + cli. Owns no docker calls; injection lives in docker_config. cowork.py at the repo root is its thin entry.
  cluster/                           # {mux}/{clstr} COHABITING agents (PoC) — leaf consumer too: member (identity + name legality) + legoset (cluster templates) + state (cluster.toml) + worktree (writer safety) + tmux/herdr (multiplexer assembly — herdr by default; the switch is ~/.ai-agents/ui_profile.toml's herdr_instead_of_tmux, edited from the picker) + launch_plan + cli. cluster.py at the root is its entry, cluster_plan.md the design record.
  quickie/                           # the `q` one-shot-question tool — leaf consumer of the core: cli.py (argparse dispatch) → ask.py (fixed-build Instance under quickie/<gibberish>, stream-json run) + render.py (thinking ticker + streamed answer) + history.py (--history listing / --answer replay). quick_question.py at the repo root is its thin entry.
  claude_code_config.py              # Claude-Code-side UX — build_status_line(instance) + set_terminal_title(name). Leaf-shaped.
  audit.py                           # state-correctness checker (run as `python -m launch.audit`).
  template_code/                     # user-facing copy / data. Pure data, no logic.
    docker_prompts.py                #   docker-side strings — build-step progress, {firewall} waiting line, install-failure prompt copy
    firewall_domains.py              #   the curated built-in whitelist domains (~135 entries)
  template_files/                    # first-launch user-side files (firewall_whitelist.txt, optional_creds_readme.txt) planted into ~/.ai-agents/user_extras/.
  tests/                             # unittest suite. Run it together with ruff + mypy via `bash check.sh` from the project root.
agents/                              # agent definitions + the tag tree
  <name>.md, <name>.lego             #   persona + default tag selections, per agent
  ai/capability.standards            #   the dated CAPABILITY STANDARDS every AI answers — a quarter whose frontier model raised the record, its index, its setter; the ends cheapest / best are each AI's own
  ai/<name>/                         #   ⟪AI⟫ members — tag.info (vendor, default harness, default, fg/bg) + efforts.tiers (this AI's tier — model + effort — per standard)
  harness/<name>/                    #   ⟦Harness⟧ members — tag.info (vendor, the AIs it runs, binary, package) + knobs.mapping (budget words → this CLI's native settings; {provider} slugs for a multi-model CLI); an adapter in launch/ai/ keyed by the member's name makes it launchable
  engine/<name>/                     #   (engine) members — tag.info + tag.budget (effort_tier + switches + amounts, AI-neutral; nested folders overlay the parent's)
  profession/code/                   #   [code] — tag.info + Dockerfile + tag.docker; webdev/ nests inside (requires code); _dood/ is {dood}'s hidden image layer
  specialty/{auto,dood,firewall,read-only}/   #   {specialty} members — tag.info (+ tag.docker, scripts); combos.info holds multi-tag warnings
  specialty/cowork/manager/          #   {manager} nests inside {cowork} — nesting IS the requires mechanism, so ticking the inner tag brings the outer one
  specialty/muxer/cluster/           #   {clstr} nests inside {mux} (a cluster is multiplexing); {mux} claims the hidden profession/_muxer layer that installs both backends (herdr — the default — and tmux)
  <name>.legoset                     #   a CLUSTER template — which agents, how many of each, default roles (agents/devteam.legoset)
  policy/{web-research,no-sudo,plan-first,…}/ #   <policy> members — tag.info + policy.json settings fragment (also no-net, no-git, vcs-safe, free-bash, all-actions, hidden _read-only)
custom_commands/                     # launcher-bundled slash commands (mounted into every container)
custom_skills/                       # launcher-bundled skills (mounted into every container)
.claude/commands/                    # workspace-local slash commands for THIS project — auto-discovered when launched here; no mount.
settings/                            # status line + bashrc + base Claude Code settings + keybindings + manifest helpers (mounted into every container)
tips/                                # reference notes. Read by humans, not the launcher.
```

## Quality gate

After changing launcher code, run the gate — the test suite plus the static
checks, one command:

```bash
bash check.sh
```

Exit 0 means the tree passes. CI (`.github/workflows/ci.yml`) runs the same
script; what exactly it checks and why is documented in the script's own header.

To smoke-test a launch without starting a container:
`python3 run.py <instance> --dry-run` walks every stage up to (but not
including) the real docker calls.
