"""The picker's PREVIEW PANE — every row's right-hand text, and the machine
that keeps building it off the UI thread (launch/gui).

Split out of menu_picker 2026-09-03, the last of that module's four roles to
leave (after the flows and the prompts). It is a genuine concern of its own:
the pane is markdown rendered to ANSI through rich, some of it interleaved
with rich renderables because markdown cannot colour a SPAN, and the
expensive part — the last human prompt, read out of a transcript that can be
tens of megabytes — is deliberately NOT computed where the keyboard is.

  _ansi                          rich renderables → the pane's ANSI text: the
                                 ONE console every preview renders through
  _read_last_prompt              that prompt's read, in a CHILD PROCESS —
                                 a CPU-bound thread convoys the GIL and
                                 stalls the picker's keystrokes (measured:
                                 803 ms vs 3.5 ms; benchmark/
                                 bench_preview_gil.py reproduces it)
  _last_prompt_display           the same, formatted for the pane
  session_preview                THE pane for anything with a state dir: a
                                 lead-in, a YAML fact block, the optional
                                 `Last prompt`, the expanded tag list — which
                                 instances, cluster members and clusters all
                                 render through (2026-09-09: each had its own
                                 hand-rolled pane before, and only the
                                 instance one showed Last used / Last prompt)
  cont_preview / member_preview / _cluster_preview
                                 its three callers, each supplying its facts
  _tag_lines / _member_line      the tag list a squashed row cannot show, and
                                 one member's summary line in a cluster pane
  _resolve_tags                  a build's names as Tag objects, plus the
                                 names that resolve to nothing
  _create_preview / _template_preview
                                 the two creation rows' panes

What did NOT come along, deliberately: `_PreviewLoader`. Its thread worker
and cache are coupled to `PickerEntry`'s deferred slots — WHEN to compute a
preview and where to put the result is the widget's business — so it stays
beside the rows it fills, and calls in here for the text.

Direction: `styles` → … → `picker_previews` → `picker_widget` → `menu_picker`.
Nothing here imports the picker: every builder takes the VALUES it needs, so
the row models stay where the rows are.
"""

from __future__ import annotations

import atexit
import io
import multiprocessing
from collections.abc import Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from ..cluster import state as cluster_state
from ..cluster.legoset import ClusterTemplate
from ..cluster.member import Member
from ..file_access import read_text
from ..transcripts import last_prompt_in_state
from ..tags import Agent, AgentBuild, Ai, Engine, Harness, Instance, Registry, Tag, TagProblem
from .styles import RICH_AGENT_NAME, rich_style, tag_style

PREVIEW_WIDTH = 80                # rich renders at this width; prompt_toolkit re-wraps if the pane is narrower
LAST_PROMPT_PREVIEW_CHARS = 250   # enough to recognise a conversation; not a transcript viewer
STYLE_ALERT = "black on red"      # rich twin of styles.STYLE_TAG_INVALID: a name that resolves to nothing


