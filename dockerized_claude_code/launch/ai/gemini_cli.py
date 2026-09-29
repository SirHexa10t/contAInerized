"""Gemini CLI — the adapter for the `gemini-cli` harness (agents/harness/gemini-cli): the record of its names.

The second harness the launcher knows, and the first it can BUILD but not yet
START (`startable=False`): its image layer exists, but its container needs a
config dir, a persona file and credentials laid out the Gemini way
(plans/adding_an_ai.md, order of work step 4) before it can run. Until then
every launch path refuses it before any docker work, with a sentence that
says so (`refusal_for`).

Sources, 2026-09-25: the CLI itself, v0.61.0 (`gemini --help`, and the
installed bundle's own source — packages/core's paths.ts, storage.ts,
chatRecordingService.ts, logger.ts), installed into a scratch prefix with
GEMINI_CLI_HOME pointed at a scratch home so nothing global was touched; and
`herdr agent start --help` for the agent kind. plans/harness_commonality.md
rows A–K compare it with the other harnesses.
"""

from .adapter import Adapter

GEMINI_CLI = Adapter(
    key="gemini-cli",
    name="Gemini CLI",
    binary="gemini",
    startable=False,                 # builds; does not start until step 4 lands its config dir, persona and credentials
    herdr_agent_kind="gemini",       # `herdr agent start --kind` lists it
    config_dir_name=".gemini",
    config_dir_env="GEMINI_CLI_HOME",
    config_dir_env_parent=True,      # GEMINI_CLI_HOME replaces HOME; the CLI creates .gemini INSIDE it
    session_name_env=None,           # none: `--session-id` sets a new session's UUID, not a name
    settings_filename="settings.json",
    settings_base=None,              # step 4 writes one (general.enableAutoUpdate off, among others); Claude Code's would mean nothing here
    config_files=(),                 # neither of Claude Code's (its statusline script, its key bindings) means anything to this CLI
    persona_filename="GEMINI.md",    # the default of the `context.fileName` setting, which can rename it
    commands_dirname="commands",     # ~/.gemini/commands (Storage.getUserCommandsDir); the file format is step 4's to verify
    skills_dirname="skills",         # ~/.gemini/skills (Storage.getUserSkillsDir)
    # Per project, unlike Claude Code's single history.jsonl at the root:
    # tmp/<project-id>/logs.json, with <project-id> assigned by the CLI's own
    # projects.json registry (the cwd's name — a container's /workspace
    # becomes `workspace`, observed 2026-09-25).
    history_glob="tmp/*/logs.json",
    transcripts_dirname="tmp",
    # session-<timestamp>-<first 8 of the session id>.jsonl, one JSON record
    # per line, turns typed `user` and `gemini` (not `assistant`).
    transcript_glob="*/chats/session-*.jsonl",
    subagent_transcript_glob=None,   # sub-agents write <session-id>.jsonl beneath chats/; the exact layout is step 7's to verify
    transcript_format=None,          # found (so resume works), not yet parsed — step 7
    # No mountable login file. The CLI's Google sign-in is keychain-first with
    # a fallback file encrypted against the host and user, so it cannot be
    # carried in from the host; a container authenticates with the AI's API
    # key (GEMINI_API_KEY in credentials/keys/gemini.env, plans/credentials.md).
    auth_files=(),
    print_flag="-p",
    continue_args=("--resume", "latest"),
    effort_flag=None,                # thinking is a settings level (thinkingLevel), never a flag
    stream_args=("--output-format", "stream-json"),
    # The model endpoint on the API-key path the launcher uses. The Google
    # sign-in path would add oauth2.googleapis.com, accounts.google.com and
    # the Code Assist endpoint; telemetry (monitoring.googleapis.com) is off.
    critical_hosts=("generativelanguage.googleapis.com",),
)
