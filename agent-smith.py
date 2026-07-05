#!/usr/bin/env python3
"""
Agent Smith - a read-only terminal dashboard.

Four panels, refreshed on a timer:
  1. Usage      - your Claude usage limits (same numbers as the settings bar),
                  fetched live from the Anthropic usage endpoint with your own
                  OAuth token (read-only; the request only asks for your usage).
  2. Agents     - every Claude Code background job, read from
                  ~/.claude/jobs/<id>/state.json
  3. SLURM      - your `squeue --me` jobs: ones placed on nodes are listed with
                  the nodes they're on; pending jobs collapse to an in-queue count
  4. Node       - htop-style CPU / memory / load / GPU + top processes for
                  whatever compute node this is running on.

Pure stdlib (curses). No pip installs. Works on Python 3.6+.

Controls:  q quit   r force-refresh   (resizes automatically)
"""

import curses
import glob
import json
import locale
import os
import pwd
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

locale.setlocale(locale.LC_ALL, "")

HOME = os.path.expanduser("~")
# Claude Code keeps its data in ~/.claude unless CLAUDE_CONFIG_DIR overrides it,
# so resolve it the same way Claude does -- this makes the tool work for any
# user (their own home/config) without edits.
CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
JOBS_DIR = os.path.join(CONFIG_DIR, "jobs")
PROJECTS_DIR = os.path.join(CONFIG_DIR, "projects")   # per-session transcripts
CRED_PATH = os.path.join(CONFIG_DIR, ".credentials.json")

REFRESH = 2.0            # seconds between UI redraws / local data samples
USAGE_REFRESH = 120.0    # seconds between (slow, networked) usage fetches
USAGE_BACKOFF = 300.0    # seconds to wait after the usage endpoint rate-limits us
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE_KB = (os.sysconf("SC_PAGE_SIZE") // 1024) if hasattr(os, "sysconf") else 4
NCPU = os.cpu_count() or 1
CUR_JOB = os.path.basename(os.environ.get("CLAUDE_JOB_DIR", "").rstrip("/")) or None

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)  # sort fallback for jobs

# Bar glyphs. These require a Unicode-capable terminal + font; on a misconfigured
# terminal they may show as "?" (the rest of the dashboard still works).
BLOCK_FULL = "█"   # full block
BLOCK_LIGHT = "░"  # light shade
PACE_GLYPH = "╎"   # dashed vertical tick: marks how far the clock is into a window

# Length of each limit's rolling window, keyed by the `kind` the usage endpoint
# reports. Used to place the "pace" marker on a bar -- i.e. what fraction of the
# window's time has elapsed. A fill that runs ahead of this marker means you're
# spending faster than the clock and will hit the cap before it resets.
WINDOW_SECONDS = {
    "session": 5 * 3600,          # rolling 5-hour session limit
    "weekly_all": 7 * 86400,      # 7-day limits
    "weekly_scoped": 7 * 86400,
    "weekly_opus": 7 * 86400,
}

# "Theoretical cost" of Claude token usage, shown in the AGENTS panel. You're on
# a subscription and are NOT billed per token -- this is a "what would this cost
# at pay-as-you-go API rates" figure, for awareness only.
#
# We deliberately do NOT use the single rolled-up token count in state.json: that
# field is a context-window snapshot (it climbs as the conversation fills and
# DROPS on every compaction / restart-from-summary), not a lifetime total.
# Instead we sum each turn's usage block from the session transcript, which
# carries the full split -- fresh input, cache read, cache write, output -- and
# price each at its own rate. Cache reads are ~10x cheaper than fresh input and
# usually dominate the token count, so pricing the split is what makes this
# honest rather than off by an order of magnitude.
COST_OPUS_IN = 5.0      # $ / 1M fresh input tokens   (Opus 4.8)
COST_OPUS_OUT = 25.0    # $ / 1M output tokens        (Opus 4.8)
COST_CACHE_READ = 0.5   # $ / 1M cache-read tokens    (~0.1x input)
COST_CACHE_WRITE = 6.25 # $ / 1M cache-write tokens   (~1.25x input, 5-min TTL)
# Fallback only, for a job whose transcript can't be read: one blended rate on
# the state.json snapshot. Directional -- the transcript path above is preferred.
COST_IN_FRAC = 0.8
COST_PER_MTOK = COST_IN_FRAC * COST_OPUS_IN + (1 - COST_IN_FRAC) * COST_OPUS_OUT  # ~$9/1M

# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

def parse_iso(s):
    """Parse the ISO timestamps Claude / the API hand back, on Python 3.6
    (no datetime.fromisoformat, and %z doesn't take a ':' in the offset)."""
    if not s:
        return None
    s = s.strip().replace("Z", "+0000")
    # turn +00:00 into +0000 so 3.6's %z accepts it
    s = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", s)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def human_delta(seconds):
    seconds = int(seconds)
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return "%s%dd %dh" % (sign, d, h)
    if h:
        return "%s%dh %dm" % (sign, h, m)
    if m:
        return "%s%dm" % (sign, m)
    return "%s%ds" % (sign, s)


