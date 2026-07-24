"""The `q` quickie tool — one-shot direct questions, answered via `claude -p`
and parked under ~/.claude-agents/quickie/. A leaf consumer of the launcher
core; the repo-root `quick_question.py` is its thin entry point, calling
`main`. Layers: cli.py (argparse: question / --history / --resume) → ask.py
(ask, fresh or resumed) + history.py (list threads)."""

from .ask import ask
from .cli import main

__all__ = ["ask", "main"]
