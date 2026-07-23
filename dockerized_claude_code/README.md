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
- **A four-kind tag system, discovered from the file tree** — every agent
  instance composes from members of `agents/{engine,profession,specialty,policy}/`:
  - `(engine)` — how hard it thinks: an `engine.conf` of model/effort env vars.
  - `[profession]` — tools it can use: a Dockerfile image layer (`[code]`
    adds Rust + Node + uv; `[webdev]` adds the playwright CLI).
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
  `Dockerfile` / `tag.docker` / `policy.json`) — no launcher code. Tree
  position encodes requirements: `profession/code/webdev/` means `[webdev]`
  requires `[code]`. Selections are made in a kind-sectioned form at
  create/modify time — engines as a radio group up top, checkboxes for the
  rest (requirements auto-check; risky picks and unmet companion requests
  warn in red) — and persist per instance.
- **Shared toolchain caches** — Cargo, npm, pnpm, etc. live under
  `~/.claude-agents/cache/` and bind-mount into `[code]`-tagged containers
  (the base image has no compilers to use them). One agent's downloads
  benefit every later launch. Files older than 7 days are pruned from any
  cache that grows past 5 GB (skipped while a container is running).
- **`{firewall}` — an outbound whitelist the user can extend** — traffic
  from `{firewall}` containers is restricted to a curated built-in
  domain list (~130 entries: Anthropic + GitHub + package registries, plus
  language docs, cloud docs, dev-tooling sites, web standards, ML / data /
  databases). Drop extra entries, one per line, into
  `~/.claude-agents/user_extras/firewall_whitelist.txt` — domains, raw IPv4
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
  `~/.claude-agents/user_extras/optional_creds/` and the matching CLI
  inside the container picks it up automatically. See *Optional Host
  Mounts* below for the full table.
- **"Edit Toolkits" menu** — a row in the picker (above the delete menu)
  for professions that expose a configurable install set (today: `[code]`).
  Toggle language toolchains on or off: Rust, Node, and CMake (on by
  default — they were always part of `[code]`), plus opt-in Go, Java,
  Kotlin, and Ruby (off by default; sizes shown in the form). Python is
  shown too but grayed out and un-toggleable — it ships in the base image,
  so it's always available. The focused row's panel names how you run the
  tool and what kind of language it is. Selections persist in
  `~/.claude-agents/code_profile.toml`, which the launcher reads on every
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
  entries referencing unknown tags, missing or empty OAuth files, and
  instances without a `history.jsonl` trace.

## Tech Stack & Setup

Host requirements:

- **Docker Engine** (plain `docker build` / `docker run` — no compose)
- **Python 3.12+**
- Three Python packages: **`prompt_toolkit`** (picker UI), **`python-dotenv`**
  (`engine.conf` parsing), **`rich`** (markdown rendering for agent previews)
  — the canonical list lives in `pyproject.toml`'s `[project]` table

