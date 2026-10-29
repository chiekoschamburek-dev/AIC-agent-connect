import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  AgentKind, AgentState, DiscoveredAgent, DomainCharter, ProductState, Scenario, VizEvent,
} from './types'

export interface UIAgentTask {
  id: string
  state: AgentState
  note?: string
}

export interface UIAgent {
  id: string
  name: string
  role: 'supervisor' | 'partner'
  kind: AgentKind // 资源 agent 与工作流伙伴分区展示；旧数据缺省 workflow
  org: string
  source: string
  skills: string[]
  state: AgentState
  note?: string
  // 任务级状态表（state 事件带 taskId 时逐任务记录）：同一 agent 并发多个任务
  // 时按任务归因聚合显示，不再按到达序互相覆盖（2026-10-27 审查）
  tasks: UIAgentTask[]
  metrics: { key: string; value: string }[]
  upAt: number
}

export interface UIEdge {
  id: string
  from: string
  to: string
  kind: 'call' | 'grant' | 'delegation'
  label?: string
  gate?: { decision: 'allow' | 'deny'; rule: string }
  domain?: string
  broken?: boolean
  maxUses?: number
  // 资源 agent 的使用关系（workspace 等）：用暖色虚线区分普通委派
  resource?: boolean
  uses?: number
}

export interface UIDomain {
  id: string
  status: 'active' | 'frozen'
  revision: number
  reconciledUses?: number
  // 旧录制数据没有章程明细；此时只画摘要卡，详情抽屉提示"未上传章程明细"
  charter?: DomainCharter
}

export interface UIProduct {
  name: string
  agent: string
  state: ProductState
}

// 发现候选池：按 AIC 去重，记录它最近一次被哪条 discover 查询发现。
export type UIDiscovered = DiscoveredAgent & { query: string; t: number }

export interface UIState {
  instruction: string
  plan: { id: string; title: string }[]
  agents: Record<string, UIAgent>
  edges: Record<string, UIEdge>
  domains: Record<string, UIDomain>
  discovered: UIDiscovered[]
  products: Record<string, UIProduct>
  ledger: VizEvent[]
  doneSummary?: string
  doneStatus?: 'done' | 'failed'
}

export const emptyState = (instruction = ''): UIState => ({
  instruction,
  plan: [],
  agents: {},
  edges: {},
  domains: {},
  discovered: [],
  products: {},
  ledger: [],
})

// 任务级状态聚合：任一失败标红，有在跑的就算工作中，全部完成才绿
function aggregateTaskState(tasks: UIAgentTask[]): AgentState | null {
  if (tasks.length === 0) return null
  if (tasks.some((t) => t.state === 'failed' || t.state === 'rejected')) return 'failed'
  if (tasks.some((t) => t.state === 'working')) return 'working'
  if (tasks.some((t) => t.state === 'awaiting-input')) return 'awaiting-input'
  if (tasks.some((t) => t.state === 'awaiting-completion')) return 'awaiting-completion'
  if (tasks.some((t) => t.state === 'planning' || t.state === 'discovering')) return 'planning'
  return 'completed'
}

// ── 资源 agent（workspace 等）的使用关系 ────────────────────────────────
// 资源 agent 不走 AIP 委派，旧录制数据里只有 ws_zone/ws_file，没有边也没有状态
// 变化——画布上只剩一个孤悬的节点（2026-10-28 用户实测）。新录制数据在事件上带
// by/resource；旧数据退化为"supervisor → 唯一的资源节点"（supervisor 确实是
// 发起工作区操作的那一方）。
function findAgent(state: UIState, ref?: string): UIAgent | undefined {
  if (!ref) return undefined
  if (state.agents[ref]) return state.agents[ref]
  const low = ref.toLowerCase()
  return Object.values(state.agents).find(
    (a) => a.id.toLowerCase() === low || a.name.toLowerCase() === low,
  )
}

function resourceAgentOf(state: UIState, ref?: string): UIAgent | undefined {
  const found = findAgent(state, ref)
  if (found) return found
  return Object.values(state.agents).find((a) => a.kind === 'resource')
}

