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
  `DEFAULTING_DIRS` — `$HOME`, `~/Desktop`, `~/Downloads`, `~/Pictures`,
  `~/Videos`, `~/.ssh`, `/tmp`, `/var/tmp`, `/` — in which case it falls back
  to `/ai_workspace`). Picker rows show a workspace hint when applicable:
  `(CURRENT DIR)` (yellow) for rows whose workspace matches `$PWD`,
  `(DEFAULT DIR)` (yellow) when `$PWD` is one of `DEFAULTING_DIRS` and the
  row's workspace is the fallback target, or `(INVALID DIR)` (red) when the
  stored workspace path no longer exists or isn't a directory — hit F2 to
  repoint it.
- **Per-agent build tags & per-instance modes** — append `[code]` to a
  filename (e.g. `refactorer[code](thinker).md`) to opt into the heavier
  Dockerfile stage with Rust + Node + uv. Modes are picked per instance at
  create/modify time: `{auto}` runs the agent unattended (`--dangerously-skip-permissions`)
  behind an iptables outbound whitelist; `{DooD}` bind-mounts the host's
  Docker socket. Tags + modes compose into a layered build chain
  (`base → code → auto`, etc.) — each step has its own `Dockerfile.<name>`
  + `compose.<name>.yml`.
- **Shared toolchain caches** — Cargo, npm, pnpm, etc. live under
  `~/.claude-agents/cache/` and bind-mount into `[code]`-tagged containers
  (the base image has no compilers to use them). One agent's downloads
  benefit every later launch. Files older than 7 days are pruned from any
  cache that grows past 5 GB (skipped while a container is running).
- **`{auto}`-mode firewall with user-extendable whitelist** — outbound
  traffic from `{auto}` containers is restricted to a curated built-in
  domain list (~130 entries: Anthropic + GitHub + package registries, plus
  language docs, cloud docs, dev-tooling sites, web standards, ML / data /
  databases). Drop extra entries, one per line, into
  `~/.claude-agents/user_extras/firewall_whitelist.txt` — domains, raw IPv4
  addresses, or CIDR ranges (`10.0.0.0/8`) are all accepted. The firewall
  resolves entries in parallel at container start, then self-tests and
  refuses to launch the agent if enforcement isn't actually working.
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
- **DooD (Docker-out-of-Docker) mode** — opt into per-instance, prompted at
  create/modify time. Bind-mounts `/var/run/docker.sock` so the agent can
  drive the host's Docker daemon (run sub-containers, build images).
  ⚠ effective host-root; declined by default; the launcher prints a stern
  warning when an instance ends up with both `{auto}` and `{DooD}` enabled.
- **Optional credential passthrough** — drop your `~/.aws` (or `gcloud`,
  `kube`, `ssh`, `gh`, `.npmrc`, `.pypirc`) under
  `~/.claude-agents/user_extras/optional_creds/` and the matching CLI
  inside the container picks it up automatically. See *Optional Host
  Mounts* below for the full table.
- **State auditor** — `python3 -m launch.audit` reports orphaned state dirs,
  ghost workspace-map / modes-map entries, missing or empty OAuth files,
  malformed modes-map values, and instances without a `history.jsonl`
  trace.

## Tech Stack & Setup

Host requirements:

- **Docker** + the Compose v2 plugin
- **Python 3.10+** (the launcher uses walrus expressions and structural
  unpacking)
- Three Python packages: **`prompt_toolkit`** (picker UI), **`python-dotenv`**
  (`.conf` parsing), **`rich`** (markdown rendering for agent previews)

Inside the container, the runtime image is built incrementally as a chain
of layers. `docker/Dockerfile` (the **base** stage) installs Claude Code +
`uv` + ripgrep — what every agent needs. On top of that, each chain step
has its own `Dockerfile.<name>` (e.g. `Dockerfile.code` adds `build-essential`,
Rust, Node; `Dockerfile.auto` adds iptables + sudo for the firewall) and
matching `compose.<name>.yml` (extra mounts, capabilities, entrypoint).
The chain assembled for a given launch is `["base", ...active tags,
...active modes]`; intermediate images are tagged `claude-agents:base`,
`claude-agents:code`, `claude-agents:code.auto`, etc., so common prefixes
are cached. Nothing else needs to be on the host for the agents themselves.

