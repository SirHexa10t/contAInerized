# Agent configuration gotchas

## `CLAUDE_CODE_SIMPLE`

**Do not set this.** It causes Claude Code to ignore your credentials — not meant for CLI use.

```bash
CLAUDE_CODE_SIMPLE=1   # ⚠ breaks CLI auth — leave unset
```