def fmt_dollars(d):
    """Format a dollar amount as a rough estimate: '~$2.1' / '~$62'.
    '' for None (nothing to show)."""
    if d is None:
        return ""
    return "~$%.1f" % d if d < 10 else "~$%d" % round(d)


def fmt_cost(tokens):
    """Fallback estimate from a bare token count via the blended COST_PER_MTOK
    (used only when a job's transcript can't be read for the exact split)."""
    if not isinstance(tokens, int):
        return ""
    return fmt_dollars(tokens / 1e6 * COST_PER_MTOK)


def short_model(mid):
    """Compact a model id for the table: 'claude-opus-4-8' -> 'opus-4.8',
    'claude-haiku-4-5-20251001' -> 'haiku-4.5', 'claude-sonnet-5' -> 'sonnet-5'.
    Unknown ids just lose the 'claude-' prefix and any trailing date stamp."""
    if not mid:
        return ""
    s = mid[len("claude-"):] if mid.startswith("claude-") else mid
    s = re.sub(r"-\d{6,}$", "", s)          # drop a trailing -20251001 style stamp
    parts = s.split("-")
    if len(parts) > 1:                      # family-a-b -> family-a.b
        return parts[0] + "-" + ".".join(parts[1:])
    return parts[0]


def human_tokens(tokens):
    """Compact token count: 1_370_000 -> '1.4M', 230_000 -> '230k', else int."""
    if not isinstance(tokens, int):
        return "-"
    if tokens >= 1000000:
        return "%.1fM" % (tokens / 1e6)
    if tokens >= 1000:
        return "%dk" % (tokens // 1000)
    return str(tokens)


def resets_in(iso):
    dt = parse_iso(iso)
    if not dt:
        return ""
    return human_delta((dt - datetime.now(timezone.utc)).total_seconds())


def ago(iso):
    dt = parse_iso(iso)
    if not dt:
        return "?"
    return human_delta((datetime.now(timezone.utc) - dt).total_seconds()) + " ago"


def seconds_until(iso):
    """Seconds from now until ISO timestamp `iso`; negative if it's in the past,
    None if the timestamp can't be parsed."""
    dt = parse_iso(iso)
    if not dt:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds()


def pace_fraction(lim):
    """Fraction (0.0-1.0) of a limit's window that has elapsed, or None when the
    window length or reset time is unknown. `resets_at` is the end of the window,
    so elapsed = window - time_remaining. Compare against the bar's fill: a fill
    above this fraction means usage is outrunning the clock."""
    window = WINDOW_SECONDS.get(lim.get("kind", ""))
    if not window:
        return None
    remaining = seconds_until(lim.get("resets_at"))
    if remaining is None:
        return None
    return max(0.0, min(1.0, (window - remaining) / float(window)))


# ---------------------------------------------------------------------------
# usage (networked, runs on a background thread so it never blocks the UI)
# ---------------------------------------------------------------------------

class Usage(object):
    """Fetches Claude usage limits on a background thread and caches the
    latest good value. The network call lives here (off the UI thread) so a
    slow or rate-limited request never freezes the dashboard."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = None        # parsed JSON of the most recent good fetch
        self.error = "fetching..."
        self.fetched_at = 0.0
        self._stop = threading.Event()

    def _read_token(self):
        with open(CRED_PATH) as f:
            return json.load(f)["claudeAiOauth"]["accessToken"]

    def _fetch_once(self):
        """Return (data, None) on success or (None, error_str) on failure.
        Uses urllib (stdlib) rather than the curl CLI so the bearer token is
        only ever held in process memory -- it never appears in a process's
        argv / /proc/<pid>/cmdline, which is world-readable on a shared node."""
        try:
            token = self._read_token()
        except Exception as e:
            return None, "no token (%s)" % e.__class__.__name__
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
        })
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # The endpoint returns a JSON body even for HTTP errors (e.g. a 429
            # rate limit), so fall through and parse it below.
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                return None, "http %s" % e.code
        except Exception:
            return None, "network error"
        try:
            obj = json.loads(body)
        except Exception:
            snippet = (body or "").strip()[:60]
            return None, "bad response: %s" % (snippet or "empty")
        # Application-level errors arrive as {"error": {"type": ..., ...}}.
        if isinstance(obj, dict) and "error" in obj:
            etype = (obj["error"] or {}).get("type", "error")
            if etype == "rate_limit_error":
                return None, "rate limited"
            return None, etype.replace("_", " ")
        return obj, None

    def loop(self):
        """Background loop: fetch, cache, sleep, repeat until stopped."""
        while not self._stop.is_set():
            data, err = self._fetch_once()
            with self.lock:
                if data is not None:
                    self.data, self.error = data, None   # fresh good data
                else:
                    self.error = err                     # keep last good self.data
                self.fetched_at = time.time()
            # Back off harder when the usage endpoint itself rate-limits us.
            # Event.wait returns immediately once stop() is called.
            delay = USAGE_BACKOFF if (err and "rate" in err.lower()) else USAGE_REFRESH
            if self._stop.wait(delay):
                return

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self.lock:
            return self.data, self.error, self.fetched_at


# ---------------------------------------------------------------------------
# claude agents / background jobs
# ---------------------------------------------------------------------------

# Per-session cost accounting. The single token count in state.json is a
# context-window snapshot, not a lifetime total, so we sum each turn's usage
# block from the session transcript (full input/output/cache split) and price
# each category correctly. Transcripts are append-only JSONL and can be tens of
# MB, so we cache per file and parse only the bytes appended since the last poll
# -- after the first read, an idle/finished job costs one os.stat() per refresh.
_cost_cache = {}    # transcript path -> {off, inp, cr, cc, out, cmp}
_session_path = {}  # sessionId -> transcript path (resolved once)


def _transcript_path(session_id):
    """Locate a session's transcript JSONL under ~/.claude/projects/*/."""
    if not session_id:
        return None
    if session_id in _session_path:
        return _session_path[session_id]
    hit = None
    for p in glob.glob(os.path.join(PROJECTS_DIR, "*", session_id + ".jsonl")):
        hit = p
        break
    if hit:                   # cache hits only; keep retrying misses (file may appear)
        _session_path[session_id] = hit
    return hit


def agent_usage(session_id):
    """Incrementally sum a session's transcript usage. Returns a dict of lifetime
    {tokens, cost, compactions}, or None if no transcript is readable."""
    path = _transcript_path(session_id)
    if not path:
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    c = _cost_cache.get(path)
    if c is None or size < c["off"]:      # new file, or truncated/replaced -> reset
        c = {"off": 0, "inp": 0, "cr": 0, "cc": 0, "out": 0, "cmp": 0, "model": None}
        _cost_cache[path] = c
    if size > c["off"]:
        try:
            with open(path, "rb") as f:
                f.seek(c["off"])
                data = f.read()
        except OSError:
            data = b""
        nl = data.rfind(b"\n")            # consume only whole lines; keep the tail
        if nl >= 0:
            chunk = data[:nl + 1]
            c["off"] += len(chunk)
            for line in chunk.split(b"\n"):
                # cheap pre-filter: skip the many lines that carry no usage/marker
                if b'"usage"' not in line and b'"isCompactSummary"' not in line:
                    continue
                try:
                    e = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                if e.get("isCompactSummary"):
                    c["cmp"] += 1
                msg = e.get("message")
                if isinstance(msg, dict):
                    mdl = msg.get("model")      # latest real model = current model
                    if mdl and mdl != "<synthetic>":
                        c["model"] = mdl
                u = msg.get("usage") if isinstance(msg, dict) else None
                if not isinstance(u, dict):
                    continue
                c["inp"] += u.get("input_tokens") or 0
                c["cr"] += u.get("cache_read_input_tokens") or 0
                c["cc"] += u.get("cache_creation_input_tokens") or 0
                c["out"] += u.get("output_tokens") or 0
    cost = (c["inp"] / 1e6 * COST_OPUS_IN + c["cr"] / 1e6 * COST_CACHE_READ +
            c["cc"] / 1e6 * COST_CACHE_WRITE + c["out"] / 1e6 * COST_OPUS_OUT)
    return {"tokens": c["inp"] + c["cr"] + c["cc"] + c["out"],
            "cost": cost, "compactions": c["cmp"], "model": c["model"]}


def get_jobs():
    jobs = []
    for path in glob.glob(os.path.join(JOBS_DIR, "*", "state.json")):
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            continue
        d["_id"] = os.path.basename(os.path.dirname(path))
        u = agent_usage(d.get("sessionId") or d.get("resumeSessionId"))
        if u:
            d["_life_tokens"] = u["tokens"]
            d["_cost"] = u["cost"]
            d["_compactions"] = u["compactions"]
            if u["model"]:
                d["_model"] = u["model"]
        jobs.append(d)

    # Most-recently-updated first; jobs with no/invalid updatedAt sort last.
    def sortkey(j):
        return parse_iso(j.get("updatedAt")) or EPOCH_UTC

    jobs.sort(key=sortkey, reverse=True)
    return jobs


# ---------------------------------------------------------------------------
# SLURM
# ---------------------------------------------------------------------------

def get_squeue():
    try:
        p = subprocess.run(
            ["squeue", "--me", "--noheader",
             "-o", "%i\x1f%j\x1f%T\x1f%M\x1f%D\x1f%R"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    rows = []
    for line in p.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) == 6:
            rows.append(parts)
    return rows


# ---------------------------------------------------------------------------
# node stats (htop-style)
# ---------------------------------------------------------------------------

class NodeSampler(object):
    def __init__(self):
        self.prev_total = None   # (total_jiffies, idle_jiffies)
        self.prev_proc = {}      # pid -> proc_jiffies
        self.prev_wall = None
        self.cpu_pct = 0.0

    def _read_cpu_total(self):
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu "):
                    vals = [int(x) for x in line.split()[1:]]
                    total = sum(vals)
                    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                    return total, idle
        return None

    def sample_cpu(self):
        cur = self._read_cpu_total()
        if cur and self.prev_total:
            dt = cur[0] - self.prev_total[0]
            di = cur[1] - self.prev_total[1]
            if dt > 0:
                self.cpu_pct = max(0.0, min(100.0, 100.0 * (dt - di) / dt))
        self.prev_total = cur
        return self.cpu_pct

    def mem(self):
        info = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    info[k] = int(v.split()[0])  # kB
        except Exception:
            return None
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = total - avail
        st = info.get("SwapTotal", 0)
        sf = info.get("SwapFree", 0)
        return {"total": total, "used": used,
                "swap_total": st, "swap_used": st - sf}

    def loadavg(self):
        try:
            with open("/proc/loadavg") as f:
                p = f.read().split()
            return p[0], p[1], p[2], p[3]  # 1m, 5m, 15m, running/total
        except Exception:
            return None

    def gpus(self):
        try:
            p = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=index,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=4)
        except Exception:
            return None
        if p.returncode != 0:
            return None
        out = []
        for line in p.stdout.splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) == 4:
                out.append(parts)
        return out

    def top_procs(self, limit=8):
        """Return the `limit` processes using the most CPU since the previous
        call, htop-style (per-core %), computed from /proc/<pid>/stat deltas."""
        wall = time.time()
        cur = {}      # pid -> cumulative (utime + stime) jiffies this sample
        meta = {}     # pid -> command name
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % pid) as f:
                    data = f.read()
                # comm (field 2) may contain spaces/parens, so split after the
                # final ')'. rest[0] is then field 3 (state); utime/stime are
                # fields 14/15 -> rest[11]/rest[12].
                rp = data.rfind(")")
                comm = data[data.find("(") + 1:rp]
                rest = data[rp + 2:].split()
                cur[pid] = int(rest[11]) + int(rest[12])
                meta[pid] = comm
            except Exception:
                continue

        procs = []
        if self.prev_wall and (wall - self.prev_wall) > 0:
            dw = wall - self.prev_wall
            for pid, jiff in cur.items():
                prev = self.prev_proc.get(pid)
                if prev is None:
                    continue
                dj = jiff - prev
                if dj <= 0:
                    continue
                pct = 100.0 * (dj / CLK_TCK) / dw   # per-core %, like top
                procs.append({"pid": pid, "comm": meta.get(pid, "?"), "cpu": pct})
        self.prev_proc = cur
        self.prev_wall = wall

        procs.sort(key=lambda x: x["cpu"], reverse=True)
        procs = procs[:limit]
        # Resolve RSS + owner only for the winners (avoids thousands of extra
        # syscalls per refresh on a busy node).
        for p in procs:
            p["rss"], p["uid"] = 0, 0
            try:
                with open("/proc/%s/statm" % p["pid"]) as f:
                    p["rss"] = int(f.read().split()[1]) * PAGE_KB
            except Exception:
                pass
            try:
                p["uid"] = os.stat("/proc/%s" % p["pid"]).st_uid
            except Exception:
                pass
        return procs


