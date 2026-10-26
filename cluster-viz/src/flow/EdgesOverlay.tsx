import { useMemo } from 'react'
import { useViewport } from 'reactflow'
import type { UIEdge, UIState } from '../engine'
import type { Region } from '../types'

const NODE_W = 150
const NODE_H = 88

interface Pt {
  x: number
  y: number
}

export function absoluteAgentPositions(state: UIState, regions: Region[]): Map<string, Pt> {
  const rel = new Map<string, Pt>()
  const byOrg = new Map<string, string[]>()
  for (const a of Object.values(state.agents).sort((x, y) => x.upAt - y.upAt)) {
    const list = byOrg.get(a.org) ?? []
    list.push(a.id)
    byOrg.set(a.org, list)
  }
  const abs = new Map<string, Pt>()
  for (const r of regions) {
    const ids = byOrg.get(r.id) ?? []
    const cols = Math.max(1, Math.floor((r.w - 40) / 170))
    ids.forEach((id, i) => {
      const agent = state.agents[id]
      let p: Pt
      if (agent.role === 'supervisor') {
        p = { x: r.w / 2 - 80, y: 42 }
      } else {
        const row = Math.floor(i / cols)
        const col = i % cols
        p = { x: 24 + col * 170, y: 42 + row * 108 + (ids.some((x) => state.agents[x].role === 'supervisor') ? 96 : 0) }
      }
      abs.set(id, { x: r.x + p.x, y: r.y + p.y })
    })
  }
  return abs
}

function bezier(s: Pt, t: Pt): string {
  const sx = s.x + NODE_W
  const sy = s.y + NODE_H / 2
  const tx = t.x
  const ty = t.y + NODE_H / 2
  const mx = (sx + tx) / 2
  return `M ${sx} ${sy} C ${mx} ${sy}, ${mx} ${ty}, ${tx} ${ty}`
}

interface EdgeVisual {
  stroke: string
  width: number
  dash?: string
  opacity: number
  label?: string
  labelFg?: string
  labelBg?: string
  animated?: boolean
}

function visualOf(e: UIEdge): EdgeVisual {
  if (e.kind === 'grant') {
    return {
      stroke: '#f59e0b', width: 2.4, dash: '7 4', opacity: e.broken ? 0.25 : 0.95,
      label: `📜 ${e.label ?? 'Grant'}`, labelFg: '#b45309', labelBg: '#fffbeb', animated: !e.broken,
    }
  }
  if (e.kind === 'delegation') {
    return {
      stroke: '#3b82f6', width: 1.8, dash: '4 3', opacity: e.broken ? 0.25 : 0.9,
      label: `🎫 ${e.label ?? 'Task Token'}`, labelFg: '#1d4ed8', labelBg: '#eff6ff', animated: !e.broken,
    }
  }
  if (e.gate) {
    const allow = e.gate.decision === 'allow'
    return {
      stroke: allow ? '#22c55e' : '#ef4444',
      width: allow ? 2 : 1.8,
      dash: allow ? undefined : '5 4',
      opacity: e.broken ? 0.25 : 0.95,
      label: `PEP ${allow ? '✓' : '✗'} ${e.gate.rule}`,
      labelFg: allow ? '#15803d' : '#b91c1c',
      labelBg: allow ? '#f0fdf4' : '#fef2f2',
      animated: allow && !e.broken,
    }
  }
  return {
    stroke: '#94a3b8', width: 1.4, opacity: e.broken ? 0.25 : 0.85,
    label: e.label, labelFg: '#475569', labelBg: '#f8fafc',
  }
}

export function EdgesOverlay({ state, regions }: { state: UIState; regions: Region[] }) {
  const positions = useMemo(() => absoluteAgentPositions(state, regions), [state, regions])
  // v11 无 ViewportPortal：手动同步 viewport 的平移缩放，让覆盖层与画布坐标对齐
  const { x: vx, y: vy, zoom } = useViewport()

  const items = useMemo(() => {
    return Object.values(state.edges)
      .map((e) => {
        const s = positions.get(e.from)
        const t = positions.get(e.to)
        if (!s || !t) return null
        const v = visualOf(e)
        const mx = (s.x + NODE_W + t.x) / 2
        const my = (s.y + t.y + NODE_H) / 2
        return { e, d: bezier(s, t), v, mx, my }
      })
      .filter((x): x is NonNullable<typeof x> => x !== null)
  }, [state.edges, positions])

  const colors = useMemo(() => [...new Set(items.map((i) => i.v.stroke))], [items])

  return (
    <div style={{ position: 'absolute', top: 0, left: 0, width: 0, height: 0, zIndex: 5, pointerEvents: 'none' }}>
      <svg
        className="edges-overlay"
        width="3000"
        height="1600"
        style={{ transform: `translate(${vx}px, ${vy}px) scale(${zoom})`, transformOrigin: '0 0', overflow: 'visible' }}
      >
        <defs>
          {colors.map((c) => (
            <marker key={c} id={`ov-arrow-${c.replace('#', '')}`} markerWidth="10" markerHeight="10" viewBox="0 0 10 10" refX="8" refY="5" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill={c} />
            </marker>
          ))}
        </defs>
        {items.map(({ e, d, v }) => (
          <path
            key={e.id}
            d={d}
            fill="none"
            stroke={v.stroke}
            strokeWidth={v.width}
            strokeDasharray={v.dash}
            opacity={v.opacity}
            markerEnd={`url(#ov-arrow-${v.stroke.replace('#', '')})`}
            className={v.animated ? 'edge-animated' : undefined}
          />
        ))}
        {items.map(({ e, v, mx, my }) =>
          v.label ? (
            <foreignObject key={`lbl-${e.id}`} x={mx - 110} y={my - 13} width="220" height="26" style={{ pointerEvents: 'none', overflow: 'visible' }}>
              <div className="edge-label" style={{ color: v.labelFg, background: v.labelBg, borderColor: v.stroke, opacity: v.opacity }}>
                {v.label}
              </div>
            </foreignObject>
          ) : null,
        )}
      </svg>
    </div>
  )
}
