---
description: Audit a single code file for smells, dead weight, and within-file simplification opportunities. For multi-file or project-structure work, use /unspaghettify instead.
argument-hint: <file-path>
---

Apply the passes below in order; report findings with concrete fixes; apply only after the user confirms.

## Subject

The user invoked `/refactor $ARGUMENTS`.

- **`$ARGUMENTS` is a file path** — target that file; run the passes below.
- **`$ARGUMENTS` is empty** — infer the target from conversation context (the file just edited or discussed). If still unclear, ask which file.
- **`$ARGUMENTS` is a directory, glob, or multiple files** — this is the wrong command for project-level structural work; redirect the user to `/unspaghettify`.

Read the target fully before scanning — partial reads miss cross-pass interactions.

## Directives

These shape the audit. Apply them around the passes — they are not passes themselves.

### Optimization priorities

Before the passes, use `AskUserQuestion` to ask what the user is optimizing for on this code:
- **Conciseness / readability** — prefer the shortest clear form, even if it pulls in a library.
- **Minimal dependencies** — prefer stdlib and hand-rolled helpers, even if it means more lines.
- **Performance** — only when measured, never speculative; treat hot paths differently from one-shots.

The same finding ("remove this dependency", "extract this helper") can be right under one priority and wrong under another. The answer reweights every recommendation throughout. Default to readability if the user has no strong preference.

### Triage first — stabilize before polish

Before the structural passes, scan for foundational issues that take precedence:
- **Security / correctness** — unsanitized input, exposed secrets, race conditions, unchecked nulls.
- **Silent failures** — code that swallows exceptions or returns plausible-looking wrong values.
- **Dangerous duplication** — copy-pasted blocks that have already diverged in ways suggesting bugs.
- **Live tech debt** — dead feature flags still wired into live paths, deprecated APIs with no migration plan.

If any are present, surface them first and propose a stabilization pass before the refactor work. Don't bury structural rot under cosmetic improvements.

## Passes

Each pass surfaces a different smell category. Stop at any pass that finds nothing; don't manufacture findings.

### 1. Misplaced logic — imports and operation scope

Two scales of the same concern: code in the wrong place creates drag that
shows up as imports the file doesn't need or guards the caller has to
re-implement at every call site.

**File-level: imports that fingerprint misplaced logic.**

For each import, ask: does the *logic that uses it* belong in this file, or is the import a footprint of logic that should live elsewhere? When logic moves, imports follow.

Smells:
- **Load + save pair imported.** If you see `load_X` and `save_X` both imported and the only use is `m = load_X(); m[k] = v; save_X(m)`, that's a domain operation pretending to be orchestration. Replace with `update_X(k, v)` next to the data; both imports collapse into one.
- **Constant imported only for an error message.** If `FOO_FILE` is referenced only inside one `sys.exit(f"... {FOO_FILE}")`, the validation belongs where `FOO_FILE` lives. Move the validation, drop the import.
- **Several helpers imported to assemble one operation.** Importing 4 things from `foo` to build a single-purpose function suggests the function belongs in `foo`. The caller imports one helper instead of four.
- **Single-use stdlib imports inside one function.** `os`, `subprocess`, `Path` etc. each used in exactly one expression deep inside one function — strong signal that function is at the wrong layer.

After applying: re-list imports. Every remaining one should serve the file's purpose at *its* level (e.g., a bootstrap shouldn't import `subprocess`).

**Function-level: operations that don't fit the function holding them.**

Same principle one level down: each *operation* inside a function should serve
the function's stated job. When an operation drifts outside that scope it
creates drag — callers grow guards around it, the function's name lies, future
edits touch the wrong site. Two directions to consider:

- **Pull operations *into* a function whose scope they fit.** If every caller
  wraps `foo()` with the same setup, guard, or post-step, that work belongs
  *inside* `foo()` — extending its responsibility by a hair beats having
  every caller re-implement the same hair. Concrete shapes: a function that's
  always called with a None-check is asking to be None-safe internally; an
  iterator that's always wrapped in `if dir.exists()` should no-op on a
  missing source; a subprocess wrapper that every caller follows with `if
  ret: sys.exit(ret)` should absorb the exit. Each callsite then loses an
  `if`, and the function's contract becomes "I handle my edges."