_uid_cache = {}


def uname(uid):
    if uid not in _uid_cache:
        try:
            _uid_cache[uid] = pwd.getpwuid(uid).pw_name
        except Exception:
            _uid_cache[uid] = str(uid)
    return _uid_cache[uid]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

# color pair ids
C_TITLE = 1
C_GREEN = 2
C_YELLOW = 3
C_RED = 4
C_CYAN = 5
C_DIM = 6
C_HEAD = 7
C_ORANGE = 8


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_TITLE, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_GREEN, curses.COLOR_GREEN, -1)
    curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_RED, curses.COLOR_RED, -1)
    curses.init_pair(C_CYAN, curses.COLOR_CYAN, -1)
    curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
    curses.init_pair(C_HEAD, curses.COLOR_MAGENTA, -1)
    # Orange for the pace marker. True orange needs a 256-color terminal (xterm
    # color 208); fall back to yellow on 8/16-color terminals.
    orange = 208 if curses.COLORS >= 256 else curses.COLOR_YELLOW
    try:
        curses.init_pair(C_ORANGE, orange, -1)
    except curses.error:
        curses.init_pair(C_ORANGE, curses.COLOR_YELLOW, -1)


# Shared layout columns (character offsets) for the label+bar panels.
LABEL_COL = 2
LABEL_W = 16
BAR_COL = LABEL_COL + LABEL_W   # bars and node rows start here


