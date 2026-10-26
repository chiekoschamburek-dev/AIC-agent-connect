// 展示层文本清洗：AIP 入站 payload 常带 XML 包裹和“外部伙伴内容”提示，
// 页面只展示人能读的任务文本；不改变 Scenario 原始数据。
export function cleanInstruction(text: string): string {
  let t = String(text || '')
  t = t.replace(/<acps_leader_call[\s\S]*?<\/acps_leader_call>/gi, '')
  t = t.replace(/\[外部伙伴内容开始\][^\n]*\n?/gi, '')
  t = t.replace(/^\s*研枢supervisor\s*$/gim, '')
  const answer = t.match(/用户回答：\s*([\s\S]+)/)
  if (answer) t = answer[1]
  t = t.replace(/\s+/g, ' ').trim()
  if (!t) return '（本轮无具体任务文本）'
  if (/start.*DAG|DAG 派发/.test(t) && /不包含任何具体研究诉求|没有具体研究诉求/.test(t)) {
    return '叮当 DAG 派发握手（本轮没有具体任务文本）'
  }
  return t.length > 240 ? `${t.slice(0, 240)}…` : t
}
