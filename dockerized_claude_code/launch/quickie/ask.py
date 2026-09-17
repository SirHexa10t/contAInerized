"""The quickie tool's orchestration — one direct question → a one-shot
`claude -p` answer, its thread parked under `~/.ai-agents/quickie/`.

A deliberately lean cousin of run.py's `launch()`: no picker, no tag form, no
store entry. It reuses the launcher's core helpers directly (setup, image
build, container run) — this package is a leaf CONSUMER of `launch/`, never
imported back by it. Any shared mechanism (run_container's print mode,
`Instance.state_dir_override`) lives in the core; here we only call it.

Personas are hidden agents (`_` prefix → excluded from the picker / CLI /
audit), loaded by path so a question never touches the interactive machinery.
The default is `_quickie` on the `quick` engine; `--explain` swaps in
`_trivia` (reliable/Opus) and `--research` a lean researcher build — see the
QuickieAgent specs below.
"""

import dataclasses
import sys
import uuid
from pathlib import Path
from typing import NamedTuple

from ..agents_crud import compute_resume_flag
from ..ai import active_adapter, adopt, refusal_for
from ..container_env import set_container_env, set_instance_env
from ..docker_config import ensure_image, require_docker, run_container, set_container_mounts
from ..file_access import ensure_dir
from ..paths import CLAUDE_CONFIG_IN_CONTAINER, quickie_communal_workspace, quickie_state_dir_path
from ..paths import AGENTS_DIR
from ..staging import stage_instance
from ..tag_handlers import apply_tags
from ..tags import Instance, Registry, TagError, load_lego, resolve_build
from ..utils import call_or_exit
from .render import render_stream

class QuickieAgent(NamedTuple):
    """A quickie persona + build: the `.md` installed as the thread's CLAUDE.md,
    the `.lego` resolved for the run, and the instance/container label.
    `--explain` / `--research` swap this out; the rest of ask() is agnostic."""
    label: str
    md: Path
    lego: Path


QUICK    = QuickieAgent("quickie",  AGENTS_DIR / "_quickie.md",   AGENTS_DIR / "_quickie.lego")   # default (quick/Sonnet)
TRIVIA   = QuickieAgent("trivia",   AGENTS_DIR / "_trivia.md",    AGENTS_DIR / "_trivia.lego")    # --explain (the `reliable` engine)
RESEARCH = QuickieAgent("research", AGENTS_DIR / "researcher.md", AGENTS_DIR / "_research.lego")  # --research (researcher engine, lean/base image)



def _gibberish() -> str:
    """A short throwaway session id — the user isn't meant to get attached to
    it; it just keeps each question's thread in its own (resumable-later) dir."""
    return uuid.uuid4().hex[:12]


def build_quickie_instance(registry: Registry, session: str, *,
                           agent: QuickieAgent = QUICK, is_brand_new: bool = True,
                           ai: str | None = None) -> Instance:
    """The Instance for one quickie question: `agent`'s hidden persona + lego
    (default QUICK; TRIVIA for `--explain`, RESEARCH for `--research`), the
    communal shared workspace mounted at /workspace, and a state dir parked
    under `quickie/` (via `state_dir_override`) rather than the main
    `instances/`. `ai` (`--ai <name>`) swaps the lego's AI for another member
    of `agents/ai/`, its harness falling back to that AI's default (the lego's
    harness runs the lego's AI, not this one); None keeps the lego's — claude,
    in every shipped quickie lego. `is_brand_new=False` marks a `--resume` so
    compute_resume_flag can offer `--continue`. Pure construction — no
    disk/docker side effects — so it's unit-testable on its own."""
    build = load_lego(agent.lego)
    if ai is not None:
        build = dataclasses.replace(build, ai=ai, harness=None)
    registry.validate_build(build, f"{agent.lego} --ai {ai}" if ai else agent.lego, scope="solo")   # shipped file → the raising validate is right; a quickie is a solo build
    return Instance(
        agent=agent.label,
        md_path=agent.md,
        session=session,
        workspace=str(quickie_communal_workspace()),
        is_brand_new=is_brand_new,
        state_dir_override=quickie_state_dir_path(session),
        **resolve_build(build, agent.label, registry),
    )


def ask(question: str, registry: Registry, *, resume_session: str | None = None,
        agent: QuickieAgent = QUICK, ai: str | None = None) -> None:
    """Answer one question one-shot with `agent` (QUICK default; TRIVIA for
    `--explain`, RESEARCH for `--research`), on the lego's AI or the `--ai`
    one. `registry` is the tree the CLI's one startup opened
    (`startup.open_launcher`, before parsing — the same first step every
    entry takes). With `resume_session` (an id from `q --history`) the
    question continues that existing thread via `--continue`; otherwise it
    opens a fresh thread under a throwaway id. Either way it stages state like
    a normal launch (minus picker / form / store / optional-creds), builds the
    image if it isn't cached, then runs `claude -p`. The thread persists under
    `quickie/` for later resume."""
    question = question.strip()
    if not question:
        sys.exit(
            f'Ask a follow-up:  q --resume {resume_session} "your question"   (quote it).'
            if resume_session else
            'Ask a question:  q "your question here"   (quote the whole question).'
        )
    require_docker()
    ensure_dir(quickie_communal_workspace())   # the /workspace mount source must exist, else docker root-creates it
    if resume_session is not None:
        if not quickie_state_dir_path(resume_session).is_dir():
            sys.exit(f"No quickie thread '{resume_session}'.  Run  q --history  to list them.")
        session, is_brand_new = resume_session, False
    else:
        session, is_brand_new = _gibberish(), True
    inst = build_quickie_instance(registry, session, agent=agent, is_brand_new=is_brand_new, ai=ai)
    if inst.harness is not None and (refused := refusal_for(inst.harness.name, inst.harness.label)) is not None:
        # A harness without an adapter — same rule as run.py; with --ai, say
        # which AI's CLI that is, since the user named the AI, not the CLI.
        lead = f"  --ai {ai}: {inst.ai.label if inst.ai else ai} answers through {inst.harness.label}, and\n" if ai else ""
        sys.exit(lead + refused)
    adopt(inst.harness.name if inst.harness else None)
    resume_flag = compute_resume_flag(inst)      # ["--continue"] when the thread has a transcript; [] otherwise

    apply_tags(inst)                             # no-op for _quickie today (no handler tag); future-proof
    # The one per-agent staging every run shape calls (launch/staging.py) —
    # a quickie keeps the default config root, like a solo instance.
    staged = call_or_exit(stage_instance, inst, registry, harness=active_adapter(),
                          config=str(CLAUDE_CONFIG_IN_CONTAINER), relocated=False,
                          exceptions=(TagError, RuntimeError))
    for notice in staged.notices:
        print(notice)
    set_container_env(inst.professions)
    set_instance_env(inst)
    set_container_mounts(inst)
    image = ensure_image(inst)
    # The harness's flags for a progress-showing one-shot event stream (the
    # adapter says which; render_stream turns it into a ticker + streamed answer).
    run_container(inst, image, list(active_adapter().stream_args), resume_flag, interactive=False,
                  print_prompt=question, stream_renderer=render_stream)