def cp(n):
    """Return the curses attribute for color pair `n` (see C_* constants)."""
    return curses.color_pair(n)


def pct_color(pct):
    if pct >= 90:
        return cp(C_RED) | curses.A_BOLD
    if pct >= 70:
        return cp(C_YELLOW)
    return cp(C_GREEN)


def sev_color(sev):
    return {"normal": cp(C_GREEN),
            "approaching": cp(C_YELLOW),
            "warning": cp(C_YELLOW),
            "exceeded": cp(C_RED) | curses.A_BOLD,
            "critical": cp(C_RED) | curses.A_BOLD}.get(sev, cp(C_GREEN))


class Screen(object):
    """Thin wrapper that clips writes so we never crash on small terminals."""

    def __init__(self, stdscr):
        self.s = stdscr
        self.h, self.w = stdscr.getmaxyx()

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.h or x >= self.w:
            return
        if x < 0:
            text = text[-x:]   # x is negative -> drop the first |x| chars
            x = 0
        avail = self.w - x
        if avail <= 0:
            return
        try:
            self.s.addstr(y, x, text[:avail], attr)
        except curses.error:
            pass

    def hline(self, y, x, ch, n, attr=0):
        if 0 <= y < self.h:
            try:
                self.s.hline(y, x, ch, min(n, self.w - x), attr)
            except curses.error:
                pass