function callerAgentOf(state: UIState, ref?: string): UIAgent | undefined {
  const found = findAgent(state, ref)
  if (found) return found
  return Object.values(state.agents).find((a) => a.role === 'supervisor')
}

function linkResourceUse(
  state: UIState,
  caller: UIAgent,
  resource: UIAgent,
  skill?: string,
): UIState {
  if (caller.id === resource.id) return state
  const id = `ws:${caller.id}->${resource.id}`
  const prev = state.edges[id]
  // 已经有一条同方向的实线（真的把 workspace 当 agent 委派）时不再叠一条，
  // 否则同一对节点会被两条几乎重合的线盖住。
  if (!prev && Object.values(state.edges).some((e) => e.from === caller.id && e.to === resource.id)) {
    return state
  }
  return {
    ...state,
    edges: {
      ...state.edges,
      [id]: {
        id,
        from: caller.id,
        to: resource.id,
        kind: 'call',
        label: prev?.label ?? (skill ? `工作区 · ${skill}` : '工作区使用'),
        resource: true,
        uses: (prev?.uses ?? 0) + 1,
      },
    },
  }
}

function markResourceWorking(
  state: UIState,
  resource: UIAgent,
  taskId?: string,
  note?: string,
): UIState {
  const tasks = taskId
    ? [...resource.tasks.filter((t) => t.id !== taskId), { id: taskId, state: 'working' as const, note }]
    : resource.tasks
  return {
    ...state,
    agents: {
      ...state.agents,
      [resource.id]: { ...resource, state: 'working', note, tasks },
    },
  }
}

