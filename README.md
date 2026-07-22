# agent-smith

A read-only terminal dashboard for keeping an eye on your Claude Code activity
and the compute node you're working on — all in one `curses` UI, refreshed on a
timer. Six panels:

1. **Usage** — your Claude usage limits (the same percentages Claude Code shows
   in settings: session / 5-hour and weekly), fetched live from the Anthropic
   usage endpoint with your own OAuth token. Each bar also carries a **pace
   marker** — an orange dashed tick placed where the elapsed fraction of
   that limit's time window falls — so a glance tells you whether usage is
   outrunning the clock (fill past the marker) or has headroom behind it.
   Every row shows **when that window resets** as an absolute local
   wall-clock time with weekday and date (e.g. `resets Fri Jul 11, 3pm`)
   and the countdown in parens
   (`(4d 4h)`), so you know both the moment usage comes back and how long
   that is. When a limit is **exhausted** (100%), the row turns red, the
   pace marker **freezes** where it stood when you ran out (pace no longer
   applies), and the cue reads **LIMIT REACHED** next to that reset time.
2. **Claude agents** — every Claude Code background job: name, live status,
   lifetime **token count** and **theoretical cost** (what those tokens would
   run to at pay-as-you-go Opus API rates — you're on a subscription, so it's an
   awareness estimate, not a bill), a **cmp** count of how many times the agent
   was restarted-from-summary (auto-compacted), the **model** it is currently
   running (e.g. `opus-4.8`, `haiku-4.5` — taken from the most recent turn, since
   a session can switch models mid-run), when it last updated, and what it's
   doing. Tokens, cost, and model are all read per turn from each agent's session
   transcript, pricing the full input / output / cache-read / cache-write split
   separately — not the single rolled-up number in `state.json`, which is a
   context-window snapshot (it drops on every compaction), not a lifetime total.
   (`state.json` carries no model at all — only the transcript records it.)
   The panel header sums tokens and cost across all agents. Read from
   `~/.claude/jobs/*/state.json` and `~/.claude/projects/*/<session>.jsonl`.
3. **SLURM** — your `squeue --me` jobs. Jobs that are actually placed get
   their own row showing the node(s) they're running on; pending jobs are
   collapsed into a single in-queue count with a per-reason breakdown.
4. **Nodes** — every compute node's SLURM state (idle / allocated / mixed /
   down / drained), a category tally in the header (`7 alloc · 5 mix · 3 idle
   · 24 down`), and — for the busy ones — who's running on them (multiple
   users on a shared node are shown `userA/userB`). **Your** nodes are pinned to
   the very top and highlighted; **click any busy node** (or the `▸`) to drill it
   open into a per-user core breakdown, so you see who holds what without wading
   through a wall of individual jobs. From `sinfo` + `squeue`, timeout-guarded.
5. **Storage** — a usage bar per shared cluster filesystem (every `/clusterfs`
   pool this node can see, auto-discovered from `/proc/mounts`). Each pool is
   `df`'d on a background thread with a per-mount timeout, so a slow or hung NFS
   mount renders `(unreachable)` instead of freezing the dashboard. Bars are
   colored by fill (green / yellow / red) so you can spot a pool about to fill up
   at a glance.
6. **Node** — htop-style CPU / memory / load / GPU plus the top processes for
   whatever machine it's running on.

It is **read-only**: it never modifies jobs, files, or your token. Pure Python
**standard library** — no `pip install` — and runs on **Python 3.6+**.

## Example

```
 Agent Smith — node07.example                                              2026-06-24 12:00:00

  USAGE                                                                                    8s ago
  Session (5h)    ###############|########..............  62%  resets Wed Jun 25, 3pm (3h 00m)  +22% over pace
  Weekly (all)    ########.......|......................  20%  resets Sat Jun 28, 7am (4d 05h)  20% headroom
  Weekly (Opus)   ###################|..................  50%  resets Fri Jun 27, 8pm (3d 12h)  on pace

  CLAUDE AGENTS [+]                                       5 · 117.7M tok · ~$83 at API rates
    name                 status       tokens    cost cmp model     updated    detail
* analysis-dashboard   running       24.6M    ~$18   2 opus-4.8  3s ago     add a color legend to the status column
  data-pipeline        needs-prompt   5.1M   ~$4.2     sonnet-5  4m ago     which reference dataset should I use?
  nightly-report       done          88.0M    ~$61   5 haiku-4.5 1h ago     report written to ./out/summary.md
    ▾ 2 more (click to expand)

  SLURM (squeue --me)                                                          2 on nodes · 3 queued
  jobid       name                   state      time        nodes
  1234567     train                   RUNNING    02:14:03    node07.example
  1234571     segment                 COMPLETING 00:31:55    node07.example
  in queue: 3   (Priority 2, Resources 1)

  STORAGE (cluster filesystems)                                                            12s ago
  nvme   ##################################......  84%  258T/306T   48T free
  nvme2  ####..................................... 10%   29T/282T  253T free
  abc2   #######################.................. 59%  2.0P/3.4P  1.4P free
  vast   ############............................  29%  309T/1.0P  750T free

  NODE: node07.example                                                                     64 cores
  CPU   ####..................................  11.0%
  MEM   ######................................  15.0%  96/640 GB
  load: 6.40 5.10 4.80   (1/5/15m)   procs: 3/2210

  pid     user         cpu%       mem  command
  204815  alice         98.4     1240M  python
  204902  bob           42.1      512M  matlab
  ...
 ▶ q quit  r refresh  u usage  d du  ↑↓ select  ←→ open/close  updates 2s
```

