"""`cluster`'s command line — the operator surface.

Six subcommands, each the only entry point to one operation:

    create    instantiate a `.legoset` as a cluster (state + git worktrees)
    list      what clusters exist, and who is in them
    plan      the exact command sequence a launch would run — the review artifact
    script    write the container entrypoint's tmux script
    destroy   remove a cluster's worktrees and state
    launch    build the union image and run the cluster (--dry-run projects)

`launch` keeps the layering the earlier PoC note demanded: assembly lives in
`launching`, and the docker invocation itself in `docker_config`
(`ensure_image` + `run_cluster_container`) — this file only resolves, gates,
and dispatches.

Every subcommand prints a human-readable result and returns an exit code; none of
them raise on ordinary refusals (a name already taken, a project that is not a
git repo), because those are answers, not faults.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..file_access import write_text
from ..paths import (
    AGENTS_DIR, TMUX_CONF_IN_CONTAINER, cluster_banner_path,
    cluster_worktrees_dir,
)
from ..utils import shell_returncode
from . import launch_plan, state, tmux, worktree
from .legoset import (
    discover_templates, instantiate, load_legoset, validate,
)
from .member import ClusterError

EXIT_OK = 0
EXIT_REFUSED = 1        # understood and declined — not a crash


def build_parser() -> argparse.ArgumentParser:
    """The whole CLI surface. Subparsers rather than flags because the verbs take
    genuinely different arguments."""
    parser = argparse.ArgumentParser(
        prog="cluster", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    create = subs.add_parser("create", help="instantiate a template as a cluster")
    create.add_argument("session", help="the cluster's name (also its tmux session)")
    create.add_argument("project", help="the project directory all members work on")
    create.add_argument("--template", default="devteam",
                        help="which .legoset to instantiate (default: devteam)")
    create.add_argument("--worktrees", action="store_true",
                        help="give each member its own git worktree (interim "
                             "isolation; the eventual model is undecided — see "
                             "cluster_plan.md). Default: all members share the "
                             "one project checkout, which is NOT safe for "
                             "concurrent file work.")

    listing = subs.add_parser("list", help="what clusters exist")
    listing.add_argument("--templates", action="store_true",
                         help="list available .legoset templates instead")

    showing = subs.add_parser("plan", help="print the command sequence a launch would run")
    showing.add_argument("session")

    writing = subs.add_parser("script", help="write the entrypoint's tmux script")
    writing.add_argument("session")
    writing.add_argument("--out", help="where to write it (default: stdout)")

    removing = subs.add_parser("destroy", help="remove a cluster's worktrees and state")
    removing.add_argument("session")

    running = subs.add_parser(
        "launch", help="build the union image and run the cluster in one container")
    running.add_argument("session")
    running.add_argument("--dry-run", action="store_true",
                         help="assemble everything and print the docker "
                              "commands instead of running them")
    return parser


def main(argv: list[str]) -> int:
    """Parse argv and dispatch. Returns an exit code rather than calling sys.exit,
    so the entry script owns the process and tests can call this directly.

    `ClusterError` is caught here, once: every one of them is a definition
    problem with a message written for a person (a bad role, an unknown agent, a
    duplicate id), so printing it beats a traceback at every call site."""
    args = build_parser().parse_args(argv)
    handlers = {"create": _create, "list": _list, "plan": _plan,
                "script": _script, "destroy": _destroy, "launch": _launch}
    try:
        return handlers[args.command](args)
    except ClusterError as error:
        print(f"  Refusing: {error}")
        return EXIT_REFUSED


def _create(args: argparse.Namespace) -> int:
    """Instantiate a template: validate, persist state, then make the worktrees.

    State first, worktrees second, so a worktree failure leaves a cluster that
    `destroy` can clean up — the reverse order would leave orphan directories git
    knows about and we do not."""
    if state.exists(args.session):
        print(f"  Refusing: a cluster named '{args.session}' already exists. "
              f"`cluster destroy {args.session}` first, or pick another name.")
        return EXIT_REFUSED

    templates = discover_templates(AGENTS_DIR)
    if args.template not in templates:
        print(f"  Refusing: no template '{args.template}'. Available: "
              f"{', '.join(sorted(templates)) or '(none)'}")
        return EXIT_REFUSED
    template = load_legoset(templates[args.template])
    validate(template, _known_agents())

    project = Path(args.project).expanduser().resolve()
    # Each member inherits its agent's own `.lego` defaults before the cluster's
    # forced tags are merged in — so a member is its agent, plus cluster-ness.
    cluster = state.from_template(args.session, project,
                                  instantiate(template, AGENTS_DIR),
                                  template=template.name)

    refusal = worktree.check(project) if args.worktrees else None
    if refusal is not None:
        print(f"  Refusing: {refusal}")
        return EXIT_REFUSED

    state.save(cluster)
    print(f"  Cluster '{cluster.session}' — {len(cluster.members)} member(s) "
          f"from template '{template.name}', project {project}")
    for member in cluster.members:
        print(f"    {member.id:28} {member.agent}")

    if not args.worktrees:
        # Stated every time, not buried in docs: PoC-0's whole simplification is
        # that members share one checkout, and the launcher's edits are
        # read-then-write with no locking.
        print("    workspace: SHARED — all members see the one project at "
              "/workspace.")
        print("    Do not have two members edit files at the same time; their "
              "writes will clobber each other.")
        print("    (`--worktrees` gives each its own checkout meanwhile.)")
        return EXIT_OK
    return _make_worktrees(cluster)


def _make_worktrees(cluster: state.Cluster) -> int:
    """Create every member's worktree, skipping any that already exist."""
    trees = worktree.plan(cluster.session, cluster.ids,
                          cluster_worktrees_dir(cluster.session))
    existing = worktree.existing_worktrees(cluster.project)
    commands = worktree.add_commands(cluster.project, trees, existing)
    if not commands:
        print("    worktrees: all present already")
        return EXIT_OK
    print(f"    worktrees: creating {len(commands)}")
    for argv in commands:
        if shell_returncode(*argv) != 0:
            print(f"  A worktree command failed: {' '.join(argv)}")
            print("  The cluster's state is saved; fix the cause and re-run "
                  "`cluster create` (it skips the worktrees that exist).")
            return EXIT_REFUSED
    for tree in trees:
        print(f"      {tree.member:26} {tree.path}  [{tree.branch}]")
    return EXIT_OK


