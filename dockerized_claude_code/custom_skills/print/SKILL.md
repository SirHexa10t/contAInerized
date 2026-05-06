---
description: Pretty-print data with type-aware formatting via rich-cli. Use whenever displaying file contents, JSON, CSV, markdown, source code, or other structured data — whether the data lives in a file or you have it inline (a string, generated output, content from earlier in the conversation).
argument-hint: "<path-or-hint>"
---

Use `rich-cli` (the `rich` command) to render the data with formatting matching its type.

**Always pass `--force-terminal`** (or `-F`). Rich auto-detects "stdout isn't a real TTY" when invoked through the Bash tool and silently disables ANSI colour codes; `--force-terminal` overrides that, so the rendered output the user sees in their terminal actually has colour. Omit it and you'll get plain ASCII boxes.

**File path argument:**

```bash
rich -m -F PATH         # markdown
rich --json -F PATH     # JSON
rich --csv -F PATH      # CSV → table
rich -F PATH            # auto-detect by extension / content (works for source code too)
```

**Inline content** (string in your context, command output, data you generated — no file involved):

```bash
echo "$DATA" | rich --json -F -            # `-` reads from stdin
printf '%s' "$DATA" | rich -m -F -

# Source code — specify the language with --lex (any Pygments lexer name):
printf '%s' "$CODE" | rich --syntax --lex python -F -        # python | python3
printf '%s' "$CODE" | rich --syntax --lex c -F -             # c | cpp
printf '%s' "$CODE" | rich --syntax --lex rust -F -          # rust
printf '%s' "$CODE" | rich --syntax --lex javascript -F -    # javascript | js | typescript | ts
printf '%s' "$CODE" | rich --syntax --lex bash -F -          # bash | sh

# Or buffer to a temp file when the content is large or multi-line:
printf '%s' "$DATA" > /tmp/print-buffer && rich --json -F /tmp/print-buffer
```

Pick the mode by:

- **File extension** when a path is given (`*.json` → `--json`, `*.md` → `-m`, `*.csv` → `--csv`; source extensions like `*.py`/`*.c`/`*.rs`/`*.js` → auto-syntax-highlight).
- **Content shape** when inline (`{` / `[` → JSON; `# header` → markdown; comma-columned → CSV; recognisable code → `--syntax --lex <lang>`; otherwise plain).
- **An explicit hint in `$ARGUMENTS`** (e.g. *"as JSON"*, *"as a table"*, *"as Python code"*) when the user gave one.

Show the rendered output. Don't paraphrase, summarise, or wrap it in framing.
