#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["textual>=3.0", "psutil>=5.9"]
# ///
"""plexonator: one-pane dashboard for AI coding agents and system resources (Linux).

Run:   ./plexonator.py            (needs uv; deps install themselves)
Demo:  ./plexonator.py --demo     (fake data, or --demo=calm)
Hooks: ./plexonator.py --install-hooks    (exact "needs you" status for Claude Code; --uninstall-hooks undoes it)
Test:  ./plexonator.py --selftest
"""
import asyncio
import json
import os
import random
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

HOME = Path.home()
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "plexonator" / "config.json"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "plexonator" / "tokens.json"
STATE = CACHE.parent / "state"  # one small file per agent session, written by the hook
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data, indent=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=indent))
    tmp.replace(path)  # atomic: a kill mid-write never leaves a broken file


# ── Claude Code hook ────────────────────────────────────────────────────────
# Claude Code runs `plexonator.py --hook` on these events with a JSON description on stdin.
# We only remember the last one per session; the dashboard turns it into a status.
HOOK_EVENTS = {"UserPromptSubmit": "working", "PostToolUse": "working", "PostToolUseFailure": "working",
               "PermissionDenied": "working", "ElicitationResult": "working",
               "PermissionRequest": "needs", "Elicitation": "needs", "Notification": None,
               "Stop": "done", "SessionEnd": None}


def hook_state(d):
    """needs / working / done for one hook event, or None when it says nothing new."""
    event = d.get("hook_event_name")
    if event == "Notification":  # also fires for "waiting for input", which Stop already covered
        asks = d.get("notification_type") in ("permission_prompt", "elicitation_dialog")
        return "needs" if asks or "permission" in str(d.get("message", "")).lower() else None
    return HOOK_EVENTS.get(event)


def hook(stream):
    try:
        d = json.load(stream)
        path = STATE / (re.sub(r"[^A-Za-z0-9_-]", "", str(d["session_id"])) + ".json")  # it becomes a file name
        if d.get("hook_event_name") == "SessionEnd":
            path.unlink(missing_ok=True)
        elif state := hook_state(d):
            save_json(path, {"state": state, "message": d.get("message"), "tool": d.get("tool_name"),
                             "ts": time.time()})
    except Exception:
        pass  # a hook must never get in the agent's way


def install_hooks(settings, remove=False):
    """Add (or remove) our hook in Claude Code's settings.json. Other hooks are left alone."""
    data = json.loads(settings.read_text()) if settings.exists() else {}  # invalid JSON: stop, do not overwrite
    backup = settings.with_name(settings.name + ".plexonator-backup")
    if settings.exists() and not backup.exists():
        shutil.copy2(settings, backup)
    script = shlex.quote(str(Path(__file__).resolve()))
    # plain python3 starts faster than uv; the hook path needs only the standard library
    command = f"python3 {script} --hook" if shutil.which("python3") else f"{script} --hook"
    hooks = data.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        kept = [g for g in hooks.get(event, [])
                if not any("plexonator" in h.get("command", "") and "--hook" in h.get("command", "")
                           for h in g.get("hooks", []))]
        if not remove:
            kept.append({"hooks": [{"type": "command", "command": command, "async": True}]})
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        del data["hooks"]
    save_json(settings, data, indent=2)
    return backup


if "--hook" in sys.argv:
    hook(sys.stdin)
    sys.exit(0)

import psutil  # noqa: E402
from rich.text import Text  # noqa: E402
from textual.app import App  # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.markup import escape  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.widgets import DataTable, Footer, Input, Label, Static  # noqa: E402

DEFAULTS = {"refresh": 2, "theme": "tokyo-night", "logo": True,
            "agents": "claude,codex,gemini,aider,opencode,amp"}
RUNTIMES = ("node", "bun", "deno", "python", "python3")
LOGO = """\
██████╗ ██╗     ███████╗██╗  ██╗ ██████╗ ███╗   ██╗ █████╗ ████████╗ ██████╗ ██████╗
██╔══██╗██║     ██╔════╝╚██╗██╔╝██╔═══██╗████╗  ██║██╔══██╗╚══██╔══╝██╔═══██╗██╔══██╗
██████╔╝██║     █████╗   ╚███╔╝ ██║   ██║██╔██╗ ██║███████║   ██║   ██║   ██║██████╔╝
██╔═══╝ ██║     ██╔══╝   ██╔██╗ ██║   ██║██║╚██╗██║██╔══██║   ██║   ██║   ██║██╔══██╗
██║     ███████╗███████╗██╔╝ ██╗╚██████╔╝██║ ╚████║██║  ██║   ██║   ╚██████╔╝██║  ██║
╚═╝     ╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚═╝  ╚═╝"""
BLOCKS = " ▁▂▃▄▅▆▇█"


