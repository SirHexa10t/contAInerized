Write or update a `README.md` for this project that takes a fresh reader from a clean machine to a running program.

## Instructions

1. **Read what's already there.** Read the existing `README.md` if present, plus any setup scripts (`setup.sh`, `scripts/setup.*`), `.env.example`, `Dockerfile`, `docker-compose.yml`, and manifest files (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, etc.). These reveal both what's installed and what's expected.

2. **Explore the project enough to describe what it does.** Read entry point(s) and surrounding code until you can write an honest purpose statement and an honest features list. If the purpose isn't clear from the code, **ask the user** rather than guess.

3. **Confirm scope before writing.** Unless the project is obviously substantial (multiple modules, dependency manifest, build system), ask the user one question:
   > "Is this a full project (deserves the full README treatment) or a one-off / spike / throwaway (a one-paragraph note is fine)?"

   Skip this question only when the answer is clearly "full" from the codebase shape.

4. **Verify what you can.** For every command, file path, or environment-variable name you'll mention, confirm it exists in this project. Don't write `python main.py` if the entry point is `src/run.py`. If you can't verify a step (domain-specific output, paid API, missing credentials), **ask the user** for a real successful run's output — don't fabricate it.

5. **Write the README** following the structure below, saving to `/workspace/README.md`.

## README Structure

Four sections, in this order. `Features` is optional for trivial scripts; the other three are required.

### 1. What the project does
One or two sentences leading the file: what it is, who it's for, what problem it solves. Without this, a visitor can't tell whether they're in the right place.

### 2. Features
A bullet list of distinct capabilities — what a user can DO with this project. One line per feature, action-oriented, focused on user-visible value rather than implementation. Skip this section entirely for trivial scripts where a feature list would be artificial.

### 3. Tech Stack Setup (its own section)
List every language, framework, and notable tool — and for each, point the user to the **recommended way to install it**. Not "install Rust" — *how*:

- **Languages / runtimes**: link to the official installer and give the shortest reliable command (e.g., the `rustup` one-liner for Rust; `uv` as the fast path to managed Python; the official Node installer or a version manager for Node).
- **Package managers**: which one, which version, how to install it cleanly.
- **System config**: any `/etc/` files, OS settings, kernel parameters, file permissions, or user groups the user needs to tweak. Name the exact file and the exact change — don't hand-wave.
- **BIOS / firmware**: if virtualization, IOMMU, Secure Boot, TPM, or similar need toggling, say (a) **how to enter BIOS** (common vendor keys), and (b) **how to verify from the OS after boot** that the setting actually took effect.
- Split steps per-OS (macOS / Linux / Windows) when they diverge. Don't assume the reader's platform.

This section ends when every required `which <tool>` would succeed and any system/BIOS prerequisites are confirmed.

**If setup is expansive** — many steps, multiple system tweaks, specific ordering — recommend a **setup script** (`setup.sh`, `scripts/setup.py`, etc.) that the README points to instead of forcing a long checklist by hand. **Tell the user before writing one** so they can approve the path. If approved, the script must:

- **Be verbose** — announce each step before running and confirm after (e.g., `Installing uv...` → `✓ uv installed`).
- **Fail loud, fail fast** — exit non-zero on any error and stop immediately. Never partially succeed silently.
- **Be described in the README** — so the reader expects the chatty output and the halt-on-failure behavior, and reads a failure mid-run as "fix the cause and re-run," not "the script is buggy."

### 4. How to Run (its own section)
Assumes setup is done. This section is about *using* the project.

- **Entry points**: which files to run, and in what order if sequence matters (e.g., "run `scripts/bootstrap.py` once, then `src/main.py`").
- **Concrete terminal examples**: at least a couple of real invocations with realistic arguments — not `<placeholders>`. Show the full command as it should be typed.
- **Expected output**: describe or show what success looks like — a sample stdout snippet, a log excerpt, a screenshot, whatever fits. If you couldn't run the project yourself, **ask the user** to paste a real successful run's output so the README reflects reality rather than a plausible guess.

## Keep it honest

Walk through the README mentally (or literally) on a clean machine before calling it done. Any step that's ambiguous, assumes pre-installed software, or quietly skips a detail is a bug in the README. A README that half-works is worse than one that's explicit about what it doesn't cover.

## Brief-mode escape hatch

If the user confirmed brief scope in step 3, write a single short paragraph: what it is, the one command to run it, the expected result. Skip the multi-section structure. Don't apologize for being short.
