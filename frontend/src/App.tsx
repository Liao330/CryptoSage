/*
 * App —— CryptoSage 作战指挥室 (mission-control cockpit)。
 * 暗色科技风格，Agent 轨道可视化 + 遥测终端流。
 * 增强：思考面板、工具链动画、联网来源标注、扫描线、步进耗时、执行历史、K线轮询。
 *
 * taste-skill 锁定: 关闭 pure black、无 AI 紫渐变、等宽数字、单一 radius 体系、动效必有动机。
 */

import React, { useState, useCallback, useRef, useEffect } from 'react'
import { motion } from 'framer-motion'
import QueryPanel from './components/QueryPanel'
import AgentConstellation, { AgentStatus, Bias, Phase } from './components/AgentConstellation'
import TelemetryStream, { LogEntry, LogKind, ToolStep } from './components/TelemetryStream'
import KlineChart from './components/KlineChart'
import SignalRadar from './components/SignalRadar'
import ReportCard from './components/ReportCard'
import CalibrationMonitor from './components/CalibrationMonitor'
import {
  startAnalysis,
  cancelAnalysis,
  connectWebSocket,
  getKlines,
  AnalyzeRequest,
  WSMessage,
  WSHandle,
  SignalAgent,
  SynthesisResult,
  KlineData,
} from './api/client'

const AGENT_META: Record<string, { code: string; label: string; isWebSearch?: boolean }> = {
  orchestrator: { code: 'CORE', label: '指挥核心' },
  technical: { code: 'TA', label: '技术面' },
  onchain: { code: 'CH', label: '链上' },
  derivatives: { code: 'DX', label: '衍生品' },
  sentiment: { code: 'SN', label: '舆情' },
  macro: { code: 'MO', label: '宏观地缘', isWebSearch: true },
}

const DIR_LABEL: Record<string, string> = { bullish: '看多', bearish: '看空', neutral: '中性', error: '分析异常' }
const BIAS_LABEL: Record<string, string> = { bullish: '偏多', bearish: '偏空', neutral: '中性' }

/* ── 执行历史类型 ── */
interface HistoryRecord {
  id: string
  time: string
  query: string
  symbol: string
  totalElapsed: number
  phase: Phase
  logs: LogEntry[]
  signals: SignalAgent[]
  report: SynthesisResult | null
}

const HISTORY_KEY = 'cryptosage_history'
const MAX_HISTORY = 20
// 全局单调递增日志 id（模块级，跨分析生命周期不重置，避免旧连接消息延迟到达时 key 冲突）
let globalLogId = 0

function loadHistory(): HistoryRecord[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY)
    return raw ? JSON.parse(raw) : []
  } catch { return [] }
}

function saveHistory(records: HistoryRecord[]) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(records.slice(0, MAX_HISTORY)))
  } catch { /* quota exceeded, silently skip */ }
}

/* 根据工具名推断工具步骤类型 */
function inferToolKind(name: string): 'web' | 'data' | 'default' {
  const lower = name.toLowerCase()
  if (/search|tavily|serpapi|web|news|scrape|crawl|fetch|http/i.test(lower)) return 'web'
  if (/kline|candle|price|orderbook|ticker|market|volume|ohlcv/i.test(lower)) return 'data'
  if (/balance|transfer|tx|transaction|etherscan|whale|exchange|flow|netflow/i.test(lower)) return 'data'
  if (/funding|open.interest|oi|liquidation/i.test(lower)) return 'data'
  if (/sentiment|fear|greed|index/i.test(lower)) return 'data'
  return 'default'
}

const Panel: React.FC<{ title?: string; right?: React.ReactNode; children: React.ReactNode; style?: React.CSSProperties }> =
  ({ title, right, children, style }) => (
    <div className="panel" style={{ padding: 18, ...style }}>
      {title && (
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 14 }}>
          <span className="eyebrow">{title}</span>
          <span style={{ marginLeft: 'auto' }}>{right}</span>
        </div>
      )}
      {children}
    </div>
  )

