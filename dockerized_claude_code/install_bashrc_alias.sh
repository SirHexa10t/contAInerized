#!/bin/sh
# alias_launcher.sh — install an `ai` alias into $HOME/.bashrc that launches run.py.
#
# Run once: `sh alias_launcher.sh` (or `./alias_launcher.sh` if executable).
# Idempotent-by-refusal: if the alias already exists in .bashrc, the script
# bails rather than duplicate it. POSIX-compliant; fails loudly on every check.

set -eu

ALIAS_NAME="ai"

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

# --- 3. Locate run.py relative to this script -------------------------------

# POSIX-portable: cd to the script's directory then pwd to get an absolute path.
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)" || die "Could not resolve this script's directory."
RUN_PY="$SCRIPT_DIR/run.py"
[ -f "$RUN_PY" ] || die "run.py not found next to this script (expected at $RUN_PY). Place alias_launcher.sh alongside run.py."
[ -r "$RUN_PY" ] || die "run.py exists at $RUN_PY but is not readable."

# Sanity-guard against pathological characters in the path that would corrupt the alias.
case "$RUN_PY" in
    *\'*) die "Path to run.py contains a single quote ($RUN_PY); aliases use single-quoted strings and would be broken. Move the project to a path without single quotes." ;;
esac

# --- 4. Required software checks (mirroring README) -------------------------

command -v docker >/dev/null 2>&1 || die "docker not found in PATH. Install Docker Desktop or Docker Engine (see README)."
docker compose version >/dev/null 2>&1 || die "docker compose v2 plugin not responding. Install or update the Compose plugin (see README)."
command -v python3 >/dev/null 2>&1 || die "python3 not found in PATH. Install Python 3.10+ (see README)."

PY_VERSION_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)' 2>/dev/null || printf 0)"
[ "$PY_VERSION_OK" = "1" ] || die "python3 is older than 3.10 (the launcher uses walrus expressions and structural unpacking). Upgrade Python (see README)."

python3 -c 'import prompt_toolkit, dotenv' >/dev/null 2>&1 \
    || die "Python deps missing: 'prompt_toolkit' and/or 'python-dotenv'. Install them (e.g. 'pip3 install prompt_toolkit python-dotenv'; see README for the recommended venv path)."

# --- 5. Alias must not already exist in .bashrc -----------------------------

# Match: optional leading whitespace, then 'alias <NAME>='.
if grep -E "^[[:space:]]*alias[[:space:]]+$ALIAS_NAME=" "$BASHRC" >/dev/null 2>&1; then
    die "An '$ALIAS_NAME' alias already exists in $BASHRC. Remove it manually first, or change ALIAS_NAME at the top of this script."
fi

# --- 6. Append the alias with two surrounding blank lines on each side ------

ALIAS_LINE="alias $ALIAS_NAME='python3 \"$RUN_PY\" '"

{
    printf '\n\n'
    printf '%s\n' "$ALIAS_LINE"
    printf '\n\n'
} >> "$BASHRC" || die "Failed to append to $BASHRC."

# --- 7. Confirmation message ------------------------------------------------

printf '\n'
printf '%s\n' "Added the following line to $BASHRC:"
printf '\n    %s\n\n' "$ALIAS_LINE"
printf '%s\n' "Open a new shell, or run:  source \"$BASHRC\""
printf '%s\n' "to activate the alias in this session. After that:"
printf '\n    %s            # opens the picker\n' "$ALIAS_NAME"
printf '    %s poet           # skip picker; new instance of poet\n' "$ALIAS_NAME"
printf '    %s poet__myproj   # continue an existing instance\n' "$ALIAS_NAME"
printf '\n'
printf '%s\n' "You may rename or remove the alias in $BASHRC at any time."
