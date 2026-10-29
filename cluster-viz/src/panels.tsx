import type { UIProduct, UIState, UIDiscovered } from './engine'
import {
  PRODUCT_LABEL, SOURCE_LABEL, SOURCE_SHORT, STATE_LABEL, type SourceKind, type VizEvent,
} from './types'
import { cleanInstruction } from './text'

const SOURCE_CLASS: Record<SourceKind, string> = {
  wutong: 'src-wutong',
  roster: 'src-roster',
  domain: 'src-domain',
  teammate: 'src-teammate',
}

function eventLine(e: VizEvent): { icon: string; cls: string; text: string } {
  switch (e.type) {
    case 'instruction':
      return { icon: '💬', cls: 'lv-instruction', text: `指令：${cleanInstruction(e.text)}` }
    case 'plan':
      return { icon: '🧩', cls: 'lv-plan', text: `任务分解：${e.subtasks.map((s) => s.title).join(' → ')}` }
    case 'discover':
      return { icon: '📡', cls: 'lv-discover', text: `发现「${e.query}」→ ${e.results.map((r) => `${r.name}（${SOURCE_LABEL[r.source]}）`).join('、')}` }
    case 'agent_up':
      return { icon: '🤖', cls: '', text: `${e.agent.name} 上线（${SOURCE_LABEL[e.agent.source]}）` }
    case 'delegate':
      return { icon: '📨', cls: '', text: `委派 ${e.from} → ${e.to}：${e.label ?? e.skill}` }
    case 'token':
      return { icon: e.kind === 'grant' ? '📜' : '🎫', cls: 'lv-token', text: `${e.kind === 'grant' ? '域级 Grant' : '派生 Token'}：${e.issuer} → ${e.holder}${e.label ? `（${e.label}）` : ''}` }
    case 'pep':
      return {
        icon: e.decision === 'allow' ? '🟢' : '⛔',
        cls: e.decision === 'allow' ? 'lv-allow' : 'lv-deny',
        text: `PEP ${e.decision === 'allow' ? '放行' : '拒绝'} ${e.caller}→${e.target} · ${e.rule}`,
      }
    case 'state':
      // 与画布节点徽章用同一份中文映射：台账直接插值英文枚举会两边不一致。
      // taskId 一并展示，多任务并发时能归因到具体任务
      return { icon: '⚙️', cls: '', text: `${e.agent}${e.taskId ? ` · ${e.taskId}` : ''} → ${STATE_LABEL[e.state] ?? e.state}${e.note ? `（${e.note}）` : ''}` }
    case 'product':
      return { icon: '📄', cls: 'lv-product', text: `产物《${e.name}》${PRODUCT_LABEL[e.state]}（${e.agent}）` }
    case 'domain': {
      const m = { create: '建域（双签）', freeze: '冻结（急停）', unfreeze: '解冻', revision: '修订' } as const
      return { icon: '📜', cls: e.action === 'freeze' ? 'lv-deny' : 'lv-token', text: `协作域 ${e.domainId}：${m[e.action]}${e.revision ? ` r${e.revision}` : ''}` }
    }
    case 'metric':
      return { icon: '📈', cls: '', text: `${e.agent} ${e.key} = ${e.value}` }
    case 'reconcile':
      return { icon: '🧾', cls: 'lv-allow', text: `对账：${e.domain} 共 ${e.totalUses} 次调用 · 双方签名` }
    case 'ws_zone':
      return {
        icon: '🗂️', cls: 'lv-plan',
        text: `任务工作区分区：${e.task}（${e.zones.join('/')}）${e.by ? ` · ${e.by}` : ''}`,
      }
    case 'ws_file':
      // note 是后端特意准备的区分文案（"委派已下发" vs "克隆完成"），必须展示，
      // 否则同一仓库的两行在界面上无法区分语义（2026-10-27 审查）
      return { icon: '🗂️', cls: 'lv-product', text: `工作区写入 [${e.zone}] ${e.path}（${e.actor}${e.note ? ` · ${e.note}` : ''}）` }
    case 'done':
      return { icon: '🏁', cls: 'lv-allow', text: e.summary }
    default:
      return { icon: '·', cls: '', text: JSON.stringify(e) }
  }
}

