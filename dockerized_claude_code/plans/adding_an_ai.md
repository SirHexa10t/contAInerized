# Adding an AI — requirements, where each lives, and how it is verified

*Companion to `harness_coupling.md`, which lists where the launcher ASSUMES
Claude Code. This file is the solutions side: as each seam is generalised it
gets a row here — what a new AI must provide, where in the code that lands,
and which check proves it. Read this before wiring up a new vendor; read the
other when wondering why something is still Claude-shaped; and read
`harness_commonality.md` for the facet-by-facet verdict on what stays one
mechanism and what splits per AI — the design input for the adapter. Started
2026-09-10 with the first adaptation.*

Vocabulary: an **agent** is a persona in `agents/` (golem, researcher, …); an
**AI** is the assistant product a container runs it on — Claude, ChatGPT,
Gemini, Grok — each from its vendor, and each a TAG MEMBER of `agents/ai/`
(`⟪Claude⟫`, the fifth tag kind since 2026-09-13); a **harness** is the agent
CLI that runs it (Claude Code, Gemini CLI, Codex CLI, Grok Build).

## Status by seam

| Seam (`harness_coupling.md` §) | State | Where the solution lives | Verified by |
|---|---|---|---|
| Which credentials file for which AI / harness | done 2026-09-15 — `plans/credentials.md`: API keys per AI (`~/.ai-agents/credentials/keys/<ai>.env`, `--env-file`, the AI's `key_env`; docker-grammar preflight + audit), login files per harness (`Adapter.auth_files` → `paths.auth_file_mounts` per launch shape; private blanks; a per-file migration of the old pair); `paths.py` no longer binds the Claude pair at import. Residue in ISSUES.md (per-solo-instance `.claude.json`, key-vs-login exclusivity, Codex's key-through-login, two probes) | `plans/credentials.md`, `launch/ai/adapter.py`, `paths.py`, `file_access.py`, `docker_config.py`, `cluster/launching.py`, `tags/migrations.py`, `audit.py` | `test_ai`, `test_file_access`, `test_docker_config.TestKeyEnvFiles`, `test_cluster_launching`, `test_tags.TestCredentialsRelocation`, `test_audit.TestCredentialFiles` |
| The HARNESS as a TAG KIND | done 2026-09-14 — `agents/harness/<key>/tag.info` (vendor, `ais` it runs, binary, package; `⟦ClaudeCode⟧` `⟦GeminiCLI⟧` `⟦CodexCLI⟧` `⟦GrokBuild⟧`, `launch/tags/harness.py`); an AI's `harness` field is the key of its default; a per-instance axis like the AI (`.lego`, `instances.toml`, cluster tables, `Instance.harness`, resolved to the build's else the AI's default; a pair the harness cannot run is refused in a `.lego` and dropped-and-flagged from the store); the form's second radio group, the picker's runtime column after the AI, second in every pane, a Harnesses legend section. The code half is keyed by it: `launch/ai/adapter.py` (`Adapter`), `ADAPTERS`, `adapter_for`, `active_adapter`, `DEFAULT_HARNESS_KEY`; refusal and adoption by the instance's harness | `test_tags.TestHarnessKind`, `test_ai.TestHarnessMembers` / `TestAdapterRecord` / `TestRefusal`, `test_forms`, `test_menu_picker`, `test_essential_files` |
| The AI as a TAG KIND (was: the catalog enum) | done 2026-09-13 — `agents/ai/<key>/` with `tag.info` (vendor, harness, `default`, colours) and `efforts.tiers` (`knobs.mapping` moved to the harness 2026-09-14); `launch/tags/ai.py` (`Ai`, scan + validation, `render`); an `ai` axis in `AgentBuild`, `.lego`, `instances.toml`, cluster tables and `Instance`; `launch/ai/catalog.py` keeps only `DEFAULT_AI_KEY` + the call-time `active_ai_key()` / `set_active_ai()` | `test_tags.TestAiKind`, `test_ai.TestAiMembers` |
| Model equivalence across AIs | done 2026-09-13 as DATA, re-cut 2026-09-14 into CAPABILITY STANDARDS — `agents/ai/capability.standards` lists the dated standards (the quarter a model raised the frontier's AA index: 2025Q1 … 2026Q3 today, setter and index recorded, estimates flagged) and each `agents/ai/<key>/efforts.tiers` answers every standard plus `cheapest` / `best` with that AI's model + effort — the cheapest configuration meeting it, else its best, flagged; the standard's date IS the order, and the registry checks every engine names a standard the AIs answer | `test_ai.TestAiMembers` (every standard, rising indices, efforts within scale), `TestEngineOrder`, `test_tags.TestStandardVocabulary` |
| §8 — the engine budget FILE | done 2026-09-13 — ONE `agents/engine/<tag>/tag.budget` per engine in the launcher's own words (step, switches, amounts — `tags/budget.py`); the 24 per-AI `<ai>.conf` files are gone | `test_tags.TestEngine`, `test_essential_files` |
| §8 — the budget's KEYS per harness | done 2026-09-13, moved 2026-09-14 — `agents/harness/<key>/knobs.mapping` (the settings surface is the CLI's, not the model's; `{provider}` slugs for a multi-model CLI) translates each budget purpose into that AI's native settings (`{value}` templates, unit conversions once); a purpose an AI cannot express is absent and reported as unmapped by `Ai.render`; the sourced reference blocks of the former default confs live there as comments | `test_ai.TestRendering` (Claude renders exactly the former env files; unmapped purposes reported; conversions) |
| §8 — the launcher READING those keys (model sort, effort flag, rendering into settings.json / config.toml) | mostly — `Instance.conf` is the engine's budget rendered by the instance's AI; engine order is the AI-neutral step rank; `effort_args` takes `Instance.effort`; only `-e KEY=VALUE` forwarding exists, so writing Gemini's settings.json / Codex's config.toml / Grok's config.toml from the rendering is the split half | `tags/ai.py`, `tags/engine.py`, `docker_config.py` | `test_ai.py` — no production module spells a harness word |
| §10 / §14 — the CLI's name in strings a user reads | done 2026-09-12 for the live strings — `Ai.cli_name` ("Claude Code" / "Gemini CLI" / "Codex CLI"); the terminal title and the firewall abort read it through `active_ai()` (the abort's hosts come from the same adapter record — §13). The §14 renames (paths, module names, `claude_args`, the container user) stay open | `launch/ai/catalog.py`, `claude_code_config.set_terminal_title`, `firewall/resolver.py` | `test_ai.py`, `test_claude_code_config.py` |
| §1 image and entrypoint | partly, 2026-09-12 — the binary and herdr's agent kind are adapter data (`Harness.binary`, `.herdr_agent_kind`), read by `docker_config`, `cluster/launch_plan`, `cluster/herdr`, `cluster/launching`; the image's install line, env switches and ENTRYPOINT moved into the harness's own layer, `agents/harness/claude-code/Dockerfile`, built last (2026-09-16) — another harness adds its own | `launch/ai/claude_code.py`, `agents/harness/<key>/Dockerfile` | `test_ai.py` (consumers read the record; no production module spells `"claude"`) |
| §2 CLI flags | partly, 2026-09-12 — the flags the launcher emits are adapter data (`print_flag`, `continue_flag`, `effort_flag`, `stream_args`); the flag MAP for another harness and the stream parser are the split half | `launch/ai/claude_code.py`; `docker_config`, `agents_crud`, `quickie/ask` | `test_ai.py` |
| §3 config-dir layout and mounts | partly, 2026-09-12 — the config root's dir and file names (`.claude`, `settings.json`, `CLAUDE.md`, `commands`, `skills`, `history.jsonl`, `projects`) and the relocation / session env vars are adapter data; `paths.py` derives its constants from the Claude record at IMPORT — the residue the switch (step 2) must turn into functions of `active_harness()` | `launch/ai/claude_code.py`, `paths.py` | `test_ai.py` (paths carry the record's names) |
| §4 persona file and injected copy | not yet | — | — |
| §5 settings, permissions, hooks | not yet | — | — |
| §6 transcript format | not yet | — | — |
| §7 streaming output | not yet | — | — |
| §9 authentication | partly, 2026-09-12 — the account and credentials FILENAMES are adapter data (`paths.ACCOUNT_FILE` / `CREDENTIALS_FILE` derive from them); the mount and refresh mechanics stay Claude-shaped | `launch/ai/claude_code.py`, `paths.py` | `test_ai.py` |
| §10 status line, title, keys | not yet | — | — |
| §11 cowork | not yet | — | — |
| §12 cluster | not yet | — | — |
| §13 firewall hosts | done 2026-09-12 — the critical hosts are adapter data (`Harness.critical_hosts`); `firewall/resolver._critical_hosts()` reads them at call time and the abort message names `active_ai().vendor` / `.cli_name` from the same record, so hosts and words move together. The widening block for Anthropic's registered space stays a resolver fact until another harness documents one | `launch/ai/claude_code.py`, `firewall/resolver.py` | `test_ai.py` |
| §14 names | not yet | — | — |
| Switching the AI an instance runs | done 2026-09-13 — `ai` is a per-instance axis (`instances.toml`, `.lego`, cluster tables), resolved to the tree's default member when unset; `Instance.conf` / `.model` / `.effort` follow it. Offered as the ⟪⟫ radio group atop every instance and member form; shown as a column between the tags and the name, first in every pane, and in an AIs section of the F8 legend. Each solo launch path (`run.py`, the quickie) refuses an AI without a harness adapter before persist and build (`launch/ai.refusal_for`) and then ADOPTS the instance's AI (`launch/ai.adopt`) before the first harness word is read; the cluster launch refuses per member and resolves the harness per member for the pane env and mounts, but adopts none — its settings install and title still read the default's names (residue, with `paths.py`'s import-time constants) | `launch/tags/*`, `launch/ai/catalog.py`, `gui/forms.py`, `gui/menu_picker.py` | `test_forms`, `test_menu_picker`, `test_picker_previews`, `test_styles`, `test_run`, `test_docker_config`, `test_cluster_launching`, `test_ai.TestRefusal` |
| Switching: what an instance can CARRY across AIs | researched 2026-09-12 — "Moving an instance between AIs" below: skills, MCP definitions and the persona file move as-is or by pointer; commands, policies, hooks and memory move with conversion and stated losses; transcripts do not move at all | the section below; Claude Code's own `/import` for the way back | the probe list below, once a binary is in an image |

