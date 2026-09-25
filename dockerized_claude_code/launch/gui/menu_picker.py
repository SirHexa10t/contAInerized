"""The launcher's MENUS (launch/gui): what rows the user is offered, what
each key does to them, and what a pick means.

Three of them. Two run on `picker_widget.pick_with_preview`; `--stop` is a
multi-select, which the picker does not do, so it runs on the forms'
`form_core.checkbox_form` and wears the picker's row anatomy over it:

  select_agent(registry)
      The main menu: creatable agents, their continuable instances nested
      beneath; then every cluster template, then every existing cluster as a
      top-level row with its members nested beneath (a cluster is not an
      instance of its template — nothing is shared after creation, so it does
      not nest under it); plus the "(Edit Preferences)" and "(Move onto
      deletions menu)" openers.
      Runs until the user picks something or cancels; handles the deletion
      submenu and the profile forms internally.
      -> Agent (new) | Instance (cont) | Cluster | None on cancel/empty

  prompt_stop(registry)
      `run.py --stop`'s multi-select over RUNNING containers.
      -> the picked docker ids, or None when nothing is running

  _delete_submenu(registry, legend_text)
      The nested destructive menu, reachable only from select_agent.

What this module knows that the widget does not: agents, instances,
clusters, which rows may be selected, what a row's preview should say, and
the F8 composition legend. Row rendering, cursor movement and the selection
loop are `picker_widget`'s — this module was 1541 lines holding both until
they were split on 2026-09-03.

The rows for EXISTING things — instances, clusters, `--stop`'s — share one
anatomy and one width pass (`_RowLayout`), and one reading of their
workspace (`_CwdContext`, resolved once per menu build), which is what lines
their paths up in a column and gives every kind the same CURRENT / DEFAULT /
INVALID hints (2026-09-09; cluster rows had their own anatomy and no hints
before). Row data comes from two factories: `continuable_instances` for instances and
`cluster_entries` for clusters, whose members are Instances too
(`Cluster.member_instance`) and so get the instance rows' deferred previews.

Row builders and state lookups come from `agents_crud`; the flows each key
triggers live in `picker_flows`; line prompts and inline dialogs in
`picker_prompts`; preview text in `picker_previews`. Every *form* lives in
`forms.py` / `cluster_form.py` — this module only opens them.

(The pre-launch banner lives in claude_code_config.print_launch_banner —
launch-stage output, not picker UI.)
"""

import dataclasses
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from rich import box                                                       # dep — declared in pyproject.toml [project]
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from ..agents_crud import (
    creatable_agents, delete_instance, instance_from_store, invalid_tags_report,
    list_all_instances, modify_instance,
)
from ..cluster import state as cluster_state
from ..cluster.legoset import (
    discover_templates, load_legoset,
    validate,
)
from ..cluster.member import ClusterError, Member
from ..docker_config import (
    cluster_container_id, docker_running_instances_subprocess,
)
from ..file_access import (
    expand_user_path, is_dir, read_text,
    resolved_cwd, resolved_path,
)
from ..paths import (
    AGENTS_COMMANDS_DIR, AGENTS_DIR, DEFAULT_WORKSPACE, DEFAULTING_DIRS,
)
from .picker_previews import (
    _ansi, _cluster_preview, _create_preview, _member_line, _resolve_tags,
    _template_preview,
)
from .picker_flows import (
    _create_cluster_flow, _destroy_cluster_flow, _edit_cluster_flow,
    _edit_member_flow, _remove_member_flow,
)
from .picker_widget import (
    ContEntry, MemberEntry, PickerAction, PickerCwdHint, PickerEntry,
    PickerRowMarker, WorkspaceView, _cont_tags_column, _deferred_preview,
    _tags_column, break_row, pick_with_preview,
)
from .picker_prompts import (
    _agent_description, ask_for_find_term, confirm_dialog,
    instance_fields, _report_to_picker,
)
from .form_core import FormOption, checkbox_form
from ..history_find import find_in_history
from .forms import edit_profiles_menu, prompt_tags
from .styles import (
    rich_style, STYLE_AGENT_NAME, STYLE_TAG_INVALID, tag_style,
)
from ..tags import Agent, Ai, Engine, Harness, Instance, Registry, SCOPES, Tag, resolve_build
from ..tags.ai import sorted_ais
from ..tags.engine import sorted_engines, effort_tier_rank
from ..tags.harness import sorted_harnesses
from ..utils import ordering_index_or_end, relative_time


# ============================================================
# Agent-picker UI strings
# ============================================================

# The confirm prompt's wording + accepted answers live in `picker_prompts`,
# which owns every line-prompt this module opens. Style class names + their
# style strings live as the UiClass enum in styles (shared by every form and
# this picker). Row marker glyphs + their styles live on picker_widget's
# PickerRowMarker; the cwd-relation labels ("(CURRENT DIR) " / "(DEFAULT DIR) ")
# on PickerCwdHint there too.

TITLE_AGENT_PICKER = "Select an agent:"
TITLE_DELETE_MENU  = "‼️  DELETE AGENT INSTANCES  ‼️"

PREFERENCES_LABEL  = "(Edit Preferences)"
DELMENU_LABEL  = "(Move onto deletions menu)"
BACK_LABEL     = "(Move back to Agent Selection)"
PREFERENCES_PREVIEW = ("One merged form, one section per profile file. Toolkits: which language "
                    "toolchains a configurable profession's shared image installs (today: [code]'s "
                    "Rust / Node / CMake) — edits ~/.ai-agents/<profession>_profile.toml; a "
                    "changed toggle rebuilds only that tool's Docker layer on the next launch. "
                    "UI configs: launcher preferences — the {mux} backend, herdr vs tmux — edits "
                    "~/.ai-agents/ui_profile.toml, read at every launch. Service CLIs "
                    "(gh, gcloud, aws, ...) are not chosen here — they install when matching creds "
                    "exist under user_extras/optional_creds/.")
