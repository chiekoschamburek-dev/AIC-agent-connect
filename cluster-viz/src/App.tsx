import { useEffect, useRef, useState } from 'react'
import { usePlayer } from './engine'
import { DomainDrawer } from './DomainDrawer'
import { CanvasView } from './flow/CanvasView'
import { DiscoveryBar, Ledger, ProductPanel } from './panels'
import { scenarioRepro } from './scenarios/repro'
import { scenarioShort } from './scenarios/short'
import { scenarioAuth } from './scenarios/auth'
import type { Scenario } from './types'
import { cleanInstruction } from './text'
import './styles.css'

const BUILTIN: Scenario[] = [scenarioRepro, scenarioShort, scenarioAuth]
const BASE = import.meta.env.BASE_URL

type LivePointer = {
  run_id?: string
  title?: string
  started_at?: number
  done?: boolean
  status?: 'running' | 'done' | 'failed'
  updated_at?: number
}

type RunIndexEntry = {
  run_id: string
  instruction?: string
  started_at?: number
  updated_at?: number
  done?: boolean
  status?: 'running' | 'done' | 'failed'
  note?: string
  events?: number
}

function RunList({
  runs,
  current,
  onPick,
}: {
  runs: RunIndexEntry[]
  current: string
  onPick: (run: RunIndexEntry) => void
}) {
  if (!runs.length) return null
  return (
    <div className="runlist">
      <div className="panel-title">
        最近任务
        <a className="runlist-follow" href="?live=1">● 跟随最新</a>
      </div>
      <div className="runlist-body">
        {runs.map((r) => {
          const at = typeof r.started_at === 'number' ? new Date(r.started_at * 1000) : null
          const hhmm = at ? at.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : ''
          const idle = !r.done && typeof r.updated_at === 'number'
            ? Math.max(0, Math.round(Date.now() / 1000 - r.updated_at))
            : null
          const failed = r.status === 'failed'
          // 未完成但长时间无更新：进程被杀/重启中断的 run 没有收口方（常驻
          // supervisor 不经 jobs_runner 看门狗），不能永远伪装"进行中"。
          const interrupted = !failed && !r.done && idle !== null && idle > 300
          return (
            <button
              key={r.run_id}
              className={`runitem ${r.run_id === current ? 'active' : ''}`}
              title={r.instruction || r.run_id}
              onClick={() => onPick(r)}
            >
              <span className={`runbadge ${failed ? 'failed' : r.done ? 'done' : interrupted ? 'stale' : 'live'}`}>
                {failed ? '失败' : r.done ? '已完成' : interrupted ? '已中断' : '进行中'}
              </span>
              <span className="runtext">
                {cleanInstruction(r.instruction || r.run_id).slice(0, 60)}
              </span>
              <span className="runmeta">
                {hhmm} · {r.events ?? 0} 事件
                {idle !== null && idle > 30 ? ` · ${Math.round(idle / 60)} 分钟无更新` : ''} · {r.run_id}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

function queryRun(): { run: string | null; scenario: string | null; live: boolean; autoplay: boolean } {
  if (typeof window === 'undefined') return { run: null, scenario: null, live: false, autoplay: false }
  const params = new URLSearchParams(window.location.search)
  return {
    run: params.get('run') || params.get('task'),
    scenario: params.get('scenario'),
    live: params.get('live') === '1',
    autoplay: params.get('autoplay') !== '0',
  }
}

export default function App() {
  // 真实场景（viz_record.py / 本次 run 的工作流）置顶；加载失败回退内置手写剧本
  const [scenarios, setScenarios] = useState<Scenario[]>(BUILTIN)
  const [scenario, setScenario] = useState<Scenario>(scenarioRepro)
  const [loadState, setLoadState] = useState<'builtin' | 'loading' | 'waiting' | 'remote' | 'missing'>('builtin')
  const [autoPlay, setAutoPlay] = useState(false)
  const [liveMode, setLiveMode] = useState(false)
  const [liveStart, setLiveStart] = useState<number | null>(null)
  const [liveDone, setLiveDone] = useState(false)
  const [recentRuns, setRecentRuns] = useState<RunIndexEntry[]>([])
  const [currentRunId, setCurrentRunId] = useState('')
  const [liveStale, setLiveStale] = useState(false)
  const [liveIdle, setLiveIdle] = useState<number | null>(null)
  const [liveStatus, setLiveStatus] = useState<'running' | 'done' | 'failed'>('running')
  // 并发 run 下，用户从"最近任务"点选某一条进行中的 run：liveMode 仍为 true，
  // 但跟随对象从 live.json 的最新 run 切换为该 run（2026-10-28 用户反馈：
  // 以前点选其他 run 只是从头回放，只有最晚发起的那个才是实时）。
  const [livePinned, setLivePinned] = useState(false)
  // 点画布上的协作域节点打开章程详情；切 run/场景时自动关闭
  const [openDomain, setOpenDomain] = useState<string | null>(null)
  const autoTitle = useRef('')
  // 是否继续跟随实时任务。用户一旦点选示例（A/B/C）就置 false，
  // 否则实时轮询每 1.5 秒会把画面覆盖回最新 run——表现就是"点了没反应"。
  const followLive = useRef(true)
  // 当前被"点选"的 run（深链或最近任务点选）。run 的增量轮询在每轮应用数据前
  // 检查它：用户点了别的任务或示例标签后，旧轮询立即让位，不再覆盖画面
  // （此前点选 A 后深链循环 ≤2.5s 会把画面拉回 A，2026-10-27 审查）。
  const pinnedRun = useRef('')

  // 最近任务列表：scenarios/index.json（并发时多条 run 都列出来，点选即切换）
  useEffect(() => {
    let alive = true
    let timer: number | undefined
    const load = async () => {
      try {
        const res = await fetch(`${BASE}scenarios/index.json`, { cache: 'no-store' })
        if (res.ok) {
          const data = await res.json()
          if (alive && Array.isArray(data?.runs)) setRecentRuns(data.runs as RunIndexEntry[])
        }
      } catch {
        /* 静态托管没有索引时静默跳过 */
      }
      if (alive) timer = window.setTimeout(load, 4000)
    }
    void load()
    return () => {
      alive = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [])

  // 点选"最近任务"里的某一条：
  // - 进行中的 run → 进入该 run 的实时跟随（挂钟 + 增量轮询），不再从头回放；
  // - 已结束的 run → 按原行为回放。
  // 并发 run 下，live.json 只能指向"最晚发起"的一个；这里允许用户逐个切到
  // 其他在跑的 run，跟随对象改为 pinnedRun，直到点"回到最新"（2026-10-28）。
  const openRun = async (entry: RunIndexEntry) => {
    const runId = entry.run_id
    if (!runId) return
    const entryIdle = typeof entry.updated_at === 'number'
      ? Math.max(0, Date.now() / 1000 - entry.updated_at)
      : null
    let loaded: Scenario
    try {
      const res = await fetch(`${BASE}scenarios/${encodeURIComponent(runId)}.json`, { cache: 'no-store' })
      if (!res.ok) return
      loaded = (await res.json()) as Scenario
    } catch {
      return /* 加载失败保持当前画面 */
    }
    followLive.current = false
    pinnedRun.current = runId
    const doneEvt = Array.isArray(loaded.events)
      ? loaded.events.find((e) => e.type === 'done')
      : undefined
    // 索引可能落后于 scenario：以 scenario 里真正的 done 事件为准
    const running = !entry.done && entry.status !== 'failed' && !doneEvt
    if (running) {
      // 用 updated_at 校正服务端时钟：started_at 是服务端时钟口径，换到浏览器时钟。
      const offset = typeof entry.updated_at === 'number'
        ? entry.updated_at - Date.now() / 1000
        : 0
      const startServer = typeof entry.started_at === 'number'
        ? entry.started_at
        : Date.now() / 1000 + offset
      setLiveMode(true)
      setLivePinned(true)
      setLiveStart(startServer - offset)
      setLiveDone(false)
      setLiveStatus('running')
      setLiveStale(entryIdle !== null && entryIdle > 300)
      setLiveIdle(entryIdle)
      setAutoPlay(false) // 实时模式由挂钟驱动，不要同时启动回放计时器
    } else {
      setLiveMode(false)
      setLivePinned(false)
      setLiveStart(null)
      setLiveDone(Boolean(doneEvt))
      setLiveStatus(doneEvt?.status === 'failed' ? 'failed' : 'done')
      setLiveStale(false)
      setLiveIdle(null)
      setAutoPlay(true)
    }
    setScenario(loaded)
    setScenarios((prev) => [loaded, ...prev.filter((s) => s.title !== loaded.title)])
    setLoadState('remote')
    setCurrentRunId(runId)
    window.history.replaceState(null, '', `?run=${encodeURIComponent(runId)}`)
    if (!running) return

    const poll = async () => {
      if (pinnedRun.current !== runId) return // 用户已切走：停止轮询，不再排队
      try {
        const res = await fetch(`${BASE}scenarios/${encodeURIComponent(runId)}.json`, { cache: 'no-store' })
        if (!res.ok) {
          window.setTimeout(poll, 1500)
          return
        }
        const next = (await res.json()) as Scenario
        if (pinnedRun.current !== runId) return
        setScenario(next)
        setScenarios((prev) => [next, ...prev.filter((s) => s.title !== next.title)])
        const doneWe = Array.isArray(next.events)
          ? next.events.find((e) => e.type === 'done')
          : undefined
        if (doneWe) {
          setLiveDone(true)
          setLiveStatus(doneWe.status === 'failed' ? 'failed' : 'done')
          setLiveStale(false)
          return
        }
        // 实时页的"无更新/已中断"判断跟着 index.json 走（与侧栏同一口径）
        const latest = recentRunsRef.current.find((r) => r.run_id === runId)
        if (latest && typeof latest.updated_at === 'number') {
          const idle = Math.max(0, Date.now() / 1000 - latest.updated_at)
          setLiveIdle(idle)
          setLiveStale(idle > 300)
        }
        window.setTimeout(poll, 1500)
      } catch {
        window.setTimeout(poll, 1500)
      }
    }
    window.setTimeout(poll, 1500)
  }

  // openRun 的轮询里要读最新的 index 条目；用 ref 避免闭包捕获点击那一刻的旧列表。
  const recentRunsRef = useRef<RunIndexEntry[]>([])
  useEffect(() => {
    recentRunsRef.current = recentRuns
  }, [recentRuns])

  // 实时总览：/?live=1。始终跟随 scenarios/live.json 指向的最新 run，
  // 旧回答里的链接也不会因为 run_id 重置而失效。
  useEffect(() => {
    const q = queryRun()
    if (!q.live) return
    setLiveMode(true)
    setLivePinned(false)
    setAutoPlay(true)
    let alive = true
    let timer: number | undefined
    let currentRun = ''
    let currentStartedAt: number | undefined
    const pointerUrl = `${BASE}scenarios/live.json`

    const load = async () => {
      // 用户已点选示例 → 停止跟随，不再覆盖画面
      if (!followLive.current) return
      try {
        const res = await fetch(pointerUrl, { cache: 'no-store' })
        if (!res.ok) {
          if (alive) setLoadState('waiting')
          if (alive) timer = window.setTimeout(load, 1500)
          return
        }
        const pointer = (await res.json()) as LivePointer
        if (!pointer?.run_id) {
          if (alive) setLoadState('waiting')
          if (alive) timer = window.setTimeout(load, 1500)
          return
        }
        // 防回退护栏（2026-10-19）：并发 run（常驻 supervisor + 作业 worker）都可能写
        // live.json。服务端虽有"旧 run 不得覆盖新 run"的检查，客户端再挡一层：
        // 指针若指向一个**更早开始**的 run，就忽略这次切换，避免时间轴在两个 run
        // 之间来回跳（现场表现为 67→69→67 反复）。
        if (
          currentRun !== '' &&
          pointer.run_id !== currentRun &&
          typeof pointer.started_at === 'number' &&
          typeof currentStartedAt === 'number' &&
          pointer.started_at < currentStartedAt - 1
        ) {
          if (alive) timer = window.setTimeout(load, 1500)
          return
        }
        const changed = pointer.run_id !== currentRun
        // 指针长期无更新且未 done：通常是进程被杀/重启中断的 run。
        // 仍然展示它（比露出内置示例更真实），但标注"已中断"。
        const pointerIdle = typeof pointer.updated_at === 'number'
          ? Math.max(0, Date.now() / 1000 - pointer.updated_at)
          : 0
        if (alive) {
          setLiveStale(!pointer.done && pointerIdle > 300)
          setLiveIdle(pointerIdle)
        }
        setCurrentRunId(pointer.run_id)
        const scenarioRes = await fetch(`${BASE}scenarios/${encodeURIComponent(pointer.run_id)}.json`, {
          cache: 'no-store',
        })
        if (!scenarioRes.ok) {
          if (alive) setLoadState('waiting')
          if (alive) timer = window.setTimeout(load, 1200)
          return
        }
        const loaded = (await scenarioRes.json()) as Scenario
        // followLive 只在循环顶部检查过：两次 await 期间用户可能已点走示例，
        // 在途响应不能把刚点出来的画面覆盖回去（2026-10-27 审查）
        if (!alive || !followLive.current) return
        setScenario(loaded)
        // 用 updated_at 校正浏览器与服务器的时钟偏差：liveStart 是"客户端时钟口径"的运行起点。
        //
        // **只在切换 run 时算一次**（2026-10-19 修）。原实现每次轮询都重算：
        // `updated_at` 只在新事件落盘时才刷新，两次刷新之间它相对 now 越来越旧，
        // offset 越来越负 → liveStart 往前漂 → liveElapsed 反而变小；
        // 等 recorder 下一次写盘，updated_at 跳新 → 时间又跳回去。
        // 现场表现就是时间轴 67→69→67 反复锯齿（实测）。
        // 时钟偏差是常量，每个 run 量一次即可。
        if (changed) {
          const offset = typeof pointer.updated_at === 'number'
            ? pointer.updated_at - Date.now() / 1000
            : 0
          setLiveStart(typeof pointer.started_at === 'number' ? pointer.started_at - offset : null)
        }
        currentRun = pointer.run_id
        if (typeof pointer.started_at === 'number') currentStartedAt = pointer.started_at
        setLiveDone(Boolean(pointer.done))
        setLiveStatus(pointer.status ?? (pointer.done ? 'done' : 'running'))
        if (changed) {
          setScenarios((prev) => [loaded, ...prev.filter((s) => s.title !== loaded.title)])
        }
        setLoadState('remote')
        // 即使本轮已 done 也继续轮询：页面要能自动接到下一次 supervisor 调用。
        if (alive) timer = window.setTimeout(load, pointer.done ? 2500 : 1500)
      } catch {
        if (alive) {
          setLoadState('waiting')
          timer = window.setTimeout(load, 2000)
        }
      }
    }

    void load()
    return () => {
      alive = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [])

  // 深链：/?run=<runId>&autoplay=1（本次任务回放）
  useEffect(() => {
    const { run, scenario: scenarioPath, live, autoplay } = queryRun()
    if (live || (!run && !scenarioPath)) return
    const url = scenarioPath || `${BASE}scenarios/${encodeURIComponent(run!)}.json`
    const target = run || scenarioPath || ''
    pinnedRun.current = target
    let alive = true
    let loadedOnce = false
    let timer: number | undefined
    setLoadState('loading')
    const load = async () => {
      try {
        const res = await fetch(url, { cache: 'no-store' })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const loaded = (await res.json()) as Scenario
        // 用户已点走（openRun 换了任务、或点了示例标签）：让位，
        // 不再覆盖画面也不再续轮询（缺这道守卫时点示例 ≤2.5s 被拉回）
        if (!alive || pinnedRun.current !== target) return
        loadedOnce = true
        setScenario(loaded)
        setScenarios((prev) => [loaded, ...prev.filter((s) => s.title !== loaded.title)])
        setAutoPlay(autoplay)
        setLoadState('remote')
        setCurrentRunId(run || '')
        // 任务进行中时增量刷新同一场任务；done 后停止轮询。
        const done = Array.isArray(loaded.events) && loaded.events.some((e) => e.type === 'done')
        if (!done && alive) timer = window.setTimeout(load, 2500)
      } catch {
        if (alive && !loadedOnce) setLoadState('missing')
      }
    }
    void load()
    return () => {
      alive = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [])

  // 无深链时才加载录制场景清单；有深链/实时模式时避免 manifest 覆盖当前任务。
  useEffect(() => {
    const { run, scenario, live } = queryRun()
    if (run || scenario || live) return
    let alive = true
    ;(async () => {
      try {
        const res = await fetch(`${BASE}scenarios/manifest.json`)
        if (!res.ok) return
        const m = await res.json()
        const paths: string[] = m?.scenarios ?? []
        const loaded = (await Promise.all(
          paths.map((p) =>
            fetch(`${BASE}${p.replace(/^\//, '')}`)
              .then((r) => (r.ok ? r.json() : null))
              .catch(() => null),
          ),
        )).filter(Boolean) as Scenario[]
        if (alive && loaded.length) {
          setScenarios([...loaded, ...BUILTIN])
          setScenario(loaded[0])
        }
      } catch {
        /* 静态托管缺 manifest 时用内置剧本 */
      }
    })()
    return () => {
      alive = false
    }
  }, [])

  const { state, time, duration, playing, speed, setSpeed, play, pause, restart, seek, backToLive, inspecting } =
    usePlayer(scenario, liveMode ? liveStart : null, liveDone)
  const selectedDomain = openDomain ? state.domains[openDomain] ?? null : null

  // 切 run / 切示例时关掉章程抽屉，避免显示上一个任务的域
  useEffect(() => {
    setOpenDomain(null)
  }, [scenario.title])

  // 台账点击 = 停在该事件：回放模式先暂停（否则立刻从该时刻继续前进，想停在
  // 某个事件上必须先手动点 ⏸，2026-10-27 审查）；实时模式 seek 会进入"审视时刻"，
  // 顶栏出现"回到实时"按钮。
  const seekLedger = (t: number) => {
    pause()
    seek(t)
  }

  // 回到全局实时：重新跟随 live.json 的最新 run（整页刷新最稳，避免与
  // openRun 的轮询和 pinnedRun 状态打架）。
  const backToLatest = () => {
    window.location.assign('?live=1')
  }

  // 实时模式下由挂钟驱动时间轴；不要同时让回放计时器也推进（两者会互相覆盖）。
  useEffect(() => {
    if (liveMode) pause()
  }, [liveMode, pause])

  // 每次切换到“另一场任务”时自动播放；同一任务内的增量刷新不抢用户暂停。
  // 实时模式不自动播放：时间轴来自 started_at 的挂钟，不是从 0 开始的回放。
  useEffect(() => {
    if (!autoPlay || liveMode) return
    if (autoTitle.current === scenario.title) return
    autoTitle.current = scenario.title
    const timer = window.setTimeout(() => play(), 600)
    return () => window.clearTimeout(timer)
  }, [autoPlay, liveMode, play, scenario.title])

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">研枢 · 智能体集群可视化</div>
        <div className="scenario-tabs">
          <a className={`tab ${liveMode && !livePinned ? 'active' : ''}`} href="?live=1">● 实时跟随</a>
          {scenarios.map((s) => (
            <button
              key={s.title}
              className={`tab ${s.title === scenario.title ? 'active' : ''}`}
              onClick={() => {
                // 点选示例 = 退出实时跟随，否则 1.5 秒内会被轮询覆盖回去；
                // 同时清掉"点选的 run"，深链/openRun 的增量轮询一并让位
                followLive.current = false
                pinnedRun.current = ''
                setLiveMode(false)
                setLivePinned(false)
                setLiveStart(null)
                setAutoPlay(true)
                setScenario(s)
              }}
            >
              {s.title}
            </button>
          ))}
        </div>
        <div className="controls">
          {liveMode ? (
            <>
              <span className="ctl active">● LIVE{livePinned ? ' · 选中任务' : ''}</span>
              {livePinned && (
                <button className="ctl primary" onClick={backToLatest}>↩ 回到最新</button>
              )}
              {inspecting && <button className="ctl primary" onClick={backToLive}>↩ 回到实时</button>}
              <span className="clock">
                {liveStatus === 'failed'
                  ? '失败 · 作业超时或异常退出'
                  : liveStale
                    ? `已中断 · 无更新 ${Math.round((liveIdle ?? 0) / 60)} 分钟`
                    : inspecting
                      ? `${time.toFixed(1)}s · 审视中`
                      : liveStart
                        ? `${time.toFixed(1)}s${liveDone ? ' · 已完成' : ' · 执行中'}`
                        : '等待任务…'}
              </span>
            </>
          ) : (
            <>
              {playing ? (
                <button className="ctl" onClick={pause}>⏸ 暂停</button>
              ) : (
                <button className="ctl primary" onClick={play}>▶ 播放</button>
              )}
              <button className="ctl" onClick={restart}>↺ 重播</button>
              {[1, 2, 4].map((v) => (
                <button key={v} className={`ctl ${speed === v ? 'active' : ''}`} onClick={() => setSpeed(v)}>
                  {v}×
                </button>
              ))}
              <input
                className="scrubber"
                type="range"
                min={0}
                max={duration}
                step={0.1}
                value={time}
                onChange={(ev) => seek(Number(ev.target.value))}
              />
              <span className="clock">{time.toFixed(1)} / {duration.toFixed(0)}s</span>
            </>
          )}
        </div>
      </header>

      {loadState !== 'builtin' && (
        <div className="instruction-bar" style={{ opacity: 0.9 }}>
          <span className="instruction-label">
            {loadState === 'remote' ? (liveMode ? '实时工作流' : '真实工作流')
              : loadState === 'waiting' ? '实时等待'
                : loadState === 'loading' ? '加载中' : '未找到本次工作流'}
          </span>
          <span className="instruction-text">
            {loadState === 'remote'
              ? `${liveMode ? `已跟随 ${scenario.title}（${liveStatus === 'failed' ? '失败' : liveStale ? '无更新' : liveDone ? '已完成' : '执行中'}）` : `已加载 ${scenario.title}`}`
              : loadState === 'waiting'
                ? '实时总览已连接，等待 supervisor 新任务；任务开始后会自动出现，不需要等终稿'
                : loadState === 'loading'
                  ? '正在加载本次 agent 协作轨迹…'
                  : '未找到本次任务的轨迹文件，已回退到内置演示场景'}
          </span>
        </div>
      )}

      <div className="instruction-bar">
        <span className="instruction-label">指令</span>
        <span className="instruction-text">{cleanInstruction(state.instruction || scenario.instruction)}</span>
        {state.plan.length > 0 && (
          <div className="plan-chips">
            {state.plan.map((p) => (
              <span key={p.id} className="plan-chip">{p.title}</span>
            ))}
          </div>
        )}
      </div>

      <DiscoveryBar items={state.discovered} agents={state.agents} />

      <div className="main">
        <div className="canvas-wrap">
          <CanvasView state={state} regions={scenario.regions} onOpenDomain={setOpenDomain} />
          {state.doneSummary && (
            <div className={`done-banner ${state.doneStatus === 'failed' ? 'failed' : ''}`}>
              {state.doneStatus === 'failed' ? '⛔' : '🏁'} {state.doneSummary}
            </div>
          )}
        </div>
        <aside className="side">
          <RunList runs={recentRuns} current={currentRunId} onPick={openRun} />
          <ProductPanel products={state.products} />
          <Ledger state={state} time={time} seek={seekLedger} />
        </aside>
      </div>

      <DomainDrawer domain={selectedDomain} state={state} onClose={() => setOpenDomain(null)} />
    </div>
  )
}
