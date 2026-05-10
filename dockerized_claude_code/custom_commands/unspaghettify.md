Audit a project's structural organization — how files are split, what they import from where, and which third-party packages they pull in. The outcome: every file/module has an obvious, single-sentence role.

For per-file content audits (naming, idioms, control flow, dead code, magic literals), use `/refactor`. This command is one level up — it's about *which files exist and what each is for*.

## Subject

The user invoked `/unspaghettify $ARGUMENTS`.

- If `$ARGUMENTS` is a directory path, scope to that directory.
- If empty, scope to the project root (look upward from cwd for `pyproject.toml`, `package.json`, `Cargo.toml`, `.git`, etc.).
- If `$ARGUMENTS` is a single file, this is the wrong command — redirect the user to `/refactor`.

## Directive: Optimization priorities

Before the phases, use `AskUserQuestion` to ask what the user is optimizing for:
- **Conciseness / readability** — prefer the shortest clear form, even if it pulls in a library.
- **Minimal dependencies** — prefer stdlib and hand-rolled helpers, even at the cost of extra lines.
- **Performance** — only when measured, never speculative.

This weights the dependency-audit recommendations heavily — the same package can be 🟢 keep under one priority and 🔴 remove under another. Default to readability if the user has no strong preference.

## Outcome

By the end, every file/module in scope should have an obvious, single-sentence role — easy to recite, easy to defend. If a file resists summarization, the audit isn't done. The completed list of one-sentence roles is the audit's primary deliverable; it doubles as a skeleton for a project summary or a CLAUDE.md.

## Phases

### 1. Map the project

Read the directory tree, identify entry points (`main`, `run.py`, `index.js`, CLI binaries), and check for an existing summary (e.g. `.claude_summary`). If one exists, read it; if not and the project is non-trivial, suggest the user run `/write-summary` first — orientation is faster from a fresh summary than from a cold tree-read.

Group files by layer:
- **Entry / bootstrap** — what the user invokes
- **Domain / business logic** — the project's core concerns
- **Data / persistence** — storage, serialization, schemas
- **UI / presentation** — menus, views, formatted output
- **Infrastructure** — docker, CI, build, deploy
- **Config** — `.conf` / `.env` / `.yml`
- **Tests**

A file that doesn't fit any layer is itself a finding for later phases.

### 2. Import evaluation across the project

This is the per-file import audit (Pass 1 of `/refactor`), scaled. Same smells, surfaced cross-file:

- **Imports that cross layers wrongly.** A "data" module importing a UI helper, or vice versa — one of them is doing the other's job.
- **Helpers imported by exactly one file.** If `foo.py`'s only consumer is `bar.py`, the helper probably belongs *inside* `bar.py` (or the two should merge). Lone-caller helpers have outgrown the original need to be shared.
- **"Junk drawer" modules imported everywhere.** A `utils.py` / `helpers.js` consumed by 80% of the project is a sign helpers were never properly homed. Each one has a real domain — surface them, propose homes.
- **Same constant declared in multiple files.** Define once, import everywhere. Cross-reference the dependency audit if the constant has a configurable origin.
- **Domain operations leaking into orchestration.** If a bootstrap imports `load_X` + `save_X` and the only use is `m = load_X(); m[k] = v; save_X(m)`, the operation is a domain helper pretending to be orchestration. Move it next to the data.
- **Cyclic-feeling import graphs.** A → B → C → A: even when the language doesn't error, one of those three is in the wrong layer.

For each finding: **propose the new home**, the rationale, and what the resulting import surface looks like.

### 3. Dependency audit

For every third-party package in the project (read `pyproject.toml` / `package.json` / `requirements.txt` / `Cargo.toml` / `go.mod` / etc.):

| Signal           | Question                                                         |
|------------------|------------------------------------------------------------------|
| **Necessity**    | Replaceable with a small utility or stdlib API?                  |
| **Health**       | Last release? Issues trend? Maintainer activity? Adoption?       |
| **Weight**       | Install / bundle footprint? Transitive deps?                     |
| **Overlap**      | Does another dep already cover this?                             |
| **Version risk** | EOL or pre-1.0? Known CVEs?                                      |

Action thresholds:
- 🔴 **Remove** — unmaintained (no release in 30+ months), known CVEs, or trivially replaceable in <50 lines.
- 🟡 **Replace** — maintained but heavy, outdated, or overlapping with another dep. Show the migration path.
- 🟢 **Keep** — actively maintained, justified, no realistic alternative.

When recommending replacement, present both a dependency-free and a lighter-dep option when the difference in code size is meaningful — the Optimization-priorities answer guides which to prefer.

If a `/refactor` pass on individual files would be needed to drop a dep (e.g., the codebase hand-rolls something a library does), note that as a follow-up — `/refactor`'s "Uninvent the wheel" pass is the inverse direction (use a library where one was hand-rolled), and the two passes should arrive at consistent recommendations.

### 4. File operations — split, merge, add, remove

After phases 2–3, the structural moves become visible. Surface them as four operation types:

- **Split.** A file with no coherent theme — you can't summarize it in one sentence. The split lines fall along natural seams: groups of functions that share a concern with each other but not with the rest of the file. Each new file gets its own one-sentence role.
- **Merge.** Two files that always change together, or one whose sole purpose is to import-and-re-export from another (a thin layer of indirection). Merge them; keep the better-named module.
- **Add.** A concern that exists in the codebase but is currently scattered. *Validation logic spread across 5 modules → one new `validation.py`.* The new file collects the scattered pieces under one roof and gets a clear role.
- **Remove.** Orphans (no callers anywhere), files left behind from an earlier refactor that were never deleted, thin re-export modules that surface exactly one symbol from elsewhere.

For each operation: list the files and contents involved, the rationale, and the resulting one-sentence role of each affected file.

### 5. Role check — the finish line

For every file in the (modified) scope, write a one-sentence role:

> *`agents_crud.py` — CRUD operations on agent state directories and the workspace/modes maps.*
> *`docker_config.py` — environment, image-build chain, and `docker compose run` orchestration.*

If you can't write that sentence cleanly, the audit isn't done — find what makes the file ambiguous (multiple themes? unclear name? doing one thing but named for another?) and propose a fix.

The completed role list is the audit's most valuable output: a clean spine for the project, a skeleton for documentation, and a baseline against which future drift can be measured.

## Output

Group findings by phase. For each:
- **What** — the proposed move (file split, dep replacement, import relocation)
- **Why** — current pain it resolves
- **Diff sketch** — files added / removed / renamed, key import changes
- **Severity** — 🔴 entangles other files / changes contracts (callers across the project affected), 🟡 structural drag (slows future changes, obscures where things live), 🔵 nit (cosmetic placement, minor naming)

After the phase-by-phase findings, present the **role table**: every file in scope, with its one-sentence role *after* the proposed changes. This is the deliverable the user judges the audit by — if a row reads thinly or two rows say almost the same thing, more work is needed.

Then ask which findings to apply. **Default to staged apply**: one structural move per conversation, verifying nothing broke (AST parse + name resolution) before the next. Project-wide moves have ripple effects — bundling them risks one bad move tainting all the others.

## Restraint

- **Don't over-restructure.** A working file in a known location beats a tidy file no one can find. Mental-model fit beats technical purity.
- **Stable seams beat clever seams.** Cuts that match how the team thinks about the code outlast cuts that look elegant on paper.
- **Apply staged, not in bulk.** One file move per conversation, with verification, beats a 30-file restructure no one can review.
- **For per-file deep cleans, use `/refactor`.** This command stops at file boundaries — everything inside a file is `/refactor`'s domain.
