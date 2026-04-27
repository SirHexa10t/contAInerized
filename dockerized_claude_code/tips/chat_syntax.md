# Chat syntax & shortcuts

Reference: [Claude Code commands](https://code.claude.com/docs/en/overview).

## Inserting a newline without submitting

- **Backslash + Enter** — type `\` then press Enter; the newline is inserted, the message is not sent.
- **Shift+Enter** (or Option+Enter on macOS / Alt+Enter on Linux/Windows) — inserts a newline directly. Shift+Enter usually requires running `/terminal-setup` once so Claude Code can register the binding with your terminal.

## Inline prefixes

- `!` — bash mode
- `/` — commands
- `@` — file paths
- `&` — background
- `/btw` — side question

## Keyboard shortcuts

- **Esc Esc** (double-tap) — clear input
- **Shift + Tab** — auto-accept edits
- **Ctrl + O** — verbose output
- **Ctrl + T** — toggle tasks
- **`\` + Enter** — newline
- **Ctrl + Shift + -** — undo
- **Ctrl + Z** — suspend
- **Ctrl + V** — paste images
- **Meta + P** — switch model
- **Meta + O** — toggle fast mode
- **Ctrl + S** — stash prompt
- **Ctrl + G** — edit in `$EDITOR`

## Built-in slash commands

- `/add-dir` — add a new working directory
- `/agents` — manage agent configurations
- `/branch` — create a branch of the current conversation at this point
- `/btw` — ask a quick side question without interrupting the main conversation
- `/chrome` — Claude in Chrome (Beta) settings
- `/clear` — clear conversation history and free up context
- `/color` — set the prompt bar color for this session
- `/compact` — clear conversation history but keep a summary in context. Optional: `/compact [instructions for summarization]`
- `/config` — open config panel
- `/context` — visualize current context usage as a colored grid
- `/copy` — copy Claude's last response to clipboard (or `/copy N` for the Nth-latest)
- `/cost` — show the total cost and duration of the current session
- `/diff` — view uncommitted changes and per-turn diffs
- `/doctor` — diagnose and verify your Claude Code installation and settings
- `/effort` — set effort level for model usage
- `/exit` — exit the REPL
- `/export` — export the current conversation to a file or clipboard
- `/fast` — toggle fast mode (Opus 4.6 only)
- `/help` — show help and available commands
- `/hooks` — view hook configurations for tool events
- `/ide` — manage IDE integrations and show status
- `/init` — initialize a new `CLAUDE.md` file with codebase documentation
- `/insights` — generate a report analyzing your Claude Code sessions
- `/install-github-app` — set up Claude GitHub Actions for a repository
- `/install-slack-app` — install the Claude Slack app
- `/login` — switch Anthropic accounts
- `/logout` — sign out from your Anthropic account
- `/mcp` — manage MCP servers
- `/memory` — edit Claude memory files
- `/mobile` — show QR code to download the Claude mobile app
- `/model` — set the AI model for Claude Code (currently Opus 4.6)
- `/permissions` — manage allow & deny tool permission rules
- `/plan` — enable plan mode or view the current session plan
- `/plugin` — manage Claude Code plugins
- `/pr-comments` — get comments from a GitHub pull request
- `/release-notes` — view release notes
- `/reload-plugins` — activate pending plugin changes in the current session
- `/rename` — rename the current conversation
- `/resume` — resume a previous conversation
- `/review` — review a pull request
- `/rewind` — restore the code and/or conversation to a previous point
- `/sandbox` — ⚠ sandbox disabled (Enter to configure)
- `/security-review` — complete a security review of the pending changes on the current branch
- `/skills` — list available skills
- `/stats` — show your Claude Code usage statistics and activity
- `/status` — show Claude Code status (version, model, account, API connectivity, tool statuses)
- `/statusline` — set up Claude Code's status-line UI
- `/stickers` — order Claude Code stickers
- `/tasks` — list and manage background tasks
- `/terminal-setup` — install Shift+Enter key binding for newlines
- `/theme` — change the theme
- `/upgrade` — upgrade to Max for higher rate limits and more Opus
- `/usage` — show plan usage limits
- `/vim` — toggle between Vim and Normal editing modes

## Custom / bundled slash commands

- `/batch` — research and plan a large-scale change, then execute it in parallel across 5–30 isolated worktree agents that each open a PR. *(bundled)*
- `/claude-api` — build apps with the Claude API or Anthropic SDK.
- `/debug` — enable debug logging for this session and help diagnose issues. *(bundled)*
- `/loop` — run a prompt or slash command on a recurring interval (e.g. `/loop 5m /foo`, defaults to 10m). *(bundled)*
- `/simplify` — review changed code for reuse, quality, and efficiency, then fix any issues found. *(bundled)*
- `/update-config` — configure the Claude Code harness via `settings.json`. Automated behaviors ("from now on when X", "each time X", "whenever X", "before/after X") require hooks configured in `settings.json` — the harness executes them, not Claude.
