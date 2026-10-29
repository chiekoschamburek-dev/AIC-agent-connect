// 可视化事件契约（与 bridge/server.py 的翻译层共用）
// 时间轴回放与未来的实时接入（WebSocket）共用同一事件模型。

export type AgentState =
  | 'idle'
  | 'planning'
  | 'discovering'
  | 'working'
  | 'awaiting-input'
  | 'awaiting-completion'
  | 'completed'
  | 'rejected'
  | 'failed'

export type ProductState = 'draft' | 'reviewing' | 'final'

export type SourceKind = 'wutong' | 'roster' | 'domain' | 'teammate'

export interface AgentSpec {
  id: string
  name: string
  role: 'supervisor' | 'partner'
  org: string // 领地 id（regions 之一）
  source: SourceKind // 来源徽章
  skills?: string[]
}

export interface Region {
  id: string
  name: string
  color: string
  x: number
  y: number
  w: number
  h: number
}

export interface DiscoveredAgent {
  aic: string
  name: string
  source: SourceKind
  org: string
  skills: string[]
}

export type VizEvent =
  | { t: number; type: 'instruction'; text: string }
  | { t: number; type: 'plan'; subtasks: { id: string; title: string }[] }
  | { t: number; type: 'discover'; query: string; results: DiscoveredAgent[] }
  | { t: number; type: 'agent_up'; agent: AgentSpec }
  | { t: number; type: 'delegate'; from: string; to: string; taskId: string; skill: string; label?: string }
  | {
      t: number
      type: 'token'
      kind: 'grant' | 'delegation'
      issuer: string
      holder: string
      domain?: string
      maxUses?: number
      skill?: string
      label?: string
    }
  | {
      t: number
      type: 'pep'
      caller: string
      target: string
      skill: string
      decision: 'allow' | 'deny'
      rule: string
      domain?: string
    }
  | { t: number; type: 'state'; agent: string; taskId?: string; state: AgentState; note?: string }
  | { t: number; type: 'product'; agent: string; name: string; state: ProductState }
  | { t: number; type: 'domain'; action: 'create' | 'freeze' | 'unfreeze' | 'revision'; domainId: string; revision?: number }
  | { t: number; type: 'metric'; agent: string; key: string; value: string }
  | { t: number; type: 'reconcile'; domain: string; totalUses: number }
  | {
      t: number
      type: 'ws_file'
      actor: string
      zone: 'repo' | 'products' | 'scratch' | 'shared'
      path: string
      bytes?: number
      sha256?: string
      note?: string
    }
  | { t: number; type: 'ws_zone'; task: string; zones: string[] }
  | { t: number; type: 'done'; summary: string; status?: 'done' | 'failed' }

export interface Scenario {
  title: string
  description: string
  instruction: string
  regions: Region[]
  agents: AgentSpec[]
  events: VizEvent[]
}

export const SOURCE_LABEL: Record<SourceKind, string> = {
  wutong: '梧桐平台·公共发现',
  roster: '本地名册',
  domain: '协作域',
  teammate: '协作域·队友组',
}

export const STATE_LABEL: Record<AgentState, string> = {
  idle: '待命',
  planning: '分解任务',
  discovering: '发现智能体',
  working: '工作中',
  'awaiting-input': '等待输入',
  'awaiting-completion': '等待产物',
  completed: '已完成',
  rejected: '已拒绝',
  failed: '失败',
}

export const PRODUCT_LABEL: Record<ProductState, string> = {
  draft: '草稿',
  reviewing: '审查中',
  final: '终稿',
}