function applyEvent(state: UIState, e: VizEvent): UIState {
  const s: UIState = { ...state, ledger: [...state.ledger, e] }
  switch (e.type) {
    case 'instruction':
      return { ...s, instruction: e.text }
    case 'plan':
      return { ...s, plan: e.subtasks }
    case 'agent_up': {
      const a = e.agent
      return {
        ...s,
        agents: {
          ...s.agents,
          [a.id]: {
            id: a.id, name: a.name, role: a.role, kind: a.kind ?? 'workflow', org: a.org, source: a.source,
            skills: a.skills ?? [], state: 'idle', tasks: [], metrics: [], upAt: e.t,
          },
        },
      }
    }
    case 'discover': {
      // 候选池按 AIC 去重：同一候选被多条 query 命中时保留最近一次查询与完整能力集。
      const byAic = new Map(s.discovered.map((x) => [x.aic, x]))
      for (const result of e.results ?? []) {
        byAic.set(result.aic, { ...result, query: e.query, t: e.t })
      }
      return { ...s, discovered: [...byAic.values()] }
    }
    case 'state': {
      const ag = s.agents[e.agent]
      if (!ag) return s
      // 带 taskId 的事件按任务归因：节点显示聚合态（任一失败标红、全完成才绿），
      // 任务之间不再互相覆盖；无 taskId 的事件（初始 planning、run 收尾）仍是
      // agent 级直接状态
      const tasks = e.taskId
        ? [...ag.tasks.filter((t) => t.id !== e.taskId), { id: e.taskId, state: e.state, note: e.note }]
        : ag.tasks
      const next = e.taskId ? (aggregateTaskState(tasks) ?? e.state) : e.state
      return { ...s, agents: { ...s.agents, [e.agent]: { ...ag, state: next, note: e.note, tasks } } }
    }
    case 'delegate': {
      const id = `call:${e.from}->${e.to}:${e.taskId}`
      return {
        ...s,
        edges: {
          ...s.edges,
          [id]: { id, from: e.from, to: e.to, kind: 'call', label: e.label ?? e.skill },
        },
      }
    }
    case 'token': {
      const id = `token:${e.kind}:${e.issuer}->${e.holder}:${e.t}`
      return {
        ...s,
        edges: {
          ...s.edges,
          [id]: {
            id, from: e.issuer, to: e.holder, kind: e.kind,
            label: e.label, domain: e.domain, maxUses: e.maxUses,
          },
        },
      }
    }
    case 'pep': {
      const id = `pep:${e.caller}->${e.target}`
      return {
        ...s,
        edges: {
          ...s.edges,
          [id]: {
            id, from: e.caller, to: e.target, kind: 'call',
            label: e.skill, gate: { decision: e.decision, rule: e.rule },
            domain: e.domain, broken: false,
          },
        },
      }
    }
    case 'domain': {
      if (e.action === 'create') {
        return {
          ...s,
          domains: {
            ...s.domains,
            [e.domainId]: {
              id: e.domainId, status: 'active', revision: e.revision ?? 1,
              charter: e.charter ?? s.domains[e.domainId]?.charter,
            },
          },
        }
      }
      const d = s.domains[e.domainId]
      if (!d) return s
      if (e.action === 'freeze') {
        const edges = Object.fromEntries(
          Object.entries(s.edges).map(([k, v]) => [k, v.domain === e.domainId ? { ...v, broken: true } : v]),
        )
        return { ...s, edges, domains: { ...s.domains, [e.domainId]: { ...d, status: 'frozen', revision: e.revision ?? d.revision + 1 } } }
      }
      if (e.action === 'unfreeze' || e.action === 'revision') {
        return {
          ...s,
          domains: {
            ...s.domains,
            [e.domainId]: {
              ...d, status: 'active', revision: e.revision ?? d.revision + 1,
              charter: e.charter ?? d.charter,
            },
          },
        }
      }
      return s
    }
    case 'product':
      return { ...s, products: { ...s.products, [e.name]: { name: e.name, agent: e.agent, state: e.state } } }
    case 'ws_zone': {
      const resource = resourceAgentOf(s, e.resource)
      const caller = callerAgentOf(s, e.by)
      let next = s
      if (resource && caller) next = linkResourceUse(next, caller, resource, e.skill)
      if (resource) {
        next = markResourceWorking(next, resource, `ws-${e.task}`,
          e.skill ? `工作区操作：${e.skill}` : '任务工作区使用中')
      }
      return next
    }
    case 'ws_file': {
      // 四个存储区面板已移除（2026-10-28：后端工作区内容无法可靠同步到前端）。
      // 事件仍用于资源连线/状态与台账；这里不再维护文件列表。
      const resource = resourceAgentOf(s, e.resource)
      let next = s
      if (resource) {
        const caller = callerAgentOf(s, e.by)
        if (caller) next = linkResourceUse(next, caller, resource)
        next = markResourceWorking(next, resource,
          e.task ? `ws-${e.task}` : undefined, `工作区写入：${e.path}`)
      }
      return next
    }
    case 'metric': {
      const ag = s.agents[e.agent]
      if (!ag) return s
      return { ...s, agents: { ...s.agents, [e.agent]: { ...ag, metrics: [...ag.metrics.slice(-4), { key: e.key, value: e.value }] } } }
    }
    case 'reconcile': {
      const d = s.domains[e.domain]
      if (!d) return s
      return { ...s, domains: { ...s.domains, [e.domain]: { ...d, reconciledUses: e.totalUses } } }
    }
    case 'done': {
      // 收尾兜底：任务已结束，还挂在 working/planning 的伙伴按后端终态收口。
      // 失败的 run 不能把没交付的伙伴翻成"已完成"——那会把失败伪装成成功
      // （2026-10-27 审查；此前这里无条件翻 completed）。
      const failed = e.status === 'failed'
      const agents = { ...s.agents }
      for (const [id, ag] of Object.entries(agents)) {
        if (ag.state === 'working' || ag.state === 'planning') {
          agents[id] = {
            ...ag,
            state: failed ? 'failed' : 'completed',
            note: failed ? 'run 失败，未确认交付' : ag.note,
            tasks: ag.tasks.map((t) =>
              t.state === 'working' || t.state === 'planning'
                ? { ...t, state: failed ? 'failed' : 'completed' }
                : t),
          }
        }
      }
      return { ...s, agents, doneSummary: e.summary, doneStatus: e.status ?? 'done' }
    }
    default:
      return s
  }
}

