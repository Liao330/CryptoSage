/*
 * KlineChart —— K线图 + EMA + 成交量 (暗色)。
 * 增强：实时更新指示器、最后刷新时间、增量数据平滑过渡。
 */

import React, { useMemo, useEffect, useRef, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import { KlineData } from '../api/client'

interface Props {
  klines: KlineData[]
  live?: boolean
  lastRefresh?: number | null
}

const KlineChart: React.FC<Props> = ({ klines, live = false, lastRefresh }) => {
  const chartRef = useRef<any>(null)
  const [lastRefreshStr, setLastRefreshStr] = useState('')

  useEffect(() => {
    if (!lastRefresh) return
    const d = new Date(lastRefresh)
    setLastRefreshStr(`${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`)
  }, [lastRefresh])

  const option = useMemo(() => {
    if (!klines || klines.length === 0) return null

    const dates = klines.map(k => {
      const tsMs = k.ts > 1e12 ? k.ts : k.ts * 1000  // 兼容秒/毫秒级时间戳
      const d = new Date(tsMs)
      return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:00`
    })
    const ohlc = klines.map(k => [k.open, k.close, k.low, k.high])
    const volumes = klines.map(k => k.volume)

    const calcEMA = (data: number[], period: number) => {
      const k = 2 / (period + 1)
      const out: number[] = []
      let ema = data[0]
      for (let i = 0; i < data.length; i++) {
        ema = i === 0 ? data[0] : data[i] * k + ema * (1 - k)
        out.push(ema)
      }
      return out
    }

    const closes = klines.map(k => k.close)
    const ema25 = calcEMA(closes, 25)
    const ema200 = calcEMA(closes, 200)

    return {
      backgroundColor: 'transparent',
      textStyle: { color: '#9aa0b0', fontFamily: 'Geist Mono, JetBrains Mono, monospace' },
      animation: true,
      animationDuration: 600,
      animationEasing: 'cubicOut',
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', lineStyle: { color: 'rgba(245,181,36,0.4)' } },
        backgroundColor: 'rgba(20,24,36,0.96)',
        borderColor: 'rgba(255,255,255,0.13)',
        textStyle: { color: '#e8eaf0' },
      },
      legend: {
        data: [
          { name: 'K线', icon: 'roundRect' },
          { name: 'EMA25', icon: 'roundRect', itemStyle: { color: '#f5b524' } },
          { name: 'EMA200', icon: 'roundRect', itemStyle: { color: '#5c9dff' } },
          { name: '成交量', icon: 'roundRect' },
        ],
        top: 0,
        textStyle: { color: '#9aa0b0' },
      },
      grid: [
        { left: 58, right: 20, top: 36, height: '56%' },
        { left: 58, right: 20, top: '74%', height: '16%' },
      ],
      xAxis: [
        { type: 'category', data: dates, gridIndex: 0, axisLabel: { show: false }, axisLine: { lineStyle: { color: 'rgba(255,255,255,0.1)' } } },
        { type: 'category', data: dates, gridIndex: 1, axisLabel: { color: '#5c6373', fontSize: 10 }, axisLine: { lineStyle: { color: 'rgba(255,255,255,0.1)' } } },
      ],
      yAxis: [
        { type: 'value', gridIndex: 0, scale: true, axisLabel: { color: '#5c6373' }, splitLine: { lineStyle: { color: 'rgba(255,255,255,0.05)' } } },
        { type: 'value', gridIndex: 1, axisLabel: { color: '#5c6373', formatter: (v: number) => (v > 1000 ? `${(v / 1000).toFixed(0)}K` : String(v)) }, splitLine: { show: false } },
      ],
      series: [
        {
          name: 'K线', type: 'candlestick', data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
          itemStyle: { color: '#2fd08a', color0: '#f0556b', borderColor: '#2fd08a', borderColor0: '#f0556b' },
        },
        { name: 'EMA25', type: 'line', data: ema25, xAxisIndex: 0, yAxisIndex: 0, smooth: true, lineStyle: { width: 1.5, color: '#f5b524' }, symbol: 'none' },
        { name: 'EMA200', type: 'line', data: ema200, xAxisIndex: 0, yAxisIndex: 0, smooth: true, lineStyle: { width: 1.5, color: '#5c9dff' }, symbol: 'none' },
        { name: '成交量', type: 'bar', data: volumes, xAxisIndex: 1, yAxisIndex: 1, itemStyle: { color: 'rgba(124,134,160,0.5)' } },
      ],
    }
  }, [klines])

  if (!option) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: '8px 0' }}>
        <div className="skeleton" style={{ height: 300 }} />
        <div style={{ textAlign: 'center', color: 'var(--text-3)', fontSize: 12.5, marginTop: 6 }}>
          加载 K 线数据中...
        </div>
      </div>
    )
  }

  return (
    <div style={{ position: 'relative' }}>
      {/* 实时状态栏 */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8,
        fontSize: 10.5, fontFamily: 'var(--font-mono)',
      }}>
        {live && (
          <span style={{
            display: 'inline-flex', alignItems: 'center', gap: 4,
            color: 'var(--long)', fontWeight: 600,
          }}>
            <span className="pulse-dot" style={{ width: 5, height: 5 }} />
            LIVE
          </span>
        )}
        {lastRefreshStr && (
          <span style={{ color: 'var(--text-3)', marginLeft: 'auto' }}>
            刷新 {lastRefreshStr}
          </span>
        )}
      </div>
      <ReactECharts
        ref={chartRef}
        option={option}
        style={{ height: 410 }}
        notMerge={false}
        opts={{ renderer: 'canvas' }}
      />
    </div>
  )
}

export default KlineChart
