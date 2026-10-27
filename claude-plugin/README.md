# Agent Smith — Claude Code plugin

The Agent Smith dashboard as a pane beside the chat, in a Claude Code terminal
session or the Code tab of the Claude desktop app (Windows, macOS, Linux).
Read-only: it only reads and displays.

What the pane shows:

- **Usage** — your Claude usage limits (session and weekly), with a marker for
  how much of each window's time has passed and when it resets. The request goes
  through Claude Code's own sign-in; the plugin never sees your token.
- **Claude agents** — every Claude Code background job on this machine: status,
  model, a **context** bar (how full its context window is now: green under 50%,
  yellow to 80%, red above, which means a compaction is coming), when it last
  updated and what it is doing.
- **SLURM and storage** — your `squeue --me` jobs and a usage bar per
  `/clusterfs/*` filesystem. Shown automatically when Claude Code runs on the
  cluster. From another computer (for example a microscope PC), set the
  **Cluster SSH host** option to `user@host`; that computer needs an SSH key that
  logs in without a password. Left empty, these sections are hidden.

Open it with `/smith` (it also opens by itself when a session starts).

## Install

In the Claude desktop app: click **+** next to the prompt box, then **Plugins**,
**Add marketplace** with `abcucberkeley/agent-smith`, then **Add plugin**.

At the prompt of a terminal session:

```
/plugin install agent-smith --marketplace abcucberkeley/agent-smith
```

From a shell:

```
claude plugin install agent-smith --marketplace abcucberkeley/agent-smith
```

Answer `y` to add the marketplace, then pick the user scope. To set the cluster
host later: `/config`, or `claude plugin configure agent-smith`.

## Notes

- Context fill = the size of the agent's latest request over its window (1M
  tokens; 200k for Haiku). Transcripts over 4 MB are read from their end with
  `tail` (Linux/macOS) or PowerShell (Windows).
- Not yet ported from the terminal dashboard: the per-node panel, the NODES
  panel and the per-agent cost column.
