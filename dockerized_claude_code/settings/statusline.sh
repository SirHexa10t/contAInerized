#!/bin/bash
cat > /dev/null  # drain stdin (harness pipes JSON in, we ignore it)
printf '%b' "${AGENT_FULL_NAME:-\033[36m● Claude Code\033[0m}"
