# Tags Rewrite — the replan

**Status: agreed design, pre-implementation. Supersedes `refactoring_plan.md`**
(whose Changes A–G and open questions Q1–Q6 are all resolved or absorbed here)
**and the taxonomy notes in `TODO.txt`.** This is a rewrite plan, not a
refactor: the old modifier system (`InstanceModifiers`, the filename grammar,
the modes map) is retired wholesale, not evolved — and so is docker-compose
(§6, decided).

Decisions settled in design sessions 2026-07-15/16. Open points live in §14;
deferred-by-decision items in §15. Everything else is considered decided.

---

## 1. The four kinds — "tags"

Every axis member is a **tag** (umbrella term, decided). There are four
*kinds* of tag. The kinds are the closed set — one Python class each; the
members of each kind are an **open set discovered from the `agents/` file
tree** at startup. Dropping a valid tag folder in is all it takes to add a
member: no code change.

| Kind | Parentheses | Nutshell | Members (initial) |
|---|---|---|---|
| **engine** | `( )` | how hard the agent THINKS | default, golem, thinker, breakthrough, researcher, poet |
| **profession** | `[ ]` | capability: what tools it can USE | code, web |
| **specialty** | `{ }` | exceptional ACCESS/RUNNING capabilities | auto, dood, firewall |
| **policy** | `< >` | what's PERMITTED | web-research, no-sudo, … |

### The classifier law

> **Can it be expressed as a Claude Code `settings.json` fragment? → policy.**
> **Does it need anything else — mounts, iptables, image layers, CLI args —
> → specialty.**

`firewall` is a specialty (iptables + container networking + whitelist
machinery); `no-sudo` is a policy (one `permissions.deny` line). A
restriction may exist in both forms under different names (soft `read-only`
policy denying Write/Edit vs. a future hard `read-only` specialty mounting
`:ro`).

### The what/how separation law

Per-tag data splits across two fixed-name files:

> **`tag.info` = WHAT the tag is** — identity, description, display,
> relations to other tags (`wants` is the borderline case, and it's still a
> "what": a relation, not a mechanism).
> **`tag.docker` = HOW it contributes to the container** — build args, run
> flags, mounts, entrypoint (§3). Optional; many tags have no container
> contribution.

### The class tree (kinds = subclasses; members = instances)

Kinds carry kind-specific fields and behavior, so they are a small class
hierarchy: each subclass owns its **scanner** (member discovery), its
**help-printer**, and its specialized fields/rules.

```python
class Tag:
    """Base for the four kinds. Class-level attrs describe the kind;
    instances are the discovered members."""
    # per-kind (ClassVar):
    parentheses: ClassVar[tuple[str, str]]   # opener/closer — labels, form sections, legend
    root: ClassVar[str]                      # subtree under agents/ ("engine", "profession", …)
    nutshell: ClassVar[str]                  # kind one-liner — section headers, F8 legend
    # per-member (from folder + tag.info / tag.docker):
    name: str                    # folder name — canonical; what instances.json stores
    shortname: str               # display inside the parentheses (defaults to name)
    description: str             # tag.info `description` field — queryable for the picker;
                                 #   first line doubles as the member nutshell, full text
                                 #   feeds the form's body panel
    requires: frozenset[str]     # DERIVED at scan time from tree position — never authored
    wants: Mapping[str, str]     # {wanted-tag: message} — 1-directional request (§4, §15)
    docker: DockerContribution | None   # parsed tag.docker, when present (§3)
    path: Path

    @classmethod
    def scan(cls) -> list[Self]: ...     # each kind implements its own discovery
    @classmethod
    def help(cls) -> str: ...            # kind help-printer (form copy, legend, docs)

    @property
    def label(self) -> str:
        o, c = self.parentheses
        return f"{o}{self.shortname}{c}"

class Engine(Tag):        # ("(", ")"), root="engine"
    ...                   # nested engine folders ⇒ conf inheritance (§3)

class Profession(Tag):    # ("[", "]"), root="profession"
    ...                   # scanner skips `_`-prefixed dirs; nesting ⇒ requires

class Specialty(Tag):     # ("{", "}"), root="specialty"
    warn: bool                       # red label + banner emphasis
    claude_args: tuple[str, ...]     # appended to the claude command
    # image layer, if any: found by convention at profession/**/_<name>/ (§2);
    # container extras (cap_add, entrypoint, mounts) come from tag.docker

class Policy(Tag):        # ("<", ">"), root="policy"
    risk_level: int                  # graded, not boolean (rubric + coloring: §14.1)
```

