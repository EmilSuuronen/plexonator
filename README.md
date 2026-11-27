# plexonator

One-pane terminal dashboard for people running many AI coding agents in tmux / cmux.
Shows running agents (Claude Code, Codex, ...), what they are doing, system resources, and token totals.
Linux only (WSL works).

## Run

    ./plexonator.py        # needs uv (https://docs.astral.sh/uv/); dependencies install themselves

Keys: `s` settings (saved to `~/.config/plexonator/config.json`), `q` quit.

## Open it with your session

tmux (`~/.tmux.conf`):

    set-hook -g session-created 'split-window -h -l 40% ~/projects/plexonator/plexonator.py'

## How it works

- Agents: scans `/proc` for the process names in settings; maps them to tmux panes / cmux terminals.
- Tokens: reads `~/.claude/projects/**/*.jsonl` and `~/.codex/sessions/**/*.jsonl` incrementally
  (cache in `~/.cache/plexonator/tokens.json`). These formats are undocumented and may change.
- Status: "working" = session log written in the last 15 s, or CPU above 10%.

Self-check: `./plexonator.py --selftest`