DELMENU_PREVIEW = "Open the deletion sub-menu to remove agent instances and their state directories."
BACK_PREVIEW    = "Return to the main agent picker."
CONFIRM_DELETE_FMT = "Delete '{name}'?"
STOP_FORM_TITLE = "Stop running containers  (Space to mark, Enter to stop):"
# Said ONCE, above the list, rather than on each row's body: it is true of
# every row (it was on the instance rows only, so whoever stopped a cluster
# never read it) and it does not change with the highlight.
STOP_FORM_PREAMBLE = [
    "# Stopping ends the CONTAINER only. State dirs and conversations live",
    "# host-side, so the next launch of each one resumes where it left off.",
]

# ============================================================
# Agent-picker styles (inline, applied per-segment)
# ============================================================

STYLE_MEMBER_COUNT    = "fg:ansigreen"    # the "(N members)" column on a template row and a cluster row
STYLE_DEL_NAME       = "bold fg:ansired"

# A running instance's row is information-only (see PickerEntry.selectable):
# the name greys out to read as unavailable, and the red tag is what draws the
# eye. Emitted conditionally like the PickerCwdHint labels — no reserved
# column, so non-running rows keep their tighter spacing.
STYLE_RUNNING_NAME   = "fg:ansibrightblack"                      # grey — this instance can't be launched right now
RUNNING_HINT         = ("bold fg:ansibrightred", "(RUNNING) ")   # (style, label) fragment, same shape as PickerCwdHint.fragment

NO_WORKSPACE_DISPLAY = "?"            # subtitle placeholder when a Cont row's store entry is missing or stale

# Every variable-width piece of a row is measured as it is built, because a
# fragment list's printable width is not len() of anything: `(fragments, its
# printable width)`. NO_COLUMN is the empty one — safe to share, as nothing
# ever appends to a column in place.
Column = tuple[list[tuple[str, str]], int]
NO_COLUMN: Column = ([], 0)

# Sentinel entry values signalling "open the delete submenu" / "open the
# toolkits editor" — used in the main picker where most rows hold an
# identity dataclass; these are the non-identity rows, so distinct
# singletons let the dispatcher match by `is` rather than tagging
# identities with extra metadata.
_OPEN_DELMENU = object()
_OPEN_PREFERENCES = object()


@dataclasses.dataclass(frozen=True)
class _ClusterTemplateRow:
    """A cluster-template row's value: which `.legoset` to open the membership
    form on. A dataclass rather than a singleton because there is one row PER
    template — the dispatcher matches by type, then reads the path."""
    name: str
    path: Path


@dataclasses.dataclass(frozen=True)
class _ClusterRow:
    """An EXISTING cluster's row value. Carries only the session name: every
    handler reloads the cluster from disk, so a row built before some other
    handler mutated the cluster cannot act on a stale member list."""
    session: str


@dataclasses.dataclass(frozen=True)
class _MemberRow:
    """One member's row value — the unit the picker EDITS (F2 re-tags, Del
    removes from the cluster). Enter does NOTHING on it (`pickable=False`): a
    member launches with its cluster, and the pause that used to explain so
    told the operator nothing new (2026-09-09). Session + member id; same
    reload-on-act rule as _ClusterRow."""
    session: str
    member_id: str


# ============================================================
# Row data — where the launcher was invoked from, and what exists
# ============================================================

@dataclasses.dataclass(frozen=True)
class _CwdContext:
    """The launch-site facts every row's workspace is judged against —
    resolved ONCE per menu build, not per row: where the launcher was invoked
    from, what "the default workspace" means right now, and whether that
    default applies at all (it does only when the cwd is one of the neutral
    DEFAULTING_DIRS, e.g. $HOME — being in a project under $HOME doesn't make
    /ai_workspace your default). Symlinks are resolved so e.g. /home/<user>
    matches /var/users/<user> when one links to the other; subdirectories
    deliberately don't count."""
    cwd: Path
    default_workspace: Path
    defaulting: bool

    @classmethod
    def here(cls) -> "_CwdContext":
        cwd = resolved_cwd()
        return cls(cwd=cwd, default_workspace=resolved_path(DEFAULT_WORKSPACE),
                   defaulting=cwd in {resolved_path(d) for d in DEFAULTING_DIRS})

    def view(self, workspace: str | Path | None) -> WorkspaceView:
        """How `workspace` reads on a row: the stored value as-is (the `?`
        placeholder when nothing is stored) and its relation to here —
        CURRENT when it IS the cwd, DEFAULT when it is the default workspace
        and the default applies, INVALID when the stored path no longer
        exists / isn't a directory (still shown, so it can be spotted and
        repointed with F2), else no hint."""
        if not workspace:
            return WorkspaceView(NO_WORKSPACE_DISPLAY, None)
        resolved = resolved_path(workspace) if is_dir(workspace) else None
        hint: PickerCwdHint | None
        if resolved is None:
            hint = PickerCwdHint.INVALID
        elif resolved == self.cwd:
            hint = PickerCwdHint.CURRENT
        elif self.defaulting and resolved == self.default_workspace:
            hint = PickerCwdHint.DEFAULT
        else:
            hint = None
        return WorkspaceView(str(workspace), hint)


def _last_used_display(mtime: float | None) -> str:
    """The `Last used` value: a relative time, or `(never)` while there is no
    history yet. Instances, members and clusters all say it this way."""
    return relative_time(mtime) if mtime is not None else "(never)"