Python floor: **3.12+** (approved; higher if a feature earns it). The
version-requirement documentation is a deliberate end-of-project sweep.

---

## 2. The `agents/` file tree

**Uniformity law: every tag is a folder; the folder name is the tag name;
every tag folder contains a `tag.info`.** Files *inside* tag folders have
fixed names (`tag.info`, `tag.docker`, `engine.conf`, `policy.json`,
`Dockerfile`, `addendum.md`) — renaming the folder is the complete rename.

```
agents/
  researcher.md                    # persona (system prompt) — unchanged role
  researcher.lego                  # agent build file (TOML syntax) — §3
  engine/
    default/      tag.info  engine.conf
    golem/        tag.info  engine.conf
    thinker/
      tag.info  engine.conf
      breakthrough/                # nesting available ⇒ breakthrough = thinker's conf + overrides
        tag.info  engine.conf     #   (whether to actually nest is a migration-time choice)
    researcher/   tag.info  engine.conf
    poet/         tag.info  engine.conf
  profession/
    code/
      tag.info  tag.docker  Dockerfile  addendum.md
      web/                         # nested ⇒ "web requires code" — the tree IS the declaration
        tag.info  tag.docker  Dockerfile  addendum.md
      _dood/                       # UNDERSCORE: dood's image layer — part-profession, but
        tag.docker  Dockerfile    #   hidden from the profession scanner (not offered);
                                   #   no tag.info here — it is not itself a tag
  specialty/
    combos.info                    # NON-NESTED: multi-tag entanglement warnings (§3) — kind-root file
    auto/
      tag.info                     # warn, claude_args, [wants] table with message — no tag.docker
      addendum.md
    dood/
      tag.info                     # warn; its layer found at profession/**/_dood ⇒ requires code
    firewall/
      tag.info  tag.docker
      init-firewall.sh  firewall-entrypoint.sh  addendum.md
  policy/
    web-research/
      tag.info                     # shortname "+query", risk_level
      policy.json                  # the pure settings fragment
    no-sudo/
      tag.info                     # shortname "-su"
      policy.json
```

**Extension-typing (root shelf):** `.md` = persona · `.lego` = agent build ·
kind subdirectories. Inside the tree: `.info` = tag manifest, `.docker` =
container contribution (both TOML syntax), `.conf` only under `engine/`,
`.json` only under `policy/`.

### Discovery rules (per-kind scanners)

1. **A dir containing `tag.info` is an offered tag** of the kind whose
   subtree it lives in. **A `_`-prefixed dir is a hidden asset dir** — not
   offered, no `tag.info`. **STRICT rule (decided): inside a kind subtree
   every subdirectory must be one or the other** — a bare dir (no `tag.info`,
   no leading `_`) is a scan error, not a silent "grouping shelf". Rationale:
   a forgotten/misnamed `tag.info` would otherwise vanish the tag AND sever
   any requirement edge routed through it (a nested `[web]` silently losing
   `requires: code`). Valid mid-level tags are unaffected — `code` carries a
   `tag.info`, so it's a full tag whose nesting still supplies `requires`.
   `_`-dir meaning is location-dependent: in the profession tree it's a
   specialty-claimed image layer; anywhere else it's just an ignored asset
   dir (e.g. a `_scripts/` inside a tag). Relaxable later (a real grouping
   need would get an explicit marker); tightening later would not be
   backward-compatible, so strict is the safe default.
2. `Profession.scan`: walk `profession/**`; skip `_`-dirs; nesting position
   ⇒ `requires` (all ancestor tag dirs).
