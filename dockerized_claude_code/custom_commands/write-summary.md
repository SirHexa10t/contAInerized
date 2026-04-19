Analyze this project and produce a structured summary that would let a fresh AI session understand the codebase quickly and work effectively without re-exploring from scratch.

## Instructions

1. **Detect what changed.** Before reading any files, run a command to list every file in the project with its last-modified timestamp (e.g., `find /workspace -not -path '*/.git/*' -not -name '.claude_summary' -type f -printf '%T@ %Tc %p\n' | sort -k1 -nr`). Exclude `.claude_summary` from this listing — it is the summary output itself and would always appear changed. Compare this list against the `### File Manifest` section in the existing summary (if one exists). Files whose timestamps match the manifest have not changed — **do not re-read them**. Only read files that are new, modified, or missing from the manifest. If there is no existing summary, read everything.

2. **Explore the project thoroughly.** Read the files identified as new or changed in step 1 — entry points, configuration, READMEs, build files, directory listings — until you have a solid understanding of what this project does and how it is organized. Do not skim; do not guess. If the project is large, prioritize breadth first (directory structure, entry points, config) before depth (individual modules).

3. **Write the summary** following the structure below. Aim for **300-800 words total** depending on the project's complexity. A trivial single-file script may need 150 words; a multi-service monorepo may need 1000. Use your judgment, but err on the side of concise — every word costs tokens in future sessions.

4. **Revise for consistent depth.** Before saving, do one revision pass: do sibling rows in each table carry comparable weight? Do sibling bullets? Is any cell or bullet padded to match a column that was not earning its space? Cut or flatten accordingly — it is fine to drop a column or collapse a row if it only fills space. Future sessions have to read the whole file on every invocation, so evenly-pitched and smaller is kinder than thorough-but-lumpy.

5. **Save the summary** to `/workspace/.claude_summary`.

6. **If a previous summary already exists in `/workspace/.claude_summary`**, read it before exploring the codebase. Then:
   - **Preserve** sections that are still accurate — do not rewrite what hasn't changed.
   - **Update** sections where the codebase has diverged: new features, removed components, changed structure, shifted priorities.
   - **Remove** anything that is no longer true. Stale information is worse than missing information.
   - After updating, the result should read as a coherent whole — not a patchwork of old and new. Smooth over seams.
   - If the existing summary is so outdated that most of it needs rewriting, replace it entirely rather than editing line by line.

## Summary Structure

Use these sections. Omit any section that genuinely does not apply.

Frame the whole summary under a `## Project Summary` top-level heading, with each section below as `###`. Immediately under that heading, include this one-line italicized disclaimer verbatim:

*AI-generated project summary produced by `/write-summary`, for future AI sessions catching up without re-analyzing line-by-line; may be out of date — re-run to refresh.*

Leave a blank line between sections — scannability matters more than density. A wall of text is harder to use than a slightly longer file with breathing room.

### Purpose
What this project does, who it is for, and what problem it solves. One to three sentences.

### Tech Stack
Languages, frameworks, key dependencies, and infrastructure (e.g., Docker, CI provider). Bullet list.

### Project Structure
Map of top-level directories and key files with a short description of each. Use a flat list or a tree — whichever is clearer. Only go one or two levels deep; do not enumerate every file.

**If the project has a recurring entity** (agents, services, API endpoints, plugins, microservices), follow the tree with a small table — one row per instance, columns for the axes that differ (name, role, model/version, config). Tables scan faster than prose when information repeats.

### Architecture
How the main components interact. **Lead with the concrete entry point** — the command, file, or function a user/operator actually invokes — so a fresh AI reader knows *where to start reading*. Then trace the workflow at a high level: which modules get called in what order, where state is produced or mutated, where the process ends. A short paragraph or numbered sequence is enough; don't descend to line-level detail, since the goal is orientation, not a full trace. Skip this section for single-file projects.

Cite implementation specifics with `file:line` (e.g., `run.py:42`) so a reader can jump straight to the code. For bind mounts, URL routes, config-to-env mappings, or any other pairings, write both sides with an arrow so the direction is explicit: `~/.claude-agents/<name> → /home/claude/.claude (per-agent state)`.