(Bars render as solid Unicode blocks in a real terminal; shown here as `#`/`.`
so the example is plain ASCII. The pace marker is an orange dashed tick (`╎`),
shown here as `|`. Status colors: green `done`, yellow `running`,
red `needs-prompt`, cyan `idle`. The `[+]` after a panel title (and the
`▾ N more` line) expands that panel to show every row — click it, or press
`1`/`2`/`3`/`4` for Agents/SLURM/Nodes/Node, or use the `▶` cursor (↑↓ to
select, →← to open/close); `[-]` / `▴` collapses. All
names/users/values above are fictional.)

## Requirements

- **Linux.** The usage panel reads your OAuth token from
  `~/.claude/.credentials.json`, which is how Claude Code stores it on Linux.
  On macOS the token lives in the Keychain, so the usage panel shows "no token"
  there — the other three panels still work.
- **Python 3.6+** (standard library only).
- *Optional:* SLURM (`squeue`) and NVIDIA (`nvidia-smi`). If either is missing,
  that panel just shows "unavailable" / hides the GPU rows — it never crashes.

## Usage

```sh
python3 agent-smith.py
```

It's a full-screen app, so run it in its own terminal (or tab/pane), **not**
inside an active Claude Code session.

On an HPC cluster, the **Node** panel reflects the machine it runs on, so launch
it on the node you want to watch:

```sh
srun --jobid=<your-running-job> --pty bash   # get a shell on that node
python3 agent-smith.py
```

For a view that survives disconnects, run it under `tmux`:

```sh
tmux new -s smith 'python3 agent-smith.py'
# detach: Ctrl-b d   reattach: tmux attach -t smith
```

**Controls:** a selection cursor (`▶`) on the left marks the current expandable
item. **↑ / ↓** move it between the panels (and individual busy nodes); **→**
opens the selected item, **←** closes it, and `Space` toggles. `g` / `G` jump the
cursor to the top / bottom, and the viewport **auto-follows** it so nothing you
select is ever off-screen. For long content (like the process list) `PageUp` /
`PageDown` and the mouse wheel raw-scroll, and `<` / `>` scroll sideways when a
row is wider than the terminal — so nothing is ever cut off and lost. You can
still **click** any `[+]` / `▾ N more` / node row directly, or press
`1`/`2`/`3`/`4` to toggle the Agents / SLURM / Nodes / Node panels. Mouse needs a
terminal with reporting on (under `tmux`, `set -g mouse on`); the keys always
work. `q` quits, `r` forces a full refresh, and the window resizes automatically.

Two on-demand keys avoid expensive work until you ask: **`u`** forces an immediate
Usage re-fetch (instead of waiting out the 2-minute timer), and **`d`** kicks off
a `du` of your own footprint on every cluster pool — slow (a multi-TB tree takes a
while), so it runs in the background and paints a magenta overlay + a
`you N.NT (age)` note onto each Storage bar as each finishes.

## How it adapts to each user

Nothing is hardcoded — run it as anyone, on any node:

- **Node:** `socket.gethostname()`.
- **You:** `squeue --me` and your `$HOME`.
- **Your Claude data:** `~/.claude` (and it honors `$CLAUDE_CONFIG_DIR` if you've
  relocated your config). Each user sees their own usage, jobs, and token.

## Notes

- The status column is derived from each job's `tempo` (real-time activity)
  with `state` as a fallback, since `state` can lag a turn behind.
- The Anthropic usage endpoint is best-effort; if it changes or rate-limits, the
  panel shows the last good value (flagged "stale") and keeps going.
- Your token is read at runtime and sent only in the request header (via
  `urllib`); it is never logged, printed, or written to disk.

## License

BSD-2-Clause — see [LICENSE](LICENSE). Copyright (c) 2026, Advanced Bioimaging
Center, University of California, Berkeley.
