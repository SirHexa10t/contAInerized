# Refactoring Plan — Agent Modifier Taxonomy Redesign

**Status: design / brainstorm captured. Nothing here is implemented yet.**
This document records the reasoning behind a proposed reshaping of the agent
modifier taxonomy (the `()` / `[]` / `{}` / `<>` "filename-grammar" axes). It
deliberately includes options we may *not* implement, so the thinking survives
even if we only act on part of it. Read it as a menu plus rationale, not a
committed work order.

Source of the original idea: `TODO.txt` lines ~300–323.

---

## 1. The conceptual model (where we are today)

The TODO describes four modifier axes, each notionally carried by a bracket
pair in the agent's identity:

| Axis (TODO name) | Bracket | Means | Examples |
|---|---|---|---|
| investment | `()` | pre-defined scope of abilities / token budget | researcher, thinker, breakthrough, golem |
| tag | `[]` | pre-defined access to tools | code, draw, model, doctor, ue5 |
| mode | `{}` | a change in *how* things are done | DooD, web, auto |
| policy | `<>` | what's allowed / disallowed | web-research, read-only, no-sudo, write-outside-project |

The TODO's own one-liner gloss: *investment = how hard it THINKS, tag = what it
CAN do, mode = how it RUNS (flagged "most dubious"), policy = what's PERMITTED.*

### 1a. Reality check — only two of these are actually filename grammar

This is the single most important correction to the mental model, and the plan
hinges on it. The four "brackets" are **not** four parallel filename features
today:

- `parse_stem` (`launch/utils.py:62`) parses exactly **two** bracket types:
  `[tag]` (accumulates into a list) and `(parent)` (single-valued, names the
  `.conf` to inherit). That's the whole grammar. See its docstring/examples.
- `{}` "modes" are **not in the filename at all.** They're stored per-instance
  in `~/.claude-agents/agent_modes_map.json` (`AGENT_MODES_MAP_FILE`,
  `launch/paths.py:52`) and chosen interactively at create/modify time
  (`prompt_for_modes` in `agent_modifiers_handler.py:198`). The `{auto}` /
  `{DooD}` curly-brace rendering is a *display* convention produced by
  `InstanceModifiers.label` (`structs.py:124`), used in the banner, picker, and
  addendums — never parsed back out of a filename.
- `<>` "policy" **does not exist in code at all.** It's purely aspirational in
  the TODO.

So the honest current state is: **two filename axes (`[tag]`, `(parent)`-conf),
one runtime-persisted axis (modes), and one unbuilt axis (policy).** Any plan
that talks about "the four-bracket grammar" as if it were uniform is describing
a target, not the present.

### 1b. Binding-time analysis (the analytical backbone)

Sorting the axes by *when each value is fixed* is what exposes the incoherence
and drives every recommendation below:

| Axis | Bracket | Bound at | Mutable after creation? | Backing store |
|---|---|---|---|---|
| investment | `()` | agent-pick time (which `.conf`) | no — pick a different agent | `(parent).conf` → `<agent>.conf` → `default.conf` (`agent_conf_path`, `paths.py:348`) |
| tag | `[]` | **image-build time** (baked into the Docker image) | no — filename-locked | filename stem + `docker/Dockerfile.<tag>` + `compose.<tag>.yml` (`compose_layer_path`, `paths.py:351`) |
| mode | `{}` | runtime (per-instance) | **yes** — modify the instance | `agent_modes_map.json` |
| policy | `<>` | (planned) filename default, runtime override | yes | (planned) `settings.local.json` |

Three of the four are clean single-axis primitives. `{}` is the outlier — and
not because of its binding time (it shares "runtime" with the planned policy
axis) but because **its current members don't actually share one axis.**

---

## 2. Core finding — why `{}` "mode" is incoherent

The TODO already flags mode as "the most dubious definition" and asks two
leading questions: *"shouldn't 'web' be a tag too? And should 'auto' be a
policy?"* The answer to both is yes, and it generalises. Decompose the three
current mode members against the axes:

- **`{web}`** → really a **capability**. It needs Playwright + Chromium *baked
  into the image* (a `Dockerfile.web` build step) and declares `TAG_CODE` as a
  prerequisite (`structs.py:170` `_prerequisites`). Image-affecting ⇒ that's
  tag/`[]`-shaped (build-time), not a runtime behavior switch. Its placement in
  the runtime `{}` bracket is exactly the smell.
- **`{DooD}`** → mostly a **capability + a mount**. It bind-mounts the host
  docker socket (`DOCKER_DOOD_MOUNTS`, `paths.py:167`) and stages a
  `DOCKER_GID` build-arg (`_apply_dood`, `agent_modifiers_handler.py:104`).
  Grants access to a tool; tag-shaped.
- **`{auto}`** → spans **three axes at once**:
  - capability/tooling: iptables firewall scripts mounted in
    (`DOCKER_AUTO_MOUNTS`, `paths.py:152`; `_apply_auto`,
    `agent_modifiers_handler.py:134`),
  - policy: the network is restricted to a whitelist,
  - behavior: `--dangerously-skip-permissions` (don't ask before acting).

Strip the mis-filed members out of `{}` and **it is empty.** There are **zero
pure-behavior switches in the system today** — `{plan-only}`,
`{checkpoint-edits}`, `{verbose}` etc. were hypotheticals invented during
brainstorming, not things anyone runs. The lone genuine behavior bit that
exists, "skip permission prompts," only ever appears *inside* `{auto}`, never
standalone.

**Conclusion:** `{}` is a leftover bin, not a coherent peer axis. The only real
thing it contains is `{auto}`, which is itself a *bundle* spanning the other
three axes.

---

## 3. Proposed changes

Each change carries a **Status** (recommended / optional / open question /
deferred), the **reasoning**, and the **code impact**. They are somewhat
independent — we can take some and leave others — but read §4 for ordering
constraints.

### Change A — Reclassify `{web}` and `{DooD}` out of "mode"

**Status: recommended (prerequisite for everything else).**

**What:** Move `web` and `DooD` from the mode bucket to the capability bucket
(today's `[]` tag, see Change D for its possible rename). `{web}` → `[web]`,
`{DooD}` → `[DooD]` conceptually.

**Why:** Per §2 they grant tools / mounts / image content. They fail the
"is this purely a *how it runs* switch?" test. `web` in particular is
image-affecting, which is the cleanest possible disqualifier from a runtime-only
bracket.

**Tension to resolve first:** tags are *build-time, filename-locked* today;
modes are *runtime, per-instance-selectable*. If `web`/`DooD` simply become
plain tags, you **lose the ability to toggle them per instance** — they'd have
to be in the `.md` filename. Two ways out:
  1. Adopt Change E (make capabilities runtime-modifiable with a filename
     default). This is the coherent resolution and the user has already leaned
     toward it ("kit should be modifiable too, set with a default like policy").
  2. Accept that `web`/`DooD` are build-time and must be chosen via filename.
     Simpler, but a UX regression for `DooD` (which is just a mount + GID and
     has no real reason to be build-time).

**Code impact:**
- `structs.py`: rename `MODE_WARN_DOOD` → `TAG_*`/`KIT_*`, `MODE_WEB` →
  `TAG_*`/`KIT_*`. Their `.label` flips from `{x}` to `[x]` automatically via
  the name-prefix logic (`structs.py:132`). `colored_label` still reds `DooD`
  via the `_WARN_` infix (keep `WARN` in the new name for DooD).
- `_prerequisites` (`structs.py:170`) unchanged in meaning (both already require
  `TAG_CODE`); `applies_to` gating still works.
- `agent_modifiers_handler.py`: `_apply_web` / `_apply_dood` stay, but the
  `compose_chain` dispatch conditionals (lines 243–251) need to reflect the new
  member names. The prompt dispatch (`prompt_for_modes`) and the danger-warning
  (`warn_if_dangerous_modes`) currently iterate mode-keyed dicts
  (`MODIFIER_YN_PROMPTS`, `MODIFIER_NOTICE_PROMPTS` in
  `template_code/modifier_prompts.py`) — those keys move with the members.
- `memory_addendums.py`: `MODIFIER_ADDENDUMS` keys (`MODE_WEB` →
  `WEB_NOTICE`) update to the new names; `WEB_NOTICE`/`FIREWALL_NOTICE` bodies
  reference `InstanceModifiers.MODE_*.label` (lines 55, 73, 85) and must follow.
- `compose_layer_path` (`paths.py:351`) already builds `compose.<value>.yml`
  from the lowercased value, so the per-layer YAML naming is value-driven and
  needs no change as long as the `.value` strings ("web", "DooD") are kept.
- Picker sort keys: `mode_sort_key` / `tag_sort_key` (`agents_crud.py:193,201`)
  partition by the subset views; they follow the member reclassification for
  free.

### Change B — Repurpose `{}` as the *preset* (composite) bracket

**Status: recommended (the heart of the redesign). Alternative in §3-B-alt.**

**What:** Stop treating `{}` as a (near-empty) peer single-axis. Redefine it as
a **named bundle that expands into values on the other three axes.** `{auto}`
becomes the canonical — and, for now, only — preset:

> `{auto}` desugars to: capability = firewall/iptables tooling,
> policy = network-restricted, behavior = skip-permission-prompts.

This gives the bracket system a real structure: **three primitives
(`()` investment, `[]` capability, `<>` policy) + one composite (`{}` preset).**
The brackets stop pretending to be four peers and instead encode a small
hierarchy. `{auto}` stops being an embarrassing cross-axis special case and
becomes the *defining* member of the composite bracket — it's *supposed* to span
axes; that's what a preset is.

**Why:**
- It's the honest description of what `{auto}` already is.
- It resolves the "mode is incoherent" problem without throwing away the one
  useful member.
- It composes cleanly with a future umbrella rename (Change G): even if every
  bracket gets called a "tag," the composite is still distinguishable because
  it's the only one that *expands* rather than setting a single value.
- The revised one-liner gloss becomes clean: *investment = how hard it THINKS,
  capability = what it CAN do, policy = what it's ALLOWED to do, preset = a named
  combination of the three.* No "how it RUNS" hand-waving, no leftover bin.

**Cost / the tradeoff to sit with:** `{}` is no longer a peer single-axis; it's
explicitly meta. If that asymmetry bothers us more than the current incoherence
does, prefer the alternative below.

**Code impact (larger — this is a new concept, not a rename):**
- New data structure: a `PRESETS: dict[str, ...]` mapping a preset name to the
  axis values it expands into — e.g. `{"auto": Expansion(capabilities=[...],
  policies=[network_restricted], behaviors=[skip_prompts])}`. Natural home:
  `structs.py` (alongside `InstanceModifiers`) or a small new
  `launch/presets.py` if it grows.
- An expansion step early in the pipeline (likely in `resolve_target` /
  `compose_chain` in `run.py` / `agent_modifiers_handler.py`) that replaces a
  preset token with its component axis values **before** chain composition and
  addendum rendering run, so downstream code only ever sees primitives.
- `behavior` becomes its own (tiny) internal axis with exactly one member today
  (skip-prompts) — it may not deserve a user-facing bracket at all (see Open
  Question Q3).
- The `{auto}` firewall plumbing (`_apply_auto`, `start_whitelist_resolution`,
  `DOCKER_AUTO_MOUNTS`) stays; it just gets triggered by "the auto preset
  expanded to include the firewall capability + network policy" rather than by a
  bare mode flag.

#### §3-B-alt — Alternative: eliminate `{}` entirely

**Status: fallback if we reject the "meta bracket" asymmetry.**

Drop the `{}` bracket. Express `auto` as an explicit combination at the point of
selection: capability `[firewall]` (or whatever we name it) + policy
`<network-restricted>` + a behavior flag. No preset indirection; the user
composes the pieces themselves.

- **Pro:** every bracket is then a true single-axis primitive; maximally honest.
- **Con:** `{auto}` is the single most-used convenience in the system. Forcing
  users to spell out three pieces every time is a real ergonomic loss. Presets
  exist precisely to name common bundles.

**Recommendation:** prefer Change B (preset). Keep this alternative on file as
the "if the asymmetry proves more annoying than the indirection" escape hatch.

#### §3-B-dead — Rejected: keep `{}` with invented pure-behavior members

Populating `{}` with `{plan-only}` / `{checkpoint-edits}` / `{verbose}` to
justify its continued existence as a behavior axis. **Rejected** because none of
these are real today — it's manufacturing members to save a bracket. If a
genuine standalone behavior switch appears later, revisit; don't pre-build the
bin for it.

### Change C — Rename `()` "investment"

**Status: open question (naming only; low code impact). Current lean: keep
"investment" or adopt "appetite".**

**What:** The `()` axis names the token/compute budget — "how power/token-hungry
the setup is" (researcher = tool-heavy, breakthrough = max-thinking, golem =
cheap/near-zero thinking). The TODO floats rank/tier/power/ability/
resource-budget/investment. The constraint the user set: the name must describe
*consumption/appetite*, and must **not imply a clean linear ordering** (the set
isn't strictly ranked — golem/thinker/breakthrough/researcher differ in *shape*,
not just magnitude).

**Candidates weighed against "evokes token-hunger + avoids false ordering":**

| Name | Captures token-hunger? | False-ordering risk? |
|---|---|---|
| investment (current) | ✓ budget metaphor | low — you can *invest differently*, not just more/less |
| appetite | ✓ strongest — "how hungry" | none — appetites differ in kind |
| budget | ✓ literal "tokens it aims to consume" | mild |
| burn | ✓ (token burn rate) | none |
| footprint / weight | ✓ resource footprint / heavy-vs-light | none |
| tier / rank / power | ✓ | **high — implies a linear scale** (disqualified) |

**Recommendation:** "investment" already says what's meant and reads cleanly in
the TODO; nothing here clearly beats it. **appetite** is the only candidate that
arguably matches "token-hungry" more viscerally and is ordering-neutral. Decide
between those two; avoid tier/rank/power.

**Code impact:** essentially none today — the `()` axis is implemented as the
`(parent)` conf reference in `parse_stem`, which is value-agnostic. "Investment"
is a *concept name* used in docs/TODO, not a code identifier. Renaming is a
documentation change unless/until we surface the axis name in UI copy.

### Change D — Rename `[]` "tag" → "kit"

**Status: optional (naming). Gated by Change G's umbrella decision.**

**What:** The TODO is unsatisfied with "tag" (too generic) and floats
"toolset" / "kit" / "purpose." The axis is fundamentally "which tools/programs
the agent can reach," so a tooling word fits.

**Why "kit":** short, concrete, reads well in a label (`[code]` is "the code
kit"). "toolset" is also accurate but longer; "purpose" overreaches (a kit
enables a purpose but isn't one). The decisive reason to rename at all is
**Change G** — freeing the word "tag" to become the umbrella term for *all*
axes.

**Code impact:** rename `TAG_*` enum prefix → `KIT_*` across `structs.py`
(member names, the `.label` prefix check at line 132, the `tags()`/`tag_values()`
subset views), `agent_modifiers_handler.py` (comments + dispatch), and
`agents_crud.py` (`tag_sort_key` → `kit_sort_key`). The on-disk `.value` strings
and the `[...]` bracket rendering **don't change** — only the internal
identifiers and human-facing axis name. Mechanical but wide; do it as one
atomic rename commit if pursued.

### Change E — Make capability (`[]`/kit) runtime-modifiable with a filename default

**Status: open question (real behavioral change). Enables Change A's clean form.**

**What:** Today capabilities are filename-locked (parsed from the `.md` stem,
fixed for the agent). The TODO proposes treating them like the planned policy
axis: the **filename provides a default**, but the user can **override per
instance** at create/modify time, with the choice persisted per instance.

**Why:** It's what makes Change A coherent — `web`/`DooD` can leave "mode" and
become capabilities *without* losing per-instance toggling. It also unifies two
mechanisms that currently differ only by accident of history (filename tags vs.
modes-map modes).

**Hard constraint — build-time vs runtime:** some capabilities genuinely affect
the *image* (`[code]` installs toolchains; `[web]` bakes in Chromium). You
**cannot** runtime-toggle an image-level capability without selecting a
different pre-built image. So "modifiable" really means: *the per-instance choice
selects which image (chain) to build/run*, not "flip a flag in a running
container." This is already how the chain works (`inst_id.chain` →
`chain_image_tag` / `chain_compose_files`), so the plumbing exists; what changes
is **where the selection comes from** (a per-instance store, not just the
filename).

**Code impact (significant):**
- Generalise the per-instance store: today `agent_modes_map.json` holds modes.
  Either add a parallel `agent_kits_map.json` or broaden the existing store to
  hold all per-instance overrides (kits + modes/presets + policies). The
  load/save + `_write_modes_entry` pattern in `agents_crud.py:69` is the
  template.
- `AgentIdentity.tags` (`structs.py:258`) currently derives tags purely from the
  filename stem. It would need to merge filename-default with per-instance
  override (which means tags become an *instance* concern, not a pure *agent*
  concern — a small but real identity-layer shift; today the agent/instance
  split is clean about this).
- `resolve_pick` (`agents_crud.py:213`) and `resolve_target` (`run.py:92`) would
  prompt for / load kit overrides the way they already do for modes.
- Picker UI: a kit-selection prompt mirroring `prompt_for_modes`.

**Caution:** this is the most invasive change in the document and the one most
likely to be deferred. It blurs the deliberately-clean `AgentIdentity` (agent
truth) vs `InstanceIdentity` (per-launch truth) boundary described in
`structs.py`'s module docstring. Worth doing only if per-instance capability
selection is actually wanted; otherwise Change A's simpler form (capabilities
stay filename-chosen) is fine.

