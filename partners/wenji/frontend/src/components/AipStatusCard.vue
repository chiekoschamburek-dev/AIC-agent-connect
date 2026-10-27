<template>
  <div v-if="loaded" class="aip-card" :class="{ 'aip-card--online': online }">
    <div class="aip-card__head">
      <span class="aip-card__badge">{{ online ? '●' : '○' }}</span>
      <span class="aip-card__title">AIP 智能体互联</span>
      <span class="aip-card__state" :class="online ? 'is-on' : 'is-off'">{{ stateLabel }}</span>
    </div>

    <dl class="aip-card__rows">
      <div class="row"><dt>AIC</dt><dd class="mono" :title="status.aic">{{ shortAic }}</dd></div>
      <div class="row"><dt>注册状态</dt><dd>{{ status.status || '—' }}</dd></div>
      <div class="row" v-if="status.public_endpoint"><dt>公网端点</dt><dd class="mono">{{ status.public_endpoint.replace('https://', '') }}</dd></div>
      <div class="row"><dt>对外能力</dt><dd>{{ (status.skills || []).map(s => s.id.replace('wenji.', '')).join(' · ') || '—' }}</dd></div>
      <div class="row" v-if="stats">
        <dt>平台调用</dt>
        <dd>{{ stats.task_total }} 次 · P50 {{ (stats.task_p50_ms / 1000).toFixed(1) }}s · RPC {{ stats.rpc_total }}</dd>
      </div>
      <div class="row" v-if="stats?.last_heartbeat_at"><dt>最近心跳</dt><dd>{{ heartbeatAgo }}</dd></div>
    </dl>

    <p class="aip-card__note">经梧桐平台（wt.ioa.pub）注册与发现，mTLS 双向认证 · AMP 可观测</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

interface AipStats { task_total: number; task_p50_ms: number | null; rpc_total: number; last_heartbeat_at: string | null }
interface AipStatus { enabled: boolean; aic?: string; status?: string; public_endpoint?: string; skills?: Array<{ id: string }>; stats?: AipStats }

const status = ref<AipStatus>({ enabled: false })
const loaded = ref(false)
let timer: number | undefined

const online = computed(() => status.value.enabled && status.value.status === 'active')
const stateLabel = computed(() => (online.value ? '在线' : status.value.enabled ? status.value.status || '未知' : '未启用'))
const shortAic = computed(() => (status.value.aic ? status.value.aic.slice(-14) : '—'))
const heartbeatAgo = computed(() => {
  const ts = status.value.stats?.last_heartbeat_at
  if (!ts) return '—'
  const seconds = Math.max(0, Math.round((Date.now() - new Date(ts).getTime()) / 1000))
  return seconds < 90 ? `${seconds}s 前` : `${Math.round(seconds / 60)} 分钟前`
})

async function refresh() {
  try {
    const res = await fetch('/api/acps/status')
    status.value = await res.json()
    loaded.value = true
  } catch {
    /* 面板静默失败，不打扰主流程 */
  }
}

onMounted(() => {
  refresh()
  timer = window.setInterval(refresh, 30_000)
})
onUnmounted(() => window.clearInterval(timer))
</script>

<style scoped>
.aip-card {
  border: 1px solid rgba(216, 90, 59, 0.25);
  border-radius: 10px;
  padding: 12px 14px;
  margin-bottom: 14px;
  background: rgba(216, 90, 59, 0.04);
}
.aip-card--online { border-color: rgba(46, 125, 50, 0.35); background: rgba(46, 125, 50, 0.05); }
.aip-card__head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.aip-card__badge { color: #9e9e9e; font-size: 10px; }
.aip-card--online .aip-card__badge { color: #2e7d32; }
.aip-card__title { font-weight: 600; font-size: 13px; letter-spacing: 0.02em; }
.aip-card__state { margin-left: auto; font-size: 11px; padding: 1px 8px; border-radius: 999px; border: 1px solid currentColor; }
.aip-card__state.is-on { color: #2e7d32; }
.aip-card__state.is-off { color: #b26a00; }
.aip-card__rows { margin: 0; display: grid; gap: 4px; }
.row { display: flex; gap: 8px; font-size: 12px; line-height: 1.5; }
.row dt { color: #8d6e63; min-width: 56px; flex: none; }
.row dd { margin: 0; word-break: break-all; }
.mono { font-family: ui-monospace, monospace; font-size: 11px; }
.aip-card__note { margin: 10px 0 0; font-size: 11px; color: #9e9e9e; line-height: 1.4; }
</style>
