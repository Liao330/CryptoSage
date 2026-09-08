import React, { useCallback, useEffect, useState } from 'react'
import {
  AgentPerformanceReport,
  DriftReport,
  ShadowStatsReport,
  getAgentPerformance,
  getDriftReport,
  getShadowStats,
} from '../api/client'

interface Props {
  refreshKey?: string
}

const LABELS: Record<string, string> = {
  technical: '技术面',
  onchain: '链上',
  derivatives: '衍生品',
  sentiment: '舆情',
  macro: '宏观',
}

const STATUS: Record<string, { label: string; color: string }> = {
  insufficient_data: { label: '等待样本', color: 'var(--text-3)' },
  stable: { label: '稳定', color: 'var(--long)' },
  improving: { label: '改善', color: 'var(--accent)' },
  degrading: { label: '漂移告警', color: 'var(--short)' },
}

const CalibrationMonitor: React.FC<Props> = ({ refreshKey }) => {
  const [performance, setPerformance] = useState<AgentPerformanceReport | null>(null)
  const [drift, setDrift] = useState<DriftReport | null>(null)
  const [shadow, setShadow] = useState<ShadowStatsReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [performanceResult, driftResult, shadowResult] = await Promise.all([
        getAgentPerformance(),
        getDriftReport(),
        getShadowStats(),
      ])
      setPerformance(performanceResult)
      setDrift(driftResult)
      setShadow(shadowResult)
    } catch (err) {
      setError(err instanceof Error ? err.message : '监控数据加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh, refreshKey])

  if (loading && !shadow) {
    return <div className="skeleton" style={{ height: 110 }} />
  }
  if (error) {
    return <div style={{ color: 'var(--short)', fontSize: 12.5 }}>{error}</div>
  }

  const metrics = shadow?.metrics
  const records = shadow?.records || []
  const latestTracking = records.find(record => record.status === 'tracking')
  const activeMultipliers = shadow?.dynamic_weight_multipliers || {}
  const calibrationSampleCount = performance?.total_samples ?? 0
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap', paddingBottom: 14, borderBottom: '1px solid var(--line)' }}>
        {[
          ['已结算', metrics?.resolved_count ?? 0],
          ['追踪中', shadow?.tracking_count ?? 0],
          ['胜率', `${(metrics?.win_rate ?? 0).toFixed(1)}%`],
          ['累计收益', `${(metrics?.cumulative_pnl_pct ?? 0).toFixed(2)}%`],
          ['最大回撤', `${(metrics?.max_drawdown_pct ?? 0).toFixed(2)}%`],
        ].map(([label, value]) => (
          <div key={label} style={{ minWidth: 82 }}>
            <div className="eyebrow" style={{ marginBottom: 4 }}>{label}</div>
            <div className="mono" style={{ color: 'var(--text)', fontSize: 15 }}>{value}</div>
          </div>
        ))}
        <button onClick={() => void refresh()} title="刷新校准监控" aria-label="刷新校准监控" style={{ marginLeft: 'auto', width: 30, height: 30, border: '1px solid var(--line-strong)', background: 'transparent', color: 'var(--text-2)', cursor: 'pointer', borderRadius: 'var(--r-ctrl)', fontSize: 17 }}>↻</button>
      </div>

      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', marginTop: 12, padding: '10px 12px', background: 'var(--surface-2)', borderLeft: `2px solid ${shadow?.tracking_count ? 'var(--accent)' : 'var(--text-3)'}`, borderRadius: 'var(--r-ctrl)' }}>
        <span className="mono" style={{ color: shadow?.tracking_count ? 'var(--accent)' : 'var(--text-3)', fontSize: 11 }}>
          {shadow?.tracking_count ? `影子交易正在跟踪 ${shadow.tracking_count} 条` : '当前没有达到准入阈值的影子交易'}
        </span>
        <span className="mono" style={{ color: 'var(--text-3)', fontSize: 10 }}>
          结算周期 {shadow?.settlement_horizon_hours ?? 24}H · {shadow?.calibration_method || 'resolved_shadow_brier_v1'}
        </span>
        {latestTracking?.target_at && <span className="mono" style={{ color: 'var(--text-3)', fontSize: 10 }}>下一结算 {latestTracking.target_at}</span>}
      </div>

      <div style={{ marginTop: 10, padding: '9px 12px', border: '1px solid var(--line)', borderRadius: 'var(--r-ctrl)', color: 'var(--text-2)', fontSize: 11.5, lineHeight: 1.5 }}>
        <div><span style={{ color: 'var(--accent)' }}>动态权重</span> {calibrationSampleCount > 0 ? '已由历史结算校准并用于融合' : '暂无结算样本，当前使用 1.00 基线'}：{Object.entries(activeMultipliers).map(([agent, multiplier]) => `${LABELS[agent] || agent} ×${multiplier.toFixed(2)}`).join(' · ') || '等待首个结算样本'}</div>
        <div style={{ color: 'var(--text-3)', marginTop: 4 }}>准入：{shadow?.eligibility_policy || '方向明确且置信度 >= 60%'}</div>
        <div style={{ color: 'var(--text-3)', marginTop: 2 }}>漂移：{shadow?.drift_method || 'recent_vs_baseline_v1'} · 最近窗口与历史基线比较</div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 0, marginTop: 8 }}>
        {Object.entries(performance?.agents || {}).map(([agent, metric]) => {
          const driftState = drift?.agents?.[agent]?.status || 'insufficient_data'
          const state = STATUS[driftState] || STATUS.insufficient_data
          return (
            <div key={agent} style={{ padding: '10px 12px 10px 0', borderBottom: '1px solid var(--line)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                <span style={{ color: 'var(--text)', fontSize: 12.5 }}>{LABELS[agent] || agent}</span>
                <span className="mono" style={{ color: state.color, fontSize: 10 }}>{state.label}</span>
              </div>
              <div className="mono" style={{ color: metric.weight_multiplier >= 1 ? 'var(--long)' : 'var(--short)', fontSize: 11, marginTop: 5 }}>
                权重 ×{metric.weight_multiplier.toFixed(2)} · n={metric.sample_count}
              </div>
              <div className="mono" style={{ color: 'var(--text-3)', fontSize: 10, marginTop: 3 }}>
                漂移样本 {drift?.agents?.[agent]?.recent?.sample_count ?? 0} / 基线 {drift?.agents?.[agent]?.baseline?.sample_count ?? 0}
              </div>
            </div>
          )
        })}
      </div>

      {records.length > 0 && (
        <div style={{ marginTop: 12, borderTop: '1px solid var(--line)', paddingTop: 10 }}>
          <div className="eyebrow" style={{ marginBottom: 7 }}>最近影子交易记录</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
            {records.slice(0, 4).map(record => {
              const statusLabel = record.status === 'resolved' ? '已结算' : record.status === 'tracking' ? '追踪中' : '已忽略'
              const pnl = record.pnl_pct == null ? '—' : `${record.pnl_pct >= 0 ? '+' : ''}${record.pnl_pct.toFixed(2)}%`
              const directionLabel = record.direction === 'bullish' ? '看多' : record.direction === 'bearish' ? '看空' : '中性'
              const directionColor = record.direction === 'bullish' ? 'var(--long)' : record.direction === 'bearish' ? 'var(--short)' : 'var(--neutral)'
              return (
                <div key={record.task_id} style={{ display: 'grid', gridTemplateColumns: '78px 64px 1fr auto', gap: 8, alignItems: 'center', color: 'var(--text-2)', fontSize: 11.5 }}>
                  <span className="mono" style={{ color: directionColor }}>{directionLabel} · {Math.round((record.confidence || 0) * 100)}%</span>
                  <span className="mono" style={{ color: record.status === 'resolved' ? 'var(--long)' : record.status === 'tracking' ? 'var(--accent)' : 'var(--text-3)' }}>{statusLabel}</span>
                  <span className="mono" style={{ color: 'var(--text-3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{record.task_id}</span>
                  <span className="mono" style={{ color: record.pnl_pct != null && record.pnl_pct >= 0 ? 'var(--long)' : record.pnl_pct != null ? 'var(--short)' : 'var(--text-3)' }}>{pnl}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {drift?.alerts && drift.alerts.length > 0 && (
        <div style={{ marginTop: 12, color: 'var(--short)', fontSize: 12 }}>
          {drift.alerts.map(alert => <div key={alert.agent}>{alert.message}</div>)}
        </div>
      )}
    </div>
  )
}

export default CalibrationMonitor
