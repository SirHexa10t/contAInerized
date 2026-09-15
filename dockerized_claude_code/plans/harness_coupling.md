# Harness coupling — where the launcher assumes Claude Code

*Where this project would only work with Claude + Claude Code, and what another
agent harness (ChatGPT on Hermes, Gemini on OpenClaw, …) would have to supply
per seam. Written 2026-09-09 from a token sweep of the tree plus a read of each
seam; line numbers are of that day. It names CONTRACTS a replacement harness
must meet — it does NOT claim what Hermes or OpenClaw actually provide, which
was not verified against their documentation. Treat every "a harness must …"
below as the question to ask of one. The SOLUTIONS side — what a new AI must
provide, where that lands, how it is verified — is `adding_an_ai.md`; the
facet-by-facet verdict on what the harnesses share and what splits is
`harness_commonality.md`.*

## The shape in one paragraph

The launcher is a docker orchestrator around ONE agent CLI. That CLI is baked
into the base image and is the image's `ENTRYPOINT`; its per-user config dir is
the state dir the launcher assembles and bind-mounts (persona file, merged
settings, slash commands, credentials); its on-disk transcripts are read back
for picker previews, resumability and cowork attribution; its settings file
carries the permission policies and the hooks two features are built on; and
its CLI flags carry effort, resume, headless print and streaming. Everything
else — the tag tree, the picker and forms, the stores, docker build/run, the
multiplexers, the firewall MECHANISM, the cluster queue's file plane — is
harness-agnostic already (see §"Already agnostic" at the end).

Severity tiers used below:

| Tier | Meaning |
|---|---|
| **HARD** | a Claude Code protocol or on-disk FORMAT the launcher parses or emits (transcripts, settings/hooks, CLI flags, config-dir layout, stream events). Needs a real adapter. |
| **ENV** | names and vocabularies that differ per vendor but are plain data (env var names, model-id scheme, API hosts, credential files). A table per harness. |
| **COPY** | launcher-injected prose naming Claude Code features or tools (`Read`, `ListAgents`, `/write-summary`, "Stop hook"). Rewording. |
| **COSMETIC** | the word `claude` in user names, paths, image tags, module names, docs. Renaming, no design. |

## Seam by seam

### 1. The image and the entrypoint — HARD (small)