# ── token logs ──────────────────────────────────────────────────────────────
# Per file we keep {"off": bytes read, "days": {iso day: [in, out, cache_read, cache_write]}}.
# Both formats are undocumented, so every parser skips lines it does not understand.
def _day(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()


def _add(days, day, v, sign=1):
    cur = days.setdefault(day, [0, 0, 0, 0])
    for i in range(4):
        cur[i] += sign * v[i]


def feed_claude(line, st):
    if b'"ai-title"' in line:
        try:
            st["title"] = str(json.loads(line)["aiTitle"])[:80]
        except (ValueError, KeyError, TypeError):
            pass
        return
    if b'"usage"' not in line:
        return
    try:
        d = json.loads(line)
        m, u = d["message"], d["message"]["usage"]
        day = _day(d["timestamp"])
        v = [int(u.get(k) or 0) for k in ("input_tokens", "output_tokens",
                                          "cache_read_input_tokens", "cache_creation_input_tokens")]
    except (ValueError, KeyError, TypeError, AttributeError):
        return
    last = st.get("last")
    # shortcut: assumes repeats of one message id are adjacent among usage lines (they are
    # today: one line per streamed content block); upgrade to a full id set if totals drift.
    if last and last[0] == m.get("id"):
        _add(st["days"], last[1], last[2], -1)
    _add(st["days"], day, v)
    st["last"] = [m.get("id"), day, v]
    if not d.get("isSidechain"):
        st["ctx"], st["model"] = v[0] + v[2] + v[3], m.get("model")  # what the last request carried in


def feed_codex(line, st):
    try:
        if b'"session_meta"' in line and "cwd" not in st:
            st["cwd"] = json.loads(line)["payload"]["cwd"]  # lets us match the log to a process
        elif b'"turn_context"' in line:
            st["model"] = json.loads(line)["payload"]["model"]
    except (ValueError, KeyError, TypeError):
        pass
    if b'"token_count"' not in line:
        return
    try:
        d = json.loads(line)
        p = d["payload"]
        info = p["info"]
        u = info["total_token_usage"]
        cached = int(u.get("cached_input_tokens") or 0)
        tot = [int(u.get("input_tokens") or 0) - cached, int(u.get("output_tokens") or 0), cached, 0]
        day = _day(d["timestamp"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return
    prev = st.get("tot", [0, 0, 0, 0])  # running totals: deltas ignore repeated events
    _add(st["days"], day, [max(0, a - b) for a, b in zip(tot, prev)])
    st["tot"] = tot
    st["ctx"] = int((info.get("last_token_usage") or {}).get("total_tokens") or 0)
    st["win"] = info.get("model_context_window")
    if p.get("rate_limits"):
        st["limits"], st["limits_ts"] = p["rate_limits"], d["timestamp"]


def codex_limits(files, now):
    """[(window label, used %)] from the newest Codex rate-limit reading that has not reset yet."""
    newest = max((st for st in list(files.values()) if st.get("limits")),
                 key=lambda st: st.get("limits_ts", ""), default=None)
    out = []
    for key in ("primary", "secondary"):
        w = (newest or {}).get("limits", {}).get(key) or {}
        try:
            if w["resets_at"] > now:
                minutes = int(w["window_minutes"])
                out.append((f"{minutes // 1440}d" if minutes >= 1440 else f"{minutes // 60}h", float(w["used_percent"])))
        except (KeyError, TypeError, ValueError):
            pass
    return out


SOURCES = {"claude": (HOME / ".claude" / "projects", feed_claude),
           "codex": (HOME / ".codex" / "sessions", feed_codex)}


def scan_file(path, st, feed):
    """Read only the bytes added since the last scan."""
    try:
        size = path.stat().st_size
        if size < st["off"]:  # file was rewritten: start over
            st.clear()
            st.update(off=0, days={})
        if size == st["off"]:
            return
        with open(path, "rb") as f:
            f.seek(st["off"])
            for line in f:
                if not line.endswith(b"\n"):
                    break  # half-written line: pick it up next scan
                feed(line, st)
                st["off"] += len(line)
    except OSError:
        pass


def totals(files, since=""):
    t = [0, 0, 0, 0]
    for st in list(files.values()):
        for day, v in list(st["days"].items()):
            if day >= since:
                t = [a + b for a, b in zip(t, v)]
    return t


# ── multiplexers ────────────────────────────────────────────────────────────
def _run(*cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def mux_map(cmux_pids):
    """shell pid -> {"where", "title", "sid", "target"} for tmux panes and cmux agent terminals."""
    out = {}
    if shutil.which("tmux"):
        fmt = "#{pane_pid}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}\t#{pane_title}"
        for row in _run("tmux", "list-panes", "-a", "-F", fmt).splitlines():
            pid, where, pane_id, title = (row.split("\t") + ["", "", ""])[:4]
            if pid.isdigit():
                out[int(pid)] = {"where": "tmux " + where, "title": title, "sid": None, "target": ["tmux", pane_id]}
    cmux = HOME / ".cmux" / "bin" / "cmux"
    if cmux.exists() and any(k.startswith("CMUX_") for k in os.environ):
        try:
            terms = json.loads(_run(str(cmux), "--json", "terminal", "list") or "[]")
        except ValueError:
            terms = []
        for t in terms:
            extra = t.get("extra") or {}
            if not extra.get("agent"):
                continue
            tid = t["id"]
            if tid not in cmux_pids:  # one lookup per terminal, then cached
                try:
                    cmux_pids[tid] = json.loads(_run(str(cmux), "--json", "terminal", tid,
                                                     "process", "show"))["pid"]
                except (ValueError, KeyError, TypeError):
                    continue
            out[cmux_pids[tid]] = {"where": "cmux", "title": t.get("title", ""),
                                   "sid": extra.get("agent_session_id"), "target": ["cmux", t.get("tab_id")]}
    return out


def jump(target):
    """Focus the pane an agent runs in. True when the multiplexer accepted it."""
    kind, ident = target or (None, None)
    if not ident:
        return False

    def ok(*cmd):
        try:
            return subprocess.run(cmd, capture_output=True, timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    if kind == "tmux":
        done = ok("tmux", "select-window", "-t", ident) and ok("tmux", "select-pane", "-t", ident)
        ok("tmux", "switch-client", "-t", ident)  # only matters when the pane is in another session
        return done
    return ok(str(HOME / ".cmux" / "bin" / "cmux"), "tab", ident, "focus")


# ── agents ──────────────────────────────────────────────────────────────────
def agent_name(name, cmdline, names):
    """Which configured agent a process is, or None. Only looks past argv[0] for script runtimes."""
    cands = [name] + [os.path.basename(c) for c in cmdline[:2 if name in RUNTIMES else 1]]
    return next((n for n in names if n in cands), None)


def session_log(name, cwd, sid, tokens):
    """The agent's session log, or None when we cannot tell which one it is."""
    if name == "claude":
        root = HOME / ".claude" / "projects"
        # shortcut: without a session id (non-cmux), two Claudes in one folder share the newest log
        files = root.glob(f"*/{sid}.jsonl") if sid else (root / re.sub(r"[^A-Za-z0-9]", "-", cwd)).glob("*.jsonl")
    elif name == "codex":  # shortcut: same folder ambiguity as above; Codex logs carry no process id
        files = [Path(p) for p, st in list(tokens.get("codex", {}).items()) if st.get("cwd") == cwd]
    else:
        return None  # shortcut: other agents are listed without log data; add a parser per agent when wanted
    best = None
    for f in files:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if not best or mtime > best[0]:
            best = (mtime, f)
    return best and best[1]


def agent_state(cpu, act, hook, now):
    """needs / working / done / idle. The hook's last event wins while nothing has been logged since."""
    if hook and hook.get("state") in ("needs", "done") and (act is None or act <= hook["ts"] + 1):
        # shortcut: a tool that runs long after you approve it still reads "needs" until it finishes,
        # because Claude Code sends no event at approval
        return hook["state"]
    if cpu > 10 or (act is not None and now - act < 15):
        return "working"
    if hook and hook.get("state") == "working" and now - max(hook["ts"], act or 0) < 300:
        return "working"  # thinking quietly; without a Stop event this ages out
    return "done" if hook and hook.get("state") == "done" else "idle"


def context_window(st):
    # shortcut: Claude logs do not state the window; assume 200K, or 1M once a session exceeds that
    return st.get("win") or (1_000_000 if st.get("ctx", 0) > 200_000 or "[1m]" in str(st.get("model")) else 200_000)


ORDER = {"needs": 0, "working": 1, "done": 2, "idle": 3}


def find_agents(names, procs, mux, tokens):
    """procs: pid -> Process, kept between calls so cpu_percent has a baseline."""
    found = {}
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        hit = agent_name(p.info["name"] or "", p.info["cmdline"] or [], names)
        if hit:
            found[p.info["pid"]] = (hit, p)
    rows, now = [], time.time()
    for pid, (name, p) in found.items():
        if p.info["ppid"] in found:
            continue  # wrapper + child process: show one row
        q = procs.setdefault(pid, p)
        try:
            with q.oneshot():
                cpu, mem, cwd, started = q.cpu_percent(None), q.memory_info().rss, q.cwd(), q.create_time()
            pane = next((mux[a.pid] for a in [q, *q.parents()] if a.pid in mux), {})  # q itself: `tmux new-window claude`
        except psutil.Error:
            continue
        log = session_log(name, cwd, pane.get("sid"), tokens)
        st = tokens.get(name, {}).get(str(log), {}) if log else {}
        try:
            act = log.stat().st_mtime if log else None
        except OSError:
            act = None
        hook = load_json(STATE / f"{log.stem}.json", None) if log and name == "claude" else None
        rows.append({"name": name, "pid": pid, "cpu": cpu, "mem": mem, "cwd": cwd, "started": started,
                     "act": act, "state": agent_state(cpu, act, hook, now), "where": pane.get("where", ""),
                     "title": pane.get("title") or st.get("title", ""), "target": pane.get("target"),
                     "log": log, "hook": hook, "model": st.get("model"), "ctx": st.get("ctx"),
                     "win": context_window(st),
                     # shortcut: subagent logs are separate files and are not added to their parent session
                     "tok": totals({"": st}) if st else None})
    for pid in set(procs) - set(found):
        del procs[pid]
    rows.sort(key=lambda r: (ORDER[r["state"]], -(r["act"] or 0)))
    return rows


# ── agent detail ────────────────────────────────────────────────────────────
def tool_line(name, args):
    """One line for a tool call: its name and the most telling argument."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return f"{name} {args}"
    if isinstance(args, dict):
        args = next((args[k] for k in ("file_path", "command", "cmd", "pattern", "url", "query", "description", "prompt")
                     if args.get(k)), "")
    return f"{name} {args}".strip()


def claude_detail(d, out):
    if d.get("type") == "last-prompt":
        out.setdefault("prompt", d.get("lastPrompt"))
    elif d.get("type") == "assistant" and not d.get("isSidechain"):
        out.setdefault("branch", d.get("gitBranch"))
        for block in reversed(d["message"].get("content") or []):
            if block.get("type") == "text" and block.get("text", "").strip():
                out.setdefault("reply", block["text"])
            elif block.get("type") == "tool_use":
                out.setdefault("tool", tool_line(block.get("name"), block.get("input")))


def codex_detail(d, out):
    p = d.get("payload") or {}
    kind = p.get("type")
    if kind == "message":
        text = " ".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict)).strip()
        if p.get("role") == "assistant" and text:
            out.setdefault("reply", text)
        elif p.get("role") == "user" and text and not text.startswith("<"):  # skip injected context blocks
            out.setdefault("prompt", text)
    elif kind == "user_message":
        out.setdefault("prompt", p.get("message"))
    elif kind in ("function_call", "custom_tool_call"):
        out.setdefault("tool", tool_line(p.get("name"), p.get("arguments", p.get("input"))))


def read_detail(log, name, tail=400_000):
    """Last prompt, reply and tool call from the end of a session log. Missing parts stay absent."""
    out = {}
    try:
        with open(log, "rb") as f:
            start = max(0, os.path.getsize(log) - tail)
            f.seek(start)
            lines = f.read().split(b"\n")[1 if start else 0:]
    except OSError:
        return out
    parse = claude_detail if name == "claude" else codex_detail
    for line in reversed(lines):
        try:
            parse(json.loads(line), out)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if all(out.get(k) for k in ("prompt", "reply", "tool")):
            break
    return out


# ── demo mode ───────────────────────────────────────────────────────────────
# Fake data for screenshots and for trying the UI: --demo reads nothing from this machine.
# Row: agent, folder, task, pane, cpu %, memory MB, uptime minutes, seconds idle, state,
#      context tokens, session tokens, model, branch.
DEMO = {
    "busy": [
        ("claude", "~/code/orbital-api", "Refactor auth middleware", "tmux dev:1.0", 3, 512, 184, 40, "needs",
         142_000, 61_400_000, "claude-opus-5-5", "auth-session-store"),
        ("codex", "~/code/orbital-api", "Fix flaky checkout test", "tmux dev:1.1", 27, 318, 96, 0, "working",
         96_000, 18_200_000, "gpt-5-codex", "fix/checkout-flake"),
        ("claude", "~/code/pixel-forge", "Port shaders to WGSL", "tmux dev:2.0", 41, 604, 301, 0, "working",
         171_000, 112_000_000, "claude-sonnet-5-5", "wgsl-port"),
        ("gemini", "~/code/tidepool-web", "Migrate router to v7", "tmux web:1.0", 19, 287, 47, 0, "working",
         None, None, None, None),
        ("claude", "~/code/kiln", "Profile incremental build", "tmux dev:3.0", 22, 455, 122, 0, "working",
         58_000, 24_900_000, "claude-opus-5-5", "perf/incremental"),
        ("claude", "~/code/tidepool-web", "Write release notes", "tmux web:2.0", 0, 338, 512, 190, "done",
         31_000, 4_100_000, "claude-haiku-5-5", "main"),
        ("codex", "~/code/pixel-forge", "Review PR #482", "tmux dev:2.1", 1, 301, 240, 410, "done",
         204_000, 39_600_000, "gpt-5-codex", "review/482"),
        ("aider", "~/code/lantern", "Add rate limiter", "tmux web:1.1", 2, 196, 58, 950, "idle",
         None, None, None, None),
        ("opencode", "~/code/kiln", "Bisect cache regression", "tmux dev:3.1", 0, 244, 75, 3300, "idle",
         None, None, None, None),
    ],
    "calm": [
        ("claude", "~/code/lantern", "Add rate limiter", "tmux main:1.0", 18, 402, 35, 0, "working",
         47_000, 9_300_000, "claude-sonnet-5-5", "rate-limiter"),
        ("codex", "~/code/lantern", "Update API docs", "tmux main:1.1", 1, 290, 35, 240, "done",
         22_000, 2_800_000, "gpt-5-codex", "docs/api"),
        ("claude", "~/code/kiln", "Waiting for review", "tmux main:2.0", 0, 311, 610, 5400, "idle",
         88_000, 51_000_000, "claude-opus-5-5", "main"),
    ],
}
DEMO_DETAIL = {
    "prompt": "Move the auth middleware onto the new session store and delete the legacy lookup. "
              "Keep the public interface the same and make sure the billing routes still pass.",
    "tool": "Bash npm run migrate:sessions -- --env staging",
    "reply": "The middleware now reads from the session store and all 42 auth tests pass. Three call sites "
             "still used legacy.get(); I updated two of them. The last one is the staging migration, which "
             "rewrites rows in place, so I am asking before I run it.",
}


def demo_agents(scenario):
    now, rows = time.time(), []
    for i, (name, cwd, title, where, cpu, mem, up, idle, state, ctx, tok, model, branch) in enumerate(DEMO[scenario]):
        hook = {"state": "needs", "message": "Claude needs your permission to use Bash", "ts": now} if state == "needs" else None
        rows.append({"name": name, "pid": 4000 + i, "cpu": cpu + random.uniform(0, 5) * (state == "working"),
                     "mem": mem * 1e6, "cwd": cwd, "started": now - up * 60, "act": now - idle - 2,
                     "state": state, "where": where, "title": title, "target": None, "log": None, "hook": hook,
                     "model": model, "ctx": ctx, "win": 258_000 if name == "codex" else 200_000,
                     "tok": tok and [int(tok * f) for f in (0.001, 0.02, 0.95, 0.029)], "branch": branch})
    return rows


def demo_tokens():
    today = date.today().isoformat()
    week = (date.today() - timedelta(days=3)).isoformat()

    def days(scale):
        return {"demo": {"days": {today: [int(v * scale) for v in (41_000, 1_900_000, 420_000_000, 7_800_000)],
                                  week: [int(v * scale) for v in (212_000, 8_300_000, 1_700_000_000, 36_000_000)],
                                  "2000-01-01": [int(v * scale) for v in (3_100_000, 96_000_000, 21e9, 410_000_000)]}}}
    out = {"claude": days(1), "codex": days(0.45)}
    out["codex"]["demo"].update(limits_ts="9", limits={
        "primary": {"used_percent": 38, "window_minutes": 300, "resets_at": time.time() + 9000},
        "secondary": {"used_percent": 61, "window_minutes": 10080, "resets_at": time.time() + 300000}})
    return out


def demo_system(scenario):
    base, gb = (58 if scenario == "busy" else 11), 1e9
    return {"cores": [max(0, min(100, random.gauss(base, 22))) for _ in range(16)],
            "mem": (62.0, 19.8 * gb, 32 * gb), "swap": (3.0, 0.24 * gb, 8 * gb), "disk": (41.0, 410 * gb, 1000 * gb),
            "down": random.uniform(2e5, 4e6), "up": random.uniform(2e4, 6e5),
            "load": (base / 9, base / 11, base / 14), "host": "devbox", "uptime": 3 * 86400 + 5 * 3600}


# ── formatting ──────────────────────────────────────────────────────────────
def human(n, unit=""):
    for suffix in ("", "K", "M", "G", "T"):
        if abs(n) < 1000:
            return (f"{n:.0f}" if suffix == "" or n >= 100 else f"{n:.1f}") + suffix + unit
        n /= 1000
    return f"{n:.1f}P{unit}"


def dur(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h{sec % 3600 // 60:02d}m"
    return f"{sec // 86400}d{sec % 86400 // 3600}h"


def level(pct):
    return "$success" if pct < 60 else "$warning" if pct < 85 else "$error"


def bar(pct, width):
    full = round(max(0, min(100, pct)) / 100 * width)
    return f"[{level(pct)}]{'█' * full}[/][dim]{'░' * (width - full)}[/]"


# state -> symbol, label, Rich style (table cells), theme colour (markup)
STATES = {"needs": ("◆", "needs you", "bold red", "$error"), "working": ("●", "working", "green", "$success"),
          "done": ("✔", "done", "dodger_blue1", "$primary"), "idle": ("○", "idle", "grey50", "$text-muted")}


def ctx_pct(r):
    return 100 * r["ctx"] / r["win"] if r.get("ctx") and r.get("win") else None


def detail_text(r, d):
    """The body of the detail panel for one agent row `r` and its log details `d`."""
    now = time.time()
    sym, word, _, colour = STATES[r["state"]]
    message = (r.get("hook") or {}).get("message") if r["state"] == "needs" else None
    pct, tok = ctx_pct(r), r.get("tok")
    active = dur(now - r["act"]) + " ago" if r["act"] else "unknown"

    def field(label, text):
        return f"[b $accent]{label:<8}[/]{text}"
    lines = [
        field("STATE", f"[b {colour}]{sym} {word}[/]" + (f"  {escape(str(message))}" if message else "")),
        field("TASK", escape(r["title"] or "—")),
        field("MODEL", escape(str(r.get("model") or "—")) + f"   [b $accent]BRANCH[/] {escape(str(d.get('branch') or '—'))}"),
        field("PANE", escape(r["where"] or "not in a known pane")
              + f"   [dim]pid {r['pid']} · up {dur(now - r['started'])} · active {active}[/]"),
        field("FOLDER", escape(r["cwd"].replace(str(HOME), "~", 1))),
        field("CONTEXT", f"{bar(pct, 30)} {pct:.0f}%  [dim]{human(r['ctx'])} of {human(r['win'])}[/]"
              if pct is not None else "[dim]not available for this agent[/]"),
        field("TOKENS", f"[b]{human(sum(tok))}[/] this session  [dim]in {human(tok[0])} · out {human(tok[1])} · "
              f"cache read {human(tok[2])} · cache write {human(tok[3])}[/]" if tok else "[dim]not available for this agent[/]"),
    ]
    for label, key, limit in (("LAST PROMPT", "prompt", 500), ("LAST TOOL CALL", "tool", 200), ("LAST REPLY", "reply", 900)):
        text = " ".join(str(d.get(key) or "").split())
        lines += ["", f"[b $accent]{label}[/]", escape(text[:limit] + ("…" if len(text) > limit else "")) if text else "[dim]—[/]"]
    return "\n".join(lines + ["", "[dim]g go to pane · esc close[/]"])


def spark(values):
    return "".join(f"[{level(v)}]{BLOCKS[min(8, round(v / 100 * 8))]}[/]" for v in values)


# ── UI ──────────────────────────────────────────────────────────────────────
class Choice(Static, can_focus=True):
    """One settings row: left/right (or enter) steps through the options."""
    BINDINGS = [("left", "step(-1)"), ("right", "step(1)"), ("enter", "step(1)")]

    def __init__(self, label, options, value, **kw):
        super().__init__(**kw)
        self.label, self.options = label, options
        self.index = options.index(value) if value in options else 0

    @property
    def value(self):
        return self.options[self.index]

    def render(self):
        v = self.value
        text = ("on" if v else "off") if isinstance(v, bool) else f"{v}s" if isinstance(v, int) else v
        return f"{'▸' if self.has_focus else ' '} {self.label:<10}[dim]◀[/] [b]{text}[/] [dim]▶[/]"

    def on_focus(self):
        self.refresh()

    def on_blur(self):
        self.refresh()

    def action_step(self, delta):
        self.index = (self.index + delta) % len(self.options)
        self.refresh()
        self.screen.changed(self)


class Settings(ModalScreen):
    """Changes apply as you make them and are saved when the dialog closes."""
    BINDINGS = [("escape", "close", "Close"), ("up", "app.focus_previous"), ("down", "app.focus_next")]

    def compose(self):
        cfg = self.app.cfg
        with Vertical(id="dialog"):
            yield Label("[b]SETTINGS[/]")
            yield Choice("Refresh", [1, 2, 5, 10], cfg["refresh"], id="set-refresh")
            yield Choice("Theme", sorted(self.app.available_themes), self.app.theme, id="set-theme")
            yield Choice("Logo", [True, False], cfg["logo"], id="set-logo")
            yield Label("  Agent process names (comma separated)")
            yield Input(cfg["agents"], id="set-agents")
            yield Label("[dim]↑↓ move · ←→ or enter change · esc close and save[/]")

    def changed(self, choice):
        self.app.cfg[choice.id.removeprefix("set-")] = choice.value  # ids are prefixed: the dashboard has #logo and #agents
        self.app.apply_config()  # live preview: theme, logo and refresh rate take effect at once

    def action_close(self):
        self.app.cfg["agents"] = self.query_one("#set-agents", Input).value
        save_json(CONFIG, self.app.cfg)
        self.dismiss()


class Detail(ModalScreen):
    """Everything we know about one agent. Follows the agent by pid while open."""
    BINDINGS = [("escape", "close", "Close"), ("enter", "close", "Close"), ("g", "jump", "Go to pane")]

    def __init__(self, pid):
        super().__init__()
        self.pid = pid

    def compose(self):
        with VerticalScroll(id="detail"):
            yield Static(id="detail-body")

    def on_mount(self):
        self.show()
        self.set_interval(2, self.show)

    def row(self):
        return next((r for r in self.app.rows if r["pid"] == self.pid), None)

    def show(self):
        r = self.row()
        if not r:
            return self.query_one("#detail-body", Static).update("[dim]This agent has exited. esc to close.[/]")
        if self.app.demo:
            d = {**DEMO_DETAIL, "branch": r.get("branch")}
        else:
            d = read_detail(r["log"], r["name"]) if r["log"] else {}
        self.query_one("#detail").border_title = f"{r['name']} · {os.path.basename(r['cwd']) or r['cwd']}"
        self.query_one("#detail-body", Static).update(detail_text(r, d))

    def action_jump(self):
        self.app.jump_to(self.row())

    def action_close(self):
        self.dismiss()


class Plexonator(App):
    TITLE = "plexonator"
    BINDINGS = [("s", "settings", "Settings"), ("g", "jump", "Go to pane"), ("q", "quit", "Quit")]
    CSS = """
    #logo { width: 100%; height: auto; content-align: center middle; color: $accent; text-style: bold; }
    #top { height: 10; }
    #top.narrow { layout: vertical; height: 20; }
    #top.narrow Static { height: 10; }
    #sys, #tok { width: 1fr; height: 100%; border: round $primary; padding: 0 1; border-title-color: $accent; }
    #agents { height: 1fr; border: round $primary; border-title-color: $accent; }
    Settings, Detail { align: center middle; }
    #detail { width: 96; max-width: 96%; height: auto; max-height: 92%; border: round $accent;
              border-title-color: $accent; background: $surface; padding: 1 2; }
    #dialog { width: 64; height: auto; border: round $accent; background: $surface; padding: 1 2; }
    #dialog Label { margin-top: 1; }
    Choice { height: 1; margin-top: 1; }
    Choice:focus { background: $boost; color: $accent; }
    """

    def __init__(self, demo=None):
        super().__init__()
        self.demo = demo  # None, or a DEMO scenario name
        self.main = None  # the dashboard screen, set on mount
        self.cfg = {**DEFAULTS, **load_json(CONFIG, {})}
        cached = load_json(CACHE, {})  # "v" bumps when the per-file fields change, forcing a rescan
        self.tokens = demo_tokens() if demo else cached if cached.get("v") == 2 else {"v": 2}
        self.rows = []
        self.scan_status = "demo data" if demo else "scanning…"
        self.mux, self.procs, self.cpu_hist, self.net_prev = {}, {}, [], None
        if demo:  # a history to draw from the first frame
            self.cpu_hist = [sum(c := demo_system(demo)["cores"]) / len(c) for _ in range(200)]

    def compose(self):
        yield Static(LOGO, id="logo", markup=False)
        with Horizontal(id="top"):
            yield Static(id="sys")
            yield Static(id="tok")
        yield DataTable(id="agents", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self):
        self.main = self.screen
        self.main.query_one("#sys").border_title = "SYSTEM"
        self.main.query_one("#tok").border_title = "TOKENS"
        psutil.cpu_percent(None)
        self.timer = None
        self.apply_config()
        if not self.demo:
            threading.Thread(target=self.slow_loop, daemon=True).start()

    def apply_config(self):
        if self.cfg["theme"] in self.available_themes:
            self.theme = self.cfg["theme"]
        if self.timer:
            self.timer.stop()
        self.timer = self.set_interval(self.cfg["refresh"], self.tick)
        self.on_resize()

    def on_resize(self, event=None):
        if not self.main:
            return  # a resize can arrive before mount
        size = event.size if event else self.size  # self.size is still the old size during a resize
        self.main.query_one("#logo").display = self.cfg["logo"] and size.width >= 88 and size.height >= 30
        self.main.query_one("#top").set_class(size.width < 124, "narrow")  # side-pane widths: stack the boxes
        self.call_after_refresh(self.tick)  # bars are sized from the laid-out panel width

    def action_settings(self):
        self.push_screen(Settings(), lambda _: self.apply_config())

    def on_data_table_row_selected(self, event):
        if event.cursor_row < len(self.rows):
            self.push_screen(Detail(self.rows[event.cursor_row]["pid"]))

    def action_jump(self):
        row = self.main.query_one("#agents", DataTable).cursor_row
        self.jump_to(self.rows[row] if row < len(self.rows) else None)

    def jump_to(self, r):
        if not r:
            return
        if not r.get("target"):
            self.notify("No tmux or cmux pane is known for this agent.", severity="warning")
        elif not jump(r["target"]):
            self.notify(f"Could not focus {r['where']}.", severity="error")

    def slow_loop(self):
        """Background thread: multiplexer layout and token logs change slowly and cost more to read."""
        cmux_pids = {}
        while True:
            self.mux = mux_map(cmux_pids)
            for prov, (root, feed) in SOURCES.items():
                files = self.tokens.setdefault(prov, {})
                paths = list(root.rglob("*.jsonl")) if root.is_dir() else []
                for i, p in enumerate(paths):
                    st = files.setdefault(str(p), {"off": 0, "days": {}})
                    before = st["off"]
                    scan_file(p, st, feed)
                    if st["off"] != before:
                        self.scan_status = f"scanning {prov} {i + 1}/{len(paths)}"
                        if i % 25 == 0:
                            save_json(CACHE, self.tokens)  # first scan can be GBs: keep progress
            save_json(CACHE, self.tokens)
            self.scan_status = "live"
            time.sleep(10)

    # ── rendering ──
    def tick(self):
        self.render_system()
        self.render_tokens()
        self.render_agents()

    def read_system(self):
        mem, swap, disk = psutil.virtual_memory(), psutil.swap_memory(), psutil.disk_usage("/")
        net, now = psutil.net_io_counters(), time.time()
        down = up = 0.0
        if self.net_prev:
            dt = max(now - self.net_prev[0], 1e-6)
            down, up = (net.bytes_recv - self.net_prev[1]) / dt, (net.bytes_sent - self.net_prev[2]) / dt
        self.net_prev = (now, net.bytes_recv, net.bytes_sent)
        return {"cores": psutil.cpu_percent(None, percpu=True), "mem": (mem.percent, mem.used, mem.total),
                "swap": (swap.percent, swap.used, swap.total), "disk": (disk.percent, disk.used, disk.total),
                "down": down, "up": up, "load": os.getloadavg(), "host": socket.gethostname(),
                "uptime": now - psutil.boot_time()}

    def render_system(self):
        w = max(10, self.main.query_one("#sys").size.width - 24)
        s = demo_system(self.demo) if self.demo else self.read_system()
        cores, load, down, up = s["cores"], s["load"], s["down"], s["up"]
        cpu = sum(cores) / len(cores)
        self.cpu_hist = (self.cpu_hist + [cpu])[-300:]
        hist = self.cpu_hist[-w:]

        def row(label, pct, text):
            return f"[b $accent]{label}[/] {bar(pct, w)} {pct:3.0f}% [dim]{text}[/]"
        lines = [
            f"[b $accent]CPU [/] {spark(hist)}{' ' * (w - len(hist))} {cpu:3.0f}% [dim]{len(cores)} cores[/]",
            f"[b $accent]CORE[/] {spark(cores)}",
            *(row(f"{k.upper():<4}", s[k][0], f"{human(s[k][1], 'B')}/{human(s[k][2], 'B')}")
              for k in ("mem", "swap", "disk")),
            f"[b $accent]NET [/] [$success]↓[/] {human(down, 'B/s'):>9}  [$warning]↑[/] {human(up, 'B/s'):>9}"
            f"   [b $accent]LOAD[/] {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}",
            f"[b $accent]AGNT[/] {len(self.rows)} running · {sum(r['cpu'] for r in self.rows):.0f}% cpu · "
            f"{human(sum(r['mem'] for r in self.rows), 'B')}",
        ]
        box = self.main.query_one("#sys")
        box.update("\n".join(lines))
        box.border_subtitle = f"{s['host']} · up {dur(s['uptime'])} · {datetime.now():%H:%M:%S}"

    def render_tokens(self):
        today = date.today()
        spans = (("today", today.isoformat()), ("7 days", (today - timedelta(days=6)).isoformat()), ("all", ""))
        lines = [f"[dim]{'':15}{'input':>8}{'output':>8}{'cache rd':>9}{'cache wr':>9}{'TOTAL':>8}[/]"]
        for prov in SOURCES:
            files = self.tokens.get(prov, {})
            for i, (label, since) in enumerate(spans):
                t = totals(files, since)
                name = f"[b $accent]{prov.upper():<7}[/]" if i == 0 else " " * 7
                lines.append(f"{name} [dim]{label:<7}[/]{human(t[0]):>8}{human(t[1]):>8}{human(t[2]):>9}"
                             f"{human(t[3]):>9}[b]{human(sum(t)):>8}[/]")
        limits = codex_limits(self.tokens.get("codex", {}), time.time())
        if limits:
            lines.append("[b $accent]CODEX  [/] [dim]limits [/]" + "   ".join(
                f"[dim]{label}[/] {bar(used, 12)} {used:.0f}%" for label, used in limits))
        box = self.main.query_one("#tok")
        box.update("\n".join(lines))
        box.border_subtitle = self.scan_status

    def render_agents(self):
        names = [n.strip() for n in self.cfg["agents"].split(",") if n.strip()]
        self.rows = rows = (demo_agents(self.demo) if self.demo
                            else find_agents(names, self.procs, self.mux, self.tokens))
        narrow = self.size.width < 124
        task_w = 20 if narrow else max(16, self.size.width - 117)  # the task column takes the spare width
        table = self.main.query_one("#agents", DataTable)
        cursor = table.cursor_row
        table.clear(columns=True)  # columns too: their widths never shrink otherwise
        table.add_columns(*(("", "AGENT", "PROJECT", "TASK", "STATE", "CTX") if narrow else
                            ("", "AGENT", "PROJECT", "TASK", "STATE", "CTX", "TOKENS", "CPU", "MEM", "ACTIVE", "WHERE")))
        now = time.time()
        for r in rows:
            cwd = r["cwd"].replace(str(HOME), "~", 1)
            sym, word, style, _ = STATES[r["state"]]
            pct = ctx_pct(r)
            ctx_style = "" if pct is None else "red" if pct >= 85 else "yellow" if pct >= 60 else "green"
            cells = [
                Text(sym, style=style),
                Text(r["name"], style="bold"),
                Text(os.path.basename(cwd)[:14] if narrow else cwd if len(cwd) < 25 else "…" + cwd[-24:]),
                Text(r["title"][:task_w].ljust(task_w)),
                Text(word, style=style),
            ]
            if narrow:
                cells.append(Text("—" if pct is None else f"{pct:3.0f}%", style=ctx_style, justify="right"))
            else:
                cells += [
                    Text("—" if pct is None else f"{pct:3.0f}% {human(r['ctx']):>5}", style=ctx_style, justify="right"),
                    Text(human(sum(r["tok"])) if r.get("tok") else "—", justify="right"),
                    Text(f"{r['cpu']:4.0f}%", justify="right"),
                    Text(human(r["mem"], "B"), justify="right"),
                    Text(dur(now - r["act"]) + " ago" if r["act"] else "—", justify="right"),
                    Text(r["where"]),
                ]
            table.add_row(*cells)
        table.move_cursor(row=cursor)
        count = {k: sum(r["state"] == k for r in rows) for k in STATES}
        table.border_title = (f"AGENTS  {count['needs']} need you · " if count["needs"] else "AGENTS  ") + \
            f"{count['working']} working / {len(rows)}"
        table.border_subtitle = "enter details · g go to pane"


def selftest():
    ts = "2026-01-02T12:00:00Z"
    day = _day(ts)
    st = {"off": 0, "days": {}}
    msg = lambda mid, out: json.dumps({"timestamp": ts, "message": {"id": mid, "usage": {
        "input_tokens": 1, "output_tokens": out, "cache_read_input_tokens": 10,
        "cache_creation_input_tokens": 100}}}).encode()
    for line in (msg("a", 5), msg("a", 7), msg("b", 2), b'{"usage" broken', b'{"text": "usage"}'):
        feed_claude(line, st)
    assert st["days"][day] == [2, 9, 20, 200], st  # repeated id "a" counted once, at its last value

    st = {"off": 0, "days": {}}
    ev = lambda i, c, o: json.dumps({"timestamp": ts, "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o}}}}).encode()
    for line in (ev(100, 60, 5), ev(100, 60, 5), ev(300, 200, 9), b'{"token_count": null}'):
        feed_codex(line, st)
    assert st["days"][day] == [100, 9, 200, 0], st  # repeated event adds nothing
    assert totals({"f": st}, "2027") == [0, 0, 0, 0]

    names = ["claude", "codex"]
    assert agent_name("claude", ["/x/claude"], names) == "claude"
    assert agent_name("node", ["node", "/x/bin/codex"], names) == "codex"
    assert agent_name("vim", ["vim", "codex"], names) is None
    assert (human(999), human(1500), human(2_500_000), dur(59), dur(3700)) == ("999", "1.5K", "2.5M", "59s", "1h01m")
    selftest_agents()
    asyncio.run(selftest_settings())
    print("selftest ok")


def selftest_agents():
    """Hook events, the hook installer, state rules, and the detail reader, all on temp files."""
    global STATE
    tmp = Path(tempfile.mkdtemp())
    real, STATE = STATE, tmp / "state"
    try:
        import io
        send = lambda **d: hook(io.StringIO(json.dumps({"session_id": "s/1", **d})))
        send(hook_event_name="PermissionRequest", tool_name="Bash")
        assert load_json(STATE / "s1.json", {})["state"] == "needs"
        send(hook_event_name="Notification", message="Claude is waiting for your input")  # says nothing new
        assert load_json(STATE / "s1.json", {})["state"] == "needs"
        send(hook_event_name="Stop")
        assert load_json(STATE / "s1.json", {})["state"] == "done"
        send(hook_event_name="SessionEnd")
        assert not (STATE / "s1.json").exists()
        hook(io.StringIO("not json"))  # must not raise
    finally:
        STATE = real

    settings = tmp / "settings.json"
    other = {"hooks": [{"type": "command", "command": "echo hi"}]}
    settings.write_text(json.dumps({"model": "x", "hooks": {"Stop": [other]}}))
    install_hooks(settings)
    install_hooks(settings)  # twice: must not duplicate
    data = json.loads(settings.read_text())
    assert data["hooks"]["Stop"][0] == other and len(data["hooks"]["Stop"]) == 2, data["hooks"]["Stop"]
    assert set(data["hooks"]) == set(HOOK_EVENTS) and data["model"] == "x"
    install_hooks(settings, remove=True)
    assert json.loads(settings.read_text()) == {"model": "x", "hooks": {"Stop": [other]}}
    assert json.loads((tmp / "settings.json.plexonator-backup").read_text())["hooks"] == {"Stop": [other]}

    now = 1000.0
    assert agent_state(0, 900, {"state": "needs", "ts": 950}, now) == "needs"
    assert agent_state(0, 990, {"state": "needs", "ts": 950}, now) == "working"  # it moved on since asking
    assert agent_state(0, 900, {"state": "done", "ts": 950}, now) == "done"
    assert agent_state(0, 900, {"state": "working", "ts": 950}, now) == "working"  # quiet, but mid-turn
    assert agent_state(0, 100, {"state": "working", "ts": 100}, now) == "idle"  # stale: interrupted
    assert agent_state(0, 100, None, now) == "idle" and agent_state(50, None, None, now) == "working"

    log = tmp / "session.jsonl"
    log.write_text("\n".join(json.dumps(d) for d in (
        {"type": "assistant", "gitBranch": "main", "message": {"content": [{"type": "text", "text": "old reply"}]}},
        {"type": "last-prompt", "lastPrompt": "fix the bug"},
        {"type": "assistant", "gitBranch": "dev", "message": {"content": [
            {"type": "text", "text": "new reply"}, {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}]}},
    )) + "\nbroken line\n")
    assert read_detail(log, "claude") == {"branch": "dev", "tool": "Edit a.py", "reply": "new reply", "prompt": "fix the bug"}
    log.write_text(json.dumps({"payload": {"type": "function_call", "name": "shell", "arguments": '{"cmd": "ls"}'}}) + "\n")
    assert read_detail(log, "codex") == {"tool": "shell ls"}
    assert read_detail(tmp / "missing", "claude") == {}

    files = {"a": {"limits_ts": "1", "limits": {"primary": {"used_percent": 5, "window_minutes": 300, "resets_at": 2000}}},
             "b": {"limits_ts": "2", "limits": {"primary": {"used_percent": 22, "window_minutes": 300, "resets_at": 2000},
                                                "secondary": {"used_percent": 9, "window_minutes": 10080, "resets_at": 500}}}}
    assert codex_limits(files, now) == [("5h", 22.0)]  # newest reading; the reset window is dropped
    assert ctx_pct({"ctx": 50_000, "win": 200_000}) == 25 and ctx_pct({"ctx": None, "win": 200_000}) is None


async def selftest_settings():
    """Arrow keys drive the dialog, the theme previews live, and closing saves."""
    global CONFIG
    real, CONFIG = CONFIG, Path(tempfile.mkdtemp()) / "config.json"  # never touch the user's config
    try:
        app = Plexonator(demo="calm")
        app.cfg = dict(DEFAULTS)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.press("s", "down")  # first row is Refresh, second is Theme
            before = app.theme
            await pilot.press("right")
            assert app.theme != before and app.cfg["theme"] == app.theme, "theme should preview live"
            await pilot.press("up", "left")  # back to Refresh: 2s -> 1s
            assert app.cfg["refresh"] == 1, app.cfg
            app.tick()  # the dashboard must keep drawing underneath the dialog
            await pilot.press("escape")
            assert load_json(CONFIG, {})["theme"] == app.theme
            await pilot.press("enter")  # the table row opens the detail panel
            assert isinstance(app.screen, Detail), app.screen
            app.tick()
            await pilot.press("g")  # demo rows have no pane: must warn, not crash
            await pilot.press("escape")
            assert not isinstance(app.screen, Detail)
    finally:
        CONFIG = real


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--install-hooks" in sys.argv or "--uninstall-hooks" in sys.argv:
        removing = "--uninstall-hooks" in sys.argv
        backup = install_hooks(CLAUDE_SETTINGS, remove=removing)
        print(f"{'Removed plexonator hooks from' if removing else 'Added plexonator hooks to'} {CLAUDE_SETTINGS}")
        print(f"Your settings from before the first install are kept in {backup}")
        print("Agents started from now on report their status." if not removing else "")
    elif sys.platform != "linux":
        sys.exit("plexonator supports Linux only for now")
    else:
        demo = next((a.partition("=")[2] or "busy" for a in sys.argv if a.startswith("--demo")), None)
        if demo and demo not in DEMO:
            sys.exit(f"unknown demo: {demo} (choose from {', '.join(DEMO)})")
        Plexonator(demo).run()
