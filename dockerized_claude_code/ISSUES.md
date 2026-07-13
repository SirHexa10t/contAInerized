# ISSUES — Launcher Refactoring Audit

Full-source audit of the launcher (every module read end-to-end, check stack run,
claims verified empirically where possible). Optimization basis requested for the
pass: **readability/conciseness first, correctness and test coverage as co-goals.**

**Status legend:** ✅ fixed (with regression test) · ⏸ deferred by decision · ⬜ open — awaiting decision.

Check-stack state:
- At audit time: 381 tests passing, `ruff check .` clean, `mypy launch/ run.py` clean.
- After the first fix pass (B1–B9 minus B4, plus the "small ones"): 441 tests passing, ruff clean, mypy clean.
- After the second pass (structure S1–S9, index relocation, modify-flow UX, network/CDN overhaul, coverage + doc bundles — see §12): **526 tests passing**, ruff clean, mypy clean.
- After the third pass (firewall coverage overhaul: wildcards, live-fetched provider ranges, drift-heal refresher, IPv6 handling — see §13): 563 tests passing, ruff clean, mypy clean.
- After the third-pass launch-flakiness fix (self-test probe + updater/init race — see §13 "Post-release fix"): **570 tests passing**, ruff clean, mypy clean.

---

## 1. Summary

The codebase is in genuinely good shape: clean layering that matches its own written
guidelines, a fast behavior-focused test suite, and essentially zero dead code (every
exported helper has live callers). No stabilization pass needed. The highest-value
remaining item is **closing the test gap on `network.py`** — the security-critical
`{auto}` firewall module has no tests — followed by `menu_picker.py`'s pure parts.
The bug cluster found by the audit (§2) is now fixed and regression-tested.

## 2. Bugs & exceptional cases

### ✅ B1 — Status line rendered the literal text "None" when no OAuth email existed
`claude_code_config.build_status_line` interpolated the raw `read_json_field` result
(`None` before first login) straight into the f-string; its docstring claimed the
prefix "drops out". **Fixed:** the whole `email : ` segment now drops out when the
field is absent. Tests: `test_claude_code_config.TestBuildStatusLine`.

### ✅ B2 — Fable-family agents sorted dead last in the picker
`agents_crud.ORDERED_MODEL_FAMILIES` was `["opus", "sonnet", "haiku"]`; every
`claude-fable-5` conf parsed as *unknown family* and sank below haiku — the most
capable agents sorted last. **Fixed:** `"fable"` added at the front. Guard test added
that walks every shipped conf and asserts its `ANTHROPIC_MODEL` parses to a known
family, so the next model-family launch can't silently regress the sort. Tests:
`test_agents_crud.TestOrderedModelFamilies`, `TestAgentSortKeyFamilies`.
- ⬜ Open sub-decision: also pre-add `"mythos"` (same tier as fable; no shipped conf
  uses it yet)?

### ✅ B3 — A corrupted JSON state map bricked every subsequent launch
Two halves, both fixed:
1. `file_access.write_text` was a plain (tearable) write — a Ctrl+C mid-save could
   truncate `agent_workspace_map.json`. **Now atomic** (same-directory temp file +
   `os.replace`; interrupt-safe, no temp litter, parent auto-create preserved). This
   also makes the "atomic rewrite" claims in `network.py`'s status-file docstrings
   true — they previously described behavior that didn't exist.
2. `file_access._cached_load_json_map` didn't catch `JSONDecodeError` — a corrupt map
   produced a raw traceback on every launch until hand-repair. **Now exits cleanly**,
   naming the file and pointing at `python -m launch.audit`.
   `audit._load_or_issue` was decoupled from the cached loaders (it parses the maps
   directly) so the audit still reports the same corruption non-fatally.
   Tests: `test_file_access.TestWriteTextAtomic`, corrupt/empty-map cases in
   `TestJsonMapCache`, `test_audit.TestLoadOrIssue`.

