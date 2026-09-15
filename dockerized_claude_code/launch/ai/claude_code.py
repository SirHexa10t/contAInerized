"""Claude Code — the adapter for the `claude-code` harness (agents/harness/claude-code): the one record of its names.
Every value here used to be a literal spelled in the module that consumed it
(docker_config, agents_crud, quickie/ask, cluster/{launch_plan,herdr,launching},
firewall/resolver, paths, tags/engine); gathered 2026-09-12 as step 1 of
`plans/adding_an_ai.md`'s order of work, values unchanged. Sources: Claude Code's
own docs for the flags and files (`--continue`, `-p`, `--effort`,
`--output-format stream-json`; `~/.claude`, `CLAUDE_CONFIG_DIR`, `settings.json`,
`CLAUDE.md`, `projects/`, `history.jsonl`), and `plans/harness_commonality.md`
rows A–K for how each compares with the other harnesses.
"""

from .adapter import Adapter

CLAUDE_CODE = Adapter(
    key="claude-code",
    name="Claude Code",
    binary="claude",
    herdr_agent_kind="claude",
    config_dir_name=".claude",
    config_dir_env="CLAUDE_CONFIG_DIR",
    session_name_env="CLAUDE_CODE_SESSION_NAME",
    settings_filename="settings.json",
    persona_filename="CLAUDE.md",
    commands_dirname="commands",
    skills_dirname="skills",
    history_filename="history.jsonl",
    transcripts_dirname="projects",
    account_filename=".claude.json",
    credentials_filename=".credentials.json",
    print_flag="-p",
    continue_flag="--continue",
    effort_flag="--effort",
    # The full stream-json event stream (needs --verbose) with token-level
    # deltas (--include-partial-messages), which quickie's render_stream turns
    # into a thinking ticker + a streamed answer. launch/quickie/render.py says
    # why the reasoning text itself cannot be shown.
    stream_args=("--output-format", "stream-json", "--verbose", "--include-partial-messages"),
    # Served from Anthropic's own registered space, not a CDN — firewall/resolver
    # widens their pins to that block (its comment carries the RDAP evidence).
    critical_hosts=("api.anthropic.com", "console.anthropic.com"),
)