- `Dockerfile` (root): `curl -fsSL https://claude.ai/install.sh | bash`,
  `ENTRYPOINT ["claude"]`, and three Claude Code env switches baked as image
  defaults — `DISABLE_AUTOUPDATER`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`,
  `USE_BUILTIN_RIPGREP`. The container user is literally `claude`
  (`useradd … claude`, `/home/claude`) — cosmetic, but every path below hangs
  off it.
- `launch/docker_config.py` `run_container` (≈l.665-690): builds
  `agent_argv = ["claude"] + effort_args + resume_flag + inst.claude_args + claude_args`
  and, when no wrapper entrypoint is in play, passes `agent_argv[1:]` because
  "`claude` comes from the image ENTRYPOINT".
- `launch/cluster/launch_plan.py:52` `DEFAULT_MEMBER_COMMAND = ("claude",)`;
  `launch/cluster/launching.py` (≈l.255) builds each member's command as
  `("claude", *effort_args, *compute_resume_flag, *inst.claude_args)`;
  `launch/cluster/solo.py` bakes the same argv into the muxer startup script.
- `launch/cluster/herdr.py:123-124`: a pane whose command is `claude` is started
  with herdr's `agent start … --kind claude` — **herdr itself knows the agent
  kind** (that is how it tracks working/idle state). Another CLI needs a herdr
  "kind", or the generic `pane run` fallback that follows in the same function.
- `agents/specialty/firewall/firewall-entrypoint.sh` is already generic
  (`exec "$@"` hands off to whatever follows; the comment says why).

*A harness must supply:* an installable CLI, its binary name (one constant
would do — today the string `"claude"` appears in four places), and whichever
image-level env defaults replace the three switches.

### 2. CLI flags the launcher emits — HARD

| Flag | Emitted by | What it encodes |
|---|---|---|
| `--continue` | `launch/agents_crud.py` `compute_resume_flag` (≈l.185-203) | resume the last conversation; also the knowledge that `--continue` against history-only state CRASHES ("No conversation found"), and the 50 MB `RESUME_SIZE_WARN_BYTES` observed limit (plans/ISSUES.md) |
| `--effort <level>` | `launch/docker_config.py` `effort_args` (≈l.555-574) | pins the effort read from the engine conf's `CLAUDE_CODE_EFFORT_LEVEL` — "the env var alone doesn't pin fresh sessions" |
| `-p "<question>"` | `run_container(print_prompt=…)` | headless one-shot mode (quickie) |
| `--output-format stream-json --verbose --include-partial-messages` | `launch/quickie/ask.py:49` `STREAM_ARGS` | streaming answer for `docker_stream_subprocess` |
| `--dangerously-skip-permissions` | `agents/specialty/auto/tag.info:5` `claude_args` | `{auto}`'s permission bypass |
| passthrough | `run.py` `parse_known_args` → `LaunchOptions.claude_args` | anything the operator types after the target goes to the CLI |

The data-model field is named for the harness: `Specialty.claude_args`
(`launch/tags/specialty.py:48`) and the tag.info key `claude_args`.

*A harness must supply:* a flag (or nothing, and the feature degrades) for each
row: resume, effort/thinking budget, headless print, structured streaming,
permission bypass.

### 3. Config-dir layout and the mounts — HARD

`launch/paths.py` is the single place, which is the good news:

- `CLAUDE_CONFIG_IN_CONTAINER = /home/claude/.claude` (l.143) — Claude Code's
  per-user config root; the instance STATE DIR is bind-mounted here rw.
- `DOCKER_BASE_MOUNTS` (≈l.215-235): `.claude.json` → `~/.claude.json`,
  `.credentials.json` → `~/.claude/.credentials.json` ("Claude Code refreshes
  the token in place"), `statusline.sh`, `_summary.py`, `_dump_last_msg.py`,
  `keybindings.json` → `~/.claude/…`, `custom_skills/` → `~/.claude/skills`.
- `state_settings_path` (l.365): the launcher-merged `settings.json`,
  RO-mounted OVER `~/.claude/settings.json` so the agent cannot relax its
  policies — relies on Claude Code reading settings from exactly that file.
- `state_commands_dir` (≈l.368): the assembled slash-command dir, RO-mounted
  whole at `~/.claude/commands` (slash-command discovery = files in that dir,
  markdown with a `description:`/`argument-hint:` frontmatter —
  `custom_commands/*.md`, `agents/_commands/*.md`; skills = `<dir>/SKILL.md`).
- `state_history_path` (l.378): `history.jsonl` — the "last used" signal.
- `state_workspace_jsonls` (l.461): `projects/-workspace/*.jsonl` — Claude
  Code's cwd-to-directory encoding (`/workspace` → `-workspace`); the source of
  every transcript read (§6).
- `state_md_path`: `CLAUDE.md` — the persona file (§4).
- `launch/cluster/launching.py`: per member `CLAUDE_CONFIG_DIR=/cluster/members/<id>`
  (≈l.249-254), the member's `sessions/` symlinked to a shared dir (l.78,
  ≈l.217-222 — cross-session discovery is per config dir), and `_SHARED_LINKS =
  ("skills", "keybindings.json")` (l.79) re-linked because a member's config
  dir would otherwise hide the shared mounts.
- `run.py:236-237` (comment): per-workspace skills are not mounted because
  "Claude Code auto-discovers those from the workspace's `.claude/skills/`".
- `settings/bashrc.sh` lists `~/.claude/commands` and `~/.claude/skills` in its
  help text and names Claude Code's built-in slash commands.

Launcher-owned and agnostic: `/workspace`, `/workspaces/<id>`, `/cluster`,
`/cowork`, `.claude_summary` (the name is ours), `~/.ai-agents` on the host.

*A harness must supply:* its config root and the env var that relocates it (for
clusters), a settings file it reads at that root (or another way to impose
permissions), where it discovers commands/skills (or accept losing them), where
and how it writes transcripts and a per-launch history marker.

### 4. The persona file and the injected copy — HARD (mechanism) + COPY

- `agents/<name>.md` is installed as `<state>/CLAUDE.md`
  (`launch/agents_crud.py` `install_latest_md`, ≈l.166-174) with
  `launch/tags/addendums.py` `compose()` appended — the whole instruction
  channel assumes the harness reads a `CLAUDE.md` from its config root.
- The launcher-universal addendums (`addendums.py` `SEEK_SUMMARY`,
  `MAINTAIN_PRIVACY`) name Claude Code tools (`Read`) and a bundled command
  (`/write-summary`).
- Tag addendums in `tag.info` files name Claude Code features:
  `agents/specialty/muxer/cluster/tag.info` (`ListAgents`, `SendMessage` — the
  cross-session tools, l.37/43); `agents/specialty/cowork/tag.info` (Stop hook,
  subagents, permission modes, `WebFetch`/`WebSearch`);
  `agents/specialty/muxer/cluster/cluster-cowork/tag.info` (`UserPromptSubmit`,
  `CLAUDE.md`); `agents/specialty/cowork/manager/tag.info` (`/cowork`).
- The personas and bundled commands use Claude Code's tool vocabulary
  (`Read` ×14, `Write` ×9, `Agent`, `AskUserQuestion`, `Skill`, `Grep`, `Glob`,
  `Bash` across `agents/*.md`, `agents/_commands/*.md`, `custom_commands/*.md`,
  `custom_skills/*/SKILL.md`); 10 of those files mention Claude Code or
  `CLAUDE.md` by name.

*A harness must supply:* a system-instructions file (or flag) the launcher can
write per instance; then the copy is a mechanical reword of tool names.

### 5. Settings, permissions and hooks — HARD (the biggest one)

- `settings/settings.json` (base): `statusLine` (runs `statusline.sh`) and
  `respondToBashCommands` — Claude Code settings keys.
- Every `agents/policy/*/policy.json` is a Claude Code **settings fragment**:
  the permission-rule grammar `Bash(git push:*)`, `Bash(sudo *)`, tool names
  (`Write`, `Edit`, `NotebookEdit`, `WebFetch`, `WebSearch`, `Read`, `Glob`,
  `Grep`, `Bash`), `permissions.defaultMode` (`dontAsk` in `_cowork`, `plan`
  in `plan-first`). The **policy tag KIND itself** is "a Claude Code settings
  fragment" — `launch/tags/policy.py` `merge_fragments` is a generic JSON merge
  (reusable), but every shipped row is harness-specific data.
- `launch/agents_crud.py` `install_settings` (≈l.143-163): base + always-on
  policies + the instance's policies + specialty-claimed fragments →
  `<state>/settings.json`, mounted RO over the config root's settings file.
- **Hooks** — two features hang on them:
  - `agents/policy/_cowork/policy.json`: a `Stop` hook piping the hook's
    stdin JSON into `/cowork/outbox/<ts>-<pid>.json`. The capture's fields
    are read by `launch/cowork/mailbox.py` (`last_assistant_message`,
    `prompt_id`, `session_id`, `transcript_path`, ≈l.146-167) and
    `transcript_path` is translated from the CONTAINER path (`host_transcript_path`).
  - `agents/policy/_cluster-cowork/policy.json`: a `UserPromptSubmit` hook
    running `cluster-chat brief` before every prompt — and the knowledge that a
    non-zero exit BLOCKS the prompt (the brief always exits 0).
- Verified behaviours the design leans on (plans/ISSUES.md, "cowork permission
  model"): `dontAsk` + an allowlist is a floor not a ceiling; deny beats
  `--dangerously-skip-permissions`; subagents inherit the mode. Another
  harness's permission model will differ in exactly these places.

*A harness must supply:* a per-tool allow/deny mechanism the launcher can
impose read-only from outside; a turn-end hook that receives the assistant's
last message plus an id joinable to the transcript; a pre-prompt hook; a
"never prompt the human" mode for headless coworkers. Without hooks, cowork's
capture path and the cluster brief have no equivalent.

### 6. The transcript format — HARD

`launch/transcripts.py` is the single reader (good) and encodes Claude Code's
JSONL: one JSON object per line, `type` in {`user`, `assistant`},
`message.content` as a string or a list of `{type: "text", text}` blocks,
`isSidechain` (subagent traffic), `timestamp` (ISO, `Z`), and — used by cowork —
`promptId`. Consumers:

- picker previews' `Last prompt` (`launch/gui/picker_previews.py`
  `_read_last_prompt`, in a child process) and `Last used`
  (`history.jsonl` mtime);
- `compute_resume_flag`'s "is there anything to continue" probe
  (`has_continuable_jsonl`) and size warning;
- quickie `--history` / `--answer` (`launch/quickie/history.py`);
- cowork attribution (`launch/cowork/mailbox.py` `prompt_text`: the turn with
  a given `promptId`, sidechains excluded);
- in-container: `settings/_dump_last_msg.py` (newest `~/.claude/projects/*/*.jsonl`,
  last non-sidechain `assistant` entry).

*A harness must supply:* where its transcripts live and their schema — or these
features degrade: no `Last prompt`, no resumability probe, no cowork attribution.

### 7. Streaming output (quickie) — HARD

`launch/quickie/render.py` consumes Claude Code's `stream-json` events:
`type: "stream_event"` wrapping Anthropic Messages-API deltas
(`content_block_start` with `content_block.type == "thinking"`,
`content_block_delta` with `delta.type == "text_delta"`), and a terminal
`type: "result"` with a `subtype`. Plus the knowledge that thinking TEXT is
redacted in headless mode (only elapsed time is shown). `docker_config.docker_stream_subprocess`
is the agnostic pipe.

*A harness must supply:* a headless mode with a line-oriented event stream (or
plain stdout, dropping the thinking ticker).

### 8. Model ids and the engine budget — ENV

- `agents/engine/*/tag.budget` (since 2026-09-13; before it, one `<ai>.conf` per AI
  and, until 2026-09-10, `engine.conf`): the budget in the launcher's OWN words
  — a capability standard, switches, amounts — so this seam is now DATA in
  `agents/ai/<key>/`: `efforts.tiers` (the standard → that AI's model + effort) and
  `knobs.mapping` — in the HARNESS's dir since 2026-09-14 — (each purpose → that CLI's native settings, `{value}`
  templates with the unit conversions). `Ai.render(budget)` is the adapter
  boundary; `Instance.conf` is the rendering for the instance's AI. The
  FORWARDING is still Claude-shaped: `docker_config.conf_env_args` emits
  `-e KEY=VALUE` for whatever the rendering says, which suits Claude Code's env
  vars; Gemini's settings.json, Codex's and Grok's config.toml need a writer.
- `launch/tags/engine.py`: `ORDERED_MODEL_FAMILIES = [fable, mythos, opus,
  sonnet, haiku]` (l.41), `parse_model_id` on `claude-<family>-<major>-<minor>`
  (l.52-60), `DEFAULT_MAX_OUTPUT_TOKENS = 32_000` (Claude Code's default, l.49).
  These drive the picker's capability sort (`agents_crud._agent_sort_key`,
  `menu_picker` `cont_sort_key`, the tag form's engine radio order).
- `docker_config.effort_args` reads `CLAUDE_CODE_EFFORT_LEVEL` by name.
- `agents/_commands/ai_project-update-models.md`: the bundled model-bump
  command reads Anthropic's model docs.

*A harness must supply:* its env var names for model/effort/budget, a
model-family ranking for its vendor (`gpt-*`, `gemini-*`), and what "effort"
means, if anything.

### 9. Authentication — ENV

- `launch/paths.py:53-54` `ACCOUNT_FILE = ~/.ai-agents/.claude.json`,
  `CREDENTIALS_FILE = ~/.ai-agents/.credentials.json`, mounted where Claude
  Code expects them (§3); `run.py` `setup_state` → `ensure_shared_oauth_files`
  so docker never creates them root-owned; `launch/audit.py` checks both.
- `launch/claude_code_config.py` reads `oauthAccount.emailAddress` out of the
  account file for the status line (l.99, l.127).
- `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` appear only as documented
  alternatives (engine conf reference, `container_env`).
- `launch/user_additions.py` optional creds (gh, gcloud, aws, …) are agnostic.

*A harness must supply:* its credential files or env (`OPENAI_API_KEY`,
`GOOGLE_API_KEY`, a login store) and, if the status line is kept, where the
account identity can be read.

### 10. Status line, terminal title, key bindings — HARD (small) + COPY

- `launch/claude_code_config.py`: `build_status_line` / `build_cluster_status_line`
  produce `AGENT_STATUS_LINE` (staged by `container_env.ContainerEnvKey`),
  which `settings/statusline.sh` prints when Claude Code runs it via the
  `statusLine` setting ("harness pipes JSON in, we ignore it");
  `set_terminal_title` emits `Claude Code — <name>` (l.140).
- `settings/keybindings.json`: Claude Code's keybinding schema
  (`$schema: …/claude-code-keybindings.json`, `chat:newline`).

*A harness must supply:* a status-line hook (or the line is lost), and its own
key-config format (or none).

### 11. Cowork (cross-container groups) — HARD

Beyond §5's Stop hook and §6's `promptId` join, cowork relies on:

- **prompt injection into a live TUI**: `launch/container_inject.py`
  `docker_attach_inject` attaches a pty, types the text, waits
  `INJECT_ENTER_DELAY = 0.4` s, sends `ENTER_KEY = "\r"` ("`\n` is swallowed by
  the input widget"), strips ANSI, and matches the container's winsize — all
  tuned to Claude Code's input widget. plans/ISSUES.md ("Socket delivery
  could replace pty injection") records why a socket wasn't adopted: the wire
  format is undocumented.
- `permissions.defaultMode = "dontAsk"` semantics for unattended coworkers (§5).
- the `/cowork` slash command (`agents/_commands/cowork.md`) and the
  `/cowork/control/` verb files (`launch/cowork/control.py`).
- `Instance.is_cowork` / `is_manager` (`launch/tags/identity.py`) — by tag
  name, harness-agnostic.

The file plane (`sync.py`, `journal.py`), roster and lifecycle are agnostic.

*A harness must supply:* a way to hand a running session a prompt (an API or
socket beats typing), a turn-end capture, and a headless permission mode.

### 12. Cluster (cohabiting members) — HARD

- `launch/cluster/launching.py`: `CLAUDE_CONFIG_DIR` per member;
  `CLAUDE_CODE_SESSION_NAME=<member id>` (≈l.255) — what makes members
  addressable by id in `ListAgents`/`SendMessage`; the shared `sessions/`
  symlink (§3); and `MESSAGING_KILL_SWITCH = "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"`
  (l.73) **unset in the generated entrypoint** — Claude Code's cross-session
  messaging is gated behind its telemetry switch (plans/cluster_plan.md,
  research spike).
- `agents/specialty/muxer/cluster/tag.info` addendum: instructs members to use
  `ListAgents` and `SendMessage` (Claude Code's own cross-session tools) for
  1:1 asks; the queue (`cluster-chat`) is the harness-agnostic half.
- `launch/cluster_work_protocol/wake.py` `inject`: wakes a member by typing
  `[cluster-chat] …` + Enter into its pane (herdr `pane run` / tmux
  `send-keys … Enter`) — assumes a TUI that treats typed text as a prompt.
- `agents/policy/_cluster-cowork/policy.json` `UserPromptSubmit` hook (§5).
- `launch/cluster/herdr.py` `--kind claude` (§1);
  `claude_code_config.build_cluster_status_line` (§10).

The queue itself (`cluster_work_protocol/queue.py`, `gates.py`, `schema.py`,
`config.py`), `state.py`, `legoset.py`, `worktree.py`, `panes.py` and both
backends' assembly are agnostic.

*A harness must supply:* a config-dir env var, a session-name env var (or
another way to name a session), optionally its own multi-session messaging
(else the queue alone), and a prompt-injection path for wakes.

### 13. Firewall host list — ENV

- `launch/template_code/firewall_domains.py` "Anthropic" section:
  `api.anthropic.com`, `console.anthropic.com`, `www.claude.ai` (the rest of
  the list — GitHub, npm, PyPI, … — is toolchain, not vendor).
- `launch/firewall/resolver.py`: `_CRITICAL_HOSTS = ("api.anthropic.com",
  "console.anthropic.com")` (l.409) resolved synchronously before `docker run`;
  `_ANTHROPIC_BLOCKS = ("160.79.104.0/21",)` (l.419) — the critical pins are
  widened to Anthropic's OWN registered range; `selftest_address()` → the
  `FIREWALL_SELFTEST_ADDR` env → `init-firewall.sh`'s DNS-free `curl --resolve`
  probe; the abort message "Claude Code cannot operate without them" (l.593).

*A harness must supply:* its API hosts as the critical set and, ideally, the
vendor's registered ranges or CDN provider for the widening; a reachable
self-test target.

### 14. Names — COSMETIC (numerous)

The container user `claude` and `/home/claude`; image tags `claude-agents:*`;
`container_probe.CONTAINER_NAME_PREFIX = "claude-code_"`; the host state root
`~/.ai-agents` (`paths.AGENTS_STATE`) and `/var/log/claude-agents` in the
`[code]` image; `.claude_summary` / `.claude_dev_guidelines`; the module
`launch/claude_code_config.py`; `Specialty.claude_args` / `LaunchOptions.claude_args`;
`ContainerEnvKey.CLAUDE_AGENT_INSTANCE`; `run.py`'s parser description; README,
`tips/`, `custom_commands/write-summary.md`. None of it is a design problem;
all of it is a large rename — and 31 of the 55 test modules pin one or more of
these strings (`--continue`, `ANTHROPIC_MODEL`, `promptId`, `stream-json`, …).

Done 2026-09-10, the words a USER reads: the terminal title and the firewall
abort message read `DEFAULT_AI.cli_name` / `.vendor` from the catalog, and the
engine descriptions name tiers, not models — the picker renders each engine's
pinned model beside them through `tags/engine.pinned_model`, the one lookup of
the model key (`adding_an_ai.md`, status rows §8 and §10 / §14).

## Already agnostic (the boundary, so nothing is over-counted)

Docker orchestration (`ensure_image`, the build chain, mounts, `--rm --name`,
the dry-run gate); `container_probe`/`container_inject`'s pty plumbing (minus
the Enter tuning); the tag tree, registry, scanning and validation; `.lego` /
`instances.toml` / `cluster.toml` stores and `toml_emit`; the whole `gui/`
package (only preview COPY names Claude Code); `label_error`/`suggested_label`;
`worktree.py`, `tmux.py`, `herdr.py` (minus `--kind claude`), `panes.py`,
`launch_plan.py`; the firewall MECHANISM (resolver, cdn_ranges, iptables,
whitelist, status); the cluster queue and gates; cowork's file plane, roster,
lifecycle, control verbs; `user_additions`; `audit.py` (minus the OAuth check
and the CLAUDE.md-shaped expectations); `utils`, `paths` builders, `file_access`.

## What would have to change — three shapes

**A. A harness adapter (recommended).** One `launch/harness/` package with a
small protocol and one module per harness (`claude_code.py` first, then others):
the binary name and image install snippet (§1); the flag map (§2); the config
root, its relocation env var, settings filename, commands/skills locations,
transcript and history locations (§3); the persona filename (§4); how to render
a policy set for that harness (§5); the transcript parser (§6); the stream
event parser (§7); model-family ranking and the env keys for model/effort/
budget (§8); credential files and the account-identity lookup (§9); the
status-line hook (§10); the turn-end and pre-prompt hooks, and the prompt
injection channel (§11-12); the critical hosts and widening blocks (§13).
The natural selector is the **engine**: an engine already is "model + budget",
and every key in an engine's budget file is harness-bound today — so the
budget vocabulary (now `tag.budget` + the AI's `efforts.tiers` + the harness's `knobs.mapping`,
2026-09-13) already IS the per-harness data, and the harness's CLI would become an image
layer selected by it, the way professions are layers. Policies become
per-harness DATA rows (`policy.<harness>.json` beside today's `policy.json`),
which keeps the tree's "a tag is folders and files, no launcher code" rule.
Features a harness cannot back degrade explicitly, per a capability matrix:
no hooks → no cowork capture, no cluster brief; no transcripts → no `Last
prompt`, no resumability probe; no messaging → the queue alone; no headless
stream → plain stdout for quickie.

**B. A fork per harness.** Copy the launcher and edit the seams in place.
Fastest to a first working ChatGPT/Gemini container; every later fix lands
twice, and §14's rename alone touches most files.

**C. Shrink to the agnostic core.** Keep the tree, picker, stores, docker and
multiplexers; drop everything in §5-§7 and §11-§12 that the other harness has
no equivalent for. Honest about capability, but gives up the two features that
make this launcher more than `docker run`.

A is the only one whose cost is paid once. Its first step costs nothing
functionally and is worth doing regardless — DONE 2026-09-12 as
`launch/ai/adapter.py` (`harness.py` until 2026-09-14) + `launch/ai/claude_code.py` with the call-time
`active_harness_key()` / `active_adapter()` (`active_ai()` / `active_harness()` at first) (`adding_an_ai.md`, order of work step 1): gather the string `"claude"`, the
flag names, the config-dir constants and the env-key names now scattered across
`docker_config`, `agents_crud`, `launch_plan`, `launching`, `herdr`, `paths`
and `engine.py` into ONE module (the `claude_code.py` adapter), with the rest
of the tree importing from it. That module's public surface IS the list of
questions to take to the Hermes and OpenClaw documentation.

## Open questions this file cannot answer

For each candidate harness, unverified here: does it read a per-user settings
file the launcher can overlay read-only; does it expose turn-end / pre-prompt
hooks with a JSON payload; does it have a headless print mode with a line
stream; does it write transcripts to disk and offer a resume flag; does it name
sessions and message between them; what is its permission model; does herdr
know it as an agent kind. The answers decide which rows of the capability
matrix a port keeps.
