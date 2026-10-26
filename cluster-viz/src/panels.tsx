import type { UIProduct, UIState, UIWsFile } from './engine'
import { PRODUCT_LABEL, SOURCE_LABEL, type VizEvent } from './types'
import { cleanInstruction } from './text'

const ZONES = ['repo', 'products', 'scratch', 'shared'] as const
const ZONE_LABEL: Record<(typeof ZONES)[number], string> = {
  repo: '📎 repo（只读参考）',
  products: '📦 products（产物归档·版本化）',
  scratch: '✏️ scratch（个人草稿）',
  shared: '🤝 shared（协作总线）',
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
      return { icon: '⚙️', cls: '', text: `${e.agent} → ${e.state}${e.note ? `（${e.note}）` : ''}` }
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
      return { icon: '🗂️', cls: 'lv-plan', text: `任务工作区已开辟：${e.task}（${e.zones.join('/')}）` }
    case 'ws_file':
      return { icon: '🗂️', cls: 'lv-product', text: `工作区写入 [${e.zone}] ${e.path}（${e.actor}）` }
    case 'done':
      return { icon: '🏁', cls: 'lv-allow', text: e.summary }
    default:
      return { icon: '·', cls: '', text: JSON.stringify(e) }
  }
}

export function Ledger({ state, seek }: { state: UIState; seek: (t: number) => void }) {
  return (
    <div className="ledger">
      <div className="panel-title">事件台账（点击跳转）</div>
      <div className="ledger-list">
        {state.ledger.length === 0 && <div className="ledger-empty">按 ▶ 开始演示</div>}
        {state.ledger.filter((e) => e.t >= 0).map((e, i) => {
          const l = eventLine(e)
          return (
            <div key={i} className={`ledger-row ${l.cls}`} onClick={() => seek(e.t + 0.01)}>
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

export function WorkspacePanel({ files, task }: { files: UIWsFile[]; task?: string }) {
  const byZone = new Map<string, UIWsFile[]>()
  for (const zone of ZONES) byZone.set(zone, [])
  for (const f of [...files].sort((a, b) => a.t - b.t)) byZone.get(f.zone)?.push(f)
  return (
    <div className="wszones">
      <div className="panel-title">任务工作区{task ? `（${task}）` : ''}</div>
      <div className="wszone-grid">
        {ZONES.map((zone) => {
          const list = byZone.get(zone) ?? []
          return (
            <div key={zone} className={`wszone wszone-${zone}`}>
              <div className="wszone-title">{ZONE_LABEL[zone]}</div>
              {list.length === 0 && <div className="wszone-empty">空</div>}
              {list.map((f) => (
                <div key={f.path + f.t} className="wsfile" title={f.sha256 ? `sha256 ${f.sha256.slice(0, 12)}…` : undefined}>
                  <span className="wsfile-path">{f.path}</span>
                  <span className="wsfile-meta">{f.actor}{f.bytes != null ? ` · ${f.bytes}B` : ''}</span>
                </div>
              ))}
            </div>
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