### ⏸ B4 — `{auto}` critical-DNS failure crashes with a raw traceback (deferred)
`network._phase1_worker` raises `RuntimeError` when the critical Anthropic domains
fail to resolve; it propagates through `wait_for_critical_addresses()` →
`docker_config.run_compose` → uncaught in `launch()`. `compose_chain` gets
`call_or_exit` treatment; this path doesn't. Repro: `{auto}` launch with DNS down.
Proposed fix: wrap the `run_compose` call (or the wait inside it) with
`call_or_exit(..., exceptions=RuntimeError)`. **Deferred by decision — discuss.**

### ✅ B5 — An empty agent `.md` crashed the picker
`menu_picker._agent_description` did `splitlines()[0]` → `IndexError` on a zero-byte
file. **Fixed** with a safe first-line default (`""`). Tests:
`test_menu_picker.TestAgentDescription`.

### ✅ B6 — `parse_stem` silently swallowed malformed filenames
An unclosed bracket (`poet[code.md`) parsed to *zero tags* with no warning — the
agent launched without its toolchain. **Fixed — contract change:** `parse_stem` /
`parse_agent_name` now raise `ValueError` on malformed stems (missing name, unclosed
or empty group, stray text between groups; the old lenient `("", [], None)` fallback
for an empty stem is gone). At the one boundary where raising would be wrong —
`paths.AGENT_MD_BY_NAME`, built at import — malformed files are **skipped with a
stderr warning** naming the file, so one typo neither crashes every launch nor hides.
Tests: `test_utils.TestParseStemMalformed`, `test_paths.TestAgentMdIndex`.

### ✅ B7 — `""` and `None` workspace map entries were handled inconsistently
`validate_workspace`'s docstring claimed empty-string "is normalized to None here" —
no normalization happened (frozen dataclass), and `resolve_target` re-prompted only
on `is None`, so a `""` entry silently fell back to `DEFAULT_WORKSPACE` instead of
re-prompting. **Fixed:** `resolve_target` treats `not workspace` as missing (both
re-prompt); docstring corrected to describe reality. Tests:
`test_run.TestResolveTarget` (including the invalid-dir exit path).

### ✅ B8 — Dry-run wasn't fully dry
`prompt_install_failures` ran a real `docker run --rm` even under `--dry-run` —
spinning up a container to read a necessarily-stale failure log from a *previous*
build, and potentially prompting about it misleadingly. **Fixed:** it now no-ops on
dry-run; `launch()`'s docstring updated to name both dry-run gates. Tests:
`test_docker_config.TestPromptInstallFailuresDryRun`.

### ✅ B9 — The docker-presence check ran *after* the interactive picker
A docker-less user answered the full picker + workspace + session + mode prompts,
then learned the launch couldn't happen. **Fixed:** `require_docker()` moved inside
`gather_input`, between `parse_cli` (so `--help` still works without docker) and
`select_agent` (so no prompts precede the gate). Still fires on dry-run. Tests:
`test_run.TestGatherInput` (order-asserting), orchestrator tests adjusted.

### Small ones
- ✅ **Future mtimes rendered garbage relative times** (clock skew → "23 hours ago").
  `utils.relative_time` now clamps negative deltas to "just now". Tests in
  `test_utils.TestRelativeTime`.
- ✅ **`known_hosts2` didn't match the `_hosts` suffix check** in
  `file_access.enforce_ssh_dir_perms`, contradicting its docstring (harmless — 600
  still readable by ssh — but the docstring lied). Now matches `("_hosts", "_hosts2")`.
  Test: `test_known_hosts2_chmod_644`.
- ✅ **Missing space in the `home/` clash message** (`"the home/entry"`) in
  `user_additions.optional_creds_mounts` — message now names the full entry.
