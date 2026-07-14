---
description: Save the assistant's previous reply verbatim into a markdown file the user names. Usage — /file-message <filename>.md
argument-hint: <filename>.md
---

The user invoked `/file-message $ARGUMENTS`.

Save the assistant reply that immediately precedes this command — the last message you sent before the user ran `/file-message` — to a file, verbatim.

## Target file

- `$ARGUMENTS` is the filename. If it has no extension, append `.md`. Resolve relative paths against the current working directory.
- If `$ARGUMENTS` is empty, ask the user for a filename — one short question; don't invent a name.
- If the file already exists, don't overwrite silently: state what's there (path + size) and ask before replacing.

## Content rules

- Byte-for-byte the markdown of that reply: no added title, no wrapper code fence, no commentary, no metadata header, no trailing sign-off.
- Only the message text — tool calls, tool results, and thinking blocks never go in.
- If the immediately-preceding turn contained no prose (e.g. it was all tool activity), use the most recent reply that did, and say which one you picked.
- If the reply contains secrets or personal identifiers (tokens, emails, credential paths), stop and confirm with the user before persisting them to disk.

## After writing

Reply with one line: the saved path and its line count. Nothing else.
