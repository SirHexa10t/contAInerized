#!/bin/sh
# install_rc_alias.sh — install the launcher aliases into your shell rc(s):
#
#     ai  ->  run.py             (the interactive launcher: picker + instances)
#     q   ->  quick_question.py  (the quickie one-shot-question tool)
#
# Both aliases invoke the launcher's own venv python (~/pydev, created by
# install_dependencies.sh) by absolute path, so they work with no activation.
# They're written into every shell rc that exists among ~/.bashrc and ~/.zshrc
# (macOS defaults to zsh); if neither exists, the one matching your login shell
# is created.
#
# Run once: `sh install_rc_alias.sh`. Per-(alias, file) idempotency: an
# alias already present in a given rc is left untouched and reported as skipped,
# so re-running (or running after an older version) safely tops up whatever's
# missing. POSIX-compliant; fails loudly on every environment check.

set -eu

MIN_DOCKER="20.10"                 # engine/client floor — older Docker has bitten some setups
PYDEV_PY="$HOME/pydev/bin/python3"  # the venv the aliases call; install_dependencies.sh builds it

# --- Failure helper ---------------------------------------------------------

die() {
    printf 'ERROR: %s\n' "$1" >&2
    exit 1
}

# --- 1. $HOME must be set ---------------------------------------------------

[ -n "${HOME:-}" ] || die "\$HOME is not set; cannot locate your shell rc files."

# --- 2. The pydev venv must exist (the aliases point straight at it) --------

[ -x "$PYDEV_PY" ] || die "The launcher venv is missing ($PYDEV_PY). Run install_dependencies.sh first (it creates ~/pydev), then re-run this script."

# --- 3. Locate the launchers relative to this script ------------------------

# POSIX-portable: cd to the script's directory then pwd to get an absolute path.
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)" || die "Could not resolve this script's directory."
RUN_PY="$SCRIPT_DIR/run.py"
Q_PY="$SCRIPT_DIR/quick_question.py"
[ -f "$RUN_PY" ] || die "run.py not found next to this script (expected at $RUN_PY)."
[ -r "$RUN_PY" ] || die "run.py exists at $RUN_PY but is not readable."
[ -f "$Q_PY" ] || die "quick_question.py not found next to this script (expected at $Q_PY)."
[ -r "$Q_PY" ] || die "quick_question.py exists at $Q_PY but is not readable."

# Aliases are single-quoted strings embedding the venv python + a launcher path;
# a single quote in either would break the quoting.
case "$PYDEV_PY $SCRIPT_DIR" in
    *\'*) die "A path (venv or project) contains a single quote; move it somewhere without one." ;;
esac

# --- 4. Required software checks --------------------------------------------

command -v docker >/dev/null 2>&1 || die "docker not found in PATH. Install Docker Desktop or Docker Engine (see README)."
docker version >/dev/null 2>&1 || die "docker not responding — start the daemon / Docker Desktop, then re-run."

# Docker floor: parse the client version ("Docker version 24.0.7, build ...")
# and compare major.minor against MIN_DOCKER. Unparseable → warn, don't block.
DOCKER_VER="$(docker --version 2>/dev/null | sed -n 's/.*[Vv]ersion \([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p')"
if [ -n "$DOCKER_VER" ]; then
    have_maj="${DOCKER_VER%%.*}"; have_min="${DOCKER_VER#*.}"; have_min="${have_min%%.*}"
    min_maj="${MIN_DOCKER%%.*}"; min_min="${MIN_DOCKER#*.}"
    if [ "$have_maj" -lt "$min_maj" ] || { [ "$have_maj" -eq "$min_maj" ] && [ "$have_min" -lt "$min_min" ]; }; then
        die "Docker $DOCKER_VER is older than the required $MIN_DOCKER. Update Docker (see README) and re-run."
    fi
else
    printf 'Warning: could not read the Docker version; skipping the >= %s floor check.\n' "$MIN_DOCKER" >&2
fi

# The launcher deps must import under the venv python the aliases use.
PY_OK="$("$PYDEV_PY" -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)' 2>/dev/null || printf 0)"
[ "$PY_OK" = "1" ] || die "The ~/pydev venv python is older than 3.10; recreate it via install_dependencies.sh."
"$PYDEV_PY" -c 'import prompt_toolkit, dotenv' >/dev/null 2>&1 \
    || die "The ~/pydev venv is missing deps (prompt_toolkit / python-dotenv). Re-run install_dependencies.sh."

# --- 5. Install each alias into every shell rc that exists ------------------

ADDED=

# install_alias RC NAME TARGET_PY — append `alias NAME='PYDEV_PY "TARGET" '` to
# RC unless a `NAME=` alias already lives there. The trailing space inside the
# quotes lets `NAME arg` forward arg through as a positional.
install_alias() {
    rc="$1"; name="$2"; target_py="$3"
    if grep -E "^[[:space:]]*alias[[:space:]]+$name=" "$rc" >/dev/null 2>&1; then
        printf '  skipped  %-3s in %s (already present)\n' "$name" "$rc"
        return
    fi
    alias_line="alias $name='$PYDEV_PY \"$target_py\" '"
    { printf '\n\n'; printf '%s\n' "$alias_line"; printf '\n\n'; } >> "$rc" \
        || die "Failed to append the '$name' alias to $rc."
    printf '  added    %-3s to %s\n' "$name" "$rc"
    ADDED=1
}

# process_rc RC — install both aliases into RC when it's a writable regular file.
process_rc() {
    rc="$1"
    [ -f "$rc" ] || return 0
    [ -w "$rc" ] || die "$rc exists but is not writable by the current user."
    install_alias "$rc" "ai" "$RUN_PY"
    install_alias "$rc" "q"  "$Q_PY"
    PROCESSED=1
}

printf '\n'
process_rc "$HOME/.bashrc"
process_rc "$HOME/.zshrc"

# Neither existed — create the one matching the login shell and use it.
if [ -z "${PROCESSED:-}" ]; then
    case "${SHELL:-}" in
        */zsh) rc="$HOME/.zshrc" ;;
        *)     rc="$HOME/.bashrc" ;;
    esac
    touch "$rc" || die "No ~/.bashrc or ~/.zshrc found, and could not create $rc."
    printf '  (no ~/.bashrc or ~/.zshrc found; created %s)\n' "$rc"
    install_alias "$rc" "ai" "$RUN_PY"
    install_alias "$rc" "q"  "$Q_PY"
fi

# --- 6. Confirmation summary ------------------------------------------------

if [ -z "${ADDED:-}" ]; then
    printf '\nNothing to do — the aliases were already present in every rc file.\n'
    exit 0
fi

printf '\nOpen a new shell (or `source` the rc file printed above) to activate. Usage:\n\n'
printf '    %s\n' "ai                                    # opens the picker"
printf '    %s\n' "ai poet                               # skip picker; new instance of poet"
printf '    %s\n' "q \"why do elephants have big ears?\"   # one-shot question (quote it)"
printf '\nRename or remove the aliases in your rc file(s) at any time.\n'