def bar(pct, width):
    """Return a `width`-char progress bar string for percentage `pct`.

    Floors the fill rather than rounding it: with round(), a 40-wide bar reads
    completely full from 98.75% up, so 99% looks identical to a maxed-out 100%.
    Flooring keeps at least one empty cell until a true 100%, so a near-cap limit
    stays visibly distinct from an exhausted one. Any nonzero usage keeps at
    least one filled cell so a sliver of usage never renders as empty."""
    pct = max(0.0, min(100.0, pct))
    filled = int(width * pct / 100.0)
    if pct > 0 and filled == 0:
        filled = 1
    return BLOCK_FULL * filled + BLOCK_LIGHT * (width - filled)


def section(scr, y, title, right=""):
    """Draw a full-width panel header at row `y` (optional right-aligned text)."""
    scr.addstr(y, 0, title.ljust(scr.w), cp(C_HEAD) | curses.A_BOLD)
    if right:
        scr.addstr(y, max(0, scr.w - len(right) - 1), right,
                   cp(C_HEAD) | curses.A_BOLD)
    return y + 1


def draw_usage(scr, y, usage):
    """Draw the usage-limit bars starting at row `y`; return the next free row."""
    data, err, fetched = usage.snapshot()
    age = "" if not fetched else ago(
        datetime.fromtimestamp(fetched, timezone.utc).isoformat())
    # data + err together means we're showing the last good value while a
    # refresh is failing (usually a transient rate-limit) -> flag it as stale.
    if data and err:
        right = "stale: %s · %s" % (err, age)
    elif data:
        right = age
    else:
        right = err or "unavailable"
    y = section(scr, y, "  USAGE", right)
    if not data:
        scr.addstr(y, 2, str(err or "fetching..."), cp(C_YELLOW))
        return y + 2

    limits = data.get("limits") or []
    labels = {"session": "Session (5h)", "weekly_all": "Weekly (all)",
              "weekly_scoped": "Weekly", "weekly_opus": "Weekly (Opus)"}
    shown = 0
    barw = max(10, min(40, scr.w - 38))
    for lim in limits:
        kind = lim.get("kind", "")
        label = labels.get(kind, kind)
        scope = lim.get("scope") or {}
        model = (scope.get("model") or {}).get("display_name")
        if model and kind == "weekly_scoped":
            label = "Weekly (%s)" % model
        pct = lim.get("percent", 0)
        sev = lim.get("severity", "normal")
        resets = resets_in(lim.get("resets_at"))
        rtxt = ("resets in %s" % resets) if resets else ""
        frac = pace_fraction(lim)
        scr.addstr(y, LABEL_COL, label.ljust(LABEL_W), cp(C_DIM))
        scr.addstr(y, BAR_COL, bar(pct, barw), sev_color(sev))
        # Overlay the pace marker at the time-elapsed position, on the same
        # scale as the fill so the two read against each other directly: fill
        # past the marker = spending faster than the clock.
        if frac is not None:
            mcol = max(0, min(barw - 1, int(round(barw * frac))))
            try:
                scr.addstr(y, BAR_COL + mcol, PACE_GLYPH,
                           cp(C_ORANGE) | curses.A_BOLD)
            except curses.error:
                pass
        scr.addstr(y, BAR_COL + barw + 1, "%3d%%" % pct, sev_color(sev) | curses.A_BOLD)
        rx = BAR_COL + barw + 6
        if rtxt:
            scr.addstr(y, rx, rtxt, cp(C_DIM))
            rx += len(rtxt) + 2
        # Text cue: how far usage is ahead of / behind the clock, in points.
        if frac is not None and rx < scr.w - 1:
            over = pct - frac * 100.0
            if over >= 1.0:
                cue, ccol, bold = ("+%d%% over pace" % int(round(over)),
                                   C_ORANGE, curses.A_BOLD)
            elif over <= -1.0:
                cue, ccol, bold = ("%d%% headroom" % int(round(-over)),
                                   C_GREEN, 0)
            else:
                cue, ccol, bold = ("on pace", C_DIM, 0)
            try:
                scr.addstr(y, rx, cue[:scr.w - 1 - rx], cp(ccol) | bold)
            except curses.error:
                pass
        y += 1
        shown += 1
    if shown == 0:
        scr.addstr(y, 2, "no active limits reported", cp(C_DIM))
        y += 1
    return y + 1


