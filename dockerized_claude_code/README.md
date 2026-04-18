# Claude Code Agents

A launcher for pre-made Claude Code agents, with common onboarding and authentication, but distinct model, parameters, instructions.

## Requirements

Requires Docker, Docker Compose, and Python 3.

Python requirements:
```bash
pip3 install pick python-dotenv
```

Requires the existence of dir (could be symlink) `/ai_workspace`. You can change the address within the Python file.

## Quick Start

```bash
python3 run.py
```

On the first launch, Claude Code will prompt you to complete OAuth onboarding. This is a one-time process — credentials are stored in `~/.claude-agents/` and shared across all agents.

## Persistent State

All agent state is written to `~/.claude-agents/`:

```
~/.claude-agents/
  .claude.json            # Shared account info
  .credentials.json       # Shared API credentials
  <agent-name>/
    CLAUDE.md             # Copy of the agent's instructions
    <various_files>       # History and management of AI Agent
```

## Custom Commands

Custom slash commands are available to all agents. To use one, type its name during a session:

| Command | Description |
|---------|-------------|
| `/write-summary` | Analyzes the project and writes a structured summary to `.claude_summary` in the workspace. On subsequent runs, updates the existing summary rather than rewriting from scratch. The summary is automatically loaded into future sessions. |

Commands are stored in `custom_commands/` and mounted read-only into every agent's environment.

## Adding an Agent

1. Create `agents/<name>.md` — this becomes the agent's `CLAUDE.md` (its system instructions).
2. Optionally create `agents/<name>.conf` to override environment variables from `agents/default.conf`; example:
   ```bash
   ANTHROPIC_MODEL="claude-sonnet-4-6"
   CLAUDE_CODE_EFFORT_LEVEL=low
   ```
3. Run `python3 run.py` and select it from the menu.