- ✅ **`add_docker_mount` accepted conflicting duplicate mounts** — same target from
  two sources emitted two `-v` flags (cryptic docker error at run time); same source
  at a new target silently *replaced* the earlier mount (source-keyed dict). Both now
  raise `RuntimeError` at staging time; identical re-stages remain idempotent no-ops.
  The user-reachable clash path (`home/` contents-mounts) still gets its friendlier
  pre-check message first. Tests: `test_docker_config.TestAddDockerMountCollisions`.

### Verified non-issue
The `www.`-prefixed entries in `BUILTIN_FIREWALL_DOMAINS` (e.g.
`www.esbuild.github.io`, `www.raw.githubusercontent.com`) looked like phantom
hostnames that would pollute the agent-visible `failed:` list and waste cascade
retries. Live DNS testing (with an NXDOMAIN control) showed **all tested entries
resolve** — the "list `www.X`, implicitly allow `X`" convention works as designed.
No change needed.

## 3. Dead code & redundancy — ✅ done

Near-none found (a genuinely positive result; every exported helper in
`utils`/`file_access` has live callers, no unused imports, no orphan modules).
- ✅ Dead `global` statement in `network.wait_for_critical_addresses` removed.
- ✅ Duplicated session-collision loop unified: `prompt_session(agent, workspace,
  current=None)` now serves both flows — modify passes `current=` (the existing
  name is always accepted; other collisions rejected). Tests:
  `test_menu_picker.TestPromptSession`.
- ✅ Cosmetic: EOF blanks trimmed; "leaking-underscore" typo fixed.

## 4. Dependency findings — ⬜ open

All three runtime deps are healthy; the gap is that **no manifest declares them**
(`pyproject.toml` has only tool config; the dep list lives in
`install_dependencies.sh` and import-site comments).