export function Ledger({ state, time, seek }: { state: UIState; time: number; seek: (t: number) => void }) {
  // 同一 (task, 调用方) 的 ws_zone 只显示第一条（旧 scenario 可能录了多条重复事件）
  const seenZones = new Set<string>()
  const rows = state.ledger
    .filter((e) => e.t >= 0)
    .filter((e) => {
      if (e.type !== 'ws_zone') return true
      // 同一 task 可能先后被 supervisor 与不同 partner 使用：按 (task, by) 去重，
      // 让每个调用方各留一行；同一方重复声明仍然只显示一次（2026-10-28）。
      const key = `${e.task}|${e.by ?? ''}`
      if (seenZones.has(key)) return false
      seenZones.add(key)
      return true
    })
  // 当前时刻所在的行（最后一个 t <= time 的事件）：跳转后给出位置反馈，
  // 也让实时模式"跳了又被挂钟拉回"的现象可见
  let activeIndex = -1
  for (let i = 0; i < rows.length; i++) {
    if (rows[i].t <= time) activeIndex = i
    else break
  }
  return (
    <div className="ledger">
      <div className="panel-title">事件台账（点击跳转）</div>
      <div className="ledger-list">
        {state.ledger.length === 0 && <div className="ledger-empty">按 ▶ 开始演示</div>}
        {rows.map((e, i) => {
          const l = eventLine(e)
          return (
            <div
              key={i}
              className={`ledger-row ${l.cls} ${i === activeIndex ? 'active' : ''}`}
              onClick={() => seek(e.t + 0.01)}
            >
              <span className="ledger-t">{e.t.toFixed(1)}s</span>
              <span className="ledger-icon">{l.icon}</span>
              <span className="ledger-text">{l.text}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// 发现候选栏：discover 事件不再只进台账。候选按 AIC 去重；同名 agent 上线后
// 打绿标，演示时能清楚看到"发现 → 上线 → 委派"的过渡（2026-10-29 补齐）。
export function DiscoveryBar({ items, agents }: {
  items: UIDiscovered[]
  agents: UIState['agents']
}) {
  if (items.length === 0) return null
  const onlineNames = new Set(Object.values(agents).map((a) => a.name))
  return (
    <div className="discovery-bar" aria-label={`发现候选 ${items.length} 个`}>
      <span className="discovery-title">发现候选 <b>{items.length}</b></span>
      <div className="discovery-list">
        {items.map((item) => {
          const online = onlineNames.has(item.name)
          const skills = (item.skills ?? []).slice(0, 2).join(' / ')
          return (
            <span
              key={item.aic}
              className={`discovery-chip ${online ? 'online' : ''}`}
              title={`${item.name} · ${item.aic}\n能力：${(item.skills ?? []).join(' / ') || '—'}\n最近查询：${item.query}`}
            >
              <span className={`discovery-source ${SOURCE_CLASS[item.source] ?? ''}`}>
                {SOURCE_SHORT[item.source] ?? item.source}
              </span>
              <span className="discovery-name">{item.name}</span>
              {online && <span className="discovery-online">已上线</span>}
              {skills && <span className="discovery-skills">{skills}</span>}
            </span>
          )
        })}
      </div>
    </div>
  )
}

export function ProductPanel({ products }: { products: Record<string, UIProduct> }) {
  const list = Object.values(products)
  return (
    <div className="products">
      <div className="panel-title">产物</div>
      <div className="product-list">
        {list.length === 0 && <div className="ledger-empty">尚无产物</div>}
        {list.map((p) => (
          <div key={p.name} className="product-card">
            <span className="product-name">📄 {p.name}</span>
            <span className="product-agent">{p.agent}</span>
            <span className={`product-state ps-${p.state}`}>{PRODUCT_LABEL[p.state]}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
