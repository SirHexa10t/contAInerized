"""The `q` quickie tool — one-shot direct questions, answered via `claude -p`
and parked under ~/.ai-agents/quickie/. A leaf consumer of the launcher
core; the repo-root `quick_question.py` is its thin entry point, calling
`main`. Layers: cli.py (the one startup, then argparse: question / --ai /
--explain / --research / --history / --answer / --resume) → ask.py (ask, fresh
or resumed, on the lego's AI or the chosen one) + history.py (list threads)."""

from .ask import ask
from .cli import main

__all__ = ["ask", "main"]
