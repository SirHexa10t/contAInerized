# Project-local Claude Code files

Claude Code natively scans the workspace for instructions, slash commands,
and skills. Commit them under `.claude/` and they reach every clone of the
repo.

## What you can drop where

| Surface | Path | Refresh | Personal vs project |
|---|---|---|---|
| Project instructions | `<repo>/CLAUDE.md` | session start | both load (aggregated) |
| Slash commands | `<repo>/.claude/commands/<name>.md` | live (file-watcher) | different names: both load; same name: personal wins |
| Skills | `<repo>/.claude/skills/<name>/SKILL.md` | live (file-watcher) | different names: both load; same name: personal wins |

Full rank order (when it applies): `enterprise > personal > project`. Within
one level, a skill outranks a same-named command. Skills and commands share
the same machinery — both create `/<name>` and accept the same frontmatter.

## Watcher gotcha

The file-watcher needs `.claude/skills/` and `.claude/commands/` to *exist*
at session start. If the directory itself is created mid-session, new entries
inside won't appear until you relaunch. Run
`mkdir -p .claude/skills .claude/commands` before launching a fresh project.

## This launcher's quirk

The launcher bind-mounts its own `custom_commands/` and `custom_skills/<name>/`
at the *personal* level inside the container, so launcher-bundled entries
outrank workspace-local ones with the same name. Rename the workspace entry
if you want it to win.

## Further reading

- [Skills + slash commands](https://code.claude.com/docs/en/skills)
- [Memory (CLAUDE.md)](https://code.claude.com/docs/en/memory)
- [Built-in commands reference](https://code.claude.com/docs/en/commands)
- [Permissions](https://code.claude.com/docs/en/permissions)
