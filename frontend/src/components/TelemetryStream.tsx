/*
 * TelemetryStream —— 遥测终端流 (作战日志)。
 * 增强：思考面板（可折叠）、工具链动画、联网来源标注、扫描线、弹性入场。
 * 动效动机: 反馈 (实时确认) + 叙事 (按序揭示) + 分层 (信息密度可控)。
 *
 * taste-skill 锁定: 关闭 pure black、无 AI 紫渐变、等宽数字、单一 radius 体系、动效必有动机。
 */

import React, { useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

export type LogKind =
  | 'dispatch'
  | 'signal-long'
  | 'signal-short'
  | 'signal-neutral'
  | 'synthesis'
  | 'critic'
  | 'final'
  | 'info'
  | 'thinking'
  | 'tool-chain'
  | 'source'

export interface ToolStep {
  name: string
  status: 'running' | 'done'
  kind?: 'web' | 'data' | 'default'
}

export interface LogEntry {
  id: number
  tag: string
  text: string
  kind: LogKind
  /** 时间戳（毫秒），用于显示步进耗时 */
  ts?: number
  /** 可选：折叠的思考内容 */
  thinking?: string
  /** 可选：工具链步骤 */
  toolSteps?: ToolStep[]
  /** 可选：联网来源 */
  sources?: string[]
  /** 可选：关联 Agent key（用于分组） */
  groupKey?: string
}

const KIND_COLOR: Record<LogKind, string> = {
  dispatch: 'var(--accent)',
  'signal-long': 'var(--long)',
  'signal-short': 'var(--short)',
  'signal-neutral': 'var(--neutral)',
  synthesis: 'var(--accent)',
  critic: 'var(--short)',
  final: 'var(--long)',
  info: 'var(--text-2)',
  thinking: 'var(--text-2)',
  'tool-chain': 'var(--accent)',
  source: '#7ab4ff',
}

const WAIT_PHRASES = [
  '正在唤醒专家 Agent 集群',
  '编排多维度情报采集路径',
  '同步链上与衍生品数据流',
  '校准市场状态权重矩阵',
  '准备对抗式审查回路',
]

/* ─── 思考面板（可折叠） ─── */
const ThinkingBlock: React.FC<{ thinking: string; tag: string }> = ({ thinking, tag }) => {
  const [open, setOpen] = useState(false)
  return (
    <motion.div
      className="think-panel"
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      transition={{ duration: 0.35, ease: 'easeOut' }}
    >
      <div className="think-header" onClick={() => setOpen(v => !v)}>
        <span style={{ fontSize: 13, lineHeight: 1 }}>{open ? '▼' : '▶'}</span>
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: 5,
          color: 'var(--accent)', fontSize: 10.5,
          fontFamily: 'var(--font-mono)', letterSpacing: '0.06em',
        }}>
          🧠 {tag} · 推理过程
        </span>
        {!open && (
          <span style={{
            color: 'var(--text-3)', fontSize: 11.5,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, minWidth: 0,
          }}>
            {thinking.slice(0, 56)}{thinking.length > 56 ? '…' : ''}
          </span>
        )}
      </div>
      <AnimatePresence>
        {open && (
          <motion.div
            key={`think-${tag}`}
            className="think-body"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.25 }}
          >
            {thinking}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

/* ─── 工具链可视化 ─── */
const ToolChainBlock: React.FC<{ steps: ToolStep[]; tag: string }> = ({ steps, tag }) => {
  const reduce = useReducedMotion()
  return (
    <motion.div
      className="tool-chain"
      initial={{ opacity: 0, x: -8 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.35 }}
    >
      <span className="mono" style={{ color: 'var(--text-3)', fontSize: 10, marginRight: 4 }}>
        {tag}
      </span>
      {steps.map((s, i) => (
        <React.Fragment key={i}>
          {i > 0 && <span className="tool-arrow">→</span>}
          <motion.span
            className={`tool-node ${s.status === 'done' ? 'executed' : 'pending'} ${s.kind === 'web' ? 'web' : ''}`}
            initial={{ opacity: 0, scale: 0.8 }}
            animate={{
              opacity: 1,
              scale: s.status === 'running' && !reduce ? [1, 1.06, 1] : 1,
            }}
            transition={{
              delay: i * 0.12,
              scale: s.status === 'running' ? { duration: 0.8, repeat: Infinity } : { duration: 0.25 },
            }}
          >
            {s.kind === 'web' && <span style={{ fontSize: 11 }}>🌐</span>}
            {s.kind === 'data' && <span style={{ fontSize: 11 }}>📊</span>}
            {s.name}
            {s.status === 'running' && <span className="pulse-dot" style={{ width: 5, height: 5 }} />}
            {s.status === 'done' && <span style={{ color: 'var(--long)', fontSize: 10, marginLeft: 2 }}>✓</span>}
          </motion.span>
        </React.Fragment>
      ))}
    </motion.div>
  )
}

/* ─── 来源标注 ─── */
const SourceTags: React.FC<{ sources: string[] }> = ({ sources }) => (
  <motion.div
    initial={{ opacity: 0 }}
    animate={{ opacity: 1 }}
    transition={{ duration: 0.3, delay: 0.15 }}
    style={{ display: 'flex', flexWrap: 'wrap', gap: 4, padding: '4px 0 4px 18px' }}
  >
    <span className="mono" style={{ color: 'var(--text-3)', fontSize: 10, lineHeight: '22px', marginRight: 4 }}>
      🔗
    </span>
    {sources.map((src, i) => (
      <span key={i} className="source-tag" title={src}>
        {src}
      </span>
    ))}
  </motion.div>
)

/* ─── 格式化耗时 ─── */
function fmtElapsed(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  const sec = (ms / 1000).toFixed(1)
  return `${sec}s`
}

/* ─── 单行条目 ─── */
const LogLine: React.FC<{ entry: LogEntry; reduce: boolean; startTs?: number | null }> =
  ({ entry, reduce, startTs }) => {
  const color = KIND_COLOR[entry.kind]
  const isThinking = entry.kind === 'thinking'
  const isToolChain = entry.kind === 'tool-chain'
  const isSource = entry.kind === 'source'
  const elapsed = startTs && entry.ts ? entry.ts - startTs : null

  if (isToolChain && entry.toolSteps) {
    return (
      <div style={{ display: 'flex', alignItems: 'baseline' }}>
        <ToolChainBlock steps={entry.toolSteps} tag={entry.tag} />
        {elapsed != null && elapsed >= 0 && (
          <span className="mono" style={{ color: 'var(--text-3)', fontSize: 9.5, marginLeft: 6, flexShrink: 0, marginTop: 9 }}>
            +{fmtElapsed(elapsed)}
          </span>
        )}
      </div>
    )
  }
  if (isSource && entry.sources) {
    return (
      <div style={{ display: 'flex', alignItems: 'baseline' }}>
        <SourceTags sources={entry.sources} />
        {elapsed != null && elapsed >= 0 && (
          <span className="mono" style={{ color: 'var(--text-3)', fontSize: 9.5, marginLeft: 6, flexShrink: 0 }}>
            +{fmtElapsed(elapsed)}
          </span>
        )}
      </div>
    )
  }
  if (isThinking && entry.thinking) {
    return (
      <div style={{ display: 'flex', alignItems: 'flex-start' }}>
        <ThinkingBlock thinking={entry.thinking} tag={entry.tag} />
        {elapsed != null && elapsed >= 0 && (
          <span className="mono" style={{ color: 'var(--text-3)', fontSize: 9.5, marginLeft: 6, flexShrink: 0, marginTop: 12 }}>
            +{fmtElapsed(elapsed)}
          </span>
        )}
      </div>
    )
  }

  // 普通文本行，保留逐字打印体验
  return <TextLine entry={entry} color={color} reduce={reduce} elapsed={elapsed} />
}

/* ─── 逐字打印文本行 ─── */
const TextLine: React.FC<{ entry: LogEntry; color: string; reduce: boolean; elapsed?: number | null }> =
  ({ entry, color, reduce, elapsed }) => {
  const noType = reduce || entry.kind === 'thinking'
  const [shown, setShown] = useState(noType ? entry.text : '')
  const [typing, setTyping] = useState(!noType)

  useEffect(() => {
    if (noType) { setShown(entry.text); setTyping(false); return }
    let i = 0
    const timer = setInterval(() => {
      i += 2
      setShown(entry.text.slice(0, i))
      if (i >= entry.text.length) {
        clearInterval(timer)
        setTyping(false)
      }
    }, 18)
    return () => clearInterval(timer)
  }, [entry.text, noType])

  return (
    <motion.div
      initial={reduce ? false : { opacity: 0, x: -6 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.25 }}
      className={entry.kind === 'signal-long' || entry.kind === 'signal-short' ? 'signal-flash' : ''}
      style={{ display: 'flex', gap: 10, padding: '4px 0', alignItems: 'baseline' }}
    >
      <span className="mono" style={{ color: 'var(--text-3)', fontSize: 11, flexShrink: 0 }}>&gt;</span>
      <span className="mono" style={{
        color, fontSize: 11, flexShrink: 0, minWidth: 74,
        textTransform: 'uppercase', letterSpacing: '0.06em',
      }}>
        {entry.tag}
      </span>
      <span style={{
        color: entry.kind === 'thinking' ? 'var(--text-2)' : 'var(--text)',
        fontSize: 12.5,
        lineHeight: 1.55,
        whiteSpace: 'pre-wrap',
      }}>
        {shown}
        {typing && <span className="caret" />}
      </span>
      {elapsed != null && elapsed >= 0 && (
        <span className="mono" style={{
          color: 'var(--text-3)', fontSize: 9.5, flexShrink: 0, marginLeft: 'auto',
          alignSelf: 'baseline',
        }}>
          +{fmtElapsed(elapsed)}
        </span>
      )}
    </motion.div>
  )
}

/* ─── 主组件 ─── */
interface Props {
  logs: LogEntry[]
  loading: boolean
  startTs?: number | null
  totalElapsed?: number | null
}

const TelemetryStream: React.FC<Props> = ({ logs, loading, startTs, totalElapsed }) => {
  const reduce = useReducedMotion()
  const scrollRef = useRef<HTMLDivElement>(null)
  const [phraseIdx, setPhraseIdx] = useState(0)
  const [scanY, setScanY] = useState(0)

  // 自动滚动到底部
  useEffect(() => {
    if (scrollRef.current) {
      const el = scrollRef.current
      el.scrollTo({ top: el.scrollHeight, behavior: reduce ? 'auto' : 'smooth' })
    }
  }, [logs, reduce])

  // 扫描线动画
  useEffect(() => {
    if (!loading) { setScanY(0); return }
    const t = setInterval(() => {
      setScanY(p => (p >= 100 ? 0 : p + 1.8))
    }, 40)
    return () => clearInterval(t)
  }, [loading])

  // 等待短语轮播
  useEffect(() => {
    if (!loading || logs.length > 0) return
    const t = setInterval(() => setPhraseIdx(p => (p + 1) % WAIT_PHRASES.length), 1800)
    return () => clearInterval(t)
  }, [loading, logs.length])

  const empty = logs.length === 0

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', position: 'relative' }}>
      {/* 扫描线 */}
      {loading && (
        <motion.div
          className="scan-line"
          animate={{ top: reduce ? '50%' : `${scanY}%` }}
          transition={reduce ? {} : { duration: 0.04, ease: 'linear' }}
        />
      )}

      {/* 顶栏 */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10,
        paddingBottom: 10, borderBottom: '1px solid var(--line)',
      }}>
        <span style={{ display: 'flex', gap: 6 }}>
          <i style={dot('var(--short)')} />
          <i style={dot('var(--accent)')} />
          <i style={dot('var(--long)')} />
        </span>
        <span className="eyebrow">telemetry / 作战日志</span>
        {loading && (
          <motion.span
            className="mono"
            style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--accent)' }}
            animate={reduce ? {} : { opacity: [0.4, 1, 0.4] }}
            transition={{ duration: 1.4, repeat: Infinity }}
          >
            LIVE
          </motion.span>
        )}
      </div>

      {/* 日志流 */}
      <div
        ref={scrollRef}
        style={{
          flex: 1, minHeight: 240, maxHeight: 380, overflowY: 'auto',
          fontFamily: 'var(--font-mono)',
        }}
      >
        {/* 空态：等待中 */}
        {empty && loading && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 0' }}>
            <span className="mono" style={{ color: 'var(--accent)', fontSize: 12 }}>&gt;</span>
            <AnimatePresence mode="wait">
              <motion.span
                key={phraseIdx}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={{ duration: 0.3 }}
                style={{ color: 'var(--text-2)', fontSize: 12.5 }}
              >
                {WAIT_PHRASES[phraseIdx]}
              </motion.span>
            </AnimatePresence>
            <span className="think-dots">
              <span /><span /><span />
            </span>
          </div>
        )}

        {/* 空态：就绪 */}
        {empty && !loading && (
          <div style={{ padding: '30px 6px', color: 'var(--text-3)', fontSize: 12.5 }}>
            系统待命。发起一次分析，指挥核心将实时广播每个 Agent 的动作。
          </div>
        )}

        {/* 日志条目 —— 不用 AnimatePresence，每个条目自带独立动画避免 key 冲突 */}
        {logs.map((e) => (
          <LogLine key={e.id} entry={e} reduce={Boolean(reduce)} startTs={startTs} />
        ))}

        {/* 总耗时 */}
        {totalElapsed != null && totalElapsed > 0 && (
          <div style={{
            textAlign: 'right', padding: '10px 6px', borderTop: '1px solid var(--line)',
            marginTop: 6,
          }}>
            <span className="mono" style={{ color: 'var(--accent)', fontSize: 11.5 }}>
              总耗时 {fmtElapsed(totalElapsed)}
            </span>
          </div>
        )}
      </div>
    </div>
  )
}

const dot = (c: string): React.CSSProperties => ({
  width: 9, height: 9, borderRadius: 999, background: c, display: 'inline-block', opacity: 0.85,
})

export default TelemetryStream
