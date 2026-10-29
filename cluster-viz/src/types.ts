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

// 资源 agent（workspace 等集群基础设施）与干活的"工作流伙伴"分区展示、不混排。
// 旧录制数据没有 kind 字段，缺省按 workflow 处理。
export type AgentKind = 'resource' | 'workflow'

export interface AgentSpec {
  id: string
  name: string
  role: 'supervisor' | 'partner'
  kind?: AgentKind
  org: string // 领地 id（regions 之一；资源 agent 用独立分区）
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

// 协作域章程：真实录制数据里可以没有（旧 run 只带 domainId/revision），
// 有则渲染完整成员/资产/签发人/双签证据（2026-10-29 补齐）。
export type CharterRole = 'provider' | 'consumer' | 'auditor' | 'issuer' | 'member' | string

export interface CharterMember {
  aic: string
  name?: string
  role: CharterRole
  agentId?: string
}

export interface CharterAsset {
  skill: string
  provider?: string
  providerAic?: string
  allowedRoles?: CharterRole[]
  allowedMembers?: string[]
}

export interface CharterSignature {
  signer?: string
  name?: string
  aic?: string
  role?: string
  verified?: boolean
}

export interface DomainCharter {
  name?: string
  members?: CharterMember[]
  assets?: CharterAsset[]
  issuers?: string[]
  signatures?: CharterSignature[]
  hash?: string
  window?: string
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
  | {
      t: number
      type: 'domain'
      action: 'create' | 'freeze' | 'unfreeze' | 'revision'
      domainId: string
      revision?: number
      // create 时携带章程；freeze/unfreeze/revision 可只带 revision
      charter?: DomainCharter
    }
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
      // by=实际使用工作区的调用方（sup 或某个伙伴）；resource=资源 agent 节点 id。
      // 旧录制数据没有这两个字段，前端会退化为"supervisor → 第一个资源节点"。
      by?: string
      resource?: string
      task?: string
    }
  | {
      t: number
      type: 'ws_zone'
      task: string
      zones: string[]
      by?: string
      resource?: string
      skill?: string
    }
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

export const SOURCE_SHORT: Record<SourceKind, string> = {
  wutong: '梧桐',
  roster: '名册',
  domain: '协作域',
  teammate: '队友组',
}

export const CHARTER_ROLE_LABEL: Record<string, string> = {
  provider: '提供方',
  consumer: '使用方',
  auditor: '审计方',
  issuer: '签发方',
  member: '成员',
}

export const AGENT_KIND_LABEL: Record<AgentKind, string> = {
  resource: '资源',
  workflow: '工作流',
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
