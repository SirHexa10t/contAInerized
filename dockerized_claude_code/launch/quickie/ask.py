"""The quickie tool's orchestration — one direct question → a one-shot
`claude -p` answer, its thread parked under `~/.claude-agents/quickie/`.

A deliberately lean cousin of run.py's `launch()`: no picker, no tag form, no
store entry. It reuses the launcher's core helpers directly (setup, image
build, container run) — this package is a leaf CONSUMER of `launch/`, never
imported back by it. Any shared mechanism (run_container's print mode,
`Instance.state_dir_override`) lives in the core; here we only call it.

Personas are hidden agents (`_` prefix → excluded from the picker / CLI /
audit), loaded by path so a question never touches the interactive machinery.
The default is `_quickie` on the `quick`/Sonnet engine; `--explain` swaps in
`_trivia` (reliable/Opus) and `--research` a lean researcher build — see the
QuickieAgent specs below.
"""

import sys
import uuid
from pathlib import Path
from typing import NamedTuple

from ..agents_crud import compute_resume_flag, install_latest_md, install_settings
from ..container_env import set_container_env
from ..docker_config import ensure_image, require_docker, run_container, set_container_mounts
from ..file_access import ensure_dir, ensure_shared_oauth_files
from ..paths import AGENTS_DIR, quickie_communal_workspace, quickie_state_dir_path
from ..tag_handlers import apply_tags
from ..tags import Instance, Registry, TagError, load_lego, resolve_build, scan_all
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
TRIVIA   = QuickieAgent("trivia",   AGENTS_DIR / "_trivia.md",    AGENTS_DIR / "_trivia.lego")    # --explain (reliable/Opus)
RESEARCH = QuickieAgent("research", AGENTS_DIR / "researcher.md", AGENTS_DIR / "_research.lego")  # --research (researcher engine, lean/base image)

# claude flags for a progress-showing one-shot: emit the full stream-json event
# stream (needs --verbose) with token-level deltas (--include-partial-messages),
# which render_stream turns into a thinking ticker + a streamed answer. See
# launch/quickie/render.py for why reasoning text itself can't be shown.
STREAM_ARGS = ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]


def _gibberish() -> str:
    """A short throwaway session id — the user isn't meant to get attached to
    it; it just keeps each question's thread in its own (resumable-later) dir."""
    return uuid.uuid4().hex[:12]


def build_quickie_instance(registry: Registry, session: str, *,
                           agent: QuickieAgent = QUICK, is_brand_new: bool = True) -> Instance:
    """The Instance for one quickie question: `agent`'s hidden persona + lego
    (default QUICK; TRIVIA for `--explain`, RESEARCH for `--research`), the
    communal shared workspace mounted at /workspace, and a state dir parked
    under `quickie/` (via `state_dir_override`) rather than the main
    `instances/`. `is_brand_new=False` marks a `--resume` so compute_resume_flag
    can offer `--continue`. Pure construction — no disk/docker side effects — so
    it's unit-testable on its own."""
    build = load_lego(agent.lego)
    registry.validate_build(build, agent.lego)   # shipped file → the raising validate is right
    return Instance(
        agent=agent.label,
        md_path=agent.md,
        session=session,
        workspace=str(quickie_communal_workspace()),
        is_brand_new=is_brand_new,
        state_dir_override=quickie_state_dir_path(session),
        **resolve_build(build, agent.label, registry),
    )


def ask(question: str, *, resume_session: str | None = None, agent: QuickieAgent = QUICK) -> None:
    """Answer one question one-shot with `agent` (QUICK default; TRIVIA for
    `--explain`, RESEARCH for `--research`). With `resume_session` (an id from
    `q --history`) the question continues that existing thread via `--continue`;
    otherwise it opens a fresh thread under a throwaway id. Either way it stages
    state like a normal launch (minus picker / form / store / optional-creds),
    builds the image if it isn't cached, then runs `claude -p`. The thread
    persists under `quickie/` for later resume."""
    question = question.strip()
    if not question:
        sys.exit(
            f'Ask a follow-up:  q --resume {resume_session} "your question"   (quote it).'
            if resume_session else
            'Ask a question:  q "your question here"   (quote the whole question).'
        )
    require_docker()
    registry = call_or_exit(scan_all, AGENTS_DIR, exceptions=TagError)
    ensure_dir(quickie_communal_workspace())   # the /workspace mount source must exist, else docker root-creates it
    if resume_session is not None:
        if not quickie_state_dir_path(resume_session).is_dir():
            sys.exit(f"No quickie thread '{resume_session}'.  Run  q --history  to list them.")
        session, is_brand_new = resume_session, False
    else:
        session, is_brand_new = _gibberish(), True
    inst = build_quickie_instance(registry, session, agent=agent, is_brand_new=is_brand_new)
    resume_flag = compute_resume_flag(inst)      # ["--continue"] when the thread has a transcript; [] otherwise

    apply_tags(inst)                             # no-op for _quickie today (no handler tag); future-proof
    install_latest_md(inst)
    call_or_exit(install_settings, inst, registry, exceptions=TagError)
    ensure_shared_oauth_files()
    set_container_env(inst)
    set_container_mounts(inst)
    image = ensure_image(inst)
    run_container(inst, image, STREAM_ARGS, resume_flag, interactive=False,
                  print_prompt=question, stream_renderer=render_stream)
