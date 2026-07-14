# ISSUES — Launcher (live tracker)

Open decisions and parked items, kept current after each fix pass. The full
audit narrative and per-pass change logs that used to live here were cleared
2026-07-13 — their outcomes are encoded in the code and its docstrings, the
test suite, `.claude_dev_guidelines`, and `.claude_summary`.

History in one breath: full-source audit (381 tests, ruff + mypy clean) →
**pass 1**: bug cluster B1–B9-minus-B4 + hardening (441) → **pass 2**:
structure S1–S9, agents-index relocation to `file_access`, modify-flow prompt
order, CDN widening + burst-batched firewall updater (526) → **pass 3**:
firewall coverage overhaul — wildcard entries, live-fetched provider ranges,
drift-heal refresher, IPv6 deny, enforcement-probe + updater/init race fixes
(570) → **pass 4**: Fable-5 conf upgrades (breakthrough / researcher /
thinker / default), per-conf `--effort` CLI passthrough (`effort_args` in
docker_config) fixing the launch-effort pin on fresh instances,
`/file-message` command (576) → **pass 5**: "mythos" family pre-add,
B4 clean-exit wrap (`call_or_exit` around the critical-DNS wait in
run_compose), `[project]` table in pyproject.toml as the dependency
source of truth (install_dependencies.sh + import-site comments now point
at it), mypy `disallow_untyped_defs` on for source (tests exempt via
override) (578) → **pass 6**: comment-density thinning — stripped
history-narration + restating comments in network / docker_config /
utils / file_access (net −12 comment lines); the rest of the ~48% is
deliberate contract/why/role/banner text, kept (578). Check stack green
after every pass.

## Open — awaiting decision

(none currently open.)

## Parked (out of scope by decision — revisit after the above)

- `refactoring_plan.md`'s modifier-taxonomy redesign (Changes A–G, decisions
  Q1–Q6 all open) — orthogonal to everything above.
- Longstanding known issues: unconditional per-launch `compose build` (no
  image-exists short-circuit), no CI gate, `network: host` build workaround.
- README and the Dockerfiles' install blocks were never deep-audited
  (`test_env_coverage` guards the env-var wiring).
- Picker PageUp/PageDown wraps around while Home/End clamp — UX taste.

## Recorded decisions (so they aren't re-litigated)

- The `pick_with_preview` prompt_toolkit TUI itself is accepted-untested;
  revisit with a pipe-input harness only if it starts regressing.
- CDN/provider widening deliberately trades "a provider block is shared with
  every customer of that CDN" for rotation-proofness — user-requested, only
  triggered by whitelisted hosts, documented at the range-fetch layer in
  `network.py`.
- Provider ranges are live-fetched with a cached-fallback chain — the earlier
  "baked ranges need a refresh cadence" question is obsolete by design.
- `paths.py`'s `DEFAULT_WORKSPACE` `is_dir` probe at import stays (documented
  tradeoff; the module is otherwise a true leaf).
- Effort is delivered through BOTH channels on purpose: the conf env var
  (request-level enforcement) and an explicit `--effort` CLI flag
  (releases Claude Code's launch-effort pin on new models so fresh
  interactive sessions run *and display* the conf's level; verified on CC
  2.1.207). A settings.json-generation approach (persisting `effortLevel`
  per instance) was tried 2026-07-14 and reverted — no visible effect.
- Mid-session whitelist edits (was open item 6): declined 2026-07-14 —
  relaunches are brief enough that applying new
  `user_extras/firewall_whitelist.txt` entries via relaunch is acceptable;
  no live re-read machinery.
- Tests are exempt from `disallow_untyped_defs` (per-module mypy override):
  annotating every `def test_*(self)` adds noise without safety — bodies are
  still checked against the annotated source API.
- No `[build-system]` table in pyproject.toml on purpose — the launcher runs
  from the repo (`python3 run.py`), it isn't pip-installed as a package;
  `[project]` exists solely as the dependency source of truth.
- Comment/docstring density (~48% of source lines) is deliberate and stays:
  a full 14-file review (2026-07-14) confirmed it's contract/why/role/banner
  text, not bloat. The only genuine trivia — history narration ("the old X",
  "used to") — was thinned from network / docker_config / utils / file_access
  (net −12 lines). Don't re-open as a bulk pass; fix stale comments
  opportunistically when touching their code.
