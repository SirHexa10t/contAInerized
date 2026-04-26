# Claude Code Agents

A launcher for pre-made Claude Code agents — each persona ships its own model,
parameters, and instructions, but all share OAuth credentials, workspace
configuration, slash commands, and toolchain caches. Each launch boots an
isolated Docker container with persistent per-instance state.

## Features

- **Pre-made agent personas** — drop `agents/<name>.md` (the agent's
  `CLAUDE.md`) plus an optional `agents/<name>.conf` for env-var overrides;
  the picker shows it on the next launch.
- **Multiple sessions per agent** — every launch is an instance keyed by
  `<agent>__<session>`, with its own conversation history and workspace.
- **Resume conversations** — picking a "Cont." row auto-passes `--continue`
  to `claude` so the conversation state is restored.
- **CLI shortcuts** — `python3 run.py poet` skips the picker and sets up a
  fresh `poet` instance; `python3 run.py poet__myproject` continues that
  specific instance directly.
- **Interactive picker** — full-screen TUI with type-to-filter, Del to
  delete an instance, F2 to redefine its session name and/or workspace.
- **Workspace-aware** — `$PWD` is the default workspace (unless `$PWD` is
  `$HOME`, in which case it falls back to `/ai_workspace`); rows whose
  workspace matches `$PWD` are tagged `(CURRENT DIR)` in yellow.
- **Shared toolchain caches** — Cargo, uv/pip, npm, pnpm, etc. live under
  `~/.claude-agents/cache/` and bind-mount into every container, with a
  size-based prune when total grows past 5 GB.
- **Custom slash commands** — drop a markdown file in `custom_commands/` and
  it's available as `/<filename>` inside every agent.
- **State auditor** — `python3 -m launch.audit` reports orphaned state dirs,
  drifted CLAUDE.md files, ghost workspace-map entries, and missing/empty
  OAuth files.

## Tech Stack & Setup

Host requirements:

- **Docker** + the Compose v2 plugin
- **Python 3.10+** (the launcher uses walrus expressions and structural
  unpacking)
- Two Python packages: **`prompt_toolkit`** (picker UI) and
  **`python-dotenv`** (`.conf` parsing)

Inside the container, the Dockerfile installs Claude Code, Rust (`rustup`),
and `uv` automatically — nothing else needs to be on the host for the agents
themselves.

### Install — macOS (Homebrew)

```bash
brew install --cask docker         # Docker Desktop — start it once before continuing
brew install python@3.12
pip3 install prompt_toolkit python-dotenv
```

### Install — Linux (Debian/Ubuntu)

```bash
sudo apt install docker.io docker-compose-plugin python3 python3-pip
sudo usermod -aG docker $USER      # log out + back in for the group to take effect
pip3 install --user prompt_toolkit python-dotenv
```

### Install — Windows

Install [Docker Desktop](https://www.docker.com/products/docker-desktop) and
run the launcher from inside WSL2; from there follow the Linux steps.

### Verify

Before the first run, confirm the toolchain is in place:

```bash
docker compose version
python3 -c "import prompt_toolkit, dotenv; print('ok')"
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
`~/.claude-agents/agent_workspace_map.txt`.

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

A successful launch prints a build line, the resolved agent definition, and
the conf file in use, then drops you into Claude Code:

```
  Building image...
  Agent definition: agents/researcher.md
  Configuration:    agents/researcher.conf
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
2. (Optional) Create `agents/<name>.conf` to override env vars from
   `agents/default.conf`:

   ```
   ANTHROPIC_MODEL="claude-sonnet-4-6"
   CLAUDE_CODE_EFFORT_LEVEL=low
   ```

3. (Optional) To share a `.conf` between several agents, name the file
   `<name>(<parent>).md` — the parenthesised suffix points at
   `agents/<parent>.conf`. For example, `refactorer(thinker).md` uses
   `agents/thinker.conf`.
4. Re-run `python3 run.py` — the new agent appears in the picker, sorted by
   model family (Opus > Sonnet > Haiku) then version.

## Persistent State Layout

```
~/.claude-agents/
  .claude.json                       # shared OAuth account info
  .credentials.json                  # shared API credentials
  agent_workspace_map.txt            # JSON: instance_id → workspace path
  cache/                             # shared toolchain caches (cargo, uv, npm, …)
  <agent>__<session>/                # one per instance
    CLAUDE.md                        # copy of the agent's .md
    projects/-workspace/...          # claude's per-project state, incl. history.jsonl
```

## Project Layout

```
run.py                               # entry point + CLI shortcuts
launch/
  __init__.py
  agents_lib.py                      # discovery, naming, conf, sort, redefine, …
  menu_picker.py                     # picker UI + ask_for_workspace
  audit.py                           # state-checker (run as `python -m launch.audit`)
agents/                              # agent definitions (.md + optional .conf)
custom_commands/                     # shared slash commands
settings/                            # status line + bashrc + Claude Code settings
memory/MEMORY.md                     # auto-loaded pointer to /workspace/.claude_summary
Dockerfile, docker-compose.yml       # container build + bind mounts
```
