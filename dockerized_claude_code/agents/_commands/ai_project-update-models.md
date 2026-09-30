---
description: Bring every AI's model ids up to date — each `agents/ai/<key>/efforts.tiers` (each rung keeps its family and its effort) and each `agents/ai/<key>/models.list` (the models an instance may pick), against that vendor's official model pages. Refreshes each file's verified-on line and reports before → after per file, and per engine whose tier moved.
---

## What this command maintains

Every `agents/ai/<key>/efforts.tiers` in `/workspace/agents/ai/` — the tree
is the list; do not assume the four that exist today. Each file pins, per
capability standard (`[cheapest]`, `[2025Q1]` … `[best]`), one `model` id and
one `effort` word for that AI. The engines under `agents/engine/` name a
STANDARD, never a model, so nothing there ever changes. Since 2026-09-13 these
files are the ONLY place model ids live — with, since 2026-09-28, the
`models.list` beside each (below), and an instance's pick of one of them in
`instances.toml` / `cluster.toml`, which the launcher itself checks against
the list at every launch.

Ids rot silently: a stale one breaks no test and no launch until the day it
is requested from the vendor. The non-Claude files rotted unnoticed for that
reason (plans/ISSUES.md, "The non-Claude AIs' model ids … age silently") — so
this command covers every AI, and it refreshes each file's verified-on line
EVEN WHEN NOTHING CHANGED, so the next reader knows how old the check is.

## And each AI's `models.list`

Beside every `efforts.tiers` sits `agents/ai/<key>/models.list`: every model
the tag form offers an instance to PIN, one per line, with each one's EFFORT
RANGE — `launch/tags/models.py` documents the columns (`efforts=`, required:
the levels it takes or `-`; `alias=`; `display=`). Where the tiers are the
engines' rated answers, the list is what a person may pick instead, so it is
the vendor's whole current line-up for that AI rather than the rated subset.
It reads the same sources as the tiers, below, in one pass — ranges from the
vendor's effort / thinking / reasoning page (Anthropic's effort page and its
`supportedModels`; OpenAI's per-model pages; Google's thinking table; xAI's
`reasoningEffortOptions`), in the HARNESS's vocabulary (the AI's `[scale]`).

- **Every live, general-purpose text model a normal API key can call.** A
  family the list leaves out is named in the file's header with the reason
  (invitation only, a specialist, not text), so the next run does not
  "discover" it again.
- **Order is the form's: strongest family first, newest version first.**
  Placing a family is a JUDGEMENT no vendor page makes, so a family the list
  does not know yet is never guessed into place: report it, and let the
  operator order it and add it.
- **A vendor's second spelling is an `alias=`,** on the line of the id the
  vendor calls the model's own (Anthropic's dated snapshot, with its
  undated alias beside it). A tier may pin either spelling.
- **Re-read every RANGE and diff it, like the ids** (researcher, gate
  model-picker-3). A level the vendor ADDED is invisible forever otherwise —
  the form cannot offer it; a level it REMOVED fails nothing until a person
  picks it and the API refuses. `efforts=-` is for a model that takes no
  effort parameter, or one the launcher chooses to send none; every `-` line
  says which, and why, in its comment. The vendors' own `none` level is never
  listed (it asks for no reasoning, which is not sending nothing). Ranges and
  tiers must agree — a tier's effort lies in its model's range, and a tier
  omits its effort exactly when the range is empty; the scan holds both.
- **A model the vendor retired is DELETED, at once** (operator, 2026-09-29) —
  and so is every other mention of its id in the tree: the tiers' pins and
  their "Also rated" / line-up comments, the other models.list comments,
  README and plans examples (`grep` the id across the repo; the hit list is
  the checklist). An instance still pinned to it runs its engine's pair with
  a notice, and `python -m launch.audit` reports it (`stale_model`); stored
  picks — `instances.toml`, `cluster.toml` — are the operator's, never
  edited here. An announced floor ("not sooner than") stays a comment beside
  a live id: a floor is not a date. Anthropic retires only after a
  deprecation notice, "at least 60 days" ahead — watch its Deprecated
  column, not the floor.
- **Every tier pin stays listed.** When a pinned model goes, its tier moves
  in the same run (the rules below), or the scan fails the tree.
- **Engines and cluster templates name no model**, so they need no edit: an
  engine names a capability standard (`tag.budget`), a `.legoset` names
  agents, and a `.lego` may not pin a model at all. No edit is not no change:
  every engine naming a standard whose pin moved now runs something else,
  and the report says which (Report shape).
- **Refresh the verified-on line** on every run, changed or not, exactly as
  for the tiers.

## Sources of truth, per AI — check in this order, never guess

For every AI: the vendor's page first, its deprecations/retirements page
second, a general web lookup third, training-data knowledge last and only
FLAGGED as "derived from training data, potentially stale".

**claude** (`agents/ai/claude/efforts.tiers`)
1. `https://platform.claude.com/docs/en/about-claude/models/overview` — the
   "Latest models comparison" table has the current Claude API ID per tier;
   "Legacy models" lists the older ids.
2. `https://platform.claude.com/docs/en/api/models/list` — the Models API
   (`GET https://api.anthropic.com/v1/models`), programmatic and robust to a
   page restructure.
3. `https://platform.claude.com/docs/en/about-claude/model-deprecations` —
   retirement dates, when a tier has two "latest" candidates.