def _ansi(*renderables: Any) -> str:
    """Rich renderables → one ANSI string, on the preview pane's console.
    Markdown and rich Text interleave freely, which is what lets a pane
    colour a SPAN (a tag label, a member name) — markdown alone cannot. Four
    call sites each built this console themselves until 2026-09-09."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="truecolor",
                      width=PREVIEW_WIDTH)
    for part in renderables:
        console.print(part)
    return buf.getvalue()


# The child process that parses transcripts — created on first use, kept for
# the launcher's lifetime (one warm child serves every preview of every menu),
# torn down at exit. See _read_last_prompt for why it exists at all.
_PROMPT_POOL: ProcessPoolExecutor | None = None


def _read_last_prompt(state_dir: Path) -> tuple[str, float] | None:
    """`last_prompt_in_state`, run in a CHILD PROCESS — because a thread is not
    background enough. Parsing a large transcript is CPU-bound (~500k
    `json.loads` calls, plus one `.splitlines()` holding the GIL for the whole
    68 MB string), and a CPU-bound thread convoys the GIL: measured on a real
    155 MB state dir, the render thread's 5 ms tick stalled up to 803 ms while
    a worker THREAD read it, versus 3.5 ms worst-case with the read in a child
    process (launch/benchmark/bench_preview_gil.py reproduces this). The
    worker thread that calls this blocks in `.result()`, which waits GIL-FREE.

    Spawn, not fork: the parent runs prompt_toolkit with live threads by the
    time this fires, and forking a threaded process copies lock state mid-use.
    The child imports only `launch.file_access` (the pickled target), so the
    one-time warmup is ~0.2 s — paid on the worker, never on the UI thread.

    Falls back to the in-process read when the pool cannot serve (a sandbox
    forbidding subprocesses, a killed child): the GIL stutter returns, but the
    preview still resolves — degraded beats broken. The fallback also makes
    this the test seam: a patched-in fake is unpicklable, so tests exercising
    the display logic stay in-process without special-casing."""
    global _PROMPT_POOL
    try:
        if _PROMPT_POOL is None:
            _PROMPT_POOL = ProcessPoolExecutor(
                max_workers=1, mp_context=multiprocessing.get_context("spawn"))
            atexit.register(_PROMPT_POOL.shutdown, wait=False, cancel_futures=True)
        return _PROMPT_POOL.submit(last_prompt_in_state, state_dir).result()
    except Exception:                          # noqa: BLE001 — any pool failure degrades, none may break the picker
        return last_prompt_in_state(state_dir)


def _last_prompt_display(state_dir: Path) -> str | None:
    """The last human prompt this instance received, condensed for the preview
    pane — or None when it has none yet (the field then drops out entirely,
    rather than showing an empty label).

    Condensed two ways, each for a reason: whitespace runs (including
    newlines) collapse to single spaces, because the value sits inside the
    preview's YAML fence and a raw line starting ``` would close the fence
    around the rest of the metadata; and anything past
    LAST_PROMPT_PREVIEW_CHARS is cut at an ellipsis, because the field exists
    to recognise the conversation, not to reread it."""
    found = _read_last_prompt(state_dir)
    if found is None:
        return None
    condensed = " ".join(found[0].split())
    if len(condensed) > LAST_PROMPT_PREVIEW_CHARS:
        condensed = condensed[:LAST_PROMPT_PREVIEW_CHARS] + "…"
    return condensed or None


def session_preview(lead: str, facts: Sequence[tuple[str, str]], *,
                    tags: Sequence[Tag], problems: Sequence[TagProblem],
                    prompt: str | None = None,
                    inherited: frozenset[str] = frozenset(),
                    fix_target: str = "this instance can start",
                    trailer: Iterable[Any] = ()) -> str:
    """The pane for anything that has a state dir — an instance, a cluster
    member, a cluster: an italic lead-in, a rule, a YAML-fenced fact block
    (rich syntax-colours keys and values; the `Label:` column is padded to
    the longest label) — with a `Last prompt` field iff `prompt` is given —
    then every tag expanded to its coloured label, full name and one-liner
    (`_tag_lines`), then any `trailer` renderables. The preview is where tags
    can be READ: the row collapses them to one-char chips once it holds
    SQUASH_AT of them, so this list is the lookup.

    One builder for the three kinds of row (2026-09-09), so they cannot drift
    in shape — a member pane and an instance pane differ only in their facts.
    Takes values, never a row object: that is what keeps this module from
    importing the picker."""
    pad = max(len(label) for label, _ in facts) + 1          # "Label:" then at least one space
    block = "".join(f"{label + ':':<{pad}} {value}\n" for label, value in facts)
    if prompt:
        block += f"\nLast prompt:\n  {prompt}\n"
    return _ansi(Markdown(f"*{lead}*\n\n---\n\n```yaml\n{block}```"),
                 _tag_lines(tags, problems, inherited=inherited, fix_target=fix_target),
                 *trailer)


def _tag_lines(tags: Sequence[Tag], problems: Sequence[TagProblem], *,
               inherited: frozenset[str] = frozenset(),
               fix_target: str = "this instance can start") -> Text:
    """The expanded tag list: one line per tag — coloured label, underlined
    full name, one-line description, and a dim `(cluster-wide)` on the tags a
    member inherits from its cluster — plus an alert line per unresolvable
    name with what to do about it (`fix_target` names what the fix unblocks).

    Built with rich Text (like the F8 legend, same RICH_BY_STYLE colours)
    rather than inside the markdown, because per-tag colouring is the point:
    the legend taught these colours, the row may be showing them as bare
    chips, and this list is what maps a chip back to a name."""
    labels = [t.label for t in tags] + [p.label for p in problems]
    pad = max((len(label) for label in labels), default=0)
    lines = Text("Tags:", style="bold")
    # Padding sits OUTSIDE each styled span: the alert style paints a red
    # background, and a red bar of trailing spaces would read as more alert.
    for tag in tags:
        lines.append("\n  ")
        lines.append(tag.label, style=rich_style(tag_style(tag)))
        lines.append(" " * (pad - len(tag.label)) + "  ")
        lines.append(tag.fullname or tag.name, style="underline")
        if tag.name in inherited:   # beside the name, so a long description wrapping cannot orphan it
            lines.append(" (cluster-wide)", style="dim")
        lines.append(f" — {tag.short_description}")
    if not tags:
        lines.append("\n  (none)")
    for problem in problems:
        lines.append("\n  ")
        lines.append(problem.label, style=STYLE_ALERT)
        lines.append(" " * (pad - len(problem.label)) + "  ")
        lines.append(f"{problem.reason.replace('_', ' ')} {problem.kind} — "
                     f"fix it via F2 before {fix_target}")
    return lines


def _resolve_tags(registry: Registry, build: AgentBuild,
                  ) -> tuple[list[Tag], list[TagProblem]]:
    """A build's profession / specialty / policy names as Tag objects (build
    order), plus a TagProblem for every name that resolves to nothing —
    `Registry.resolve_store_build`'s non-raising partition, as objects. The
    engine is left out: it is a fact line, not a tag-list entry. Rows use the
    tags; panes use both."""
    clean, problems = registry.resolve_store_build(build)
    names = (*clean.professions, *clean.specialties, *clean.policies)
    return [tag for name in names if (tag := registry.get(name)) is not None], problems


def engine_fact(inst: Instance) -> str:
    """The preview's Engine fact: the engine's name and, after it, the model
    its budget pins for the AI in use — `quick  claude-sonnet-5` — so the
    model shows without the engine's description having to name it."""
    name = inst.engine.name if inst.engine else "(default)"
    model = inst.model
    return f"{name}  {model}" if model else name