# state -> (color id, bold). Resolved at draw time, since color pairs don't
# exist until curses is initialized. Traffic-light scheme:
#   green  = finished / nothing needed from you
#   yellow = actively working
#   red    = needs your attention (waiting on a prompt, or errored)
STATE_COLOR = {
    # green: done
    "done": (C_GREEN, True),
    "completed": (C_GREEN, True),
    "complete": (C_GREEN, True),
    "finished": (C_GREEN, True),
    "exited": (C_GREEN, False),
    # yellow: running / working
    "running": (C_YELLOW, True),
    "working": (C_YELLOW, True),
    "active": (C_YELLOW, True),
    "in_progress": (C_YELLOW, True),
    # red: needs prompting / blocked / errored
    "blocked": (C_RED, True),
    "waiting": (C_RED, True),
    "needs_input": (C_RED, True),
    "needs-input": (C_RED, True),
    "needsinput": (C_RED, True),
    "input": (C_RED, True),
    "error": (C_RED, True),
    "failed": (C_RED, True),
    # neutral
    "idle": (C_CYAN, False),
    "queued": (C_CYAN, False),
}


def state_attr(state):
    color, bold = STATE_COLOR.get((state or "").lower(), (C_DIM, False))
    return cp(color) | (curses.A_BOLD if bold else 0)


def agent_status(job):
    """Return (label, curses_attr) for a job's current status.

    `tempo` is the real-time activity signal and is checked first, because the
    `state` field lags a turn behind -- an agent that is actively generating
    reports state='blocked' but tempo='active'. So:
      tempo active            -> running       (yellow)
      blocked / tempo blocked -> needs-prompt  (red, waiting on you)
      error / failed          -> as-is         (red)
      done / completed        -> done          (green)
      tempo idle              -> idle          (cyan)
    """
    state = (job.get("state") or "").lower()
    tempo = (job.get("tempo") or "").lower()
    if tempo == "active":
        return "running", state_attr("working")
    if tempo == "blocked" or state in ("blocked", "waiting", "needs_input",
                                       "needs-input", "needsinput", "input"):
        return "needs-prompt", state_attr("blocked")
    if state in ("error", "failed"):
        return state, state_attr(state)
    if state in ("done", "completed", "complete", "finished", "exited"):
        return "done", state_attr("done")
    if tempo == "idle":
        return "idle", state_attr("idle")
    # fallback when tempo is absent: trust the state word
    if state in ("working", "running", "active", "in_progress"):
        return "running", state_attr("working")
    return (state or tempo or "?"), state_attr(state or tempo)