### Conventions and Patterns
Naming conventions, code style, configuration patterns, or recurring idioms that someone working in the codebase should know. Only include things that are not obvious from the language/framework defaults.

State non-obvious gotchas inline in parentheses, next to the fact they qualify — not in a separate "gotchas" section or footnote. E.g., "Per-agent `.conf` fully replaces `default.conf` (not merged — agent configs must re-declare defaults they still want)." The caveat should sit where the reader first encounters the claim.

### Known Issues
Observed problems a fresh agent should be aware of — bugs, missing features, bad code habits, stale dependencies, unfinished migrations, known footguns. Each item one or two lines with a `file:line` citation when applicable.

Distinct from Current State: Known Issues are **persistent problems** that stay in place until someone fixes them. In-flight work or pending decisions belong in Current State.

Only list issues with concrete evidence — observed in code, flagged by the user, or documented in the repo. Don't speculate about hypothetical problems.

If there are none, say so in one line.

### Current State
Active work, TODOs, or incomplete features visible in the code. Anything that a fresh session should be aware of so it does not repeat work or miss context.

**Only note a recent change when you actually know *why* it was made** — from conversation context, an explicit code comment, or a commit message you have read. Do not speculate or back-fill a plausible-sounding rationale. If the reason is not genuinely known to you, omit the note entirely; a missing bullet is better than an invented one.

When you do include such a note, it must be *load-bearing* — a fresh session would act differently because of it (a pending rebuild, an in-flight migration, a workaround that would otherwise surprise the next reader). On every pass, **prune notes whose behavior has settled into routine**: these accumulate across commits and crowd out what still matters.

If there is nothing notable, say so in one line.

### File Manifest
A table of every project file with its last-modified timestamp (epoch and human-readable). This section is machine-consumed: future runs of `/write-summary` compare against it to skip unchanged files. Use this exact format:

```
| File | Modified (epoch) | Modified (human) |
|------|-----------------|------------------|
| run.py | 1713408840.0 | Fri 18 Apr 2025 03:34:00 |
| ... | ... | ... |
```

## Calibration Guidelines

- **Do** include: facts a new session would waste time rediscovering (e.g., "tests are in `__tests__/` not `tests/`", "the CLI entry point is `bin/run`", "auth uses OAuth stored in `~/.creds`").
- **Do** favor density that aids scanning: `## Project Summary` as the root with blank lines between sections, tables over prose for repeating entities, `file:line` citations for implementation claims, and `host → container` arrows for mappings.
- **Do** flag recently-changed behavior *when you know the reason* — that's tribal knowledge a fresh session can't recover. Never invent a rationale to justify including a note; omit it instead.
- **Do** name each layer when configuration is split across files (Dockerfile `ENV`, compose `environment`, `.conf`/`.env` overrides, CLI flags, in-code defaults). List which keys live in which layer. A reader changing behavior must know whether to edit the image, the compose file, a per-instance config, or the source — otherwise they will edit the wrong layer or miss a baked-in default. This applies to any split-configuration pattern, not just containers.
- **Do not** include: line-by-line code explanations, full API docs, dependency version numbers (unless critical), or implementation details that are obvious from reading the code.
- **Do not** elaborate where elaboration doesn't earn its line. Every sentence must carry information a fresh session would act on. Cut restatements of what a table already shows, qualifiers that repeat the section heading, and colour commentary. Prefer a terse correct line over a padded one.
- **Do not** pad with filler. If the project is simple, the summary should be short. But do not sacrifice scannability (whitespace, tables, headings) to hit a lower word count — structure is not filler.
- **Do not** speculate. If something is unclear, note the ambiguity rather than guessing.
- **Always** regenerate the File Manifest from a fresh `find` — never copy timestamps from the previous manifest without verifying.
- **Exclude** `.claude_summary` from the File Manifest — it is the summary output itself and would always appear as changed, creating a self-referential loop.
