import type { Region, AgentSpec } from '../types'

export const REGIONS: Region[] = [
  { id: 'wutong', name: '梧桐平台公共区', color: '#8b5cf6', x: 40, y: 20, w: 560, h: 150 },
  { id: 'yanshu', name: '研枢集群（机构 A）', color: '#0ea5e9', x: 40, y: 200, w: 560, h: 640 },
  { id: 'edge', name: '算力提供方（机构 B）', color: '#f59e0b', x: 640, y: 200, w: 480, h: 300 },
  { id: 'teammate', name: '合作课题组（机构 C）', color: '#10b981', x: 640, y: 530, w: 480, h: 310 },
]

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
