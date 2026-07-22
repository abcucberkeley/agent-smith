#!/usr/bin/env python3
"""
Agent Smith - a read-only terminal dashboard.

Six panels, refreshed on a timer:
  1. Usage      - your Claude usage limits (same numbers as the settings bar),
                  fetched live from the Anthropic usage endpoint with your own
                  OAuth token (read-only; the request only asks for your usage).
  2. Agents     - every Claude Code background job, read from
                  ~/.claude/jobs/<id>/state.json
  3. SLURM      - your `squeue --me` jobs: ones placed on nodes are listed with
                  the nodes they're on; pending jobs collapse to an in-queue count
  4. Nodes      - every compute node's SLURM state (idle / alloc / mix / down /
                  drain) with a category tally and who's on the busy ones, from
                  `sinfo` + `squeue`.
  5. Storage    - a usage bar per shared cluster filesystem (the /clusterfs
                  pools), df'd on a background thread so a hung mount can't stall
                  the UI; shows which pool is filling up.
  6. Node       - htop-style CPU / memory / load / GPU + top processes for
                  whatever compute node this is running on.

Pure stdlib (curses). No pip installs. Works on Python 3.6+.

Controls:  q quit   r force-refresh   1/2/3 expand panels
           arrows / PgUp-PgDn / wheel scroll (content that overflows the
           terminal is scrollable, not lost)   (resizes automatically)
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
STORAGE_REFRESH = 30.0   # seconds between df samples of the cluster filesystems
STORAGE_TIMEOUT = 8.0    # per-mount df timeout so a hung NFS pool can't stall us
STORAGE_PREFIXES = ("/clusterfs",)  # mountpoint prefixes treated as cluster storage

# Scrolling: content is drawn into an off-screen pad this big, and the visible
# window is blitted between the pinned title/footer. Anything that doesn't fit
# on the terminal is reachable with the arrow keys / PgUp-PgDn instead of lost.
PAD_H = 512              # scroll-buffer height (rows) -- plenty for expanded panels
PAD_W = 512              # scroll-buffer width (cols)
MIN_CONTENT_W = 100      # lay content out to >= this wide; scroll sideways if narrower
NODE_PROC_ROWS = 40      # top-process rows when the node panel is expanded (14 collapsed)
SCROLL_STEP = 2          # rows per arrow-key press
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE_KB = (os.sysconf("SC_PAGE_SIZE") // 1024) if hasattr(os, "sysconf") else 4
NCPU = os.cpu_count() or 1
CUR_JOB = os.path.basename(os.environ.get("CLAUDE_JOB_DIR", "").rstrip("/")) or None
try:
    ME = os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
except Exception:
    ME = os.environ.get("USER") or ""

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)  # sort fallback for jobs

# Bar glyphs. These require a Unicode-capable terminal + font; on a misconfigured
# terminal they may show as "?" (the rest of the dashboard still works).
BLOCK_FULL = "█"   # full block
BLOCK_LIGHT = "░"  # light shade
PACE_GLYPH = "╎"   # dashed vertical tick: marks how far the clock is into a window

# Tiny low-res Agent Smith caricature (slicked hair, sunglasses, suit collar),
# tucked into the top-right when the terminal is wide enough to have room there.
SMITH_LOGO = [
    "  ▟█████▙  ",   # slicked-back hair
    " ██▀▀▀▀▀██ ",
    " █▉▉▉█▉▉▉█ ",   # wraparound sunglasses (two lenses + bridge)
    " █▔▔▔█▔▔▔█ ",
    " ▜▖ ▀▀▀ ▗▛ ",   # stern set jaw
    "  ▀█▄▄▄█▀  ",
    " ▟█▀███▀█▙ ",   # suit shoulders
    "██▘ █▬█ ▝██",   # lapels + collar
    "▘   ▐█▌   ▝",   # hanging tie
]

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


def human_bytes(n):
    """Compact byte size in base-1024 units, like `df -h`: 512K, 18T, 3.5P.
    One decimal below 100, none above, so the storage bars stay aligned."""
    n = float(n)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if n < 1024.0 or unit == "P":
            if unit in ("B", "K", "M") or n >= 100:
                return "%.0f%s" % (n, unit)
            return "%.1f%s" % (n, unit)
        n /= 1024.0


def resets_in(iso):
    dt = parse_iso(iso)
    if not dt:
        return ""
    return human_delta((dt - datetime.now(timezone.utc)).total_seconds())


def clock12(dt_local):
    """'3pm', '9:46pm', '12:05am' -- 12-hour local clock, no leading zero,
    minutes dropped when the time lands on the hour, lowercase am/pm."""
    ampm = "am" if dt_local.hour < 12 else "pm"
    h12 = dt_local.hour % 12 or 12
    if dt_local.minute:
        return "%d:%02d%s" % (h12, dt_local.minute, ampm)
    return "%d%s" % (h12, ampm)


def resets_when(iso):
    """Absolute local reset time with weekday AND calendar date -- e.g.
    'Fri Jul 11, 3pm' or 'Sat Jul 11, 9:46pm'. The date matters because a weekly
    window can reset several days out, where a bare weekday ('Sat') still makes
    you count. '' if the timestamp can't be parsed. Uses the node's local
    timezone via astimezone(); the dashboard is read on that same (Pacific) node,
    so local time is what the user expects. Complements resets_in()'s countdown."""
    d = parse_iso(iso)
    if not d:
        return ""
    loc = d.astimezone()
    return "%s %s %d, %s" % (loc.strftime("%a"), loc.strftime("%b"),
                             loc.day, clock12(loc))


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
        self._wake = threading.Event()   # set to force an immediate re-fetch

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
            # sleep until the next scheduled fetch, or until refresh_now()/stop()
            self._wake.wait(delay)
            self._wake.clear()
            if self._stop.is_set():
                return

    def refresh_now(self):
        """Wake the fetch loop for an immediate refresh (bound to the 'u' key)."""
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def snapshot(self):
        with self.lock:
            return self.data, self.error, self.fetched_at


