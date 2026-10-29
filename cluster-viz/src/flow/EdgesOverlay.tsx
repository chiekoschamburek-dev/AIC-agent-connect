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

const LABEL_W = 220
const LABEL_H = 26
const LABEL_PAD = 6

function bezierPoint(s: Pt, t: Pt, k: number): Pt {
  const sx = s.x + NODE_W
  const sy = s.y + NODE_H / 2
  const tx = t.x
  const ty = t.y + NODE_H / 2
  const mx = (sx + tx) / 2
  const it = 1 - k
  const w0 = it * it * it
  const w1 = 3 * it * it * k
  const w2 = 3 * it * k * k
  const w3 = k * k * k
  return {
    x: w0 * sx + w1 * mx + w2 * mx + w3 * tx,
    y: w0 * sy + w1 * sy + w2 * ty + w3 * ty,
  }
}

interface Rect { x: number; y: number; w: number; h: number }

function intersects(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.w + LABEL_PAD
    && a.x + a.w + LABEL_PAD > b.x
    && a.y < b.y + b.h + LABEL_PAD
    && a.y + a.h + LABEL_PAD > b.y
}

// 多条边在画布中心交叉时标签会叠在一起。按信息量排序（Grant/Token/PEP/资源优先），
// 沿曲线多个采样点 + 垂直偏移找第一个不与节点/已放标签冲突的位置（2026-10-29）。
function labelPriority(e: UIEdge): number {
  if (e.kind === 'grant') return 0
  if (e.kind === 'delegation') return 1
  if (e.gate) return 2
  if (e.resource) return 3
  return 4
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
  if (e.resource) {
    // 资源 agent（workspace 等）的使用关系：暖色虚线，和灰色委派线区分开
    return {
      stroke: '#d97706', width: 2, dash: '6 4', opacity: e.broken ? 0.25 : 0.9,
      label: `🗂️ ${e.label ?? '工作区使用'}`,
      labelFg: '#b45309', labelBg: '#fffbeb', animated: !e.broken,
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
    const base = Object.values(state.edges)
      .map((e) => {
        const s = positions.get(e.from)
        const t = positions.get(e.to)
        if (!s || !t) return null
        const v = visualOf(e)
        const mx = (s.x + NODE_W + t.x) / 2
        const my = (s.y + t.y + NODE_H) / 2
        return { e, s, t, d: bezier(s, t), v, mx, my, labelX: mx - LABEL_W / 2, labelY: my - LABEL_H / 2 }
      })
      .filter((x): x is NonNullable<typeof x> => x !== null)
    const placed: Rect[] = Object.values(positions).map((p) => ({
      x: p.x - 4, y: p.y - 4, w: NODE_W + 8, h: NODE_H + 8,
    }))
    const ts = [0.5, 0.42, 0.58, 0.34, 0.66, 0.26, 0.74]
    const dys = [0, -16, 16, -32, 32, -48, 48, -64, 64]
    const ordered = [...base].sort((a, b) => labelPriority(a.e) - labelPriority(b.e))
    for (const item of ordered) {
      if (!item.v.label) continue
      let best: Rect | null = null
      for (const k of ts) {
        const point = bezierPoint(item.s, item.t, k)
        for (const dy of dys) {
          const rect = {
            x: point.x - LABEL_W / 2,
            y: point.y - LABEL_H / 2 + dy,
            w: LABEL_W,
            h: LABEL_H,
          }
          if (!placed.some((p) => intersects(rect, p))) {
            best = rect
            break
          }
        }
        if (best) break
      }
      const chosen = best ?? {
        x: item.mx - LABEL_W / 2,
        y: item.my - LABEL_H / 2,
        w: LABEL_W,
        h: LABEL_H,
      }
      item.labelX = chosen.x
      item.labelY = chosen.y
      placed.push(chosen)
    }
    return base
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
        {items.map(({ e, v, labelX, labelY }) =>
          v.label ? (
            <foreignObject key={`lbl-${e.id}`} x={labelX} y={labelY} width={LABEL_W} height={LABEL_H} style={{ pointerEvents: 'none', overflow: 'visible' }}>
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
