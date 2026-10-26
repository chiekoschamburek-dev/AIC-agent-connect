import type { Scenario } from '../types'
import { REGIONS, AGENTS } from './common'

// 剧本 B：短链路（检索→成文）——证明工作流按需编排，不是固定流水线
export const scenarioShort: Scenario = {
  title: 'B · 短链路：检索即成文',
  description: '只分解出两个子任务——工作流由任务决定，不是固定模板',
  instruction: '查一下图注意力网络近一年的进展，直接写成两页综述。',
  regions: REGIONS,
  agents: AGENTS.filter((a) => ['sup', 'scout', 'writer'].includes(a.id)),
  events: [
    { t: 0.2, type: 'instruction', text: 'GAT 近一年进展 → 两页综述' },
    { t: 0.8, type: 'state', agent: 'sup', state: 'planning' },
    {
      t: 2.0,
      type: 'plan',
      subtasks: [
        { id: 't1', title: '检索' },
        { id: 't2', title: '成文' },
      ],
    },
    { t: 2.8, type: 'state', agent: 'sup', state: 'discovering' },
    {
      t: 3.4,
      type: 'discover',
      query: 'literature.search · graph attention',
      results: [
        { aic: '1.2.156.3088.1.1.F3BH…0PMW', name: '文献检索', source: 'wutong', org: 'yanshu', skills: ['arxiv.search', 'openalex.search'] },
      ],
    },
    { t: 4.2, type: 'agent_up', agent: AGENTS.find((a) => a.id === 'scout')! },
    { t: 4.6, type: 'agent_up', agent: AGENTS.find((a) => a.id === 'writer')! },
    { t: 5.4, type: 'delegate', from: 'sup', to: 'scout', taskId: 't1', skill: 'arxiv.search', label: '检索' },
    { t: 5.6, type: 'state', agent: 'scout', taskId: 't1', state: 'working' },
    { t: 6.0, type: 'pep', caller: 'sup', target: 'scout', skill: 'arxiv.search', decision: 'allow', rule: 'public-skill (L0)' },
    { t: 7.4, type: 'product', agent: 'scout', name: '资料清单.md', state: 'final' },
    { t: 7.8, type: 'state', agent: 'scout', taskId: 't1', state: 'completed' },
    { t: 8.4, type: 'delegate', from: 'sup', to: 'writer', taskId: 't2', skill: 'paper.write', label: '成文' },
    { t: 8.6, type: 'state', agent: 'writer', taskId: 't2', state: 'working' },
    { t: 10.0, type: 'product', agent: 'writer', name: 'GAT 综述.md', state: 'draft' },
    { t: 11.6, type: 'product', agent: 'writer', name: 'GAT 综述.md', state: 'final' },
    { t: 12.0, type: 'state', agent: 'writer', taskId: 't2', state: 'completed' },
    { t: 12.8, type: 'done', summary: '两个子任务即闭环——工作流按指令动态编排，公开技能走 L0 免授权。' },
  ],
}