def continuable_instances(registry: Registry,
                          running: frozenset[str] | None = None,
                          here: "_CwdContext | None" = None) -> list[ContEntry]:
    """ContEntry list for the picker's Cont/DELETE rows. Orphans (missing .md)
    skipped — instance_from_store returns None for those. Sorted by active
    tag set (tag-less first, then registry order: specialties dominate,
    professions next), then engine capability, then agent/session. Each
    entry's workspace is read against `here` (the launch site — see
    `_CwdContext`; None resolves it now). The contained Instance is what the
    picker hands back on selection — stored workspace + resolved tag objects
    baked in so the modify flow's pre-fill reads straight off the identity.
    A store entry naming an unknown tag fails fast here (validate_build
    raises) — `python -m launch.audit` reports the same defect non-fatally
    when the picker is the wrong place to crash on a typo.

    Also flags which instances are running right now (`is_running`). `running`
    is the docker_running_instances_subprocess snapshot to flag from — pass it
    when the caller already probed (select_agent shares ONE `docker ps` per
    menu build between these rows and the cluster rows); None probes here, so
    the marks still refresh per menu rebuild (including on return from the
    delete / toolkits submenus) without costing a subprocess per keystroke. An
    undeterminable docker state marks nothing, deliberately: over-flagging
    would wrongly lock rows the user can actually launch."""
    here = here or _CwdContext.here()
    if running is None:
        running = docker_running_instances_subprocess() or frozenset()   # None (can't tell) → flag nothing

    out = []
    for dir_name in list_all_instances():
        inst = instance_from_store(dir_name, registry)
        if inst is None:
            continue
        out.append(ContEntry(
            identity=inst,
            workspace=here.view(inst.workspace),   # the stored value even when invalid; `?` only when no entry at all
            last_used_display=_last_used_display(inst.last_used_mtime),
            is_running=dir_name in running,
        ))

    spec_order, prof_order = list(registry.specialties), list(registry.professions)

    def cont_sort_key(e: ContEntry) -> tuple[Any, ...]:
        i = e.identity
        return (
            tuple(sorted(ordering_index_or_end(s.name, spec_order) for s in i.specialties)),
            tuple(sorted(ordering_index_or_end(p.name, prof_order) for p in i.professions)),
            -effort_tier_rank(i.engine),
            i.agent,
            i.session,
        )

    out.sort(key=cont_sort_key)
    return out


@dataclasses.dataclass(frozen=True)
class ClusterEntry:
    """An EXISTING cluster's row data — the cluster-shaped twin of ContEntry
    (the same workspace view, last-used and running facts, so a cluster row
    wears the Cont-row anatomy) plus its members as rows of their own: a
    `MemberEntry` per member whose agent still exists, in picker order, and
    the `Member` records whose agent `.md` is gone (`missing`) — shown as red
    rows, never dropped: a member the listing hides is the silently-degraded
    peer the launch refuses. The preview is eager (no transcript in it) and
    already rendered."""
    cluster: cluster_state.Cluster
    members: tuple[MemberEntry, ...]
    missing: tuple[Member, ...]
    workspace: WorkspaceView
    last_used_display: str
    is_running: bool
    preview: str


def cluster_entries(registry: Registry, running: frozenset[str],
                    here: _CwdContext) -> list[ClusterEntry]:
    """ClusterEntry list for the picker's cluster rows (and `--stop`'s), one
    per cluster on disk, session-sorted (`cluster_state.discover`). Each
    member becomes the Instance it launches as (`Cluster.member_instance`) —
    what gives member rows the instance rows' deferred `Last used` / `Last
    prompt` previews for free — or lands in `missing` when its agent is gone.
    A cluster's `Last used` is its newest member's; display only, the order
    stays session-sorted."""
    out: list[ClusterEntry] = []
    for cluster in cluster_state.discover():
        inherited = frozenset({*cluster.tags.professions, *cluster.tags.specialties,
                               *cluster.tags.policies})
        is_running = cluster_container_id(cluster.session) in running
        workspace = here.view(cluster.project)
        members: list[MemberEntry] = []
        missing: list[Member] = []
        for member in cluster_state.picker_order(cluster.members, registry):
            inst = cluster.member_instance(member, registry)
            if inst is None:
                missing.append(member)
                continue
            members.append(MemberEntry(
                identity=inst, workspace=workspace,
                last_used_display=_last_used_display(inst.last_used_mtime),
                is_running=is_running,
                member=member, cluster=cluster.session, inherited=inherited))
        # The cluster pane lists each member with its OWN tags (the inherited
        # ones are the pane's tag list already) and its own last use.
        lines = [_member_line(entry.member.id, entry.identity.ai, entry.identity.harness, entry.identity.engine,
                              [t for t in entry.identity.active_tags if t.name not in inherited],
                              [p for p in entry.identity.invalid_tags if p.name not in inherited],
                              entry.last_used_display)
                 for entry in members]
        lines += [_member_line(member.id, None, None, None, [], [], "", missing_agent=member.agent)
                  for member in missing]
        tags, problems = _resolve_tags(registry, cluster.tags, scope="cluster")
        last_used = _last_used_display(cluster.last_used_mtime)
        out.append(ClusterEntry(
            cluster=cluster, members=tuple(members), missing=tuple(missing),
            workspace=workspace, last_used_display=last_used, is_running=is_running,
            preview=_cluster_preview(cluster, tags, problems, last_used, lines)))
    return out


# ============================================================
# Row anatomy — what every row for an EXISTING thing looks like
# ============================================================

def _runtime_column(ai: Ai | None, harness: Harness | None) -> Column:
    """The runtime column of a row — the AI's ⟪label⟫ in its own colours,
    then the harness's ⟦label⟧ (operator, 2026-09-14), each followed by a
    space — as (fragments, width); NO_COLUMN for a row with neither (a cluster
    row: its members carry theirs; an agent row: both are chosen per
    instance). Sits between the tag column and the name (operator,
    2026-09-13), padded per population like the tags."""
    frags: list[tuple[str, str]] = []
    width = 0
    for tag in (ai, harness):
        if tag is not None:
            frags += [(tag_style(tag), tag.label), ("", " ")]
            width += len(tag.label) + 1
    return frags, width


@dataclasses.dataclass(frozen=True)
class _RowCells:
    """The variable-width pieces of ONE row: its tag column and its runtime
    column, each as the `(fragments, width)` pair the column builders
    return. A row with neither (`--stop`'s stray containers) still gets a
    cells object, so its name lands in the same column as everyone else's."""
    column: Column = NO_COLUMN
    runtime: Column = NO_COLUMN


