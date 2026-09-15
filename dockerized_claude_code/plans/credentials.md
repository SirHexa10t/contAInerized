# Credentials and account files per harness — what to mount where

Status 2026-09-14: RESEARCHED (researcher__primary, every fact from a primary
page; one open point settled by a probe here), DESIGN PROPOSED below, awaiting
the operator's decisions. Tracked in `ISSUES.md` ("One credentials pair for
every AI") and the status table of `adding_an_ai.md`.

## Where the launcher stands

One pair under the state dir (`~/.ai-agents/`): `.claude.json` (Claude Code's
account file, mounted at the container HOME) and `.credentials.json` (its
OAuth tokens, mounted read-write into the config root so the CLI refreshes
them in place). Their names come from the Claude Code adapter
(`launch/ai/claude_code.py`: `account_filename`, `credentials_filename`),
bound into `paths.ACCOUNT_FILE` / `paths.CREDENTIALS_FILE` at import; the
solo launch and the cluster launch mount that pair into every container
whatever the instance's AI or harness. Nothing selects a credentials file by
AI or harness.

## What each harness keeps, and where (researched 2026-09-14)

| Harness | Login methods | Auth file(s) | Rewritten at runtime | Relocation var | Notes |
|---|---|---|---|---|---|
| Claude Code | claude.ai / Console OAuth (`/login`), `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `apiKeyHelper`, `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`, 1 year, saved nowhere) | `$CLAUDE_CONFIG_DIR/.credentials.json` (0600; macOS Keychain first) + `.claude.json` (account, trust decisions, MCP servers, UI state — rewritten on every `/config` change and trust approval, 5 backups kept) | yes, both → read-write | `CLAUDE_CONFIG_DIR` (the dir itself; also keys the Keychain entry) | **Probe 2026-09-14 (this container, throwaway HOME, no credentials):** with `CLAUDE_CONFIG_DIR=<dir>` the CLI writes `<dir>/.claude.json`; unset, `$HOME/.claude.json` beside `$HOME/.claude/`. So the account file FOLLOWS the config dir. `--bare` ignores `CLAUDE_CODE_OAUTH_TOKEN`. `hasCompletedOnboarding` is undocumented. |
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
  keys/<ai>.env                 # API keys, one 0600 file per AI: ANTHROPIC_API_KEY=… — injected as -e into whichever harness runs that AI
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

## Decisions for the operator

1. Adopt the two-axis layout above — keys per AI, OAuth per harness?
   (recommended: it is the only shape that fits all seven).
2. `.claude.json` for SOLO instances: keep it shared (status quo, the
   researcher's clobbering warning applies) or make it per instance by
   setting `CLAUDE_CONFIG_DIR` for solo launches the way the cluster already
   does (the probe shows the file follows the dir)? A probe first: does a
   fresh `.claude.json` beside a valid shared `.credentials.json` start
   logged in, or does the account section in `.claude.json` have to be
   seeded from the shared one?
3. Key-first policy where a vendor forbids third-party OAuth (Anthropic in
   any harness but Claude Code) and where OAuth cannot be mounted (Gemini
   CLI in a container)?
4. Probes to run once a binary is in an image (order-of-work step 3):
   OpenCode `XDG_DATA_HOME` for `auth.json`; Grok's `auth.json` directory;
   Codex `CODEX_HOME` pre-creation in the image.

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