- **Push operations *out of* a function whose scope they don't fit.** A
  function named `compute_X` that also writes a file, sends a metric, or
  mutates a global is doing too much. (Pass 8 covers naming-the-lie; this is
  the *move* side — split the off-scope work into its right home.) If you
  can't justify the extra responsibility under the function's one-sentence
  job description, it doesn't belong.

The test: state each function's job in one sentence. Anything it does that
doesn't serve that sentence is a candidate for relocation — inward (the work
belongs to a callee) or outward (it belongs to a caller / sibling).

### 2. Wrappers that became pure delegation

After any extraction (in this pass or a prior refactor), scan local functions for shapes like:

```python
def stage4(a, b):
    """Stage 4 — Persist."""
    domain_helper(a, b)
```

That wrapper is dead indirection — the call site can call `domain_helper(a, b)` directly. Inline. Keep wrappers only when they add genuine local logic beyond forwarding args.

### 3. Source-of-truth, dispatch, and set operations

- **Iterate the source-of-truth, not the input.** When order matters (priority lists, render order, build chains), iterate the canonical ordered list and check membership in the input — *not* the reverse with a sort-after step.
- **Dispatch table over `if/elif` chain.** When each branch is `if x == "foo": _foo(); elif x == "bar": _bar(); ...`, replace with a `{"foo": _foo, "bar": _bar}.get(x)` lookup. Adding a case becomes one line in one place.
- **Walrus to bind constants inside their ordered list.** Instead of:
  ```python
  TAG_CODE = "code"
  ORDERED_TAGS = [TAG_CODE]
  ```
  use:
  ```python
  ORDERED_TAGS = [TAG_CODE := "code"]
  ```
  One source, no chance of drift.
- **Set operations for membership/validation.** For "items in X not in Y", use `X - Y` rather than a loop with a flag.

### 4. Control-flow simplifications

The shape of *how* a function reads. Watch for patterns that hide intent under nesting or chains:

- **Special-case via early return**, not nested branching, when the special case is structurally distinct (e.g., `if len(chain) == 1: return BASE_TAG`).
- **Nested ternaries → early returns.** `x = a if cond else (b if cond2 else c)` is harder to scan than three guarded `return`s, especially when the conditions are independent.
- **Deep nesting → flatten.** Pyramid-of-doom code (4+ levels of indentation from any combination of `if`/`for`/`while`, callbacks, or promise chains) usually flattens to a linear sequence: invert conditions for early returns, extract inner blocks to named helpers, replace callbacks with `await`. Each level of indentation is one more thing the reader has to hold on the stack.
- **Complex boolean chains → named predicates.** `if a and (b or c) and not d and e` is unreadable. Extract the meaningful predicate as a named local: `is_authorized = a and (b or c) and not d` — the condition self-documents and most explanatory comments become unnecessary.
- **Removable `if`s — push the guard into the callee.** When every caller writes `if x is not None: foo(x)` or `if not dir.exists(): return []` before iterating, the guard belongs *inside* the function whose domain it concerns. Make `foo` None-safe internally (`if x is None: return ...`) or make the iterator no-op on a missing source — and every caller sheds one `if`. Pattern: the callee's domain owns "what empty/None/missing means in my world"; the caller shouldn't second-guess.
- **Removable `if`s — fold loop-exits into the while-condition.** A polling loop `while ...: if check(): return X; sleep()` folds to `while ... and not (result := check()): sleep(); return result`. The exit condition becomes explicit in the while line — reads as "while X and Y, wait" which matches how the function would be described in English. Walrus binds the result for the post-loop return.
- **Removable `if`s — idempotent operations.** Replace `if k in d: del d[k]; save(d)` with `d.pop(k, None); save(d)`. `set.discard(x)` over `if x in s: s.remove(x)`. `s.add(x)` without an `if x not in s` guard — set add is idempotent at no cost; adding twice is the same as adding once. Where the language provides an idempotent form for "remove if present" / "add if absent", the surrounding `if` is dead weight — the operation already handles the absent / already-present case silently.
- **Removable `if`s — compute the value directly.** When the `if`'s only job is picking between values the expression could already produce on its own, drop it. `if x % 2 == 0: y = 0 else: y = 1` → `y = x % 2`. `if s: result = s.upper() else: result = ""` → `result = s.upper()` (empty string's `.upper()` is empty). `if name: greeting = f"Hi, {name}" else: greeting = "Hi"` → `greeting = f"Hi, {name}".rstrip(", ")` (or use `f"Hi{', ' + name if name else ''}"`). Where the underlying expression already produces the correct value across the relevant input range, the surrounding `if` is paraphrasing the computation — drop it.
- **Fewer returns when branches share shape.** `if cond: return X + 1 else: return X - 1` → `return X + 1 if cond else X - 1`. Two distinct returns are the right shape for distinct cases (decision-tree form — different things happen in each branch); branches that differ only in a value or arg collapse to one expression. Don't force-merge branches with different side effects or shapes — that just packs heterogeneous logic onto one line.