@dataclasses.dataclass(frozen=True)
class _RowLayout:
    """The widths one POPULATION of rows is padded to, and the anatomy every
    row for an existing thing wears — an instance, a cluster, `--stop`'s
    rows: lead · tag column · runtime column (AI, harness) · name · a gap ·
    `(RUNNING)` · the cwd hint · the workspace path. One definition
    (2026-09-09) is what lines the paths up in a column whatever the row
    kind, and what gave cluster rows the hints instance rows had; the widths
    joined it on 2026-09-18, because computing them was four steps every
    caller was spelling out for itself.

    WHAT SHARES A POPULATION IS THE CALLER'S CALL, and the two menus answer
    it differently on purpose. The main menu pads each kind against ITSELF —
    tying agent rows to instance rows once pushed agent names way out to
    align with the widest instance tag set, even though the two never share
    a row, and its cluster rows sit in blocks of their own anyway. `--stop`
    pads every running thing TOGETHER, because there they are one flat
    checkbox list, where a name column that restarts halfway down reads as a
    rendering fault."""
    column_width: int
    runtime_width: int
    name_width: int

    @classmethod
    def over(cls, *populations: dict[str, _RowCells]) -> "_RowLayout":
        """The layout for every row in `populations` — pass one dict per kind
        that must line up with the others, keyed by the name each row shows."""
        cells = [c for population in populations for c in population.values()]
        names = [n for population in populations for n in population]
        return cls(column_width=max((c.column[1] for c in cells), default=0),
                   runtime_width=max((c.runtime[1] for c in cells), default=0),
                   name_width=max((len(name) for name in names), default=0))

    def row(self, lead: list[tuple[str, str]], cells: _RowCells, name: str, *,
            workspace: WorkspaceView, inert: bool, running_hint: bool,
            ) -> list[tuple[str, str]]:
        """One row, padded to this layout. The two flags are INDEPENDENT and
        both used to hang off one `running` argument: `inert` greys the name
        (this row is information-only — the picker's `selectable=False`
        rows), while `running_hint` appends the red `(RUNNING)` label.
        `--stop` wants the label gone and the names live, and said so by
        claiming its containers were not running (2026-09-18)."""
        frags, width = cells.column
        run_frags, run_len = cells.runtime
        out = [*lead, *frags, ("", " " * (self.column_width - width)),
               *run_frags, ("", " " * (self.runtime_width - run_len)),
               (STYLE_RUNNING_NAME if inert else STYLE_AGENT_NAME, f"{name:<{self.name_width}}"),
               ("", "    ")]
        if running_hint:
            out.append(RUNNING_HINT)
        return out + workspace.fragments


def _instance_cells(entries: Iterable[ContEntry], *,
                    emphasize: frozenset[str] = frozenset()) -> dict[str, _RowCells]:
    """Instance rows' cells, keyed by instance id: the instance's resolved
    tags (with `emphasize` for the one the menu is about) and its AI +
    harness."""
    return {e.identity.instance: _RowCells(_cont_tags_column(e.identity, emphasize=emphasize),
                                           _runtime_column(e.identity.ai, e.identity.harness))
            for e in entries}


def _cluster_cells(registry: Registry,
                   entries: Iterable[ClusterEntry]) -> dict[str, _RowCells]:
    """Cluster rows' cells, keyed by session. No runtime column: which AI and
    CLI run is a per-MEMBER fact, and the member rows carry it."""
    return {c.cluster.session: _RowCells(_cluster_column(registry, c)) for c in entries}


def _bare_cells(names: Iterable[str]) -> dict[str, _RowCells]:
    """Cells for rows nothing is known about but their name — `--stop`'s
    stray containers. They still join the width pass, so a stray's id lines
    up with the names above it instead of starting at the margin."""
    return {name: _RowCells() for name in names}


def _cluster_column(registry: Registry, entry: ClusterEntry) -> Column:
    """A cluster row's tag column: the tags the cluster forces on every member
    (`{cc}` emphasized — the one that changes how the team works; an
    unresolvable name in the alert style), then the member count in the
    template rows' green. Sits where instance rows show their tags, so the
    two kinds line up."""
    tags, problems = _resolve_tags(registry, entry.cluster.tags, scope="cluster")
    frags, width = _tags_column(tags, emphasize=frozenset({"cluster-cowork"}),
                                problems=problems)
    count = f"({len(entry.cluster.members)} members)"
    return [*frags, (STYLE_MEMBER_COUNT, count), ("", " ")], width + len(count) + 1


# ============================================================
# The F8 composition legend
# ============================================================

def _tag_commands(registry: Registry) -> list[tuple[Tag, str, str]]:
    """`(tag, /command, description)` for every slash command a TAG grants.

    Read from the declarations rather than any directory: a tag names its
    commands in tag.info (`commands = [...]`) and the files live centrally in
    `agents/_commands/` — one findable place, and one file grantable by several
    tags. The registry has already validated that every declared name resolves
    to a real file, so the read here cannot miss. Any KIND may declare —
    `{manager}` and `[self]` both do — which is why this iterates all four.

    The description comes from the command file's own `description:` frontmatter —
    the same line Claude Code shows in its `/help` — so the legend cannot drift
    from what the command says about itself. A file without one is listed anyway
    with an empty description; silently hiding it would be worse than a blank cell.
    """
    return [(tag, f"/{name}",
             _frontmatter_description(AGENTS_COMMANDS_DIR / f"{name}.md"))
            for tag in registry.get_all() for name in tag.commands]


def _frontmatter_description(md: Path) -> str:
    """The `description:` line from a command file's YAML frontmatter, or "".

    Deliberately a line scan rather than a YAML parse: the frontmatter here is
    two or three flat keys, and a dependency (or a hand-rolled parser) for one
    field would be more to maintain than to read."""
    for line in read_text(md).splitlines()[:12]:
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