Inside the container, the runtime image is built incrementally as a chain of
layers. The root `Dockerfile` (the **base** stage) installs Claude Code +
`uv` + ripgrep + iptables — what every agent needs. On top of that, each
image-bearing tag supplies its own Dockerfile from the agents/ tree
(`agents/profession/code/Dockerfile` adds `build-essential`, Rust, Node;
`agents/profession/code/webdev/Dockerfile` adds the playwright CLI;
`agents/profession/code/_dood/Dockerfile` is `{dood}`'s layer), plus an
optional `tag.docker` declaring its build-args, mounts, capabilities, and
entrypoint. Run-only specialties (`{auto}`, `{firewall}`) contribute
container config without an image layer. Intermediate images are tagged
`claude-agents:base`, `claude-agents:code`, `claude-agents:code.dood`,
etc., so common prefixes are cached. Nothing else needs to be on the host
for the agents themselves.

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
sites, then `pip install prompt_toolkit python-dotenv rich`
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
python3 -c "import prompt_toolkit, dotenv, rich; print('ok')"
```

If either errors out, fix it before proceeding — `run.py` exits early with a
clear message if `docker` isn't on `$PATH`, but the Python packages will only
fail at the picker step.

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
the instance's `~/.claude-agents/instances.toml` entry.

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

On first launch, Claude Code walks you through OAuth onboarding inside the
container; the resulting `~/.claude-agents/.claude.json` and
`~/.claude-agents/.credentials.json` are bind-mounted into every subsequent
container, so you never have to re-authenticate per agent.

A successful launch prints a banner summarising the resolved agent (chain,
skills, optional creds, whitelist), then builds each chain step incrementally,
then drops you into Claude Code:

```
  Agent definition: agents/researcher.md
  Engine:           (researcher) — agents/engine/researcher
  Professions:      [code]
  Specialties:      {auto} {frwl}
  User whitelist:   3 domains (from ~/.claude-agents/user_extras/firewall_whitelist.txt)
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
| (any printable character) | Filter rows by substring |
| Backspace | Edit the filter |
| Enter | Select |
| Del | Delete the highlighted instance (with confirmation) |
| F2 | Redefine an instance — walks through workspace, session name, and the tag form |
| F8 | Toggle the composition legend — overlays one table per kind (engines / professions / specialties / policies) in the preview pane, explaining each tag. Esc closes it without leaving the picker. |
| Esc / Ctrl-C | Cancel and exit |

### Audit

Inspect persistent state for issues:

```bash
python3 -m launch.audit
```

Reports tag-tree faults (malformed `tag.info`, dangling references),
orphans (state dirs without an agent .md), ghost `instances.toml` entries
(entry without a state dir), bad workspaces (entry points nowhere),
entries referencing unknown tags or the wrong axis, missing/empty OAuth
files, and instances with no `history.jsonl` (the file the picker uses for
the "Last used" hint). Prints `All clear. N instance(s)…` when nothing is
wrong.

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
3. (Optional) Give the agent its own engine: `agents/engine/<name>/` with an
   `engine.conf` (env vars like `ANTHROPIC_MODEL`, `CLAUDE_CODE_EFFORT_LEVEL`)
   and a `tag.info` (description). Nested engine folders overlay their
   parent's conf key-by-key.
4. Re-run `python3 run.py` — the new agent appears in the picker, grouped by
   profession set and sorted by engine model family (Fable > Opus > Sonnet >
   Haiku) then version.

### Adding a tag

Every tag kind is discovered from the tree — a new member is a folder, not
launcher code:

- **Engine**: `agents/engine/<name>/{tag.info, engine.conf}`.
- **Profession**: `agents/profession/<name>/{tag.info, Dockerfile}` (+
  optional `tag.docker` naming the build-args its Dockerfile consumes).
  Nest it under another profession to declare a requirement
  (`profession/code/webdev/` ⇒ `[webdev]` requires `[code]`).
- **Specialty**: `agents/specialty/<name>/tag.info` (fields: `description`,
  `warn`, `claude_args`, `[wants]`). If it needs an image layer, add a
  hidden `_<name>/Dockerfile` under the profession it depends on (that tree
  position supplies the requirement — see `_dood` under `code/`). Static
  container config (mounts, `cap_add`, `entrypoint`, env forwards) goes in
  `tag.docker`; `agents/specialty/combos.info` holds warnings for risky
  multi-tag combinations.
- **Policy**: `agents/policy/<name>/{tag.info, policy.json}` — the JSON is a
  Claude Code settings fragment. Fragments merge (lists concatenate; a
  scalar conflict aborts the launch naming both policies) on top of
  `settings/settings.json`, and the result is mounted read-only.

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
"toml", "*.info": "toml", "*.docker": "toml"}`). `engine.conf` is dotenv
(`KEY=value`), and `policy.json` is JSON.

## Persistent State Layout

```
~/.claude-agents/
  .claude.json                       # shared OAuth account info
  .credentials.json                  # shared API credentials
  instances.toml                     # per-instance tag selections + workspace — one table per <agent>__<session> (launcher-owned; the picker's F2 form is the supported editor)
  cache/                             # shared toolchain caches (cargo, npm, …); mounted into [code] agents only
  user_extras/                       # hand-edited, non-project-specific user configuration
    firewall_whitelist.txt           # user-managed extra domains for {firewall} (auto-created with a template preamble; comments + one domain per line)
    optional_creds/                  # opt-in passthrough creds; see "Optional Host Mounts" below
  <agent>__<session>/                # one per instance
    CLAUDE.md                        # rewritten each launch: source agent .md + active-tag addendums (project summary pointer, privacy rules, credentials notice, {firewall} guidance — composed by tags/addendums.py)
    settings.json                    # rewritten each launch: settings/settings.json + the instance's policy fragments; mounted READ-ONLY over ~/.claude/settings.json in-container
    projects/-workspace/memory/MEMORY.md   # Claude Code's auto-memory file, agent-owned (the launcher doesn't touch it)
    projects/-workspace/...          # claude's per-project state, incl. history.jsonl