| Package | Verdict | Notes |
|---|---|---|
| `prompt_toolkit` | 🟢 Keep | TUI foundation (picker); active, ecosystem staple (IPython's TUI layer), 1 transitive dep (`wcwidth`). No lighter full-screen-TUI alternative. |
| `rich` | 🟢 Keep | Markdown→ANSI for previews + F8 legend; very active, huge adoption, 3 transitive deps (`markdown-it-py`, `pygments`, `mdurl`). Hand-rolling this would be a regression. |
| `python-dotenv` | 🟢 Keep (noted alternative) | Single call site (`dotenv_values` in `file_access.load_conf`) on simple `KEY=VALUE` confs. Active, zero transitive deps, ~25 KB. A ~15-line stdlib parser could replace it if minimal-deps ever becomes the priority. |

- ⬜ Proposal: add a `[project]` table (`name`, `version`, `requires-python`,
  `dependencies`) to `pyproject.toml` — one source of truth, `pip install -e .`-able;
  `install_dependencies.sh` and the `/test-project` preflight stop hardcoding the
  list. Trivial effort. Lockfile decision rides on this (known issue: no lockfile,
  no CI gate).

## 5. Structural simplifications — ✅ done (S1–S9)

File-level verdict first: **every module passes the one-sentence-role test — no
splits, merges, or removals warranted** (role table in §9). Within files:

- ✅ **S1** — `LaunchOptions` NamedTuple in `run.py`: `parse_cli`/`gather_input`
  return it, `launch()` reads `opts.*`; tuple-unpacking call sites keep working.
- ✅ **S2** — `HostnameEntry` is a NamedTuple; the opaque `t[1]` index reads are gone.
- ✅ **S3** — `network._expand_whitelist` extracted (pure: dedupe, `*.`-strip,
  `www.`→apex, literal/hostname split, sorted) with direct unit coverage
  (`test_network.TestExpandWhitelist`).
- ✅ **S4** — `compose_chain` now dispatches `_apply_<value.lower()>` by naming
  convention, looked up at call time (patch-friendly for tests; auto-wires new
  modifiers). All handlers share the uniform `(inst_id)` signature.
  `test_essential_files` still enforces the pairing; the add-a-modifier recipe in
  `.claude_dev_guidelines` lost its "add one conditional" step.
- ✅ **S5** — `WORKSPACE_IN_CONTAINER` constant added to `paths.py`;
  `CLAUDE_SUMMARY_IN_CONTAINER` derives from it; `docker_config` uses it.
- ✅ **S6** — `force_remove` no longer blocks on `input()`; the keypress gate moved
  to `agents_crud.delete_instance` via `prompt_keypress` (the disk layer only prints).
- ✅ **S7** — superseded by the index relocation (§12): the agents/ glob left
  `paths.py` entirely for a lazily-cached `file_access.agent_md_index()`, so no
  import-time directory listing remains. The `DEFAULT_WORKSPACE` `is_dir` probe
  stays (documented tradeoff, left deliberately) — `paths.py` is otherwise a true
  leaf now (zero in-project imports).
- ✅ **S8** — `BUILTIN_FIREWALL_DOMAINS` moved to
  `template_code/firewall_domains.py` (data-only module), joined by the new
  `CDN_IPV4_RANGES` provider table.
- ✅ **S9** — annotation sweep done. Behavioral side effect worth knowing:
  `load_conf` now returns `dict[str, str]` and drops valueless conf keys (a bare
  `KEY` line) instead of forwarding them as `-e KEY=None`. The
  `disallow_untyped_defs` question remains open (§11).

**The "shorten the code" elephant:** 46% of source lines are comments/docstrings
(measured ~2,356 of ~5,089 at audit time). Much is high-value rationale, but it's
also where drift lives (§7). Whether to thin *stale or restating-the-obvious*
documentation is a policy decision — ⬜ open.

## 6. Test coverage — partially addressed, remainder ⬜ open

This pass added 60 tests (381 → 441), including first-ever coverage for
`claude_code_config` and `menu_picker`'s pure helpers, plus error-path tests
(corrupt maps, malformed stems, mount collisions, invalid workspaces).

Second-pass additions (441 → 526 tests):
- ✅ 🔴 **`test_network.py` — 54 tests** over the `{auto}` security surface:
  `_expand_whitelist`, `_cascade` retry semantics (fake resolver),
  resolution-cache round-trip + TTL gate, `_WhitelistResolutionStatus`
  transitions + YAML shape (incl. the new `cdn:` section), `_index_by_host`,
  CDN detection/widening policy (`_cdn_provider_ranges`, `_tokens_for`),
  resolver-output validation, `_iptables_rules_for` (incl. shell-injection
  rejection), `_flush_rules` chunking/retry, and synchronous
  `_updater_worker` batching.
- ✅ 🟡 `test_menu_picker.py` extended: `continuable_instances` (orphan skip,
  mode conversion, CURRENT/INVALID/placeholder flags, sort order),
  `_modifier_display` ANSI→fragment round-trip, unified `prompt_session`.
- ✅ 🔵 `test_summary_helper.py` — 14 tests for `settings/_summary.py`
  (noise filtering, manifest parse/save/refusal, NEW/CHANGED/DELETED classify).

Remaining:
- ⬜ B4's error path (critical-DNS failure → clean exit) gets its test when B4
  is fixed.
- Accepted-untested (decision recorded): the `pick_with_preview` TUI itself —
  revisit with prompt_toolkit's pipe-input harness only if it starts regressing.

## 7. Documentation drift — ✅ done (one follow-up recommended)

Fixed in the first pass (halves of code fixes): `validate_workspace` (B7),
`launch()`/`gather_input` dry-run + docker-gate wording (B8/B9),
`enforce_ssh_dir_perms`, `write_text`/network "atomic" claims (B3),
`prompt_install_failures` (B8), stale test comments.

Fixed in the second pass:
- ✅ `menu_picker.py` docstring — `select_agent()` return shape corrected (bare
  identities, not `('new', …)` tuples); `prompt_session` signature documented.
- ✅ `agents_crud.py` docstring — dead `warn_dood_with_auto` reference replaced
  with the real `warn_if_dangerous_modes` routing note.
- ✅ `file_access.py` docstring — the index lives here now (`agent_md_index`),
  and the docstring says so.
- ✅ `.claude_dev_guidelines` — `launch/templates/` → `template_files/`;
  identity-chain typo fixed; role list refreshed (template_code trio incl.
  firewall_domains, paths as true leaf, file_access owning the index, network's
  new role); dependency-direction diagram redrawn; add-a-modifier recipe updated
  for convention dispatch; new "directory-contents lookup" placement example.
- ✅ `.claude_summary` — the four stale claims corrected in place
  (docker gate, `continuable_instances` location, test counts, fable models)
  plus the now-stale paths/network role lines and the `parse_cli` tuple note;
  header notes the targeted-fix state. **Follow-up:** a full `/write-summary`
  regeneration is still recommended — this pass changed a lot of surface.
- ✅ `modifier_prompts.py` `{auto}` body — whitelist described accurately
  (~140 curated domains + user whitelist + CDN-block behavior).
- ✅ Also caught: `network.py`'s cascade-budget comment claimed a 29s worst case;
  the stages sum to 50s.

## 8. High-impact warnings

- Firewall-adjacent edits touch the `{auto}` security posture — review with
  security eyes. **The CDN widening (§12) is the deliberate, user-requested case:**
  allowing a provider block makes every customer of that CDN on those addresses
  reachable, not just the whitelisted host. Documented loudly in
  `template_code/firewall_domains.py`; widening only triggers for whitelisted
  hosts detected on a provider block, and only for default-port entries.
- S4 changed the documented add-a-modifier recipe — `.claude_dev_guidelines` and
  the handler convention moved together (done).
- Contract changes landed in this pass (intentional, tested): `parse_stem` raises on
  malformed stems (was lenient); malformed agent files are skipped-with-warning from
  the index; `add_docker_mount` rejects conflicting duplicates (was
  last-write-wins); `prompt_install_failures` no-ops on dry-run; `require_docker`
  fires inside `gather_input` (before the picker, after `--help`).

## 9. Module role table (structural audit deliverable)

| File | Role |
|---|---|
| `run.py` | Seven-stage `launch()` orchestrator: input → validate → resume → persist → categorise → setup → run. |
| `launch/utils.py` | Domain-neutral pure helpers: formatting, parsing, sorting, subprocess shapes, prompt primitives. |
| `launch/paths.py` | Every host + container path constant, mount dict, and path-builder lambda — true leaf, declarations only. |
| `launch/file_access.py` | Sole disk-I/O chokepoint: reads, atomic writes, scans, stats, caches, sudo-fallback removal, agents/ name→md index. |
| `launch/structs.py` | Identity dataclasses + the `InstanceModifiers` taxonomy and its coloring. |
| `launch/compose_env.py` | Compose env-var staging and `-e`-flag emission (`ComposeEnvKey` + accumulator + formatters). |
| `launch/docker_config.py` | Docker CLI wrappers, bind-mount accumulator, image-chain naming, build/run orchestration. |
| `launch/agent_modifiers_handler.py` | Modifier semantics: chain-composition dispatch, mode prompts, danger warnings, cache lifecycle. |
| `launch/agents_crud.py` | Persistent instance-state CRUD, identity factories, picker sort keys. |
| `launch/user_additions.py` | User-side container contributions: optional-cred mounts + first-launch template plants. |
| `launch/network.py` | `{auto}` firewall: whitelist expansion + CDN widening, two-phase DNS cascade, burst-batched iptables updater, status file. |
| `launch/menu_picker.py` | prompt_toolkit picker UI, line prompts, launch banner. |
| `launch/claude_code_config.py` | Host-staged in-container UX: status line + terminal title. |
| `launch/audit.py` | Read-only state-correctness checker (`python -m launch.audit`). |
| `launch/template_code/` | Pure data: prompt copy, docker-side strings, CLAUDE.md addendums, firewall domains + CDN ranges. |
| `launch/template_files/` | First-launch user-side file templates. |
| `launch/benchmark/` | Ad-hoc micro-benchmarks, outside the suite. |
| `settings/_summary.py` | In-container manifest diff/save helper backing `/write-summary`. |

Every row summarizes cleanly — the file layout needs no surgery.

## 10. Out of scope (noted, untouched)

- `refactoring_plan.md`'s modifier-taxonomy redesign (Changes A–G, decisions Q1–Q6
  open) — everything in this document is orthogonal to it.
