# Inserting newlines in the Claude Code prompt

When typing into the prompt, you sometimes want to insert a newline without submitting. The intuitive choice — `Shift+Enter` — doesn't always work, because most terminal emulators send the same byte (`\r`) for `Enter` and `Shift+Enter` by default. Without a distinct byte, no application running inside the terminal can tell them apart.

Claude Code provides a few escape mechanisms that work everywhere, plus a configurable `keybindings.json` that catches `Shift+Enter` *if* the terminal already distinguishes it.

## Always-works keys

These insert a newline without submitting in every terminal, no setup required:

| Key | Effect |
|---|---|
| `\<Enter>` | backslash + Enter — newline (default escape) |
| `Ctrl+J` | newline (Claude Code's default binding) |
| `Alt+Enter` | newline (works in most terminals because `Alt` prefixes the byte with `\e`) |

## Terminal compatibility for `Shift+Enter`

| Terminal | Shift+Enter support |
|---|---|
| iTerm2 (macOS) | native |
| WezTerm | native |
| Ghostty | native |
| Kitty | native |
| Warp | native |
| Alacritty | needs config — auto-set via `/terminal-setup` |
| VS Code integrated terminal | needs config — auto-set via `/terminal-setup` |
| Cursor / Windsurf / Zed | needs config — auto-set via `/terminal-setup` |
| GNOME Terminal | not supported (VTE doesn't allow custom key → sequence mappings) |
| Konsole, Terminal.app, Windows Console, plain xterm | not supported by default; needs manual config (terminal-specific) |

For "needs config" terminals, run `/terminal-setup` once from a Claude Code session **on the host** (not from inside Docker — the dockerised TTY reports as `xterm-256color` and the setup command can't tell what the host terminal actually is). Setup writes to that terminal's profile/config and persists; future Claude Code sessions in that terminal — including dockerised ones — inherit the new behaviour.

For "not supported" terminals, your only options are: switch to a different terminal (recommendation: Kitty), or stick with the always-works keys above.

## `settings/keybindings.json` — role and limitations

The project's `settings/keybindings.json` is bind-mounted into every agent at `/home/claude/.claude/keybindings.json`. It maps **logical keys** (the named events Claude Code receives) to **named actions** (`chat:submit`, `chat:newline`, `chat:cancel`, `chat:clearInput`, `chat:stash`, etc.).

Edits to `settings/keybindings.json` propagate **live** — Claude Code watches the file and picks up changes mid-session, no relaunch needed.

### What `keybindings.json` *can* do

- Map a logical key to an existing action — e.g. `"shift+enter": "chat:newline"` adds Shift+Enter as newline alongside the defaults.
- Override a default binding — e.g. swap what `Ctrl+L` does.
- Add chord bindings (`Ctrl+K Ctrl+S`-style) — see Claude Code's keybindings reference for the schema.

### What `keybindings.json` *can't* do

- **Make a terminal distinguish keys it doesn't already distinguish.** If your terminal sends the same byte for `Enter` and `Shift+Enter`, no entry in `keybindings.json` can save you — Claude Code only ever sees the one byte and has no event to bind to. The fix has to happen at the terminal layer (auto via `/terminal-setup` or manually in the terminal's config).
- **Override terminal-multiplexer interception.** If you run inside `tmux` or `screen` and they swallow a key combo, `keybindings.json` never sees those events either. Fix the multiplexer's config first.
- **Send raw escape sequences.** It binds keys to *named Claude Code actions*, not to byte sequences. To make a terminal *emit* a sequence Claude Code recognises, that's the terminal's job, not Claude Code's.

## Layered debugging

If `Shift+Enter` isn't behaving the way you expect, check each layer in order:

1. **Terminal layer** — does your terminal send a distinct byte for `Shift+Enter`? Outside Claude Code, in any shell, run `cat`, type `text`, press `Shift+Enter`, type `text2`, press `Enter`, then `Ctrl+D`. If `cat` echoed both texts on a single line, your terminal is conflating Shift+Enter with Enter. If on two lines, the terminal IS distinguishing them and the layer above is responsible.
2. **Multiplexer layer** — if you're inside `tmux`/`screen`, the multiplexer may swallow or rewrite the key. Test with the same `cat` recipe but outside the multiplexer.
3. **Claude Code layer** — confirm `~/.claude/keybindings.json` contains a `shift+enter` entry. Edit it; the change is live.

## Quick reference

To enable Shift+Enter end-to-end:

1. Use a terminal that distinguishes Shift+Enter (see compatibility table).
2. For "needs config" terminals, run `/terminal-setup` on the host once.
3. Make sure `settings/keybindings.json` includes `"shift+enter": "chat:newline"`.
4. Don't run inside a multiplexer that swallows the key.

If any of those steps is impractical, fall back to the always-works keys (`\<Enter>`, `Ctrl+J`, `Alt+Enter`) — they work everywhere, no setup, no caveats.
