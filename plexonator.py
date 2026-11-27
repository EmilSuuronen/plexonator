#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["textual>=3.0", "psutil>=5.9"]
# ///
"""plexonator: one-pane dashboard for AI coding agents and system resources (Linux).

Run:   ./plexonator.py            (needs uv; deps install themselves)
Test:  ./plexonator.py --selftest
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import psutil
from rich.text import Text
from textual.app import App
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, Select, Static, Switch

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
class Settings(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Cancel")]

    def compose(self):
        cfg = self.app.cfg
        with Vertical(id="dialog"):
            yield Label("[b]SETTINGS[/]  [dim]saved to " + str(CONFIG).replace(str(HOME), "~") + "[/]")
            yield Label("Refresh interval")
            yield Select([(f"{n}s", n) for n in (1, 2, 5, 10)], value=cfg["refresh"], allow_blank=False, id="refresh")
            yield Label("Theme")
            yield Select([(t, t) for t in sorted(self.app.available_themes)], value=self.app.theme,
                         allow_blank=False, id="theme")
            yield Label("Show logo")
            yield Switch(cfg["logo"], id="logo-on")
            yield Label("Agent process names (comma separated)")
            yield Input(cfg["agents"], id="agent-names")
            with Horizontal(id="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event):
        if event.button.id != "save":
            return self.dismiss(None)
        self.dismiss({"refresh": self.query_one("#refresh", Select).value,
                      "theme": self.query_one("#theme", Select).value,
                      "logo": self.query_one("#logo-on", Switch).value,
                      "agents": self.query_one("#agent-names", Input).value})


class Plexonator(App):
    TITLE = "plexonator"
    BINDINGS = [("s", "settings", "Settings"), ("q", "quit", "Quit")]
    CSS = """
    #logo { width: 100%; height: auto; content-align: center middle; color: $accent; text-style: bold; }
    #top { height: 9; }
    #sys, #tok { width: 1fr; height: 100%; border: round $primary; padding: 0 1; border-title-color: $accent; }
    #agents { height: 1fr; border: round $primary; border-title-color: $accent; }
    Settings { align: center middle; }
    #dialog { width: 64; height: auto; border: round $accent; background: $surface; padding: 1 2; }
    #dialog Label { margin-top: 1; }
    #buttons { height: auto; margin-top: 1; }
    #buttons Button { margin-right: 2; }
    """

    def __init__(self):
        super().__init__()
        self.cfg = {**DEFAULTS, **load_json(CONFIG, {})}
        self.tokens = load_json(CACHE, {})
        self.scan_status = "scanning…"
        self.mux, self.procs, self.cpu_hist, self.net_prev = {}, {}, [], None

    def compose(self):
        yield Static(LOGO, id="logo", markup=False)
        with Horizontal(id="top"):
            yield Static(id="sys")
            yield Static(id="tok")
        yield DataTable(id="agents", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self):
        self.query_one("#sys").border_title = "SYSTEM"
        self.query_one("#tok").border_title = "TOKENS"
        self.query_one("#agents", DataTable).add_columns(
            "", "AGENT", "PROJECT", "TASK", "WHERE", "CPU", "MEM", "UP", "ACTIVE")
        psutil.cpu_percent(None)
        self.timer = None
        self.apply_config()
        threading.Thread(target=self.slow_loop, daemon=True).start()

    def apply_config(self):
        if self.cfg["theme"] in self.available_themes:
            self.theme = self.cfg["theme"]
        if self.timer:
            self.timer.stop()
        self.timer = self.set_interval(self.cfg["refresh"], self.tick)
        self.on_resize()
        self.tick()

    def on_resize(self, event=None):
        self.query_one("#logo").display = self.cfg["logo"] and self.size.width >= 88 and self.size.height >= 30

    def action_settings(self):
        def done(new):
            if new:
                self.cfg.update(new)
                save_json(CONFIG, self.cfg)
                self.apply_config()
        self.push_screen(Settings(), done)

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

    def render_system(self):
        w = max(10, self.query_one("#sys").size.width - 24)
        cores = psutil.cpu_percent(None, percpu=True)
        cpu = sum(cores) / len(cores)
        self.cpu_hist = (self.cpu_hist + [cpu])[-w:]
        mem, swap, disk = psutil.virtual_memory(), psutil.swap_memory(), psutil.disk_usage("/")
        net, now = psutil.net_io_counters(), time.time()
        down = up = 0.0
        if self.net_prev:
            dt = max(now - self.net_prev[0], 1e-6)
            down, up = (net.bytes_recv - self.net_prev[1]) / dt, (net.bytes_sent - self.net_prev[2]) / dt
        self.net_prev = (now, net.bytes_recv, net.bytes_sent)
        load = os.getloadavg()

        def row(label, pct, text):
            return f"[b $accent]{label}[/] {bar(pct, w)} {pct:3.0f}% [dim]{text}[/]"
        lines = [
            f"[b $accent]CPU [/] {spark(self.cpu_hist)}{' ' * (w - len(self.cpu_hist))} {cpu:3.0f}% [dim]{len(cores)} cores[/]",
            f"[b $accent]CORE[/] {spark(cores)}",
            row("MEM ", mem.percent, f"{human(mem.used, 'B')}/{human(mem.total, 'B')}"),
            row("SWAP", swap.percent, f"{human(swap.used, 'B')}/{human(swap.total, 'B')}"),
            row("DISK", disk.percent, f"{human(disk.used, 'B')}/{human(disk.total, 'B')}"),
            f"[b $accent]NET [/] [$success]↓[/] {human(down, 'B/s'):>9}  [$warning]↑[/] {human(up, 'B/s'):>9}"
            f"   [b $accent]LOAD[/] {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}",
        ]
        box = self.query_one("#sys")
        box.update("\n".join(lines))
        box.border_subtitle = f"{socket.gethostname()} · up {dur(time.time() - psutil.boot_time())} · {datetime.now():%H:%M:%S}"

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
        box = self.query_one("#tok")
        box.update("\n".join(lines))
        box.border_subtitle = self.scan_status

    def render_agents(self):
        names = [n.strip() for n in self.cfg["agents"].split(",") if n.strip()]
        rows = find_agents(names, self.procs, self.mux)
        table = self.query_one("#agents", DataTable)
        cursor = table.cursor_row
        table.clear()
        now = time.time()
        for r in rows:
            cwd = r["cwd"].replace(str(HOME), "~", 1)
            table.add_row(
                Text("●", style="green" if r["working"] else "grey50"),
                Text(r["name"], style="bold"),
                Text(cwd if len(cwd) < 32 else "…" + cwd[-31:]),
                Text(r["title"][:48]),
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
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif sys.platform != "linux":
        sys.exit("plexonator supports Linux only for now")
    else:
        Plexonator().run()