- Known-issues items not re-litigated: unconditional per-launch `compose build`, no
  CI gate, `network: host` build workaround, DNS-pin drift.
- README and the Dockerfiles' install blocks: skimmed, not audited
  (`test_env_coverage` guards the env-var wiring).
- Picker PageUp/PageDown wraps around while Home/End clamp — UX taste, flagged only.

## 11. Decisions needed (remaining)

1. **B4** — wrap the critical-DNS failure path in `call_or_exit` (clean exit instead
   of traceback)? Includes its error-path test.
2. **B2 follow-up** — pre-add `"mythos"` to `ORDERED_MODEL_FAMILIES`?
3. **Bundle F (packaging)** — `[project]` deps table in `pyproject.toml`
   (+ lockfile?).
4. **Mypy strictness** — adopt `disallow_untyped_defs` now that S9 landed?
5. **Comment-density policy** — keep the 46% documentation style as-is, or authorize
   a targeted thinning pass limited to stale/redundant docstrings?
6. **`/write-summary` re-run** — recommended after this pass's surface changes
   (targeted corrections applied; a regeneration would make it authoritative again).
7. **CDN range maintenance** — the curated blocks in
   `template_code/firewall_domains.py` are long-stable published lists, but they do
   evolve; decide a refresh cadence (or a future fetch-and-pin tool).