def draw_jobs(scr, y, jobs, maxrows):
    """Draw the Claude background-agents table; return the next free row.

    tokens/cost are LIFETIME totals summed from each agent's transcript (priced
    per category), not the context-window snapshot in state.json. `cmp` counts
    how many times the agent was restarted-from-summary (auto-compacted)."""
    # Totals are over ALL jobs, not just the maxrows we can show. Prefer the exact
    # transcript-derived cost; fall back to the snapshot * blended rate only for a
    # job whose transcript we couldn't read.
    total_tok = 0
    total_cost = 0.0
    for j in jobs:
        if "_cost" in j:
            total_tok += j["_life_tokens"]
            total_cost += j["_cost"]
        elif isinstance(j.get("tokens"), int):
            total_tok += j["tokens"]
            total_cost += j["tokens"] / 1e6 * COST_PER_MTOK
    right = "%d · %s tok · %s at API rates" % (
        len(jobs), human_tokens(total_tok), fmt_dollars(total_cost) or "~$0")
    y = section(scr, y, "  CLAUDE AGENTS", right)
    scr.addstr(y, 2, "%-20s %-12s %6s %7s %3s %-9s %-10s %s" %
               ("name", "status", "tokens", "cost", "cmp", "model", "updated", "detail"),
               cp(C_DIM) | curses.A_UNDERLINE)
    y += 1
    if not jobs:
        scr.addstr(y, 2, "no background jobs", cp(C_DIM))
        return y + 1
    for j in jobs[:maxrows]:
        name = (j.get("name") or j.get("_id") or "?")
        star = "*" if j.get("_id") == CUR_JOB else " "
        label, col = agent_status(j)
        if "_cost" in j:                    # accurate: lifetime from the transcript
            tok = human_tokens(j["_life_tokens"])
            cost = fmt_dollars(j["_cost"])
            ncmp = j.get("_compactions", 0)
        else:                               # fallback: state.json snapshot
            snap = j.get("tokens")
            tok = human_tokens(snap) if isinstance(snap, int) else "-"
            cost = fmt_cost(snap)
            ncmp = 0
        cmp_s = str(ncmp) if ncmp else ""
        model = short_model(j.get("_model"))
        upd = ago(j.get("updatedAt"))
        detail = (j.get("detail") or j.get("needs") or "").replace("\n", " ")
        # columns: name@2(20) status@23(12) tokens@36(6) cost@43(7) cmp@51(3)
        #          model@55(9) updated@65(10) detail@76
        scr.addstr(y, 0, star, cp(C_CYAN) | curses.A_BOLD)
        scr.addstr(y, 2, name[:20].ljust(20), curses.A_BOLD)
        scr.addstr(y, 23, label[:12].ljust(12), col)
        scr.addstr(y, 36, tok.rjust(6), cp(C_DIM))
        scr.addstr(y, 43, cost.rjust(7), cp(C_GREEN))
        scr.addstr(y, 51, cmp_s.rjust(3), cp(C_DIM))
        scr.addstr(y, 55, model[:9].ljust(9), cp(C_CYAN))
        scr.addstr(y, 65, upd[:10].ljust(10), cp(C_DIM))
        scr.addstr(y, 76, detail, cp(C_DIM))
        y += 1
    if len(jobs) > maxrows:
        scr.addstr(y, 2, "... %d more" % (len(jobs) - maxrows), cp(C_DIM))
        y += 1
    return y + 1