def _build_composition_legend(registry: Registry) -> str:
    """Build the F8 'composition legend' shown over the preview pane — one
    table per tag kind, header = the kind's nutshell, rows = each discovered
    member's underlined fullname + short description (the fullname spells
    out what the shortname abbreviates). Built with rich Table objects and
    rich styles — injecting raw ANSI into markdown-table source made rich
    count escape bytes as width and misalign the columns."""
    parts: list[Any] = []
    sections: list[tuple[str, str, str, Iterable[Tag]]] = [
        ("AIs",         "AI",         "Which AI runs the agent — what each is for, coloured after its logo.", sorted_ais(registry.ais.values())),
        ("Harnesses",   "Harness",    "Which agent CLI wraps the AI — the program the container runs; each names the AIs it runs.",
         sorted_harnesses(registry.harnesses.values(), registry.default_ai.name if registry.default_ai else None)),
        ("Engines",     "Engine",     "How hard the agent thinks — a capability standard plus switches (most capable first).", sorted_engines(registry.engines.values())),
        ("Professions", "Profession", "Tools it can use — each is a docker image layer.", registry.professions.values()),
        ("Specialties", "Specialty",  "Exceptional access or running conditions.", registry.specialties.values()),
        # Policies sort by shortname WITH its symbol (`!` < `+` < `-`), so
        # same-stance policies group: demands, grants, denials.
        ("Policies",    "Policy",     "What it's permitted to do — orange grants, blue denies, white demands.",
         sorted(registry.policies.values(), key=lambda t: t.shortname)),
    ]
    for title, singular, nutshell, members in sections:
        table = Table(box=box.SIMPLE_HEAD, header_style="cyan", pad_edge=False)
        table.add_column(singular)
        table.add_column("Description")
        if singular == "Engine":
            table.add_column("Pinned model")   # the engine's words name no model; the default AI's tier for its standard sits beside them
        for t in members:
            cells = [Text(t.label, style=rich_style(tag_style(t))),
                     Text.assemble((t.fullname, "underline"), f": {t.short_description}")]
            if isinstance(t, Harness):
                cells[1].append("  · runs " + " ".join(registry.ais[a].label for a in t.ais if a in registry.ais), style="dim")
            if t.forbid_on:   # where a .lego author may not put it — the same fact the form greys out
                cells[1].append(f"  · not on: {', '.join(s for s in SCOPES if s in t.forbid_on)}", style="dim")
            if isinstance(t, Engine):
                default_ai = registry.default_ai
                cells.append(Text(default_ai.tier(t.budget.effort_tier).model if default_ai and t.budget.effort_tier else "", style="dim"))   # "" for an engine with no standard
            table.add_row(*cells)
        parts += [Markdown(f"# {title}\n\n{nutshell}"), Text(), table]

    # Commands a TAG grants, if any. Omitted entirely when none do, rather than
    # printing an empty table that implies the feature is broken. "Tag", not
    # "Specialty": any kind may declare (`[self]` is a profession).
    if commands := _tag_commands(registry):
        table = Table(box=box.SIMPLE_HEAD, header_style="cyan", pad_edge=False)
        table.add_column("Tag")
        table.add_column("Command")
        table.add_column("Description")
        for tag, command, description in commands:
            table.add_row(Text(tag.label, style=rich_style(tag_style(tag))),
                          Text(command, style="bold"),
                          Text(description))
        parts += [Markdown(
            "# Tag Commands\n\n"
            "Slash commands that arrive WITH a tag — present only in instances "
            "carrying a granting tag, unlike the shared commands every agent gets."),
            Text(), table]
    return _ansi(*parts)


# ============================================================
# The menus
# ============================================================

def prompt_stop(registry: Registry) -> list[str] | None:
    """The `--stop` selector: every RUNNING instance and cluster as a checkbox
    row wearing the picker's own Cont-row anatomy (`_RowLayout.row`) — tags ·
    name · cwd hint · workspace — minus the `(RUNNING)` hint, which would say
    nothing in a list that is running by definition, and with the names left
    live rather than greyed, because here they are what the user is reaching
    for. `{muxer}` is emphasized on INSTANCE rows: a sticky instance is this
    flag's reason to exist (a muxer container outlives its terminal, so this
    list is how one is ended without re-attaching). Cluster rows do not call
    it out — `muxer` is in `cluster_state.LOCKED_SPECIALTIES`, so every
    cluster carries it and the emphasis would mark them all.

    Unlike the main menu, every row here is padded against every other: this
    is one flat list rather than nested blocks, so instances, clusters and
    strays share one name column (see `_RowLayout`).

    Returns the picked docker ids, CONTAINER_NAME_PREFIX already stripped
    (the running-snapshot's spelling), OR None when nothing is running at
    all — the distinction the caller needs, since an empty list also means
    "the form opened and the user cancelled or ticked nothing", and only one
    of those two deserves to be told that nothing was running (the
    `docker_running_instances_subprocess` tri-state rule, one layer up).

    A running id that matches no store entry and no cluster still gets a bare
    row (id only): a stray is exactly what someone reaching for --stop most
    needs to be able to stop."""
    running = docker_running_instances_subprocess() or frozenset()
    here = _CwdContext.here()

    live = [e for e in continuable_instances(registry, running, here) if e.is_running]
    live_clusters = [c for c in cluster_entries(registry, running, here) if c.is_running]
    known = ({e.identity.instance for e in live}
             | {cluster_container_id(c.cluster.session) for c in live_clusters})
    strays = sorted(running - known)
    if not (live or live_clusters or strays):
        return None                     # nothing to offer — never open an empty form

    instances, clusters = (_instance_cells(live, emphasize=frozenset({"muxer"})),
                           _cluster_cells(registry, live_clusters))
    bare = _bare_cells(strays)
    layout = _RowLayout.over(instances, clusters, bare)

    options = [
        FormOption(key=entry.identity.instance,
                   label=layout.row([], instances[entry.identity.instance],
                                    entry.identity.instance,
                                    workspace=entry.workspace,
                                    inert=False, running_hint=False),
                   body=[("", f"last used {entry.last_used_display}")])
        for entry in live]
    options += [
        FormOption(key=cluster_container_id(entry.cluster.session),
                   label=layout.row([], clusters[entry.cluster.session],
                                    entry.cluster.session,
                                    workspace=entry.workspace,
                                    inert=False, running_hint=False),
                   body=[("", f"last used {entry.last_used_display}   ·   members: "
                              + ", ".join(entry.cluster.ids))])
        for entry in live_clusters]
    options += [
        FormOption(key=stray,
                   label=layout.row([], bare[stray], stray,
                                    workspace=WorkspaceView("", None),
                                    inert=False, running_hint=False),
                   body=[("", "a running launcher container with no store entry — "
                              "stoppable, not otherwise known here")])
        for stray in strays]

    result = checkbox_form(STOP_FORM_TITLE, options, preamble=STOP_FORM_PREAMBLE)
    return [] if result is None else result.checked      # Esc — stop nothing


