#!/usr/bin/env python3
"""`q` — the quickie one-shot question launcher (the alias target).

Thin entry: hand argv to launch.quickie.main, which parses it (argparse, so
`q -h` prints THIS tool's help, not claude's) and dispatches. A script at the
repo root (like run.py) so `import launch` resolves from any cwd. Run
`install_rc_alias.sh` to install the `q` alias (alongside `ai`), or add it
by hand:  alias q='python3 /path/to/repo/quick_question.py'
Usage:  q "why do elephants have big ears?"   (quote the whole question)
        q --history                            (list past threads)
        q --resume <id> "and their trunks?"    (continue a thread)"""

import sys

from launch.quickie import main

if __name__ == "__main__":
    main(sys.argv[1:])
