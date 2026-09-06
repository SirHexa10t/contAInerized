"""Turning whitelist tokens into iptables rules, and applying a batch of them
to a live container.

The tail end of the `{firewall}` pipeline: everything upstream decides WHICH
addresses to open, and this decides what that means in iptables and how the
rules get there. Split out of `resolver.py` 2026-09-03 — it is the only part
of the resolver with no coupling to the phase machinery (no queues, no
threads, no shared mutable view), and the only part whose output is a shell
script, which is a good reason for it to be readable on its own.

Two rules per address by default (443 + 80), inserted at position 1 of the
OUTPUT chain — BEFORE init-firewall.sh's catch-all REJECT, which is why
arrival order never matters and rules can keep accumulating mid-session.

**The security boundary lives here.** `rules_for` validates every token
against the strict address/port shape before it is allowed anywhere near a
`sh -c` string; a token that fails is dropped with a warning rather than
escaped, because there is no legitimate whitelist token that needs escaping.
That is defense in depth on top of the resolver's own output validation —
the two exist independently so that neither one being wrong is sufficient.
"""

import sys

from ..container_probe import docker_exec_root_subprocess
from ..utils import split_host_port
from .whitelist import _IP_OR_CIDR_RE

# HTTPS + HTTP — opened for any whitelist entry that doesn't specify :port.
DEFAULT_OPEN_PORTS = ("443", "80")

# Rules per `docker exec sh -c` invocation — bounds the argv/script size.
# A full-whitelist launch is a few hundred rules → a handful of execs.
_BATCH_MAX_RULES = 100


def rules_for(token: str) -> list[str]:
    """iptables command strings opening `token` (`addr[:port]`; addr may be a
    CIDR) at position 1 of the OUTPUT chain — BEFORE the catch-all REJECT.
    Port absent → the default HTTPS+HTTP pair. Tokens failing the strict
    address/port validation are dropped with a warning: these strings get
    joined into a `sh -c` script, so nothing that hasn't matched
    `^[0-9./]+$`-shaped patterns may pass (defense in depth on top of
    `resolver._resolve_a_records`' own output validation)."""
    addr, port = split_host_port(token)
    if not _IP_OR_CIDR_RE.match(addr) or (port and not port.isdigit()):
        print(f"  warning: dropping malformed firewall token {token!r}", file=sys.stderr)
        return []
    ports = [port] if port else list(DEFAULT_OPEN_PORTS)
    return [f"iptables -I OUTPUT 1 -d {addr} -p tcp --dport {p} -j ACCEPT" for p in ports]


def flush(container_name: str, tokens: list[str]) -> None:
    """Apply `tokens` to the running container's iptables in chunks of
    ≤_BATCH_MAX_RULES rules, one `docker exec --user root sh -c`
    per chunk. `&&`-joined so a mid-chunk failure surfaces as a non-zero
    exit; each failed chunk retries once (duplicate -I inserts from a
    partially-applied first attempt are harmless) then warns and moves on
    — best-effort."""
    rules = [rule for token in tokens for rule in rules_for(token)]
    for i in range(0, len(rules), _BATCH_MAX_RULES):
        script = " && ".join(rules[i:i + _BATCH_MAX_RULES])
        result = docker_exec_root_subprocess(container_name, "sh", "-c", script)
        if result.returncode != 0:
            result = docker_exec_root_subprocess(container_name, "sh", "-c", script)   # one retry — transient exec races
        if result.returncode != 0:
            print(
                f"  warning: batched iptables insert failed ({len(rules[i:i + _BATCH_MAX_RULES])} rules): "
                f"{result.stderr.strip() or result.stdout.strip()}",
                file=sys.stderr,
            )