export function computeStateAt(scenario: Scenario, time: number): UIState {
  let s = emptyState(scenario.instruction)
  for (const a of scenario.agents.filter((x) => x.role === 'supervisor')) {
    s = applyEvent(s, { t: -1, type: 'agent_up', agent: a })
  }
  for (const e of scenario.events) {
    if (e.t <= time) s = applyEvent(s, e)
    else break
  }
  return s
}

export function scenarioDuration(scenario: Scenario): number {
  return scenario.events.reduce((m, e) => Math.max(m, e.t), 0) + 1
}

export function usePlayer(
  scenario: Scenario,
  liveStart: number | null = null,
  liveDone = false,
) {
  const baseDuration = useMemo(() => scenarioDuration(scenario), [scenario])
  const [time, setTime] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const [now, setNow] = useState(() => Date.now())
  // 实时模式的"审视时刻"：非 null 时挂钟覆盖 effect 让位，画面停在该时刻，
  // 点 backToLive 恢复跟随。否则台账点击 ≤250ms 内就会被挂钟拉回，
  // 面板宣称的"点击跳转"在实时模式形同虚设（2026-10-27 审查）。
  const [inspect, setInspect] = useState<number | null>(null)
  const lastRef = useRef<number>(0)

  // 实时模式用挂钟推进时间轴：即使长时间没有新事件（agent 正在跑一个长工具），
  // 时钟和进度也要继续走，不能停在最后一个事件的时间点。
  useEffect(() => {
    if (!liveStart || liveDone) return
    const timer = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(timer)
  }, [liveStart, liveDone])

  const liveElapsed = liveStart ? Math.max(0, now / 1000 - liveStart) : 0
  const duration = liveStart
    ? Math.max(baseDuration, liveDone ? liveElapsed : liveElapsed + 1)
    : baseDuration

  useEffect(() => {
    setTime(0)
    setPlaying(false)
    setInspect(null)
    // 实时轮询会不断替换 scenario 对象；只有切换“另一场任务”（title 变化）才重置播放，
    // 同一场任务的增量事件不应把进度打回 0。
  }, [scenario.title])

  useEffect(() => {
    if (!liveStart || inspect !== null) return
    setTime(Math.min(liveElapsed, duration))
  }, [liveStart, liveElapsed, duration, inspect])

  useEffect(() => {
    if (!playing) return
    lastRef.current = performance.now()
    // setInterval 而非 rAF：隐藏/后台标签页 rAF 完全不触发，录屏环境不确定时用
    // setInterval 仍可推进（降级为 ~1Hz），可见标签页保持 50ms 平滑。
    const timer = setInterval(() => {
      const now = performance.now()
      const dt = ((now - lastRef.current) / 1000) * speed
      lastRef.current = now
      setTime((t) => {
        const nt = t + dt
        if (nt >= duration) {
          setPlaying(false)
          return duration
        }
        return nt
      })
    }, 50)
    return () => clearInterval(timer)
  }, [playing, speed, duration])

  const state = useMemo(() => computeStateAt(scenario, time), [scenario, time])

  const play = useCallback(() => setPlaying(true), [])
  const pause = useCallback(() => setPlaying(false), [])
  const restart = useCallback(() => { setTime(0); setPlaying(true) }, [])
  const seek = useCallback((t: number) => {
    const nt = Math.max(0, Math.min(t, duration))
    setTime(nt)
    // 实时模式下的 seek 进入"审视时刻"，由顶栏"回到实时"恢复跟随
    if (liveStart) setInspect(nt)
  }, [duration, liveStart])
  const backToLive = useCallback(() => setInspect(null), [])

  return {
    state, time, duration, playing, speed, setSpeed,
    play, pause, restart, seek, backToLive, inspecting: inspect !== null,
  }
}