def _list(args: argparse.Namespace) -> int:
    """Clusters (default) or the templates they can be built from."""
    if args.templates:
        templates = discover_templates(AGENTS_DIR)
        if not templates:
            print("  no .legoset templates found")
            return EXIT_OK
        for name, path in sorted(templates.items()):
            members = load_legoset(path).members
            print(f"  {name:16} {len(members)} member(s): "
                  f"{', '.join(m.id for m in members)}")
        return EXIT_OK

    clusters = state.discover()
    if not clusters:
        print("  no clusters yet — `cluster create <name> <project>`")
        return EXIT_OK
    for cluster in clusters:
        print(f"\n  {cluster.session}  ({len(cluster.members)} members, "
              f"template {cluster.template or 'none'})")
        print(f"    project   {cluster.project}")
        for member in cluster.members:
            tree = cluster.worktree(member.id)
            mark = "" if tree.is_dir() else "   (no worktree)"
            print(f"    member    {member.id:26} {member.agent}{mark}")
    return EXIT_OK


def _plan(args: argparse.Namespace) -> int:
    """Print what a launch would do, without doing any of it.

    The PoC's main review artifact: it shows the mounts, the per-member windows
    with their env and cwd, and the literal tmux commands — so the parts that are
    not yet wired to docker can still be checked by reading."""
    cluster = _resolve(args.session)
    if cluster is None:
        return EXIT_REFUSED
    cluster = _picker_ordered(cluster)
    plan = launch_plan.build(cluster, personal_workspaces=_has_worktrees(cluster))

    print(f"  cluster '{plan.session}' — {len(plan.members)} member(s)")
    print(f"  project {cluster.project}\n")
    print("  mounts (host → container):")
    for source, target in plan.mounts().items():
        print(f"    {source} → {target}")
    print("\n  members:")
    for member in plan.members:
        print(f"    {member.member.id}")
        print(f"      cwd    {member.container_cwd}")
        print(f"      branch {member.worktree.branch}")
        print(f"      env    {', '.join(f'{k}={v}' for k, v in sorted(member.env.items()))}")
        print(f"      run    {' '.join(member.command)}")
    print("\n  tmux:")
    for argv in tmux.startup_argv(plan.session, plan.panes(),
                                  banner=plan.container_banner,
                                  shell_cwd=plan.container_shell_cwd):
        print(f"    {' '.join(argv)}")
    print(f"    {' '.join(tmux.attach_argv(plan.session))}")
    print("\n  (not shown: the image build + `docker run` — integration step; "
          "docker_config owns container assembly.)")
    return EXIT_OK