def _hit_counts(term: str) -> dict[Path, int]:
    """`{state dir: turns that said it}` for alt+f — the picker's view of a
    search, which is a count per ROW rather than the quoted turns `--find`
    prints. The quotes have nowhere to go here: the picker owns the whole
    screen, so anything printed behind it is hidden until it closes."""
    return {found.state_dir: len(found.hits) for found in find_in_history(term)}


def _found_row_data(instances: list[ContEntry], clusters: list[ClusterEntry],
                    agents: list[Agent], hits: dict[Path, int],
                    ) -> tuple[list[ContEntry], list[ClusterEntry], list[Agent]]:
    """The three row populations narrowed to what a find matched: instances
    that said it, clusters keeping only the members that said it, and the
    agent rows those instances nest under (an instance row without its agent
    heading would render orphaned — the heading is the create row, and it
    stays usable).

    A cluster row survives on its MEMBERS' hits: a cluster holds no
    transcript of its own, every word of it was said by a member."""
    kept_instances = [entry for entry in instances if hits.get(entry.identity.state_dir)]
    kept_clusters = []
    for cluster in clusters:
        members = tuple(member for member in cluster.members
                        if hits.get(member.identity.state_dir))
        if members:
            kept_clusters.append(dataclasses.replace(cluster, members=members,
                                                     missing=()))
    kept_agent_names = {entry.identity.agent for entry in kept_instances}
    return (kept_instances, kept_clusters,
            [agent for agent in agents if agent.name in kept_agent_names])


