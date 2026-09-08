/*
 * SignalRadar —— 多维度信号雷达图 (暗色)。
 */

import React, { useMemo } from 'react'
import ReactECharts from 'echarts-for-react'
import { SignalAgent } from '../api/client'

const DIM_ORDER = ['technical', 'onchain', 'derivatives', 'sentiment', 'macro']
const DIM_LABELS: Record<string, string> = {
  technical: '技术面',
  onchain: '链上',
  derivatives: '衍生品',
  sentiment: '舆情',
  macro: '宏观',
}

interface Props {
  signals: SignalAgent[]
}

const SignalRadar: React.FC<Props> = ({ signals }) => {
  const derivativesSignal = signals?.find(signal => signal.agent === 'derivatives')
  const onchainSignal = signals?.find(signal => signal.agent === 'onchain')
  const macroSignal = signals?.find(signal => signal.agent === 'macro')
  const option = useMemo(() => {
    if (!signals || signals.length === 0) return null

    const map: Record<string, SignalAgent> = {}
    signals.forEach(s => { if (DIM_ORDER.includes(s.agent)) map[s.agent] = s })

    const indicators = DIM_ORDER.map(dim => ({ name: DIM_LABELS[dim], max: 100 }))
    const scoreData = DIM_ORDER.map(dim => (map[dim] ? map[dim].score : 50))
    const confData = DIM_ORDER.map(dim => (map[dim] ? Math.round((map[dim].confidence || 0.5) * 100) : 50))

    return {
      backgroundColor: 'transparent',
      textStyle: { color: '#9aa0b0', fontFamily: 'Geist Mono, JetBrains Mono, monospace' },
      tooltip: {
        trigger: 'item',
        backgroundColor: 'rgba(20,24,36,0.96)',
        borderColor: 'rgba(255,255,255,0.13)',
        textStyle: { color: '#e8eaf0' },
        formatter: (params: any) => {
          const dim = DIM_ORDER[params.dataIndex]
          const s = map[dim]
          if (!s) return `${params.name}: ${params.value}`
          return `${params.name}<br/>强度分: ${s.score}/100<br/>可信度: ${((s.confidence || 0.5) * 100).toFixed(0)}%<br/>偏向: ${s.bias}`
        },
      },
      legend: {
        data: ['信号强度', '可信度'],
        bottom: 0,
        textStyle: { color: '#9aa0b0' },
        icon: 'roundRect',
      },
      radar: {
        indicator: indicators,
        center: ['50%', '48%'],
        radius: '62%',
        axisName: { color: '#9aa0b0', fontSize: 12 },
        splitLine: { lineStyle: { color: 'rgba(255,255,255,0.08)' } },
        splitArea: { areaStyle: { color: ['rgba(255,255,255,0.015)', 'rgba(255,255,255,0.03)'] } },
        axisLine: { lineStyle: { color: 'rgba(255,255,255,0.08)' } },
      },
      series: [
        {
          type: 'radar',
          name: '信号强度',
          data: [{ value: scoreData, name: '信号强度' }],
          symbol: 'circle',
          symbolSize: 5,
          lineStyle: { width: 2, color: '#f5b524' },
          areaStyle: { color: 'rgba(245,181,36,0.16)' },
          itemStyle: { color: '#f5b524' },
        },
        {
          type: 'radar',
          name: '可信度',
          data: [{ value: confData, name: '可信度' }],
          symbol: 'diamond',
          symbolSize: 5,
          lineStyle: { width: 1.5, color: '#2fd08a', type: 'dashed' },
          areaStyle: { color: 'rgba(47,208,138,0.06)' },
          itemStyle: { color: '#2fd08a' },
        },
      ],
    }
  }, [signals])

  if (!option) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: '24px 8px' }}>
        <div className="skeleton" style={{ height: 220, borderRadius: 999, maxWidth: 220, margin: '0 auto', width: '100%' }} />
        <div style={{ textAlign: 'center', color: 'var(--text-3)', fontSize: 12.5 }}>
          等待 Agent 产出信号...
        </div>
      </div>
    )
  }

  const options = derivativesSignal?.options_snapshot
  const onchain = onchainSignal?.raw_metrics
  const macro = macroSignal?.raw_metrics
  const governmentConfirmation = macroSignal?.government_event_confirmation
  const statusLabels: Record<string, string> = {
    confirmed_sale: '链上入金确认',
    probable_sale: '多源潜在出售',
    auction_announced: '官方拍卖公告',
    unconfirmed_transfer: '单源转移线索',
    unconfirmed_event: '未确认事件',
  }
  return (
    <div>
      <ReactECharts option={option} style={{ height: 340 }} notMerge />
      {onchainSignal && onchain && (
        <div style={{ marginTop: 2, marginBottom: 8, padding: '10px 12px', border: '1px solid var(--line)', borderRadius: 'var(--r-ctrl)', background: 'var(--surface-2)' }}>
          <div className="eyebrow" style={{ marginBottom: 7 }}>链上 24H 证据摘要</div>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', color: 'var(--text-2)', fontSize: 11.5 }}>
            <span className="mono">覆盖 {onchainSignal.data_quality || 'unknown'}</span>
            <span className="mono">净流 {onchain.netflow_eth == null ? '不可用' : `${onchain.netflow_eth} ETH`}</span>
            <span className="mono">流入 {onchain.inflow_eth == null ? '-' : `${onchain.inflow_eth} ETH`}</span>
            <span className="mono">流出 {onchain.outflow_eth == null ? '-' : `${onchain.outflow_eth} ETH`}</span>
            <span className="mono">鲸鱼笔数 {String(onchain.whale_tx_count ?? onchain.whale_activity ?? 0)}</span>
            <span className="mono">最大单笔 {String(onchain.largest_whale_transfer ?? '-')} </span>
            <span className="mono">地址覆盖 {String(onchain.exchange_wallet_count ?? 0)}</span>
          </div>
          <div style={{ color: 'var(--text-3)', fontSize: 11, lineHeight: 1.5, marginTop: 7 }}>
            {String(onchain.netflow_signal || 'BTC 当前为大额输出活跃度近似，未将其冒充为交易所净流。')}
          </div>
        </div>
      )}
      {macroSignal && macro && (
        <div style={{ marginTop: 2, marginBottom: 8, padding: '10px 12px', border: '1px solid var(--line)', borderRadius: 'var(--r-ctrl)', background: 'var(--surface-2)' }}>
          <div className="eyebrow" style={{ marginBottom: 7 }}>宏观新闻覆盖</div>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', color: 'var(--text-2)', fontSize: 11.5 }}>
            <span className="mono">覆盖 {macroSignal.data_quality || 'unknown'}</span>
            <span className="mono">已验证事件 {String(macro.event_count ?? 0)}</span>
            <span className="mono">政府资产事件 {String(macro.government_event_count ?? 0)}</span>
            <span className="mono">最新 {macro.freshest_age_hours == null ? '-' : `${String(macro.freshest_age_hours)}h`}</span>
            <span className="mono">范围 {String(macro.news_focus_profile || 'crypto_market_and_us_policy')}</span>
            {macro.supplemental_search_count != null && <span className="mono">核心补搜 {String(macro.supplemental_search_count)}</span>}
            {macro.filtered_china_result_count != null && <span className="mono">过滤中国新闻 {String(macro.filtered_china_result_count)}</span>}
          </div>
          {Array.isArray(macro.missing_mandatory_news_queries) && macro.missing_mandatory_news_queries.length > 0 && (
            <div style={{ color: 'var(--short)', fontSize: 11, lineHeight: 1.5, marginTop: 7 }}>
              未执行必查：{macro.missing_mandatory_news_queries.map(String).join('、')}
            </div>
          )}
        </div>
      )}
      {governmentConfirmation && Array.isArray(governmentConfirmation.events) && governmentConfirmation.events.length > 0 && (
        <div style={{ marginTop: 2, marginBottom: 8, padding: '10px 12px', border: '1px solid var(--line)', borderRadius: 'var(--r-ctrl)', background: 'var(--surface-2)' }}>
          <div className="eyebrow" style={{ marginBottom: 7 }}>政府资产事件确认</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
            {governmentConfirmation.events.slice(0, 3).map(event => (
              <div key={event.event_id} style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap', fontSize: 11.5 }}>
                <span className="mono" style={{ color: event.chain_confirmed ? 'var(--short)' : event.multi_source_confirmed ? 'var(--accent)' : 'var(--text-3)' }}>{statusLabels[event.status] || event.label}</span>
                <span style={{ color: 'var(--text-2)', minWidth: 0, flex: 1 }}>{event.title || '政府资产事件'}</span>
                <span className="mono" style={{ color: 'var(--text-3)', fontSize: 10 }}>{event.independent_source_count ?? 0} 源</span>
              </div>
            ))}
          </div>
        </div>
      )}
      {options && (
        <div style={{ marginTop: 2, padding: '10px 12px', border: '1px solid var(--line)', borderRadius: 'var(--r-ctrl)', background: 'var(--surface-2)' }}>
          <div className="eyebrow" style={{ marginBottom: 7 }}>期权交割辅助信号 · Deribit</div>
          {options.data_quality === 'real' ? (
            <>
              <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', color: 'var(--text-2)', fontSize: 11.5 }}>
                <span className="mono">距交割 {options.hours_to_expiry?.toFixed(1) ?? '-'}h</span>
                <span className="mono">Call/Put OI {options.call_put_oi_ratio?.toFixed(2) ?? '-'}</span>
                <span className="mono">最大痛点 ${options.max_pain?.toLocaleString() ?? '-'}</span>
                <span className="mono">Call墙 ${options.call_wall?.toLocaleString() ?? '-'}</span>
                <span className="mono">Put墙 ${options.put_wall?.toLocaleString() ?? '-'}</span>
              </div>
              <div style={{ color: 'var(--text-3)', fontSize: 11, lineHeight: 1.5, marginTop: 7 }}>
                {options.interpretation || '期权定位仅作辅助证据，买卖方和做市商 gamma 未知。'}
              </div>
            </>
          ) : (
            <div style={{ color: 'var(--text-3)', fontSize: 11.5, lineHeight: 1.5 }}>
              <div>期权数据暂不可用：{options.error || 'Deribit 未返回有效期权链'}</div>
              {options.network_hint && <div style={{ color: 'var(--accent)', marginTop: 5 }}>{options.network_hint}</div>}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default SignalRadar