class StorageSampler(object):
    """Samples usage of the shared cluster filesystems (the /clusterfs pools)
    with `df`, on a background thread so a slow -- or hung -- NFS mount can never
    freeze the dashboard. Each pool is df'd separately with its own timeout, so
    one wedged mount shows as '(unreachable)' while the rest still report."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rows = []          # list of {mount,total,used,avail,pct,stale}
        self.error = "sampling..."
        self.sampled_at = 0.0
        self._stop = threading.Event()

    def _mounts(self):
        """Distinct mountpoints under STORAGE_PREFIXES, read from /proc/mounts so
        the panel adapts to whatever pools this node actually has. Falls back to
        '/' when there are no cluster mounts (e.g. running off a laptop)."""
        seen = []
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    mp = parts[1]
                    if mp.startswith(STORAGE_PREFIXES) and mp not in seen:
                        seen.append(mp)
        except Exception:
            pass
        return seen or ["/"]

    def _df_one(self, mp):
        """df a single mountpoint; return a row dict, or a stale row on timeout."""
        try:
            out = subprocess.check_output(
                ["df", "-P", "-B1", mp],
                stderr=subprocess.DEVNULL, timeout=STORAGE_TIMEOUT,
            ).decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            return {"mount": mp, "total": 0, "used": 0, "avail": 0,
                    "pct": 0.0, "stale": True}
        except Exception:
            return None
        lines = out.splitlines()
        if len(lines) < 2:
            return None
        f = lines[1].split()          # df -P keeps each fs to a single line
        if len(f) < 6:
            return None
        try:
            total, used, avail = int(f[1]), int(f[2]), int(f[3])
        except ValueError:
            return None
        pct = (100.0 * used / total) if total else 0.0
        return {"mount": mp, "total": total, "used": used, "avail": avail,
                "pct": pct, "stale": False}

    def _sample_once(self):
        rows = [r for r in (self._df_one(mp) for mp in self._mounts()) if r]
        rows.sort(key=lambda r: r["mount"])
        return (rows, None) if rows else (None, "df error")

    def loop(self):
        while not self._stop.is_set():
            rows, err = self._sample_once()
            with self.lock:
                if rows is not None:
                    self.rows, self.error = rows, None   # fresh good data
                else:
                    self.error = err                     # keep last good rows
                self.sampled_at = time.time()
            if self._stop.wait(STORAGE_REFRESH):
                return

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self.lock:
            return list(self.rows), self.error, self.sampled_at


class MyShareSampler(object):
    """On-demand `du` of the current user's data on each cluster filesystem.
    Never runs on its own -- a du of a multi-TB tree can take minutes to hours --
    it's triggered by the 'd' key. Each pool is measured on its own daemon
    thread; the cached result is shown as a magenta overlay on that pool's usage
    bar plus a 'you N.NT (Nh ago)' note. (No cheap per-user source exists on
    these NFS mounts -- quota is empty -- so a real du is the only way.)"""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}    # mount -> {"bytes":int|None, "at":float, "running":bool, "err":bool}

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def measure(self, mounts):
        """Start (or restart) a du for each mountpoint, skipping any in flight."""
        for mp in mounts:
            with self.lock:
                cur = self.data.get(mp)
                if cur and cur.get("running"):
                    continue
                self.data[mp] = {"bytes": (cur or {}).get("bytes"),
                                 "at": (cur or {}).get("at"),
                                 "running": True, "err": False}
            th = threading.Thread(target=self._run, args=(mp,))
            th.daemon = True
            th.start()

    def _run(self, mount):
        dirs = self._my_dirs(mount)
        total, ok = 0, True           # no dirs owned by me => 0 here (valid answer)
        for d in dirs:
            b = self._du_bytes(d)
            if b is None:
                ok = False            # a du errored -> keep the previous value
            else:
                total += b
        with self.lock:
            prev = self.data.get(mount, {})
            self.data[mount] = {
                "bytes": total if ok else prev.get("bytes"),
                "at": time.time() if ok else prev.get("at"),
                "running": False, "err": not ok}

    def _my_dirs(self, mount):
        """Top-level dirs on `mount` owned by me -- 'my share' of that pool by
        path (du sums the whole subtree regardless of who owns files deeper
        down, which is what 'how much am I taking' means)."""
        try:
            myuid = os.getuid()
        except AttributeError:
            return []
        out = []
        try:
            for name in os.listdir(mount):
                p = os.path.join(mount, name)
                try:
                    if os.path.isdir(p) and not os.path.islink(p) \
                            and os.stat(p).st_uid == myuid:
                        out.append(p)
                except OSError:
                    pass
        except OSError:
            pass
        return out

    def _du_bytes(self, path):
        try:
            out = subprocess.check_output(["du", "-sb", path],
                                          stderr=subprocess.DEVNULL)
            return int(out.split()[0])
        except Exception:
            return None


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


def get_nodes():
    """One snapshot of every compute node: state + CPU alloc/total + the users
    running on it. Two cheap SLURM calls, each timeout-guarded; returns None on
    failure so the panel degrades to 'unavailable' instead of crashing.

      sinfo -N          -> per-node state and CPU A/I/O/T counts (one row/node)
      squeue -t RUNNING -> node -> users, aggregated (a node can host several)

    A job's nodelist may be a range (n[0024-0025].abc0); those are expanded via
    `scontrol show hostnames` (cached, only when a '[' is actually present)."""
    try:
        p = subprocess.run(
            ["sinfo", "-N", "-h", "-o", "%N\x1f%t\x1f%C"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=6)
    except Exception:
        return None
    if p.returncode != 0:
        return None

    nodes, index = [], {}
    for line in p.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        name, state, cpus = parts
        a = t = 0
        seg = cpus.split("/")            # Allocated/Idle/Other/Total
        if len(seg) == 4:
            try:
                a, t = int(seg[0]), int(seg[3])
            except ValueError:
                a = t = 0
        d = {"node": name, "state": state.strip(),
             "cpus_alloc": a, "cpus_total": t, "users": set(),
             "user_cores": {}}          # user -> cores in use on this node
        nodes.append(d)
        index[name] = d

    # node -> users. Best effort: if squeue is slow/unavailable we still show
    # states, just without the "who".
    expand = {}   # nodelist -> [hostnames], cached within this call
    try:
        q = subprocess.run(
            ["squeue", "-h", "-t", "RUNNING", "-o", "%N\x1f%u\x1f%C"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=6)
    except Exception:
        q = None
    if q is not None and q.returncode == 0:
        for line in q.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 3:
                continue
            nodelist, user = parts[0].strip(), parts[1].strip()
            try:
                cores = int(parts[2].strip())
            except ValueError:
                cores = 0
            if not nodelist:
                continue
            if "[" in nodelist:
                hosts = expand.get(nodelist)
                if hosts is None:
                    try:
                        e = subprocess.run(
                            ["scontrol", "show", "hostnames", nodelist],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=4)
                        hosts = e.stdout.split() if e.returncode == 0 else [nodelist]
                    except Exception:
                        hosts = [nodelist]
                    expand[nodelist] = hosts
            else:
                hosts = [nodelist]
            for h in hosts:
                d = index.get(h)
                if d is not None:
                    d["users"].add(user)
                    d["user_cores"][user] = d["user_cores"].get(user, 0) + cores

    for d in nodes:
        d["users"] = sorted(d["users"])
    return nodes


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
    """Thin wrapper that clips writes so we never crash on small terminals.

    Wraps either stdscr (dims read from the terminal) or an off-screen pad
    (dims passed explicitly, since a pad's own size is the scroll buffer, not
    the logical content width we want panels to lay out to)."""

    def __init__(self, win, h=None, w=None):
        self.s = win
        if h is None:
            self.h, self.w = win.getmaxyx()
        else:
            self.h, self.w = h, w

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


# ---------------------------------------------------------------------------
# expand / collapse: each dynamic panel can be clicked (or keyed) open to show
# every row instead of the capped preview. `_expanded` holds the per-panel state;
# `_click_targets` is rebuilt every frame as a list of (row, x0, x1, key) hitboxes
# that the main loop tests a mouse click against. Keys: "jobs", "slurm", "node".
# ---------------------------------------------------------------------------
_expanded = {"jobs": False, "slurm": False, "node": False, "nodes": False}
_expanded_node_rows = set()   # NODES-panel node names drilled open (cores-by-user)
_click_targets = []


def add_target(y, x0, x1, key):
    """Register a clickable region [x0, x1) on row `y` that toggles panel `key`."""
    _click_targets.append((y, x0, x1, key))


def hit_target(mx, my):
    """Return the panel key whose hitbox contains screen cell (mx, my), or None."""
    for (ty, x0, x1, key) in _click_targets:
        if my == ty and x0 <= mx < x1:
            return key
    return None


def draw_toggle(scr, hy, title, key):
    """Draw a [+]/[-] expander just after `title` on header row `hy` and register
    it as a click target. [+] = collapsed (more to show), [-] = expanded."""
    glyph = " [-]" if _expanded.get(key) else " [+]"
    x = len(title)
    scr.addstr(hy, x, glyph, cp(C_YELLOW) | curses.A_BOLD)
    add_target(hy, x, x + len(glyph), key)


def draw_more(scr, y, key, hidden):
    """Draw the clickable expand/collapse line under a panel and register it.
    `hidden` = rows not shown while collapsed. Returns the next free row."""
    if _expanded.get(key):
        txt = "  ▴ show fewer (click)"
    elif hidden > 0:
        txt = "  ▾ %d more (click to expand)" % hidden
    else:
        return y
    scr.addstr(y, 2, txt, cp(C_YELLOW))
    add_target(y, 2, 2 + len(txt), key)
    return y + 1


# Once a limit is exhausted the clock keeps ticking but "pace" is meaningless, so
# we snapshot the pace fraction the first frame we see it maxed and hold it there
# (keyed by limit kind). Cleared when the limit resets and usage is available.
_pace_frozen = {}


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
        # No usage left: turn the row red, and treat pace specially (see below).
        exhausted = pct >= 100 or sev == "exceeded"
        resets = resets_in(lim.get("resets_at"))
        when = resets_when(lim.get("resets_at"))
        # Lead with the absolute wall-clock reset time (what the user asked to
        # see), keep the countdown in parens as the at-a-glance "how long left".
        if when and resets:
            rtxt = "resets %s (%s)" % (when, resets)
        elif resets:
            rtxt = "resets in %s" % resets
        else:
            rtxt = ""
        frac = pace_fraction(lim)
        if exhausted:
            frac = _pace_frozen.setdefault(kind, frac)   # capture once, then hold
            row_col = cp(C_RED) | curses.A_BOLD
        else:
            _pace_frozen.pop(kind, None)                  # usage returned -> resume
            row_col = sev_color(sev)
        scr.addstr(y, LABEL_COL, label.ljust(LABEL_W),
                   row_col if exhausted else cp(C_DIM))
        scr.addstr(y, BAR_COL, bar(pct, barw), row_col)
        # Overlay the pace marker at the time-elapsed position, on the same
        # scale as the fill so the two read against each other directly: fill
        # past the marker = spending faster than the clock. Once exhausted this
        # is the FROZEN position it held when we ran out -- it stops advancing.
        if frac is not None:
            mcol = max(0, min(barw - 1, int(round(barw * frac))))
            try:
                scr.addstr(y, BAR_COL + mcol, PACE_GLYPH,
                           cp(C_ORANGE) | curses.A_BOLD)
            except curses.error:
                pass
        scr.addstr(y, BAR_COL + barw + 1, "%3d%%" % pct, row_col | curses.A_BOLD)
        rx = BAR_COL + barw + 6
        if rtxt:
            scr.addstr(y, rx, rtxt, cp(C_DIM))
            rx += len(rtxt) + 2
        if exhausted:
            # Stop reporting pace once there's nothing left to pace against;
            # say so plainly instead.
            if rx < scr.w - 1:
                try:
                    scr.addstr(y, rx, "LIMIT REACHED"[:scr.w - 1 - rx],
                               cp(C_RED) | curses.A_BOLD)
                except curses.error:
                    pass
        elif frac is not None and rx < scr.w - 1:
            # Text cue: how far usage is ahead of / behind the clock, in points.
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
    title = "  CLAUDE AGENTS"
    hy = y
    y = section(scr, y, title, right)
    hidden = max(0, len(jobs) - maxrows)
    limit = len(jobs) if _expanded["jobs"] else maxrows
    if hidden > 0 or _expanded["jobs"]:
        draw_toggle(scr, hy, title, "jobs")
    scr.addstr(y, 2, "%-20s %-12s %6s %7s %3s %-9s %-10s %s" %
               ("name", "status", "tokens", "cost", "cmp", "model", "updated", "detail"),
               cp(C_DIM) | curses.A_UNDERLINE)
    y += 1
    if not jobs:
        scr.addstr(y, 2, "no background jobs", cp(C_DIM))
        return y + 1
    for j in jobs[:limit]:
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
    y = draw_more(scr, y, "jobs", hidden)
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
    title = "  SLURM (squeue --me)"
    hy = y
    y = section(scr, y, title, right)
    hidden = max(0, len(on_nodes) - maxrows)
    limit = len(on_nodes) if _expanded["slurm"] else maxrows
    if hidden > 0 or _expanded["slurm"]:
        draw_toggle(scr, hy, title, "slurm")
    scr.addstr(y, 2, "%-11s %-22s %-10s %-11s %s" %
               ("jobid", "name", "state", "time", "nodes"),
               cp(C_DIM) | curses.A_UNDERLINE)
    y += 1
    if not on_nodes and not queued:
        scr.addstr(y, 2, "no queued or running jobs", cp(C_DIM))
        return y + 1
    for r in on_nodes[:limit]:
        jid, name, st, tm, ndcount, nodelist = r
        scr.addstr(y, 2, "%-11s " % jid[:11], curses.A_NORMAL)
        scr.addstr(y, 14, "%-22s " % name[:22], curses.A_NORMAL)
        scr.addstr(y, 37, st[:10].ljust(10), state_attr(st))
        scr.addstr(y, 48, "%-11s " % tm[:11], cp(C_DIM))
        nd = nodelist or "-"
        scr.addstr(y, 60, nd[:max(0, scr.w - 61)], cp(C_CYAN))
        y += 1
    if not on_nodes:
        scr.addstr(y, 2, "no jobs on nodes", cp(C_DIM))
        y += 1
    else:
        y = draw_more(scr, y, "slurm", hidden)
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


# sinfo compact-state flag suffixes (* not responding, ~ powered down, - drain
# flag, etc.) -- strip them to get the base state we color on.
def _node_base_state(state):
    return state.rstrip("*~#!%$@-").lower()


# base state -> (color id, bold). idle=green (free), alloc/mix=cyan (busy+healthy),
# down/drain=red (unusable), comp/resv=yellow (transient).
_NODE_STATE_COLOR = {
    "idle": (C_GREEN, False),
    "mix": (C_CYAN, False), "mixed": (C_CYAN, False),
    "alloc": (C_CYAN, True), "allocated": (C_CYAN, True),
    "comp": (C_YELLOW, False), "completing": (C_YELLOW, False),
    "resv": (C_YELLOW, False), "reserved": (C_YELLOW, False),
    "drain": (C_RED, False), "draining": (C_RED, False), "drng": (C_RED, False),
    "down": (C_RED, True), "downp": (C_RED, True),
    "fail": (C_RED, True), "failing": (C_RED, True),
    "unk": (C_DIM, False), "unknown": (C_DIM, False),
}


def _node_attr(state):
    color, bold = _NODE_STATE_COLOR.get(_node_base_state(state), (C_DIM, False))
    return cp(color) | (curses.A_BOLD if bold else 0)


def draw_nodes(scr, y, nodes, maxrows):
    """Draw the NODES panel: one row per compute node with its SLURM state, CPU
    alloc/total, and (for busy nodes) who's on it. The header carries a tally on
    the right (e.g. '7 alloc · 5 mix · 3 idle · 24 down'). Busy nodes sort to the
    top; the list collapses to `maxrows` with a clickable 'N more' expander."""
    title = "  NODES (sinfo)"
    hy = y
    if nodes is None:
        y = section(scr, y, title)
        scr.addstr(y, 2, "sinfo unavailable", cp(C_YELLOW))
        return y + 1

    tally = {}
    for d in nodes:
        b = _node_base_state(d["state"])
        tally[b] = tally.get(b, 0) + 1
    order = ["alloc", "mix", "idle", "comp", "resv", "drain", "down"]
    seen, bits = set(), []
    for k in order:
        if tally.get(k):
            bits.append("%d %s" % (tally[k], k)); seen.add(k)
    for k in sorted(tally):                     # anything unusual, appended
        if k not in seen and tally[k]:
            bits.append("%d %s" % (tally[k], k))
    y = section(scr, y, title, " · ".join(bits))

    # busy first (has users, or alloc/mix), then idle, then down/other; name
    # order inside each bucket -- keeps the useful rows visible while collapsed.
    # your nodes first, then busy, then idle, then down/other; name order within.
    def rank(d):
        b = _node_base_state(d["state"])
        if ME and ME in d["users"]:
            return -1                           # my nodes pinned to the very top
        if d["users"] or b in ("alloc", "mix"):
            return 0
        if b == "idle":
            return 1
        return 2
    ordered = sorted(nodes, key=lambda d: (rank(d), d["node"]))

    hidden = max(0, len(ordered) - maxrows)
    limit = len(ordered) if _expanded["nodes"] else maxrows
    if hidden > 0 or _expanded["nodes"]:
        draw_toggle(scr, hy, title, "nodes")

    scr.addstr(y, 2, "  %-12s %-9s %-9s %s" % ("node", "state", "cpu", "users"),
               cp(C_DIM) | curses.A_UNDERLINE)
    y += 1
    if not ordered:
        scr.addstr(y, 2, "no nodes reported", cp(C_DIM))
        return y + 1

    for d in ordered[:limit]:
        if y >= scr.h - 1:                      # never draw over the footer row
            break
        short = d["node"].split(".")[0]         # drop the .abc0 domain for width
        mine = bool(ME and ME in d["users"])
        opened = d["node"] in _expanded_node_rows
        # a node with jobs on it is click-to-expand into its per-user core split
        expandable = bool(d["user_cores"])
        glyph = ("▾ " if opened else "▸ ") if expandable else "  "
        scr.addstr(y, 2, glyph, cp(C_YELLOW))
        scr.addstr(y, 4, "%-12s" % short[:12],
                   (cp(C_GREEN) | curses.A_BOLD) if mine else cp(C_DIM))
        scr.addstr(y, 17, "%-9s" % d["state"][:9], _node_attr(d["state"]))
        scr.addstr(y, 27, "%2d/%-2d" % (d["cpus_alloc"], d["cpus_total"]),
                   cp(C_DIM))
        if d["users"]:
            who = "/".join(d["users"])          # multiple users separated by /
            scr.addstr(y, 37, who[:max(0, scr.w - 38)], cp(C_CYAN))
        if expandable:                          # whole row is the click target
            add_target(y, 2, scr.w, "node:" + d["node"])
        y += 1
        # drilled open: one indented line per user with their core count on it
        if opened and d["user_cores"]:
            for u, c in sorted(d["user_cores"].items(), key=lambda kv: (-kv[1], kv[0])):
                if y >= scr.h - 1:
                    break
                a = (cp(C_GREEN) | curses.A_BOLD) if u == ME else cp(C_DIM)
                scr.addstr(y, 6, "%-14s %d core%s" %
                           (u[:14], c, "" if c == 1 else "s"), a)
                y += 1

    y = draw_more(scr, y, "nodes", hidden)
    return y + 1


def draw_storage(scr, y, storage, share=None):
    """Draw a usage bar per shared cluster filesystem (the /clusterfs pools), so
    you can see at a glance which one is filling up. Data comes from a background
    df sampler (StorageSampler); an unreachable/hung pool renders '(unreachable)'
    and never stalls the UI. Bar color follows pct_color (green/yellow/red).

    `share` (from MyShareSampler, populated on the 'd' key) overlays your own
    footprint on each bar as a magenta segment + a 'you N.NT (age)' note."""
    rows, err, ts = storage
    right = ("%s ago" % human_delta(time.time() - ts)) if ts else ""
    y = section(scr, y, "  STORAGE (cluster filesystems)", right)
    if not rows:
        scr.addstr(y, 2, err or "no cluster filesystems", cp(C_DIM))
        return y + 1
    barw = max(10, min(40, scr.w - 34))
    for r in rows:
        if y >= scr.h - 1:            # never draw over the footer row
            break
        name = (os.path.basename(r["mount"]) or r["mount"])[:6]
        scr.addstr(y, 2, name.ljust(6), cp(C_DIM))
        if r.get("stale") or not r["total"]:
            scr.addstr(y, 8, "(unreachable)", cp(C_YELLOW))
            y += 1
            continue
        pct = r["pct"]
        scr.addstr(y, 8, bar(pct, barw), pct_color(pct))
        si = share.get(r["mount"]) if share else None
        # overlay my share of this pool as a magenta segment over the used part
        if si and si.get("bytes") and r["total"]:
            mine = max(0, min(barw, int(barw * si["bytes"] / r["total"])))
            if mine:
                scr.addstr(y, 8, BLOCK_FULL * mine, cp(C_HEAD) | curses.A_BOLD)
        scr.addstr(y, 8 + barw + 1, "%5.1f%%" % pct, pct_color(pct) | curses.A_BOLD)
        info = "%s/%s  %s free" % (human_bytes(r["used"]), human_bytes(r["total"]),
                                   human_bytes(r["avail"]))
        if si:
            if si.get("running"):
                info += "  · you: measuring…"
            elif si.get("err") and not si.get("bytes"):
                info += "  · you: du failed"
            elif si.get("at") is not None:
                info += "  · you %s (%s ago)" % (
                    human_bytes(si.get("bytes") or 0),
                    human_delta(time.time() - si["at"]))
        scr.addstr(y, 8 + barw + 8, info, cp(C_DIM))
        y += 1
    return y


NODE_PROCS_COLLAPSED = 14   # top-process rows shown before the panel is expanded


def draw_node(scr, y, node, cpu_pct, host, proc_avail, procs=None):
    """Draw the node CPU/MEM/load/GPU bars and the top-process table.

    `proc_avail` is how many process rows fit between here and the footer. While
    collapsed we cap the list at NODE_PROCS_COLLAPSED; expanded, we fill the space
    (a [+] appears on the header only when there's actually more room to fill).

    `procs` is an optional pre-sampled top_procs() list -- passed in so a redraw
    (on a keystroke) reuses the cached sample instead of re-reading /proc and
    resetting the CPU-delta baseline. When None we sample directly."""
    title = "  NODE: %s" % host
    hy = y
    y = section(scr, y, title, "%d cores" % NCPU)
    if proc_avail > NODE_PROCS_COLLAPSED or _expanded["node"]:
        draw_toggle(scr, hy, title, "node")
    nprocs = proc_avail if _expanded["node"] else min(NODE_PROCS_COLLAPSED, proc_avail)
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
    plist = node.top_procs(nprocs) if procs is None else procs[:nprocs]
    for p in plist:
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
    # Enable single-click reporting so the [+]/[-] expanders are clickable. Best
    # effort: terminals without mouse support (or tmux without `mouse on`) just
    # never send events, and the 1/2/3 keys still toggle the same panels.
    try:
        mask = curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED
        mask |= getattr(curses, "BUTTON4_PRESSED", 0)   # wheel up
        mask |= getattr(curses, "BUTTON5_PRESSED", 0)   # wheel down
        curses.mousemask(mask)
    except curses.error:
        pass
    # snapshot mode: force a full repaint each frame so a screen capture has no
    # incremental-diff artifacts. Off by default to keep the live UI flicker-free.
    snapshot = bool(os.environ.get("AGENT_SMITH_SNAPSHOT"))

    usage = Usage()
    usage_thread = threading.Thread(target=usage.loop)
    usage_thread.daemon = True
    usage_thread.start()

    storage = StorageSampler()
    storage_thread = threading.Thread(target=storage.loop)
    storage_thread.daemon = True
    storage_thread.start()

    share = MyShareSampler()   # on-demand du of my footprint per pool (the 'd' key)

    node = NodeSampler()
    node.sample_cpu()      # prime the CPU delta
    node.top_procs()       # prime the per-proc delta
    host = socket.gethostname()

    # Panels are drawn into an off-screen pad from cached samples; the title
    # (row 0) and footer (last row) stay pinned on stdscr and the middle is a
    # scrollable viewport. A selection cursor (▶, drawn at the left) moves
    # between the expandable items: up/down move it, right/left open/close it,
    # and the viewport auto-follows. PgUp/PgDn + wheel raw-scroll long content
    # (e.g. the process list). Redraw works off the cache, so moving the cursor
    # never re-runs sinfo/df/du -- only the 2s tick re-samples.
    pad = curses.newpad(PAD_H, PAD_W)
    scroll_y = scroll_x = 0
    content_h = 1                # pad rows used (set each redraw)
    vw = MIN_CONTENT_W           # logical content width (set each redraw)
    footer = ""
    cursor_key = None            # the focused expandable item
    focus_list = []              # focusable toggle keys, top-to-bottom
    focus_rows = {}              # key -> pad row
    cache = {"cpu": 0.0, "jobs": [], "sq": None, "nodes": None, "procs": []}

    def redraw():
        """Draw every panel into the pad from the cached samples, rebuild the
        list of focusable items, and stamp the selection cursor. No sampling
        (sinfo/df/du/proc) here, so it's cheap to call on every keystroke."""
        nonlocal content_h, vw, cursor_key
        w = stdscr.getmaxyx()[1]
        vw = min(PAD_W, max(MIN_CONTENT_W, w))
        pad.erase()
        scr = Screen(pad, PAD_H, vw)
        del _click_targets[:]
        jobs, sq, nodes = cache["jobs"], cache["sq"], cache["nodes"]
        job_rows = max(2, min(len(jobs) or 1, 6))
        sq_rows = max(1, min(len(sq) if sq else 1, 5))
        nodes_rows = max(3, min(len(nodes) if nodes else 1, 8))
        y = 0
        y = draw_usage(scr, y, usage)
        y = draw_jobs(scr, y, jobs, job_rows)
        y = draw_squeue(scr, y, sq, sq_rows)
        y = draw_nodes(scr, y, nodes, nodes_rows)
        y = draw_storage(scr, y, storage.snapshot(), share.snapshot())
        y = draw_node(scr, y, node, cache["cpu"], host, NODE_PROC_ROWS,
                      cache["procs"])
        content_h = max(1, y)
        rowmap = {}
        for (ty, _x0, _x1, k) in _click_targets:
            if k not in rowmap or ty < rowmap[k]:
                rowmap[k] = ty
        focus_list[:] = sorted(rowmap, key=lambda k: rowmap[k])
        focus_rows.clear()
        focus_rows.update(rowmap)
        if cursor_key not in focus_rows:
            cursor_key = focus_list[0] if focus_list else None
        if cursor_key in focus_rows:
            scr.addstr(focus_rows[cursor_key], 0, "▶", cp(C_YELLOW) | curses.A_BOLD)

    def present():
        """Pin title+footer on stdscr and blit the visible pad window between
        them, auto-following the cursor and clamping to the content."""
        nonlocal scroll_y, scroll_x
        h, w = stdscr.getmaxyx()
        view_h = max(1, h - 2)                     # screen rows 1..h-2 show content
        if cursor_key in focus_rows:               # keep the selection in view
            fr = focus_rows[cursor_key]
            if fr < scroll_y:
                scroll_y = fr
            elif fr > scroll_y + view_h - 1:
                scroll_y = fr - view_h + 1
        max_sy = max(0, content_h - view_h)
        max_sx = max(0, vw - w)
        scroll_y = max(0, min(scroll_y, max_sy))
        scroll_x = max(0, min(scroll_x, max_sx))
        stdscr.erase()
        clock = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title = " Agent Smith — %s " % host
        try:
            stdscr.addstr(0, 0, title.ljust(w)[:w], cp(C_TITLE) | curses.A_BOLD)
            if w > len(clock) + 1:
                stdscr.addstr(0, w - len(clock) - 1, clock,
                              cp(C_TITLE) | curses.A_BOLD)
        except curses.error:
            pass
        hint = ""
        if scroll_y > 0 or scroll_y < max_sy:
            hint = "  %s%s more" % ("↑" if scroll_y > 0 else "",
                                    "↓" if scroll_y < max_sy else "")
        try:
            stdscr.addstr(h - 1, 0, (footer + hint).ljust(w)[:w], cp(C_TITLE))
        except curses.error:
            pass
        # Agent Smith logo: carved out of the top-right corner (over the usage
        # bars' empty right edge) so it costs no vertical space and never covers
        # panel text -- only when the terminal is wide/tall enough to have room.
        lh = len(SMITH_LOGO)
        lw = max(len(s) for s in SMITH_LOGO)
        show_logo = (w >= lw + 80) and (h >= lh + 4)
        lx = w - lw - 1
        if show_logo:
            for i, line in enumerate(SMITH_LOGO):
                try:
                    stdscr.addstr(1 + i, lx, line, cp(C_GREEN) | curses.A_BOLD)
                except curses.error:
                    pass
        stdscr.noutrefresh()
        try:
            if show_logo:
                # blit L-shaped: left of the logo for its rows, full width below
                pad.noutrefresh(scroll_y, scroll_x, 1, 0, lh, max(0, lx - 1))
                pad.noutrefresh(scroll_y + lh, scroll_x, 1 + lh, 0, h - 2,
                                max(0, w - 1))
            else:
                pad.noutrefresh(scroll_y, scroll_x, 1, 0, h - 2, max(0, w - 1))
        except curses.error:
            pass
        curses.doupdate()

    def cursor_move(delta):
        nonlocal cursor_key
        if not focus_list:
            return
        i = focus_list.index(cursor_key) if cursor_key in focus_list else 0
        cursor_key = focus_list[max(0, min(len(focus_list) - 1, i + delta))]

    def cursor_expand(want):
        # want: True open, False close, None toggle -- of the focused item
        if cursor_key is None:
            return
        if cursor_key.startswith("node:"):
            n = cursor_key[5:]
            new = (n not in _expanded_node_rows) if want is None else want
            if new:
                _expanded_node_rows.add(n)
            else:
                _expanded_node_rows.discard(n)
        else:
            cur = _expanded.get(cursor_key, False)
            _expanded[cursor_key] = (not cur) if want is None else want

    last = 0.0
    while True:
        # input (responsive even between refreshes)
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            usage.stop()
            storage.stop()
            usage_thread.join(timeout=1.0)
            storage_thread.join(timeout=1.0)
            return
        elif ch == ord("r"):
            last = 0.0                               # force a full re-sample
        elif ch in (ord("u"), ord("U")):
            usage.refresh_now(); redraw(); present()
        elif ch in (ord("d"), ord("D")):
            # du of my footprint on every pool (slow; runs in the background and
            # overlays each bar as it finishes)
            share.measure([r["mount"] for r in storage.snapshot()[0]])
            redraw(); present()
        elif ch == curses.KEY_RESIZE:
            last = 0.0
        elif ch in (ord("1"), ord("2"), ord("3"), ord("4")):
            _expanded[{"1": "jobs", "2": "slurm", "3": "node",
                       "4": "nodes"}[chr(ch)]] ^= True
            redraw(); present()
        # cursor navigation: up/down move the selection, right/left open/close it
        elif ch in (curses.KEY_DOWN, ord("j")):
            cursor_move(1); redraw(); present()
        elif ch in (curses.KEY_UP, ord("k")):
            cursor_move(-1); redraw(); present()
        elif ch in (curses.KEY_RIGHT, ord("l")):
            cursor_expand(True); redraw(); present()
        elif ch in (curses.KEY_LEFT, ord("h")):
            cursor_expand(False); redraw(); present()
        elif ch in (ord(" "), curses.KEY_ENTER, 10, 13):
            cursor_expand(None); redraw(); present()
        elif ch in (ord("g"), curses.KEY_HOME):
            cursor_move(-1000000); scroll_y = scroll_x = 0; redraw(); present()
        elif ch in (ord("G"), curses.KEY_END):
            cursor_move(1000000); redraw(); present()
        # raw viewport scroll for long content (e.g. the process list)
        elif ch == curses.KEY_NPAGE:
            scroll_y += max(1, stdscr.getmaxyx()[0] - 3); present()
        elif ch == curses.KEY_PPAGE:
            scroll_y -= max(1, stdscr.getmaxyx()[0] - 3); present()
        elif ch in (ord("<"), ord(",")):
            scroll_x -= SCROLL_STEP * 3; present()
        elif ch in (ord(">"), ord(".")):
            scroll_x += SCROLL_STEP * 3; present()
        elif ch == curses.KEY_MOUSE:
            try:
                _mid, mx, my, _mz, bstate = curses.getmouse()
            except curses.error:
                bstate = 0
                mx = my = -1
            wheel_up = getattr(curses, "BUTTON4_PRESSED", 0)
            wheel_dn = getattr(curses, "BUTTON5_PRESSED", 0)
            if wheel_up and (bstate & wheel_up):
                scroll_y -= SCROLL_STEP; present()
            elif wheel_dn and (bstate & wheel_dn):
                scroll_y += SCROLL_STEP; present()
            elif bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
                # screen coords -> pad coords (content starts at screen row 1)
                if 1 <= my <= stdscr.getmaxyx()[0] - 2:
                    key = hit_target(mx + scroll_x, my - 1 + scroll_y)
                    if key:
                        cursor_key = key          # move selection to the click
                        cursor_expand(None)       # and toggle it
                        redraw(); present()

        now = time.time()
        if now - last >= REFRESH:
            last = now
            # the only place that runs the (slow) samplers; redraw() reuses these
            cache["cpu"] = node.sample_cpu()
            cache["jobs"] = get_jobs()
            cache["sq"] = get_squeue()
            cache["nodes"] = get_nodes()
            cache["procs"] = node.top_procs(NODE_PROC_ROWS)
            footer = (" q quit  r refresh  u usage  d du  ↑↓ select  ←→ open/close"
                      "  updates %ds " % int(REFRESH))
            if snapshot:
                stdscr.clearok(True)   # next present() fully clears + repaints
            redraw(); present()

        time.sleep(0.1)


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