def draw_squeue(scr, y, rows, maxrows):
    """Draw the SLURM panel. Jobs actually placed on nodes get their own rows
    (with the nodes they're running on); pending jobs are collapsed into a single
    "in queue" summary, since their per-row "(Priority)" / "(Resources)" reasons
    are rarely what you want to stare at -- the count and the why is enough.

    The split key: squeue's %R is an actual nodelist for a placed job, but a
    parenthesized reason (e.g. "(Resources)") for one still waiting."""
    if rows is None:
        y = section(scr, y, "  SLURM (squeue)")
        scr.addstr(y, 2, "squeue unavailable", cp(C_YELLOW))
        return y + 1
    on_nodes, queued = [], []
    for r in rows:
        reason = r[5] if len(r) > 5 else ""
        (queued if reason.startswith("(") else on_nodes).append(r)
    right = "%d on nodes · %d queued" % (len(on_nodes), len(queued))
    y = section(scr, y, "  SLURM (squeue --me)", right)
    scr.addstr(y, 2, "%-11s %-22s %-10s %-11s %s" %
               ("jobid", "name", "state", "time", "nodes"),
               cp(C_DIM) | curses.A_UNDERLINE)
    y += 1
    if not on_nodes and not queued:
        scr.addstr(y, 2, "no queued or running jobs", cp(C_DIM))
        return y + 1
    for r in on_nodes[:maxrows]:
        jid, name, st, tm, ndcount, nodelist = r
        scr.addstr(y, 2, "%-11s " % jid[:11], curses.A_NORMAL)
        scr.addstr(y, 14, "%-22s " % name[:22], curses.A_NORMAL)
        scr.addstr(y, 37, st[:10].ljust(10), state_attr(st))
        scr.addstr(y, 48, "%-11s " % tm[:11], cp(C_DIM))
        nd = nodelist or "-"
        scr.addstr(y, 60, nd[:max(0, scr.w - 61)], cp(C_CYAN))
        y += 1
    if len(on_nodes) > maxrows:
        scr.addstr(y, 2, "... %d more on nodes" % (len(on_nodes) - maxrows),
                   cp(C_DIM))
        y += 1
    elif not on_nodes:
        scr.addstr(y, 2, "no jobs on nodes", cp(C_DIM))
        y += 1
    if queued:
        # Bundle the pending jobs into one line: total + a per-reason tally
        # (most common first), e.g. "in queue: 5   (Priority 3, Resources 2)".
        counts = {}
        for r in queued:
            reason = r[5].strip("()") or "pending"
            counts[reason] = counts.get(reason, 0) + 1
        tally = ", ".join("%s %d" % (k, v) for k, v in
                          sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        line = "in queue: %d   (%s)" % (len(queued), tally)
        scr.addstr(y, 2, line[:max(0, scr.w - 3)], cp(C_YELLOW))
        y += 1
    return y + 1


def draw_node(scr, y, node, cpu_pct, host, maxprocs):
    """Draw the node CPU/MEM/load/GPU bars and the top-process table."""
    y = section(scr, y, "  NODE: %s" % host, "%d cores" % NCPU)
    barw = max(10, min(40, scr.w - 30))

    scr.addstr(y, 2, "CPU".ljust(6), cp(C_DIM))
    scr.addstr(y, 8, bar(cpu_pct, barw), pct_color(cpu_pct))
    scr.addstr(y, 8 + barw + 1, "%5.1f%%" % cpu_pct, pct_color(cpu_pct) | curses.A_BOLD)
    y += 1

    m = node.mem()
    if m and m["total"]:
        used_pct = 100.0 * m["used"] / m["total"]
        scr.addstr(y, 2, "MEM".ljust(6), cp(C_DIM))
        scr.addstr(y, 8, bar(used_pct, barw), pct_color(used_pct))
        scr.addstr(y, 8 + barw + 1, "%5.1f%%" % used_pct,
                   pct_color(used_pct) | curses.A_BOLD)
        scr.addstr(y, 8 + barw + 8, "%.0f/%.0f GB" %
                   (m["used"] / 1048576.0, m["total"] / 1048576.0), cp(C_DIM))
        y += 1

    la = node.loadavg()
    if la:
        scr.addstr(y, 2, "load: %s %s %s   (1/5/15m)   procs: %s" %
                   (la[0], la[1], la[2], la[3]), cp(C_DIM))
        y += 1

    gpus = node.gpus()
    if gpus:
        for g in gpus:
            idx, util, mu, mt = g
            try:
                gp = float(util)
            except ValueError:
                gp = 0.0
            scr.addstr(y, 2, ("GPU%s" % idx).ljust(6), cp(C_DIM))
            scr.addstr(y, 8, bar(gp, barw), pct_color(gp))
            scr.addstr(y, 8 + barw + 1, "%5s%%" % util, pct_color(gp) | curses.A_BOLD)
            scr.addstr(y, 8 + barw + 8, "%s/%s MB" % (mu, mt), cp(C_DIM))
            y += 1

    y += 1
    scr.addstr(y, 2, "%-7s %-10s %6s %9s  %s" %
               ("pid", "user", "cpu%", "mem", "command"),
               cp(C_DIM) | curses.A_UNDERLINE)
    y += 1
    for p in node.top_procs(maxprocs):
        if y >= scr.h - 1:        # never draw over the footer row
            break
        scr.addstr(y, 2, "%-7s %-10s %6.1f %8.0fM  %s" %
                   (p["pid"], uname(p["uid"])[:10], p["cpu"],
                    p["rss"] / 1024.0, p["comm"]),
                   pct_color(p["cpu"]) if p["cpu"] >= 50 else cp(C_DIM))
        y += 1
    return y


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------

def main(stdscr):
    curses.curs_set(0)
    init_colors()
    stdscr.nodelay(True)
    # snapshot mode: force a full repaint each frame so a screen capture has no
    # incremental-diff artifacts. Off by default to keep the live UI flicker-free.
    snapshot = bool(os.environ.get("AGENT_SMITH_SNAPSHOT"))

    usage = Usage()
    usage_thread = threading.Thread(target=usage.loop)
    usage_thread.daemon = True
    usage_thread.start()

    node = NodeSampler()
    node.sample_cpu()      # prime the CPU delta
    node.top_procs()       # prime the per-proc delta
    host = socket.gethostname()

    last = 0.0
    while True:
        # input (responsive even between refreshes)
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            usage.stop()
            usage_thread.join(timeout=1.0)
            return
        if ch == ord("r"):
            last = 0.0
        if ch == curses.KEY_RESIZE:
            last = 0.0

        now = time.time()
        if now - last >= REFRESH:
            last = now
            stdscr.erase()
            scr = Screen(stdscr)

            clock = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            title = " Agent Smith — %s " % host
            scr.addstr(0, 0, title.ljust(scr.w), cp(C_TITLE) | curses.A_BOLD)
            scr.addstr(0, max(0, scr.w - len(clock) - 1), clock,
                       cp(C_TITLE) | curses.A_BOLD)

            cpu_pct = node.sample_cpu()
            jobs = get_jobs()
            sq = get_squeue()

            y = 2
            y = draw_usage(scr, y, usage)

            # cap each dynamic panel; the node panel gets whatever rows remain
            job_rows = max(2, min(len(jobs) or 1, 6))
            sq_rows = max(1, min(len(sq) if sq else 1, 5))

            y = draw_jobs(scr, y, jobs, job_rows)
            y = draw_squeue(scr, y, sq, sq_rows)

            proc_room = max(2, min(14, scr.h - y - 2))
            draw_node(scr, y, node, cpu_pct, host, proc_room)

            footer = " q quit   r refresh   *=this session   updates %ds " % int(REFRESH)
            scr.addstr(scr.h - 1, 0, footer.ljust(scr.w),
                       cp(C_TITLE))
            if snapshot:
                stdscr.clearok(True)   # next refresh fully clears + repaints
            stdscr.refresh()

        time.sleep(0.1)


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
