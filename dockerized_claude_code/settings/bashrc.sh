# Sourced by every non-interactive bash inside the container (via BASH_ENV).
# Edit this file to add your own functions / aliases; they take effect on next ./run.py launch.

shopt -s expand_aliases   # aliases are off by default in non-interactive bash

# custom ls
alias ll='ls -tarlushFN --color=always --time-style="+%F_%T" --group-directories-first '

# List every custom slash command (under /home/claude/.claude/commands/),
# then a short roster of Claude Code's built-in slash commands.
mango() {
    local commands_dir="$HOME/.claude/commands"

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
