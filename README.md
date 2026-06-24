# agent-smith

A read-only terminal dashboard for keeping an eye on your Claude Code activity
and the compute node you're working on — all in one `curses` UI, refreshed on a
timer. Four panels:

1. **Usage** — your Claude usage limits (the same percentages Claude Code shows
   in settings: session / 5-hour and weekly), fetched live from the Anthropic
   usage endpoint with your own OAuth token. Each bar also carries a **pace
   marker** — an orange dashed tick placed where the elapsed fraction of
   that limit's time window falls — so a glance tells you whether usage is
   outrunning the clock (fill past the marker) or has headroom behind it.
2. **Claude agents** — every Claude Code background job: name, live status,
   token count, when it last updated, and what it's doing. Read from
   `~/.claude/jobs/*/state.json`.
3. **SLURM** — your jobs from `squeue --me`.
4. **Node** — htop-style CPU / memory / load / GPU plus the top processes for
   whatever machine it's running on.

It is **read-only**: it never modifies jobs, files, or your token. Pure Python
**standard library** — no `pip install` — and runs on **Python 3.6+**.

## Example

```
 Agent Smith — node07.example                                              2026-06-24 12:00:00

  USAGE                                                                                    8s ago
  Session (5h)    ###############|########..............  62%  resets in 3h 00m  +22% over pace
  Weekly (all)    ########.......|......................  20%  resets in 4d 05h  20% headroom
  Weekly (Opus)   ###################|..................  50%  resets in 3d 12h  on pace

  CLAUDE AGENTS                                                                            3 total
  name                   status         tokens updated     detail
* analysis-dashboard      running        12,840 3s ago      add a color legend to the status column
  data-pipeline           needs-prompt    8,102 4m ago      which reference dataset should I use?
  nightly-report          done           21,455 1h ago      report written to ./out/summary.md

  SLURM (squeue --me)                                                                       2 jobs
  jobid        name                   state     time         n    nodelist/reason
  1234567      train                   RUNNING   02:14:03     1    node07.example
  1234568      preprocess              PENDING   00:00:00     1    (Priority)

  NODE: node07.example                                                                     64 cores
  CPU   ####..................................  11.0%
  MEM   ######................................  15.0%  96/640 GB
  load: 6.40 5.10 4.80   (1/5/15m)   procs: 3/2210

  pid     user         cpu%       mem  command
  204815  alice         98.4     1240M  python
  204902  bob           42.1      512M  matlab
  ...
 q quit   r refresh   *=this session   updates 2s
```

(Bars render as solid Unicode blocks in a real terminal; shown here as `#`/`.`
so the example is plain ASCII. The pace marker is an orange dashed tick (`╎`),
shown here as `|`. Status colors: green `done`, yellow `running`,
red `needs-prompt`, cyan `idle`. All names/users/values above are fictional.)

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

**Controls:** `q` quit, `r` force a refresh. The window resizes automatically.

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
