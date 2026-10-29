import { memo, type MouseEvent } from 'react'
import { Handle, Position, type NodeProps } from 'reactflow'
import type { UIAgent, UIDomain } from '../engine'
import { AGENT_KIND_LABEL, SOURCE_LABEL, STATE_LABEL } from '../types'
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
  failed: '#ef4444',
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
    <div
      className={`agent-node ${a.role === 'supervisor' ? 'is-supervisor' : ''} ${a.kind === 'resource' ? 'is-resource' : ''} agent-pop`}
      style={{ borderColor: color }}
    >
      <Handle type="target" position={Position.Left} className="rf-handle" />
      <Handle type="source" position={Position.Right} className="rf-handle" />
      <div className="agent-head">
        <span className="agent-name">{a.name}</span>
        {a.kind === 'resource' && <span className="agent-kind">{AGENT_KIND_LABEL.resource}</span>}
        <span className="agent-state" style={{ background: color }}>
          {STATE_LABEL[a.state]}
        </span>
      </div>
      <div className="agent-source">来源：{SOURCE_LABEL[a.source as SourceKind] ?? a.source}</div>
      {a.note && <div className="agent-note">{a.note}</div>}
      {a.tasks.length > 1 && (
        <div className="agent-metrics">
          <span className="metric-chip">
            任务 {a.tasks.filter((t) => t.state === 'completed').length}/{a.tasks.length}
          </span>
        </div>
      )}
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

export const TreatyNode = memo(({ data }: NodeProps<{ domain: UIDomain; onOpen?: (id: string) => void }>) => {
  const d = data.domain
  const frozen = d.status === 'frozen'
  const signatures = d.charter?.signatures ?? []
  const verified = signatures.filter((s) => s.verified).length
  // 旧录制数据没有章程明细：不再无条件宣称“双签 ✓✓”，只显示 revision
  const signatureText = signatures.length > 0
    ? `章程双签 ${verified}/${signatures.length}${verified === signatures.length ? ' ✓✓' : ''}`
    : '章程摘要'
  const open = (event: MouseEvent) => {
    event.stopPropagation()
    data.onOpen?.(d.id)
  }
  return (
    <div className={`treaty-node ${frozen ? 'is-frozen' : ''}`} onClick={open}>
      <Handle type="target" position={Position.Left} className="rf-handle" />
      <Handle type="source" position={Position.Right} className="rf-handle" />
      <div className="treaty-head">
        <span>{frozen ? '🔒' : '📜'} 协作域 · {d.id}</span>
        <span className={`treaty-status ${frozen ? 'frozen' : 'active'}`}>{frozen ? '已冻结' : '活跃'}</span>
      </div>
      <div className="treaty-row">revision r{d.revision} · {signatureText}</div>
      {d.reconciledUses !== undefined && (
        <div className="treaty-row reconcile">对账完成：{d.reconciledUses} 次调用 · 双方签名 ✓✓</div>
      )}
      {data.onOpen && (
        <button type="button" className="treaty-open" onClick={open}>
          查看章程 →
        </button>
      )}
    </div>
  )
})

export const nodeTypes = { region: RegionNode, agent: AgentNode, treaty: TreatyNode }
