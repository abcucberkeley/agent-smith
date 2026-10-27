import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { AgentRow, ClusterView, DiskRow, LimitRow, SlurmRow, UsageView } from '../types'

// Agent Smith as a Claude Code pane: the same read-only dashboard as
// ~/agent-smith.py, drawn beside the chat in the terminal or the desktop app.

const PANE = 'agent-smith'
const TITLE = 'Agent Smith'

const USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
const AGENTS_MS = 5000
const CLUSTER_MS = 30000
const USAGE_MS = 120000
const USAGE_BACKOFF_MS = 300000
const TAIL_BYTES = 3000000          // enough to hold an agent's latest request
const READ_LIMIT = 4000000          // $.fs.read refuses files over 4 MiB
const CONTEXT_WINDOW = 1000000      // current Opus/Sonnet/Fable sessions here
const CONTEXT_WINDOW_SMALL = 200000 // Haiku

const WINDOW_SECONDS: Record<string, number> = {
  session: 5 * 3600,
  weekly_all: 7 * 86400,
  weekly_scoped: 7 * 86400,
  weekly_opus: 7 * 86400,
}
const LIMIT_LABELS: Record<string, string> = {
  session: 'Session (5h)',
  weekly_all: 'Weekly (all)',
  weekly_scoped: 'Weekly',
  weekly_opus: 'Weekly (Opus)',
}

const usageAtom = atom({ plugin: 'agent-smith', key: 'usage' } as const,
  { limits: [], err: 'fetching...', fetchedAt: 0 } as UsageView)
const agentsAtom = atom({ plugin: 'agent-smith', key: 'agents' } as const, [] as AgentRow[])
const clusterAtom = atom({ plugin: 'agent-smith', key: 'cluster' } as const,
  { mode: 'off', slurm: [], disks: [], err: null, fetchedAt: 0 } as ClusterView)

// ---------------------------------------------------------------- helpers

const pad = (s: string, n: number) => (s.length >= n ? s.slice(0, n) : s + ' '.repeat(n - s.length))
const lpad = (s: string, n: number) => (s.length >= n ? s.slice(0, n) : ' '.repeat(n - s.length) + s)

function bar(pct: number, width: number): string {
  const p = Math.max(0, Math.min(100, pct))
  let filled = Math.floor((width * p) / 100)
  if (p > 0 && filled === 0) filled = 1
  return '█'.repeat(filled) + '░'.repeat(width - filled)
}

function ago(iso: string | null, now: number): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (isNaN(t)) return ''
  const s = Math.max(0, Math.round((now - t) / 1000))
  if (s < 60) return s + 's ago'
  if (s < 3600) return Math.floor(s / 60) + 'm ago'
  if (s < 86400) return Math.floor(s / 3600) + 'h ago'
  return Math.floor(s / 86400) + 'd ago'
}

