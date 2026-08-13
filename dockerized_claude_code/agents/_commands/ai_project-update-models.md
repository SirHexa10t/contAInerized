---
description: Upgrade the project's agents by updating each `.conf` to the latest ANTHROPIC_MODEL per the official Anthropic docs (keeping each agent's existing tier). Reports before → after per file.
---

## Sources of truth — check in this order

1. **Models overview page** (canonical):
   `https://platform.claude.com/docs/en/about-claude/models/overview`
   The "Latest models comparison" table at the top has the current **Claude API ID** for each tier. The "Legacy models" section lists older IDs (some marked deprecated with retirement dates).

2. **Models API** (programmatic, more robust against page restructuring):
   `https://platform.claude.com/docs/en/api/models/list`
   `GET https://api.anthropic.com/v1/models` returns every available model ID. Use this if the docs page is unreachable or the table is malformed.

3. **Model deprecations page** (for retirement context):
   `https://platform.claude.com/docs/en/about-claude/model-deprecations`
   Cross-reference if a tier appears to have multiple "latest" candidates.

4. **General web lookup** — fall back if all three above are unreachable.

5. **Training-data knowledge** — last-resort fallback. If you go here, **flag the version chosen as derived from training-data knowledge and potentially stale.**

## Where the model confs live

`/workspace/agents/engine/<name>/engine.conf` — one conf per engine tag; the `ANTHROPIC_MODEL` line is the value to update (nested engine dirs inherit and override their parent's conf, so check the whole `engine/` tree). If that tree is missing or holds no `engine.conf` files, locate the correct directory (it's the one whose confs carry `ANTHROPIC_MODEL` lines); if you can't find it, ask the user where it lives before proceeding.

## Rules

- **Keep the tier**: an agent on `claude-haiku-X-Y` stays on haiku; only the `X-Y` version bumps. Same for sonnet and opus. Never silently move between tiers.
- **Update both active and commented-out references** in the same file — keep example references consistent with the active value so the documentation around each conf doesn't lie.
- **Respect deliberate pins**: if a conf has a comment indicating the version is pinned intentionally (e.g. `# pinned to opus-4-6 for reproducibility`), **leave it untouched** and report it in the summary.
- **Report shape**: for each file changed, list `before → after`. If everything is already current, say so and give approximate release timing where the model ID carries a date suffix or you know it confidently; acknowledge uncertainty otherwise.
