#!/bin/bash
cat > /dev/null  # drain stdin (harness pipes JSON in, we ignore it)
name=$(head -n1 /home/claude/.claude/CLAUDE.md 2>/dev/null | sed 's/^#\+ *//')
printf '\033[36m● %s\033[0m' "${name:-Claude Code}"