```

(Upgrading from the pre-tags layout? The first launch folds
`agent_workspace_map.json` + `agent_modes_map.json` into `instances.toml`
automatically and renames the originals `*.pre-rewrite.bak`.)

## Optional Host Mounts

Beyond the always-on bind-mounts (workspace, agent state, OAuth credentials,
shared commands/settings/skills), several paths are **conditional** — applied
only when the relevant host-side resource exists or when the instance opts in.
Each is independent; nothing here is required for a basic launch.

| Mount | Source | Container path | Trigger |
|---|---|---|---|
| Workspace prompts | `<workspace>/.prompts/` | (left in-place at `/workspace/.prompts/`; surfaced by the in-container `man`) | dir present in workspace |
| Toolchain caches | `~/.claude-agents/cache/<rel>` | `/home/claude/<rel>` (cargo/registry, .npm, .cache, …) | instance has the `[code]` profession |
| Firewall scripts | `agents/specialty/firewall/{init-firewall,firewall-entrypoint}.sh` | `/usr/local/bin/` (ro) | instance has `{firewall}` (declared in its `tag.docker`) |
| Docker socket | `/var/run/docker.sock` (host) | `/var/run/docker.sock` | instance has `{dood}` (declared in `_dood/tag.docker`) |
| Optional creds | `~/.claude-agents/user_extras/optional_creds/<service>/` | varies by service (see below) | path exists on host |

### Optional credentials (recognized services)

Drop a directory or file under `~/.claude-agents/user_extras/optional_creds/`
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
Creds-presence is the only driver for these CLIs — the "Edit Toolkits" form
covers the language toolchains, a disjoint set.
Removing a credential reverses it on the next build. Auto-install only
happens for `[code]` agents; others get the credentials passthrough but no
CLI (those agents probably don't need cloud tools anyway).

**Token files** (`<service>/token`): for services that authenticate via an
env-var token (currently `jira` → `$JIRA_API_TOKEN`), put the secret in a
plain-text file at `~/.claude-agents/user_extras/optional_creds/<service>/token`. The
launcher reads its trimmed contents at launch and forwards as the matching
env var (the CLI in the container picks it up the same way it does on your
host). Service→env-var mapping lives in `OPTIONAL_CREDS_TOKEN_ENV_VARS`
(`launch/paths.py`); to add a new tokened service, add one entry there —
the launcher forwards every present token as a `-e` flag on `docker run`.

To enable AWS in any `[code]` agent, for example:

```bash
mkdir -p ~/.claude-agents/user_extras/optional_creds
ln -s ~/.aws ~/.claude-agents/user_extras/optional_creds/aws    # symlink so host edits propagate
# or:  cp -r ~/.aws ~/.claude-agents/user_extras/optional_creds/aws
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

`--refresh-installs` busts both cache-buster build-args (`SOFTWARE_STACK_REFRESH`
and `FORCE_INSTALLS_REFRESH`) with a per-launch timestamp, forcing every
install layer in the `[code]` Dockerfile to rebuild — already-installed tools
fast-path through their package manager's no-op (`apt install -y` on a
present package, `npm install -g` on a present global, etc.); previously-
failed installs get a fresh shot. Successful installs strip their own name
from the failure log so the warning clears once a retry actually works.

## Project Layout

