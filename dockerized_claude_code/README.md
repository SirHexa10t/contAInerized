# Claude Code Agents

A launcher for pre-made Claude Code agents — each persona ships its own model,
parameters, and instructions, but all share OAuth credentials, workspace
configuration, slash commands, and toolchain caches. Each launch boots an
isolated Docker container with persistent per-instance state.

## Features

- **Pre-made agent personas** — drop `agents/<name>.md` (the agent's
  `CLAUDE.md`) plus an optional `agents/<name>.conf` for env vars; the picker
  shows it on the next launch.
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
  `DEFAULTING_DIRS` — `$HOME`, `~/Desktop/Documents/Downloads`, `/tmp`,
  `/var/tmp`, `/` — in which case it falls back to `/ai_workspace`). Rows
  whose workspace matches `$PWD` are tagged `(CURRENT DIR)` in yellow; when
  `$PWD` is one of `DEFAULTING_DIRS`, rows whose workspace is the fallback
  target get `(DEFAULT DIR)` in the same yellow.
- **Per-agent build mode** — append `[prog]` to a filename (e.g.
  `refactorer[prog](thinker).md`) to opt into the heavier Dockerfile stage
  with Rust + Node. Untagged agents get the lighter base image (bash, git,
  ripgrep, Python + uv, Claude Code).
- **Shared toolchain caches** — Cargo, npm, pnpm, etc. live under
  `~/.claude-agents/cache/` and bind-mount into `[prog]`-tagged containers
  (the base image has no compilers to use them). One agent's downloads
  benefit every later launch. Files older than 7 days are pruned from any
  cache that grows past 5 GB (skipped while a container is running).
- **Custom slash commands** — drop a markdown file in `custom_commands/` and
  it's available as `/<filename>` inside every agent.
- **Project-wide key bindings** — `settings/keybindings.json` is mounted into
  every agent. Current entries add `Shift+Enter` as newline-without-submit
  alongside the defaults (`Enter` submits, `Ctrl+J` newline, `Alt+Enter`
  newline in most terminals). Edits propagate live, no restart required.
- **Per-workspace skills** — drop a `.skills/` folder in your workspace with
  one or more `<name>/SKILL.md` files; each becomes a `/<name>` slash command
  scoped to that workspace, with all the skills features (auto-invocation,
  resource bundles, hot reload). Absent folder = no project skills loaded.
- **Per-workspace prompts** — `@<path>` works on any file to inline its
  contents into a message; the `.prompts/` folder is a discovery convention,
  so the in-container `man` command surfaces those files as ready-to-paste
  `@.prompts/<file>` lines. Absent folder just means `man` shows nothing
  under "Custom prompts".
- **DooD (Docker-out-of-Docker) mode** — opt into per-instance, prompted
  at create/modify time for any `[prog]`-tagged agent. Bind-mounts
  `/var/run/docker.sock` so the agent can drive the host's Docker daemon
  (run sub-containers, build images). ⚠ effective host-root; declined by
  default.
- **Optional credential passthrough** — drop your `~/.aws` (or `gcloud`,
  `kube`, `ssh`, `gh`, `.npmrc`, `.pypirc`) under
  `~/.claude-agents/optional_creds/` and the matching CLI inside the
  container picks it up automatically. See *Optional Host Mounts* below
  for the full table.
- **State auditor** — `python3 -m launch.audit` reports orphaned state dirs,
  drifted CLAUDE.md files, ghost workspace-map entries, and missing/empty
  OAuth files.

## Tech Stack & Setup

Host requirements:

- **Docker** + the Compose v2 plugin
- **Python 3.10+** (the launcher uses walrus expressions and structural
  unpacking)
- Three Python packages: **`prompt_toolkit`** (picker UI), **`python-dotenv`**
  (`.conf` parsing), **`rich`** (markdown rendering for agent previews)

Inside the container, a two-stage Dockerfile (`docker/Dockerfile`) supplies
the runtime: the **base** stage installs Claude Code + `uv` + ripgrep — what
every agent needs — and the **prog** stage adds `build-essential`, Rust
(`rustup`), and Node.js for `[prog]`-tagged agents. Nothing else needs to
be on the host for the agents themselves.

### Install — Linux

Run the installer from the project root:

```bash
bash install_dependencies.sh
```

