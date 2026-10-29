import { useMemo } from 'react'
import ReactFlow, { Background, Controls, type Node } from 'reactflow'
import 'reactflow/dist/style.css'
import type { UIState } from '../engine'
import type { Region } from '../types'
import { EdgesOverlay } from './EdgesOverlay'
import { nodeTypes } from './nodes'

export function CanvasView({
  state,
  regions,
  onOpenDomain,
}: {
  state: UIState
  regions: Region[]
  onOpenDomain?: (id: string) => void
}) {
  const nodes: Node[] = useMemo(() => {
    const ns: Node[] = []
    for (const r of regions) {
      ns.push({
        id: `region:${r.id}`,
        type: 'region',
        position: { x: r.x, y: r.y },
        // 显式尺寸：RF 节点级 width/height 视为已测量（SSR 路径），无头环境也可量
        width: r.w,
        height: r.h,
        style: { width: r.w, height: r.h },
        data: { name: r.name, color: r.color, w: r.w, h: r.h },
        draggable: false,
        selectable: false,
        zIndex: -1,
      })
    }
    const byOrg = new Map<string, string[]>()
    for (const a of Object.values(state.agents).sort((x, y) => x.upAt - y.upAt)) {
      const list = byOrg.get(a.org) ?? []
      list.push(a.id)
      byOrg.set(a.org, list)
    }
    const regionById = Object.fromEntries(regions.map((r) => [r.id, r]))
    for (const r of regions) {
      const ids = byOrg.get(r.id) ?? []
      const cols = Math.max(1, Math.floor((r.w - 40) / 170))
      ids.forEach((id, i) => {
        const a = state.agents[id]
        let p: { x: number; y: number }
        if (a.role === 'supervisor') {
          p = { x: r.w / 2 - 80, y: 42 }
        } else {
          const row = Math.floor(i / cols)
          const col = i % cols
          p = { x: 24 + col * 170, y: 42 + row * 108 + (ids.some((x) => state.agents[x].role === 'supervisor') ? 96 : 0) }
        }
        ns.push({
          id: a.id,
          type: 'agent',
          position: p,
          parentNode: `region:${a.org}`,
          extent: 'parent',
          width: 150,
          height: 88,
          style: { width: 150 },
          data: { agent: a, regionColor: regionById[a.org]?.color ?? '#888' },
        })
      })
    }
    let ti = 0
    for (const d of Object.values(state.domains)) {
      ns.push({
        id: `treaty:${d.id}`,
        type: 'treaty',
        position: { x: 640, y: 24 + ti * 150 },
        width: 300,
        height: d.charter ? 126 : d.reconciledUses !== undefined ? 110 : 84,
        style: { width: 300 },
        data: { domain: d, onOpen: onOpenDomain },
      })
      ti += 1
    }
    return ns
  }, [state, regions, onOpenDomain])

  return (
    <ReactFlow
      nodes={nodes}
      edges={[]}
      nodeTypes={nodeTypes}
      fitView
      minZoom={0.3}
      maxZoom={1.4}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
      proOptions={{ hideAttribution: true }}
    >
      <Background gap={24} size={1} color="#e2e8f0" />
      <Controls showInteractive={false} position="bottom-left" />
      <EdgesOverlay state={state} regions={regions} />
    </ReactFlow>
  )
}