### Change F — Build the `<>` policy axis (currently nonexistent)

**Status: deferred (net-new feature, not a refactor). Documented for completeness.**

**What:** The fourth axis from the TODO: per-instance allow/deny of tools and
actions, mapped onto Claude Code's `settings.local.json`
(`permissions.allow` / `permissions.deny`). Filename default + runtime override,
same hybrid-scope model as Change E. Examples from the TODO: `read-only`,
`no-sudo`, `web-research` (don't prompt for web searches),
`write-outside-project`.

**Why it's separate:** there is no policy code today. This is a feature build,
not a reshaping of existing structure. It interacts with the preset work
(Change B) because `{auto}`'s "network-restricted" component is conceptually a
policy. But policy can be designed and landed independently.

**Code impact (new surface):**
- A policy taxonomy (parallel to `InstanceModifiers`, or folded in as a third
  kind alongside kit/preset).
- A writer that renders the chosen policies into the instance's
  `settings.local.json` inside the state dir (alongside `install_latest_md`'s
  CLAUDE.md write in `agents_crud.py:100`).
- Per-instance persistence (shares the store discussion from Change E).
- Picker prompt + banner display.

**Recommendation:** treat as a follow-on project. Keep the `<>` notation
reserved for it so the grammar stays forward-compatible.

### Change G — Umbrella rename: call every axis a "tag"

**Status: open question (vocabulary). Depends on Change D freeing "tag".**

**What:** The TODO's closing thought: *"We could start calling all of the above
'tags', if 'tag' gets freed as a term."* If `[]` is renamed to "kit" (Change D),
"tag" is free to become the **collective noun** for any modifier across all four
brackets — an investment-tag, a kit-tag, a preset-tag, a policy-tag.

**Why it's attractive:** gives a single word for "the things in brackets after
an agent name," which is currently awkward to refer to collectively
(`InstanceModifiers` is the code name but is a mouthful in prose/UI).

**Why it's an open question:** "tag" is a heavily overloaded word generally
(git tags, HTML tags, etc.), and the codebase's `InstanceModifiers` already
serves as the umbrella in code. The win is mostly in human-facing prose. Decide
whether the collective noun earns the churn.

---

## 4. Sequencing & dependencies

If we pursue the redesign, a sane order that keeps the tree green at each step:

1. **Change A + B together** (recommended core). Reclassify web/DooD, introduce
   the preset concept, expand `{auto}` into primitives. This is the coherence
   win and is mostly internal (enum reshaping + a preset-expansion step +
   updating the addendum/dispatch keys). Land with full test updates.
2. **Change C** (naming of `()`): trivial, can ride along or land anytime —
   doc-level until surfaced in UI.
3. **Change D + G** (kit rename + umbrella): one atomic vocabulary commit, only
   if we commit to the new naming. Pure rename; do it when no other taxonomy
   work is in flight to avoid merge pain.
4. **Change E** (modifiable kits): only if per-instance capability selection is
   actually desired. Most invasive; gates on a decision about the
   AgentIdentity/InstanceIdentity boundary.
5. **Change F** (policy axis): independent follow-on feature; reserve `<>` now.

**Dependency notes:**
- A is a prerequisite for B's cleanliness (you can't define `{auto}`'s expansion
  targets until web/DooD-style capabilities are first-class).
