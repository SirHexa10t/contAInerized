# Harness commonality — what stays one thing, what splits per AI

*Design input for the adapter step (`adding_an_ai.md`, "Order of work" step 1).
For every facet of running an agent CLI in a container, this file says whether
the four harnesses share a STRUCTURE — so the launcher keeps ONE mechanism and
the difference is a row of data — or differ in structure, so the handling
splits into per-harness code, or one harness lacks the thing and the feature
degrades explicitly. Columns: Claude Code (verified this week from its docs and
from running it), Gemini CLI and Codex CLI (verified from their docs, schemas
and per-model pages — `agents/engine/default/{gemini,chatgpt}.conf` carry the
sources), and Grok Build — xAI's official, open-source coding agent (`docs.x.ai/build`,
`github.com/xai-org/grok-build`; the docs brand the company "SpaceXAI (xAI)"),
researched 2026-09-12 by a second reader and re-checked on the pages by the
author (the docs serve every page as raw markdown by appending `.md`); the
community `grok-cli` wrappers on npm are not it. The operator's working
assumption is that what holds for the three verified columns holds for
ChatGPT's harness as well — Codex IS that harness, so the column is evidence,
not assumption. Started 2026-09-12.*

Verdicts:

- **COMMON** — same structure everywhere; the launcher keeps one mechanism and
  the per-AI difference is DATA: a member dir under `agents/ai/<key>/`
  (`tag.info`, `efforts.tiers`; `knobs.mapping` moved to `agents/harness/<key>/` on 2026-09-14), a harness record
  (`launch/ai/<key>.py`), a filename.
- **SPLIT** — the structure differs; the adapter owns one method per harness,
  tested per harness.
- **DEGRADE** — a harness lacks it; the capability matrix switches the feature
  off for that AI, explicitly and visibly, never silently.
- **PROBE** — settled only by a run against the real binary
  (`adding_an_ai.md`, "Probes once a binary is in an image").

## A. Getting the harness into a container (§1)

| Facet | Claude Code | Gemini CLI | Codex CLI | Grok | Verdict |
|---|---|---|---|---|---|
| Distribution | npm `@anthropic-ai/claude-code` | npm `@google/gemini-cli` | npm `@openai/codex` | npm `@xai-official/grok` (or `curl … x.ai/cli/install.sh \| bash`; the enterprise page says npm avoids needing the `x.ai` host) | COMMON — one npm image layer; package and binary names are data |
| Binary | `claude` | `gemini` | `codex` | `grok` | COMMON (data) |
| Container user, paths, image tags | `claude`, `/home/claude`, `claude-agents:*`, `~/.ai-agents` | — | — | — | COMMON mechanism; the names are §14's cosmetic rename, last |

## B. Invoking it (§2, §7)

