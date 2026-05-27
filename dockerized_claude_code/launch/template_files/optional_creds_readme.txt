(Auto-generated on first launch by run.py — will be re-created if altered or missing.)

This directory (`~/.claude-agents/user_extras/optional_creds/`) holds credentials
for cloud / dev CLI tools (aws, gcloud, gh, glab, kube, vercel, railway, jira,
etc.). Each recognised entry below becomes a bind-mount into agent containers at
the matching default path, so the corresponding CLI just works inside the container.

What goes in each entry — three patterns:

  <service>/                Put the CLI's normal config tree here, as you'd find
                            it on the host: e.g. `aws/credentials` + `aws/config`,
                            or the full contents of `~/.config/gcloud/`. The
                            directory is mounted into the container at the CLI's
                            expected path. See paths.py:OPTIONAL_CREDS_MOUNTS for
                            the full source→target table.

                            Special case — `ssh/`: the launcher fixes up perms on
                            the host-side dir before mounting (700 on the dir,
                            600 on every file EXCEPT `*.pub` and `*_hosts` which
                            get 644), since ssh refuses to read keys with looser
                            perms. The contents are expected to be COPIES of
                            your everyday keys (or fresh agent-only keys) rather
                            than symlinks to `~/.ssh/` — chmodding the entries
                            here is part of the launch and propagates to whatever
                            the symlink targets if you used one.

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

  home/                     Catch-all for loose dotfiles that don't belong to a
                            known service (e.g. `.gitconfig`, `.git-credentials`,
                            `.gnupg/`, `.tmux.conf`). Each TOP-LEVEL entry under
                            `home/` is mounted as-is at the matching path under
                            `/home/claude/` — files become file mounts, directories
                            become whole-dir mounts. Subdirs within `home/` are
                            NOT walked (so `home/.config/git/config` doesn't work
                            — drop `.gitconfig` at the top of home/ instead, or
                            mount `.config/` as a whole dir).

                            The launcher refuses to shadow a mount it has already
                            staged: if `home/.bashrc` collides with the bundled
                            settings/bashrc.sh mount, the launch halts with a
                            clear message naming both paths. Move or rename the
                            home/ entry to proceed.

Example — a populated directory (only services you actually use need to exist;
this just shows the patterns side-by-side):

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
  │   ├── id_ed25519      ← chmod 600 by launcher
  │   ├── id_ed25519.pub  ← chmod 644 by launcher
  │   └── known_hosts     ← chmod 644 by launcher
  ├── home/
  │   ├── .gitconfig          ← file mount → /home/claude/.gitconfig
  │   ├── .git-credentials    ← file mount → /home/claude/.git-credentials
  │   └── .gnupg/             ← dir mount  → /home/claude/.gnupg/
  ├── npmrc               ← file (not a directory); bind-mounted as /home/claude/.npmrc
  └── .gitconfig          ← file (not a directory); bind-mounted as /home/claude/.gitconfig

For [code]-tagged agents, the matching CLI is also auto-installed in the code image
whenever the cred dir is present (each tool has its own INSTALL_<TOOL> build-arg in
compose.code.yml). Service-to-env-var mapping lives in OPTIONAL_CREDS_TOKEN_ENV_VARS
(paths.py) — adding a new service is one entry in each map.