- E unlocks A's *fully* clean form (per-instance web/DooD) but A can ship in a
  reduced form (filename-chosen capabilities) without E.
- G requires D (can't make "tag" the umbrella while it still names one axis).

---

## 5. Consolidated target taxonomy (if Changes A–C land)

| Axis | Bracket | Kind | Binds at | Members today |
|---|---|---|---|---|
| investment *(or appetite)* | `()` | primitive | pick-time (conf) | researcher / thinker / breakthrough / golem (conf-level) |
| capability *(kit)* | `[]` | primitive | build-time (image) | code, web, DooD *(web/DooD migrated in from mode)* |
| policy | `<>` | primitive | filename default + runtime | (deferred — Change F) |
| preset | `{}` | **composite** (expands into the three above) | runtime | auto *(only member; expands to firewall-capability + network-policy + skip-prompts behavior)* |

Revised gloss: **investment = how hard it THINKS · capability = what it CAN do ·
policy = what it's ALLOWED to do · preset = a named combination of the three.**

---

## 6. Open questions / decisions needed

- **Q1 — `()` name:** keep "investment" or switch to "appetite"? (Avoid
  tier/rank/power — false ordering.)
- **Q2 — `{}` resolution:** preset/composite (Change B) vs. eliminate (3-B-alt)?
  i.e. is the meta-bracket asymmetry acceptable, or do we want four pure peers?
- **Q3 — does "behavior" deserve a bracket?** Skip-prompts is the only behavior
  bit and only appears inside `{auto}`. Options: (a) keep it purely internal to
  preset expansion (no user-facing bracket), (b) give it a bracket only if a
  second standalone behavior switch ever appears. Current lean: (a).
- **Q4 — modifiable capabilities (Change E):** do we want per-instance kit
  selection at all, given it blurs the AgentIdentity/InstanceIdentity boundary?
- **Q5 — kit rename + umbrella (D + G):** worth the wide mechanical churn for a
  vocabulary improvement?
- **Q6 — per-instance store shape:** if E and/or F land, do we add parallel
  JSON maps (`agent_kits_map.json`, policies) or generalise `agent_modes_map`
  into one per-instance overrides store?

---

## 7. Explicitly out of scope / deferred

- The actual **policy feature** (Change F) — net-new, follow-on.
- Any **filename-grammar extension** to parse a third/fourth bracket type. Today
  `parse_stem` handles `[]` and `()` only; modes are runtime-stored, not parsed.
  Presets/policies in the *filename* would require extending the grammar — not
  planned unless we decide presets should be filename-selectable (currently they
  default to runtime selection like modes).
- **Behavior switches** beyond skip-prompts — none exist; don't build the bin
  speculatively.
- Anything touching the docker build/run mechanics beyond renaming dispatch
  keys. The chain → image-tag → compose-file pipeline is sound and stays.

---

## 8. Test impact (when implementation begins)

The taxonomy is well covered, so reshaping it will ripple through tests — treat
green tests as the definition of done:

- `launch/tests/test_agents_crud.py` — `tag_sort_key` / `mode_sort_key` tests,
  `_write_modes_entry` tests, and the `install_latest_md` addendum round-trip
  (asserts on `SEEK_SUMMARY.body` and the section heading) all key off the
  current member set; they update with any rename/reclassification.
- `InstanceModifiers` member-name changes touch every test that references
  `MODE_WARN_AUTO` / `MODE_WARN_DOOD` / `MODE_WEB` / `TAG_CODE` by name.
- New preset-expansion logic needs its own unit tests: a preset token expands to
  exactly its component axis values, and the expansion runs *before* chain
  composition / addendum rendering (so downstream sees only primitives).
- Keep tests asserting on **behavior/contract** (what ends up in the chain, what
  addendums render), not on the internal enum names, to limit brittleness —
  consistent with the existing suite's style.
