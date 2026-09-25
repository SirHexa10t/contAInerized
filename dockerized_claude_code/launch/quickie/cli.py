"""`q`'s command line — an argparse front end so `q -h` prints THIS tool's help
(not claude's), and so the mode flags are consumed here rather than forwarded to
the model as prompt text. A thin dispatcher: the work lives in ask.py (ask a
question with a chosen agent, fresh or resumed) and history.py (list threads /
show a saved answer).

The launcher's one startup (`startup.open_launcher`: the state-dir migrations,
then the tag tree) runs FIRST, before parsing — every verb here reads the
state dir (`--history` and `--answer` read the threads; a question writes one),
and the parser itself needs the tree: `--ai` offers the AI members
`agents/ai/` holds, and its help says which of them can answer today.
"""

import argparse

from ..ai import ADAPTERS
from ..paths import quickie_state_dir_path
from ..startup import open_launcher
from ..tags import Registry, TagError
from ..utils import call_or_exit
from .ask import QUICK, RESEARCH, TRIVIA, ask
from .history import print_answer, print_history


def ai_choices(registry: Registry) -> tuple[list[str], list[str]]:
    """(every AI member's name, the names whose default CLI has an adapter) —
    the `--ai` choices and the help's "can answer today" list. An AI without
    an adapted CLI is still a legal choice: `ask` refuses it with the one
    message every launch path uses, naming what an adapter needs."""
    names = sorted(registry.ais)
    runnable = [name for name in names if registry.ais[name].harness in ADAPTERS]
    return names, runnable


def build_parser(registry: Registry) -> argparse.ArgumentParser:
    """The `q` parser: an optional question, the agent flags `--explain` /
    `--research` (mutually exclusive), `--ai <name>` (one of the tree's AI
    members; the lego's own — claude — when omitted), and the thread flags
    `--history` / `--answer <id>` / `--resume <id>`. Split out from main() so
    tests can parse without dispatching."""
    names, runnable = ai_choices(registry)
    default_ai = registry.default_ai.name if registry.default_ai else "the lego's"
    parser = argparse.ArgumentParser(
        prog="q",
        description='Ask one direct question, answered in one shot. Put your prompt in quotes.'
                    ' If you needs to work with files, ask-for/reference them in'
                    ' ~/.ai-agents/quickie/communal/',
    )
    agent = parser.add_mutually_exclusive_group()
    agent.add_argument(
        "--explain", action="store_true",
        help="Answer with the 'trivia' agent — the answer plus connections and related tidbits.",
    )
    agent.add_argument(
        "--research", action="store_true",
        help="Answer with the research agent (deeper, source-checked). Not combinable with --explain.",
    )
    parser.add_argument(
        "--ai", metavar="NAME", choices=names,
        help=f"Which AI answers — one of: {', '.join(names)} (default: {default_ai}). "
             f"Only an AI whose CLI has an adapter can answer today: {', '.join(runnable) or 'none'}; "
             f"the others are described, not run (plans/adding_an_ai.md).",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="List past question threads (timestamp, id, last question; oldest first) and exit.",
    )
    parser.add_argument(
        "--answer", metavar="ID", nargs="?", const="",
        help="Print a thread's saved answer and exit — the LATEST thread with no id, "
             "or the one named (an id from --history).",
    )
    parser.add_argument(
        "--resume", metavar="ID", nargs="?", const="",
        help="Ask the question as a follow-up — in the LATEST thread with no id, "
             "or the one named (an id from --history).",
    )
    parser.add_argument(
        "question", nargs="*",
        help="The question. Quote it so the shell keeps it as a single argument.",
    )
    return parser


def resume_and_question(args: argparse.Namespace) -> tuple[str | None, str]:
    """`(thread to continue, question)` — untangling what argparse cannot.

    `--resume` takes an OPTIONAL id, so argparse hands it the next token
    whatever that token is: `q --resume "and their trunks?"` parses the
    QUESTION as the id. The disambiguation is the disk, not a guess about what
    an id looks like: a value naming no thread under `quickie/` is the first
    word of the question, and the thread meant is the latest (`""`). A value
    that does name one is the id, and the rest is the question."""
    question = " ".join(args.question)
    if args.resume and not quickie_state_dir_path(args.resume).is_dir():
        return "", " ".join(word for word in (args.resume, question) if word)
    return args.resume, question


def main(argv: list[str]) -> None:
    """Open the launcher (migrations, then the tree — the one startup), parse
    argv against that tree, and dispatch. `--history` / `--answer` are
    standalone display modes (reject any other argument; `--answer` with no id
    means the latest thread); otherwise ask the
    question with the selected agent (default QUICK; `--explain`→TRIVIA,
    `--research`→RESEARCH) on the lego's AI or the `--ai` one, optionally
    continuing the `--resume` thread (its id, or the latest with none)."""
    registry = call_or_exit(open_launcher, exceptions=TagError)
    parser = build_parser(registry)
    args = parser.parse_args(argv)
    # `is not None`, not truthiness: `--answer` with no id is the LATEST
    # thread and arrives as "", which is a request, not an absence.
    answering = args.answer is not None
    if args.history or answering:
        if (args.history and answering) or _has_ask_args(args):
            parser.error("--history / --answer take no other arguments")
        print_history() if args.history else print_answer(args.answer)
        return
    agent = TRIVIA if args.explain else RESEARCH if args.research else QUICK
    resume, question = resume_and_question(args)
    ask(question, registry, resume_session=resume, agent=agent, ai=args.ai)


def _has_ask_args(args: argparse.Namespace) -> bool:
    """True if any ask-mode argument is set — used to reject them alongside the
    standalone `--history` / `--answer` display modes. `--resume` counts by
    PRESENCE, not truthiness: with no id it arrives as "" and still means a
    question is being asked."""
    return bool(args.question or args.explain or args.research or args.ai) or args.resume is not None
