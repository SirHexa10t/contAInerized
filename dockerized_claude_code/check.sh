#!/usr/bin/env bash
# The project's quality gate — the single definition of "does this tree pass?".
# CI calls this script (.github/workflows/ci.yml); run it by hand before
# handing work over. Both paths therefore check exactly the same things.
#
# What it runs
# ------------
#   1. tests   python3 -m unittest discover -s launch/tests
#   2. ruff    ruff check .                  (rule set pinned in pyproject.toml)
#   3. mypy    mypy launch/ *.py             (the package + every root entry point)
#
# Usage
# -----
#   bash check.sh        # exit 0 = all three clean; 1 = at least one failed
#
# Deliberately NOT fail-fast, unlike install_dependencies.sh. A half-finished
# install leaves a broken machine, so that script stops at the first error —
# whereas a checker that stops at the first failure hides the other two. Every
# check runs here, and the failures are named together at the end.
#
# It installs nothing. A missing checker counts as a FAILURE rather than a skip:
# a check that did not run is not a check that passed. The install hint is
# printed with the failure. CI installs the tools in its own step before
# calling this script, which keeps "what to check" separate from "what to have".
#
# Not included: `python3 run.py --dry-run`. It opens the interactive picker, and
# prompts for a workspace + session name when the instance doesn't exist yet, so
# it cannot run unattended. Run it by hand against an instance that exists:
#   python3 run.py <instance> --dry-run

set -uo pipefail        # NOT -e — see the fail-fast note above.

# Repo root, resolved from this script's own location so the gate works from
# any directory: `ruff check .` and the *.py glob are both root-relative.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Where `uv tool install` and `pip3 install --user` both put executables.
# Appended, not prepended, so an activated venv still wins. Harmless if absent.
PATH="$PATH:$HOME/.local/bin"

command -v python3 >/dev/null 2>&1 || {
    echo "✗ python3 not found — nothing can run. See README.md ('Host requirements')."
    exit 1
}

# Plain counters rather than an array: `${ARR[@]}` on an empty array trips
# `set -u` on bash 3.2, which is what macOS still ships.
CHECKS=0
FAILURES=0
FAILED_LABELS=""

# run_check <label> <install-hint> <command> [args...]
# Captures the command's output, prints a one-line verdict when it passes, and
# the whole output when it doesn't — so a clean run stays readable and a failure
# withholds nothing.
run_check() {
    local label=$1 hint=$2
    shift 2
    CHECKS=$((CHECKS + 1))
    printf '\n▸ %-5s %s\n' "$label" "$*"

    if ! command -v "$1" >/dev/null 2>&1; then
        echo "  ✗ '$1' not found — $hint"
        FAILURES=$((FAILURES + 1))
        FAILED_LABELS="$FAILED_LABELS $label(missing)"
        return
    fi

    local captured
    captured="$(mktemp)"
    if "$@" >"$captured" 2>&1; then
        # The summary lines each tool ends on; anything else is noise on a pass.
        grep -E '^(Ran |OK|All checks passed|Success:)' "$captured" | sed 's/^/  ✓ /' \
            || echo "  ✓ clean"
    else
        sed 's/^/  /' "$captured"
        FAILURES=$((FAILURES + 1))
        FAILED_LABELS="$FAILED_LABELS $label"
    fi
    rm -f "$captured"
}

echo "Quality gate — $SCRIPT_DIR"
echo "  $(python3 -V) at $(command -v python3)"

run_check tests "it ships with python3" \
    python3 -m unittest discover -s launch/tests

run_check ruff "install with: uv tool install ruff" \
    ruff check .

# The glob expands here, in the repo root, so a new root entry point is
# type-checked the day it lands instead of the day someone remembers to add it.
run_check mypy "install with: uv tool install mypy" \
    mypy launch/ *.py

echo
if [[ "$FAILURES" -gt 0 ]]; then
    echo "✗ gate FAILED — $FAILURES of $CHECKS:$FAILED_LABELS"
    exit 1
fi
echo "✓ gate passed — $CHECKS/$CHECKS clean"
