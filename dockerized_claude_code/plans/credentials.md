# Credentials and account files per harness — what to mount where

Status: RESEARCHED 2026-09-14 (researcher__primary, every fact from a primary
page; one open point settled by a probe here), DESIGN APPROVED and
IMPLEMENTED 2026-09-15 (gate `credentials-layout`, four riders folded in —
see "What landed"). Residue in `ISSUES.md` ("Credentials: the residue") and
the status table of `adding_an_ai.md`.

## What landed (2026-09-15)

- `~/.ai-agents/credentials/keys/<ai>.env` — the API key axis. Passed to
  docker as `--env-file` (never `-e KEY=value`: that lands in `ps` and in
  `--dry-run` output). Docker's grammar is VERBATIM, not dotenv: `NAME=value`,
  quotes kept, `#` only at line start, no `export`, a bare `NAME` would import
  the host's variable — so `file_access.key_file_problems` (the one reader of
  the file; it names variables and lines, never a value) refuses those at
  launch (`docker_config.preflight_key_file`, which also fixes the mode to
  600) and in the audit, and requires exactly the AI's `key_env` variable
  (every other line lands in the container's environment and could override
  a launcher setting). Codex CLI is the exception that stays open: it takes a
  key through `codex login --with-api-key`, which writes `auth.json` — an
  adapter concern, not a line in this file.
- `~/.ai-agents/credentials/<harness>/<file>` — the OAuth axis. The adapter's
  `auth_files` (`launch/ai/adapter.py` `AuthFile`: name, role, anchor, mode,
  blank, scope) replace the flat filename pair; `paths.auth_file_mounts` turns
  them into mounts per launch shape (solo: default config root, `account`
  anchor at HOME; cluster member: its relocated dir takes both);
  `file_access.ensure_auth_files` creates missing ones as PRIVATE blanks
  (0600 — the CLI refreshes tokens in place, so the file keeps the mode it
  was born with; `write_text` alone gave 0644 under a default umask).
  `paths.py` no longer binds the pair at import. The cluster's free shell pane
  also gets the default harness's files at the default location, for a human
  running the CLI by hand there.
- Migration (`tags/migrations.relocate_credentials`, before any blank is
  touched, on both entry paths): the old pair at the state root moves under
  `credentials/claude-code/` per file, atomically over nothing or a blank,
  never over a real token (both present → hands off and says so), tolerating
  a concurrent launcher, and chmods the moved file 600.
- Precedence notice (`claude_code_config.credentials_notice`, printed before
  docker runs): a key file beside a stored login — the key wins (Claude Code
  reads `ANTHROPIC_API_KEY` at precedence 3, its `/login` session at 7, and
  asks once whether to use the key, remembering the answer in
  `.claude.json`); no key and no login yet — the CLI will ask inside the
  container, and the login is kept.
- Audit: `oauth` findings per adapted harness file (missing, empty, invalid,
  mode), `key_file` findings per AI key file (mode, grammar, stray
  variables).