| Facet | Claude Code | Gemini CLI | Codex CLI | Grok | Verdict |
|---|---|---|---|---|---|
| Headless / print mode | `claude -p` | `gemini -p` (`-i` seeds and stays interactive) | `codex exec` (`codex e`) | `grok -p` | COMMON concept; the flag map is data |
| Line-delimited streaming | `--output-format stream-json` | `-o stream-json` (also `text`, `json`) | `--json` (newline-delimited events) | `--output-format streaming-json` — newline-delimited JSON, but the events are ACP (`session/update`, `agent_message_chunk`), not stream-json | COMMON transport (one JSON object per line); SPLIT event schema — a parser per harness, and Grok's is the ACP shape |
| Resume | `--continue`, `--resume` | `--resume [index\|UUID]`, `--list-sessions`, `/resume` save/list/resume | `codex resume` (`--last`, `--all`), `codex exec resume` | `--resume <id>` / bare `--resume` (most recent here), `-c` continue, `--fork-session`; `-s / --session-id` names a NEW session | COMMON concept; SPLIT flags and session-id semantics |
| Model on the command line | `--model` | `-m` / `--model` | `-m` / `--model` | `-m` / `--model` | COMMON |
| Exit codes | conventional | 0 / 1 / 42 (input) / 53 (turn limit) | not researched | not researched | SPLIT (data) |
| Non-interactive trap | — | — | `CODEX_NON_INTERACTIVE` is an INSTALLER variable, not a headless switch | pass `--no-auto-update` in scripts (the docs' own advice for `-p` and ACP) | note for the adapter |

## C. Where it keeps state (§3, §6)

| Facet | Claude Code | Gemini CLI | Codex CLI | Grok | Verdict |
|---|---|---|---|---|---|
| Config root + relocation variable | `~/.claude`, `CLAUDE_CONFIG_DIR` | `~/.gemini`, `GEMINI_CLI_HOME` | `~/.codex`, `CODEX_HOME` (+ `CODEX_SQLITE_HOME`) | `~/.grok`, `GROK_HOME` ("config, auth, sessions, skills, plugins, and logs") | COMMON — one root, one variable: a catalog row |
| Settings file | `settings.json` (user, project `.claude/settings.json`, managed) | `settings.json` (system-defaults < user < project `.gemini/settings.json` < system < env < flags); string values interpolate `$VAR` / `${VAR:-default}` | `config.toml` (flags / `-c key=value` > project `.codex/config.toml` > profile > user > managed > `/etc/codex` > built-ins) | `config.toml` (TOML); managed `managed_config.toml` under the root and `/etc/grok/`; `requirements.toml` pins, fail-closed; `GROK_CONFIG` (inline JSON) / `GROK_CONFIG_PATH` overlays. Project `.grok/config.toml` contributes ONLY `[mcp_servers]`, `[plugins]`, `[permission]` — no per-project model or effort | SPLIT — format (JSON vs TOML) and keys differ; the adapter renders. The project-overlay PATTERN is COMMON, but Grok's project scope is restricted: model and effort are user-config only |
| Transcripts on disk | `projects/<slug>/<uuid>.jsonl` + `history.jsonl` | `tmp/<project-hash>/chats/`, checkpoints beside | `sessions/YYYY/MM/DD/rollout-<ISO8601>-<uuid>.jsonl` (+ SQLite state) | `~/.grok/sessions/`, grouped by working directory (URL-encoded; slug + hash over 255 bytes, real path in a `.cwd` file); stores prompts, responses, tool calls, file snapshots; `grok sessions list \| search \| delete`; `grok export <id>` writes a Markdown transcript. FILE FORMAT NOT FOUND | SPLIT — a reader per harness for last prompt, last-used time, resumability; content never converts (`adding_an_ai.md`, transfer §1). Grok's export is the one documented transcript-out path — raw material for the summary handoff |
| Retention | `cleanupPeriodDays` | `sessionRetention.enabled / maxAge / maxCount` (30 days) | `history.persistence`, `history.max_bytes` | NOT FOUND | SPLIT (data) |

## D. Telling it who it is (§4)

| Facet | Claude Code | Gemini CLI | Codex CLI | Grok | Verdict |
|---|---|---|---|---|---|
| Persona file | `CLAUDE.md` | `GEMINI.md` (renameable: `context.fileName`) | `AGENTS.md`; `AGENTS.override.md` wins | reads `AGENTS.md`, `Agents.md`, `AGENT.md`, `CLAUDE.md`, `Claude.md`, `CLAUDE.local.md` and every `*.md` under `.grok/rules/`, `.claude/rules/`, `.cursor/rules/` — our CLAUDE.md as-is | COMMON mechanism — the launcher composes one file and installs it under the harness's name; the name is data. Bridge: an `@AGENTS.md` line or a symlink |
| Discovery | global → project root → cwd → subdirs on demand | global → project root → cwd → subdirs (`discoveryMaxDirs` 200) | global (override, then plain, first non-empty) → git root → cwd | global `~/.grok/` first, then repo root down to cwd, deeper wins; gitignored files skipped | COMMON shape |
| Imports | `@path`, 4 hops | `context.includeDirectories` | none (`developer_instructions` appends) | none needed — every listed file is read whole | SPLIT — the launcher must INLINE its addendums, never rely on imports |
| Size cap | skips over 4 MiB; target under 200 lines | NOT FOUND | `project_doc_max_bytes` 32 KiB per level | none: "Files are loaded in full, with no size cap" | SPLIT (data) — the composed persona must fit the smallest cap |
| System prompt replace / append | `--append-system-prompt` | `GEMINI_SYSTEM_MD` (replaces) | `model_instructions_file` (replaces), `developer_instructions` (appends) | `--system-prompt-override` (replaces), `--rules` (appends) | SPLIT |

## E. What it may do (§5)

| Facet | Claude Code | Gemini CLI | Codex CLI | Grok | Verdict |
|---|---|---|---|---|---|
| Permission model | `permissions.allow / ask / deny`, argument patterns (`Bash(git *)`), `defaultMode` | `tools.core / allowed / confirmationRequired / exclude` (tool NAMES), `general.defaultApprovalMode` default / auto_edit / plan | `approval_policy` on-request / never / granular, `sandbox_mode` read-only / workspace-write / danger-full-access, `default_permissions`, `permissions.<name>` profiles, execpolicy rules | `[permission]` `allow` / `deny` / `ask` rules with ARGUMENT PATTERNS (`{ action = "allow", tool = "bash", pattern = "git *" }`), tool filters Bash / Edit / Read / Grep / MCPTool / WebFetch / WebSearch; "deny always wins over allow"; modes Normal → Plan → Auto → Always-approve; rules in user config only | SPLIT — policies become per-harness DATA files (`policy.<ai>.json` beside `policy.json`); DEGRADE where a set is unexpressible (Gemini: read-only lossy, argument-pattern denial probably impossible → hook). Grok's model is the closest to Claude's: patterns, so no-git is expressible; `read-only` is a named sandbox profile |
| Bypass everything | `bypassPermissions` | `--yolo` | `--dangerously-bypass-approvals-and-sandbox` | Always-approve mode | COMMON concept; SPLIT spelling |
| Network switch | none — the launcher's firewall | `tools.sandboxNetworkAccess` | `sandbox_workspace_write.network_access` | sandbox profiles `read-only` / `strict` block child network (Linux only), or `restrict_network` in a custom profile | The launcher's container firewall stays COMMON; the harness's own switch is extra data |
| Harness sandbox | `sandbox.enabled` | `tools.sandbox` docker / podman / cmd | `sandbox_mode` | `--sandbox` / `[sandbox] profile` / `GROK_SANDBOX`: off (default), workspace, devbox, read-only, strict; custom profiles in `~/.grok/sandbox.toml` | COMMON decision: the container IS the sandbox — run each harness's own sandbox OFF (Codex `danger-full-access` inside it, or map `{ro}`) |
| Shell env filtering | — | — | `shell_environment_policy` filters names matching KEY / SECRET / TOKEN by default | not researched | note — the adapter must allow the launcher's own `-e` names through |

## F. Hooks — the seam cowork and the cluster stand on (§5, §11, §12)

| Facet | Claude Code | Gemini CLI | Codex CLI | Grok | Verdict |
|---|---|---|---|---|---|
| Pre-prompt | `UserPromptSubmit` | `BeforeAgent` | `UserPromptSubmit` | `UserPromptSubmit` — same name, but NON-BLOCKING (see the contract row) | COMMON concept; the names are a data map |
| Turn end | `Stop` | `AfterAgent` | `Stop` (+ `notify` command on turn complete) | `Stop` (+ `StopFailure`) | COMMON concept; data map |
| Tool before / after | `PreToolUse` / `PostToolUse` | `BeforeTool` / `AfterTool` | `PreToolUse` / `PostToolUse` | `PreToolUse` / `PostToolUse` (+ `PostToolUseFailure`, `PermissionDenied`) | COMMON concept; data map |
| Compaction, session | `PreCompact`; `SessionStart` / `SessionEnd` | `PreCompress`; `SessionStart` / `SessionEnd` | `PreCompact` / `PostCompact`; `SessionStart` / `SessionEnd` | `PreCompact` / `PostCompact`; `SessionStart` / `SessionEnd` | COMMON concept; data map |
| Extras | `SubagentStop`, `Notification` … | `BeforeModel`, `AfterModel`, `BeforeToolSelection`, `Notification` | `PermissionRequest`, `SubagentStart` / `SubagentStop`, `Interrupt` | `Notification`, `SubagentStart` / `SubagentStop` | per-harness; unused by the launcher |
| Payload and blocking contract | `prompt_id`, `session_id`, `transcript_path`; non-zero exit blocks | NOT FOUND | NOT FOUND | stdin JSON `hookEventName`, `sessionId`, `cwd`, `workspaceRoot` (+ `toolName`, `toolInput`); env `GROK_HOOK_EVENT`, `GROK_HOOK_NAME`, `GROK_SESSION_ID`, `GROK_WORKSPACE_ROOT`. "`PreToolUse` — the only blocking event": exit 0 allows, exit 2 denies, everything else fail-open; NO `transcript_path` | PROBE for Gemini and Codex; for Grok the contract is DOCUMENTED and BREAKS: a `UserPromptSubmit` hook cannot block a prompt, so the cluster brief degrades to advisory, and cowork's capture must derive the transcript from `sessionId` + `~/.grok/sessions/` |
| Where hooks are declared | `settings.json` `hooks` | `settings.json` `hooks.<Event>` | `config.toml` `hooks.<Event>[]` or `hooks.json`; `features.hooks` | JSON files `~/.grok/hooks/*.json`, `<project>/.grok/hooks/*.json`; ALSO reads `.claude/settings.json` and `.cursor/hooks.json` hooks; project hooks need trust (`/hooks-trust`, `--trust`) | SPLIT format (the adapter renders) |

## G. Model, effort and budget (§8)

| Facet | Claude Code | Gemini CLI | Codex CLI | Grok | Verdict |
|---|---|---|---|---|---|
| Budget | `agents/ai/claude/efforts.tiers` + `agents/harness/claude-code/knobs.mapping` | `agents/ai/gemini/…` + `agents/harness/gemini-cli/…` | `agents/ai/chatgpt/…` + `agents/harness/codex-cli/…` | `agents/ai/grok/…` + `agents/harness/grok-build/…` | COMMON — done 2026-09-13: engines state a budget in the launcher's words (`tag.budget`); each AI's two files translate it |
| Model key | `ANTHROPIC_MODEL` (env) | `GEMINI_MODEL` (env), `model.name` (settings) | `model` (toml) | `GROK_DEFAULT_MODEL` (env) / `[models] default` (toml); per-model `[model.<id>]` tables | COMMON — done: `Ai.model_key`, read through `tags/engine.pinned_model` |
| Effort | `CLAUDE_CODE_EFFORT_LEVEL` low / medium / high / max, plus thinking on/off | `thinkingLevel` MINIMAL / LOW / MEDIUM / HIGH (Gemini 3), `thinkingBudget` tokens (Gemini 2.5) | `model_reasoning_effort` minimal / low / medium / high / xhigh (`max`: PROBE) | `[models] default_reasoning_effort` / `[model.<id>] reasoning_effort`, gated by `supports_reasoning_effort`; AA rates Grok 4.6 at low / medium / high / xhigh | COMMON vocabulary (Artificial Analysis's effort levels, `launch/ai/equivalence.EFFORT_LEVELS`); SPLIT key, casing, ceilings, and the 2.5 budget exception |
| Thinking visibility | `CLAUDE_CODE_ENABLE_THINKING` | `ui.inlineThinkingMode` off / full | `model_reasoning_summary` auto / concise / detailed / none | `GROK_SHOW_THINKING_BLOCKS` / `[ui] show_thinking_blocks` (default true) | COMMON concept; SPLIT keys |
| Output cap | `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | `generateContentConfig.maxOutputTokens` | none in the config surface (the models cap at 128,000) | `max_completion_tokens` (`[models]` or per model) | DEGRADE on Codex |
| Compaction trigger | percent (`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`) | fraction (`model.compressionThreshold`) | absolute tokens (`model_auto_compact_token_limit` + `_scope`) | PERCENT: `[session] auto_compact_threshold_percent` 0–100, default 85 — the only true percent analogue of Claude's | SPLIT units — convert at the adapter boundary (Grok needs none) |
| Tool-output caps | tokens, per kind (`MAX_MCP_OUTPUT_TOKENS`, `…FILE_READ…`) | characters, every tool (`tools.truncateToolOutputThreshold`) + distillation tokens | tokens per stored output (`tool_output_token_limit`) | not researched | SPLIT units (≈ 4 characters per token, Google's figure) |
| Subagent knobs | `CLAUDE_CODE_SUBAGENT_MODEL`, `…MAX_CONCURRENT_SUBAGENTS` | `experimental.enableAgents`; per-scope overrides | `agents.enabled`, `agents.default_subagent_model`, `agents.max_concurrent_threads_per_session` | `[subagents] enabled / toggle / models`, `GROK_SUBAGENTS` | partial — COMMON on/off, SPLIT the rest |
| Rendering the budget into the harness | `-e KEY=VALUE` (done) | env for `GEMINI_*`; `settings.json` for dotted keys (or a `${VAR}` template) | `config.toml` or `codex -c key=value` | `config.toml` under `GROK_HOME`, or `GROK_CONFIG` inline JSON / `GROK_CONFIG_PATH` | SPLIT — the adapter's central method |
| Ranking models | family heuristic, Claude ids only (`engine_sort_key`) | — | — | — | COMMON via `equivalence.ladder()` once the sort is generalised |
| Refreshing model ids | `ai_project-update-models` (Claude only) | not covered | not covered | not covered | SPLIT data sources; one command should cover every catalog member (open decision) |

## H. Authentication (§9)

| Facet | Claude Code | Gemini CLI | Codex CLI | Grok | Verdict |
|---|---|---|---|---|---|
| Credentials | `.claude.json` (account), `.credentials.json` (OAuth) | `GEMINI_API_KEY` / `GOOGLE_API_KEY` / `GOOGLE_APPLICATION_CREDENTIALS` (+ `GOOGLE_CLOUD_PROJECT`, `_LOCATION`); OAuth cache under the config root | `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, `OPENAI_API_KEY` (the provider's `env_key`); `cli_auth_credentials_store` file / keyring / auto | browser OIDC (`grok login`), device code (`grok login --device-auth` — documented for containers and headless hosts), external `auth_provider_command`, API key `XAI_API_KEY` / `model.api_key`; resolution `model.api_key` > `model.env_key` > session token > `XAI_API_KEY`; credential FILENAME NOT FOUND (under `GROK_HOME`) | COMMON mechanism (mount the harness's credential files read-only, forward its env names); SPLIT data |
| Account identity for the status line | `oauthAccount.emailAddress` in `.claude.json` | not researched | not researched | not researched | SPLIT; DEGRADE where unknown |

## I. Display (§10)

| Facet | Claude Code | Gemini CLI | Codex CLI | Grok | Verdict |
|---|---|---|---|---|---|
| Status line | `statusLine` running a script | NOT FOUND | `tui.status_line`, a declarative item list | `[ui.status_line]` `type` = builtin \| command \| disabled (default), `items`, `command`, `refresh_interval`; user or admin config only | DEGRADE (Gemini); SPLIT (Codex — declarative); Grok's `type = "command"` is a direct analogue of the launcher's script |
| Terminal title | the LAUNCHER emits OSC 0 (`DEFAULT_AI.cli_name — <name>`) | `CLI_TITLE` (env) exists | `tui.terminal_title` exists | NOT FOUND (the launcher's own OSC 0 covers it) | COMMON — launcher-side, already reads the catalog |
| Key bindings | `keybindings.json` | not researched | not researched | not researched | unknown; low priority |

## J. Extensions the launcher mounts

| Facet | Claude Code | Gemini CLI | Codex CLI | Grok | Verdict |
|---|---|---|---|---|---|
| MCP servers | `.mcp.json` / `~/.claude.json`: `mcpServers{}` with `command` / `args` / `env` / `url` / `headers` | `mcpServers` in `settings.json`, same shape | `mcp_servers.<id>` in `config.toml`, same fields | `[mcp_servers.<name>]` with `command` / `args` / `url` / `headers` / `bearer_token_env_var` / `enabled` / timeouts — Codex's field set; also reads Claude's MCP config | COMMON model; SPLIT container (JSON object vs TOML table) — mechanical |
| Skills | Agent Skills standard; `.claude/skills` (+ Claude-only frontmatter fields) | Agent Skills standard; `.gemini/skills` or `.agents/skills` | Agent Skills standard; `.agents/skills` (cwd → repo root, home, `/etc/codex/skills`) | Agent Skills standard; `./.grok/skills/`, `~/.grok/skills/`, `~/.agents/skills/`, plugin skills, `[skills] paths`; "Extra keys are ignored" (accepts `model`, `effort`, `license`, `compatibility` without applying them); "Grok is fully compatible with Claude Code with zero configuration needed" — reads Claude's marketplaces, plugins, skills, MCPs, agents, hooks and instruction files | COMMON format IF frontmatter stays inside the spec fields (a lint); SPLIT mount path — mount to `.claude/skills` and `.agents/skills`. Grok reads both and ignores extra keys, so the lint is for Gemini and Codex |
| Custom commands | `.claude/commands/*.md` (legacy) | `~/.gemini/commands/*.toml` | `~/.codex/prompts/*.md` — deprecated | user-invocable skills appear as `/<skill-name>`; also reads `~/.agents/commands/` | SPLIT — or port to skills, the one form all keep |
| Auto memory | `MEMORY.md` index + topic files | `experimental.autoMemory` patch inbox | `features.memories` pipeline | `[memory] enabled` / `GROK_MEMORY` (default off) — a cross-session switch; nothing beyond the toggle is documented (the feature page 404s) | SPLIT / DEGRADE — content (markdown) is COMMON, the mechanism is not |

## K. Traffic (§13)

| Facet | Claude Code | Gemini CLI | Codex CLI | Grok | Verdict |
|---|---|---|---|---|---|
| Telemetry switch | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `GEMINI_TELEMETRY_ENABLED` (+ `_TARGET`, `_OTLP_*`), `privacy.usageStatisticsEnabled` | NOT FOUND (diagnostics only) | NOT FOUND as a key; the enterprise page says `requirements.toml` is where telemetry is disabled and that some of Claude's `managed-settings.json` flags are read | SPLIT; DEGRADE on Codex |
| Critical hosts (firewall) | `api.anthropic.com`, `console.anthropic.com` | ONE host documented: `cloudcode-pa.googleapis.com` (Google's Code Assist network-access page); `generativelanguage.googleapis.com`, `oauth2.googleapis.com`, `accounts.google.com` appear only in community issues for the API-key and OAuth paths — unconfirmed | NO official list; `api.openai.com` and `chatgpt.com` occur as EXAMPLE strings in the permissions docs (`chatgpt.com` for WebSocket upgrades); `auth.openai.com` unverified | DOCUMENTED table: required `cli-chat-proxy.grok.com` (inference proxy, settings) and `auth.x.ai` (OIDC); `api.x.ai` only for API-key auth; optional `code.grok.com` (session sync), `assets.grok.com`; `x.ai` and `storage.googleapis.com` only for the shell installer and `grok update`. TLS via rustls, cannot be disabled — a TLS-inspecting proxy needs its CA in the OS store | COMMON mechanism (the resolver); SPLIT data — a catalog row, moved together with the abort message (`adding_an_ai.md` §13) |

## L. Cowork and cluster (§11, §12)

| Facet | Claude Code | Gemini CLI | Codex CLI | Grok | Verdict |
|---|---|---|---|---|---|
| Prompt injection channel | pty injection via herdr / tmux | same | same | same | COMMON — launcher-side, harness-agnostic (`harness_coupling.md`, "Already agnostic") |
| Turn-end and pre-prompt signals | hooks (F) | hooks (F) | hooks (F) | hooks (F) — but non-blocking except `PreToolUse` | as F: COMMON names map, PROBE the contract |
| Messaging between members | `cluster-chat`, mailbox, `SendMessage` — launcher and Claude Code | launcher side same; the CLI's own messaging: none known | launcher side same; `agents.*` multi-agent tools are in-process | launcher side same; the CLI's own: not researched | COMMON for the launcher's own channel; DEGRADE the CLI-side tool where absent |

## M. Grok in the engine ladder, and what adding it would take

Artificial Analysis rates Grok per effort like the others (Intelligence Index
v4.3; index values re-checked by the author against the full records embedded in Artificial Analysis's grok-4-6 page — all eight match; the page carries several records per release slug, most of them thin references without the metrics; the check that holds is to filter on the presence of the field being read and require exactly one survivor per slug (`launch/ai/equivalence.py`, refresh notes)):

| slug | AA name | index |
|---|---|---|
| `grok-4-6` | Grok 4.6 (high) | 44.41 |
| `grok-4-6-xhigh` | Grok 4.6 (xhigh) | 44.27 |
| `grok-4-6-medium` | Grok 4.6 (medium) | 43.01 |
| `grok-4-5` | Grok 4.5 (high) | 39.08 |
| `grok-4-6-low` | Grok 4.6 (low) | 35.41 |
| `grok-build-0-1-06-16` | Grok Build 0.1 0616 | 27.17 (estimated) |
| `grok-4-3` | Grok 4.3 (high) | 25.39 |
| `grok-4-fast-reasoning` | Grok 4 Fast | 17.94 (estimated) |

Grok 4.6: 500k context, 2 / 6 USD per 1M tokens; xAI's models page: "For
everything else, including code, use Grok 4.6." Against the launcher's Claude
anchors a four-rung ladder is buildable and NOT inverted: Fable 53.37 has no
Grok within 8.9 points and Opus 50.70 is 6.3 above the best Grok (both top
rungs → Grok 4.6 at high, accepting the ceiling); Sonnet 5 at max 38.36 sits
Δ0.72 from Grok 4.5 (high); Haiku 4.5 (reasoning) 17.59 sits Δ0.35 from Grok
4 Fast. Note that on Grok 4.6, xhigh (44.27) scores BELOW high (44.41) — the
effort ceiling is not the rung's top.

Grok became a tag member on 2026-09-13 (`agents/ai/grok/` — its tiers and
knobs from this research), so an instance can be described on it. What it
cannot yet do is launch: that takes a harness adapter (`launch/ai/grok.py`, a
`Adapter` record (`launch/ai/adapter.py`; the harness itself is a tag member since 2026-09-14) with its binary, flags, config-root files, env vars and the
hosts the enterprise page documents — the only one of the four that does).
Two things are open before that: the telemetry key (NOT FOUND) and the
session file format (NOT FOUND — `grok export` is the documented way out).

## What falls out of the matrix

1. **Same structure, different names → a row of data.** Distribution, binary,
   config root and its variable, persona filename, model / effort / thinking
   keys, hook-event names, credential files, critical hosts. These belong in
   the catalog (`Ai`) or in the per-AI budget file — never in an `if ai is`.
2. **Different structure → one adapter method per harness, tested per
   harness.** Rendering settings and policies (JSON vs TOML, patterns vs name
   lists vs sandbox modes), parsing transcripts, parsing the event stream,
   composing the persona within each harness's import and size rules,
   converting budget units (percent / fraction / absolute; tokens /
   characters). This is the adapter's method list — the public surface step 1
   of the order of work gathers for Claude first.
3. **Absent → the capability matrix, explicit.** No output cap (Codex), no
   status-line script (Gemini, Codex), no telemetry switch (Codex), no
   argument-pattern denial (Gemini, estimate), no transcript import
   (everyone). A feature a harness cannot back is switched off visibly for that
   AI, with the picker saying so, never emulated in silence.
4. **Launcher-side mechanisms stay common and untouched:** the container and
   its firewall, pty injection, the picker, the stores, the tag tree, the
   cluster queue. They are what makes this more than `docker run`, and none
   of them knows which AI runs.
5. **Units convert at the boundary, once.** The engine files already state the
   conversions (60 % → 0.6; 100000 tokens → 400000 characters); the adapter
   applies them so no engine author does arithmetic.
6. **Unverified contracts are probes, not assumptions in code** — the list in
   `adding_an_ai.md`; the hook payload is the one that decides how much of
   cowork and the cluster survives a switch.
7. **A harness that reads another's files is a shortcut for the MOUNT, not
   for the adapter.** Grok reads `.claude/` skills, hooks, MCP config and
   CLAUDE.md with zero configuration, which removes conversion work — and
   still breaks on the stream shape (ACP) and the hook contract (only
   `PreToolUse` blocks). Names and files converging does not mean behaviour
   converged; the contract rows decide.
8. **Documentation of the firewall surface is itself uncommon.** Grok
   publishes its required hosts; Google names one; OpenAI names none. The
   allowlist for a harness whose hosts are undocumented has to come from a
   probe (a run behind the launcher's own resolver, watching what fails), and
   the plan should say which rows are documented and which were observed.

Credentials and account files per harness — what to mount where, the
keys-per-AI / OAuth-per-harness rule, the probe results — live in
`plans/credentials.md` (2026-09-14).