function until(iso: string | null, now: number): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (isNaN(t)) return ''
  const s = Math.max(0, Math.round((t - now) / 1000))
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60)
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`
}

function shortModel(id: string | null | undefined): string {
  if (!id) return ''
  let s = id.startsWith('claude-') ? id.slice(7) : id
  s = s.replace(/-\d{6,}$/, '')
  const parts = s.split('-')
  return parts.length > 1 ? parts[0] + '-' + parts.slice(1).join('.') : parts[0]
}

function humanBytes(n: number): string {
  const u = ['B', 'K', 'M', 'G', 'T', 'P']
  let i = 0
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++ }
  return (n >= 10 || i === 0 ? n.toFixed(0) : n.toFixed(1)) + u[i]
}

// Status from state.json: `tempo` is the live signal; `state` lags a turn.
function agentStatus(j: any): { status: string; tone: AgentRow['tone'] } {
  const state = String(j.state || '').toLowerCase()
  const tempo = String(j.tempo || '').toLowerCase()
  if (tempo === 'active') return { status: 'running', tone: 'run' }
  if (tempo === 'blocked' || ['blocked', 'waiting', 'needs_input', 'needs-input', 'input'].includes(state))
    return { status: 'needs-prompt', tone: 'need' }
  if (state === 'error' || state === 'failed') return { status: state, tone: 'err' }
  if (['done', 'completed', 'complete', 'finished', 'exited'].includes(state)) return { status: 'done', tone: 'done' }
  if (tempo === 'idle') return { status: 'idle', tone: 'idle' }
  if (['working', 'running', 'active', 'in_progress'].includes(state)) return { status: 'running', tone: 'run' }
  return { status: state || tempo || '?', tone: 'idle' }
}

// ---------------------------------------------------------------- register

// module-level state (reset on hot reload; that only costs one re-read)
const S: {
  paths: { home: string; config: string; isWindows: boolean; selfJob: string } | null
  transcriptOf: Record<string, string>
  ctxCache: Record<string, { size: number; mtime: number; ctx: number | null; model: string | null }>
  usageWaitUntil: number
  clusterMode: ClusterView['mode'] | null
  clusterHost: string
} = { paths: null, transcriptOf: {}, ctxCache: {}, usageWaitUntil: 0, clusterMode: null, clusterHost: '' }

async function getPaths($: any) {
  if (S.paths) return S.paths
  const isWindows = (await $.env.get('OS')) === 'Windows_NT'
  const home = (await $.env.get('HOME')) || (await $.env.get('USERPROFILE')) || ''
  const config = (await $.env.get('CLAUDE_CONFIG_DIR')) || `${home}/.claude`
  const jobDir = (await $.env.get('CLAUDE_JOB_DIR')) || ''
  const selfJob = jobDir.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || ''
  S.paths = { home, config, isWindows, selfJob }
  return S.paths
}

// Last bytes of a (possibly huge) transcript. $.fs.read stops at 4 MiB.
async function tail($: any, path: string, size: number, isWindows: boolean): Promise<string> {
  if (size <= READ_LIMIT) return await $.fs.read(path)
  if (isWindows) {
    const p = path.replace(/'/g, "''")
    const ps =
      `$f=[IO.File]::Open('${p}','Open','Read','ReadWrite');` +
      `$n=[Math]::Min($f.Length,${TAIL_BYTES});$null=$f.Seek(-$n,'End');` +
      `$b=New-Object byte[] $n;$r=0;while($r -lt $n){$r+=$f.Read($b,$r,$n-$r)};$f.Close();` +
      `[Console]::Out.Write([Text.Encoding]::UTF8.GetString($b))`
    const r = await $.process.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], { timeoutMs: 20000 })
    return r.stdout
  }
  const r = await $.process.run(['tail', '-c', String(TAIL_BYTES), path], { timeoutMs: 20000 })
  return r.stdout
}

async function findTranscript($: any, config: string, sid: string): Promise<string | null> {
  if (S.transcriptOf[sid]) return S.transcriptOf[sid]
  const root = `${config}/projects`
  let dirs: any[] = []
  try { dirs = await $.fs.list(root) } catch { return null }
  for (const d of dirs) {
    if (d.kind !== 'directory') continue
    const p = `${root}/${d.name}/${sid}.jsonl`
    if (await $.fs.exists(p)) { S.transcriptOf[sid] = p; return p }
  }
  return null
}

// Context now = the latest main-thread request (input + cache read + cache write).
async function contextOf($: any, config: string, sid: string, isWindows: boolean) {
  const path = await findTranscript($, config, sid)
  if (!path) return { ctx: null, model: null }
  let st: any
  try { st = await $.fs.stat(path) } catch { return { ctx: null, model: null } }
  const c = S.ctxCache[path]
  if (c && c.size === st.size && c.mtime === st.mtimeMs) return c
  let text = ''
  try { text = await tail($, path, st.size, isWindows) } catch { return c || { ctx: null, model: null } }
  const lines = text.split('\n')
  if (st.size > READ_LIMIT) lines.shift()          // first line of a tail is partial
  let ctx: number | null = null, model: string | null = null
  for (let i = lines.length - 1; i >= 0 && (ctx === null || model === null); i--) {
    const line = lines[i]
    if (!line.includes('"usage"')) continue
    let e: any
    try { e = JSON.parse(line) } catch { continue }
    const m = e && e.message
    if (!m || typeof m !== 'object') continue
    if (model === null && m.model && m.model !== '<synthetic>') model = m.model
    if (ctx === null && !e.isSidechain && m.usage) {
      const u = m.usage
      ctx = (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0)
    }
  }
  const out = { size: st.size, mtime: st.mtimeMs, ctx, model }
  S.ctxCache[path] = out
  return out
}

async function refreshAgents($: any) {
  const { config, isWindows, selfJob } = await getPaths($)
  const jobsDir = `${config}/jobs`
  let entries: any[] = []
  try { entries = await $.fs.list(jobsDir) } catch { await update($, agentsAtom, () => []); return }
  const rows: AgentRow[] = []
  for (const ent of entries) {
    if (ent.kind !== 'directory') continue
    let j: any
    try { j = JSON.parse(await $.fs.read(`${jobsDir}/${ent.name}/state.json`)) } catch { continue }
    const { status, tone } = agentStatus(j)
    const sid = j.sessionId || j.resumeSessionId
    const cx = sid ? await contextOf($, config, sid, isWindows) : { ctx: null, model: null }
    const model = cx.model || ''
    rows.push({
      id: ent.name,
      name: String(j.name || ent.name),
      status, tone,
      model: shortModel(model),
      ctx: cx.ctx,
      window: model.includes('haiku') ? CONTEXT_WINDOW_SMALL : CONTEXT_WINDOW,
      updatedAt: j.updatedAt || null,
      detail: String(j.detail || j.needs || '').replace(/\s+/g, ' '),
      isSelf: ent.name === selfJob,
    })
  }
  rows.sort((a, b) => (Date.parse(b.updatedAt || '') || 0) - (Date.parse(a.updatedAt || '') || 0))
  await update($, agentsAtom, () => rows)
}

async function refreshUsage($: any, now: number) {
  if (now < S.usageWaitUntil) return
  let err: string | null = null
  let limits: LimitRow[] | null = null
  try {
    const auth = await $.session.authorize()
    if (!auth) err = 'not signed in'
    else {
      const r = await $.http.fetch(USAGE_URL, {
        headers: { 'anthropic-beta': 'oauth-2025-04-20' },
        auth: auth.handle,
      })
      let obj: any = null
      try { obj = JSON.parse(r.text) } catch { err = r.ok ? 'bad response' : `http ${r.status}` }
      if (obj && obj.error) {
        const t = String(obj.error.type || 'error')
        err = t === 'rate_limit_error' ? 'rate limited' : t.replace(/_/g, ' ')
        if (t === 'rate_limit_error') S.usageWaitUntil = now + USAGE_BACKOFF_MS
      } else if (obj) {
        limits = (obj.limits || []).map((l: any) => {
          const model = l.scope && l.scope.model && l.scope.model.display_name
          const label = model && l.kind === 'weekly_scoped' ? `Weekly (${model})` : LIMIT_LABELS[l.kind] || l.kind
          return {
            label,
            percent: Number(l.percent || 0),
            severity: String(l.severity || 'normal'),
            resetsAt: l.resets_at || null,
            windowSeconds: WINDOW_SECONDS[l.kind] || null,
          }
        })
      }
    }
  } catch (e: any) {
    err = 'network error'
  }
  await update($, usageAtom, prev => ({
    limits: limits || prev.limits,            // keep the last good bars while failing
    err,
    fetchedAt: limits ? now : prev.fetchedAt,
  }))
}

const CLUSTER_SCRIPT =
  "squeue --me --noheader -o '%i|%j|%T|%M|%R' 2>/dev/null; echo @@DF; " +
  'timeout 8 df -P -B1 /clusterfs/* 2>/dev/null'

async function refreshCluster($: any, now: number) {
  const { isWindows } = await getPaths($)
  if (S.clusterMode === null) {
    let local = false
    if (!isWindows) {
      try { local = (await $.process.run(['squeue', '--version'], { timeoutMs: 5000 })).exitCode === 0 } catch {}
    }
    S.clusterMode = local ? 'local' : S.clusterHost ? 'ssh' : 'off'
  }
  if (S.clusterMode === 'off') {
    await update($, clusterAtom, p => ({ ...p, mode: 'off' as const }))
    return
  }
  const argv = S.clusterMode === 'local'
    ? ['bash', '-c', CLUSTER_SCRIPT]
    : ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6', S.clusterHost, CLUSTER_SCRIPT]
  let out = ''
  let err: string | null = null
  try {
    const r = await $.process.run(argv, { timeoutMs: 25000 })
    out = r.stdout
    if (!out.includes('@@DF')) err = (r.stderr || 'no reply').trim().split('\n').pop() || 'no reply'
  } catch (e: any) {
    err = S.clusterMode === 'ssh' ? 'ssh failed' : 'squeue failed'
  }
  if (err) {
    await update($, clusterAtom, p => ({ ...p, mode: S.clusterMode as ClusterView['mode'], err }))
    return
  }
  const [sq, df] = out.split('@@DF')
  const slurm: SlurmRow[] = sq.split('\n').filter(l => l.includes('|')).map(l => {
    const [id, name, state, time, where] = l.split('|')
    return { id, name, state, time, where }
  })
  const disks: DiskRow[] = []
  for (const l of (df || '').split('\n').slice(1)) {
    const f = l.trim().split(/\s+/)
    if (f.length < 6) continue
    const total = Number(f[1]), used = Number(f[2])
    if (!total) continue
    disks.push({ mount: f[5].split('/').pop() || f[5], used, total })
  }
  await update($, clusterAtom, () => ({ mode: S.clusterMode as ClusterView['mode'], slurm, disks, err: null, fetchedAt: now }))
}

export const register: Register = (on, options) => {
  S.clusterHost = String((options as any)?.cluster_host || '').trim()

  // ------------------------------------------------------------ lifecycle

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await $.command.register({ name: 'smith', description: 'Open the Agent Smith dashboard pane' })
    void $.ui.open({ id: PANE, title: TITLE })

    const tickAgents = async () => { try { await refreshAgents($) } catch {} }
    const tickUsage = async () => { try { await refreshUsage($, await $.clock.now()) } catch {} }
    const tickCluster = async () => { try { await refreshCluster($, await $.clock.now()) } catch {} }
    void tickAgents(); void tickUsage(); void tickCluster()
    $.clock.every(AGENTS_MS, tickAgents)
    $.clock.every(USAGE_MS, tickUsage)
    $.clock.every(CLUSTER_MS, tickCluster)
    return started
  })

  on('command.run', { command: 'smith' }, async $ => {
    await $.ui.open({ id: PANE, title: TITLE })
    return { text: 'Agent Smith pane opened.' }
  })

  // ------------------------------------------------------------ drawing

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const now = await $.clock.now()
    const usage = await read($, usageAtom)
    const agents = await read($, agentsAtom)
    const cluster = await read($, clusterAtom)
    const cols = Math.max(40, (e.props as any)?.bodyColumns ?? e.viewport?.columns ?? 100)
    const rows = Math.max(12, (e.props as any)?.scroll?.bodyRows ?? e.viewport?.rows ?? 40)

    const head = (title: string, right = '') => (
      <Text mono bold color="claude" wrap="truncate">{pad(title, Math.max(0, cols - right.length - 1)) + right}</Text>
    )
    const sevColor = (sev: string, pct: number) =>
      pct >= 100 || sev === 'exceeded' || sev === 'critical' ? 'error' : sev === 'warning' || sev === 'approaching' ? 'warning' : 'success'
    const toneColor: Record<string, string> = { run: 'warning', need: 'error', err: 'error', done: 'success', idle: 'suggestion' }

    // usage
    const barW = Math.max(10, Math.min(30, cols - 40))
    const usageRight = usage.err ? (usage.limits.length ? `stale: ${usage.err}` : usage.err) : ago(new Date(usage.fetchedAt).toISOString(), now)
    const usageRows = usage.limits.map(l => {
      let b = bar(l.percent, barW)
      if (l.windowSeconds && l.resetsAt) {
        const left = (Date.parse(l.resetsAt) - now) / 1000
        const frac = Math.max(0, Math.min(1, (l.windowSeconds - left) / l.windowSeconds))
        const at = Math.max(0, Math.min(barW - 1, Math.round(barW * frac)))
        b = b.slice(0, at) + '│' + b.slice(at + 1)
      }
      const r = until(l.resetsAt, now)
      return (
        <Text mono wrap="truncate">
          <Text dimColor>{pad(l.label, 15)}</Text>
          <Text color={sevColor(l.severity, l.percent)}>{b}</Text>
          <Text>{lpad(Math.round(l.percent) + '%', 5)}</Text>
          <Text dimColor>{r ? '  resets in ' + r : ''}</Text>
        </Text>
      )
    })

    // agents
    const clusterLines = cluster.mode === 'off' ? 0 : Math.min(cluster.slurm.length, 6) + cluster.disks.length + 4
    const agentRoom = Math.max(3, rows - (usageRows.length + 3) - clusterLines - 4)
    const shown = agents.slice(0, agentRoom)
    const nameW = 20, statW = 13, modW = 10, ctxW = 12, updW = 8
    const detW = Math.max(0, cols - (2 + nameW + statW + modW + ctxW + updW))
    const agentHeader = '  ' + pad('name', nameW) + pad('status', statW) + pad('model', modW) + pad('context', ctxW) + pad('updated', updW) + (detW > 6 ? 'detail' : '')
    const agentRows = shown.map(a => {
      const pct = a.ctx ? (100 * a.ctx) / a.window : null
      const cc = pct === null ? 'inactive' : pct >= 80 ? 'error' : pct >= 50 ? 'warning' : 'success'
      return (
        <Text mono wrap="truncate">
          <Text color="claude" bold>{a.isSelf ? '* ' : '  '}</Text>
          <Text bold>{pad(a.name, nameW - 1) + ' '}</Text>
          <Text color={toneColor[a.tone]}>{pad(a.status, statW)}</Text>
          <Text color="suggestion">{pad(a.model, modW)}</Text>
          <Text color={cc}>{pct === null ? pad('-', ctxW) : bar(pct, 6) + lpad(Math.min(100, Math.floor(pct)) + '%', 5) + ' '}</Text>
          <Text dimColor>{pad(ago(a.updatedAt, now), updW)}</Text>
          <Text dimColor>{detW > 6 ? a.detail.slice(0, detW) : ''}</Text>
        </Text>
      )
    })
    const running = agents.filter(a => a.tone === 'run').length
    const needs = agents.filter(a => a.tone === 'need').length

    // cluster
    const clusterBlock: any[] = []
    if (cluster.mode !== 'off') {
      const where = cluster.mode === 'ssh' ? `via ssh ${S.clusterHost}` : 'local'
      const running = cluster.slurm.filter(s => s.state === 'RUNNING')
      const pending = cluster.slurm.length - running.length
      clusterBlock.push(<Text> </Text>)
      clusterBlock.push(head(' SLURM', `${running.length} running · ${pending} queued · ${cluster.err ? 'error: ' + cluster.err : where} `))
      for (const s of running.slice(0, 6)) {
        clusterBlock.push(
          <Text mono wrap="truncate">
            <Text dimColor>{'  ' + pad(s.id, 14)}</Text>
            <Text>{pad(s.name, 18)}</Text>
            <Text color="warning">{pad(s.time, 12)}</Text>
            <Text color="suggestion">{s.where}</Text>
          </Text>,
        )
      }
      if (running.length > 6) clusterBlock.push(<Text mono dimColor>{`  +${running.length - 6} more running`}</Text>)
      clusterBlock.push(head(' STORAGE', ''))
      const dW = Math.max(10, Math.min(30, cols - 36))
      for (const d of cluster.disks) {
        const pct = (100 * d.used) / d.total
        const c = pct >= 95 ? 'error' : pct >= 85 ? 'warning' : 'success'
        clusterBlock.push(
          <Text mono wrap="truncate">
            <Text dimColor>{'  ' + pad(d.mount, 12)}</Text>
            <Text color={c}>{bar(pct, dW)}</Text>
            <Text>{lpad(Math.round(pct) + '%', 5)}</Text>
            <Text dimColor>{`  ${humanBytes(d.used)} / ${humanBytes(d.total)}`}</Text>
          </Text>,
        )
      }
    }

    return (
      <Box flexDirection="column">
        {head(' USAGE', usageRight + ' ')}
        {usageRows.length ? usageRows : <Text mono dimColor>{'  ' + (usage.err || 'fetching...')}</Text>}
        <Text> </Text>
        {head(' CLAUDE AGENTS', `${agents.length} · ${running} running${needs ? ` · ${needs} need you` : ''} `)}
        <Text mono dimColor wrap="truncate">{agentHeader}</Text>
        {agentRows.length ? agentRows : <Text mono dimColor>  no background agents</Text>}
        {agents.length > shown.length && <Text mono dimColor>{`  +${agents.length - shown.length} more`}</Text>}
        {clusterBlock}
      </Box>
    )
  })
}
