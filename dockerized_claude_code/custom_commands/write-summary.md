Analyze this project and produce a structured summary that would let a fresh AI session understand the codebase quickly and work effectively without re-exploring from scratch.

## Instructions

1. **Detect what changed.** Before reading any files, run a command to list every file in the project with its last-modified timestamp (e.g., `find /workspace -not -path '*/.git/*' -not -name '.claude_summary' -type f -printf '%T@ %Tc %p\n' | sort -k1 -nr`). Exclude `.claude_summary` from this listing — it is the summary output itself and would always appear changed. Compare this list against the `### File Manifest` section in the existing summary (if one exists). Files whose timestamps match the manifest have not changed — **do not re-read them**. Only read files that are new, modified, or missing from the manifest. If there is no existing summary, read everything.

2. **Explore the project thoroughly.** Read the files identified as new or changed in step 1 — entry points, configuration, READMEs, build files, directory listings — until you have a solid understanding of what this project does and how it is organized. Do not skim; do not guess. If the project is large, prioritize breadth first (directory structure, entry points, config) before depth (individual modules).

2. **Write the summary** following the structure below. Aim for **300-800 words total** depending on the project's complexity. A trivial single-file script may need 150 words; a multi-service monorepo may need 1000. Use your judgment, but err on the side of concise — every word costs tokens in future sessions.

3. **Save the summary** to `MEMORY_DIR/MEMORY.md`, where `MEMORY_DIR` is your project memory directory.

4. **If a previous summary already exists in `MEMORY.md`**, read it before exploring the codebase. Then:
   - **Preserve** sections that are still accurate — do not rewrite what hasn't changed.
   - **Update** sections where the codebase has diverged: new features, removed components, changed structure, shifted priorities.
   - **Remove** anything that is no longer true. Stale information is worse than missing information.
   - After updating, the result should read as a coherent whole — not a patchwork of old and new. Smooth over seams.
   - If the existing summary is so outdated that most of it needs rewriting, replace it entirely rather than editing line by line.

## Summary Structure

Use these sections. Omit any section that genuinely does not apply.

### Purpose
What this project does, who it is for, and what problem it solves. One to three sentences.

### Tech Stack
Languages, frameworks, key dependencies, and infrastructure (e.g., Docker, CI provider). Bullet list.

### Project Structure
Map of top-level directories and key files with a short description of each. Use a flat list or a tree — whichever is clearer. Only go one or two levels deep; do not enumerate every file.

### Architecture
How the main components interact. Data flow, entry points, key abstractions. Keep it to a short paragraph or a small diagram description. Skip this section for single-file projects.

### Conventions and Patterns
Naming conventions, code style, configuration patterns, or recurring idioms that someone working in the codebase should know. Only include things that are not obvious from the language/framework defaults.

### Current State
Active work, known issues, TODOs, or incomplete features visible in the code. Anything that a fresh session should be aware of so it does not repeat work or miss context. If there is nothing notable, say so in one line.

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
- **Do not** include: line-by-line code explanations, full API docs, dependency version numbers (unless critical), or implementation details that are obvious from reading the code.
- **Do not** pad with filler. If the project is simple, the summary should be short.
- **Do not** speculate. If something is unclear, note the ambiguity rather than guessing.
- **Always** regenerate the File Manifest from a fresh `find` — never copy timestamps from the previous manifest without verifying.
- **Exclude** `.claude_summary` from the File Manifest — it is the summary output itself and would always appear as changed, creating a self-referential loop.