3. `Specialty.scan`: `specialty/*/tag.info`; then locate an optional image
   layer at `profession/**/_<name>/` — the layer's tree position contributes
   the specialty's `requires` (everything above `_dood` is required ⇒ code).
4. `Engine.scan`: `engine/**/tag.info`; nesting ⇒ conf inheritance chain
   (§3), and implicitly `requires` in the "extends" sense.
5. `Policy.scan`: `policy/*/tag.info` + `policy.json`.
6. Validate at startup, fail loud: unique names across ALL kinds (one
   namespace: form keys, store values, image tags); every **profession-tree**
   `_`-dir (a claimed image layer) matches exactly one specialty and contains
   no `tag.info` (`_`-dirs elsewhere are plain asset dirs — no such check);
   every `.lego` / `wants` / `combos.info` reference resolves; policy
   fragments parse as settings JSON; every file a `tag.docker` references
   exists.

### The dependency law

**All `requires` relations are filesystem-derived — none are authored in
manifest keys.** (Decided: no split-brain between tree-driven and
manifest-driven dependencies.)

Expressible today: profession→profession (nesting), specialty→profession
(the `_<name>` layer's position), engine→engine (nesting = conf extension).
**Not expressible — and not currently needed**: specialty→policy,
specialty→specialty, policy→anything, multi-parent requirements. No real tag
today has such an edge (auto–firewall is a `wants`, not a require). If one
ever appears, a new marker mechanism gets designed then — nothing is
pre-built.

### Docker-asset placement law

- **Dockerfile-bearing contribution → lives in the profession tree**, at its
  requirement position (`_`-prefixed when owned by a specialty).
- **Non-image contribution (mounts, caps, entrypoint, env/arg forwards) →
  declared in the owner's `tag.docker`**, with its script assets beside it.
- `iptables` moves into the **base image** (tiny, dormant unless firewall is
  active) — that's what lets firewall own no image layer.
- Chain mechanics keep today's shape: parameterized parent image, image tags
  from lowercased member names joined by dots. Chain order: tree depth
  (parents first), alphabetical among siblings, professions before
  specialties.
- `docker/` shrinks to the **base Dockerfile** plus genuinely shared,
  non-tag-owned pieces. No compose files anywhere (§6).

---

## 3. File formats

### `.lego` — the agent build file (TOML syntax)

```toml
# researcher.lego — snap the agent together.  (syntax: TOML)
# Every entry is a *starting point*: pre-picked in the create-form, un-pickable there.
# Missing file, or any missing key = empty default.
engine      = "researcher"        # -> agents/engine/researcher/
professions = ["code"]
specialties = ["firewall"]
policies    = ["web-research"]    # full names here — shortnames are display-only
```

Missing `.lego` → all axes empty; engine falls back to
`engine/<agent-name>/` if present, else `engine/default/`.

### `tag.info` — the universal per-tag manifest (TOML syntax)

All keys are plain TOML fields — **including `description`** (a field, not a
comment block, so the picker can query it directly). The first line of
`description` doubles as the member's nutshell (picker rows, legend); the
full text feeds the form's body panel.

- Common keys: `description`, `shortname` (display inside the parentheses;
  defaults to the folder name), `[wants]` (table of `{tag = message}` —
  §4/§15).
- Kind-specific keys: Specialty → `warn` (bool), `claude_args` (list);
  Policy → `risk_level` (int).
- **No `requires` key exists** — requirements come from the tree only.
- **No container mechanics here** — that's `tag.docker`'s job (what/how law).

```toml
# specialty/auto/tag.info
description = """
Work nonstop without asking for permission.
Passes --dangerously-skip-permissions to claude. Pair with {firewall}
unless you truly want an unattended agent with an open network."""
shortname   = "auto"
warn        = true
claude_args = ["--dangerously-skip-permissions"]

[wants]                            # 1-directional: auto proclaims its almost-dependency;
firewall = "Without {firewall}, this unattended agent has an OPEN network."
```

```toml
# policy/web-research/tag.info
description = "Never ask permission for web searches and fetches."
shortname   = "+query"
risk_level  = 1
```

Emerging shortname convention (soft, not enforced): `+` = grants/relaxes,
`-` = restricts (`<+query>`, `<-su>`).

### `tag.docker` — the tag's container contribution (TOML syntax; optional)

The "how" file (what/how law, §1): **static, declarative** docker
contributions only. Dynamic values (resolved whitelist addresses, detected
`DOCKER_GID`, per-instance mounts) stay in the Python kind-handlers, which
read these declarations and stage the values at launch.

```toml
# specialty/firewall/tag.docker
[run]
cap_add     = ["NET_ADMIN"]
entrypoint  = "firewall-entrypoint.sh"       # relative to this tag dir
mounts      = [
  "init-firewall.sh       -> /usr/local/bin/init-firewall.sh:ro",
  "firewall-entrypoint.sh -> /usr/local/bin/firewall-entrypoint.sh:ro",
]
env_forward = ["WHITELIST_ADDRESSES"]        # staged-by-launcher values forwarded as -e
```

```toml
# profession/code/tag.docker
[build]
arg_forward = ["PARENT_IMAGE", "SOFTWARE_STACK_REFRESH", "FORCE_INSTALLS_REFRESH", "INSTALL_*"]
```

```toml
# profession/code/_dood/tag.docker  (hidden layer dirs may carry one)
[build]
arg_forward = ["PARENT_IMAGE", "DOCKER_GID"]
[run]
mounts = ["/var/run/docker.sock -> /var/run/docker.sock"]   # absolute source = host path
```

- Sections: `[build]` (`arg_forward` — staged build-arg names for this
  layer's Dockerfile) and `[run]` (`cap_add`, `entrypoint`, `mounts` as
  `"src -> target[:ro]"` with relative `src` resolved against the tag dir,
  `env_forward`).
- Optional per tag: `auto` has none (pure `claude_args`); most policies and
  engines never will.
- Why TOML and not a per-tag shell script: the rewrite's direction is
  centralizing orchestration in auditable, testable Python — per-tag scripts
  would re-scatter it. Shell still lives where shell belongs: as tag
  *assets* (`init-firewall.sh`) invoked via declared entrypoints.

### `engine/<name>/engine.conf` — pure data now

The existing env-var `.conf` format, **minus the explanatory header
comments** — those migrate into the engine's `tag.info` `description`
(single home for tag prose). **Nested engine folders inherit**: the
effective conf = parent-chain confs merged top-down, child keys override.
(An engine "exactly like another, with some additions" = a nested folder
holding only the additions.)

### `profession/<path>/` — docker dir + manifests

`Dockerfile` + `tag.docker` + `tag.info`. Optional `addendum.md` for the
launch-time CLAUDE.md section.

### `policy/<name>/policy.json` — a settings fragment, nothing else

Selected policies' fragments **deep-merge** with the shared settings template
into a per-instance `settings.json`, written to the state dir and
**bind-mounted read-only** over `~/.claude/settings.json` — the agent cannot
relax its own leash. Merge rules: dicts recurse; lists concatenate + dedupe;
**scalar conflicts abort the launch** naming both policies (silent last-wins
would make combinations order-dependent).

Tier honesty (for docs and prompts): harness-level denial stops the *tool
call*; the binary/grant still exists in the container. Hard variants
(sudoers removal, `:ro` mounts) are specialties. Side effect to remember:
an RO-mounted settings.json also means in-container `/model`-style "save as
default" writes can't persist — accepted; policy integrity wins.

### `specialty/combos.info` — multi-tag entanglement warnings (kind-root file)

**Scope: 2+ tag entanglements only** — relations no single tag owns, where
per-tag placement would force text duplication (dood doesn't get along with
auto *and* auto doesn't get along with dood). One-sided concerns belong to
the concerned tag's own `[wants]` table instead (auto→firewall lives in
auto's tag.info — firewall doesn't care).

```toml
# specialty/combos.info — co-selection warnings, rendered live (red) in the
# form's warning zone and echoed in the launch banner.
[warnings]
"dood + auto" = """
YOU'VE ENABLED BOTH {dood} AND {auto} - PROCEED WITH CAUTION:
THE AI AGENT HAS THE POWER TO DO ANYTHING ON YOUR COMPUTER,
AND DOESN'T REQUIRE PERMISSION!"""
```

---

## 4. Relations and their form behavior

| Relation | Source | Form behavior |
|---|---|---|
| **requires** | file tree (derived at scan) | Row stays **enabled**; requirement named in a parenthetical next to the label — `[ ] [web]  playwright… (requires: code)`. **Checking a tag auto-checks its transitive requirements**; unchecking a requirement that has checked dependents unchecks those dependents too. No disabling, no indentation (deferred: indent trees get complicated once cross-kind requirements exist). |
| **wants** | `[wants]` table in the wanter's `tag.info` | Two halves. **Ships with the form:** while the wanter is checked and a wanted tag isn't, the want's message renders in the red warning zone — this is the auto/firewall split's guard. Costs zero extra I/O: the form already reads every `tag.info` for `description`/`shortname`, so the wants mapping is built during that same read. **Deferred (§15):** auto-ticking the wanted tag. |
| **entanglement warnings** | `specialty/combos.info` | Live red warnings above `[ Confirm ]` while all named tags are co-selected. |

**The form** (create + modify — one screen, built on the existing
`checkbox_form`):

```
Configure researcher__proj:

 ENGINE — how hard the agent THINKS                     (radio — exactly one)
   (•) (researcher)   ( ) (thinker)   ( ) (golem) …
 PROFESSION — what tools it can USE
   [x] [code]   coding toolchains…
   [ ] [web]    playwright browser…        (requires: code)
 SPECIALTY — exceptional access/running
   [ ] {auto}   work nonstop…
   [x] {firewall}  outbound whitelist…
   [ ] {dood}   host docker…               (requires: code)
 POLICY — what's permitted
   [ ] <+query>  never ask before web searches

 ⚠ (live warnings zone — unmet wants messages + combos.info)
 [ Confirm ]
```

`checkbox_form` gains: non-focusable section-header rows (kind nutshells),
a radio-row flavor (engine), requirement parentheticals + check-cascades,
wants-message warnings. Pre-picks come from `.lego` (create) or the instance
store (modify); everything un-pickable. Cancel semantics carry over (Esc:
create → clean exit; modify → back to picker).

---

## 5. The auto/firewall split

- `{auto}` keeps: skip-permissions claude arg + addendum + `warn` + its
  `[wants] firewall = "…"` message. No container contribution.
- `{firewall}` takes: the container networking contribution (its
  `tag.docker`: cap-add, entrypoint, script mounts, `WHITELIST_ADDRESSES`
  forward), `init-firewall.sh`, the entrypoint wrapper (renamed
  `firewall-entrypoint.sh` during the move), and the whole
  whitelist-resolution + burst-updater machinery in `network.py` (hooks move
  behind the Specialty handler).
- Either, neither, or both is legal. The default pairing rides on `.lego`
  defaults; unpairing shows auto's wants message in the warning zone.
- User-state migration: stored modes `["auto"]` → specialties
  `["auto", "firewall"]` (preserves today's behavior exactly); `["web"]` →
  professions `+["web"]`; `["DooD"]` → specialties `["dood"]`.

---

## 6. Launcher architecture

### Plain docker — compose retired (decided)

Checked feature-by-feature before deciding: the launcher used compose as a
thin wrapper over flags it already assembles itself.

| Compose gave us | Plain-docker equivalent |
|---|---|
| `compose build` with `-f` overlay chain + `${PARENT_IMAGE}` substitution | `docker build -f <Dockerfile> --build-arg PARENT_IMAGE=… -t claude-agents:<tag> --network=host <context>` per chain step (the overlay chain existed *only* to feed compose) |
| `compose run --rm -it --name … -v … -e …` | `docker run --rm -it --name … -v … -e …` — identical TTY/cleanup semantics |
| per-layer `entrypoint:` / `cap_add:` / env passthrough | `--entrypoint` / `--cap-add` / `-e`, declared in `tag.docker` |
| `network: host` build workaround (YAML) | a plain `--network=host` flag |

What compose is genuinely for — multi-service orchestration, service
networks, `depends_on`, named volumes, long-lived lifecycle — this launcher
uses none of; container "management" here is one ephemeral `run --rm`.
Bonus: the Compose v2 plugin stops being an install prerequisite
(`install_dependencies.sh` currently hard-fails without it).

`docker_config.py` is rewritten as the plain-docker build/run assembler
(reads `tag.docker` declarations + dynamic stagings from kind handlers).

### Per-kind modules

Each kind class lives in its own module, owning its scanner, help-printer,
and launch-stage contribution (decided):

```
launch/
  tags/
    __init__.py        # Tag base + registry: scan-all, validate, name lookup
    engine.py          # Engine: conf-chain merge → env staging + --effort arg
    profession.py      # Profession: tree walk, chain contribution
    specialty.py       # Specialty: tag.info/tag.docker, claude_args, _layer lookup, combos.info
    policy.py          # Policy: fragment load, deep-merge, settings.json build + RO mount
  docker_config.py     # plain-docker build/run assembly (tag.docker + dynamic stagings)
  form / menu_picker   # sectioned checkbox form: radio rows, parentheticals, cascades, warnings
```

Launch pipeline (same seven-stage spine, new stage contents):

```
scan+validate tags → pick agent (.md/.lego) → form (defaults pre-picked)
  → persist instance axes → compose chain (profession/specialty contributions)
  → stage (env from engine, mounts/caps/args from tag.docker + handlers,
           settings.json from policies)
  → ensure_image (docker build per step) → docker run
```

Sorting is rebuilt per kind: picker sorts agents by engine (model
family/version parsed from the engine's conf — `ORDERED_MODEL_FAMILIES`
logic moves into `engine.py`); instances by specialty/profession sets (tree
order replaces enum-declaration order).

---

## 7. `~/.claude-agents/` — one instance store

`agent_modes_map.json` + `agent_workspace_map.json` fold into
**`instances.json`** (one file, one loader, one save path):

```json
{
  "researcher__proj": {
    "workspace":   "/home/user/proj",
    "engine":      "researcher",
    "professions": ["code", "web"],
    "specialties": ["auto", "firewall"],
    "policies":    ["web-research"]
  }
}
```

- **Full names stored** — shortnames are display-only (decided).
- **Full-replacement semantics**: an instance entry wins over `.lego`
  defaults wholesale; no entry → form opens with `.lego` pre-picks. Engine
  is per-instance overridable.
- A store entry referencing a tag that no longer exists in the tree fails
  the launch loudly, naming the instance + tag and pointing at the modify
  flow (same fail-loud contract as `.lego` references).
- Same cached load-mutate-save pattern and atomic `write_text` as today.
- One-time migration: read both old maps, apply §5 translations, write
  `instances.json`; old files renamed `*.pre-rewrite.bak`.

---

## 8. What each existing surface re-sources to

| Surface | Old source | New source |
|---|---|---|
| Picker legend (F8) | enum descriptions | kind `nutshell` (ClassVar) + member `description` fields |
| Row labels / colors | `.label`, `_WARN_` name infix | `parentheses` + `shortname`; `Specialty.warn` / `Policy.risk_level` |
| Form copy | `MODIFIER_YN_PROMPTS` | `tag.info` `description` |
| Warnings | `MODIFIER_NOTICE_PROMPTS` | unmet `[wants]` messages + `specialty/combos.info` |
| CLAUDE.md addendums | `memory_addendums.py` constants | `addendum.md` in tag dirs, composed in chain order |
| Launch banner | tags/modes off identity | axes off the instance record, kind by kind |
| conf → env / `--effort` | `load_conf` + `effort_args` | `tags/engine.py` (with conf inheritance) |
| conf header comments | `.conf` files | engine `tag.info` `description` |
| skip-permissions | baked into auto entrypoint | `claude_args` in `specialty/auto/tag.info` |
| per-layer compose YAML | `docker/compose.<step>.yml` | `tag.docker` `[build]`/`[run]` sections |
| `docker compose build/run` | `docker_compose_subprocess` | plain `docker build` / `docker run` assembly |

---

## 9. Migration (repo files)

Scripted, one-shot, verified by the suite after:

1. `agents/*.conf` → `agents/engine/<name>/engine.conf`; header comments
   lift out into each engine's generated `tag.info` `description`.
2. `docker/Dockerfile.code` → `agents/profession/code/Dockerfile`;
   `compose.code.yml` converts into `profession/code/tag.docker`; same for
   `web` (nested) and dood's files → `profession/code/_dood/`.
3. Firewall assets → `agents/specialty/firewall/` (+ its `tag.docker`);
   write `tag.info` for auto/dood/firewall + `specialty/combos.info`; move
   `iptables` into the base Dockerfile; rename the entrypoint.
4. Agent filenames de-bracketed: `bug-investigator[code](breakthrough).md` →
   `bug-investigator.md` + `.lego` (`engine="breakthrough"`,
   `professions=["code"]`). Every agent gets a `.lego`.
5. Addendum/prompt copy split out of `template_code/` into tag dirs.
6. Root `compose.yml` and all `compose.<step>.yml` retired with the
   compose-free build/run path.
7. User state: `instances.json` migration (§7) on first launch.

**Expected one-time cost:** changing the base Dockerfile (iptables) plus the
file moves invalidates every image layer — the first launch of each agent
after migration does a full rebuild. Schedule accordingly; not a bug.

## 10. Testing strategy

- **Scanners/validation**: fixture trees (valid, `_`-claimed, nested,
  colliding names, dangling references, orphan `_`-dirs) → exact member sets
  or exact errors, per kind.
- **Contract tests** (evolve `test_essential_files`): every real tag dir
  valid; every `.lego` resolves; policy fragments parse; base image installs
  iptables; combos.info + wants keys reference real tags; every `tag.docker`
  reference (entrypoint, mount sources) exists.
- **Engine conf inheritance**: parent-chain merge, child override, cycle
  impossibility (it's a tree).
- **Merge rules**: list-concat/dedupe, dict recursion, scalar-conflict abort.
- **tag.docker → args**: declaration-to-flag assembly (`cap_add`, mounts
  with relative-source resolution, env/arg forwards only when staged).
- **Form assembly** (pure parts): sections, requirement parentheticals,
  check-cascades both directions, radio single-select, unmet-wants warnings,
  combo warnings.
- **Migration**: old-map fixtures → expected `instances.json`.
- TUI Applications stay accepted-untested; keep the pipe-input smoke pattern.

## 11. Phasing (each lands green)

**Precondition:** put the repo under version control before P0 — a
multi-phase rewrite with mass file moves needs a rollback point per phase
(one commit minimum per landed phase).

- **P0** — `launch/tags/`: Tag base + four kind classes, scanners,
  `tag.info`/`tag.docker` parsing, validation, tests. Consumes nothing yet.
- **P1** — repo-tree migration (§9 1–6) + `.lego`; launcher reads the new
  tree through the new modules; old instance maps still honored via an
  interim translation shim (§5 mappings applied read-only).
- **P2** — `instances.json` + sectioned form (radio engine, parentheticals,
  cascades, wants + combos warnings) + per-instance axes; auto/firewall
  split live; policy settings-build + RO mount; plain-docker build/run.
- **P3** — delete the legacy (§13 checklist), with user-state migration.
- **P4** — documentation sweep: README, `.claude_dev_guidelines`,
  `.claude_summary`, Python ≥3.12 requirement, editor-association notes for
  `.lego` / `.info` / `.docker`.

## 12. Small details & conventions

- Tag names unique across ALL kinds (one namespace: form keys, store values,
  image tags). Lowercase canonical (`dood` — the mixed-case `DooD` dies in
  migration; image tags were lowercased anyway).
- Fixed filenames inside tag folders; folder rename = complete rename.
- `tag.info` presence = "offered tag" marker; `_` prefix = hidden asset dir;
  `tag.docker` optional everywhere (including `_`-dirs). STRICT: any other
  bare subdir in a kind subtree is a scan error (§2 rule 1).
- `engine.conf` files are pure env-var data — prose lives in tag.info.
- Empty and missing `.lego` are equivalent; both legal.
- Dangling `.lego` / `wants` / combos / instance-store references fail
  launch naming file+key.
- `shortname` defaults to the folder name; `+`/`-` prefix convention is soft.
- `Specialty.warn` colors the label red everywhere; policy coloring is
  §14.1's open question.
- `agents/engine/default/` keeps its role: fallback when `.lego` names no
  engine and no `engine/<agent>/` exists.
- The `--effort` CLI passthrough (launch-effort pin fix) moves into
  `tags/engine.py` unchanged.
- Quiesce running agent instances during P1/P3 file moves — containers
  bind-mount paths that are being relocated.

## 13. Explicit retirements checklist

`InstanceModifiers` · `parse_stem`/`parse_agent_name` bracket grammar ·
`MODIFIER_YN_PROMPTS` / `MODIFIER_NOTICE_PROMPTS` · `memory_addendums.py`
(as enum-keyed module) · `agent_modes_map.json` · `agent_workspace_map.json`
· `agent_modifiers_handler.py` (split into `tags/*`) · **docker-compose
entirely** (root `compose.yml`, every `compose.<step>.yml`,
`docker_compose_subprocess`, the Compose-plugin install prerequisite) ·
`tag_sort_key`/`mode_sort_key` (rebuilt per kind) · filename-embedded
`[tags]`/`(parent)` · `.conf` header comments (→ tag.info) · spec.toml
(superseded by `tag.info` before ever existing).

## 14. Open questions / TODOs

1. **TODO — policy coloring.** Grading policies is two-axis: allowing more
   operations is a positive (green as capability) but weaker security (red);
   restricting is the reverse. Candidate directions: dual-color rendering,
   a `risk_level`-driven ramp, or coloring by the security lens only.
   **Deliberately parked until after the serious parts of the rewrite.**
   (Subsumes the `risk_level` rubric question.)
2. **Engine nesting at migration** — nest `breakthrough` under `thinker`
   (conf inheritance) or keep all engines flat initially? Mechanism ships
   either way.
3. **Hard `read-only`** (workspace `:ro` specialty): initial roster or later?
4. **`poet`/`golem` `.lego` contents** — engine-only, or also default grants
   (e.g. poet + `web-research`)? Decide at migration.
5. **Currently-inexpressible dependency edges** (specialty→policy,
   specialty→specialty, multi-parent) — no mechanism until a real tag needs
   one; revisit the marker design then.

## 15. Deferred by decision — `wants` auto-ticking (TODO)

`wants` is a **message-bearing, 1-directional request** declared in the
wanter's `tag.info` (`[wants]` table: `{tag = message}`). Firewall doesn't
care about auto; auto proclaims its almost-dependency — so the relation and
its copy live with auto alone, and `combos.info` stays reserved for true
multi-tag entanglements.

Split delivery:
- **Ships with the form (P2):** rendering an unmet want's message in the red
  warning zone (wanter checked, wanted unchecked). This is the auto/firewall
  split's safety guard and is not deferred. Implementation is free-riding:
  the form already reads every `tag.info` for description/shortname — the
  wants mapping is built during that same read.
- **Deferred:** auto-ticking the wanted tag when the wanter is checked (the
  convenience half). Candidate behavior when we return: check-wanter ⇒
  wanted auto-ticks, still un-tickable; perhaps a subtle "auto suggests
  firewall" hint. Not critical; `.lego` defaults + the warning message carry
  the posture until then.