def select_agent(registry: Registry) -> "Agent | Instance | cluster_state.Cluster | None":
    """Run the agent picker (main + nested deletion submenu) until selection or cancel.
    Returns an Agent (create), an Instance (continue), a Cluster (launch it),
    or None (cancel). Caller must ensure at least one agent .md exists before
    invoking.

    alt+f narrows the list to the conversations that SAID something (see
    `_found_row_data`). It closes the picker to ask for the term, exactly as
    the delete confirmation does, and reopens filtered — a search that ran
    while the picker was open would have to freeze it, and the picker owns
    the screen the answer would print on."""
    legend_text = _build_composition_legend(registry)   # built once per call — the loop below only re-scans instances
    find_term = ""          # "" = no find active; the list is everything
    while True:
        agents = creatable_agents(registry)
        # ONE `docker ps` and ONE reading of the launch site per menu build,
        # shared by the instance rows and the cluster rows below.
        running = docker_running_instances_subprocess() or frozenset()
        here = _CwdContext.here()
        instances = continuable_instances(registry, running, here)
        clusters = cluster_entries(registry, running, here)
        templates = discover_templates(AGENTS_DIR) if not find_term else {}
        if find_term:
            instances, clusters, agents = _found_row_data(
                instances, clusters, agents, _hit_counts(find_term))

        instances_by_agent: dict[str, list[ContEntry]] = {}
        for inst in instances:
            instances_by_agent.setdefault(inst.identity.agent, []).append(inst)

        # Each row population — Create rows, Cont rows, cluster rows — pads
        # its tag column and its name column against ITSELF, so each gets its
        # own layout (see `_RowLayout`; `--stop` is the menu that does it the
        # other way).
        #
        # Create rows show the `.lego` default professions/specialties (the
        # names resolve through the registry for warn-aware coloring); Cont
        # rows show the instance's actual resolved tag objects plus the
        # instance's resolved AI and harness — an agent row carries neither,
        # because which AI runs, and in which CLI, is decided when an
        # instance is created (operator, 2026-09-14); cluster rows the tags
        # they force on every member plus their member count.
        agent_cells = {a.name: _RowCells(_tags_column(_resolve_tags(registry, a.build, scope="solo")[0]))
                       for a in agents}
        inst_cells = _instance_cells(instances)
        cluster_cells = _cluster_cells(registry, clusters)
        agent_layout = _RowLayout.over(agent_cells)
        cont_layout = _RowLayout.over(inst_cells)
        cluster_layout = _RowLayout.over(cluster_cells)

        entries: list[PickerEntry] = []
        for agent in agents:
            tag_frags, tag_len = agent_cells[agent.name].column
            entries.append(PickerEntry(
                display=[
                    *PickerRowMarker.NEW.fragments("  "),
                    *tag_frags,
                    ("", " " * (agent_layout.column_width - tag_len)),
                    (STYLE_AGENT_NAME, f"{agent.name:<{agent_layout.name_width}}"),
                    ("", f" — {_agent_description(read_text(agent.md_path))}"),
                ],
                preview=_create_preview(agent),
                value=agent,
                marker=PickerRowMarker.NEW,
                deletable=False,
                modifiable=False,
            ))
            for inst in instances_by_agent.get(agent.name, []):
                identity = inst.identity
                entries.append(PickerEntry(
                    display=cont_layout.row(PickerRowMarker.CONT.fragments("      "),
                                            inst_cells[identity.instance], identity.instance,
                                            workspace=inst.workspace,
                                            inert=inst.is_running, running_hint=inst.is_running),
                    preview=_deferred_preview(inst),   # reads transcripts on first highlight, not at menu open
                    preview_quick=_deferred_preview(inst, quick=True),
                    value=identity,
                    marker=PickerRowMarker.CONT,
                    # A live container owns the name (Enter would hit a docker
                    # name conflict) and owns the state dir rw (Del would delete
                    # under it), so the row is information-only. selectable=False
                    # is what actually blocks all three keys; the other two are
                    # explicit belt-and-braces.
                    selectable=not inst.is_running,
                    deletable=not inst.is_running,
                    modifiable=not inst.is_running,
                ))

        # One row per `.legoset` — a cluster is CREATED from here (the
        # membership form). A template that fails to parse or names unknown
        # agents renders as an unselectable red row instead of crashing the
        # picker: templates are hand-authored files, and the picker is where
        # the author is.
        known_agents = frozenset(a.name for a in agents)
        for template_name, template_path in templates.items():
            try:
                template = load_legoset(template_path)
                validate(template, known_agents)
            except ClusterError as error:
                entries.append(PickerEntry(
                    display=[*PickerRowMarker.CLUSTER.fragments("  "),
                             (STYLE_TAG_INVALID, template_name),
                             ("", f" — broken template: {error}")],
                    preview=_ansi(Markdown(f"*`agents/{template_path.name}` failed to "
                                           f"load:*\n\n```\n{error}\n```")),
                    value=None, marker=PickerRowMarker.CLUSTER,
                    selectable=False, deletable=False, modifiable=False,
                ))
                continue
            # Same anatomy as an agent row — lead, a metadata column, the NAME
            # in the agents' name column, then " — description". The metadata
            # column holds the member count where agent rows hold tags, padded
            # so the name lands exactly where agent names do (the cluster tab
            # is wider, so the pad is measured, not copied).
            count = f"({len(template.members)} members)"
            name_column = PickerRowMarker.NEW.width("  ") + agent_layout.column_width
            pad = max(name_column - PickerRowMarker.CLUSTER.width("  ")
                      - len(count), 1)
            entries.append(PickerEntry(
                display=[
                    *PickerRowMarker.CLUSTER.fragments("  "),
                    (STYLE_MEMBER_COUNT, count),
                    ("", " " * pad),
                    (STYLE_AGENT_NAME, f"{template_name:<{agent_layout.name_width}}"),
                    ("", f" — {template.description or ', '.join(m.id for m in template.members)}"),
                ],
                preview=_template_preview(template, template_path),
                value=_ClusterTemplateRow(template_name, template_path),
                marker=PickerRowMarker.CLUSTER,
                deletable=False,
                modifiable=False,
            ))

        # Existing clusters follow ALL the template rows as top-level rows of
        # their own — never indented under "their" template: unlike an agent's
        # instances, which share the agent's CLAUDE.md, a cluster shares
        # nothing with its template once created, and may be renamed away
        # from it entirely (operator, 2026-09-09). Members nest beneath their
        # cluster, as instances do under their agent. The rows wear the same
        # anatomy instances wear, so a cluster's project path and hints line
        # up with everything else's. Enter on the cluster row launches it,
        # Del destroys it; a member row is the editing unit: F2 re-tags, Del
        # removes.
        # One cluster's rows (its own, then its members) form a BLOCK, and a
        # rule separates consecutive blocks — spanning the columns the rows
        # align in, never the variable workspace tail. Filtering keeps the
        # rules (`_visible_indices`), which is what they are for: two members
        # matching one typed word stay visibly apart when they belong to
        # different clusters (operator, 2026-09-17).
        block_break = break_row(PickerRowMarker.CLSTR.width("  ")
                                + cluster_layout.column_width + cluster_layout.name_width)
        for position, cluster_entry in enumerate(clusters):
            if position:
                entries.append(block_break)
            cluster = cluster_entry.cluster
            # The same information-only rule running instances get: the live
            # container owns the name (Enter → docker name conflict) and has
            # the state dir mounted at /cluster (F2's rename would move it out
            # from under the container; Del would delete it) — and members
            # follow their cluster: F2/Del write cluster.toml inside that dir.
            editable = not cluster_entry.is_running
            entries.append(PickerEntry(
                display=cluster_layout.row(PickerRowMarker.CLSTR.fragments("  "),
                                           cluster_cells[cluster.session], cluster.session,
                                           workspace=cluster_entry.workspace,
                                           inert=cluster_entry.is_running,
                                           running_hint=cluster_entry.is_running),
                preview=cluster_entry.preview,
                value=_ClusterRow(cluster.session),
                marker=PickerRowMarker.CLSTR,
                selectable=editable, deletable=editable, modifiable=editable,
            ))
            # Rows in picker order — the same derived order the windows will
            # launch in, so the list here IS the `^b 1..9` numbering. A member
            # row shows only its OWN tags: the cluster's are on the cluster row.
            # Enter is inert on a member (`pickable=False`) — it launches with
            # its cluster; F2 and Del are what the row is for.
            for member_entry in cluster_entry.members:
                own_tags = [t for t in member_entry.identity.active_tags
                            if t.name not in member_entry.inherited]
                own_problems = [p for p in member_entry.identity.invalid_tags
                                if p.name not in member_entry.inherited]
                member_tags, _ = _tags_column(own_tags, problems=own_problems)
                member_runtime, _ = _runtime_column(member_entry.identity.ai, member_entry.identity.harness)
                entries.append(PickerEntry(
                    display=[
                        *PickerRowMarker.MEMBER.fragments(""),
                        (STYLE_RUNNING_NAME if cluster_entry.is_running else STYLE_AGENT_NAME,
                         member_entry.member.id),
                        ("", "  "),
                        *member_runtime,     # the member's AI and harness right after its name — this anatomy leads with the name
                        *member_tags,
                    ],
                    preview=_deferred_preview(member_entry),   # the member's transcripts, read like an instance's
                    preview_quick=_deferred_preview(member_entry, quick=True),
                    value=_MemberRow(cluster.session, member_entry.member.id),
                    marker=PickerRowMarker.MEMBER,
                    pickable=False,
                    selectable=editable, deletable=editable, modifiable=editable,
                ))
            # A member whose agent `.md` is gone stays LISTED — red, with the
            # fault named — because a member the picker hides is the
            # silently-degraded peer the launch refuses. Del still removes it;
            # F2 has nothing to re-tag.
            for member in cluster_entry.missing:
                entries.append(PickerEntry(
                    display=[*PickerRowMarker.MEMBER.fragments(""),
                             (STYLE_TAG_INVALID, member.id),
                             ("", f" — no agent '{member.agent}' in agents/")],
                    preview=_ansi(Markdown(
                        f"*`{member.id}` — member of cluster `{cluster.session}`.*\n\n---\n\n"
                        f"Its agent `{member.agent}` has no `agents/{member.agent}.md` "
                        f"any more, so neither it nor the cluster can launch. "
                        f"Del removes it from the cluster.")),
                    value=_MemberRow(cluster.session, member.id),
                    marker=PickerRowMarker.MEMBER,
                    pickable=False,
                    selectable=editable, deletable=editable, modifiable=False,
                ))

        # Unconditional, unlike the toolkit-only era: the form's UI section
        # (the {mux} backend pick) is profession-independent, so the row must
        # exist even when no configurable profession does.
        entries.append(PickerEntry(
            display=[
                *PickerRowMarker.TOOLS.fragments("  "),
                ("", PREFERENCES_LABEL),
            ],
            preview=PREFERENCES_PREVIEW,
            value=_OPEN_PREFERENCES,
            marker=PickerRowMarker.TOOLS,
            deletable=False,
            modifiable=False,
        ))

        entries.append(PickerEntry(
            display=[
                *PickerRowMarker.DELMNU.fragments("  "),
                ("", DELMENU_LABEL),
            ],
            preview=DELMENU_PREVIEW,
            value=_OPEN_DELMENU,
            marker=PickerRowMarker.DELMNU,
            deletable=False,
            modifiable=False,
        ))

        title = (f'{TITLE_AGENT_PICKER}   —   found "{find_term}"'
                 if find_term else TITLE_AGENT_PICKER)
        action, value = pick_with_preview(title, entries, allow_delete=True,
                                          allow_modify=True, allow_find=True,
                                          legend_text=legend_text)
        if action is None:
            return None

        if action == PickerAction.FIND:
            # Esc at the prompt, or an empty term, CLEARS an active find —
            # otherwise the only way back to the full list would be to cancel
            # the picker and start it again.
            asked = ask_for_find_term(find_term)
            if asked and not _hit_counts(asked):
                _report_to_picker(f'  Nothing said "{asked}" in any conversation.')
                continue
            find_term = asked
            continue

        if action == PickerAction.DELETE:  # picker enforces deletability — cont, cluster, and member rows reach here
            if isinstance(value, _ClusterRow):
                _destroy_cluster_flow(value.session)
            elif isinstance(value, _MemberRow):
                _remove_member_flow(value.session, value.member_id)
            elif confirm_dialog(CONFIRM_DELETE_FMT.format(name=value.instance)):
                delete_instance(value)
            continue

        if action == PickerAction.MODIFY and isinstance(value, _ClusterRow):
            _edit_cluster_flow(registry, value.session)
            continue

        if action == PickerAction.MODIFY and isinstance(value, _MemberRow):
            _edit_member_flow(registry, value.session, value.member_id)
            continue

        if action == PickerAction.MODIFY:  # instance cont rows — the only other modifiable kind
            old_inst = value
            # ONE form: workspace + name as text fields above the tags — the
            # same no-terminal-prompt shape cluster editing has.
            result = prompt_tags(
                registry, old_inst.build, instance=old_inst.agent, scope="solo",
                fields=instance_fields(old_inst.agent,
                                       workspace=old_inst.workspace,
                                       suffix=old_inst.session,
                                       current=old_inst.session))
            if result is None:   # Esc — abort the modify, back to the picker
                continue
            values, new_build = result
            new_inst = dataclasses.replace(
                old_inst, session=values["session"],
                workspace=expand_user_path(values["workspace"]),
                invalid_tags=(),   # re-picking against the live registry clears any stale/typo'd tags
                **resolve_build(new_build, old_inst.agent, registry),
            )  # is_brand_new stays False via the dataclass replace
            modify_instance(old_inst, new_inst)
            continue

        if value is _OPEN_PREFERENCES:
            edit_profiles_menu(registry)
            continue

        if value is _OPEN_DELMENU:
            _delete_submenu(registry, legend_text)
            continue

        if isinstance(value, _ClusterTemplateRow):
            _create_cluster_flow(registry, value.path)
            continue   # created (or cancelled) — back to the picker either way

        if isinstance(value, _ClusterRow):
            # Enter on a cluster LAUNCHES it — run.py owns docker, so hand the
            # loaded cluster back the same way an Agent/Instance is handed.
            # Reloaded from disk (the row may predate an edit); vanished means
            # someone destroyed it underneath — explain, don't crash.
            if (picked_cluster := cluster_state.load(value.session)) is not None:
                return picked_cluster
            _report_to_picker(
                f"  Cluster '{value.session}' is gone from disk.")
            continue

        if isinstance(value, Instance) and not value.is_startable:
            # A Cont row whose stored tags no longer resolve — explain and
            # bounce back to the picker (F2 re-picks; Del removes it) rather
            # than starting a half-resolved instance.
            _report_to_picker(invalid_tags_report(value) + "\n")
            continue

        return value  # Agent (new) | Instance (cont)