- Secrets in the environment, said plainly: a key passed by `--env-file`
  sits in the container's environment — readable by every process, every
  cluster member (one `--env-file` per distinct member AI, so each member
  sees every AI's key), every hook, and by `docker inspect` from any
  container holding the docker socket (`{dood}`). Inherent to key auth for
  CLIs that read env; prefer the OAuth axis where the vendor allows it.

## The incident, same day, and what it changed

Opening an existing cluster through `cluster.py launch` mounted the OLD
location's blanks — its verb called the launcher without the migrations the
other entries ran — while the real pair stayed at the state root; the
members' not-logged-in Claude Code wrote startup state into the blank
`.claude.json`, and the next solo launch's migration refused to overwrite a
non-blank file, moved only `.credentials.json`, and asked for a login again.
Three entry points had three spellings of "start up" and two of "stage the
credentials". Now: `launch/startup.open_launcher()` is every entry's first
call (migrations, then the tree — `run.py`, `q`, `cluster.py`, `cowork.py`;
the audit is the read-only exception); `docker_config.stage_credentials` is the
one credentials staging (solo, quickie, each cluster member);
`AuthFile.login_key` + `file_access.login_state` is the one reading of a
login file (`claudeAiOauth`, `oauthAccount` — the CLI's private shape,
pinned in `test_ai`): `absent` (nothing, or the launcher's blank) and `none`
(JSON without the key — the incident's file) are replaceable, `recorded` is
never overwritten, `unreadable` (empty or partial — a CLI refreshing the
token in place) is never touched. The migration keeps what it displaces —
`<name>.replaced.bak` beside a replaced no-login file, `<name>.superseded.bak`
at the state root for an old login outranked by the one at the new place —
so a misjudged file costs a copy, never a login, and nothing nags on the
next launch. The launch notice counts a login only when EVERY login-keyed
file records it (the split the incident left is where the CLI prompts). The
audit, which never migrates, reports a login file still at the state root or
the retired `~/.claude-agents` dir as `unmigrated` — the fix being one
launch. `paths.base_mounts()` / `paths.settings_mount()` are the shared
spellings of the mounts both shapes need. The cowork hub, host-side and
reading the state dir like the launches, opens the same way.

Same day, the two orchestrators themselves stopped diverging
(`launch/staging.py`): `stage_instance` is the one per-agent staging for a
solo instance, a quickie and each cluster member (installs, login files, key
preflight, the read-only settings AND commands shadows — a member's commands
dir had been writable), `docker_config.add_docker_mount` is the one mount
accumulator for both shapes (pairs, so one login file mounts into N member
dirs; one collision rule), `container_env.set_container_env` is the container
half every shape stages (the cluster's image had been built from Dockerfile
defaults with no BASH_ENV) and `set_instance_env` the one-agent half, and the
cluster mounts the operator's optional creds and runs the tag handlers over
its union probe like a solo launch. What stays cluster-shaped, deliberately:
a member's identity (status line, config dir, session name) in its own pane's
env, and the generated entrypoint script.

## Where the launcher stood (until 2026-09-15)

One pair under the state dir: `.claude.json` (Claude Code's account file,
mounted at the container HOME) and `.credentials.json` (its OAuth tokens,
mounted read-write into the config root). Their names came from the Claude
Code adapter, bound into `paths.ACCOUNT_FILE` / `paths.CREDENTIALS_FILE` at
import; every launch mounted that pair whatever the instance's AI or harness.

## What each harness keeps, and where (researched 2026-09-14)

| Harness | Login methods | Auth file(s) | Rewritten at runtime | Relocation var | Notes |
|---|---|---|---|---|---|
| Claude Code | claude.ai / Console OAuth (`/login`), `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `apiKeyHelper`, `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`, 1 year, saved nowhere). Precedence: the key (3) beats the `/login` session (7) — with a one-time approve prompt remembered in `.claude.json` | `$CLAUDE_CONFIG_DIR/.credentials.json` (0600; macOS Keychain first) + `.claude.json` (account, trust decisions, MCP servers, UI state — rewritten on every `/config` change and trust approval, 5 backups kept) | yes, both → read-write | `CLAUDE_CONFIG_DIR` (the dir itself; also keys the Keychain entry) | **Probe 2026-09-14 (this container, Claude Code 2.1.266, throwaway HOME, no credentials):** with `CLAUDE_CONFIG_DIR=<dir>` the CLI writes `<dir>/.claude.json`; unset, `$HOME/.claude.json` beside `$HOME/.claude/`. So the account file FOLLOWS the config dir. `--bare` ignores `CLAUDE_CODE_OAUTH_TOKEN`. `hasCompletedOnboarding` is undocumented. |
| Gemini CLI | Google OAuth (browser), `GEMINI_API_KEY`, Vertex ADC / `GOOGLE_APPLICATION_CREDENTIALS` | OS keychain (service `gemini-cli-oauth`) first; file fallback `.gemini/gemini-credentials.json` **AES-256-GCM, key salted with `hostname-username-gemini-cli`** → a host-written file is undecryptable in a container with another hostname or user; `.gemini/google_accounts.json`; `.gemini/.env` auto-loaded (project, then home) | yes | `GEMINI_CLI_HOME` — the PARENT: the CLI creates `.gemini/` inside it | `GEMINI_FORCE_FILE_STORAGE=true` skips the keyring. Legacy `oauth_creds.json` is migrated once and deleted. OAuth cannot be mounted from the host; an API key in `.gemini/.env` can. |
| Codex CLI | ChatGPT sign-in (subscription, browser) OR API key (`codex login --with-api-key`, `--with-access-token`); Codex cloud needs ChatGPT | `$CODEX_HOME/auth.json`, **plaintext**, auth only (settings in `config.toml`); or the OS store | yes (token refresh before expiry) → read-write | `CODEX_HOME` (default `~/.codex`; **must already exist**), `CODEX_SQLITE_HOME` | `cli_auth_credentials_store = "file" \| "keyring" \| "auto" \| "ephemeral"` — set `file` or the mount may be bypassed. `CODEX_API_KEY` for a non-interactive process. CLI and IDE share one login. |
| Grok Build | browser OIDC (`grok login`), **device code (`grok login --device-auth`) — the one first-class headless login**, external `auth_provider_command`, `XAI_API_KEY` / `model.api_key` | `auth.json` (dir never stated — `$GROK_HOME/auth.json` inferred, unverified); `trusted_folders.toml`; `mcp_credentials.json` | yes for the three OAuth kinds | `GROK_HOME` (default `~/.grok`) | Precedence `model.api_key` > `model.env_key` > session token > `XAI_API_KEY`. Reads Claude Code's `managed-settings.json` but not its `disableBypassPermissionsMode`. |
| OpenCode | `opencode auth login` / `/connect` per provider: OAuth where offered (ChatGPT Plus/Pro yes; SuperGrok device code yes; **Claude Pro/Max — "Anthropic explicitly prohibits this", plugins unbundled in 1.3.0**), else API key | **`~/.local/share/opencode/auth.json` — one file, every provider, keyed by the models.dev id** (`anthropic`, `openai`, `google`, `xai`) | yes | none for `auth.json` — only `OPENCODE_CONFIG` / `OPENCODE_CONFIG_DIR` / `OPENCODE_CONFIG_CONTENT` for config | Isolation needs a `$HOME` override; whether `XDG_DATA_HOME` moves `auth.json` is undocumented — a probe once a binary is in an image. Env fallback: the vendor var names (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` / `GOOGLE_API_KEY`, `XAI_API_KEY`). |
| OpenClaw | `openclaw onboard` (`--auth-choice apiKey` non-interactive); provider OAuth (SuperGrok, ChatGPT) or keys | **SQLite**: `~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite` — no credential JSON; `auth.profiles` in config is routing metadata only; legacy JSON files import only via `openclaw doctor --fix`, the runtime fails closed | n/a (database) | `OPENCLAW_STATE_DIR` (whole root incl. the DB), `OPENCLAW_CONFIG_PATH`; `--profile <name>` → `~/.openclaw-<name>` | Mountable path: `~/.openclaw/.env` (how its daemon gets provider keys). Its own container tests mount `~/.codex/auth.json`, `~/.codex/config.toml`, `.claude.json`, `~/.claude/.credentials.json`, `settings.json`, `settings.local.json` — independent corroboration of the Claude Code and Codex rows. Warns: two harnesses refreshing one OAuth grant fight; the loser is logged out. |
| Hermes Agent | `hermes setup` (wizard / `--portal`), `hermes config set`; provider keys; xAI Grok OAuth (SuperGrok / Premium+) | **`$HERMES_HOME/.env` for secrets (one var per provider — many AIs in one file), `config.yaml` for settings**; `auth.json` for platform OAuth (Discord …), rewritten → read-write | `.env` no; `auth.json` yes | `HERMES_HOME`, `HERMES_PROFILE` (`hermes -p <name>`), `HERMES_CONFIG`, `HERMES_ENV` | Vendor's own recipe: `docker run -v ~/.hermes:/opt/data -e ANTHROPIC_API_KEY=…`; a `-e` flag overrides `.env`. `providers.<id>.key_cmd` = an `apiKeyHelper`. |

Subscription use is vendor-gated: OpenAI permits ChatGPT sign-in in Codex and
OpenCode; xAI permits SuperGrok OAuth in Grok Build, OpenCode and Hermes;
Anthropic permits Claude Pro/Max only in Claude Code (OpenCode's
characterisation, acted on by unbundling — no first-party page says it in
those words; the consumer terms forbid sharing credentials generally).

## The two axes (the rule that falls out)

- An **API key** is a property of the AI (its vendor), not of the harness:
  every harness reads the vendor's variable (`ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY` — Codex: `CODEX_API_KEY` for a non-interactive process —,
  `GEMINI_API_KEY` / `GOOGLE_API_KEY`, `XAI_API_KEY`). It is static, shareable
  across instances and harnesses, portable across hosts, and involves no
  keyring, no hostname binding, no refresh-write and no onboarding prompt. It
  is the only path that works uniformly across all seven.
- An **OAuth grant** is a property of the harness (and of the vendor within a
  multi-provider harness): refreshed in place (read-write mount), sometimes
  keyring-first (Codex, Gemini — a knob forces the file), once
  hostname-bound (Gemini — unmountable), once a database (OpenClaw), never to
  be shared between two harnesses (the refresh race). Its point is a
  subscription, and vendors gate which harness may use theirs.

Consequences for what must be per instance versus shared:

| Per instance | Shared, read-only | Shared, read-write |
|---|---|---|
| `.claude.json` (mixes account with trust, MCP, UI state; rewritten constantly) — today SHARED by every solo instance | `settings.json` / `settings.local.json`; Codex, Grok, OpenCode, Hermes settings files; any `.env` holding only keys | `.credentials.json`, Codex `auth.json`, Grok `auth.json`, Hermes `auth.json` — a read-only mount breaks the session when the token expires, and it looks like a hang |
| Gemini `gemini-credentials.json` (hostname-bound, refreshed) | | |
| OpenClaw's SQLite (per agent by construction — `--profile`); Hermes profile dirs | | |
| every `sessions/`, `projects/`, transcript dir | | |

## Proposed layout (for the operator to decide)

```
~/.ai-agents/credentials/
  keys/<ai>.env                 # API keys, one 0600 file per AI: ANTHROPIC_API_KEY=… — passed as --env-file to whichever harness runs that AI
  <harness>/                    # OAuth state, one dir per harness, mounted where that CLI keeps it (read-write):
    claude-code/.credentials.json   → $CLAUDE_CONFIG_DIR/.credentials.json
    codex-cli/auth.json             → $CODEX_HOME/auth.json          (+ knobs: cli_auth_credentials_store = "file")
    grok-build/auth.json            → $GROK_HOME/auth.json           (location a probe; device-code login works headless)
    opencode/auth.json              → ~/.local/share/opencode/auth.json  (HOME override; XDG_DATA_HOME a probe)
    hermes/auth.json                → $HERMES_HOME/auth.json         (platform OAuth only; provider keys from keys/)
    gemini-cli/                     — none mountable (hostname-bound); keys only, or a login inside the container per instance
    openclaw/                       — none (SQLite in the state root); keys via .env
```

Shape in code: the `Adapter` record's flat `account_filename` /
`credentials_filename` pair fits only the single-vendor harnesses. Replace it
with `auth_files: tuple[AuthFile, ...]` — host name under
`credentials/<harness>/`, container path (a template over the harness's
relocation variable), mode (rw / ro), scope (shared / per instance) — and
give each AI's `tag.info` a `key_env` (the vendor's variable); a harness that
reads another name (Codex's `CODEX_API_KEY`) maps it in its `knobs.mapping`
under an `[api_key]` purpose. `paths.py` stops binding the Claude pair at
import (the last import-time residue): `ACCOUNT_FILE` / `CREDENTIALS_FILE`
become functions of the instance's harness. The audit checks, per harness in
use, that each expected file exists and parses, and that `keys/*.env` are
0600. The migration moves today's `.claude.json` + `.credentials.json` into
`credentials/claude-code/` on first launch.

First launch of a harness with neither a key nor an OAuth blob: as today for
Claude Code — the container starts, the user logs in inside it, and the files
persist under `credentials/<harness>/` for every later launch. Gemini CLI
cannot do that portably: recommend the key.

## Decisions (operator, 2026-09-15: "sounds fine") and the residue

1. The two-axis layout — DONE as above.
2. `.claude.json` for SOLO instances stays SHARED (status quo); `AuthFile.scope`
   carries the `instance` value for the day it changes, and
   `auth_file_mounts` refuses it until built. A probe first: does a fresh
   `.claude.json` beside a valid shared `.credentials.json` start logged in?
   Related, pre-existing and now structural: N cluster members refresh ONE
   read-write `.credentials.json` (and one `.claude.json`) — the same refresh
   race the design forbids across harnesses. Residue.
3. Key-first where a vendor forbids third-party OAuth or the file cannot be
   mounted: the launcher does not enforce exclusivity per launch; it states
   the precedence in the README and prints the notice. Residue: a policy knob
   if the notice proves insufficient.
4. Probes once a binary is in an image (order-of-work step 3): OpenCode
   `XDG_DATA_HOME` for `auth.json`; Grok's `auth.json` directory; Codex
   `CODEX_HOME` pre-creation and `cli_auth_credentials_store = "file"`;
   Codex's key-through-login (`codex login --with-api-key`).

## Sources

Claude Code IAM and directory references (docs.claude.com: iam,
claude-directory, env-vars); gemini-cli source `fileKeychain.ts`,
`oauth-credential-storage.ts`, `keychainService.ts`, `paths.ts`, and its
docs (authentication.mdx, configuration.md); Codex `learn.chatgpt.com/docs/auth`,
`config-reference`, `environment-variables`; xAI `docs.x.ai/build/enterprise`,
`settings/reference`; OpenCode `opencode.ai/docs/providers`, `/config`, `/cli`;
OpenClaw `docs.openclaw.ai/llms-full.txt`; Hermes
`hermes-agent.nousresearch.com/docs/llms-full.txt`; models.dev `api.json`;
Anthropic consumer terms. The probe: this container, Claude Code 2.1.266.
