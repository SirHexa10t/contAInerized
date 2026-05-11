#!/usr/bin/env bash
# Install host-side dependencies for running the launcher (run.py).
# Mirrors the README's Install sections (Linux + macOS). Idempotent: safe to re-run.
#
# What it sets up
# ---------------
#   • uv               — Python toolchain manager (Astral's installer if missing)
#   • ~/pydev          — uv-managed venv with Python 3.14 + the launcher's pip
#                        deps: prompt_toolkit, python-dotenv, rich, publicsuffix2
#   • Docker engine    — Linux: official convenience script (bundles Compose v2)
#                        macOS: Docker Desktop (via Homebrew if available;
#                                otherwise prints a manual-install hint)
#   • docker group     — Linux only: adds your user so docker commands don't
#                        need sudo. macOS Docker Desktop manages permissions
#                        differently, so this step is skipped there.
#
# Usage
# -----
#   bash install_dependencies.sh
#
# When it finishes:
#   • Linux: log out + back in (or `newgrp docker`) to activate the docker group.
#   • macOS: launch Docker Desktop manually (the daemon won't start on its own).
#   • Both:  `source ~/pydev/bin/activate` before running `python3 run.py`.

set -euo pipefail

err() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "→ $*"; }

# Sanity checks -----------------------------------------------------------------

[[ "$EUID" -eq 0 ]] && err "Run as your normal user, not root. The script uses sudo where it actually needs to."

case "$(uname -s)" in
    Linux*)  OS=linux ;;
    Darwin*) OS=macos ;;
    *)       err "Unsupported OS: $(uname -s). Install the host requirements listed in README.md ('Host requirements' section) manually." ;;
esac
log "Detected OS: $OS"

# 1. uv (cross-platform) --------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
    log "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# Bring uv into this script's PATH whether it was just installed or already on disk.
if [[ -f "$HOME/.local/bin/env" ]]; then
    # shellcheck disable=SC1091
    source "$HOME/.local/bin/env"
fi
command -v uv >/dev/null 2>&1 || err "uv install failed; check the output above."
log "uv ready: $(uv --version)"

# 2. Python venv at ~/pydev (cross-platform) -----------------------------------

if [[ -d "$HOME/pydev" ]]; then
    log "Python venv at ~/pydev exists; reusing it."
else
    log "Creating Python venv at ~/pydev (uv will auto-download Python 3.14 if needed)..."
    uv venv "$HOME/pydev" --python 3.14
fi
log "Installing/updating launcher deps in ~/pydev (prompt_toolkit, python-dotenv, rich, publicsuffix2)..."
# shellcheck disable=SC1091
source "$HOME/pydev/bin/activate"
uv pip install prompt_toolkit python-dotenv rich publicsuffix2

# 3. Docker (OS-specific) ------------------------------------------------------

GROUP_ADDED=
DOCKER_DESKTOP_INSTALLED=

if [[ "$OS" == "linux" ]]; then
    if ! command -v docker >/dev/null 2>&1; then
        log "Installing Docker via the official convenience script..."
        curl -fsSL https://get.docker.com | sh
    else
        log "Docker already installed: $(docker --version)"
    fi
    if ! docker compose version >/dev/null 2>&1; then
        err "Docker Compose v2 plugin not found. Reinstall via https://get.docker.com or check your apt sources."
    fi
    # Group membership (Linux only)
    if id -nG "$USER" | grep -qw docker; then
        log "$USER is already in the docker group."
    else
        log "Adding $USER to the docker group (sudo)..."
        sudo usermod -aG docker "$USER"
        GROUP_ADDED=1
    fi

elif [[ "$OS" == "macos" ]]; then
    if command -v docker >/dev/null 2>&1; then
        log "Docker already installed: $(docker --version)"
        if ! docker info >/dev/null 2>&1; then
            log "  (daemon not reachable — launch Docker Desktop manually before running run.py)"
        fi
    elif command -v brew >/dev/null 2>&1; then
        log "Installing Docker Desktop via Homebrew..."
        brew install --cask docker
        DOCKER_DESKTOP_INSTALLED=1
    else
        err "Docker not found and Homebrew not available. Install Docker Desktop manually from
       https://www.docker.com/products/docker-desktop, then re-run this script to verify the rest."
    fi
fi

# Wrap-up ----------------------------------------------------------------------

echo
echo "✓ Dependencies installed."
echo
echo "Next steps:"
if [[ -n "$GROUP_ADDED" ]]; then
    echo "  • Activate the docker group for this shell:    newgrp docker"
    echo "    (or log out + back in for it to stick everywhere)"
fi
if [[ -n "$DOCKER_DESKTOP_INSTALLED" ]]; then
    echo "  • Open Docker Desktop once so the daemon starts (it doesn't auto-start)."
fi
if [[ "$OS" == "macos" && -z "$DOCKER_DESKTOP_INSTALLED" ]]; then
    echo "  • If Docker Desktop isn't running, start it before launching."
fi
echo "  • Activate the Python venv before launching:    source ~/pydev/bin/activate"
echo "  • Then run the launcher:                        python3 run.py"
