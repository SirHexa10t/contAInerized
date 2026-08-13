---
description: Run the project's quality gate (bash check.sh — tests, lint, types) plus a launcher dry-run smoke. Read-only; reports findings, doesn't fix.
---

## Instructions

Run the preflight first, then the two numbered steps in order. **Do not stop on failure** — run everything so the user sees a complete picture in one pass. Report each step's outcome under a clear heading so failures can't hide.

The checks themselves are not defined here. `check.sh` at the project root is the single definition of a passing tree — the same script CI runs — and this command's job is only to (a) repair the environment first, which the script deliberately never does, and (b) add the dry-run smoke, which CI can't run (it's interactive). If you're tempted to run one of the gate's tools directly, edit `check.sh` instead — a check that lives only here would drift from CI (`TestQualityGate` in `launch/tests/test_essential_files.py` enforces this).

### Preflight — tool presence (+ conditional install)

Detect each tool / runtime dep. **When missing, attempt to install it inline** so the gate reports real findings instead of missing-tool failures. The installer choice cascades by what's available: `uv` first (project convention — fast, manages tool isolation), `pip3 --user` as a fallback for the Python deps. If neither's available, fall through to a printed manual command so the user knows what to run.

A missing tool that we successfully install ends the run as "✓ now available"; a missing tool we couldn't install ends as "✗ still missing" — that distinction matters for the report.

```bash
# docker — required by the dry-run step (require_docker fires in BOTH modes
# now, so dry-run will exit early if docker isn't on PATH). Not auto-installable
# here; surface the gap so the user knows to install Docker Engine.
if command -v docker >/dev/null 2>&1; then
    echo "✓ docker"
else
    echo "✗ docker MISSING — install Docker Engine from https://docs.docker.com/engine/install/"
fi

# The gate's CLI tools — uv tool install puts them in an isolated env, no
# venv-activation needed. check.sh counts a missing one as a FAILURE (a check
# that didn't run isn't a check that passed), so repair here, before the gate.
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

# Python runtime deps (prompt_toolkit, python-dotenv, rich) — needed by the
# suite's run/picker tests and the dry-run step.
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

**Still run the numbered steps even if a tool stayed missing** — the gate names each check it couldn't run, which confirms what was absent. The final report frames "still missing" failures as environmental, not regressions; "now available" cases proceed to real findings.

### 1. Quality gate

From the project root (`/workspace`):

```bash
bash check.sh
```

Runs the test suite, the linter, and the type-check. It never stops at the first failure — every check runs, each gets a `✓`/`✗` verdict, and the exit code is non-zero if any failed. A failing check prints its tool's full output; surface failing test names and tracebacks verbatim in the report — don't paraphrase.

### 2. Launcher dry-run

Exercise the launcher's full orchestration up to (but not including) the real `docker build` / `docker run` calls — catches import-time errors and orchestration bugs that the unit tests stub past.

```bash
python3 run.py --dry-run
```

**`require_docker` runs in both modes now** — dry-run is a faithful projection of "what would happen on a real run," so a missing docker daemon surfaces as the same one-line exit a real run would produce. Install Docker Engine (the preflight check above flags it) before running this step. Two further caveats worth knowing:

- **Picker is interactive.** With no target arg, the launcher opens the prompt_toolkit picker. If running this non-interactively, pass an existing instance name to skip the picker, e.g. `python3 run.py poet__myproject --dry-run`.
- **New instances prompt for workspace + session name.** If the chosen instance doesn't exist yet, both will be asked. Set `AI_WORKSPACE=<path>` in the env to skip the workspace prompt; the session-name prompt is unavoidable for fresh instances.

Exit 0 = orchestration completed through every stage; failures surface as the usual stack trace.

## Report shape

After everything runs, lead with the Preflight result + two step-result bullets, then list findings underneath each. Frame any failure that's clearly an environmental gap (missing tool, missing Python dep) as such — not as a code regression. Example with a clean environment:

```
Preflight: ✓ docker, ✓ ruff, ✓ mypy, ✓ runtime deps
- Gate:    3/3 clean (tests, lint, types)
- Dry-run: clean

Dry-run:
  (no errors; ran through gather_input → resolve_target → apply_tags → setup_state → ensure_image → run_container; docker_subprocess printed each "would invoke" line and returned)
```

Example with real findings — the gate names its failing checks; quote each tool's output underneath:

```
Preflight: ✓ docker, ✓ ruff (installed inline), ✓ mypy (installed inline), ✓ runtime deps
- Gate:    FAILED — 2 of 3: lint, types (tests clean)
- Dry-run: clean

Lint findings:
  launch/foo.py:42:1  F841  local variable 'unused' is assigned but never used

Type findings:
  launch/bar.py:17: error: Incompatible return value type (got "str", expected "int")
```

Example where docker is missing — dry-run no longer skips that check, so it surfaces as a step failure rather than a hidden assumption:

```
Preflight: ✗ docker MISSING, ✓ ruff, ✓ mypy, ✓ runtime deps
- Gate:    3/3 clean (tests, lint, types)
- Dry-run: blocked — "docker is required but was not found in PATH" (require_docker runs in both modes now)

Action: install Docker Engine, then re-run /test-ai-project for the dry-run step.
```

Example where the auto-install couldn't proceed (e.g., `uv` itself was missing) — surface that gap up front so the blockages aren't misread as code regressions:

```
Preflight: ✗ docker MISSING, ✗ ruff still missing (uv not in PATH), ✗ mypy still missing (uv not in PATH), ✗ runtime deps still missing
- Gate:    FAILED — 3 of 3: tests (import errors — env issue, see Preflight), lint(missing), types(missing)
- Dry-run: blocked (runtime deps missing — `python3 run.py` can't import menu_picker)

Action: install uv (https://astral.sh/uv), then re-run /test-ai-project — the preflight will pick up from there.
```

Do not auto-fix anything. The user reviews findings and decides what to do.