### Install — Linux

Run the installer from the project root:

```bash
bash install_dependencies.sh
```

It uses uv to set up a Python venv at `~/pydev` with all three pip deps,
installs Docker via the official convenience script (which bundles the
Compose v2 plugin), and adds your user to the `docker` group. Idempotent —
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
and [Python 3.10+](https://www.python.org/downloads/) from their official
sites, then `pip install prompt_toolkit python-dotenv rich`
in your shell of choice.

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
2. `$PWD`, except when `$PWD` is one of `DEFAULTING_DIRS` (`$HOME`,
   `~/Desktop`, `~/Downloads`, `~/Pictures`, `~/Videos`, `~/.ssh`, `/tmp`,
   `/var/tmp`, `/`) — in which case it falls back to `/ai_workspace` (so
   launching from a "neutral" directory doesn't accidentally share something
   like your entire `$HOME` with the container).

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

A successful launch prints a banner summarising the resolved agent (chain,
skills, optional creds, whitelist), then builds each chain step incrementally,
then drops you into Claude Code:

```
  Agent definition: agents/researcher[code].md
  Configuration:    agents/researcher.conf
  Tags:             [code]
  Modes:            {auto}
  User whitelist:   3 domains (from ~/.claude-agents/user_extras/firewall_whitelist.txt)
  Building base → claude-agents:base...
  Building code → claude-agents:code...
  Building auto → claude-agents:code.auto...
[docker compose build output]
init-firewall.sh: resolving whitelist of 137 domains (up to 64 in parallel, 8s timeout each)...
init-firewall.sh: resolved all 137 domains.
init-firewall.sh: testing enforcement...
[Claude Code starts; status line shows: ● Researcher - Myproject ( /path/to/workspace )]
```

Lines for tags, modes, optional creds, and whitelist are conditional — they
only appear when the relevant feature is in play.

### Picker controls

| Key | Action |
|-----|--------|
| ↑ / ↓ | Move between rows |
| (any printable character) | Filter rows by substring |
| Backspace | Edit the filter |
| Enter | Select |
| Del | Delete the highlighted instance (with confirmation) |
| F2 | Redefine an instance — walks through new session name and new workspace |
| F8 | Toggle the composition legend — overlays a `Tags` + `Modes` table in the preview pane, explaining what each `[tag]` / `{mode}` marker means. Esc closes it without leaving the picker. |
| Esc / Ctrl-C | Cancel and exit |

### Audit

Inspect persistent state for issues:

```bash
python3 -m launch.audit
```

Reports orphans (state dirs without an agent .md), ghost workspace-map /
modes-map entries (entry without a state dir), bad workspaces (mapping
points nowhere), missing/empty OAuth files, modes-map shape problems
(non-list value, empty list, unknown mode strings), and instances with no
`history.jsonl` (the file the picker uses for the "Last used" hint).
Prints `All clear. N instance(s)…` when nothing is wrong.

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
4. (Optional) Append a `[<tag>]` to the filename to opt into a build tag.
   Tags are per-agent (in the filename) and stable; modes are picked
   per-instance at create-time and stored in `agent_modes_map.json`.

   **Tags** (currently supported):

   - `[code]` — uses the `code` Dockerfile stage (Rust + Node + a real
     compiler) and bind-mounts the shared toolchain caches into the
     container. Untagged agents stay on `base`.

   **Modes** (prompted per instance, also re-prompted on F2-modify):

   - `{auto}` — `--dangerously-skip-permissions` behind an iptables outbound
     whitelist (init-firewall.sh runs at container start). Lets the agent
     run unattended. ⚠ guardrail-only; combine with `{DooD}` and the agent
     can do anything on the host.
   - `{DooD}` — bind-mount `/var/run/docker.sock`. Lets the agent drive the
     host's Docker daemon. ⚠ effective host-root.

   Tag, mode, and conf-alias suffixes can combine and order doesn't matter —
   `refactorer[code](thinker).md` and `refactorer(thinker)[code].md` parse
   identically. **Reserved syntax**: `[…]` for tags, `(…)` for the conf
   alias, `{…}` for modes (modes don't appear in filenames; the syntax is
   reserved for the picker UI). Don't use these characters in agent names.

5. Re-run `python3 run.py` — the new agent appears in the picker, sorted by
   model family (Opus > Sonnet > Haiku) then version.

## Persistent State Layout

```
~/.claude-agents/
  .claude.json                       # shared OAuth account info
  .credentials.json                  # shared API credentials
  agent_workspace_map.json           # instance_id → workspace path
  agent_modes_map.json               # instance_id → list of opted-in modes (e.g. ["auto", "DooD"])
  cache/                             # shared toolchain caches (cargo, npm, …); mounted into [code] agents only
  user_extras/                       # hand-edited, non-project-specific user configuration
    firewall_whitelist.txt           # user-managed extra domains for {auto} mode (auto-created with a template preamble; comments + one domain per line)
    optional_creds/                  # opt-in passthrough creds; see "Optional Host Mounts" below
  <agent>__<session>/                # one per instance
    CLAUDE.md                        # rewritten each launch: source agent .md + active-modifier addendums (project summary pointer, privacy rules, credentials notice, {auto} firewall guidance — composed by memory_addendums.composed_addendum)
    projects/-workspace/memory/MEMORY.md   # Claude Code's auto-memory file, agent-owned (the launcher doesn't touch it)
    projects/-workspace/...          # claude's per-project state, incl. history.jsonl
```

## Optional Host Mounts

Beyond the always-on bind-mounts (workspace, agent state, OAuth credentials,
shared commands/settings/skills), several paths are **conditional** — applied
only when the relevant host-side resource exists or when the instance opts in.
Each is independent; nothing here is required for a basic launch.

| Mount | Source | Container path | Trigger |
|---|---|---|---|
| Workspace prompts | `<workspace>/.prompts/` | (left in-place at `/workspace/.prompts/`; surfaced by the in-container `man`) | dir present in workspace |
| Toolchain caches | `~/.claude-agents/cache/<rel>` | `/home/claude/<rel>` (cargo/registry, .npm, .cache, …) | agent filename includes `[code]` |
| Firewall whitelist | `~/.claude-agents/user_extras/firewall_whitelist.txt` | `/usr/local/etc/firewall_whitelist.txt` (ro) | instance has `{auto}` mode enabled |
| Docker socket | `/var/run/docker.sock` (host) | `/var/run/docker.sock` | instance has `{DooD}` mode enabled (asked at create/modify) |
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
| `ssh/`     | `/home/claude/.ssh/`                       | `ssh`, `git` over ssh | — already in `base` image |
| `gh/`      | `/home/claude/.config/gh/`                 | `gh`      | ✓ via apt (GitHub apt repo) |
| `glab/`    | `/home/claude/.config/glab-cli/`           | `glab`    | ✓ via apt (GitLab packagecloud repo) |
| `jira/`    | `/home/claude/.config/.jira/`              | `jira` (ankitpokhrel/jira-cli) | ✓ static binary from GitHub releases. Drop `.config.yml` (server/login) here; put the API key in a plain-text file named `jira/token` — the launcher reads it and forwards as `$JIRA_API_TOKEN` (jira-cli's default token env var). Your Jira host (`<org>.atlassian.net`) needs to be in the {auto} whitelist for the CLI to reach it. |
| `vercel/`  | `/home/claude/.local/share/com.vercel.cli/`| `vercel`  | ✓ via `npm install -g vercel` |
| `railway/` | `/home/claude/.config/railway/`            | `railway` | ✓ via `npm install -g @railway/cli` |
| `npmrc`    | `/home/claude/.npmrc`                      | `npm` (auth tokens) | — `npm` is in the code image |
| `pypirc`   | `/home/claude/.pypirc`                     | `twine` / pip uploads | — install yourself: `uv tool install twine` |

**Auto-install:** for entries marked ✓, dropping the credentials dir on the
host also flips an `INSTALL_<TOOL>=1` build-arg, and `Dockerfile.code`
installs the CLI on the next `[code]` build. Each tool gets its own ARG, so
adding a new credential only invalidates that tool's layer (downstream
layers re-run as no-ops). Removing a credential reverses it on the next
build. Auto-install only happens for `[code]`-tagged agents; non-code
agents get the credentials passthrough but no CLI (those agents probably
don't need cloud tools anyway).

**Token files** (`<service>/token`): for services that authenticate via an
env-var token (currently `jira` → `$JIRA_API_TOKEN`), put the secret in a
plain-text file at `~/.claude-agents/user_extras/optional_creds/<service>/token`. The
launcher reads its trimmed contents at launch and forwards as the matching
env var (the CLI in the container picks it up the same way it does on your
host). The token stays in `os.environ` and is passed via compose's
`environment:` block — never on the `docker compose` command line, so it
doesn't appear in host-side `ps auxe`. Service→env-var mapping lives in
`OPTIONAL_CREDS_TOKEN_ENV_VARS` (`launch/paths.py`); to add a new tokened
service, add one entry there and one passthrough line to `docker/compose.yml`'s
`environment:` block.

To enable AWS in any `[code]` agent, for example:

```bash
mkdir -p ~/.claude-agents/user_extras/optional_creds
ln -s ~/.aws ~/.claude-agents/user_extras/optional_creds/aws    # symlink so host edits propagate
# or:  cp -r ~/.aws ~/.claude-agents/user_extras/optional_creds/aws
```

Next launch of any `[code]` agent: the code image rebuilds with `awscli`
installed, and the mounted creds make it ready to use.

## Project Layout

```
run.py                               # entry point + 7-stage launch() orchestrator (parse → resolve → resume? → persist → categorise → setup → run)
launch/
  paths.py                           # centralised path constants — host (AGENTS_STATE, USER_EXTRAS_DIR, OPTIONAL_CREDS_MOUNTS, OPTIONAL_CREDS_TOKEN_ENV_VARS, DEFAULTING_DIRS), container (CLAUDE_HOME_IN_CONTAINER, CLAUDE_CONFIG_IN_CONTAINER, SKILLS_IN_CONTAINER), per-layer bind-mount dicts (DOCKER_BASE_MOUNTS, DOCKER_AUTO_MOUNTS, DOCKER_DOOD_MOUNTS, CACHE_MOUNTS), path-builder lambdas. Import root: zero internal deps.
  utils.py                           # domain-neutral helpers — plural, relative_time, ordering_index_or_end, split_host_port. No disk access (that's file_access). Leaf module.
  file_access.py                     # every disk-touching call routes through here. Agent filename grammar (parse_stem) + .md/.conf lookup (find_md_for_agent, conf_path_for, load_conf), cached load/save of agent_workspace_map.json + agent_modes_map.json, ensure_shared_oauth_files (touches the two OAuth files as `{}` if absent), force_remove with sudo + `sudo -k` fallback, installed_cred_clis (space-joins CLIs with creds present).
  structs.py                         # identity dataclasses — AgentIdentity → InstanceIdentity → SessionIdentity (frozen=True, inheritance) + InstanceModifiers enum (BASE / TAG_CODE / MODE_AUTO / MODE_DOOD). SessionIdentity.chain returns the active-modifier-values tuple (BASE first, declaration order) and validates self.tags/self.modes against the taxonomy.
  compose_env.py                     # compose-side env-var staging — ComposeEnvKey enum, _compose_env accumulator + stage_compose_env, subprocess_env overlay, container_env_args (→ `-e KEY=VALUE` flags), conf_env_args, install_creds_flags, token_env_dict. set_container_env orchestrator (sister to docker_config's set_container_mounts).
  docker_config.py                   # docker subprocesses + bind-mount accumulator + image-chain naming. add_docker_mount, set_container_mounts, ensure_image, run_compose; docker CLI wrappers (require_docker, detect_docker_gid, wait_for_container_running, docker_exec_root, any_agent_container_running). Every direct `docker` call lives here.
  memory_addendums.py                # launch-time directives for CLAUDE.md. Addendum(NamedTuple) instances — SEEK_SUMMARY, MAINTAIN_PRIVACY, CREDENTIALS_NOTICE, FIREWALL_NOTICE — mapped per modifier via MODIFIER_ADDENDUMS. composed_addendum(chain) renders the active sub-sections under a single `## Launch-time addendums` heading.
  agent_composition.py               # compose_chain(sess_id) dispatch → _apply_code / _apply_auto / _apply_dood handlers; warn_if_dangerous_modes ({auto}+{DooD} red press-any-key warning); cache prepare/prune helpers ([code] only).
  network.py                         # {auto}-mode firewall coordination — BUILTIN_FIREWALL_DOMAINS (~135 entries), two-phase DNS resolution (sync Phase 1 → streaming Phase 2 via docker exec iptables -I), cross-launch resolved-IP cache (~/.claude-agents/resolved_domains.txt, 6h TTL), agent-visible status file (domains_pending_resolve.yml).
  agents_crud.py                     # instance-state CRUD — list_all_instances, update_workspace_map, set_instance_modes, install_latest_md (writes source `.md` + composed_addendum to state-dir CLAUDE.md in one go), modify_instance, delete_instance, resolve_pick, picker-entry builders (creatable_agents, continuable_instances), sort keys.
  user_additions.py                  # optional_creds_* (mounts, install env flags, token env vars) + plant_user_extras (auto-creates user_extras/optional_creds_readme.txt always; firewall_whitelist.txt only under {auto}). Bundled skills + commands ride along in DOCKER_BASE_MOUNTS; no per-skill code lives here.
  menu_picker.py                     # prompt_toolkit picker UI + LEGEND_TEXT (F8 composition legend) + ask_for_workspace + prompt_modes + prompt_session + print_launch_banner.
  claude_code_config.py              # Claude-Code-side UX — build_status_line(inst_id) + set_terminal_title(name). Leaf-shaped.
  audit.py                           # state-correctness checker (run as `python -m launch.audit`). Per-entry helpers (_check_json_file, _modes_map_issues) are unit-testable in isolation; main() handles orchestration.
  templates/                         # first-launch user-side files (firewall_whitelist.txt, optional_creds_readme.txt) planted into ~/.claude-agents/user_extras/ on first {auto} / first launch respectively.
  tests/                             # unittest suite — 14 files, 353 tests, ~0.1s. Run via `python3 -m unittest discover -s launch/tests` from the project root.
agents/                              # agent definitions — drop `<name>[tag](parent).md` (+ optional `.conf`) here
custom_commands/                     # launcher-bundled slash commands (mounted into every container) — `/refactor`, `/unspaghettify`, `/write-readme`, `/write-summary`
custom_skills/                       # launcher-bundled skills (mounted into every container) — currently `print/SKILL.md`
.claude/commands/                    # workspace-local slash commands for THIS project — `/update-models`. Auto-discovered by Claude Code natively when launched here; no mount.
settings/                            # status line + bashrc + Claude Code settings + keybindings + manifest helpers (mounted into every container)
tips/                                # reference notes (`project_claude_files.md`, `running_audit.md`, `chat_syntax.md`, …). Read by humans, not the launcher.
docker/
  Dockerfile, compose.yml            # base image + base compose (compose.yml uses `network: host` for builds to dodge BuildKit DNS issues)
  Dockerfile.code, compose.code.yml  # [code] tag layer (Rust + Node + uv); conditional CLI installs gated by INSTALL_<TOOL>=1 build-args
  Dockerfile.auto, compose.auto.yml  # {auto} mode layer (iptables + sudo + entrypoint wrapper)
  Dockerfile.dood, compose.dood.yml  # {DooD} mode layer (docker.sock bind-mount)
  init-firewall.sh                   # iptables outbound whitelist; container-side does no DNS — it sees pre-resolved IPs only
  auto-entrypoint.sh                 # runs init-firewall.sh, sudo -k, unsets WHITELIST_ADDRESSES, execs claude
```
