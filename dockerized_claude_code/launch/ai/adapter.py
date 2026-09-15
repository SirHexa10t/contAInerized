"""The ADAPTER — what the launcher knows, in code, about one harness (an agent
CLI, a member of `agents/harness/`): the record of the facts it needs to run
it. This is the DATA half of the adapter `plans/harness_coupling.md` (shape A)
and `plans/harness_commonality.md` describe: everything that differs between
harnesses only by NAME (binary, flags, filenames, env vars, hosts) is a field
here, so the rest of the tree reads `active_adapter().<field>` instead of
spelling Claude Code's word. The BEHAVIOUR half — rendering settings and
policies, parsing transcripts and the event stream, composing the persona —
differs by STRUCTURE and arrives as methods when those seams are generalised
(`adding_an_ai.md`, order of work).

One instance per adapted harness lives in its own module (`claude_code.py`
today), keyed by the harness member's name; the registry and
`active_adapter()` are in `launch/ai/__init__.py`. The harness TAG
(`launch/tags/harness.py`) carries what a user reads — vendor, description,
the AIs it runs, binary, package — and a test holds the two halves together
(same name, same binary). Fields carry only what has a caller in the tree
today — the fuller questionnaire is the commonality matrix — and a harness
that lacks one is a `None`, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Adapter:
    key: str                         # the `agents/harness/<key>` member this adapter implements (the tag carries the display facts and the AIs it runs)
    name: str                        # the CLI's own name as the vendor writes it — "Claude Code", "Gemini CLI", "Codex CLI", "Grok Build" — for the strings a user reads; equals the member's fullname
    binary: str                      # the executable in the image (`docker run … <binary> …`; herdr matches it to pick `agent start`)
    herdr_agent_kind: str | None     # herdr's `agent start --kind <kind>` vocabulary for this CLI; None = herdr has no kind for it, members fall back to `pane run`
    config_dir_name: str             # the CLI's config root under the container user's home (`.claude`) — the instance state dir is bind-mounted there
    config_dir_env: str              # the env var that relocates that root (cluster members each get their own)
    session_name_env: str | None     # the env var that names the session inside the CLI (the cluster sets it to the member id); None if the CLI has none
    settings_filename: str           # the settings file the launcher generates and RO-mounts into the config root
    persona_filename: str            # the instruction file the launcher composes into the state dir (`CLAUDE.md`)
    commands_dirname: str            # slash-command dir under the config root, assembled per launch
    skills_dirname: str              # skills dir under the config root (the shared custom_skills/ mount)
    history_filename: str            # the per-launch input log at the state-dir root — the "last launched" signal
    transcripts_dirname: str         # where session transcripts live under the config root (`projects/<cwd-slug>/*.jsonl` for Claude Code)
    account_filename: str            # the shared account file (OAuth identity) mounted into the container
    credentials_filename: str        # the shared credentials file mounted into the config root
    print_flag: str                  # one-shot headless mode (quickie)
    continue_flag: str               # resume the most recent conversation of this config root
    effort_flag: str                 # pin the session's effort on the command line (see docker_config.effort_args for why a flag AND an env var)
    stream_args: tuple[str, ...]     # the flags that make print mode emit a line-delimited event stream quickie can render
    critical_hosts: tuple[str, ...]  # the hosts the CLI cannot operate without — the firewall resolves them first and aborts the launch if it cannot
