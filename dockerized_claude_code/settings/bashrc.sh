# Sourced by every non-interactive bash inside the container (via BASH_ENV).
# Edit this file to add your own functions / aliases; they take effect on next ./run.py launch.

shopt -s expand_aliases   # aliases are off by default in non-interactive bash

# custom ls
alias ll='ls -tarlushFN --color=always --time-style="+%F_%T" --group-directories-first '

# List every custom slash command (under /home/claude/.claude/commands/),
# then any project skills (under /home/claude/.claude/skills/), 
# then any stored prompts (under /workspace/.prompts/, only shown when that dir exists),
# and finally a short roster of Claude Code's built-in slash commands.
man() {
    local commands_dir="$HOME/.claude/commands"
    local skills_dir="$HOME/.claude/skills"
    local prompts_dir="/workspace/.prompts"

    printf '\nCustom commands (from custom_commands/):\n'
    if [ -d "$commands_dir" ] && compgen -G "$commands_dir/*.md" >/dev/null; then
        for cmd_file in "$commands_dir"/*.md; do
            local name first_line
            name=$(basename "$cmd_file" .md)
            first_line=$(head -1 "$cmd_file" | sed 's/^#* *//')
            printf '  /%-20s %s\n' "$name" "$first_line"
        done
    else
        printf '  (none)\n'
    fi

    if [ -d "$skills_dir" ] && compgen -G "$skills_dir/*/SKILL.md" >/dev/null; then
        printf '\nProject skills:\n'
        for skill_md in "$skills_dir"/*/SKILL.md; do
            [ -f "$skill_md" ] || continue
            local skill_name skill_desc
            skill_name=$(basename "$(dirname "$skill_md")")
            skill_desc=$(awk '/^description:/{sub(/^description:[[:space:]]*/, ""); sub(/^"/, ""); sub(/"$/, ""); print; exit}' "$skill_md")
            if [ -n "$skill_desc" ]; then
                printf '  /%-20s %s\n' "$skill_name" "$skill_desc"
            else
                printf '  /%s\n' "$skill_name"
            fi
        done
    fi

    if [ -d "$prompts_dir" ]; then
        printf "\nCustom prompts (run to use the specified file's contents as a prompt):\n"
        local found=0
        for prompt_file in "$prompts_dir"/*; do
            [ -f "$prompt_file" ] || continue
            printf '  @%s\n' "${prompt_file#/workspace/}"
            found=1
        done
        [ "$found" -eq 0 ] && printf '  (none)\n'
    fi

    printf '\n'
    printf 'Built-in Claude Code commands (core set — run /help in Claude for the full list):\n'
    printf '  /cost       Token counts and cost for this session\n'
    printf '  /usage      Rate-limit headroom (5h + 7d windows)\n'
    printf '  /context    Context window usage visualization\n'
    printf '  /stats      Usage patterns across models\n'
    printf '  /status     Version, model, account, connectivity\n'
    printf '  /memory     View / edit loaded CLAUDE.md + auto memory\n'
    printf '  /compact    Compress conversation history\n'
    printf '  /clear      Clear the session\n'
    printf '  /model      Switch model mid-session\n'
    printf '  /help       List of built-in commands\n\n'
}

# Resolve _summary.py: container bind-mount path first, else next to this bashrc.
_SUMMARY_PY="/home/claude/.claude/_summary.py"
[ -f "$_SUMMARY_PY" ] || _SUMMARY_PY="$(dirname "${BASH_SOURCE[0]}")/_summary.py"

# Diff /workspace against the manifest in /workspace/.claude_summary;
# print NEW / CHANGED / DELETED lines for every file that differs.
# Useful when the AI also needs to know about DELETED files to prune from the prose.
summary_diff() { python3 "$_SUMMARY_PY" diff; }

# Same comparison, but prints just one path per line for the files that actually need
# re-reading (NEW + CHANGED, no prefixes, no DELETED). The cleanest list for the AI
# to walk through during /write-summary.
summary_files() { python3 "$_SUMMARY_PY" files; }

# Replace the manifest block in /workspace/.claude_summary with a fresh listing.
# Refuses to run unless the manifest's <!-- manifest:begin --> / <!-- manifest:end -->
# markers are present (so a missing block fails loudly rather than silently
# corrupting the file). Run AFTER summary_diff has informed you of changes —
# running it earlier would clobber the manifest summary_diff is about to compare
# against, and a re-run would then show no changes at all.
summary_save_manifest() { python3 "$_SUMMARY_PY" save; }

