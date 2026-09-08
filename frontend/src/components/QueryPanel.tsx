/*
 * QueryPanel —— 指令输入命令栏 (command bar)。
 */

import React, { useState } from 'react'
import { motion } from 'framer-motion'
import { AnalyzeRequest } from '../api/client'

interface Props {
  onAnalyze: (req: AnalyzeRequest) => void
  loading: boolean
}

const QUICK_QUERIES = [
  '分析 BTC 当前是否适合进场',
  'BTC 近期趋势研判',
  'ETH 刚才突然大跌，发生了什么',
  '综合多维度分析当前市场',
]

const SYMBOLS = [
  { value: 'BTC-USDT', label: 'BTC' },
  { value: 'ETH-USDT', label: 'ETH' },
]

const QueryPanel: React.FC<Props> = ({ onAnalyze, loading }) => {
  const [query, setQuery] = useState('')
  const [symbol, setSymbol] = useState('BTC-USDT')

  const submit = () => {
    if (!query.trim() || loading) return
    onAnalyze({ symbol, query: query.trim() })
  }

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, alignItems: 'stretch', flexWrap: 'wrap' }}>
        {/* 币种切换 pill 组 */}
        <div style={{
          display: 'flex', background: 'var(--surface-2)', border: '1px solid var(--line)',
          borderRadius: 'var(--r-ctrl)', padding: 4, gap: 4,
        }}>
          {SYMBOLS.map(s => {
            const active = symbol === s.value
            return (
              <button
                key={s.value}
                onClick={() => setSymbol(s.value)}
                className="mono"
                style={{
                  border: 'none', cursor: 'pointer', padding: '8px 16px',
                  borderRadius: 8, fontSize: 13, fontWeight: 700, letterSpacing: 0.5,
                  background: active ? 'var(--accent-soft)' : 'transparent',
                  color: active ? 'var(--accent)' : 'var(--text-3)',
                  transition: 'all 0.2s',
                }}
              >
                {s.label}
              </button>
            )
          })}
        </div>

        {/* 输入框 */}
        <div style={{
          flex: 1, minWidth: 240, display: 'flex', alignItems: 'center', gap: 10,
          background: 'var(--surface-2)', border: '1px solid var(--line)',
          borderRadius: 'var(--r-ctrl)', padding: '0 14px',
        }}>
          <span className="mono" style={{ color: 'var(--accent)', fontSize: 14 }}>&gt;_</span>
          <input
            value={query}
            maxLength={2000}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={onKey}
            placeholder="下达分析指令，如：分析 BTC 当前是否适合进场"
            style={{
              flex: 1, background: 'transparent', border: 'none', outline: 'none',
              color: 'var(--text)', fontSize: 14, padding: '13px 0', fontFamily: 'var(--font-sans)',
            }}
          />
        </div>

        {/* 提交 */}
        <motion.button
          onClick={submit}
          disabled={!query.trim() || loading}
          whileTap={{ scale: 0.97 }}
          style={{
            border: 'none', cursor: query.trim() && !loading ? 'pointer' : 'not-allowed',
            padding: '0 26px', borderRadius: 'var(--r-ctrl)', fontSize: 14, fontWeight: 700,
            background: query.trim() && !loading ? 'var(--accent)' : 'var(--surface-3)',
            color: query.trim() && !loading ? '#1a1405' : 'var(--text-3)',
            transition: 'background 0.2s', minWidth: 108,
          }}
        >
          {loading ? '分析中' : '发起分析'}
        </motion.button>
      </div>

      {/* 快捷指令 */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 14 }}>
        {QUICK_QUERIES.map((q, i) => (
          <button
            key={i}
            disabled={loading}
            onClick={() => { setQuery(q); onAnalyze({ symbol, query: q }) }}
            style={{
              cursor: loading ? 'not-allowed' : 'pointer',
              background: 'transparent', border: '1px solid var(--line-strong)',
              color: 'var(--text-2)', borderRadius: 'var(--r-pill)',
              padding: '6px 14px', fontSize: 12.5, transition: 'all 0.18s',
            }}
            onMouseEnter={e => { if (!loading) { e.currentTarget.style.borderColor = 'var(--accent-line)'; e.currentTarget.style.color = 'var(--accent)' } }}
            onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--line-strong)'; e.currentTarget.style.color = 'var(--text-2)' }}
          >
            {q}
          </button>
        ))}
      </div>
    </div>
  )
}

export default QueryPanel