## 12. Second-pass change log (this round's non-audit items)

- **Index relocation** — `AGENT_MD_BY_NAME` (import-time glob in `paths.py`)
  became `file_access.agent_md_index()`, a lazily-cached function in the disk
  layer where directory listings belong. `paths.py` is now a true leaf (zero
  in-project imports). All eight consumers rewired; tests moved to
  `test_file_access.TestAgentMdIndex`.
- **Modify-flow UX order** — modifying an instance now prompts in the same order
  as creating one: workspace path → session name → modes (was name-first). The
  session prompt is the shared `prompt_session` (see §3).
- **CDN widening** — whitelisted hosts that resolve into a known provider block
  (Cloudflare / Fastly / GitHub / CloudFront — `CDN_IPV4_RANGES`) get the whole
  containing block whitelisted instead of pinned IPs, killing the
  rotation-breaks-CDN-sites failure that forced manual whitelist additions.
  Applies to Phase 1 too (api.anthropic.com's block lands in the *initial*
  ruleset). Per-launch block dedupe; IPs covered by a block emit no extra rule;
  explicit-port entries stay pinned (scope safety). The agent-visible status
  file gained a `cdn:` section naming widened hosts.
- **Batched firewall updater** — the Phase-2 updater drains resolution bursts
  and applies each as ONE `docker exec sh -c` (≤100 rules/exec, `&&`-joined,
  one retry). Every token is regex-validated before entering the script
  (resolver output is treated as untrusted). Measured by
  `benchmark/bench_firewall_updater.py`: the full builtin list (278 rules)
  applied in **3 execs / 0.16s** vs the old per-rule scheme's **278 execs /
  13.9s** at a simulated 50ms exec latency (~87×; real docker-exec latency is
  higher, so the real-world gap is larger).
- **Resolver-output validation** — `_resolve_a_records` now drops any token
  that isn't a plain IPv4 address; `_iptables_rules_for` re-validates before
  scripting (defense in depth).

## 13. Third-pass change log — firewall/CDN coverage overhaul