def _runtime_tags(inst: Instance) -> tuple[Tag, ...]:
    """The pane's tag list for an instance: its AI FIRST (which AI runs it is
    the first thing to know), the harness that wraps it second, then its
    active tags."""
    return (*((inst.ai,) if inst.ai else ()), *((inst.harness,) if inst.harness else ()), *inst.active_tags)


def cont_preview(inst: Instance, workspace_display: str,
                 last_used_display: str, prompt: str | None) -> str:
    """A Cont row's pane — `session_preview` with an instance's facts; the
    tag list opens with the instance's AI."""
    return session_preview(
        f"Continue session `{inst.instance}`.",
        [("Agent", inst.agent),
         ("Session", inst.session),
         ("Workspace", workspace_display),
         ("Engine", engine_fact(inst)),
         ("State", str(inst.state_dir)),
         ("Last used", last_used_display)],
        tags=_runtime_tags(inst), problems=inst.invalid_tags, prompt=prompt)


def member_preview(inst: Instance, member: Member, cluster: str,
                   project_display: str, last_used_display: str,
                   prompt: str | None, inherited: frozenset[str]) -> str:
    """A member row's pane — the same builder with a member's facts: who it
    is within its cluster, then everything an instance pane shows, because
    the member IS an instance (`Cluster.member_instance`). Its tags are its
    REAL build, the cluster's marked `(cluster-wide)`; the row shows only
    its own. A stale tag blocks the whole cluster's launch, which is what
    the alert line says."""
    return session_preview(
        f"`{member.id}` — member of cluster `{cluster}`.",
        [("Agent", member.agent),
         ("Role", member.role),
         ("Cluster", cluster),
         ("Project", project_display),
         ("Engine", engine_fact(inst)),
         ("State", str(inst.state_dir)),
         ("Last used", last_used_display)],
        tags=_runtime_tags(inst), problems=inst.invalid_tags, prompt=prompt,
        inherited=inherited, fix_target="this cluster can launch")


