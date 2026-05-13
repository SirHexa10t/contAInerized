(Auto-generated on first launch by run.py — safe to edit or delete; only re-created if missing.)

This directory (`~/.claude-agents/user_extras/optional_creds/`) holds credentials
for cloud / dev CLI tools (aws, gcloud, gh, glab, kube, vercel, railway, jira,
etc.). Each recognised entry below becomes a bind-mount into agent containers at
the matching default path, so the corresponding CLI just works inside the container.

What goes in each entry — two patterns:

  <service>/                Put the CLI's normal config tree here, as you'd find
                            it on the host: e.g. `aws/credentials` + `aws/config`,
                            or the full contents of `~/.config/gcloud/`. The
                            directory is mounted into the container at the CLI's
                            expected path. See paths.py:OPTIONAL_CREDS_MOUNTS for
                            the full source→target table.

  <service>/token           For services that authenticate via an env-var token
                            instead of (or in addition to) a config file, drop a
                            plain-text `token` file inside that service's directory.
                            The launcher reads the file at launch and forwards its
                            contents to the container as the matching env var (the
                            CLI inside the container then picks it up the same way
                            it would on your host). Currently:

                              jira/token  →  $JIRA_API_TOKEN  (jira-cli)

                            Content: just the secret, no quotes, no `KEY=value`
                            framing. Leading/trailing whitespace is trimmed.

Example — a populated directory (only services you actually use need to exist;
this just shows both patterns side-by-side):

  ~/.claude-agents/user_extras/optional_creds/
  ├── README.txt          (this file)
  ├── aws/
  │   ├── credentials
  │   └── config
  ├── gh/
  │   └── hosts.yml
  ├── jira/
  │   ├── .config.yml     ← jira-cli config (server / login / project)
  │   └── token           ← API key; read by launcher → exported as $JIRA_API_TOKEN
  ├── ssh/
  │   ├── id_ed25519
  │   ├── id_ed25519.pub
  │   └── known_hosts
  └── npmrc               ← file (not a directory); bind-mounted as /home/claude/.npmrc

For [prog]-tagged agents, the matching CLI is also auto-installed in the prog image
whenever the cred dir is present (each tool has its own INSTALL_<TOOL> build-arg in
compose.prog.yml). Service-to-env-var mapping lives in OPTIONAL_CREDS_TOKEN_ENV_VARS
(paths.py) — adding a new service is one entry in each map.
