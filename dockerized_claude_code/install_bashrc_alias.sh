#!/bin/sh
# install_bashrc_alias.sh — install the launcher aliases into $HOME/.bashrc:
#
#     ai  ->  run.py             (the interactive launcher: picker + instances)
#     q   ->  quick_question.py  (the quickie one-shot-question tool)
#
# Run once: `sh install_bashrc_alias.sh` (or `./install_bashrc_alias.sh` if
# executable). Per-alias idempotency: an alias already present in .bashrc is
# left untouched and reported as skipped rather than duplicated, so re-running
# (or running after an older version that installed only `ai`) safely tops up
# whatever's missing. POSIX-compliant; fails loudly on every environment check.

set -eu

# --- Failure helper ---------------------------------------------------------

die() {
    printf 'ERROR: %s\n' "$1" >&2
    exit 1
}

# --- 1. $HOME must be set ---------------------------------------------------

[ -n "${HOME:-}" ] || die "\$HOME is not set; cannot locate .bashrc."

# --- 2. .bashrc must exist and be writable ----------------------------------

BASHRC="$HOME/.bashrc"
[ -e "$BASHRC" ] || die "$BASHRC does not exist. Create it first (touch \"$BASHRC\"), then re-run this script."
[ -f "$BASHRC" ] || die "$BASHRC exists but is not a regular file."
[ -w "$BASHRC" ] || die "$BASHRC is not writable by the current user."

# --- 3. Locate the launchers relative to this script ------------------------

# POSIX-portable: cd to the script's directory then pwd to get an absolute path.
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)" || die "Could not resolve this script's directory."
RUN_PY="$SCRIPT_DIR/run.py"
Q_PY="$SCRIPT_DIR/quick_question.py"
[ -f "$RUN_PY" ] || die "run.py not found next to this script (expected at $RUN_PY). Place install_bashrc_alias.sh alongside run.py."
[ -r "$RUN_PY" ] || die "run.py exists at $RUN_PY but is not readable."
[ -f "$Q_PY" ] || die "quick_question.py not found next to this script (expected at $Q_PY). Place install_bashrc_alias.sh alongside quick_question.py."
[ -r "$Q_PY" ] || die "quick_question.py exists at $Q_PY but is not readable."

# Sanity-guard against pathological characters in the path that would corrupt
# the aliases (both derive from SCRIPT_DIR, so one check covers both).
case "$SCRIPT_DIR" in
    *\'*) die "Project path contains a single quote ($SCRIPT_DIR); aliases use single-quoted strings and would be broken. Move the project to a path without single quotes." ;;
esac

# --- 4. Required software checks (mirroring README) -------------------------

command -v docker >/dev/null 2>&1 || die "docker not found in PATH. Install Docker Desktop or Docker Engine (see README)."
docker version >/dev/null 2>&1 || die "docker not responding. Install Docker Engine (see README) and ensure the daemon is running."
command -v python3 >/dev/null 2>&1 || die "python3 not found in PATH. Install Python 3.10+ (see README)."

PY_VERSION_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)' 2>/dev/null || printf 0)"
[ "$PY_VERSION_OK" = "1" ] || die "python3 is older than 3.10 (the launcher uses walrus expressions and structural unpacking). Upgrade Python (see README)."

python3 -c 'import prompt_toolkit, dotenv' >/dev/null 2>&1 \
    || die "Python deps missing: 'prompt_toolkit' and/or 'python-dotenv'. Install them (e.g. 'pip3 install prompt_toolkit python-dotenv'; see README for the recommended venv path)."

# --- 5. Install each alias (skipping any already present) -------------------

ADDED=""     # space-separated names we appended this run
SKIPPED=""   # space-separated names already present, left untouched

# install_alias NAME TARGET_PY — append `alias NAME='python3 "TARGET_PY" '` to
# .bashrc unless a `NAME=` alias already lives there. The trailing space inside
# the quotes lets `NAME arg` forward arg through as a positional. Updates the
# ADDED / SKIPPED accumulators for the closing summary.
install_alias() {
    name="$1"
    target_py="$2"
    # Match: optional leading whitespace, then 'alias <NAME>='.
    if grep -E "^[[:space:]]*alias[[:space:]]+$name=" "$BASHRC" >/dev/null 2>&1; then
        SKIPPED="$SKIPPED $name"
        return
    fi
    alias_line="alias $name='python3 \"$target_py\" '"
    {
        printf '\n\n'
        printf '%s\n' "$alias_line"
        printf '\n\n'
    } >> "$BASHRC" || die "Failed to append the '$name' alias to $BASHRC."
    ADDED="$ADDED $name"
    printf 'Added:  %s\n' "$alias_line"
}

printf '\n'
install_alias "ai" "$RUN_PY"
install_alias "q"  "$Q_PY"

# --- 6. Confirmation summary ------------------------------------------------

if [ -n "$SKIPPED" ]; then
    printf 'Skipped (already present in %s):%s\n' "$BASHRC" "$SKIPPED"
fi

if [ -z "$ADDED" ]; then
    printf '\nNothing to do — both aliases were already present. Remove them from\n'
    printf '%s first if you want this script to re-add them.\n' "$BASHRC"
    exit 0
fi

printf '\n%s\n' "Open a new shell, or run:  source \"$BASHRC\""
printf '%s\n' "to activate the new alias(es) in this session. Usage:"
printf '\n'
printf '    %s\n' "ai                                    # opens the picker"
printf '    %s\n' "ai poet                               # skip picker; new instance of poet"
printf '    %s\n' "ai poet__myproj                       # continue an existing instance"
printf '    %s\n' "q \"why do elephants have big ears?\"   # one-shot question (quote it)"
printf '\n'
printf '%s\n' "You may rename or remove the aliases in $BASHRC at any time."