## Requirements for a new AI (grows with the table above)

### 1. Add a member dir: `agents/ai/<key>/`

The key is the dir name — lowercase, passing the launcher's own label rule
(`tags.identity.label_error`); it becomes the value stored in `.lego` and
`instances.toml` (`ai = "<key>"`) and the key a harness adapter registers
under. Three files, all required (`launch/tags/ai.py` validates them at scan
time under the tree's strict rule, so a fault fails `bash check.sh`, not a
launch):

- **`tag.info`** — the kind's usual `shortname` (what shows inside `⟪ ⟫`),
  `fullname`, `short_description`, `full_description` (name the COMPANY in it,
  for searchability: Anthropic, Google, OpenAI, SpaceXAI (xAI)); plus
  `vendor`, `harness` (the agent CLI's name — the one commonly bundled with
  the product), `default` (exactly one member says `true`), and the tag's own
  colours `fg` / `bg` as hex — the first kind coloured per member, after the
  logos.
- **`efforts.tiers`** — this AI's TIER for every CAPABILITY STANDARD: a table
  per standard — `cheapest`, each dated quarter of the kind's shared
  `agents/ai/capability.standards`, `best` — with a `model` id and an `effort`
  word, plus `[scale]` listing the AI's effort words. A dated standard is a
  quarter in which a model raised the frontier's Artificial Analysis index
  (`set_by`, `index`, `estimated`); standards only rise with time, so the
  date is the order, and the launcher validates that the indices climb. A tier
  is the cheapest live, rated, launchable configuration whose index meets the
  standard (AA blended 3:1 price, then the lowest index that meets); an AI
  that cannot meet it answers with its best, flagged as falling short. The
  ends are each AI's own cheapest and strongest. Inline comments carry the
  index and the token cost. `tags/ai.py` validates the file at scan.
- **`knobs.mapping`** — since 2026-09-14 a HARNESS file (`agents/harness/<key>/`),
  not the AI's: the launcher's budget purposes → that CLI's native settings
  (`Harness.render(budget, ai)`), with `{provider}` slugs where a multi-model
  CLI spells `anthropic/<model>`. See the harness kind's row above.

**Verify:** `test_ai.TestAiMembers` (every member complete, every step,
efforts within the scale, one default that the code's `DEFAULT_AI_KEY`
matches) and `TestRendering` (every engine renders on every AI with its
model; Claude renders byte-for-byte what its former env files said);
`test_tags.TestAiKind` covers the validation faults on fixture trees.

### 2. Engines never learn the AI

