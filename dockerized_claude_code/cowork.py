#!/usr/bin/env python3
"""`cowork` — drive the multi-agent group-hosting hub (the alias target).

Thin entry: hand argv to launch.cowork.main, which parses it (argparse, so
`cowork -h` prints THIS tool's help, not claude's) and dispatches. A script at
the repo root (like run.py) so `import launch` resolves from any cwd.
Usage:  cowork serve                                  (run the hub)
        cowork roster --as <manager>                   (who could be recruited)
        cowork recruit <manager> <project> <peer>...   (create/extend a group)
        cowork send <group> <peer> "do this thing"     (deliver and wake)
        cowork status                                  (hub + every group)
        cowork close <group>                           (end a group)"""

import sys

from launch.cowork import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
