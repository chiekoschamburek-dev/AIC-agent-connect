import type { Scenario } from '../types'
import { REGIONS, AGENTS, LAB_CHARTER } from './common'

// 剧本 C：授权戏剧——拦截、签发、急停、配额、对账（PEP/CDAP 专场）
export const scenarioAuth: Scenario = {
  title: 'C · 授权实战：谁能用 · 谁不能用',
  description: '外部拦截 → 域授权嵌套 → 配额烧尽 → 冻结急停 → 解冻 → 双签对账',
  instruction: '演示授权层：无凭据与自签令牌被拦截，域授权完成训练，配额、急停与对账。',
  regions: REGIONS,
  agents: AGENTS.filter((a) => ['sup', 'exp', 'gpu', 'edge-leader', 'outsider'].includes(a.id)),
  events: [
    { t: 0.2, type: 'instruction', text: '授权层实战演示' },
    { t: 0.8, type: 'agent_up', agent: AGENTS.find((a) => a.id === 'outsider')! },
    { t: 1.4, type: 'agent_up', agent: AGENTS.find((a) => a.id === 'gpu')! },
    { t: 1.8, type: 'agent_up', agent: AGENTS.find((a) => a.id === 'edge-leader')! },
    { t: 2.2, type: 'agent_up', agent: AGENTS.find((a) => a.id === 'exp')! },

    { t: 3.0, type: 'pep', caller: 'outsider', target: 'gpu', skill: 'gpu.run', decision: 'deny', rule: 'no-credential · 无凭据直连' },
    { t: 4.6, type: 'pep', caller: 'outsider', target: 'gpu', skill: 'gpu.run', decision: 'deny', rule: 'issuer-not-authorized · 自签令牌' },

    { t: 6.2, type: 'domain', action: 'create', domainId: 'yanshu-edge-lab', revision: 1, charter: LAB_CHARTER },
    { t: 7.0, type: 'token', kind: 'grant', issuer: 'edge-leader', holder: 'sup', domain: 'yanshu-edge-lab', maxUses: 2, label: '域级 Grant · 配额 2' },
    { t: 8.0, type: 'token', kind: 'delegation', issuer: 'sup', holder: 'exp', domain: 'yanshu-edge-lab', skill: 'gpu.run', label: '派生 Task Token' },
    { t: 8.8, type: 'delegate', from: 'sup', to: 'exp', taskId: 't1', skill: 'exp.run', label: '训练 #1' },
    { t: 9.2, type: 'pep', caller: 'exp', target: 'gpu', skill: 'gpu.run', decision: 'allow', rule: 'domain-delegation', domain: 'yanshu-edge-lab' },
    { t: 9.6, type: 'state', agent: 'gpu', taskId: 't1', state: 'working' },
    { t: 11.0, type: 'state', agent: 'gpu', taskId: 't1', state: 'completed' },

    { t: 11.8, type: 'delegate', from: 'sup', to: 'exp', taskId: 't2', skill: 'exp.run', label: '训练 #2（配额 2/2）' },
    { t: 12.2, type: 'pep', caller: 'exp', target: 'gpu', skill: 'gpu.run', decision: 'allow', rule: 'domain-delegation', domain: 'yanshu-edge-lab' },
    { t: 12.6, type: 'state', agent: 'gpu', taskId: 't2', state: 'working' },
    { t: 13.8, type: 'state', agent: 'gpu', taskId: 't2', state: 'completed' },

    { t: 14.6, type: 'delegate', from: 'sup', to: 'exp', taskId: 't3', skill: 'exp.run', label: '训练 #3' },
    { t: 15.0, type: 'pep', caller: 'exp', target: 'gpu', skill: 'gpu.run', decision: 'deny', rule: 'quota-exhausted · 配额烧尽', domain: 'yanshu-edge-lab' },

    { t: 16.6, type: 'domain', action: 'freeze', domainId: 'yanshu-edge-lab', revision: 2 },
    { t: 17.4, type: 'pep', caller: 'exp', target: 'gpu', skill: 'gpu.run', decision: 'deny', rule: 'domain-frozen · grant 同步死亡', domain: 'yanshu-edge-lab' },
    { t: 18.6, type: 'pep', caller: 'exp', target: 'gpu', skill: 'gpu.run', decision: 'deny', rule: 'domain-frozen · 派生 token 同步死亡', domain: 'yanshu-edge-lab' },

    { t: 20.2, type: 'domain', action: 'unfreeze', domainId: 'yanshu-edge-lab', revision: 3 },
    { t: 21.0, type: 'token', kind: 'grant', issuer: 'edge-leader', holder: 'sup', domain: 'yanshu-edge-lab', maxUses: 1, label: '新 Grant（r3 解冻后）' },
    { t: 21.8, type: 'pep', caller: 'exp', target: 'gpu', skill: 'gpu.run', decision: 'allow', rule: 'domain-delegation · 恢复', domain: 'yanshu-edge-lab' },

    { t: 23.4, type: 'reconcile', domain: 'yanshu-edge-lab', totalUses: 3 },
    { t: 24.6, type: 'done', summary: '拦截有据、授权可达、急停为真、用量双签——可信协作闭环。' },
  ],
}