**gemini** (`agents/ai/gemini/efforts.tiers`)
1. `https://ai.google.dev/gemini-api/docs/models` — the Gemini API's model
   table; an id is live iff this page lists it.
2. `https://ai.google.dev/gemini-api/docs/deprecations` — shutdown dates.
3. The Gemini CLI's OWN model tables (`github.com/google-gemini/gemini-cli`:
   the alias table — `pro`, `flash`, `flash-lite`, `auto` — and its
   `modelIdResolutions`). The CLI re-resolves ids it knows, so a pin the API
   lists as live can still be rewritten by the CLI; and the file's
   verbatim-request assumption (an id ABSENT from those tables is requested
   as written) must be re-stated if the tables gain a rule for one of our
   ids. Report, do not fix here, an alias of the CLI's that points at a
   shut-down model — that is the CLI's bug, and the hazard lives in the
   file's comments.

**chatgpt** (`agents/ai/chatgpt/efforts.tiers`)
1. `https://learn.chatgpt.com/docs/models` — the file's cited source for the
   Codex slugs (Astra, Sol, Terra, Luna families).
2. OpenAI's platform models page and its deprecations page, linked from it,
   for retirement dates and successors.

**grok** (`agents/ai/grok/efforts.tiers`)
1. `https://docs.x.ai/developers/models` — ids AND prices: the file records
   the vendor's list price (USD per 1M tokens, input / output) beside each
   tier, so a price change is a change to report too.

A new AI added under `agents/ai/` adds its own sources here, in its file's
header, before this command is run for it.

## Rules

The authority on WHICH configuration a tier pins is
`agents/ai/capability.standards`: the cheapest configuration that is LIVE,
RATED and LAUNCHABLE and whose index meets the standard. This command keeps
the pins honest against the vendors; it never overrides that rule.

- **Keep the rung.** A tier's model stays in its FAMILY and its effort word
  stays; only the version bumps. Haiku stays Haiku, Flash-Lite stays
  Flash-Lite, Luna stays Luna, a non-reasoning pin stays non-reasoning.
  Never move a tier to another family because the newer one "looks better" —
  which family serves which standard is the capability rule, and re-deriving
  THAT needs the Artificial Analysis leaderboard, which this command does not
  read.
- **An unrated successor is AVAILABLE, not a tier.** A newer id in the same
  family with no Artificial Analysis index yet cannot be shown to meet its
  standard, so it does not replace the pin. Record it in the file's header
  as available, with its release date and price, and leave the rated pin in
  place. The next re-derivation, once the leaderboard rates it, moves the
  rung. (2026-09-25: the first run under this command bumped four families
  to unrated successors and had to put them back.)
- **Launchable means the harness takes it.** A model released after the CLI
  in the harness image can be refused by that CLI until the image's next
  weekly rebuild; a vendor listing a model does not make it runnable in a
  container built last Monday.
- **Dead is urgent, scheduled is recorded.** An id the vendor has already
  shut down is replaced now, by the family's rated successor, with the date
  in the report — or, when the family has none, by the capability rule's
  answer in ANOTHER family, the one case "keep the rung" yields: Haiku 4.5
  is the last Haiku, so when it goes, Claude's `[cheapest]` moves to the
  cheapest live model at its lowest effort (a Sonnet, today) and golem, which
  names `cheapest`, follows with no edit of its own. Say so in the report,
  since the rung's price moves with it. An id with a scheduled shutdown stays
  pinned while it is live and the rule's answer: write the date and the
  vendor's replacement beside it, so every later reader and run sees it
  coming, and move it on the last run before that date.
- **Update active and commented references together** in the same file — the
  "Also rated" and line-up comments must not contradict the pins above them.
- **Respect deliberate pins.** A comment saying an id is pinned on purpose
  (`# pinned to … for reproducibility`) is left untouched and reported.
- **Refresh the verified-on line** in every file's header —
  `# Ids verified <YYYY-MM-DD> against <the source you actually read>` — on
  every run, including a run that changed no id. It is the only signal of
  how stale a file is.

## Report shape

Per file: `before → after` for every changed pin, or "already current" with
the verified-on date you wrote. Per `models.list`: the models added, the ones
deleted (with the vendor's retirement date, and every other file the id was
cleaned from), every range that changed (`before → after`), and every new
family left out pending the operator's placement. Then, once for the run: every successor
recorded as available but unrated, any id the vendor has retired or
scheduled, any Gemini CLI alias that resolves to a shut-down model, any price
that moved, and anything you could not verify (name the page that was
unreachable). Where a model id carries a date suffix or you know its
release confidently, give approximate release timing; acknowledge uncertainty
otherwise.

Per ENGINE, derived rather than remembered: every standard whose pin changed
on any AI, with the engines that name it and each one's `before → after`
(model, effort) per AI. The engines' own files never change, so this is the
only line that shows an engine now runs a different model (2026-09-29: five
of eight moved and none of their files changed; the golem paragraph above is
one instance of this). Read the map off the RESOLVED budgets, never by grepping
the files: a nested engine may inherit its tier, and a grep would drop it
silently. Check the count:

    python3 -c "from pathlib import Path; from launch.tags.registry import scan_all; r = scan_all(Path('agents')); [print(f'{e.budget.effort_tier:<10} {n}') for n, e in sorted(r.engines.items())]; print(len(r.engines), 'engines')"