**Don't touch genuine dispatch.** An `if/elif/else` chain that fans into structurally-different operations — per-type handlers, per-state transitions, per-tag setups — is real control flow. The `if`s ARE the program logic, not gates around it. Pass 3 handles the dispatch-table transformation when the chain is large enough to merit it; the four `if`-removal bullets above apply to *guarding* `if`s, not switch-like fan-outs.

### 5. Naming and shape

- **`def` over `lambda` for anything named.** Functions deserve docstrings; lambdas don't get them. Reserve lambdas for trivial inline closures (sort keys, filters).
- **Long parameter lists (>5 args).** Consider a small dict/dataclass if the params travel as a group, OR splitting the function if the params signal two responsibilities.
- **Boolean parameters that gate large branches.** Usually a hint to split into two functions.
- **Same value re-computed at multiple call sites.** Derive once at the boundary, thread through. (E.g., if `instance_name(agent, session)` is called four times, compute `instance` once and pass it down.)

### 6. Magic literals and constant placement

Numbers and strings buried in expressions are *magic* — opaque to readers, easy to mistype, hard to find when they need to change. The same goes for constants defined where they're first used: they belong at the top.

- **Magic numbers.** `if size > 5368709120:` becomes `if size > MAX_BACKUP_BYTES:` with `MAX_BACKUP_BYTES = 5 * 1024**3` defined up top. Especially flagrant for thresholds, retry counts, timeouts, byte limits, version cutoffs.
- **Magic strings.** Repeated string literals — status keys, format specifiers, error categories, env-var names — belong as named constants. If `"PENDING"` appears in 6 places, it's a constant.
- **Definitions mid-script.** Constants and helpers defined in the middle of a file (right where they're first used) force the reader to scroll just to know what something resolves to. Hoist constants to the top; hoist helpers to module level.
- **One-shot literals are fine.** Don't extract a constant for a number that appears once and won't change. The threshold is repetition, non-obvious meaning, or "this might need to change someday and I want one place to change it".

### 7. Idiomatic expression

Code should read like the language was *meant* to be written. Statements that loop through `len()` to get an index, compare to `True`, or use camelCase in a snake_case file are amateur tells — they work, but they signal the writer hasn't internalized the idiom.

- **Useless roundabouts.** `if x is True:` → `if x:`. `if len(x) == 0:` → `if not x:`. `if x == None:` → `if x is None:`. `for i in range(len(xs)):` → `for x in xs:` (or `for i, x in enumerate(xs):` if you need both). `result = []; for x in xs: result.append(f(x))` → `[f(x) for x in xs]`.
- **Naming-convention violations.**
  - Constants in ALL_CAPS, mutable bindings in lower_snake (Python) / camelCase (JS/TS) / etc. A `MAX_RETRIES` that gets reassigned later either isn't a constant (rename) or shouldn't be reassigned (fix the call site). The casing is a contract — breaking it lies about the value.
  - Single-letter vars outside their conventional roles. `i, j, k` for loop indices, `x, y, z` for coordinates, `e` for an exception, `_` for ignored — fine. `x = some_database_record()` is not. The name should hint at the role.
  - Names that don't match the surrounding language idiom: `myVariable` in a Python file with `my_variable` everywhere else; `snake_case` in a TypeScript file. Match the language's convention, not the writer's previous-language muscle memory.
- **Hand-repeated operations.** Five lines doing the same thing on `arg1, arg2, arg3, arg4, arg5` should be a loop. The same `try/except` block copy-pasted around four call sites should be a context manager or a wrapper.

### 8. Side-effect honesty

- **Pure-looking helpers with side effects.** A function named `compute_X` that also does `mkdir`, `os.environ.update`, or file writes is lying. Either rename, split (`prepare_X` + `compute_X`), or document with explicit `SIDE EFFECTS:` and `RETURNS:` sections in the docstring.
- **Validation mixed into the happy path.** `if invalid: sys.exit(...)` interleaved with the main logic reads cleanly when extracted to a guard at the top — happy path stays uninterrupted below.
- **Edge-case challenges at every input boundary.** For each function that takes external input or processes user data, ask: *what happens when it's empty / enormous / malformed / malicious / concurrent?* Missing edge cases often hide as a happy-path branch with no guard. Flag the missing branch; suggest the guard.

### 9. DRY where the shapes really match

- Two functions in this file that differ only in a path/key/constant: extract a private `_helper(path, key, ...)` and rebuild both as one-liners over it. (For the same pattern repeated across files, see `/unspaghettify`.)
- **Real callers today, not hypothetical.** Extract only when 2+ real callers exist *now* — "we might need this later" is not a real caller. Even with two real callers, if the shapes could plausibly diverge, leave them as two slightly-similar lines. Apply DRY when the shapes have stabilized — usually around the third caller.

### 10. Design-pattern matching

When the structure of a problem matches a known design pattern, propose the pattern *by name*. Patterns are vocabulary — saying "this is a Strategy" is faster than describing the structure from scratch, and a future reader recognizes the shape immediately.

Common matches worth flagging:
- **Strategy / dispatch table** — branching on a type or key with similar-shape handlers (cross-reference Pass 3).
- **Factory** — repeated complex setup before constructing an object. Move the setup into a `make_X(...)` next to the type.
- **Context manager / RAII** — resources that need cleanup (file handles, locks, sockets, DB connections) where cleanup is scattered or missed in error paths.
- **Observer / pub-sub** — code that polls when it could be notified, or fires N hardcoded callbacks where one event bus would do.
- **State machine** — `if state == ...` branches with transitions scattered across the file. Centralize the transitions in one table.
- **Decorator / middleware** — a cross-cutting concern (logging, retries, auth, metrics) added inline at every call site instead of wrapped once.

**Don't shoehorn.** Only propose a pattern when the fit is clear *and* the pattern simplifies the code. "Could this be an Observer?" with a yes-but-it-doesn't-help answer is noise.

### 11. Uninvent the wheel

When code does something complex that a stdlib or widely-used library does perfectly well, replace it. The hand-rolled version inevitably has edge cases, bugs, and ongoing maintenance cost the library has already absorbed.

Common candidates:
- **Date/time parsing or arithmetic** → stdlib `datetime`, `dateutil`, `chrono`, etc. (not 50 lines of regex + manual leap-year handling).
- **CLI argument parsing** → `argparse` / `clap` / `commander` (not chains of `if sys.argv[i] == ...`).
- **HTTP retries / backoff / circuit breaking** → `tenacity`, `requests`, `httpx`.
- **Path manipulation** → `pathlib` (not string `+` and `os.path.join` chains).
- **Config parsing** → `tomllib`, `configparser`, `python-dotenv` (not hand-rolled key=value splitters).
- **Templating** → `jinja2`, `mustache` (not f-string concatenation across many lines).
- **Concurrency primitives** → `concurrent.futures`, `asyncio` (not hand-rolled `threading.Thread` + shared state).

**Caveat: weight against the priority directive.** Under *minimal dependencies*, a 20-line hand-rolled helper may beat pulling in a library — surface both options when the call is close, and let the chosen priority guide the recommendation.

### 12. Dead weight

- Unused imports (a static AST check catches these).
- Unused or never-tweaked "tweakable" constants — fictional knobs add noise.
- Comments restating the code (`# increment counter` next to `i += 1`). Delete.
- `# removed for X` markers next to deleted code. Delete; git history is for that.
- Half-finished/commented-out blocks. Delete or finish.

### 13. Lint / type-check after applying

Once any of the above passes have actually been applied, run the language's
lint + type-check tools against the modified file and address whatever they
flag. Different stacks, different tools — use what the project already runs;
install the missing one if there's no baseline. Common starting points:

- **Python** — `mypy <file>` (static type checking; sharper still: `pyright`)
  and `ruff check <file>` for lint smells `/refactor` doesn't catch
  (unused vars/args, shadowed names, walrus misuse, etc.).
- **JavaScript/TypeScript** — `tsc --noEmit <file>` + `eslint <file>`.
- **Rust** — `cargo clippy --fix` + `cargo check`.
- **Go** — `go vet ./...` + `staticcheck`.

Treat tool findings as the same kind of evidence as the earlier passes: real
issues get a fix, known false positives get a targeted ignore comment that
names the rule (`# type: ignore[arg-type]`, `// eslint-disable-next-line ...`).
Bulk-suppressing warnings is a smell of its own — if a whole category is
noisy, fix the root cause or scope the suppression narrowly.

A clean tool-run on the modified file is part of "done". Wave-of-new-warnings
after a refactor means the refactor isn't finished yet.

### 14. Persist the style — `.claude_dev_guidelines`

Once any refactors have landed, distill the recurring style choices into
`.claude_dev_guidelines` at the project root so future agents writing new
code in this repo adopt the same patterns instead of inventing fresh ones
the next reviewer has to push back on.

Update an existing file, or create one if absent. Cover at minimum:

- **Optimization priorities.** The codebase's stated priority (readability /
  minimal deps / performance) — captured from the up-front
  Optimization-priorities directive answer. Written down so future
  contributors don't have to re-elicit it conversation-by-conversation.