It uses uv to set up a Python venv at `~/pydev` with all three pip deps,
installs Docker via the official convenience script (which bundles the
Compose v2 plugin), and adds your user to the `docker` group. Idempotent —
safe to re-run; if `~/pydev` already exists, it's reused as-is.

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
and [Python 3.10+](https://www.python.org/downloads/) from their official
sites, then `pip install prompt_toolkit python-dotenv rich` in your shell
of choice.

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
docker compose version
python3 -c "import prompt_toolkit, dotenv, rich; print('ok')"
```

If either errors out, fix it before proceeding — `run.py` exits early with a
clear message if `docker` isn't on `$PATH`, but the Python packages will only
fail at the picker step.

### Workspace location

Every container bind-mounts a host directory at `/workspace`. By default:

1. `$AI_WORKSPACE` if set in the host environment, else
2. `$PWD`, except when `$PWD` is `$HOME` — in which case it falls back to
   `/ai_workspace` (so launching from your home directory doesn't accidentally
   share your entire `$HOME` with the container).

You can also pick the workspace interactively when the launcher prompts. The
form you type (with `~` expanded but symlinks preserved) is stored verbatim in
`~/.claude-agents/agent_workspace_map.json`.

## How to Run

From the project root:

```bash
python3 run.py                       # opens the picker
python3 run.py poet                  # skip picker; new instance of `poet`
python3 run.py poet__myproject       # skip picker; continue that instance
```

(`run.py` has a shebang; `chmod +x run.py` once and you can run `./run.py …`
directly.)

On first launch, Claude Code walks you through OAuth onboarding inside the
container; the resulting `~/.claude-agents/.claude.json` and
`~/.claude-agents/.credentials.json` are bind-mounted into every subsequent
container, so you never have to re-authenticate per agent.

A successful launch prints the resolved agent definition and conf, then a
build line, then drops you into Claude Code:

```
  Agent definition: agents/researcher.md
  Configuration:    agents/researcher.conf
  Building image...
[docker compose build output]
[Claude Code starts; status line shows: ● Researcher - Myproject ( /path/to/workspace )]
```

### Picker controls

| Key | Action |
|-----|--------|
| ↑ / ↓ | Move between rows |
| (any printable character) | Filter rows by substring |
| Backspace | Edit the filter |
| Enter | Select |
| Del | Delete the highlighted instance (with confirmation) |
| F2 | Redefine an instance — walks through new session name and new workspace |
| Esc / Ctrl-C | Cancel and exit |

### Audit

Inspect persistent state for issues:

```bash
python3 -m launch.audit
# or:
python3 launch/audit.py
```

Reports orphans, drifted CLAUDE.md files, ghost mapping entries, missing or
empty OAuth files, and instances with no `history.jsonl` (the file the picker
uses for the "Last used" hint). Prints `All clear. N instance(s)…` when nothing
is wrong.

## Adding an Agent

1. Create `agents/<name>.md`. The first line becomes the picker label; the
   whole file becomes the agent's `CLAUDE.md` inside the container.
2. (Optional) Create `agents/<name>.conf` for env vars. **Per-agent `.conf`
   replaces `agents/default.conf` wholesale — not merged.** If you only want
   to change one key, copy the rest of `default.conf` over too, otherwise
   you'll silently lose the defaults you didn't redeclare.

   ```
   ANTHROPIC_MODEL="claude-sonnet-4-6"
   CLAUDE_CODE_EFFORT_LEVEL=low
   ```

3. (Optional) To share a `.conf` between several agents, name the file
   `<name>(<parent>).md` — the parenthesised suffix points at
   `agents/<parent>.conf`. For example, `feature-identifier(thinker).md`
   uses `agents/thinker.conf`.
4. (Optional) Append a `[<tag>]` to the filename to opt into a build mode.
   Currently supported:

   - `[prog]` — uses the `prog` Dockerfile stage (Rust + Node + a real
     compiler) and bind-mounts the shared toolchain caches into the
     container. Untagged agents stay on `base`.

   Tag and conf-alias suffixes can combine and order doesn't matter —
   `refactorer[prog](thinker).md` and `refactorer(thinker)[prog].md` parse
   identically. **Reserved syntax**: `[…]` is reserved for tags, `(…)` for
   the conf alias. Don't use these characters in agent names.

5. Re-run `python3 run.py` — the new agent appears in the picker, sorted by
   model family (Opus > Sonnet > Haiku) then version.

## Persistent State Layout

```
~/.claude-agents/
  .claude.json                       # shared OAuth account info
  .credentials.json                  # shared API credentials
  agent_workspace_map.json           # instance_id → workspace path
  agent_modes_map.json               # instance_id → list of opted-in modes (e.g. ["DooD"])
  cache/                             # shared toolchain caches (cargo, npm, …); mounted into [prog] agents only
  optional_creds/                    # opt-in passthrough creds; see "Optional Host Mounts" below
  <agent>__<session>/                # one per instance
    CLAUDE.md                        # copy of the agent's .md
    projects/-workspace/...          # claude's per-project state, incl. history.jsonl
```

## Optional Host Mounts

Beyond the always-on bind-mounts (workspace, agent state, OAuth credentials,
shared commands/settings/skills), several paths are **conditional** — applied
only when the relevant host-side resource exists or when the instance opts in.
Each is independent; nothing here is required for a basic launch.

| Mount | Source | Container path | Trigger |
|---|---|---|---|
| Workspace skills | `<workspace>/.skills/<name>/` | `/home/claude/.claude/skills/<name>` | dir present in workspace; each becomes a `/<name>` slash command |
| Workspace prompts | `<workspace>/.prompts/` | (left in-place at `/workspace/.prompts/`; surfaced by the in-container `man`) | dir present in workspace |
| Toolchain caches | `~/.claude-agents/cache/<rel>` | `/home/claude/<rel>` (cargo/registry, .npm, .cache, …) | agent filename includes `[prog]` |
| Docker socket | `/var/run/docker.sock` (host) | `/var/run/docker.sock` | instance has DooD mode enabled (asked at create/modify) |
| Optional creds | `~/.claude-agents/optional_creds/<service>/` | varies by service (see below) | path exists on host |

### Optional credentials (recognized services)

Drop a directory or file under `~/.claude-agents/optional_creds/` and it
gets bind-mounted into the container at the matching default location, so
the corresponding CLI just works. Read-write (cloud CLIs need to refresh
tokens, write cache, etc.). Anything not in this list is ignored — extend
`OPTIONAL_CREDS_MOUNTS` in `launch/user_additions.py` to recognize more tools.

| `optional_creds/` entry | Container path | CLI | Auto-installed in `[prog]` |
|---|---|---|---|
| `aws/`     | `/home/claude/.aws/`                       | `aws`     | ✓ via `uv tool install awscli` |
| `gcloud/`  | `/home/claude/.config/gcloud/`             | `gcloud`, `gsutil` | ✓ via apt (Google Cloud apt repo) — heavy install (~400-500MB) |
| `kube/`    | `/home/claude/.kube/`                      | `kubectl` | ✓ static binary into `~/.local/bin` |
| `ssh/`     | `/home/claude/.ssh/`                       | `ssh`, `git` over ssh | — already in `base` image |
| `gh/`      | `/home/claude/.config/gh/`                 | `gh`      | ✓ via apt (GitHub apt repo) |
| `glab/`    | `/home/claude/.config/glab-cli/`           | `glab`    | ✓ via apt (GitLab packagecloud repo) |
| `vercel/`  | `/home/claude/.local/share/com.vercel.cli/`| `vercel`  | ✓ via `npm install -g vercel` |
| `railway/` | `/home/claude/.config/railway/`            | `railway` | ✓ via `npm install -g @railway/cli` |
| `npmrc`    | `/home/claude/.npmrc`                      | `npm` (auth tokens) | — `npm` is in the prog image |
| `pypirc`   | `/home/claude/.pypirc`                     | `twine` / pip uploads | — install yourself: `uv tool install twine` |

**Auto-install:** for entries marked ✓, dropping the credentials dir on the
host also flips an `INSTALL_<TOOL>=1` build-arg, and `Dockerfile.prog`
installs the CLI on the next `[prog]` build. Each tool gets its own ARG, so
adding a new credential only invalidates that tool's layer (downstream
layers re-run as no-ops). Removing a credential reverses it on the next
build. Auto-install only happens for `[prog]`-tagged agents; non-prog
agents get the credentials passthrough but no CLI (those agents probably
don't need cloud tools anyway).

To enable AWS in any `[prog]` agent, for example:

```bash
mkdir -p ~/.claude-agents/optional_creds
ln -s ~/.aws ~/.claude-agents/optional_creds/aws    # symlink so host edits propagate
# or:  cp -r ~/.aws ~/.claude-agents/optional_creds/aws
```

Next launch of any `[prog]` agent: the prog image rebuilds with `awscli`
installed, and the mounted creds make it ready to use.

## Project Layout

```
run.py                               # entry point + CLI shortcuts
launch/
  __init__.py
  agent_composition.py               # filename grammar (parse_stem), conf loading, sort policy, [tag] dispatch
  agents_crud.py                     # state-dir lifecycle, workspace map, picker-entry builders
  user_additions.py                  # per-workspace skills + optional credentials passthrough
  menu_picker.py                     # picker UI + ask_for_workspace
  audit.py                           # state-checker (run as `python -m launch.audit`)
agents/                              # agent definitions (.md + optional .conf)
custom_commands/                     # shared slash commands
settings/                            # status line + bashrc + Claude Code settings
memory/MEMORY.md                     # auto-loaded pointer to /workspace/.claude_summary
docker/                              # multi-stage Dockerfile + compose files (base + prog override)
```
