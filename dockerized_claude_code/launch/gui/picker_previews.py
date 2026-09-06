"""The picker's PREVIEW PANE — every row's right-hand text, and the machine
that keeps building it off the UI thread (launch/gui).

Split out of menu_picker 2026-09-03, the last of that module's four roles to
leave (after the flows and the prompts). It is a genuine concern of its own:
the pane is markdown rendered to ANSI through rich, some of it interleaved
with rich renderables because markdown cannot colour a SPAN, and the
expensive part — the last human prompt, read out of a transcript that can be
tens of megabytes — is deliberately NOT computed where the keyboard is.

  _render_md / _render_parts     markdown → ANSI, and the interleaved form
  _read_last_prompt              that prompt's read, in a CHILD PROCESS —
                                 a CPU-bound thread convoys the GIL and
                                 stalls the picker's keystrokes (measured:
                                 803 ms vs 3.5 ms; benchmark/
                                 bench_preview_gil.py reproduces it)
  _last_prompt_display           the same, formatted for the pane
  cont_preview                   a Cont row's metadata + tags + that prompt
  _tags_preview                  the tag list a squashed row cannot show
  _create_preview / _template_preview / _cluster_preview / _member_preview
                                 one builder per kind of row

What did NOT come along, deliberately: `_PreviewLoader`. Its thread worker
and cache are coupled to `PickerEntry`'s deferred slots — WHEN to compute a
preview and where to put the result is the widget's business — so it stays
beside the rows it fills, and calls in here for the text.

Direction: `styles` → … → `picker_previews` → `menu_picker`. Nothing here
imports the picker: the Cont-row preview takes the values it needs
(`cont_preview`), so the row model stays where the rows are.
"""

from __future__ import annotations

import atexit
import io
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from ..cluster import state as cluster_state
from ..cluster.legoset import ClusterTemplate
from ..file_access import read_text
from ..transcripts import last_prompt_in_state
from ..tags import Agent, AgentBuild, Instance, Registry
from .styles import RICH_BY_STYLE, tag_style

def _render_md(text: str) -> str:
    """Render markdown text to an ANSI-encoded string for the picker's preview
    pane. Width is fixed to 80; prompt_toolkit re-wraps if the pane is
    narrower."""
    buf = io.StringIO()
    Console(
        file=buf, force_terminal=True, color_system="truecolor", width=80,
    ).print(Markdown(text))
    return buf.getvalue()


