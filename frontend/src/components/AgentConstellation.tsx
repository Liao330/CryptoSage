/*
 * AgentConstellation —— 轨道式指挥核心可视化 (mission-control orbital view)。
 * 增强：粒子尾迹、完成火花、相位过渡辉光、更强脉冲反馈。
 * 动效动机: 状态转换 (调度→采集→定论) + 层级引导 + 进度叙事。
 *
 * taste-skill 锁定: 关闭 pure black、无 AI 紫渐变、等宽数字、单一 radius 体系、动效必有动机。
 */

import React, { useMemo } from 'react'
import { motion, useReducedMotion } from 'framer-motion'

export type AgentStatus = 'idle' | 'active' | 'done'
export type Bias = 'bullish' | 'bearish' | 'neutral'
export type Phase = 'idle' | 'planning' | 'gathering' | 'synthesis' | 'critic' | 'done'

interface Spec {
  key: string
  code: string
  label: string
  icon: string
}

const SPECIALISTS: Spec[] = [
  { key: 'technical', code: 'TA', label: '技术面', icon: '📈' },
  { key: 'onchain', code: 'CH', label: '链上', icon: '⛓' },
  { key: 'derivatives', code: 'DX', label: '衍生品', icon: '💹' },
  { key: 'sentiment', code: 'SN', label: '舆情', icon: '📢' },
  { key: 'macro', code: 'MO', label: '宏观地缘', icon: '🌍' },
]

interface Props {
  status: Record<string, AgentStatus>
  bias: Record<string, Bias>
  phase: Phase
  criticRounds: number
}

const SIZE = 460
const CX = SIZE / 2
const CY = SIZE / 2
const R = 160

const biasColor = (b?: Bias): string => {
  if (b === 'bullish') return 'var(--long)'
  if (b === 'bearish') return 'var(--short)'
  return 'var(--neutral)'
}

function nodePos(i: number, total: number) {
  const angle = (-90 + (360 / total) * i) * (Math.PI / 180)
  return { x: CX + R * Math.cos(angle), y: CY + R * Math.sin(angle) }
}

const PHASE_TEXT: Record<Phase, string> = {
  idle: '待命',
  planning: '规划策略',
  gathering: '采集情报',
  synthesis: '融合研判',
  critic: '对抗审查',
  done: '定论',
}

