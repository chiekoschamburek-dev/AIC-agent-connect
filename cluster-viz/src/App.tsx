import { useEffect, useRef, useState } from 'react'
import { usePlayer } from './engine'
import { CanvasView } from './flow/CanvasView'
import { Ledger, ProductPanel, WorkspacePanel } from './panels'
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
  updated_at?: number
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
  const autoTitle = useRef('')
  // 是否继续跟随实时任务。用户一旦点选示例（A/B/C）就置 false，
  // 否则实时轮询每 1.5 秒会把画面覆盖回最新 run——表现就是"点了没反应"。
  const followLive = useRef(true)

  // 实时总览：/?live=1。始终跟随 scenarios/live.json 指向的最新 run，
  // 旧回答里的链接也不会因为 run_id 重置而失效。
  useEffect(() => {
    const q = queryRun()
    if (!q.live) return
    setLiveMode(true)
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
        const scenarioRes = await fetch(`${BASE}scenarios/${encodeURIComponent(pointer.run_id)}.json`, {
          cache: 'no-store',
        })
        if (!scenarioRes.ok) {
          if (alive) setLoadState('waiting')
          if (alive) timer = window.setTimeout(load, 1200)
          return
        }
        const loaded = (await scenarioRes.json()) as Scenario
        if (!alive) return
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
    let alive = true
    let loadedOnce = false
    let timer: number | undefined
    setLoadState('loading')
    const load = async () => {
      try {
        const res = await fetch(url, { cache: 'no-store' })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const loaded = (await res.json()) as Scenario
        if (!alive) return
        loadedOnce = true
        setScenario(loaded)
        setScenarios((prev) => [loaded, ...prev.filter((s) => s.title !== loaded.title)])
        setAutoPlay(autoplay)
        setLoadState('remote')
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

  const { state, time, duration, playing, speed, setSpeed, play, pause, restart, seek } = usePlayer(
    scenario,
    liveMode ? liveStart : null,
    liveDone,
  )

  // 每次切换到“另一场任务”时自动播放；同一任务内的增量刷新不抢用户暂停。
  useEffect(() => {
    if (!autoPlay && !liveMode) return
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
          <a className={`tab ${liveMode ? 'active' : ''}`} href="?live=1">● 实时跟随</a>
          {scenarios.map((s) => (
            <button
              key={s.title}
              className={`tab ${s.title === scenario.title ? 'active' : ''}`}
              onClick={() => {
                // 点选示例 = 退出实时跟随，否则 1.5 秒内会被轮询覆盖回去
                followLive.current = false
                setLiveMode(false)
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
              <span className="ctl active">● LIVE</span>
              <span className="clock">
                {liveStart ? `${time.toFixed(1)}s${liveDone ? ' · 已完成' : ' · 执行中'}` : '等待任务…'}
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
              ? `${liveMode ? `已跟随 ${scenario.title}（${liveDone ? '已完成' : '执行中'}）` : `已加载 ${scenario.title}`}`
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

      <div className="main">
        <div className="canvas-wrap">
          <CanvasView state={state} regions={scenario.regions} />
          {state.doneSummary && <div className="done-banner">🏁 {state.doneSummary}</div>}
        </div>
        <aside className="side">
          <ProductPanel products={state.products} />
          <WorkspacePanel files={state.wsFiles} task={state.wsTask} />
          <Ledger state={state} seek={seek} />
        </aside>
      </div>
    </div>
  )
}
