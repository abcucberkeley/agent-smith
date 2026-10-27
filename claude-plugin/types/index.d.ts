export type LimitRow = {
  label: string
  percent: number
  severity: string
  resetsAt: string | null
  windowSeconds: number | null
}

export type UsageView = { limits: LimitRow[]; err: string | null; fetchedAt: number }

export type AgentRow = {
  id: string
  name: string
  status: string
  tone: 'run' | 'need' | 'done' | 'idle' | 'err'
  model: string
  ctx: number | null
  window: number
  updatedAt: string | null
  detail: string
  isSelf: boolean
}

export type SlurmRow = { id: string; name: string; state: string; time: string; where: string }
export type DiskRow = { mount: string; used: number; total: number }

export type ClusterView = {
  mode: 'local' | 'ssh' | 'off'
  slurm: SlurmRow[]
  disks: DiskRow[]
  err: string | null
  fetchedAt: number
}

declare module 'claude-code' {
  interface PluginState {
    'agent-smith': { usage: UsageView; agents: AgentRow[]; cluster: ClusterView }
  }
}
