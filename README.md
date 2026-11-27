# plexonator

One-pane terminal dashboard for people running many AI coding agents in tmux / cmux.
Shows running agents (Claude Code, Codex, ...), what they are doing, system resources, and token totals.
Linux only (WSL works).

## Run

    ./plexonator.py        # needs uv (https://docs.astral.sh/uv/); dependencies install themselves

Try it with made-up data: `./plexonator.py --demo` (or `--demo=calm`).

Keys: `enter` agent details, `g` go to the agent's pane, `s` settings, `q` quit.

## Exact "needs you" status (Claude Code)

    ./plexonator.py --install-hooks

Adds a small hook to `~/.claude/settings.json` (a backup is kept next to it) so plexonator knows when an
agent is blocked on a permission prompt, working, or done. Without it, status is estimated from activity.
`--uninstall-hooks` removes it. Applies to agents started after you install.

## Open it with your session

tmux (`~/.tmux.conf`):

    set-hook -g session-created 'split-window -h -l 40% ~/projects/plexonator/plexonator.py'

## How it works

- Agents: scans `/proc` for the process names in settings; maps them to tmux panes / cmux terminals.
- Tokens: reads `~/.claude/projects/**/*.jsonl` and `~/.codex/sessions/**/*.jsonl` incrementally
  (cache in `~/.cache/plexonator/tokens.json`). These formats are undocumented and may change.
- Status: from the hook's last event when installed; otherwise "working" = session log written in the
  last 15 s, or CPU above 10%.
- Per-agent context and tokens come from the same logs (Claude Code and Codex only). Claude logs do not
  state the context window size, so it is assumed to be 200K, or 1M once a session exceeds that.
- `g` asks tmux or cmux to focus the agent's pane.

Self-check: `./plexonator.py --selftest`

## Website

`docs/` is a static page for GitHub Pages. Its screenshots come from demo mode only:
`./docs/make_shots.py` regenerates them.
