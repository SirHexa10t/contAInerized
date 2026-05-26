---
description: Run the project's full check stack — unit tests, ruff lint, mypy type-check, and a launcher dry-run smoke. Read-only; reports findings, doesn't fix.
---

## Instructions

Run the preflight first, then each numbered step in order. **Do not stop on failure** — run everything so the user sees a complete picture in one pass. Report each step's outcome under a clear heading so failures can't hide.

### Preflight — tool presence (+ conditional install)

Detect each tool / runtime dep. **When missing, attempt to install it inline** so the numbered steps can actually run. The installer choice cascades by what's available: `uv` first (project convention — fast, manages tool isolation), `pip3 --user` as a fallback for the Python deps. If neither's available, fall through to a printed manual command so the user knows what to run.

A missing tool that we successfully install ends the run as "✓ now available"; a missing tool we couldn't install ends as "✗ still missing" — that distinction matters for the report.

```bash
# CLI tools (ruff, mypy) — uv tool install puts them in an isolated env, no venv-activation needed
for tool in ruff mypy; do
    if command -v "$tool" >/dev/null 2>&1; then
        echo "✓ $tool"
    else
        echo "✗ $tool MISSING — installing..."
        if command -v uv >/dev/null 2>&1; then
            uv tool install "$tool" 2>&1 | tail -3 | sed 's/^/    /'
        else
            echo "    uv not in PATH — cannot auto-install. Install uv from https://astral.sh/uv, then re-run."
        fi
        command -v "$tool" >/dev/null 2>&1 && echo "  ✓ $tool now available" || echo "  ✗ $tool still missing"
    fi
done

# Python runtime deps (prompt_toolkit, python-dotenv, rich) — needed for test_run + the dry-run step.
# `pip3 --user --break-system-packages` is the cheap path: doesn't need root, and the
# --break-system-packages flag bypasses PEP 668's externally-managed marker on Debian/Ubuntu
# (no-op everywhere else). `uv pip install` without a venv refuses outright, so we don't try it.
# install_dependencies.sh is the canonical path (creates ~/pydev venv); fall through to that
# hint if pip3 isn't available either.
if python3 -c "import prompt_toolkit, dotenv, rich" 2>/dev/null; then
    echo "✓ Python runtime deps (prompt_toolkit, python-dotenv, rich)"
else
    echo "✗ Python runtime deps MISSING — installing..."
    if command -v pip3 >/dev/null 2>&1; then
        pip3 install --user --break-system-packages prompt_toolkit python-dotenv rich 2>&1 | tail -3 | sed 's/^/    /'
    else
        echo "    pip3 not in PATH — run 'install_dependencies.sh' from the project root to set up the ~/pydev venv."
    fi
    python3 -c "import prompt_toolkit, dotenv, rich" 2>/dev/null && echo "  ✓ runtime deps now available" || echo "  ✗ runtime deps still missing"
fi
```

**Still run the numbered steps even if a tool stayed missing** — the per-step `command not found` / ImportError output confirms which were absent. The final report frames "still missing" failures as environmental, not regressions; "now available" cases proceed to real findings.

### 1. Tests

From the project root (`/workspace`):

```bash
python3 -m unittest discover -s launch/tests
```

Expect a `Ran NNN tests in 0.NNNs / OK` tail when clean. On failure, surface the failing test names + their tracebacks verbatim — don't paraphrase.

### 2. Ruff (linter)

From the project root:

```bash
ruff check .
```

Reports any rule violations across all Python files. Exit 0 = clean.

### 3. Mypy (type-check)

From the project root:

```bash
mypy launch/ run.py
```

Reports any type errors. Exit 0 = clean.

### 4. Launcher dry-run

Exercise the launcher's full orchestration up to (but not including) `docker compose run` — catches import-time errors and orchestration bugs that the unit tests stub past.

```bash
python3 run.py --dry-run
```

`require_docker` is gated on `not --dry-run`, so docker doesn't need to be installed for this step. Two caveats worth knowing:

- **Picker is interactive.** With no target arg, the launcher opens the prompt_toolkit picker. If running this non-interactively, pass an existing instance name to skip the picker, e.g. `python3 run.py poet__myproject --dry-run`.
- **New instances prompt for workspace + session name.** If the chosen instance doesn't exist yet, both will be asked. Set `AI_WORKSPACE=<path>` in the env to skip the workspace prompt; the session-name prompt is unavoidable for fresh instances.

Exit 0 = orchestration completed through every stage; failures surface as the usual stack trace.

## Report shape

After everything runs, lead with the Preflight result + four step-result bullets, then list findings underneath each. Frame any per-step failure that's clearly an environmental gap (missing tool, missing Python dep) as such — not as a code regression. Example with a clean environment:

```
Preflight: ✓ ruff, ✓ mypy, ✓ runtime deps
- Tests:   340 passed, 0 failed
- Ruff:    2 findings
- Mypy:    clean
- Dry-run: clean

Ruff findings:
  launch/foo.py:42:1  E501  line too long (95 > 88 characters)
  launch/bar.py:17:5  F841  local variable 'unused' is assigned but never used

Mypy findings:
  (none)

Dry-run:
  (no errors; ran through select_pick → resolve_target → compose_runtime → setup_state, exited at run_compose's --dry-run guard)
```

Example where preflight auto-installed everything that was missing (the run then proceeds with real findings):

```
Preflight: ✓ ruff (installed inline), ✓ mypy (installed inline), ✓ runtime deps (installed inline)
- Tests:   340 passed, 0 failed
- Ruff:    clean
- Mypy:    clean
- Dry-run: clean
```

Example where the auto-install couldn't proceed (e.g., `uv` itself was missing) — surface that gap up front so the per-step blockages aren't misread as code regressions:

```
Preflight: ✗ ruff still missing (uv not in PATH), ✗ mypy still missing (uv not in PATH), ✗ runtime deps still missing
- Tests:   340 passed, 1 module failed to import (env issue — see Preflight)
- Ruff:    blocked (tool missing)
- Mypy:    blocked (tool missing)
- Dry-run: blocked (runtime deps missing — `python3 run.py` can't import menu_picker)

Action: install uv (https://astral.sh/uv), then re-run /test-project — the preflight will pick up from there.
```

Do not auto-fix anything. The user reviews findings and decides what to do.