const AgentConstellation: React.FC<Props> = ({ status, bias, phase, criticRounds }) => {
  const reduce = useReducedMotion()
  const anyActive = Object.values(status).some(s => s === 'active')
  const phaseActive = phase !== 'idle'
  const fusing = phase === 'synthesis' || phase === 'critic'
  const isDone = phase === 'done'

  // 预计算所有节点位置（避免每次渲染重复计算三角函数）
  const nodePositions = useMemo(
    () => SPECIALISTS.map((_, i) => nodePos(i, SPECIALISTS.length)),
    [],
  )

  // 完成火花
  const sparkles = useMemo(() => {
    if (!isDone) return []
    const result: { x: number; y: number; delay: number }[] = []
    for (let i = 0; i < 14; i++) {
      const angle = (i / 14) * Math.PI * 2
      const r = 40 + Math.random() * 50
      result.push({
        x: CX + r * Math.cos(angle),
        y: CY + r * Math.sin(angle),
        delay: Math.random() * 0.6,
      })
    }
    return result
  }, [isDone])

  return (
    <div style={{ width: '100%', display: 'flex', justifyContent: 'center' }}>
      <svg
        viewBox={`0 0 ${SIZE} ${SIZE}`}
        style={{ width: '100%', maxWidth: 480, height: 'auto' }}
        role="img"
        aria-label="Agent 指挥核心"
      >
        <defs>
          <radialGradient id="coreGlow" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="rgba(245,181,36,0.65)" />
            <stop offset="40%" stopColor="rgba(245,181,36,0.14)" />
            <stop offset="100%" stopColor="rgba(245,181,36,0)" />
          </radialGradient>
          <radialGradient id="greenGlow" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="rgba(47,208,138,0.5)" />
            <stop offset="60%" stopColor="rgba(47,208,138,0.08)" />
            <stop offset="100%" stopColor="rgba(47,208,138,0)" />
          </radialGradient>
          <filter id="soft" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="2.8" />
          </filter>
          <filter id="glow" x="-80%" y="-80%" width="260%" height="260%">
            <feGaussianBlur stdDeviation="5" />
          </filter>
        </defs>

        {/* 外层轨道环 */}
        <circle cx={CX} cy={CY} r={R} fill="none" stroke="var(--line)" strokeWidth={1} strokeDasharray="3 8" />
        <circle cx={CX} cy={CY} r={R - 38} fill="none" stroke="var(--line)" strokeWidth={0.6} />
        <circle cx={CX} cy={CY} r={R + 38} fill="none" stroke="var(--line)" strokeWidth={0.4} strokeDasharray="1 12" />

        {/* 连线 + 数据包（使用预计算位置减少重复三角函数） */}
        {SPECIALISTS.map((sp, i) => {
          const p = nodePositions[i]
          const st = status[sp.key] || 'idle'
          const col = st === 'done' ? biasColor(bias[sp.key]) : 'var(--accent)'
          const lineOpacity = st === 'idle' ? 0.14 : st === 'active' ? 0.85 : 0.5
          return (
            <g key={`line-${sp.key}`}>
              {/* 连线 */}
              <line
                x1={CX} y1={CY} x2={p.x} y2={p.y}
                stroke={col}
                strokeWidth={st === 'active' ? 1.8 : 1}
                strokeOpacity={lineOpacity}
                strokeDasharray={st === 'active' ? '4 3' : 'none'}
              >
                {st === 'active' && !reduce && (
                  <animate attributeName="stroke-dashoffset" from="14" to="0" dur="0.6s" repeatCount="indefinite" />
                )}
              </line>

              {/* 数据包: 活跃时沿线飞行（仅在活跃 agent 上显示，减少动画数） */}
              {st === 'active' && !reduce && anyActive && (
                <motion.circle
                  r={4}
                  fill="var(--accent)"
                  filter="url(#soft)"
                  initial={{ cx: CX, cy: CY, opacity: 0 }}
                  animate={{ cx: [CX, p.x], cy: [CY, p.y], opacity: [0, 1, 1, 0] }}
                  transition={{ duration: 0.9, repeat: Infinity, ease: 'easeInOut' }}
                />
              )}
            </g>
          )
        })}

        {/* 核心辉光 */}
        <circle cx={CX} cy={CY} r={74} fill="url(#coreGlow)" />
        {isDone && <circle cx={CX} cy={CY} r={80} fill="url(#greenGlow)" />}

        {/* 核心脉冲环 */}
        {phaseActive && !reduce && (
          <>
            <motion.circle
              cx={CX} cy={CY} fill="none"
              stroke={isDone ? 'var(--long)' : 'var(--accent-line)'}
              strokeWidth={isDone ? 1.6 : 1}
              initial={{ r: 28, opacity: 0.5 }}
              animate={{ r: [28, 90], opacity: [0.5, 0] }}
              transition={{ duration: 2.5, repeat: Infinity, ease: 'easeOut' }}
            />
            <motion.circle
              cx={CX} cy={CY} fill="none"
              stroke={isDone ? 'var(--long)' : 'var(--accent-line)'}
              strokeWidth={1}
              initial={{ r: 28, opacity: 0.5 }}
              animate={{ r: [28, 90], opacity: [0.5, 0] }}
              transition={{ duration: 2.5, repeat: Infinity, ease: 'easeOut', delay: 1.25 }}
            />
          </>
        )}

        {/* 雷达扫描扇形 */}
        {phaseActive && !reduce && (
          <motion.g
            style={{ originX: `${CX}px`, originY: `${CY}px` }}
            animate={{ rotate: 360 }}
            transition={{ duration: fusing ? 2.5 : 5, repeat: Infinity, ease: 'linear' }}
          >
            <path
              d={`M ${CX} ${CY} L ${CX} ${CY - 62} A 62 62 0 0 1 ${CX + 44} ${CY - 44} Z`}
              fill={isDone ? 'rgba(47,208,138,0.08)' : 'rgba(245,181,36,0.10)'}
            />
          </motion.g>
        )}

        {/* 完成火花 —— 用 r 动画替代 scale（SVG 下 scale 会丢失 r 属性） */}
        {isDone && !reduce && sparkles.map((s, i) => (
          <motion.circle
            key={`spark-${i}`}
            cx={s.x} cy={s.y}
            fill="var(--accent)"
            initial={{ r: 0, opacity: 0 }}
            animate={{ r: [0, 3, 0], opacity: [0, 1, 0] }}
            transition={{ duration: 1.2, delay: s.delay, ease: 'easeOut' }}
          />
        ))}

        {/* 核心体 —— 用 r 动画替代 scale（SVG motion.circle 中 scale 会丢失 r） */}
        <motion.circle
          cx={CX} cy={CY}
          fill="var(--surface-2)"
          stroke={isDone ? 'var(--long)' : fusing ? 'var(--accent)' : 'var(--accent-line)'}
          strokeWidth={fusing ? 2.2 : isDone ? 2 : 1.4}
          filter={isDone ? 'url(#glow)' : undefined}
          initial={{ r: 34 }}
          animate={reduce ? {} : { r: fusing ? [34, 36.5, 34] : isDone ? [34, 35.5, 34] : [34] }}
          transition={{
            duration: fusing ? 1.2 : 1.8,
            repeat: fusing || isDone ? Infinity : 0,
            ease: 'easeInOut',
          }}
        />
        <text x={CX} y={CY - 3} textAnchor="middle"
          fill={isDone ? 'var(--long)' : 'var(--accent)'}
          style={{ fontFamily: 'var(--font-mono)', fontSize: 13, fontWeight: 700, letterSpacing: 1 }}>
          CORE
        </text>
        <text x={CX} y={CY + 13} textAnchor="middle" fill="var(--text-2)"
          style={{ fontFamily: 'var(--font-mono)', fontSize: 9.5, letterSpacing: 1 }}>
          {PHASE_TEXT[phase]}
        </text>

        {/* 专家节点 */}
        {SPECIALISTS.map((sp, i) => {
          const p = nodePositions[i]
          const st = status[sp.key] || 'idle'
          const col = st === 'done' ? biasColor(bias[sp.key]) : 'var(--accent)'
          const labelBelow = p.y > CY
          const isActive = st === 'active'
          const isDoneAgent = st === 'done'

          return (
            <g key={sp.key}>
              {/* 活跃脉冲光晕 */}
              {isActive && !reduce && (
                <>
                  <motion.circle
                    cx={p.x} cy={p.y} fill="none" stroke="var(--accent)" strokeWidth={1}
                    initial={{ r: 22, opacity: 0.6 }}
                    animate={{ r: [22, 44], opacity: [0.6, 0] }}
                    transition={{ duration: 1.2, repeat: Infinity, ease: 'easeOut' }}
                  />
                  <motion.circle
                    cx={p.x} cy={p.y} fill="none" stroke="var(--accent)" strokeWidth={0.6}
                    initial={{ r: 22, opacity: 0.5 }}
                    animate={{ r: [22, 44], opacity: [0.5, 0] }}
                    transition={{ duration: 1.2, repeat: Infinity, ease: 'easeOut', delay: 0.6 }}
                  />
                </>
              )}

              {/* 完成光晕 —— 避免 SVG fillOpacity 与 animate opacity 冲突 */}
              {isDoneAgent && !reduce && (
                <motion.circle
                  cx={p.x} cy={p.y} fill={col} r={26}
                  filter="url(#soft)"
                  initial={{ fillOpacity: 0.1 }}
                  animate={{ r: [26, 34, 26], fillOpacity: [0.1, 0.04, 0.1] }}
                  transition={{ duration: 2.5, repeat: Infinity, ease: 'easeInOut' }}
                />
              )}

              {/* 节点主体 —— 用 r 动画替代 scale */}
              <motion.circle
                cx={p.x} cy={p.y}
                fill={st === 'idle' ? 'var(--surface)' : 'var(--surface-2)'}
                stroke={st === 'idle' ? 'var(--line-strong)' : col}
                strokeWidth={isActive ? 2.2 : isDoneAgent ? 1.8 : 1.4}
                filter={st !== 'idle' ? 'url(#soft)' : undefined}
                initial={{ r: 22 }}
                animate={reduce ? {} : { r: isActive ? [22, 24, 22] : [22] }}
                transition={{ duration: 0.8, repeat: isActive ? Infinity : 0 }}
              />

              {/* 内层实心 */}
              {isDoneAgent ? (
                <motion.text
                  x={p.x} y={p.y + 4.5} textAnchor="middle"
                  fill={col}
                  style={{ fontSize: 13 }}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  transition={{ duration: 0.3, type: 'spring' }}
                >
                  ✓
                </motion.text>
              ) : (
                <circle cx={p.x} cy={p.y} r={isActive ? 5 : 4}
                  fill={st === 'idle' ? 'var(--text-3)' : col}
                  opacity={isActive ? 0.9 : 0.8}
                />
              )}

              {/* 代号 */}
              <text x={p.x} y={p.y - 8} textAnchor="middle"
                fill={st === 'idle' ? 'var(--text-3)' : col}
                style={{ fontFamily: 'var(--font-mono)', fontSize: 9, fontWeight: 700, letterSpacing: 1 }}>
                {sp.code}
              </text>

              {/* 标签 */}
              <text
                x={p.x}
                y={labelBelow ? p.y + 42 : p.y - 32}
                textAnchor="middle"
                fill={st === 'idle' ? 'var(--text-3)' : 'var(--text-2)'}
                style={{ fontFamily: 'var(--font-sans)', fontSize: 10.5 }}
              >
                {sp.icon} {sp.label}
              </text>
            </g>
          )
        })}

        {/* 对抗审查计数 */}
        {criticRounds > 0 && (
          <motion.text
            x={CX} y={SIZE - 6} textAnchor="middle" fill="var(--short)"
            style={{ fontFamily: 'var(--font-mono)', fontSize: 10, letterSpacing: 1 }}
            initial={{ opacity: 0 }}
            animate={{ opacity: [0.5, 1, 0.5] }}
            transition={{ duration: 1.8, repeat: Infinity }}
          >
            {`⚔ ADVERSARIAL x${criticRounds}`}
          </motion.text>
        )}
      </svg>
    </div>
  )
}

export default AgentConstellation
