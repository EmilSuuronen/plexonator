#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["textual>=3.0", "psutil>=5.9"]
# ///
"""plexonator: one-pane dashboard for AI coding agents and system resources (Linux).

Run:   ./plexonator.py            (needs uv; deps install themselves)
Demo:  ./plexonator.py --demo     (fake data, or --demo=calm)
Test:  ./plexonator.py --selftest
"""
import asyncio
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import psutil
from rich.text import Text
from textual.app import App
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Label, Static

HOME = Path.home()
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "plexonator" / "config.json"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "plexonator" / "tokens.json"
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


# ── config ──────────────────────────────────────────────────────────────────
def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)  # atomic: a kill mid-write never leaves a broken file


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


def feed_codex(line, st):
    if b'"token_count"' not in line:
        return
    try:
        d = json.loads(line)
        p = d["payload"]
        u = p["info"]["total_token_usage"]
        cached = int(u.get("cached_input_tokens") or 0)
        tot = [int(u.get("input_tokens") or 0) - cached, int(u.get("output_tokens") or 0), cached, 0]
        day = _day(d["timestamp"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return
    prev = st.get("tot", [0, 0, 0, 0])  # running totals: deltas ignore repeated events
    _add(st["days"], day, [max(0, a - b) for a, b in zip(tot, prev)])
    st["tot"] = tot
    if p.get("rate_limits"):
        st["limits"] = p["rate_limits"]


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
    """shell pid -> {"where", "title", "sid"} for tmux panes and cmux agent terminals."""
    out = {}
    if shutil.which("tmux"):
        fmt = "#{pane_pid}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_title}"
        for row in _run("tmux", "list-panes", "-a", "-F", fmt).splitlines():
            pid, where, title = (row.split("\t") + ["", ""])[:3]
            if pid.isdigit():
                out[int(pid)] = {"where": "tmux " + where, "title": title, "sid": None}
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
                                   "sid": extra.get("agent_session_id")}
    return out


# ── agents ──────────────────────────────────────────────────────────────────
def agent_name(name, cmdline, names):
    """Which configured agent a process is, or None. Only looks past argv[0] for script runtimes."""
    cands = [name] + [os.path.basename(c) for c in cmdline[:2 if name in RUNTIMES else 1]]
    return next((n for n in names if n in cands), None)


def last_activity(name, cwd, sid):
    """mtime of the agent's session log, or None when we cannot find it."""
    if name != "claude":
        return None  # shortcut: only Claude logs are mapped to processes; add Codex when needed
    root = HOME / ".claude" / "projects"
    # shortcut: without a session id (non-cmux), two Claudes in one folder share the newest log
    files = root.glob(f"*/{sid}.jsonl") if sid else (root / re.sub(r"[^A-Za-z0-9]", "-", cwd)).glob("*.jsonl")
    try:
        return max((f.stat().st_mtime for f in files), default=None)
    except OSError:
        return None


def find_agents(names, procs, mux):
    """procs: pid -> Process, kept between calls so cpu_percent has a baseline."""
    found = {}
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        hit = agent_name(p.info["name"] or "", p.info["cmdline"] or [], names)
        if hit:
            found[p.info["pid"]] = (hit, p)
    rows = []
    for pid, (name, p) in found.items():
        if p.info["ppid"] in found:
            continue  # wrapper + child process: show one row
        q = procs.setdefault(pid, p)
        try:
            with q.oneshot():
                cpu, mem, cwd, started = q.cpu_percent(None), q.memory_info().rss, q.cwd(), q.create_time()
            pane = next((mux[a.pid] for a in q.parents() if a.pid in mux), {})
        except psutil.Error:
            continue
        act = last_activity(name, cwd, pane.get("sid"))
        working = cpu > 10 or (act is not None and time.time() - act < 15)
        rows.append({"name": name, "pid": pid, "cpu": cpu, "mem": mem, "cwd": cwd, "started": started,
                     "act": act, "working": working, "where": pane.get("where", ""),
                     "title": pane.get("title", "")})
    for pid in set(procs) - set(found):
        del procs[pid]
    rows.sort(key=lambda r: (not r["working"], -(r["act"] or 0)))
    return rows


# ── demo mode ───────────────────────────────────────────────────────────────
# Fake data for screenshots and for trying the UI: --demo reads nothing from this machine.
# Row: agent, folder, task, pane, cpu %, memory MB, uptime minutes, seconds idle (0 = working).
DEMO = {
    "busy": [
        ("claude", "~/code/orbital-api", "Refactor auth middleware", "tmux dev:1.0", 34, 512, 184, 0),
        ("codex", "~/code/orbital-api", "Fix flaky checkout test", "tmux dev:1.1", 27, 318, 96, 0),
        ("claude", "~/code/pixel-forge", "Port shaders to WGSL", "tmux dev:2.0", 41, 604, 301, 0),
        ("gemini", "~/code/tidepool-web", "Migrate router to v7", "tmux web:1.0", 19, 287, 47, 0),
        ("claude", "~/code/kiln", "Profile incremental build", "tmux dev:3.0", 22, 455, 122, 0),
        ("aider", "~/code/lantern", "Add rate limiter", "tmux web:1.1", 2, 196, 58, 95),
        ("codex", "~/code/pixel-forge", "Review PR #482", "tmux dev:2.1", 1, 301, 240, 410),
        ("claude", "~/code/tidepool-web", "Write release notes", "tmux web:2.0", 0, 338, 512, 1900),
        ("opencode", "~/code/kiln", "Bisect cache regression", "tmux dev:3.1", 0, 244, 75, 3300),
    ],
    "calm": [
        ("claude", "~/code/lantern", "Add rate limiter", "tmux main:1.0", 18, 402, 35, 0),
        ("codex", "~/code/lantern", "Update API docs", "tmux main:1.1", 1, 290, 35, 240),
        ("claude", "~/code/kiln", "Waiting for review", "tmux main:2.0", 0, 311, 610, 5400),
    ],
}


def demo_agents(scenario):
    now, rows = time.time(), []
    for i, (name, cwd, title, where, cpu, mem, up, idle) in enumerate(DEMO[scenario]):
        rows.append({"name": name, "pid": 4000 + i, "cpu": cpu + random.uniform(0, 5) * (idle == 0),
                     "mem": mem * 1e6, "cwd": cwd, "started": now - up * 60, "act": now - idle - 2,
                     "working": idle == 0, "where": where, "title": title})
    return rows


def demo_tokens():
    today = date.today().isoformat()
    week = (date.today() - timedelta(days=3)).isoformat()

    def days(scale):
        return {"demo": {"days": {today: [int(v * scale) for v in (41_000, 1_900_000, 420_000_000, 7_800_000)],
                                  week: [int(v * scale) for v in (212_000, 8_300_000, 1_700_000_000, 36_000_000)],
                                  "2000-01-01": [int(v * scale) for v in (3_100_000, 96_000_000, 21e9, 410_000_000)]}}}
    return {"claude": days(1), "codex": days(0.45)}


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


class Plexonator(App):
    TITLE = "plexonator"
    BINDINGS = [("s", "settings", "Settings"), ("q", "quit", "Quit")]
    CSS = """
    #logo { width: 100%; height: auto; content-align: center middle; color: $accent; text-style: bold; }
    #top { height: 9; }
    #top.narrow { layout: vertical; height: 18; }
    #top.narrow Static { height: 9; }
    #sys, #tok { width: 1fr; height: 100%; border: round $primary; padding: 0 1; border-title-color: $accent; }
    #agents { height: 1fr; border: round $primary; border-title-color: $accent; }
    Settings { align: center middle; }
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
        self.tokens = demo_tokens() if demo else load_json(CACHE, {})
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
        box = self.main.query_one("#tok")
        box.update("\n".join(lines))
        box.border_subtitle = self.scan_status

    def render_agents(self):
        names = [n.strip() for n in self.cfg["agents"].split(",") if n.strip()]
        rows = demo_agents(self.demo) if self.demo else find_agents(names, self.procs, self.mux)
        narrow = self.size.width < 124
        task_w = 24 if narrow else max(24, self.size.width - 100)  # the task column takes the spare width
        table = self.main.query_one("#agents", DataTable)
        cursor = table.cursor_row
        table.clear(columns=True)  # columns too: their widths never shrink otherwise
        table.add_columns("", "AGENT", "PROJECT", "TASK", "WHERE", "CPU", "MEM", "UP", "ACTIVE")
        now = time.time()
        for r in rows:
            cwd = r["cwd"].replace(str(HOME), "~", 1)
            table.add_row(
                Text("●", style="green" if r["working"] else "grey50"),
                Text(r["name"], style="bold"),
                Text(os.path.basename(cwd) if narrow else cwd if len(cwd) < 32 else "…" + cwd[-31:]),
                Text(r["title"][:task_w].ljust(task_w)),
                Text(r["where"]),
                Text(f"{r['cpu']:4.0f}%", justify="right"),
                Text(human(r["mem"], "B"), justify="right"),
                Text(dur(now - r["started"]), justify="right"),
                Text(dur(now - r["act"]) + " ago" if r["act"] else "—", justify="right"),
            )
        table.move_cursor(row=cursor)
        table.border_title = f"AGENTS  {sum(r['working'] for r in rows)} working / {len(rows)}"


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
    asyncio.run(selftest_settings())
    print("selftest ok")


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
    finally:
        CONFIG = real


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif sys.platform != "linux":
        sys.exit("plexonator supports Linux only for now")
    else:
        demo = next((a.partition("=")[2] or "busy" for a in sys.argv if a.startswith("--demo")), None)
        if demo and demo not in DEMO:
            sys.exit(f"unknown demo: {demo} (choose from {', '.join(DEMO)})")
        Plexonator(demo).run()