```
run.py                               # entry point + staged launch() orchestrator (scan tags → migrate store → resolve → resume? → persist → apply tags → setup → build → run)
Dockerfile                           # base image — Claude Code + uv + ripgrep + iptables/sudo ({firewall} prerequisites); built with `network: host` to dodge BuildKit DNS issues
launch/
  paths.py                           # centralised path constants — host (AGENTS_STATE, INSTANCES_FILE, USER_EXTRAS_DIR, OPTIONAL_CREDS_MOUNTS, OPTIONAL_CREDS_TOKEN_ENV_VARS, DEFAULTING_DIRS), container (CLAUDE_HOME_IN_CONTAINER, CLAUDE_CONFIG_IN_CONTAINER, SKILLS_IN_CONTAINER), bind-mount dicts (DOCKER_BASE_MOUNTS, CACHE_MOUNTS), path-builder lambdas. Import root: zero internal deps.
  utils.py                           # domain-neutral helpers — plural, relative_time, ordering_index_or_end, split_host_port, prompt_keypress, call_or_exit. No disk access. Leaf module.
  file_access.py                     # every disk-touching call routes through here — agent_md_index, atomic write_text, force_remove (sudo + `sudo -k` fallback), per-instance state-dir probes, optional-creds discovery.
  tags/                              # the tag system — kinds as classes, members discovered from agents/
    base.py                          #   Tag record + DockerContribution + tag.info/tag.docker parsing + the STRICT tree rule + TagError
    engine.py, profession.py,        #   the four kind classes, each with its own scanner; profession also discovers
    specialty.py, policy.py          #   hidden `_<name>` layers; specialty adds combos.info; policy adds merge_fragments
    registry.py                      #   scan_all(agents_dir) → Registry: discover + cross-validate + look up
    lego.py                          #   AgentBuild + `.lego` loading (an agent's default tag selections)
    identity.py                      #   Agent (pickable) + Instance (fully-resolved launch: chain, build_steps, docker_contributions, conf, claude_args, unmet_wants)
    store.py                         #   instances.toml load/save (stdlib tomllib in; small TOML emitter out)
    migrations.py                    #   ISOLATED one-shot conversions from retired on-disk formats (legacy two-map JSON → instances.toml)
    addendums.py                     #   chain-keyed CLAUDE.md addendum copy + compose()
  container_env.py                   # env staging — ContainerEnvKey enum, the staged-value accumulator, `-e`/build-arg formatters, set_container_env orchestrator.
  docker_config.py                   # plain-docker orchestration — ensure_image (base + per-layer `docker build`), run_container (`docker run` assembly), tag.docker flag emitters (build_arg_flags / env_forward_flags / entrypoint_flags), bind-mount accumulator, docker CLI wrappers, dry-run gate, prompt_install_failures.
  tag_handlers.py                    # apply_tags(instance): stages declarative tag.docker mounts, then dispatches per-tag `_apply_<name>` handlers (code cache prep/prune, dood GID staging, firewall DNS kickoff). Tags without a handler are data-only no-ops.
  firewall/                          # {firewall} subsystem (package): __init__ facade + resolver.py (two-phase DNS resolution — sync Phase 1 → streaming Phase 2 via docker exec iptables -I, CDN widening, cross-launch resolved-IP cache) + whitelist.py (entry expansion) + status.py (agent-visible domains_pending_resolve.yml). Curated domain list lives in template_code/firewall_domains.py.
  agents_crud.py                     # instance-state CRUD — instances.toml writers (persist/delete/modify), install_latest_md + install_settings (state-dir CLAUDE.md + merged settings.json), resolve_pick, picker-entry factories, engine sort keys.
  user_additions.py                  # optional_creds mounts + plant_user_extras (readme always; firewall_whitelist.txt under {firewall}).
  menu_picker.py                     # prompt_toolkit picker UI + the tag checkbox form (requires-cascade, combos + wants warnings) + F8 legend + workspace/session prompts + launch banner.
  claude_code_config.py              # Claude-Code-side UX — build_status_line(instance) + set_terminal_title(name). Leaf-shaped.
  audit.py                           # state-correctness checker (run as `python -m launch.audit`).
  template_code/                     # user-facing copy / data. Pure data, no logic.
    docker_prompts.py                #   docker-side strings — build-step progress, {firewall} waiting line, install-failure prompt copy
    firewall_domains.py              #   the curated built-in whitelist domains (~135 entries)
  template_files/                    # first-launch user-side files (firewall_whitelist.txt, optional_creds_readme.txt) planted into ~/.claude-agents/user_extras/.
  tests/                             # unittest suite — ~600 tests, <1s. Run via `python3 -m unittest discover -s launch/tests` from the project root.
agents/                              # agent definitions + the tag tree
  <name>.md, <name>.lego             #   persona + default tag selections, per agent
  engine/<name>/                     #   (engine) members — tag.info + engine.conf (nested folders overlay parent conf)
  profession/code/                   #   [code] — tag.info + Dockerfile + tag.docker; webdev/ nests inside (requires code); _dood/ is {dood}'s hidden image layer
  specialty/{auto,dood,firewall}/    #   {specialty} members — tag.info (+ tag.docker, scripts); combos.info holds multi-tag warnings
  policy/{web-research,no-sudo}/     #   <policy> members — tag.info + policy.json settings fragment
custom_commands/                     # launcher-bundled slash commands (mounted into every container)
custom_skills/                       # launcher-bundled skills (mounted into every container)
.claude/commands/                    # workspace-local slash commands for THIS project — auto-discovered when launched here; no mount.
settings/                            # status line + bashrc + base Claude Code settings + keybindings + manifest helpers (mounted into every container)
tips/                                # reference notes. Read by humans, not the launcher.
```