An engine is `agents/engine/<tag>/{tag.info, tag.budget}` — the budget in the
launcher's own words (`effort_tier`, switches, amounts; `tags/budget.py`), overlaid
key-by-key by nested dirs. Adding an AI touches no engine: the AI's two files
translate every engine. What the current engines mean, in standards: `golem` →
`cheapest`, `poet` → `2025Q3`, `quick` → `2025Q4`, `reliable` → `2026Q2`,
`default` / `thinker` / `researcher` / `breakthrough` → `best` (on Claude the
same models and efforts the eight named steps gave until 2026-09-14). `tag.info`
describes the TIER, never a model (a test forbids model and vendor words
there); the tag form renders the engine's own standard beside the words, and
the legend / preview / banner render the model the AI runs for it.

### 3. Give it a harness adapter (the code half)

`launch/ai/<key>.py` — an `Adapter` record (binary, flags, config-root files,
env vars, critical hosts; `launch/ai/adapter.py`) registered in
`launch/ai/__init__.ADAPTERS` under the HARNESS member's key
(`agents/harness/<key>`), with `name` equal to that member's fullname and
`binary` equal to its binary (a test holds them together). Until it exists,
`adapter_for(key)` raises and the
launcher refuses to launch an instance on that AI (`refusal_for`, checked in
`run.py` before persist and build, and per member by the cluster launch — the
picker still describes such an instance; F2 switches it back). The behaviour half — rendering the settings into the harness's
file, parsing its transcripts and event stream, composing the persona — comes
seam by seam (order of work below).

## Tier equivalence (engine → model and effort, per AI) — history

*Since 2026-09-13 the ladder lives in `agents/ai/*/efforts.tiers` (one file
per AI; since 2026-09-14 keyed by CAPABILITY STANDARD — `agents/ai/capability.standards`
— instead of eight named steps); this section records how the per-engine rungs
were first derived and is kept for the evidence.*

How the seven non-default engines were derived on 2026-09-10 — the rung each
tier's `claude.conf` occupies, and the id on the same rung of the other two
line-ups. The rungs come from the vendors' own descriptions (quoted in each
file); the ids are ones the CLI's own tables know (Gemini: the settings
schema's built-in aliases and `modelIdResolutions`; Codex: the models page's
"recommended" set), checked the same day against the API's model pages for
status and limits.