const App: React.FC = () => {
  const [loading, setLoading] = useState(false)
  const [taskId, setTaskId] = useState<string | null>(null)

  const [status, setStatus] = useState<Record<string, AgentStatus>>({})
  const [bias, setBias] = useState<Record<string, Bias>>({})
  const [phase, setPhase] = useState<Phase>('idle')
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [signals, setSignals] = useState<SignalAgent[]>([])
  const [criticRounds, setCriticRounds] = useState(0)
  const [synthesisResult, setSynthesisResult] = useState<SynthesisResult | null>(null)
  const [finalReport, setFinalReport] = useState<SynthesisResult | null>(null)
  const [klines, setKlines] = useState<KlineData[]>([])

  // 计时
  const [startTs, setStartTs] = useState<number | null>(null)
  const [totalElapsed, setTotalElapsed] = useState<number | null>(null)
  const [klineLastRefresh, setKlineLastRefresh] = useState<number | null>(null)

  // 当前分析元数据（用于保存历史）
  const currentMeta = useRef<{ query: string; symbol: string }>({ query: '', symbol: '' })

  // 历史记录视图
  const [showHistory, setShowHistory] = useState(false)
  const [historyRecords, setHistoryRecords] = useState<HistoryRecord[]>(() => loadHistory())
  const historyRef = useRef<HTMLDivElement>(null)

  // 打开历史时自动滚动
  useEffect(() => {
    if (showHistory && historyRef.current) {
      historyRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [showHistory])

  // 定时器
  const [now, setNow] = useState(Date.now())

  const wsRef = useRef<WSHandle | null>(null)
  const logId = useRef(0)
  const klineTimer = useRef<ReturnType<typeof setInterval> | null>(null)
  const cancelledRef = useRef(false)
  const replayRef = useRef(false)
  // 同步守卫：防止快速重复点击触发多次 POST /api/analyze
  const analyzingRef = useRef(false)
  // useEffect 幂等保护：同一 task_id 的历史只保存一次
  const savedTaskRef = useRef<string | null>(null)
  // 当前有效任务 id：用于过滤旧连接延迟到达的消息
  const activeTaskIdRef = useRef<string | null>(null)

  // 实时时钟（用于顶栏计时器）
  useEffect(() => {
    if (!loading) return
    const t = setInterval(() => setNow(Date.now()), 200)
    return () => clearInterval(t)
  }, [loading])

  const pushLog = useCallback((tag: string, text: string, kind: LogKind, extra?: Partial<LogEntry>) => {
    // 全局单调递增，不随分析重置，避免旧 WS 消息延迟到达时与新日志 id 冲突（重复 key 警告）
    globalLogId += 1
    setLogs(prev => [...prev, { id: globalLogId, tag, text, kind, ts: Date.now(), ...extra }])
  }, [])

  /* ── 终止分析 ── */
  const handleCancel = useCallback(() => {
    cancelledRef.current = true
    const activeTaskId = activeTaskIdRef.current
    activeTaskIdRef.current = null
    if (wsRef.current) { wsRef.current.close(); wsRef.current = null }
    if (klineTimer.current) { clearInterval(klineTimer.current); klineTimer.current = null }
    setLoading(false)
    setPhase('idle')
    setTotalElapsed(Date.now() - (startTs || Date.now()))
    pushLog('CORE', '⚠ 用户终止分析', 'critic')
    if (activeTaskId) {
      void cancelAnalysis(activeTaskId).catch((error) => {
        console.warn('后端任务取消失败:', error)
        pushLog('ERR', '后端取消请求失败，任务可能仍在收尾', 'critic')
      })
    }
  }, [startTs, pushLog])

  /* ── K线定时轮询 ── */
  const fetchKlines = useCallback((symbol: string) => {
    getKlines(symbol, '4H', 200)
      .then(res => {
        setKlines(res.data || [])
        setKlineLastRefresh(Date.now())
      })
      .catch(console.warn)
  }, [])

  useEffect(() => {
    // 分析完成后停止轮询
    if (!loading) {
      if (klineTimer.current) { clearInterval(klineTimer.current); klineTimer.current = null }
      return
    }
    // 分析进行中每 45 秒刷新一次 K线
    const sym = currentMeta.current.symbol || 'BTC-USDT'
    klineTimer.current = setInterval(() => fetchKlines(sym), 45000)
    return () => {
      if (klineTimer.current) { clearInterval(klineTimer.current); klineTimer.current = null }
    }
  }, [loading, fetchKlines])

  /* ── 分析完成后保存历史（终止的不保存；同一 task_id 只保存一次） ── */
  useEffect(() => {
    if (phase !== 'done' || !finalReport || cancelledRef.current || replayRef.current) return
    // 幂等保护：同一 task_id 只能保存一次（防止 React StrictMode / 重渲染 / WS 重连触发重复保存）
    if (savedTaskRef.current === taskId) return
    savedTaskRef.current = taskId || null
    const record: HistoryRecord = {
      id: taskId || '',
      time: new Date().toISOString(),
      query: currentMeta.current.query,
      symbol: currentMeta.current.symbol,
      totalElapsed: totalElapsed || 0,
      phase: 'done',
      logs,
      signals,
      report: finalReport,
    }
    const history = loadHistory()
    // 二次保护：按 id 去重，避免同 task_id 重复入栈
    const deduped = [record, ...history.filter(r => r.id !== record.id)]
    saveHistory(deduped.slice(0, MAX_HISTORY))
    setHistoryRecords(deduped.slice(0, MAX_HISTORY))
  }, [phase, finalReport]) // eslint-disable-line react-hooks/exhaustive-deps

  /* ── 加载历史记录回放 ── */
  const loadRecord = useCallback((rec: HistoryRecord) => {
    replayRef.current = true
    setTaskId(rec.id)
    setPhase(rec.phase)
    setLogs(rec.logs)
    setSignals(rec.signals)
    setFinalReport(rec.report)
    setSynthesisResult(null)
    setCriticRounds(0)
    setStatus({})
    setBias({})
    setKlines([])
    setLoading(false)
    setTotalElapsed(rec.totalElapsed)
    setStartTs(null)
    setShowHistory(false)
  }, [])

  const handleAnalyze = useCallback(async (req: AnalyzeRequest) => {
    // 同步守卫：阻止 React state 更新间隙的快速重复点击触发多次分析
    if (analyzingRef.current) {
      console.warn('[CryptoSage] 已有分析在进行中，忽略本次点击')
      return
    }
    analyzingRef.current = true
    // 停止旧定时器 + 重置取消标记
    if (klineTimer.current) { clearInterval(klineTimer.current); klineTimer.current = null }
    cancelledRef.current = false
    replayRef.current = false
    // 新一轮分析开始，清空幂等记录
    savedTaskRef.current = null

    const analysisStart = Date.now()
    setStartTs(analysisStart)
    setTotalElapsed(null)
    setLoading(true)
    setStatus({})
    setBias({})
    setPhase('planning')
    setLogs([])
    setSignals([])
    setCriticRounds(0)
    setSynthesisResult(null)
    setFinalReport(null)
    setKlines([])
    setKlineLastRefresh(null)
    logId.current = 0
    currentMeta.current = { query: req.query, symbol: req.symbol }

    pushLog('CORE', `接收指令：${req.query}（标的 ${req.symbol}）`, 'dispatch')

    // 首次加载 K线
    fetchKlines(req.symbol)

    try {
      const resp = await startAnalysis(req)
      setTaskId(resp.task_id)
      activeTaskIdRef.current = resp.task_id

      if (wsRef.current) wsRef.current.close()
      const ws = connectWebSocket(resp.task_id, (msg: WSMessage) => {
        // 忽略非当前任务的过期消息（防止旧连接延迟消息污染新一轮分析状态）
        if (msg.task_id && msg.task_id !== activeTaskIdRef.current) return
        switch (msg.type) {
          case 'agent_start': {
            const agent = msg.data.agent || 'orchestrator'
            const meta = AGENT_META[agent] || { code: agent.toUpperCase().slice(0, 3), label: agent }
            if (agent === 'orchestrator') {
              setPhase('planning')
              pushLog('CORE', '规划分析策略，拆解为多维度子任务', 'dispatch')
            } else if (agent === 'synthesis') {
              setPhase('synthesis')
              pushLog('FUSE', '融合各维度信号，生成初步研判', 'synthesis')
            } else if (agent === 'critic') {
              setPhase('critic')
            } else {
              setPhase('gathering')
              setStatus(prev => ({ ...prev, [agent]: 'active' }))
              const webHint = meta.isWebSearch ? ' · 启动联网搜索' : ''
              pushLog(meta.code, `${meta.label} Agent 已激活，开始采集与推理${webHint}`, 'dispatch')
            }
            break
          }
          case 'signal': {
            const s: SignalAgent = msg.data
            const meta = AGENT_META[s.agent] || { code: s.agent.toUpperCase().slice(0, 3), label: s.agent }
            setStatus(prev => ({ ...prev, [s.agent]: 'done' }))
            setBias(prev => ({ ...prev, [s.agent]: (s.bias as Bias) || 'neutral' }))
            setSignals(prev => [...prev, s])

            // 1) 工具链可视化
            if (s.tools && s.tools.length > 0) {
              const steps: ToolStep[] = s.tools.map((name) => ({
                name,
                status: 'done' as const,
                kind: inferToolKind(name),
              }))
              pushLog(meta.code, '', 'tool-chain', { toolSteps: steps, groupKey: s.agent })
            }

            // 2) 模型思考
            if (s.thinking && s.thinking.trim()) {
              pushLog(meta.code, '', 'thinking', {
                thinking: s.thinking.trim(),
                groupKey: s.agent,
              })
            }

            // 3) 信号产出
            const signalKind: LogKind = s.bias === 'bullish' ? 'signal-long' : s.bias === 'bearish' ? 'signal-short' : 'signal-neutral'
            pushLog(meta.code,
              `产出信号 ${BIAS_LABEL[s.bias] || s.bias} · 强度 ${s.score} · 可信度 ${Math.round((s.confidence || 0.5) * 100)}%`,
              signalKind, { groupKey: s.agent })
            break
          }
          case 'synthesis': {
            const syn: SynthesisResult = msg.data
            setSynthesisResult(syn)
            setPhase('synthesis')
            pushLog('FUSE', `融合各维度信号 · 市场状态 ${syn.market_state || '判定中'} · 初步 ${DIR_LABEL[syn.direction] || syn.direction || ''}`, 'synthesis')
            if (syn.thinking && syn.thinking.trim()) {
              pushLog('FUSE', '', 'thinking', {
                thinking: syn.thinking.trim(),
                groupKey: 'synthesis',
              })
            }
            break
          }
          case 'critic': {
            const round = msg.data.round || 0
            setCriticRounds(round)
            setPhase('critic')
            pushLog('CRIT', `第 ${round} 轮对抗审查，检验确认偏误与风险敞口`, 'critic')
            if (msg.data.thinking && msg.data.thinking.trim()) {
              pushLog('CRIT', '', 'thinking', {
                thinking: msg.data.thinking.trim(),
                groupKey: `critic-${round}`,
              })
            }
            if (Array.isArray(msg.data.critiques)) {
              msg.data.critiques.forEach((c: string) => {
                if (c && c.trim()) pushLog('CRIT', `质疑：${c.trim()}`, 'critic')
              })
            }
            break
          }
          case 'final': {
            const r: SynthesisResult = msg.data
            const endTs = Date.now()
            setFinalReport(r)
            setPhase('done')
            setTotalElapsed(endTs - analysisStart)
            setStatus(prev => {
              const next = { ...prev }
              Object.keys(next).forEach(k => { if (next[k] === 'active') next[k] = 'done' })
              return next
            })
            pushLog('DONE', `定论达成 ${DIR_LABEL[r.direction] || r.direction} · 置信度 ${Math.round((r.confidence || 0) * 100)}%`, 'final')
            setLoading(false)
            break
          }
        }
      },
      () => {
        console.warn('[CryptoSage] WebSocket 连接异常')
      },
      () => {
        pushLog('NET', '实时连接中断，正在从断点恢复', 'info')
      },
      () => {
        pushLog('ERR', '实时连接恢复失败，请重新发起分析', 'critic')
        setTotalElapsed(Date.now() - analysisStart)
        setLoading(false)
        setPhase('idle')
      })
      wsRef.current = ws
    } catch (e) {
      console.error('分析失败:', e)
      pushLog('ERR', '分析请求失败，请检查后端服务', 'critic')
      setTotalElapsed(Date.now() - analysisStart)
      setLoading(false)
      setPhase('idle')
    } finally {
      // 无论成功失败，都释放同步守卫
      analyzingRef.current = false
    }
  }, [pushLog, fetchKlines])

  const hasReport = synthesisResult || finalReport

  return (
    <div style={{ minHeight: '100dvh', paddingBottom: 48 }}>
      {/* 顶栏 */}
      <header style={{
        position: 'sticky', top: 0, zIndex: 40,
        borderBottom: '1px solid var(--line)',
        background: 'rgba(8,9,13,0.72)', backdropFilter: 'blur(12px)',
      }}>
        <div className="wrap" style={{ height: 60, display: 'flex', alignItems: 'center', gap: 14 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <motion.div
              animate={{ rotate: loading ? 360 : 0 }}
              transition={{ duration: 3, repeat: loading ? Infinity : 0, ease: 'linear' }}
              style={{
                width: 26, height: 26, borderRadius: 8,
                border: '1.5px solid var(--accent)', display: 'grid', placeItems: 'center',
                boxShadow: loading ? '0 0 22px rgba(245,181,36,0.45)' : '0 0 18px rgba(245,181,36,0.35)',
              }}
            >
              <span style={{ width: 8, height: 8, borderRadius: 999, background: 'var(--accent)' }} />
            </motion.div>
            <span style={{ fontSize: 17, fontWeight: 800, letterSpacing: '-0.02em' }}>CryptoSage</span>
          </div>
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', letterSpacing: '0.08em' }}>
            MULTI-AGENT / 加密市场作战室
          </span>
          {/* 实时计时器 + 终止按钮 */}
          {loading && (
            <>
              <motion.button
                onClick={handleCancel}
                whileTap={{ scale: 0.95 }}
                style={{
                  marginLeft: 'auto', border: '1px solid var(--short)', borderRadius: 'var(--r-ctrl)',
                  background: 'rgba(240,85,107,0.1)', color: 'var(--short)',
                  padding: '4px 14px', fontSize: 11.5, fontWeight: 600, cursor: 'pointer',
                  fontFamily: 'var(--font-mono)', letterSpacing: '0.04em',
                }}
              >
                ⏹ 终止
              </motion.button>
              <span className="mono" style={{
                fontSize: 11, color: 'var(--accent)',
                letterSpacing: '0.06em',
              }}>
                已运行 {startTs ? ((now - startTs) / 1000).toFixed(1) : '0.0'}s
              </span>
            </>
          )}
          {!loading && (
            <>
              <button
                onClick={() => { setShowHistory(v => !v); setHistoryRecords(loadHistory()) }}
                style={{
                  marginLeft: 'auto', border: '1px solid var(--line-strong)', borderRadius: 'var(--r-ctrl)',
                  background: showHistory ? 'var(--accent-soft)' : 'transparent',
                  color: showHistory ? 'var(--accent)' : 'var(--text-2)',
                  padding: '4px 12px', fontSize: 11, cursor: 'pointer',
                  fontFamily: 'var(--font-mono)', letterSpacing: '0.04em',
                  transition: 'all 0.2s',
                }}
              >
                📋 历史 ({historyRecords.length})
              </button>
            </>
          )}
        </div>
      </header>

      <main className="wrap" style={{ marginTop: 20, display: 'flex', flexDirection: 'column', gap: 18 }}>
        {/* 命令栏 */}
        <Panel style={{ padding: 20 }}>
          <QueryPanel onAnalyze={handleAnalyze} loading={loading} />
        </Panel>

        {/* 历史记录面板 —— 紧贴命令栏，点击即见 */}
        {showHistory && (
          <div ref={historyRef}>
            <HistoryPanel records={historyRecords} onLoad={loadRecord} onDelete={(id) => {
              const all = loadHistory()
              const updated = all.filter(r => r.id !== id)
              saveHistory(updated)
              setHistoryRecords(updated)
            }} />
          </div>
        )}

        {/* 指挥核心 + 遥测流 */}
        <div className="grid-main">
          <Panel title="orchestration / 指挥核心">
            <AgentConstellation status={status} bias={bias} phase={phase} criticRounds={criticRounds} />
          </Panel>
          <Panel>
            <TelemetryStream logs={logs} loading={loading} startTs={startTs} totalElapsed={totalElapsed} />
          </Panel>
        </div>

        {/* 信号雷达 + 研判 */}
        <div className="grid-main">
          <Panel title="signals / 多维度信号">
            <SignalRadar signals={signals} />
          </Panel>
          <Panel title="verdict / 综合研判">
            {hasReport ? (
              <ReportCard report={(finalReport || synthesisResult)!} criticRounds={criticRounds} />
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <div className="skeleton" style={{ height: 68 }} />
                <div className="skeleton" style={{ height: 16, width: '70%' }} />
                <div className="skeleton" style={{ height: 16, width: '85%' }} />
                <div className="skeleton" style={{ height: 16, width: '55%' }} />
                <div style={{ color: 'var(--text-3)', fontSize: 12.5, marginTop: 4 }}>
                  综合研判将在各 Agent 信号采集与对抗审查完成后生成。
                </div>
              </div>
            )}
          </Panel>
        </div>

        {/* K线 */}
        <Panel title="chart / K线图表 · BTC-USDT · 4H">
          <KlineChart klines={klines} live={loading} lastRefresh={klineLastRefresh} />
        </Panel>

        <Panel title="validation / 影子交易与 Agent 漂移监控">
          <CalibrationMonitor refreshKey={`${taskId || ''}:${phase}`} />
        </Panel>

        {/* 免责声明 */}
        <div style={{
          padding: '14px 18px', borderRadius: 'var(--r-card)',
          border: '1px solid rgba(245,181,36,0.28)', background: 'var(--accent-soft)',
        }}>
          <span className="mono" style={{ color: 'var(--accent)', fontSize: 11, marginRight: 8 }}>DISCLAIMER</span>
          <span style={{ color: 'var(--text-2)', fontSize: 12.5, lineHeight: 1.6 }}>
            本工具仅供研究学习与教育目的使用，所有分析结果均不构成任何形式的投资建议。加密货币市场具有极高风险，价格波动可能导致本金全部损失。请自行判断并承担投资风险。
          </span>
        </div>

        <footer style={{ textAlign: 'center', color: 'var(--text-3)', fontSize: 11.5, marginTop: 4 }}>
          <span className="mono">CryptoSage · Powered by Hy3 Model API · 个人参赛作品（2026 犀牛鸟开源人才培养计划 · 混元大语言模型项目实战任务）</span>
        </footer>
      </main>
    </div>
  )
}

/* ── 历史记录面板 ── */
const HistoryPanel: React.FC<{
  records: HistoryRecord[]
  onLoad: (rec: HistoryRecord) => void
  onDelete: (id: string) => void
}> = ({ records, onLoad, onDelete }) => {
  if (records.length === 0) {
    return (
      <Panel title="archive / 执行历史">
        <div style={{ color: 'var(--text-3)', fontSize: 12.5, padding: '20px 0', textAlign: 'center' }}>
          暂无历史记录。完成一次分析后将自动保存。
        </div>
      </Panel>
    )
  }

  return (
    <Panel title={`archive / 执行历史 · ${records.length} 条`}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 400, overflowY: 'auto' }}>
        {records.map((rec) => {
          const d = new Date(rec.time)
          const timeStr = `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
          const dir = rec.report?.direction || 'neutral'
          const dirLabel = DIR_LABEL[dir] || dir
          const conf = rec.report?.confidence != null ? Math.round(rec.report.confidence * 100) : '?'
          const dirColor = dir === 'bullish' ? 'var(--long)' : dir === 'bearish' ? 'var(--short)' : 'var(--neutral)'
          const elapsed = rec.totalElapsed > 0 ? `${(rec.totalElapsed / 1000).toFixed(1)}s` : '?'
          return (
            <motion.div
              key={rec.id}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              style={{
                display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
                padding: '12px 16px', background: 'var(--surface-2)',
                borderRadius: 'var(--r-ctrl)', border: '1px solid var(--line)',
              }}
            >
              <span className="mono" style={{ color: 'var(--text-3)', fontSize: 10.5, minWidth: 72 }}>
                {timeStr}
              </span>
              <span className="mono" style={{
                color: 'var(--accent)', fontSize: 10, background: 'var(--accent-soft)',
                padding: '2px 8px', borderRadius: 'var(--r-pill)',
                maxWidth: 100, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }} title={rec.symbol}>
                {rec.symbol}
              </span>
              <span style={{
                color: 'var(--text)', fontSize: 13, flex: 1, minWidth: 180,
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}>
                {rec.query}
              </span>
              <span className="mono" style={{ color: dirColor, fontSize: 13, fontWeight: 700, minWidth: 42 }}>
                {dirLabel}
              </span>
              <span className="mono" style={{ color: 'var(--text-2)', fontSize: 11, minWidth: 36 }}>
                {conf}%
              </span>
              <span className="mono" style={{ color: 'var(--text-3)', fontSize: 10, minWidth: 40 }}>
                {elapsed}
              </span>
              <span className="mono" style={{ color: 'var(--text-3)', fontSize: 10 }} title="前端展示日志条数，不等于后端执行节点数">
                UI {rec.logs.length}
              </span>
              {rec.report?.execution_metrics && (
                <span className="mono" style={{ color: 'var(--text-3)', fontSize: 10 }} title="后端 LangGraph 节点和工具调用统计">
                  BE {rec.report.execution_metrics.backend_trace_steps ?? 0} 节点 · {rec.report.execution_metrics.tool_call_count ?? 0} 工具
                </span>
              )}
              <div style={{ display: 'flex', gap: 6, marginLeft: 'auto' }}>
                <button
                  onClick={() => onLoad(rec)}
                  style={{
                    border: '1px solid var(--accent-line)', borderRadius: 'var(--r-ctrl)',
                    background: 'transparent', color: 'var(--accent)', cursor: 'pointer',
                    padding: '4px 12px', fontSize: 11, fontFamily: 'var(--font-mono)',
                    transition: 'all 0.15s',
                  }}
                  onMouseEnter={e => { e.currentTarget.style.background = 'var(--accent-soft)' }}
                  onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}
                >
                  回放
                </button>
                <button
                  onClick={() => onDelete(rec.id)}
                  style={{
                    border: '1px solid rgba(240,85,107,0.3)', borderRadius: 'var(--r-ctrl)',
                    background: 'transparent', color: 'var(--short)', cursor: 'pointer',
                    padding: '4px 8px', fontSize: 11, fontFamily: 'var(--font-mono)',
                    transition: 'all 0.15s',
                  }}
                  title="删除"
                >
                  ✕
                </button>
              </div>
            </motion.div>
          )
        })}
      </div>
    </Panel>
  )
}

export default App
