"""Generic helpers shared across launch/ modules. Domain-neutral — nothing
here knows about agents, modes, paths, or docker.

Imports nothing from sibling launch/ modules — kept as a leaf so it can be
pulled in anywhere without circular-import risk."""


def parse_lines(path):
    """Iterate non-empty, non-comment-only lines from `path`, with inline `#`
    comments stripped and surrounding whitespace trimmed. Yields nothing if the
    file is missing. Suits plain one-token-per-line config files (e.g. the
    firewall whitelist) — see agent_composition.load_conf for `KEY=VALUE` style."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            yield line
