import type { DomainCharter, Region, AgentSpec } from '../types'

export const REGIONS: Region[] = [
  { id: 'wutong', name: '梧桐平台公共区', color: '#8b5cf6', x: 40, y: 20, w: 560, h: 150 },
  { id: 'yanshu', name: '研枢集群（机构 A）', color: '#0ea5e9', x: 40, y: 200, w: 560, h: 640 },
  { id: 'edge', name: '算力提供方（机构 B）', color: '#f59e0b', x: 640, y: 200, w: 480, h: 300 },
  { id: 'teammate', name: '合作课题组（机构 C）', color: '#10b981', x: 640, y: 530, w: 480, h: 310 },
]

// 资源 agent 独立分区（workspace 等基础设施），不与工作流伙伴混排。
// 放在画布最右侧，避开协作域节点（x=640 起）与 edge/teammate 区（至 x=1120）。
export const INFRA_REGION: Region = {
  id: 'yanshu-infra', name: '基础设施（资源 agent）', color: '#f59e0b', x: 1160, y: 200, w: 340, h: 280,
}

export const AGENTS: AgentSpec[] = [
  { id: 'sup', name: '研枢 · 调度大脑', role: 'supervisor', org: 'yanshu', source: 'roster', skills: ['plan', 'orchestrate'] },
  { id: 'scout', name: '文献检索', role: 'partner', org: 'yanshu', source: 'wutong', skills: ['arxiv.search', 'openalex.search'] },
  { id: 'exp', name: '实验员', role: 'partner', org: 'yanshu', source: 'roster', skills: ['exp.run'] },
  { id: 'writer', name: '综述撰写', role: 'partner', org: 'yanshu', source: 'roster', skills: ['paper.write'] },
  { id: 'critic', name: '审阅', role: 'partner', org: 'yanshu', source: 'roster', skills: ['paper.review'] },
  { id: 'gpu', name: 'GPU 算力节点', role: 'partner', org: 'edge', source: 'domain', skills: ['gpu.run', 'gpu.env'] },
  { id: 'edge-leader', name: '算力管理方', role: 'partner', org: 'edge', source: 'domain', skills: ['grant.issue'] },
  { id: 'expd', name: '实验设计', role: 'partner', org: 'teammate', source: 'teammate', skills: ['exp.design'] },
  { id: 'data', name: '数据分析', role: 'partner', org: 'teammate', source: 'teammate', skills: ['data.analyze'] },
  { id: 'outsider', name: '外部智能体 X', role: 'partner', org: 'wutong', source: 'wutong', skills: [] },
]

// 资源 agent：任务工作区（文件总线）。org 指向 INFRA_REGION，与录制器
// viz_live.RESOURCE_ORG 同名同区。
export const WS_AGENT: AgentSpec = {
  id: 'workspace', name: '任务工作区', role: 'partner', kind: 'resource',
  org: 'yanshu-infra', source: 'roster',
  skills: ['ws.create', 'ws.write', 'ws.read', 'ws.list', 'ws.clone_repo'],
}

// 内置 A/C 共用的协作域章程（演示数据）：A 展示完整链路，C 展示冻结/解冻时同一章程
// 的 revision 变化。真实录制数据若没有 charter，前端只显示摘要并提示"未上传明细"。
export const LAB_CHARTER: DomainCharter = {
  name: '论文复现协作域 · 研枢 × 算力方',
  hash: 'sha256:7f3a9c1e…e2',
  window: '2026-10-01 ~ 2026-10-31',
  members: [
    { role: 'provider', name: '算力管理方', aic: '1.2.156.3088.1.1.EDGE…LEAD', agentId: 'edge-leader' },
    { role: 'provider', name: 'GPU 算力节点', aic: '1.2.156.3088.1.1.GP5060…0IGR', agentId: 'gpu' },
    { role: 'consumer', name: '实验员', aic: '1.2.156.3088.1.1.EXP…0001', agentId: 'exp' },
    { role: 'auditor', name: '研枢 · 调度大脑', aic: '1.2.156.3088.1.1.SUP…0001', agentId: 'sup' },
  ],
  assets: [
    {
      skill: 'gpu.run',
      provider: 'GPU 算力节点',
      providerAic: '1.2.156.3088.1.1.GP5060…0IGR',
      allowedRoles: ['consumer'],
    },
  ],
  issuers: ['算力管理方', '研枢 · 调度大脑'],
  signatures: [
    { role: 'provider', name: '算力管理方', aic: '1.2.156.3088.1.1.EDGE…LEAD', verified: true },
    { role: 'auditor', name: '研枢 · 调度大脑', aic: '1.2.156.3088.1.1.SUP…0001', verified: true },
  ],
}
