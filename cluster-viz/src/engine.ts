import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { AgentState, ProductState, Scenario, VizEvent } from './types'

export interface UIAgent {
  id: string
  name: string
  role: 'supervisor' | 'partner'
  org: string
  source: string
  skills: string[]
  state: AgentState
  note?: string
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
}

export interface UIDomain {
  id: string
  status: 'active' | 'frozen'
  revision: number
  reconciledUses?: number
}

export interface UIProduct {
  name: string
  agent: string
  state: ProductState
}

export interface UIWsFile {
  path: string
  actor: string
  zone: 'repo' | 'products' | 'scratch' | 'shared'
  bytes?: number
  sha256?: string
  t: number
}

export interface UIState {
  instruction: string
  plan: { id: string; title: string }[]
  agents: Record<string, UIAgent>
  edges: Record<string, UIEdge>
  domains: Record<string, UIDomain>
  products: Record<string, UIProduct>
  wsFiles: UIWsFile[]
  wsTask?: string
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
  products: {},
  wsFiles: [],
  ledger: [],
})

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
            id: a.id, name: a.name, role: a.role, org: a.org, source: a.source,
            skills: a.skills ?? [], state: 'idle', metrics: [], upAt: e.t,
          },
        },
      }
    }
    case 'discover':
      return s
    case 'state': {
      const ag = s.agents[e.agent]
      if (!ag) return s
      return { ...s, agents: { ...s.agents, [e.agent]: { ...ag, state: e.state, note: e.note } } }
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
        return { ...s, domains: { ...s.domains, [e.domainId]: { id: e.domainId, status: 'active', revision: e.revision ?? 1 } } }
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
        return { ...s, domains: { ...s.domains, [e.domainId]: { ...d, status: 'active', revision: e.revision ?? d.revision + 1 } } }
      }
      return s
    }
    case 'product':
      return { ...s, products: { ...s.products, [e.name]: { name: e.name, agent: e.agent, state: e.state } } }
    case 'ws_zone':
      return { ...s, wsTask: e.task }
    case 'ws_file': {
      const key = `${e.zone}/${e.actor}/${e.path}`
      const rest = s.wsFiles.filter((f) => f.path !== `${e.zone}/${e.actor}/${e.path}`)
      return { ...s, wsFiles: [...rest, { path: e.path, actor: e.actor, zone: e.zone, bytes: e.bytes, sha256: e.sha256, t: e.t }] }
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
    // 实时轮询会不断替换 scenario 对象；只有切换“另一场任务”（title 变化）才重置播放，
    // 同一场任务的增量事件不应把进度打回 0。
  }, [scenario.title])

  useEffect(() => {
    if (!liveStart) return
    setTime(Math.min(liveElapsed, duration))
  }, [liveStart, liveElapsed, duration])

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
  const seek = useCallback((t: number) => setTime(Math.max(0, Math.min(t, duration))), [duration])

  return { state, time, duration, playing, speed, setSpeed, play, pause, restart, seek }
}