def _render_parts(*parts: Any) -> str:
    """Markdown and rich renderables interleaved into one ANSI preview string —
    `_render_md`'s console, accepting prepared renderables. Exists because
    markdown cannot colour a SPAN, and the cluster previews colour member
    names and tag labels inline."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="truecolor",
                      width=80)
    for part in parts:
        console.print(part)
    return buf.getvalue()


RICH_MEMBER_NAME = "bold bright_blue"


LAST_PROMPT_PREVIEW_CHARS = 250   # enough to recognise a conversation; not a transcript viewer

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


def cont_preview(inst: Instance, workspace_display: str,
                 last_used_display: str, prompt: str | None) -> str:
    """A Cont row's preview: an italic lead-in, a rule, a YAML-fenced
    metadata block (rich syntax-colours keys and values) — with a `Last
    prompt` field iff `prompt` is given — then every active tag expanded
    to its coloured label, full name and one-liner. The preview is where
    tags can be READ: the row itself collapses them to one-char chips
    once it holds SQUASH_AT of them, so this list is the lookup.

    Takes the four values it needs rather than the row object: that is
    what keeps this module from importing the picker (2026-09-03).
    """
    return _render_md(
        f"*Continue session `{inst.instance}`.*\n\n"
        f"---\n\n"
        f"```yaml\n"
        f"Agent:     {inst.agent}\n"
        f"Session:   {inst.session}\n"
        f"Workspace: {workspace_display}\n"
        f"Engine:    {inst.engine.name if inst.engine else '(default)'}\n"
        f"State:     {inst.state_dir}\n"
        f"Last used: {last_used_display}\n"
        + (f"\nLast prompt:\n  {prompt}\n" if prompt else "")
        + "```\n"
        ) + _tags_preview(inst)


def _tags_preview(inst: Instance) -> str:
    """The Cont preview's expanded tag list, ANSI-rendered: one line per
    active tag — colored label, underlined full name, one-line description —
    plus an alert line per invalid tag with what to do about it.

    Built with rich Text (like the F8 legend, same RICH_BY_STYLE colors)
    rather than inside the markdown, because per-tag coloring is the point:
    the legend taught these colors, the row may be showing them as bare chips,
    and this list is what maps a chip back to a name."""
    labels = [t.label for t in inst.active_tags] + [p.label for p in inst.invalid_tags]
    pad = max((len(label) for label in labels), default=0)
    lines = Text("Tags:\n", style="bold")
    # Padding sits OUTSIDE each styled span: the invalid style paints a red
    # background, and a red bar of trailing spaces would read as more alert.
    for tag in inst.active_tags:
        lines.append("  ")
        lines.append(tag.label, style=RICH_BY_STYLE[tag_style(tag)])
        lines.append(" " * (pad - len(tag.label)) + "  ")
        lines.append(tag.fullname or tag.name, style="underline")
        lines.append(f" — {tag.short_description}\n")
    if not inst.active_tags:
        lines.append("  (none)\n")
    for problem in inst.invalid_tags:
        lines.append("  ")
        lines.append(problem.label, style="black on red")
        lines.append(" " * (pad - len(problem.label)) + "  ")
        lines.append(f"{problem.reason.replace('_', ' ')} {problem.kind} — "
                     f"fix it via F2 before this instance can start\n")
    buf = io.StringIO()
    Console(file=buf, force_terminal=True, color_system="truecolor", width=80,
            ).print(lines, end="")
    return buf.getvalue()


def _create_preview(agent: Agent) -> str:
    """Build the Create-row preview markdown from a creatable_agents Agent
    and render to ANSI. Italic source line, horizontal rule, then the .md content as-is."""
    return _render_md(
        f"*Create a new instance of `{agent.name}` — `agents/{agent.md_path.name}`*\n\n"
        f"---\n\n"
        f"{read_text(agent.md_path)}"
    )


def _template_preview(template: ClusterTemplate, path: Path) -> str:
    """The cluster-template row's preview: what a cluster is and who the
    default members are (names in the picker's blue). No key tutorial — the
    picker's status bar and F8 legend own the keys, same as every other row."""
    description = f"{template.description}\n\n" if template.description else ""
    members = Text()
    for m in template.members:
        members.append("  • ", style="dim")
        members.append(m.id, style=RICH_MEMBER_NAME)
        members.append("\n")
    members.rstrip()
    return _render_parts(
        Markdown(f"*Create a cluster from `agents/{path.name}`*\n\n---\n\n"
                 f"{description}"
                 f"A **cluster** is N agents cohabiting one container on one "
                 f"project, each in its own multiplexer window, able to message "
                 f"each other by name.\n\nDefault members:"),
        Text(),
        members)


# The rich twin of STYLE_AGENT_NAME: previews render through rich, the rows
# through prompt_toolkit, and member names must wear the same blue in both.


def _member_line(registry: Registry, identifier: str, build: AgentBuild) -> Text:
    """One preview line for a member: bullet, BLUE name (the colour agent names
    wear everywhere in the picker), then its tag labels in their legend colours
    — a name that no longer resolves renders alert-style rather than vanishing."""
    line = Text("  • ", style="dim")
    line.append(identifier, style=RICH_MEMBER_NAME)
    names = (*((build.engine,) if build.engine else ()),
             *build.professions, *build.specialties, *build.policies)
    for name in names:
        line.append("  ")
        if (tag := registry.get(name)) is not None:
            line.append(tag.label, style=RICH_BY_STYLE[tag_style(tag)])
        else:
            line.append(name, style="black on red")
    return line


def _cluster_preview(registry: Registry, cluster: "cluster_state.Cluster") -> str:
    """An existing cluster's preview: who is in it, wearing what. Keys are the
    status bar's and legend's job, same as every other row."""
    origin = f" (from `{cluster.template}.legoset`)" if cluster.template else ""
    return _render_parts(
        Markdown(f"*Cluster `{cluster.session}`{origin}*\n\n---\n\n"
                 f"project: `{cluster.project}`"),
        Text(),
        *[_member_line(registry, m.id, m.build)
          for m in cluster_state.picker_order(cluster.members, registry)])


def _member_preview(registry: Registry, cluster: "cluster_state.Cluster",
                    member: "cluster_state.Member") -> str:
    """One member's preview: its agent, its tags — plus the one member-specific
    fact worth stating (the forced tags), with the generic key tutorial gone
    like every other preview's."""
    tags = _member_line(registry, member.id, member.build)
    return _render_parts(
        Markdown(f"*`{member.id}` — member of cluster `{cluster.session}`*"
                 f"\n\n---\n\n"
                 f"agent: `{member.agent}` · role: `{member.role}`"),
        Text(),
        tags,
        Markdown("\n*`{muxer}` and `{cluster}` are re-applied on every edit — "
                 "every member carries them.*"))