def _delete_submenu(registry: Registry, legend_text: str) -> None:
    """Flat deletion submenu — every row red. Loops until Esc / Back."""
    while True:
        instances = continuable_instances(registry)
        if not instances:
            return
        entries: list[PickerEntry] = []
        for inst in instances:
            identity = inst.identity
            row = [*PickerRowMarker.DLET.fragments("  "),
                   (STYLE_RUNNING_NAME if inst.is_running else STYLE_DEL_NAME, identity.instance)]
            if inst.is_running:
                row.append(("", "  "))
                row.append(RUNNING_HINT)
            entries.append(PickerEntry(
                display=row,
                preview=_deferred_preview(inst),       # deferred, as in the main picker
                preview_quick=_deferred_preview(inst, quick=True),
                value=identity,
                marker=PickerRowMarker.DLET,
                # Deleting a live container's state dir (bind-mounted rw) could
                # corrupt the running session — information-only here too.
                selectable=not inst.is_running,
                deletable=not inst.is_running,
                modifiable=not inst.is_running,
            ))
        entries.append(PickerEntry(
            display=PickerRowMarker.BACK.fragments(f"  {BACK_LABEL}"),
            preview=BACK_PREVIEW,
            value=None,
            marker=PickerRowMarker.BACK,
            deletable=False,
        ))

        action, value = pick_with_preview(TITLE_DELETE_MENU, entries, allow_delete=True, legend_text=legend_text)
        if action is None or value is None:
            return
        if confirm_dialog(CONFIRM_DELETE_FMT.format(name=value.instance)):
            delete_instance(value)
