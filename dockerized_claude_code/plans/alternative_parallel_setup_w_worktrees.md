# Alternative: per-member git WORKTREES instead of clones

The road not taken, kept because it is genuinely viable and cheap to switch to.

Cluster mode gives each member its own checkout of the shared project so N
cohabiting agents do not clobber one working tree. **Local clones won** (see
`cluster_plan.md`), but worktrees were measured to work too, and this file records
exactly how — so choosing them later is a build, not a re-investigation.

Everything below was measured on **git 2.47.3**, 2026-08-12. Nothing here is
inferred from documentation.

---

## The one thing that makes it work

A worktree is **not self-contained**. Its `.git` is a *file* holding a path back
to the parent repo's object store:

```
gitdir: /host/path/project/.git/worktrees/alpha
```

That path is absolute, so mounting a worktree into a container at a different
path breaks git outright — `fatal: not a git repository`. Two facts rescue it:

1. **Put the worktrees INSIDE the project** (`<project>/.cluster-worktrees/<member>/`)
   and mount the project *whole* at `/workspace`, so the parent `.git` travels
   with them and is reachable at a predictable place.
2. **Run `git worktree repair <paths…>` after the mount.** Passing the new paths
   is essential — bare `git worktree repair` does nothing, because it cannot find
   worktrees whose administrative data still names the old location.

Measured: relocate a repo to a different absolute path, run
`git worktree repair .cluster-worktrees/alpha .cluster-worktrees/beta`, and the
result is healthy — `git worktree list` shows correct paths, nothing is
`prunable`, and commits work in every worktree.

## Essential steps

Host side, at cluster creation:

```bash
# once per member, from the project root
git -C <project> worktree add -b cluster/<session>/<member> .cluster-worktrees/<member>
echo '.cluster-worktrees/' >> <project>/.git/info/exclude     # keep it out of the user's index
```

Container side, in the entrypoint, BEFORE any member starts:

```sh
# /workspace is the project, mounted whole (with its .git)
cd /workspace && git worktree repair .cluster-worktrees/*
```

Each member's cwd is then `/workspace/.cluster-worktrees/<member-id>/`, and its
branch is `cluster/<session>/<member-id>`.

Teardown (host side): `git worktree remove --force <path>` per member, then
`git worktree prune`. Branches survive — committed work stays reachable under
`cluster/<session>/*`.

## What else was measured, so nobody re-tests it

- **Files are genuinely separate.** Different inodes; an edit in one worktree
  leaves the others and the main checkout untouched. Worktrees do isolate.
- **The forward pointer tolerates a relative path.** Hand-writing
  `gitdir: ../../.git/worktrees/<name>` works on 2.47.3 and survives relocation,
  so the `--relative-paths` flag (git 2.48+) is not required.
- **The reverse pointer does NOT.** `.git/worktrees/<name>/gitdir` must be
  absolute; a relative value makes git report *"gitdir file points to
  non-existent location"* and mark the worktree `prunable`. This is why `repair`
  is the mechanism rather than a pointer hack.
- **`git gc` does not silently prune worktrees.** Only an explicit
  `git worktree prune` removes the administrative entries, so a member running
  ordinary git operations cannot destroy its siblings' checkouts.
- **Two worktrees cannot share a branch** — `fatal: 'alpha' is already used by
  worktree at …`. Fine for this design (each member wants its own branch), but a
  hard rule rather than a convention.

## Trade-offs against local clones

| | Worktrees | Local clones (chosen) |
|---|---|---|
| Progress inside the user's project | yes | yes (`.cluster-clones/`) |
| Disk | one history | hardlinked ≈ the same |
| Container portability | needs `git worktree repair` after every mount | works at any path, no step |
| Integration between members | commits instantly visible; local merge | explicit `push`/`pull`, project as `origin` |
| Failure mode | stale pointers read as `prunable`; valid for one path at a time | none of this class |
| Branch freedom | each member MUST hold a different branch | members may share a branch name |
| Blast radius | one object store: a corruption or aggressive gc touches everyone | independent object stores |

### Advantages worktrees keep

- **No sync step to see a sibling's work.** A commit in one member is
  immediately readable from every other member and from the main checkout,
  because they share one object database. With clones a `fetch` is required.
- **Exactly one history on disk**, with no reliance on hardlinking behaving.
- **Cheap teardown**: removing a worktree cannot orphan objects the others need.
- **The user's project is the single repo.** No second repository to reason
  about, no `origin` to configure, nothing to explain about where a member's
  commits "really" live.

### Why clones were chosen anyway

The deciding difference is **one path at a time**. A worktree's administrative
pointers are absolute, so they are correct for the host *or* for the container,
never both — every transition needs a `repair`, and the failure mode when someone
forgets is a confusing `prunable` state rather than a clean error. Clones have no
such duality, and the disk advantage that would have justified the complexity
mostly evaporated once local clones were measured to hardlink their objects
(verified: identical inodes, and a clone still works after its source is deleted
outright).

Concurrency was checked rather than assumed: 40 commits made simultaneously
across two hardlinked clones left `git fsck` clean on all three repositories.
Git objects are immutable and content-addressed, so hardlinks only ever share
already-written data, and every piece of mutable state (refs, index, `HEAD`,
config, logs) is per-clone with its own inode.

> **If clones are ever revisited, the one rule that matters:** never use
> `git clone --shared` / `--reference`. Those create an `objects/info/alternates`
> file that *borrows* the source's object store — a real coupling, where a `gc`
> in the source can orphan objects the borrower needs. A plain local clone
> hardlinks instead and writes no `alternates` file (verified absent). The safety
> argument above applies to hardlinks only.

## Switching cost

Small, by construction. The cluster package keeps the mechanism behind
`launch/cluster/worktree.py` (already written and tested) plus one flag on
`launch_plan.build`. A switch means pointing the create/teardown paths at it and
adding the `repair` line to the entrypoint; nothing in `member`, `legoset`,
`state`, `tmux`, or the CLI cares which mechanism produced the directories.
