import type { Scenario } from '../types'
import { REGIONS, AGENTS } from './common'

// 剧本 A：论文复现全链路（域授权嵌套 + 真实训练 + 对账）
export const scenarioRepro: Scenario = {
  title: 'A · 论文复现全链路',
  description: '选题→检索→设计→执行→分析→成文→审阅，算力走协作域授权',
  instruction: '复现一篇 GNN 引用网络论文：检索文献、设计实验、协作域授权调 GPU 训练、数据分析、撰写并审阅复现报告。',
  regions: REGIONS,
  agents: AGENTS.filter((a) => a.id !== 'outsider'),
  events: [
    { t: 0.2, type: 'instruction', text: '复现一篇 GNN 引用网络论文（全链路）' },
    { t: 0.8, type: 'state', agent: 'sup', state: 'planning' },
    {
      t: 2.0,
      type: 'plan',
      subtasks: [
        { id: 't1', title: '检索文献' },
        { id: 't2', title: '实验设计' },
        { id: 't3', title: '授权训练' },
        { id: 't4', title: '数据分析' },
        { id: 't5', title: '撰写报告' },
        { id: 't6', title: '审阅' },
      ],
    },
    { t: 3.0, type: 'state', agent: 'sup', state: 'discovering' },
    {
      t: 3.6,
      type: 'discover',
      query: 'literature.search · 引用网络',
      results: [
        { aic: '1.2.156.3088.1.1.F3BH…0PMW', name: '文献检索', source: 'wutong', org: 'yanshu', skills: ['arxiv.search', 'openalex.search'] },
      ],
    },
    {
      t: 4.6,
      type: 'discover',
      query: 'exp.design · data.analyze',
      results: [
        { aic: '1.2.156.3088.1.1.TM01…88AZ', name: '实验设计', source: 'teammate', org: 'teammate', skills: ['exp.design'] },
        { aic: '1.2.156.3088.1.1.TM02…91BQ', name: '数据分析', source: 'teammate', org: 'teammate', skills: ['data.analyze'] },
      ],
    },
    {
      t: 5.6,
      type: 'discover',
      query: 'gpu.run · 协作域资产',
      results: [
        { aic: '1.2.156.3088.1.1.GP5060…0IGR', name: 'GPU 算力节点', source: 'domain', org: 'edge', skills: ['gpu.run', 'gpu.env'] },
      ],
    },
    { t: 6.4, type: 'agent_up', agent: AGENTS.find((a) => a.id === 'scout')! },
    { t: 6.8, type: 'agent_up', agent: AGENTS.find((a) => a.id === 'expd')! },
    { t: 7.2, type: 'agent_up', agent: AGENTS.find((a) => a.id === 'data')! },
    { t: 7.6, type: 'agent_up', agent: AGENTS.find((a) => a.id === 'gpu')! },
    { t: 8.0, type: 'agent_up', agent: AGENTS.find((a) => a.id === 'edge-leader')! },
    { t: 8.4, type: 'agent_up', agent: AGENTS.find((a) => a.id === 'exp')! },
    { t: 8.8, type: 'agent_up', agent: AGENTS.find((a) => a.id === 'writer')! },
    { t: 9.2, type: 'agent_up', agent: AGENTS.find((a) => a.id === 'critic')! },
    { t: 9.8, type: 'state', agent: 'sup', state: 'idle', note: '编排就绪' },

    { t: 10.4, type: 'domain', action: 'create', domainId: 'yanshu-edge-lab', revision: 1 },
    { t: 11.2, type: 'token', kind: 'grant', issuer: 'edge-leader', holder: 'sup', domain: 'yanshu-edge-lab', maxUses: 5, label: '域级 Grant · 配额 5 · 7 天' },

    { t: 12.2, type: 'delegate', from: 'sup', to: 'scout', taskId: 't1', skill: 'arxiv.search', label: '检索文献' },
    { t: 12.4, type: 'state', agent: 'scout', taskId: 't1', state: 'working' },
    { t: 12.6, type: 'delegate', from: 'sup', to: 'expd', taskId: 't2', skill: 'exp.design', label: '实验设计' },
    { t: 12.8, type: 'state', agent: 'expd', taskId: 't2', state: 'working' },
    { t: 14.2, type: 'product', agent: 'scout', name: '资料清单.md', state: 'draft' },
    { t: 15.0, type: 'product', agent: 'expd', name: '实验方案.md', state: 'draft' },
    { t: 15.8, type: 'product', agent: 'scout', name: '资料清单.md', state: 'final' },
    { t: 16.0, type: 'state', agent: 'scout', taskId: 't1', state: 'completed' },
    { t: 16.6, type: 'product', agent: 'expd', name: '实验方案.md', state: 'final' },
    { t: 17.0, type: 'state', agent: 'expd', taskId: 't2', state: 'completed' },

    { t: 17.8, type: 'token', kind: 'delegation', issuer: 'sup', holder: 'exp', domain: 'yanshu-edge-lab', skill: 'gpu.run', label: '派生 Task Token · 10min · 收窄 ⊆' },
    { t: 18.6, type: 'delegate', from: 'sup', to: 'exp', taskId: 't3', skill: 'exp.run', label: '执行训练' },
    { t: 18.8, type: 'state', agent: 'exp', taskId: 't3', state: 'working' },
    { t: 19.4, type: 'pep', caller: 'exp', target: 'gpu', skill: 'gpu.run', decision: 'allow', rule: 'domain-delegation', domain: 'yanshu-edge-lab' },
    { t: 20.0, type: 'state', agent: 'gpu', taskId: 't3', state: 'working', note: '容器沙箱内训练' },
    { t: 21.0, type: 'metric', agent: 'gpu', key: 'loss', value: '0.89' },
    { t: 22.5, type: 'metric', agent: 'gpu', key: 'loss', value: '0.62' },
    { t: 24.0, type: 'metric', agent: 'gpu', key: 'loss', value: '0.41' },
    { t: 25.5, type: 'metric', agent: 'gpu', key: 'loss', value: '0.27' },
    { t: 26.5, type: 'state', agent: 'gpu', taskId: 't3', state: 'awaiting-completion' },
    { t: 27.0, type: 'product', agent: 'exp', name: 'metrics.json', state: 'final' },
    { t: 27.4, type: 'state', agent: 'exp', taskId: 't3', state: 'completed' },

    { t: 28.0, type: 'delegate', from: 'sup', to: 'data', taskId: 't4', skill: 'data.analyze', label: '数据分析' },
    { t: 28.2, type: 'state', agent: 'data', taskId: 't4', state: 'working' },
    { t: 29.6, type: 'product', agent: 'data', name: '数据分析报告.md', state: 'final' },
    { t: 30.0, type: 'state', agent: 'data', taskId: 't4', state: 'completed' },

    { t: 30.6, type: 'delegate', from: 'sup', to: 'writer', taskId: 't5', skill: 'paper.write', label: '撰写报告' },
    { t: 30.8, type: 'state', agent: 'writer', taskId: 't5', state: 'working' },
    { t: 32.0, type: 'product', agent: 'writer', name: '复现报告.md', state: 'draft' },
    { t: 33.0, type: 'delegate', from: 'sup', to: 'critic', taskId: 't6', skill: 'paper.review', label: '审阅' },
    { t: 33.2, type: 'state', agent: 'critic', taskId: 't6', state: 'working' },
    { t: 33.4, type: 'product', agent: 'writer', name: '复现报告.md', state: 'reviewing' },
    { t: 35.0, type: 'product', agent: 'writer', name: '复现报告.md', state: 'final' },
    { t: 35.4, type: 'state', agent: 'writer', taskId: 't5', state: 'completed' },
    { t: 35.6, type: 'state', agent: 'critic', taskId: 't6', state: 'completed' },

    { t: 36.4, type: 'reconcile', domain: 'yanshu-edge-lab', totalUses: 1 },
    { t: 37.4, type: 'done', summary: '复现完成：loss 0.27，报告通过审阅；全程授权留痕，双签对账。' },
  ],
}
