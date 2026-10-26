import { memo } from 'react'
import { Handle, Position, type NodeProps } from 'reactflow'
import type { UIAgent, UIDomain } from '../engine'
import { SOURCE_LABEL, STATE_LABEL } from '../types'
import type { SourceKind } from '../types'

const STATE_COLOR: Record<string, string> = {
  idle: '#64748b',
  planning: '#a855f7',
  discovering: '#8b5cf6',
  working: '#f59e0b',
  'awaiting-input': '#eab308',
  'awaiting-completion': '#eab308',
  completed: '#22c55e',
  rejected: '#ef4444',
}

export const RegionNode = memo(({ data }: NodeProps<{ name: string; color: string; w: number; h: number }>) => (
  <div
    className="region-node"
    style={{ width: data.w, height: data.h, borderColor: data.color, background: `${data.color}0d` }}
  >
    <div className="region-title" style={{ color: data.color }}>
      {data.name}
    </div>
  </div>
))

export const AgentNode = memo(({ data }: NodeProps<{ agent: UIAgent; regionColor: string }>) => {
  const a = data.agent
  const color = STATE_COLOR[a.state] ?? '#64748b'
  return (
    <div className={`agent-node ${a.role === 'supervisor' ? 'is-supervisor' : ''} agent-pop`} style={{ borderColor: color }}>
      <Handle type="target" position={Position.Left} className="rf-handle" />
      <Handle type="source" position={Position.Right} className="rf-handle" />
      <div className="agent-head">
        <span className="agent-name">{a.name}</span>
        <span className="agent-state" style={{ background: color }}>
          {STATE_LABEL[a.state]}
        </span>
      </div>
      <div className="agent-source">来源：{SOURCE_LABEL[a.source as SourceKind] ?? a.source}</div>
      {a.note && <div className="agent-note">{a.note}</div>}
      {a.metrics.length > 0 && (
        <div className="agent-metrics">
          {a.metrics.map((m, i) => (
            <span key={i} className="metric-chip">
              {m.key}={m.value}
            </span>
          ))}
        </div>
      )}
    </div>
  )
})

export const TreatyNode = memo(({ data }: NodeProps<{ domain: UIDomain }>) => {
  const d = data.domain
  const frozen = d.status === 'frozen'
  return (
    <div className={`treaty-node ${frozen ? 'is-frozen' : ''}`}>
      <Handle type="target" position={Position.Left} className="rf-handle" />
      <Handle type="source" position={Position.Right} className="rf-handle" />
      <div className="treaty-head">
        <span>{frozen ? '🔒' : '📜'} 协作域 · {d.id}</span>
        <span className={`treaty-status ${frozen ? 'frozen' : 'active'}`}>{frozen ? '已冻结' : '活跃'}</span>
      </div>
      <div className="treaty-row">revision r{d.revision} · 双签章程 ✓✓</div>
      {d.reconciledUses !== undefined && (
        <div className="treaty-row reconcile">对账完成：{d.reconciledUses} 次调用 · 双方签名 ✓✓</div>
      )}
    </div>
  )
})

export const nodeTypes = { region: RegionNode, agent: AgentNode, treaty: TreatyNode }