| Engine (Claude rung) | Claude | Gemini CLI | Codex CLI |
|---|---|---|---|
| `default`, `thinker`, `researcher`, `breakthrough` (Fable, max) | `claude-fable-5-1`, max | `gemini-3.8-flash`, `thinkingLevel HIGH` (41.19, the strongest Gemini; since 2026-09-12 — `gemini-3.1-pro-preview`, 30.36, before) | `gpt-6-astra`, `xhigh` |
| `reliable` (Opus — dependable, a different line) | `claude-opus-5`, max | `gemini-3.7-flash`, `HIGH` (39.43, the previous Flash generation; since 2026-09-12 — `gemini-2.5-pro`, 16.66, before) | `gpt-5.6-sol`, `xhigh` (the top of the previous line) |
| `quick` (Sonnet, high) | `claude-sonnet-5`, high (since 2026-09-12; 4.6 before) | `gemini-3.5-flash`, `HIGH` (the newest Flash the CLI's tables know) | `gpt-5.6-terra`, `high` |
| `poet` (Sonnet, medium) | `claude-sonnet-5`, medium (since 2026-09-12; 4.6 before) | `gemini-3.5-flash`, `MEDIUM` | `gpt-5.6-terra`, `medium` |
| `golem` (Haiku, low, thinking off) | `claude-haiku-4-5`, low, `MAX_THINKING_TOKENS=0` | `gemini-3.1-flash-lite`, `MINIMAL` (the CLI's `flash-lite` tier; its documented near-zero level) | `gpt-5.6-luna`, `low`, `model_reasoning_summary "none"` |

Caps and switches carried with the tier: `CLAUDE_CODE_MAX_OUTPUT_TOKENS` →
Gemini `maxOutputTokens`, same number (Codex: NOT FOUND); the tool-output caps
of `researcher` / `breakthrough` → Gemini `tools.truncateToolOutputThreshold =
400000` characters, Codex `tool_output_token_limit = 100000`; `researcher`'s
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=60` → Gemini `model.compressionThreshold = 0.6`,
Codex commented (absolute tokens; the window is unconfirmed); `poet` / `golem`
memory → `experimental.autoMemory` / `features.memories`; `golem`'s "no
background subagents" → `experimental.enableAgents = false` /
`agents.enabled = false`; `golem`'s telemetry → `GEMINI_TELEMETRY_ENABLED =
false` + `privacy.usageStatisticsEnabled = false` (Codex: NOT FOUND); prompt
caching: NOT FOUND on both.

**Which criterion decides each rung** — so a refresh never re-derives a rung
from the index alone: the Fable rung is the ladder's top (nearest index to
Fable: Astra at max, Δ0.56); `reliable` is stability — a dependable model from
a different line, the one Gemini pin with no re-resolution entry — and NOT
nearest index (Sol at xhigh sits Δ6.56 below Opus 5 at max; Astra at high
would be Δ0.35 but is the default's line); `golem` is cheapest above a floor;
the Sonnet rung is the one genuine nearest-index question. The index's own
verdicts, 2026-09-10 (`launch/ai/equivalence.py`; "est." = AA's estimate, not
a measurement): Sonnet 5 scores 28.44 at medium and 38.36 at max, with high —
`quick`'s effort — unscored; `poet`'s pins, Terra (medium) 30.42 and 3.5 Flash
(medium) 33.63 est., sit Δ2 and Δ5 above its medium, and `quick`'s Terra (high)
34.49 and 3.5 Flash (high) 32.98 sit inside the medium-to-max bracket; Haiku 4.5
(non-reasoning) 15.41 est. → Gemini 3.1 Flash-Lite (high) 16.03 Δ0.62, Codex
Luna (non-reasoning) 16.76 est. — `golem`'s Luna at low is 21.83 est., and its
Gemini pin runs 3.1 Flash-Lite at MINIMAL, which AA does not rate (its one row
is at high), so the real bottom rung sits below 16.03 by an unknown amount. And the
two findings the index made against the Gemini pins of 2026-09-10, applied
on 2026-09-12 (the ladder decision below): the Fable-rung pin 3.1 Pro Preview
scored 30.36 (Sonnet level) and the Opus-rung pin 2.5 Pro 16.66 (below the
Haiku reference); they are now 3.8 Flash (high) 41.19 and 3.7 Flash (high)
39.43, ids the CLI's tables do not know, pinned under a stated assumption.

Facts that decided the rungs, for the next re-check: the API lists newer Flash
models (`gemini-3.5-flash-lite`, `gemini-3.6/3.7/3.8-flash`) that the CLI's
tables do not know; `gemini-3-pro-preview` was shut down at the API on 2026-03-09 (replacement
`gemini-3.1-pro-preview`, per the deprecations page) while still the CLI's
`pro` / `auto` default; without preview access the Fable and Opus rungs both
resolve to `gemini-2.5-pro`, leaving the Gemini ladder three rungs, not four,
and `gemini-2.5-pro` / `gemini-3.1-flash-lite` are the two pins the CLI uses
verbatim; Codex's per-model pages give
`gpt-5.6-sol/-terra/-luna` the efforts none / low / medium (default) / high /
xhigh / max and `gpt-6-astra` low / medium / high / xhigh / max — `minimal`
(in the config enum) is on none of them, so the cheapest tier uses `low`.

## Knob equivalence (Claude Code → Gemini CLI → Codex CLI)

What the two baselines were built from, 2026-09-10 — first from the pages the
author fetched, then RECONCILED the same day with `researcher__primary`'s
independent review of the official docs (its source list is appended below).
Two corrections came out of that pass and are reflected here and in the files:
Codex has NO output-token cap key (`model_max_output_tokens` is absent from the
exhaustive reference — a summary of the config-advanced page had suggested it),
and `gpt-5.6` is not a listed slug (the models page names `gpt-6-astra` as the
most capable and lists the `gpt-5.6-sol/-terra/-luna` family; unset, Codex picks
"a recommended model"). "env" = an environment variable the CLI reads;
"settings" = a `settings.json` path; "toml" = a `config.toml` key (also settable
as `codex -c key=value`); "flag" = a CLI flag.

| Claude Code | Gemini CLI | Codex CLI |
|---|---|---|
| `ANTHROPIC_MODEL` (env) | `GEMINI_MODEL` (env), `model.name` (settings), `-m`/`--model` (flag; default `auto` → 2.5-pro or 3-pro-preview by preview settings). Principal ids (the full list, incl. `-customtools` and the Gemma 4 aliases, is in `gemini.conf`): `gemini-3.1-pro-preview`, `gemini-3.1-flash-lite`, `gemini-3-flash-preview`, `gemini-3.5-flash`, `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-2.5-flash-lite` — and `gemini-3-pro-preview`, SHUT DOWN at the API 2026-03-09 yet still the CLI's `pro`/`auto` target, listed so nobody re-adds it; tiers `pro`/`flash`/`flash-lite`/`auto` (never use one in a conf) | `model` (toml), `-m`/`--model` (flag). Slugs: `gpt-6-astra` (most capable), `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.3-codex-spark` (preview), `gpt-5.5` (previous gen); `gpt-5.4`/`-mini` retired 2026-08-31; `gpt-5.2`, `gpt-5.3-codex` deprecated. No literal default |
| `CLAUDE_CODE_EFFORT_LEVEL` low/medium/high/max (env + `--effort`) | `…generateContentConfig.thinkingConfig.thinkingLevel` (Gemini 3 — `HIGH` in the built-in `chat-base-3` alias) or `.thinkingBudget` tokens (Gemini 2.5 — 8192 in `chat-base-2.5`), set on a `modelConfigs` alias or override (settings) | `model_reasoning_effort` minimal/low/medium/high/xhigh (toml); `plan_mode_reasoning_effort` adds `none`. (The models page's Light/…/Max/Ultra are UI labels, not config values) |
| `CLAUDE_CODE_ENABLE_THINKING` | inherent; visibility via `ui.inlineThinkingMode` off/full (settings), `thinkingConfig.includeThoughts` (schema) | inherent; `model_reasoning_summary` auto/concise/detailed/none, `hide_agent_reasoning`, `show_raw_agent_reasoning` (toml) |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | `…generateContentConfig.maxOutputTokens` (settings, per alias/override) | NOT FOUND — no output cap in the reference (`model_context_window` sets the context, not the output) |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (percent) | `model.compressionThreshold` 0.0–1.0 fraction (default 0.5) | `model_auto_compact_token_limit` absolute tokens + `_scope` total/body_after_prefix |
| `MAX_MCP_OUTPUT_TOKENS`, `CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS` | `tools.truncateToolOutputThreshold` (CHARACTERS, default 40000 — label the unit), `model.summarizeToolOutput` per-tool token budgets, `contextManagement.tools.distillation.maxOutputTokens` (tokens, default 10000) | `tool_output_token_limit` (token budget per stored tool output); `mcp_servers.<id>.tools.<tool>.output_token_limit` per MCP tool |
| `ENABLE_TOOL_SEARCH` | NOT FOUND; `mcp.allowed` / `mcp.excluded`, `--allowed-tools` | NOT FOUND; `mcp_servers.<id>.enabled_tools` / `disabled_tools`; `features.code_mode.enabled` (off, under development) |
| `CLAUDE_CODE_SUBAGENT_MODEL` | NOT FOUND as a key — workaround: `modelConfigs.overrides[].match.overrideScope = "<agentName>"`; `experimental.enableAgents` (default true) | `agents.default_subagent_model`, `agents.default_subagent_reasoning_effort` |
| `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` | NOT FOUND | `agents.max_concurrent_threads_per_session` |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` / `DISABLE_TELEMETRY` | `GEMINI_TELEMETRY_ENABLED` (+ `_TARGET`, `_OTLP_*`, `_LOG_PROMPTS`) (env; opposite polarity), `privacy.usageStatisticsEnabled` | NOT FOUND (diagnostics only: `RUST_LOG`, `log_dir`) |
| `MAX_THINKING_TOKENS=0` (thinking off) | Gemini 3: `thinkingLevel MINIMAL` ("as close as possible to a zero budget"); Gemini 2.5 Flash / Flash-Lite: `thinkingBudget 0`; 2.5 Pro cannot turn thinking off | `model_reasoning_effort` at the model's floor (`low` on the 5.6 line) + `model_reasoning_summary "none"`; the API's `none` is not in the config enum |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | `experimental.autoMemory` (default false; patches held for review in `/memory inbox`) — the GEMINI.md `/memory` itself is always on | `features.memories` (off by default); `memories.use_memories` / `memories.generate_memories` (default true) |
| `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`, `CLAUDE_CODE_DISABLE_CRON` | `experimental.enableAgents` (default true); scheduled tasks: NOT FOUND | `agents.enabled` (default true), `features.multi_agent`; scheduled tasks have no config key |
| `DISABLE_PROMPT_CACHING` | NOT FOUND | NOT FOUND |
| `CLAUDE_CONFIG_DIR` (§3, §12) | `GEMINI_CLI_HOME` (env; + `GEMINI_CLI_SYSTEM_SETTINGS_PATH`, `GEMINI_CLI_SYSTEM_DEFAULTS_PATH`) | `CODEX_HOME` (env; + `CODEX_SQLITE_HOME`) |
| `CLAUDE.md` (§4) | `GEMINI.md` — global `~/.gemini/GEMINI.md`, project root → cwd, subdirs up to `context.discoveryMaxDirs`; rename via `context.fileName`. `GEMINI_SYSTEM_MD` REPLACES the system prompt | `AGENTS.md` — global `$CODEX_HOME/AGENTS.md` (`AGENTS.override.md` wins), git root → cwd, `project_doc_max_bytes` (32 KiB), `project_doc_fallback_filenames`, `project_root_markers`. `model_instructions_file` REPLACES the built-in instructions; `developer_instructions` appends |
| `permissions.defaultMode` / allow / deny (§5) | `general.defaultApprovalMode` default/auto_edit/plan (YOLO via flag); `tools.core` / `allowed` / `confirmationRequired` / `exclude`; `security.*`, `admin.*` | `approval_policy` on-request/never/granular (`untrusted` retired); `sandbox_mode` read-only/workspace-write/danger-full-access + `sandbox_workspace_write.*`; `default_permissions`; `web_search`; `shell_environment_policy.*` (decides which launcher `-e` vars the agent's shell sees) |
| hooks `Stop`, `UserPromptSubmit` (§5, §11, §12) | `hooks.AfterAgent` ≈ Stop, `hooks.BeforeAgent` ≈ UserPromptSubmit (+ BeforeTool/AfterTool/SessionStart/SessionEnd/PreCompress/BeforeModel/AfterModel/BeforeToolSelection/Notification) | `hooks.Stop`, `hooks.UserPromptSubmit` — same names (+ PreToolUse/PermissionRequest/PostToolUse/PreCompact/PostCompact/SessionStart/SessionEnd/SubagentStart/SubagentStop/Interrupt); `notify` runs on turn completion |
| `claude -p`, `--output-format stream-json` (§2, §7) | `gemini -p`; `-o`/`--output-format text|json|stream-json`; `-i`/`--prompt-interactive`; exit 0/1/42/53 | `codex exec` (`codex e`); `--json`; `-o`/`--output-last-message`; `--ephemeral`; `--skip-git-repo-check` |
| `--continue` (§2) | NOT FOUND on the pages read | `codex exec resume` |
| `.claude.json` / `.credentials.json` (§9) | `GEMINI_API_KEY` / `GOOGLE_API_KEY` / `GOOGLE_APPLICATION_CREDENTIALS` (+ `GOOGLE_CLOUD_PROJECT`/`_LOCATION`) (env) | `CODEX_API_KEY` (non-interactive), `CODEX_ACCESS_TOKEN` (automation), `OPENAI_API_KEY` (the `openai` provider's `env_key`); endpoint override is the toml key `openai_base_url` (no env var); `cli_auth_credentials_store` |
| — (a trap, not a knob) | — | `CODEX_NON_INTERACTIVE` is an INSTALLER variable, not a headless switch |

A bridge worth knowing for the Gemini adapter: `settings.json` string values
interpolate `$VAR`, `${VAR}` and `${VAR:-default}` at load, so an env-shaped
budget can drive Gemini through a settings template that references the
variables the launcher exports.

Sources — fetched by the author: geminicli.com/docs/reference/configuration/,
geminicli.com/docs/cli/headless/, geminicli.com/docs/cli/generation-settings/,
github.com/google-gemini/gemini-cli/blob/main/schemas/settings.schema.json,
learn.chatgpt.com/docs/config-file/{config-basic,config-advanced,config-reference},
learn.chatgpt.com/docs/config-file/environment-variables, learn.chatgpt.com/docs/models;
for the tier files (2026-09-10): ai.google.dev/gemini-api/docs/{models,thinking,tokens} and
.../docs/models/<id>, cloud.google.com/vertex-ai/generative-ai/docs/thinking,
platform.openai.com/docs/models/<slug>.
Supplied by the researcher's review (URL given, not re-fetched here):
github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md,
…/docs/cli/settings.md, geminicli.com/docs/cli/cli-reference/, …/docs/cli/model/,
github.com/google-gemini/gemini-cli/blob/main/docs/get-started/gemini-3.md,
learn.chatgpt.com/docs/developer-commands?surface=cli,
learn.chatgpt.com/docs/agent-configuration/{agents-md,subagents}.

## Order of work (agreed 2026-09-12)

The seams above are done in the order that lets a non-Claude container start
soonest while paying every cost once — shape A of `harness_coupling.md`, one
adapter per harness:

1. Gather the Claude constants into one adapter module (binary name, the
   print / continue / stream flags, config directory and its relocation
   variable, settings and persona filenames, transcript locations, credential
   files, the critical firewall hosts), the rest of the tree importing from
   it; and make the AI a call-time value (`active_ai()`), not an import-time
   one. No behaviour changes. **Done 2026-09-12:** `launch/ai/harness.py`
   (`Harness`, renamed `adapter.py` / `Adapter` on 2026-09-14 when the harness
   became a tag kind), `launch/ai/claude_code.py` (`CLAUDE_CODE`), `active_ai()` /
   `active_harness()` (now `active_harness_key()` / `active_adapter()`); nine modules re-pointed; a test forbids the words
   elsewhere. Residue: `paths.py` binds the record at import.
2. Decide and store the switch — a per-instance field defaulting from the
   catalog; the picker UI for it later. **Done 2026-09-13 as the AI TAG KIND**
   (the `ai` axis, stored like the engine), and the same day the ⟪⟫ radio
   section, picker column, preview line, legend section, `adopt()` at each
   solo launch path and the no-adapter refusal (solo and per cluster member).
3. §1: an image layer per CLI, selected by the instance's AI, the way
   professions are layers. Unblocks every probe in the list below.
4. §3, §4, §9: config directory, persona file, credentials — a plain-mode
   container starts.
5. §2, §7: flags and the stream parser — quickie works.
6. §5: policies as per-harness data files beside `policy.json`.
7. §6, §11, §12: transcripts, cowork hooks, cluster hooks, per the capability
   matrix.
8. §14: the renames, last.

## Probes once a binary is in an image

Everything below is an ASSUMPTION or a NOT FOUND today; each is settled by one
run against the real CLI, and none should be re-researched from documentation:

- Gemini CLI requests an id absent from its alias and `modelIdResolutions`
  tables verbatim (the re-rung's one assumption: `gemini-3.8-flash`,
  `gemini-3.7-flash`).
- Gemini CLI's compression helper for a model without a dedicated alias falls
  back to `chat-compression-default`, which names the shut-down
  `gemini-3-pro-preview` — does compression fail for such a model?
- Codex accepts `model_reasoning_effort = "max"` in `config.toml` (the API
  lists it; the config reference's enum stops at `xhigh`).
- Codex's effective context window under ChatGPT sign-in (`codex debug
  models`) — settles `researcher`'s commented compaction limit.
- Gemini's `AfterAgent` / `BeforeAgent` and Codex's `Stop` /
  `UserPromptSubmit` hooks: does the payload carry a session id and a
  transcript path, and does a non-zero exit block the prompt? Both are what
  cowork's capture and the cluster brief rely on.
- Gemini: is there any single read-only switch, and can `tools.allowed` /
  `tools.exclude` express an argument pattern like `Bash(git *)`, or is a
  hook the only way to say no-git?

## Moving an instance between AIs — what a switch can carry

Researched 2026-09-12 by a second reader from the vendors' documentation (the
sources are listed at the end; estimates are marked); the four headline claims
— Claude Code's `/import`, the AGENTS.md ↔ CLAUDE.md bridge, the shared
`.agents/skills` path, Codex's prompt deprecation — were re-verified verbatim
on the pages by the author. Short form: **configuration transfers, and half of
that job already exists one-way; conversation transcripts do not transfer at
all.**

### 1. Chat history — does not transfer

| | Claude Code | Gemini CLI | Codex CLI |
|---|---|---|---|
| Where | `<config>/projects/<slug>/<uuid>.jsonl` (cwd as slug), `history.jsonl` at the root | `~/.gemini/tmp/<project-hash>/chats/`, checkpoints beside | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ISO8601>-<uuid>.jsonl` |
| Shape | JSONL: `type` user/assistant, `message.content` string or blocks, `isSidechain`, `timestamp` | JSON `checkpoint-<tag>.json`; records prompts, responses, every tool call and result, token usage, reasoning summaries "when available" | JSONL: `{timestamp, ordinal, type, payload}`, types `session_meta` / `event_msg` / `response_item` (line shape from community discussions only) |
| Resume | `--continue`, `--resume` | `--resume [index\|UUID]`, `--list-sessions`, `/resume` with save / list / resume | `codex resume` picker, `--last`, `--all`, `codex exec resume` |
| Import from a file | none documented | none documented | none documented |
| Retention | `cleanupPeriodDays` | `sessionRetention.*`, default 30 days | `history.persistence`, `history.max_bytes` |

Lossless conversion is not plausible (assessment): no CLI documents an
import path; the turn models differ (Codex splits events from response
items; Claude's `thinking` blocks carry no readable text — `transcripts.py`
says so; Gemini keeps reasoning summaries only sometimes); reasoning content
is provider-bound (Gemini's minimal level requires thought signatures and
returns a 400 without them); and Codex broke its own rollout format at 0.32.0.
The realistic handoff is lossy: summarise the old transcript and hand it over
as the first prompt — `transcripts.py` already parses the Claude side.

### 2. Skills and commands — skills move as-is, commands do not

All three implement the Agent Skills open standard (agentskills.io): a
`SKILL.md` with YAML frontmatter carrying `name` and `description`.

| | Claude Code | Gemini CLI | Codex CLI |
|---|---|---|---|
| Skill paths | `~/.claude/skills/<n>/SKILL.md`, `.claude/skills/…`, nested, plugin, enterprise | `~/.gemini/skills/` or `~/.agents/skills/`; `.gemini/skills/` or `.agents/skills/` | `$CWD/.agents/skills` up to `$REPO_ROOT/.agents/skills`, `$HOME/.agents/skills`, `/etc/codex/skills` |
| Commands | `.claude/commands/<n>.md`, Markdown + frontmatter, `$ARGUMENTS` / `$1` / named; legacy, skills preferred | `~/.gemini/commands/*.toml` and `<project>/.gemini/commands/`: TOML, `prompt` required, `{{args}}`, `!{…}` shell, `@{…}` file, subdir → `/git:commit` | `~/.codex/prompts/*.md`, top level only, positional args — **deprecated**: "Custom prompts are deprecated. Use skills" |

`.agents/skills` is a path Gemini and Codex both read, so a skill directory
placed there serves both without conversion; Claude does not read it, but
`/import` covers that gap. Gemini's own words, on the skills landing page:
"The `.agents/skills/` alias provides an interoperable path for managing
agent-specific expertise that remains compatible across different AI tools"
— the same page ties Gemini to the standard ("Based on the Agent Skills open
standard, a 'skill' is a self-contained directory…"), while the four
discovery locations are on its "Using Agent Skills" page. Trap: Claude's frontmatter has spec fields
(`name`, `description`, `license`, `compatibility`, `metadata`,
`allowed-tools`) and Claude-only fields (`disable-model-invocation`,
`user-invocable`, `model`, `effort`, `context`, `agent`, `background`,
`hooks`, `paths`, `shell`, `arguments`) — a non-spec field makes packaging
fail elsewhere, so a skill meant for more than one AI stays inside the spec
set (a lint, when we ship such skills). Commands do not share a format at
all; if ours are ever ported, port them to SKILLS — the only form all three
keep.

### 3. Permissions and hooks — mostly faithful, contract unverified

| | Claude Code | Gemini CLI | Codex CLI |
|---|---|---|---|
| Allow / deny | `permissions.allow / ask / deny`, deny → ask → allow, rules like `Bash(npm run *)`, `Read(./.env)` | `tools.core`, `tools.allowed`, `tools.confirmationRequired`, `tools.exclude` | `permissions.<name>.filesystem / network / unix_sockets` profiles, execpolicy rules |
| Mode | `permissions.defaultMode` | `general.defaultApprovalMode`: default / auto_edit / plan | `approval_policy` on-request / never / granular; `sandbox_mode` read-only / workspace-write / danger-full-access; `default_permissions` |
| Sandbox | `sandbox.enabled` | `tools.sandbox`, `tools.sandboxAllowedPaths`, `tools.sandboxNetworkAccess` | `sandbox_workspace_write.{writable_roots, network_access, exclude_slash_tmp, exclude_tmpdir_env_var}` |

The launcher's policy sets, mapped (assessment):

| policy | Gemini CLI | Codex CLI |
|---|---|---|
| no-net | `tools.sandboxNetworkAccess = false` — faithful | `sandbox_workspace_write.network_access = false` or a profile — faithful |
| read-only | no single switch found; `tools.exclude` of every write tool — lossy | `sandbox_mode = "read-only"` — faithful |
| free-bash, all-actions | `--yolo` / approval mode yolo — faithful | `--dangerously-bypass-approvals-and-sandbox` — faithful |
| plan-first | `general.defaultApprovalMode = "plan"` + `general.plan.enabled` — faithful | plan mode exists (`plan_mode_reasoning_effort`) — likely, unverified |
| no-git | Claude denies by argument pattern; Gemini's lists read as tool NAMES — probably needs a hook (estimate) | execpolicy rules or a profile — likely faithful |

Hook events, from Gemini's settings schema and Codex's config reference:

| Claude Code | Gemini CLI | Codex CLI |
|---|---|---|
| PreToolUse / PostToolUse | BeforeTool / AfterTool | PreToolUse / PostToolUse |
| **UserPromptSubmit** | **BeforeAgent** | **UserPromptSubmit** |
| **Stop** | **AfterAgent** | **Stop** |
| PreCompact | PreCompress | PreCompact, PostCompact |
| SessionStart / SessionEnd | SessionStart / SessionEnd | SessionStart / SessionEnd |
| — | BeforeModel, AfterModel, BeforeToolSelection, Notification | PermissionRequest, SubagentStart, SubagentStop, Interrupt |

Codex uses Claude's names for the two hooks the launcher relies on. What is
NOT FOUND for either CLI is the contract behind the name: whether the payload
carries `prompt_id`, `session_id` and `transcript_path`, and whether a
non-zero exit blocks the prompt — the probe list above.

### 4. Memory and instruction files — content moves, mechanisms do not

| | Claude Code | Gemini CLI | Codex CLI |
|---|---|---|---|
| Instruction file | `CLAUDE.md` | `GEMINI.md` (renameable, `context.fileName`) | `AGENTS.md`; `AGENTS.override.md` wins |
| Hierarchy | managed → `~/.claude/CLAUDE.md` → `./CLAUDE.md` or `./.claude/CLAUDE.md` → `./CLAUDE.local.md`, concatenated root-down; subdir files on demand | global `~/.gemini/<name>` → project root down to cwd → subdirs, `context.discoveryMaxDirs` (200) | global `$CODEX_HOME` (override, then plain — first non-empty only) → git root down to cwd; `project_doc_fallback_filenames` |
| Size | skips a file over 4 MiB; target under 200 lines | NOT FOUND | `project_doc_max_bytes`, 32 KiB |
| Imports | `@path`, 4 hops | `context.includeDirectories` | none; `developer_instructions` appends |
| Replace the system prompt | `--append-system-prompt` | `GEMINI_SYSTEM_MD` | `model_instructions_file` |
| Auto memory | `<project>/memory/` with `MEMORY.md` index + topic files, first 200 lines / 25 KB loaded; `autoMemoryDirectory` | `experimental.autoMemory` (off): `.patch` files under `<projectMemoryDir>/.inbox/<kind>/`, held for review | `features.memories` (off) + `memories.*` extraction and consolidation knobs |

The persona file moves by pointer, not conversion: Claude's docs say "Claude
Code reads CLAUDE.md, not AGENTS.md" and prescribe an `@AGENTS.md` import
line or `ln -s AGENTS.md CLAUDE.md`. `MEMORY.md` is plain markdown, so its
content carries; the three auto-memory mechanisms do not (an index plus topic
files; a review queue of diffs; a managed extraction pipeline). On a switch,
flatten `MEMORY.md` and its topic files into the target's instruction file and
accept that it becomes static — and mind Codex's 32 KiB per-level cap.

### 5. Other per-instance configuration

| | Claude Code | Gemini CLI | Codex CLI |
|---|---|---|---|
| MCP | `.mcp.json` (project), `~/.claude.json` (user): `mcpServers{}` with `command` / `args` / `env` / `url` / `headers` | `mcpServers` in `settings.json`, same shape | `mcp_servers.<id>` in `config.toml`, same fields in TOML |
| Status line | `statusLine` running a script | NOT FOUND | `tui.status_line`, a list of items, not a script |
| Terminal title | set by the CLI | `CLI_TITLE` (env) | `tui.terminal_title`, default `["spinner", "project"]` |
| Config dir | `CLAUDE_CONFIG_DIR` | `GEMINI_CLI_HOME` | `CODEX_HOME` |

MCP definitions are the second portable item: one semantic model, the same
field names, JSON object versus TOML table. The launcher's status-line hook is
the odd one out — Codex's equivalent is declarative, so the script contract
does not survive.

### Summary, and the one-way bridge that already exists

- **As-is:** skills inside the spec field set (`.agents/skills` read by
  Gemini and Codex; `/import` for Claude); MCP definitions, JSON ↔ TOML; the
  persona file, by import line or symlink.
- **With conversion:** commands (target skills, not commands); policy sets
  (faithful for network, plan and bypass; weak for read-only on Gemini;
  doubtful for argument-pattern denial on Gemini); hook wiring (names map,
  payload and blocking contract unverified); memory content (flattened,
  static).
- **Not at all:** conversation transcripts — plan for summary-as-first-prompt.
- **Already built, one-way:** Claude Code's
  `/import [codex|gemini|cursor] [--dry-run] [--yes]` (v2.1.213+; not on
  Bedrock, Google Cloud Agent Platform, Microsoft Foundry and some other
  surfaces) carries instruction files, MCP servers, commands, subagents and
  skills INTO Claude — the migration for switching an instance back; do not
  rebuild it.

Sources: code.claude.com/docs/en/{memory,commands,skills,mcp,permissions};
geminicli.com/docs/cli/{skills,using-agent-skills,creating-skills}/;
github.com/google-gemini/gemini-cli — schemas/settings.schema.json (hook
events, tool keys, approval mode, read directly), docs/cli/{session-management,
custom-commands,checkpointing}.md; learn.chatgpt.com/docs/{config-file/
config-reference,build-skills,custom-prompts,developer-commands};
github.com/openai/codex discussions #3827 and #1076 (rollout line shape and the
0.32.0 break — community-grade, flagged); `launch/transcripts.py` (the Claude
JSONL shape, first-hand). Searched for and not found: a transcript import in
any CLI; a Gemini instruction-file size cap; a Gemini status-line equivalent;
the hook payload and blocking-exit contract for Gemini and Codex.

## Decisions still open

- **How the operator switches AIs** — a `ui_profile.toml` key, a per-instance
  choice in the form, or per engine? Until decided, `DEFAULT_AI` is the single
  value every consumer reads, so the switch will be one assignment plus the
  UI that sets it. The former blocker is gone (2026-09-10): the engines'
  `tag.info` descriptions name the tier, not a model (a test forbids model
  and vendor words there), and the form label, F8 legend, preview fact and
  launch banner render the pinned model from the conf via
  `tags/engine.pinned_model` — so a switch shows each engine's Gemini or
  Codex model with no tag.info edit. An init-order trap the switch must
  respect: `tags/engine.py` binds `CONF_FILE = DEFAULT_AI.conf_filename` at
  IMPORT time and imports `DEFAULT_AI` by name, so a switch read from a
  profile AFTER import would keep the old budget filename bound — the labels would
  show Claude's ids on a Gemini launch and `conf_env_args` would ship Claude's
  env keys, with nothing failing loudly. The switch therefore needs call-time
  reads (an `active_ai()` function every consumer calls) or must be settled
  before `launch.tags.engine` is imported.
- **The model-bump command covers only Claude** —
  `agents/_commands/ai_project-update-models.md` reads Anthropic's pages and
  bumps `agents/ai/claude/efforts.tiers`. The Gemini / ChatGPT / Grok
  `efforts.tiers` ids (one a `-preview` id; the CLI's own `pro` alias points
  at a model that died within months) age silently, and nothing launches
  them until an adapter exists. Before any adapter ships, extend the bump to every catalog member
  (each file's header carries the date its ids were verified).
- **Codex's effort ceiling** — the config reference's enum stops at `xhigh`,
  while the models page shows the CLI's own selector offering Max and Ultra
  and the API pages list `max` as a real `reasoning.effort` value for every
  current slug, astra included (verified 2026-09-10, twice). The API half is
  therefore settled; the one remaining unknown is whether Codex's own config
  parser accepts the value — a single yes/no,
  `codex -c model_reasoning_effort=max`, once a binary is at hand. If it is
  accepted, the max tiers (`default`, `thinker`, `researcher`,
  `breakthrough`, `reliable`) move to it; until then `xhigh` is the
  documented top, and doc evidence alone does not move it.
- **The Sonnet rung on Gemini — settled 2026-09-12.** With the Claude side
  on Sonnet 5 (28.44 at medium, 38.36 at max, high unscored), `quick` and
  `poet` keep `gemini-3.5-flash`: its rows (32.98 at HIGH, 33.63 est. at
  MEDIUM) are exact effort matches inside that bracket, it is the newest Flash
  the CLI's own tables know, and the two stronger Flash models now serve the
  rungs above it (3.7 Flash for `reliable`, 3.8 Flash for the top tier). The
  earlier prose argument for 3.8 Flash as the Sonnet-shaped model was answered
  by the index: 3.8 is the top of the Gemini ladder, not its middle. 3.1 Pro
  Preview (30.36, Δ1.9 from Sonnet 5 medium) is not taken — a Pro name under
  Flash tops would mislead. Re-check when AA scores Sonnet 5 at high.
- **The Gemini ladder — re-rung by the index on 2026-09-12 (operator's
  decision, option 1 of the three that stood here).** Found 2026-09-10 while
  building the table: the Fable-rung pin `gemini-3.1-pro-preview` scored 30.36
  (Sonnet level) and the Opus-rung pin `gemini-2.5-pro` 16.66 (below the Haiku
  reference), because the rungs had been assigned on vendor naming (Pro above
  Flash), which the index says stopped being true. Now: Fable rung → 3.8 Flash
  (high) 41.19, the strongest Gemini in the candidate set (still Δ12 short of
  Fable — no Gemini reaches Fable or Astra); Opus rung → 3.7 Flash (high) 39.43,
  the previous Flash generation, so `reliable` keeps "a different, dependable
  model" from the top tier's; Sonnet rung → 3.5 Flash (above); Haiku rung →
  3.1 Flash-Lite at MINIMAL, kept for the tier's cheap-and-fast meaning though
  unrated at that level (every rated Flash-Lite row is at HIGH). THE ONE
  ASSUMPTION, stated in `agents/ai/gemini/efforts.tiers`: 3.8 and 3.7 Flash are
  absent from the CLI's alias and `modelIdResolutions` tables, and that table
  describes itself as rules for resolving REQUESTED names — no entry, no rule,
  so the id is requested verbatim. If the backend refuses it the failure is
  loud at the first request. The probe that verifies it is the first job once
  a Gemini binary is in an image (roadmap step 3), together with a CLI-side
  hazard that predates this change: for any model without a dedicated
  compression alias the CLI falls back to `chat-compression-default`, which
  names the shut-down `gemini-3-pro-preview`. Not chosen: re-runging within
  the ids the CLI knows (a Flash model at the top with `reliable` on the same
  Flash — the ladder would no longer mean what the tier words say) and keeping
  the vendor-naming rungs (a `reliable` tier weaker than `golem`).
- **Sonnet 4.6 → Sonnet 5 on the Claude side — done 2026-09-12** through the
  project's own bump command (`ai_project-update-models.md`: latest Claude API
  ID per tier, tier kept). Anthropic's models overview lists `claude-sonnet-5`
  for the Sonnet tier; its deprecations page still lists `claude-sonnet-4-6`
  as Active (retirement not sooner than 2027-02-17), so this is a currency
  bump, not a forced migration — Artificial Analysis's "newer model" pointer
  from 4.6 to 5 agrees. `quick` and `poet` moved; the index rates Sonnet 5
  (max) 38.36 against 4.6's 30.45, at 2 / 10 USD per 1M tokens against 3 / 15.
  Consequences carried in the same pass: `test_ai`'s unrated pins are now
  `quick`'s Sonnet 5 at high (listed by AA, not yet scored); the mid rungs on
  Gemini and Codex stay by the effort-label mapping (3.5 Flash HIGH / MEDIUM,
  Terra high / medium — Terra at medium 30.42 sits Δ2 above Sonnet 5 medium).
  The other three Claude rungs were already current: `claude-fable-5-1`,
  `claude-opus-5`, and `golem`'s `claude-haiku-4-5` alias, which tracks the
  dated snapshot `claude-haiku-4-5-20251001` the table lists.