Trigger: recurring in-container `ConnectionRefused` on hosts that were *supposed*
to be whitelisted — CDN subdomain hosts that rotate or are minted per request
(uncoverable by per-host entries), `github.com` going dark mid-session after the
machine's DNS answers changed (its per-POP `/32` edges churn too fast for any
curated block list), release downloads dying on the unlisted
`release-assets.githubusercontent.com` redirect host, and pasted IPv6 literals
burning the full DNS cascade into `failed:` noise.

- **Wildcard whitelist entries** — `*.foo.com` is no longer silently stripped.
  The base host resolves; if it sits on a known CDN provider, ALL of that
  provider's published blocks open (subdomains can't be enumerated via DNS, so
  the provider's whole edge is the only IP-shaped grant matching what `*.`
  asks). Unknown-provider wildcards degrade to base-host pinning and are
  surfaced in a new `wildcard_gaps:` status section. An explicit `:port`
  narrows the block tokens rather than downgrading to pinning.
- **Provider ranges fetched, never baked** — the static `CDN_IPV4_RANGES`
  table is gone; no IP address space lives in the source. Each provider's
  published list is fetched at launch (`network._RANGE_FETCHERS`: cloudflare
  ips-v4 text, fastly public-ip-list JSON, github /meta edge services, AWS
  ip-ranges filtered to CLOUDFRONT, google as the netmask-aware difference
  goog.json − cloud.json — provider services without rentable-cloud space)
  and cached per provider under `~/.claude-agents/cdn_ranges/`. Degradation
  chain per provider: fresh cache → live fetch (saved back) → stale cache
  (warn) → provider skipped for the launch (its hosts stay IP-pinned).
  Fetched bodies are external input: everything funnels through
  `_clean_cidrs` (parse-as-IPv4-or-drop, collapse) before rule generation.
  Live fetching also covers what curation never could — e.g. GitHub's
  fast-churning per-POP `/32` edges are now simply part of the set.
- **Post-launch drift healing** — after Phase 2's stream ends, the updater
  hands off to a refresher daemon that re-resolves the entire hostname list
  every 5 minutes and batch-inserts rules for newly-reported addresses.
  Additive only (nothing revoked mid-session; a failed cycle never demotes a
  host). This closes the VPN-swap / CDN-steering stranding the 6h cache used
  to make WORSE — and heals launch-time `failed:` hosts, which the status file
  now moves back to `resolved:`.
- **Cache demoted from substitute to safety net** — every launch resolves
  fresh DNS for every host; the cross-launch cache only unions in (host vs
  container resolver divergence) and rescues outright failures. Only fresh
  answers are persisted back, so a dead IP ages out after one TTL instead of
  being immortalized by the rolling mtime.
- **Cache TTL extended to 3 days** — one `_CACHE_TTL_SECONDS` shared by the
  resolved-domains cache and the per-provider range caches (was 6h for
  resolutions). Safe at 3 days precisely because of the demotion above:
  neither cache ever masks live data — stale resolution entries can only ADD
  rules, and published provider ranges change on the order of months.