def _member_line(identifier: str, ai: Ai | None, harness: Harness | None, engine: Engine | None, tags: Sequence[Tag],
                 problems: Sequence[TagProblem], last_used: str, *,
                 missing_agent: str | None = None) -> Text:
    """One member's line in its cluster's pane: bullet, BLUE name (the colour
    names wear everywhere in the picker), its AI, its harness, its engine and OWN tag labels in
    their legend colours — an unresolvable name in the alert style rather than
    vanishing — and when it last ran, dim. A member whose agent `.md` is gone
    (`missing_agent`) renders its name in the alert style with what to do."""
    line = Text("  • ", style="dim")
    if missing_agent is not None:
        line.append(identifier, style=STYLE_ALERT)
        line.append(f"  no agent '{missing_agent}' in agents/ — Del removes it "
                    f"from the cluster", style="bold red")
        return line
    line.append(identifier, style=RICH_AGENT_NAME)
    for tag in (*((ai,) if ai else ()), *((harness,) if harness else ()), *((engine,) if engine else ()), *tags):
        line.append("  ")
        line.append(tag.label, style=rich_style(tag_style(tag)))
    for problem in problems:
        line.append("  ")
        line.append(problem.label, style=STYLE_ALERT)
    line.append(f"   · last used {last_used}", style="dim")
    return line


def _cluster_preview(cluster: cluster_state.Cluster, tags: Sequence[Tag],
                     problems: Sequence[TagProblem], last_used_display: str,
                     member_lines: Sequence[Text]) -> str:
    """An existing cluster's pane — the same builder: its facts (Last used is
    its newest member's), the tags it forces on every member expanded with
    any unresolvable name flagged, then who is in it wearing what
    (`_member_line`, in picker order — the window order). Keys are the status
    bar's and legend's job, same as every other row."""
    origin = f" (from `{cluster.template}.legoset`)" if cluster.template else ""
    members = Text("Members:", style="bold")
    for line in member_lines:
        members.append("\n")
        members.append_text(line)
    return session_preview(
        f"Cluster `{cluster.session}`{origin}.",
        [("Project", str(cluster.project)),
         ("Template", cluster.template or "(none)"),
         ("State", str(cluster_state.cluster_dir(cluster.session))),
         ("Last used", last_used_display)],
        tags=tags, problems=problems, fix_target="this cluster can launch",
        trailer=(Text(), members))


def _create_preview(agent: Agent) -> str:
    """Build the Create-row preview markdown from a creatable_agents Agent
    and render to ANSI. Italic source line, horizontal rule, then the .md
    content as-is. No AI here: which AI runs is decided when the instance is
    created (the form's first question), not by the agent."""
    return _ansi(Markdown(
        f"*Create a new instance of `{agent.name}` — `agents/{agent.md_path.name}`*\n\n"
        f"---\n\n"
        f"{read_text(agent.md_path)}"
    ))


def _template_preview(template: ClusterTemplate, path: Path) -> str:
    """The cluster-template row's preview: what a cluster is and who the
    default members are (names in the picker's blue). No key tutorial — the
    picker's status bar and F8 legend own the keys, same as every other row."""
    description = f"{template.description}\n\n" if template.description else ""
    members = Text()
    for m in template.members:
        members.append("  • ", style="dim")
        members.append(m.id, style=RICH_AGENT_NAME)
        members.append("\n")
    members.rstrip()
    return _ansi(
        Markdown(f"*Create a cluster from `agents/{path.name}`*\n\n---\n\n"
                 f"{description}"
                 f"A **cluster** is N agents cohabiting one container on one "
                 f"project, each in its own multiplexer window, able to message "
                 f"each other by name.\n\nDefault members:"),
        Text(),
        members)