- **Idioms in active use.** The patterns this codebase reaches for, with
  pointers to canonical examples. E.g.:
  - *"`def` over named `lambda` for anything that takes a docstring; lambda
    reserved for trivial inline closures (sort keys, filters) — see
    `paths.py`'s path-builder map for the rare-exception form."*
  - *"Walrus operator to bind constants inside their ordered list rather
    than separate declarations."*
  - *"`Enum` + memoized `classmethod` views for taxonomies (see
    `InstanceModifiers`)."*
  Concrete enough that an agent can pattern-match.
- **Anti-patterns removed during this / prior refactors.** What NOT to
  introduce, with the reasoning. Drawn from real findings this pass
  surfaced — e.g. *"single-letter vars outside conventional roles (loop
  indices, exceptions); magic numbers outside one-shot literals; load+save
  pairs at orchestration boundaries — write `update_X(k, v)` instead and
  keep the I/O in the data layer."*
- **Type-annotation policy.** What level of typing the codebase requires
  for new code (return types only? params too? `from __future__ import
  annotations`? mypy gate in CI?). One paragraph is plenty; the goal is
  consistency, not exhaustiveness.

If the file already exists, MERGE — add new sections, refresh outdated
style notes, preserve sections covering concerns outside this pass. The
`/unspaghettify` command writes complementary *layout* guidance to the
same file (different sections); leave that material alone.

End-state: a fresh agent writing new code in this repo reads
`.claude_dev_guidelines` and matches the codebase's voice on the first
attempt — reducing the noise the next reviewer has to filter.

## Output

For each finding, report:
- **File:line** — the smell
- **Why** — what drags or breaks as the file evolves
- **Fix** — concrete new shape (function signature, replaced import line, etc.)
- **Severity** — 🔴 critical (breaks correctness, changes the file's exported contract, callers affected), 🟡 warning (local drag — harder to read, fragile, easy to misuse), 🔵 nit (style, naming, minor simplification)

Group findings by pass. After listing, ask which to apply (the user may want a subset, or want to apply in stages). Apply nothing without explicit confirmation.

## Restraint

- Don't refactor speculatively. Flag only what currently bites — *evidence in this file*, not hypothetical futures.
- Three similar lines beat a premature abstraction.
- A short, slightly-redundant function beats a clever one.
- If a pass yields nothing, say so plainly. Padding the audit defeats its purpose.
- After applying, run an AST parse + a static name-resolution check to confirm nothing broke. Report the diff in line counts and the trimmed import list.