- **IPv6 entries skipped with reason** — v6 literals/CIDRs land in a new
  `skipped:` status section ("IPv4-only; whitelist the hostname or v4 range
  instead") instead of cascading through 50s of DNS timeouts into `failed:`.
- **IPv6 egress denied in-container** — `init-firewall.sh` now applies an
  ip6tables deny-all (loopback + established-inbound replies only). On a
  v6-enabled docker network the old script left v6 completely unfirewalled —
  a full whitelist bypass. Aborts the launch if a v6 default route exists but
  ip6tables can't apply.
- **Builtin list fixes** — added `objects.githubusercontent.com` +
  `release-assets.githubusercontent.com` (where GitHub release downloads
  actually 302), `gist.githubusercontent.com`, `static.rust-lang.org` (rustup
  components inside `[code]{auto}`); replaced the fictitious
  `www.raw.githubusercontent.com` / `www.objects.githubusercontent.com` forms
  (they only resolved via wildcard DNS luck) with the real hostnames.
- **Status file** — `failed:` reworded (hosts are re-attempted by the
  refresher), new `skipped:` + `wildcard_gaps:` sections, `cdn:` notes
  wildcard-widened hosts as `<provider> (all blocks — wildcard)`.
  `FIREWALL_NOTICE` and the whitelist template document all of it.
- **Dead code** — `_WhitelistResolutionStatus.resolved_snapshot()` removed
  (its only caller now persists `_fresh_resolutions` instead).
- Tests: 563 (was 526) — wildcard expansion/widening, IPv6 skip, union/
  fallback cache semantics, refresher pass (new-token flush, steady-state
  no-op, never-demote, late CDN widening), updater→refresher handoff, status
  sections, plus the fetch layer: per-provider parsers driven by canned
  payloads, `_clean_cidrs` validation/collapse, `_subtract_networks`
  netmask math, and the cache/fallback degradation chain. Widening-policy
  tests seed a stand-in provider table via `_set_provider_blocks` — no
  network anywhere in the suite. `benchmark/bench_firewall_updater.py`
  unaffected.

### Post-release fix — launch failures ("firewall not enforcing", ~19/20 launches dying)

Two defects from this pass compounded into a mostly-failing launch:

1. **The enforcement self-test's negative probe target went stale.** It
   asserted `example.com` must be unreachable — but example.com moved onto a
   major CDN (post-Edgio-shutdown it resolves into Cloudflare's ranges), the
   same provider dozens of whitelisted hosts legitimately widen to. Once any
   Cloudflare block was open, the probe saw example.com reachable and killed
   the container as "not enforcing". Under provider widening NO real site can
   be a negative probe — any public host may share edge space with a
   whitelisted one. **Fixed:** the probe now targets reserved documentation
   space (`192.0.2.1`, RFC 5737 TEST-NET-1 — never emitted by the pipeline,
   routed nowhere) and discriminates by curl exit code: 7 = instantly refused
   by our REJECT = enforcing; 28 (timeout = packet escaped and black-holed) or
   anything else = not enforcing.
2. **The phase-2 updater raced init-firewall.sh.** "Container is running"
   only means the entrypoint *started* — the updater began `iptables -I`
   injections while the script was still flushing/writing/self-testing.
   Inserts landing before the flush were silently wiped; inserts landing
   mid-self-test opened widened provider blocks that made the (old) probe's
   target reachable — so surviving a launch was literally a race between the
   first Cloudflare burst and a 3-second curl. On failure, the dead container
   then drew "batched iptables insert failed … container is not running"
   warnings from the still-flushing updater. **Fixed:** init-firewall.sh
   touches `/var/run/init-firewall.done` after its self-test passes
   (mirrored as `paths.FIREWALL_DONE_IN_CONTAINER`, sync guarded by test);
   the updater gates on `docker_config.wait_for_firewall_applied` — marker
   present → proceed; container died without it → bail silently (no corpse
   exec spam, no refresher); timed out with a live container → proceed
   best-effort (late rules beat no rules).

Tests: 570 (was 563) — gate polling/death/timeout semantics, updater
gate-respect + bail path, and a drift guard asserting the shell script and
the Python constant agree on the marker path.

Companion utility: `tips/evaluate_addresses.sh` (source it and call
`evaluate_addresses "${domains[@]}"`, or execute it with domains as args)
classifies a list of whitelist entries — resolves each base host against the
same live provider ranges and prints ready-to-paste `*.<apex>  # via
<provider>` lines for the ones a wildcard would actually help; everything
else goes to stderr with the reason. The whitelist template points to it.

Deferred (flagged, not done): re-reading the user whitelist file on each
refresher pass would let entries added mid-session apply without a relaunch —
needs cache-busting in `user_firewall_whitelist_lines` and skip/pending
bookkeeping for late entries; worth a look if mid-session whitelist edits
become a habit.