def _script(args: argparse.Namespace) -> int:
    """Emit the tmux startup script a container entrypoint would run.

    Also writes the banner file, because the script's status line reads it and a
    missing one would render an empty right-hand side on first attach."""
    cluster = _resolve(args.session)
    if cluster is None:
        return EXIT_REFUSED
    cluster = _picker_ordered(cluster)
    plan = launch_plan.build(cluster, personal_workspaces=_has_worktrees(cluster))
    # Written host-side, read container-side — two paths for one file.
    banner = cluster_banner_path(cluster.session)
    text = tmux.script(plan.session, plan.panes(), banner=plan.container_banner,
                       shell_cwd=plan.container_shell_cwd,
                       # The key policy (quit, help, mouse) lives in this file —
                       # without it a cluster session would have no quit binding.
                       user_conf=TMUX_CONF_IN_CONTAINER)

    write_text(banner, tmux.banner_text(cluster.ids, project=str(cluster.project)))
    if args.out:
        destination = Path(args.out).expanduser()
        write_text(destination, text)
        print(f"  wrote {destination} ({len(text.splitlines())} lines)")
        print(f"  banner at {banner}")
        return EXIT_OK
    print(text)
    return EXIT_OK


def _launch(args: argparse.Namespace) -> int:
    """Build the union image and run the cluster. The heavy lifting lives in
    `launching` (assembly) and `docker_config` (execution); this verb resolves,
    gates on docker, and dispatches. `--dry-run` rides docker_config's own
    projection: everything host-side (installs, script, banner) happens for
    real, and the docker commands print instead of running — same contract as
    `run.py --dry-run`."""
    from ..docker_config import require_docker, set_dry_run
    from ..tags import scan_all
    from . import launching
    cluster = _resolve(args.session)
    if cluster is None:
        return EXIT_REFUSED
    set_dry_run(args.dry_run)
    require_docker()
    launching.launch(cluster, scan_all(AGENTS_DIR))
    return EXIT_OK


def _destroy(args: argparse.Namespace) -> int:
    """Remove the worktrees, then the state — the one definition lives in
    `state.destroy` (the picker's Del shares it); this verb only narrates."""
    cluster = _resolve(args.session)
    if cluster is None:
        return EXIT_REFUSED
    state.destroy(cluster)
    print(f"  '{cluster.session}' destroyed — worktrees removed, state deleted. "
          f"Its branches (cluster/{cluster.session}/*) are kept.")
    return EXIT_OK


def _has_worktrees(cluster: state.Cluster) -> bool:
    """Whether this cluster was created with personal workspaces.

    Read from DISK (do the worktree dirs exist?) rather than stored in
    cluster.toml: the worktrees are the fact, and a stored flag could disagree
    with them after a manual cleanup. Cheap, and it makes `plan` describe the
    cluster as it actually is."""
    return any(cluster.worktree(identifier).is_dir() for identifier in cluster.ids)


def _picker_ordered(cluster: state.Cluster) -> state.Cluster:
    """The cluster with members in the DERIVED display/window order — what the
    real launch uses (launching reorders the same way), so these preview verbs
    never show a sequence the launch would then contradict."""
    import dataclasses
    from ..tags import scan_all
    return dataclasses.replace(
        cluster,
        members=state.picker_order(cluster.members, scan_all(AGENTS_DIR)))


def _resolve(session: str) -> state.Cluster | None:
    """The named cluster, or None having explained why not."""
    cluster = state.load(session)
    if cluster is None:
        print(f"  No cluster '{session}'. `cluster list` shows them.")
    return cluster


def _known_agents() -> frozenset[str]:
    """Every agent name a `.legoset` may reference — the `.md` files in the
    agents dir, which is the same index the picker builds its create-rows from.

    Read here rather than taken from a Registry because a template names AGENTS,
    not tags, and `agent_md_index` is the authority on which agents exist. It
    takes no argument and reads `paths.AGENTS_DIR` itself (cached per process)."""
    from ..file_access import agent_md_index
    return frozenset(agent_md_index())
