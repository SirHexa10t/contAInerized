"""`q`'s command line — an argparse front end so `q -h` prints THIS tool's help
(not claude's), and so the mode flags are consumed here rather than forwarded to
the model as prompt text. A thin dispatcher: the work lives in ask.py (ask a
question with a chosen agent, fresh or resumed) and history.py (list threads /
show a saved answer)."""

import argparse

from .ask import QUICK, RESEARCH, TRIVIA, ask
from .history import print_answer, print_history


def build_parser() -> argparse.ArgumentParser:
    """The `q` parser: an optional question, the agent flags `--explain` /
    `--research` (mutually exclusive), and the thread flags `--history` /
    `--answer <id>` / `--resume <id>`. Split out from main() so tests can parse
    without dispatching."""
    parser = argparse.ArgumentParser(
        prog="q",
        description='Ask one direct question, answered in one shot. Put your prompt in quotes.'
                    'If you needs to work with files, ask-for/reference them in '
                    '~/.claude-agents/quickie/communal/',
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
        "--history", action="store_true",
        help="List past question threads (timestamp, id, last question; oldest first) and exit.",
    )
    parser.add_argument(
        "--answer", metavar="ID",
        help="Print the saved answer for a past thread (an id from --history) and exit.",
    )
    parser.add_argument(
        "--resume", metavar="ID",
        help="Ask the question as a follow-up in an existing thread (an id from --history).",
    )
    parser.add_argument(
        "question", nargs="*",
        help="The question. Quote it so the shell keeps it as a single argument.",
    )
    return parser


def main(argv: list[str]) -> None:
    """Parse argv and dispatch. `--history` / `--answer` are standalone display
    modes (reject any other argument); otherwise ask the question with the
    selected agent (default QUICK; `--explain`→TRIVIA, `--research`→RESEARCH),
    optionally continuing the `--resume` thread."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.history or args.answer:
        if (args.history and args.answer) or _has_ask_args(args):
            parser.error("--history / --answer take no other arguments")
        print_history() if args.history else print_answer(args.answer)
        return
    agent = TRIVIA if args.explain else RESEARCH if args.research else QUICK
    ask(" ".join(args.question), resume_session=args.resume, agent=agent)


def _has_ask_args(args: argparse.Namespace) -> bool:
    """True if any ask-mode argument is set — used to reject them alongside the
    standalone `--history` / `--answer` display modes."""
    return bool(args.question or args.resume or args.explain or args.research)
