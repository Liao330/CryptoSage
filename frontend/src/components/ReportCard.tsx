/*
 * ReportCard —— 综合研判定论 (暗色)。
 */

import React, { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { SynthesisResult } from '../api/client'

interface Props {
  report: SynthesisResult
  criticRounds: number
}

const DIR: Record<string, { color: string; soft: string; label: string; arrow: string }> = {
  bullish: { color: 'var(--long)', soft: 'var(--long-soft)', label: '看多', arrow: '▲' },
  bearish: { color: 'var(--short)', soft: 'var(--short-soft)', label: '看空', arrow: '▼' },
  neutral: { color: 'var(--neutral)', soft: 'rgba(124,134,160,0.13)', label: '中性 / 震荡', arrow: '◆' },
}

/**
 * 防御性归一化：LLM 输出偶尔与声明的 string[] 类型不符（如返回
 * [{type, description}, ...] 对象数组而非纯字符串数组），直接把对象
 * 当 React 子节点渲染会抛 "Objects are not valid as a React child" 异常，
 * 在没有 ErrorBoundary 兜底时会导致整个应用白屏/黑屏。这里统一转成可读字符串。
 */
function toDisplayText(item: unknown): string {
  if (typeof item === 'string') return item
  if (item == null) return ''
  if (typeof item === 'object') {
    const obj = item as Record<string, unknown>
    const type = obj.type || obj.name || obj.risk || obj.point
    const desc = obj.description || obj.detail || obj.reason
    if (type && desc) return `${type}：${desc}`
    if (type) return String(type)
    if (desc) return String(desc)
    try {
      return JSON.stringify(item)
    } catch {
      return String(item)
    }
  }
  return String(item)
}

/** 短标签版本：对象优先取 type/name 作为 pill 展示文案，避免长 description 撑破 UI。 */
function toShortLabel(item: unknown, maxLen = 24): string {
  if (typeof item === 'object' && item != null) {
    const obj = item as Record<string, unknown>
    const type = obj.type || obj.name || obj.risk || obj.point
    if (type) return String(type)
  }
  const text = toDisplayText(item)
  return text.length > maxLen ? `${text.slice(0, maxLen)}…` : text
}

const Section: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div style={{ marginTop: 22 }}>
    <div className="eyebrow" style={{ marginBottom: 10 }}>{label}</div>
    {children}
  </div>
)

const AGENT_LABELS: Record<string, string> = {
  technical: '技术面',
  onchain: '链上',
  derivatives: '衍生品',
  sentiment: '舆情',
  macro: '宏观',
}

const LevelRow: React.FC<{ level?: number; confluence?: number; reason?: string; color: string }> = ({ level, confluence, reason, color }) => (
  <div style={{
    display: 'flex', alignItems: 'center', gap: 12, padding: '9px 12px',
    background: 'var(--surface-2)', borderRadius: 'var(--r-ctrl)', marginBottom: 6,
    borderLeft: `2px solid ${color}`,
  }}>
    <span className="mono" style={{ color, fontSize: 15, fontWeight: 700, minWidth: 96 }}>
      ${level?.toLocaleString() || '-'}
    </span>
    {confluence && confluence >= 2 ? (
      <span className="mono" style={{
        fontSize: 10.5, color: 'var(--accent)', background: 'var(--accent-soft)',
        padding: '2px 8px', borderRadius: 'var(--r-pill)',
      }}>{confluence} 重汇聚</span>
    ) : (
      <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>{confluence || 0}x</span>
    )}
    <span style={{ color: 'var(--text-2)', fontSize: 12.5, flex: 1 }}>{reason}</span>
  </div>
)

const ReportCard: React.FC<Props> = ({ report, criticRounds }) => {
  const d = DIR[report.direction] || DIR.neutral
  const pct = Math.round((report.confidence || 0) * 100)
  const forecastMode = report.forecast_mode || (['bullish', 'bearish'].includes(report.direction) ? 'directional' : 'neutral')
  const executionMode = report.execution_mode || (forecastMode === 'directional' && pct >= 60 ? 'execute' : 'observe')
  const supports = report.key_levels?.supports || []
  const resistances = report.key_levels?.resistances || []

  // 置信度语义分级
  const confidence_level = pct < 35 ? '极低' : pct < 50 ? '低' : pct < 75 ? '中' : '高'
  const confidence_advice = pct < 60 ? ' — 未达到执行阈值，建议观望' : ''
  const calibration = report.confidence_calibration
  const horizons = report.timeframe_outlook || {}
  const horizonKeys = ['4H', '24H', '7D'].filter(key => horizons[key])
  const [activeHorizon, setActiveHorizon] = useState('24H')

  useEffect(() => {
    if (!horizons[activeHorizon] && horizonKeys.length > 0) {
      setActiveHorizon(horizonKeys[0])
    }
  }, [activeHorizon, horizonKeys.join('|')]) // eslint-disable-line react-hooks/exhaustive-deps

  const activeOutlook = horizons[activeHorizon]
  const performanceAgents = Object.entries(report.agent_performance_calibration?.agents || {})
    .filter(([, value]) => value.sample_count > 0)
  const dynamicMultipliers = calibration?.dynamic_weight_multipliers || {}
  const graph = report.news_event_graph
  const graphNodes = graph?.nodes?.slice(0, 8) || []
  const governmentConfirmation = report.government_event_confirmation
  const governmentStatusLabel: Record<string, string> = {
    confirmed_sale: '链上入金确认，仍需区分托管转移与成交',
    probable_sale: '多源确认的交易所转移/潜在出售',
    auction_announced: '官方拍卖公告，尚未证明成交',
    unconfirmed_transfer: '单源交易所转移线索，未确认出售',
    unconfirmed_event: '政府资产事件线索，未确认出售',
  }
  const governmentStatusColor: Record<string, string> = {
    confirmed_sale: 'var(--short)',
    probable_sale: 'var(--accent)',
    auction_announced: 'var(--accent)',
    unconfirmed_transfer: 'var(--text-3)',
    unconfirmed_event: 'var(--text-3)',
  }
  const graphNodePositions = graphNodes.reduce<Record<string, { x: number; y: number }>>((acc, event, index) => {
    const columns = graphNodes.length > 4 ? 4 : Math.max(1, graphNodes.length)
    const row = Math.floor(index / columns)
    const column = index % columns
    acc[event.event_id] = {
      x: 82 + column * (columns === 1 ? 0 : 150),
      y: 56 + row * 104,
    }
    return acc
  }, {})

  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
      {/* 定论头 */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 20, flexWrap: 'wrap',
        padding: '20px 22px', background: d.soft, borderRadius: 'var(--r-card)',
        border: `1px solid ${d.color}`,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ color: d.color, fontSize: 30, lineHeight: 1 }}>{d.arrow}</span>
          <div>
            <div className="eyebrow">{report.primary_horizon || '24H'} 方向倾向</div>
            <div style={{ color: d.color, fontSize: 26, fontWeight: 800, letterSpacing: '-0.02em' }}>{pct === 0 ? '数据不足' : d.label}</div>
          </div>
        </div>

        {/* 置信度条 */}
        <div style={{ flex: 1, minWidth: 200 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
            <span className="eyebrow">方向把握度</span>
            <span className="mono" style={{ color: d.color, fontSize: 18, fontWeight: 700 }}>{pct}% <span style={{ fontSize: 11, fontWeight: 400, color: 'var(--text-3)' }}>{confidence_level}{confidence_advice}</span></span>
          </div>
          <div style={{ height: 6, background: 'var(--surface-3)', borderRadius: 999, overflow: 'hidden' }}>
            <motion.div
              initial={{ width: 0 }}
              animate={{ width: `${pct}%` }}
              transition={{ duration: 0.8, ease: 'easeOut' }}
              style={{ height: '100%', background: pct >= 50 ? d.color : 'var(--neutral)', borderRadius: 999 }}
            />
          </div>
          {calibration && (
            <div className="mono" style={{ marginTop: 8, color: 'var(--text-3)', fontSize: 10.5, display: 'flex', gap: 12, flexWrap: 'wrap' }}>
              {report.direction_score != null && <span>净信号 {report.direction_score.toFixed(1)}/100</span>}
              {calibration.signal_agreement != null && <span>一致度 {Math.round(calibration.signal_agreement * 100)}%</span>}
              {calibration.directional_agent_count != null && <span>方向 Agent {calibration.aligned_agent_count ?? 0}/{calibration.directional_agent_count}</span>}
              {calibration.directional_evidence_weak && <span style={{ color: 'var(--accent)' }}>方向预测保留，未形成执行共识</span>}
              {calibration.limited_data && <span style={{ color: 'var(--short)' }}>数据维度不足，已封顶</span>}
              {calibration.data_coverage != null && <span>数据覆盖 {Math.round(calibration.data_coverage * 100)}%</span>}
              {(calibration.critic_penalty || 0) < 0 && <span>审查折扣 {Math.round((calibration.critic_penalty || 0) * 100)}%</span>}
            </div>
          )}
        </div>

        {criticRounds > 0 && (
          <span className="mono" style={{
            fontSize: 11, color: 'var(--short)', border: '1px solid var(--short)',
            padding: '5px 11px', borderRadius: 'var(--r-pill)',
          }}>
            {criticRounds} 轮对抗审查
          </span>
        )}
        {report.market_state && (
          <span className="mono" style={{
            fontSize: 11, color: 'var(--text-2)', border: '1px solid var(--line-strong)',
            padding: '5px 11px', borderRadius: 'var(--r-pill)',
          }}>
            市场状态 {report.market_state}
          </span>
        )}
      </div>

      {report.execution_metrics && (
        <div className="mono" style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginTop: 9, color: 'var(--text-3)', fontSize: 10.5 }}>
          <span style={{ color: executionMode === 'execute' ? 'var(--long)' : 'var(--accent)' }}>
            {forecastMode === 'directional' ? '方向预测可评估' : '中性预测'} · {executionMode === 'execute' ? '执行模式' : '观望模式'}
          </span>
          <span>服务端耗时 {((report.execution_metrics.duration_ms || 0) / 1000).toFixed(1)}s</span>
          <span>后端节点 {report.execution_metrics.backend_trace_steps ?? 0}</span>
          <span>工具调用 {report.execution_metrics.tool_call_count ?? 0}</span>
          {report.execution_metrics.news_fetch_lag_ms != null && <span style={{ color: report.execution_metrics.news_freshness_status === 'stale_at_completion' ? 'var(--short)' : 'var(--text-3)' }}>新闻滞后 {(report.execution_metrics.news_fetch_lag_ms / 1000).toFixed(0)}s</span>}
          <span>{report.execution_metrics.observe_shadow ? '观望影子追踪已创建' : '执行影子追踪待确认'}</span>
        </div>
      )}

      {/* 多周期展望 */}
      {activeOutlook && (
        <Section label="多周期展望">
          <div role="tablist" aria-label="分析周期" style={{ display: 'flex', borderBottom: '1px solid var(--line)', gap: 18 }}>
            {horizonKeys.map(horizon => {
              const outlook = horizons[horizon]
              const selected = activeHorizon === horizon
              const color = (DIR[outlook.direction] || DIR.neutral).color
              return (
                <button
                  key={horizon}
                  role="tab"
                  aria-selected={selected}
                  onClick={() => setActiveHorizon(horizon)}
                  style={{
                    border: 0, borderBottom: `2px solid ${selected ? color : 'transparent'}`,
                    background: 'transparent', color: selected ? color : 'var(--text-3)',
                    padding: '7px 2px', cursor: 'pointer', fontFamily: 'var(--font-mono)',
                    fontSize: 11.5, letterSpacing: 0,
                  }}
                >
                  {horizon} · {(DIR[outlook.direction] || DIR.neutral).label}
                </button>
              )
            })}
          </div>
          <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', paddingTop: 12, color: 'var(--text-2)', fontSize: 12.5 }}>
            <span className="mono">净信号 {(activeOutlook.direction_score ?? 50).toFixed(1)}/100</span>
            <span className="mono">把握度 {Math.round((activeOutlook.confidence ?? 0) * 100)}%</span>
            <span>主导维度 {(activeOutlook.dominant_agents || []).map(agent => AGENT_LABELS[agent] || agent).join(' / ') || '暂无'}</span>
          </div>
          <div style={{ marginTop: 12, padding: '12px 14px', background: 'var(--surface-2)', borderLeft: '2px solid var(--accent)', borderRadius: 'var(--r-ctrl)' }}>
            <div style={{ color: 'var(--text)', fontSize: 13, lineHeight: 1.55 }}>{activeOutlook.thesis || `该周期重点观察${activeOutlook.focus || '多维度信号'}。`}</div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 8 }}>
              {(activeOutlook.drivers || []).map(driver => (
                <span key={driver.agent} className="mono" style={{ fontSize: 10.5, color: driver.bias === 'bullish' ? 'var(--long)' : driver.bias === 'bearish' ? 'var(--short)' : 'var(--text-3)', border: '1px solid var(--line-strong)', padding: '3px 7px', borderRadius: 'var(--r-pill)' }}>
                  {driver.label || AGENT_LABELS[driver.agent] || driver.agent} {driver.contribution_points >= 0 ? '+' : ''}{driver.contribution_points.toFixed(1)}
                </span>
              ))}
            </div>
            {activeOutlook.risk && <div style={{ color: 'var(--text-3)', fontSize: 11.5, marginTop: 8 }}>失效风险：{activeOutlook.risk}</div>}
          </div>
          {activeOutlook.weight_profile && (
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 10 }}>
              {Object.entries(activeOutlook.weight_profile).map(([agent, weight]) => (
                <span key={agent} className="mono" style={{ fontSize: 10, color: 'var(--text-3)' }}>
                  {AGENT_LABELS[agent] || agent} {Math.round(weight * 100)}%
                </span>
              ))}
            </div>
          )}
        </Section>
      )}

      {/* 关键发现 */}
      {report.key_findings && report.key_findings.length > 0 && (
        <Section label="关键发现">
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {report.key_findings.map((f, i) => (
              <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'baseline' }}>
                <span className="mono" style={{ color: 'var(--accent)', fontSize: 11 }}>{String(i + 1).padStart(2, '0')}</span>
                <span style={{ color: 'var(--text)', fontSize: 13.5, lineHeight: 1.55 }}>{toDisplayText(f)}</span>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* 新闻事件图 */}
      {graph && (
        <Section label={`新闻事件知识图谱 · ${graph.event_count} 事件 / ${graph.article_count} 文章`}>
          {graphNodes.length > 0 ? (
            <>
              <div style={{ overflowX: 'auto', border: '1px solid var(--line)', borderRadius: 'var(--r-ctrl)', background: 'var(--surface-2)' }}>
                <svg viewBox={`0 0 ${graphNodes.length > 4 ? 600 : Math.max(180, graphNodes.length * 150)} ${graphNodes.length > 4 ? 220 : 120}`} role="img" aria-label="新闻事件关系图" style={{ display: 'block', width: '100%', minWidth: graphNodes.length > 4 ? 600 : 180, height: 'auto' }}>
                  {graph.edges.map(edge => {
                    const from = graphNodePositions[edge.source]
                    const to = graphNodePositions[edge.target]
                    if (!from || !to) return null
                    return <line key={`${edge.source}-${edge.target}`} x1={from.x} y1={from.y} x2={to.x} y2={to.y} stroke="var(--accent-line)" strokeWidth="1.5" strokeDasharray="4 4" />
                  })}
                  {graphNodes.map(event => {
                    const pos = graphNodePositions[event.event_id]
                    const color = event.cross_source_confirmed ? 'var(--long)' : 'var(--accent)'
                    return (
                      <g key={event.event_id}>
                        <circle cx={pos.x} cy={pos.y} r="20" fill="var(--surface-3)" stroke={color} strokeWidth="2" />
                        <text x={pos.x} y={pos.y + 4} textAnchor="middle" fill={color} style={{ fontFamily: 'var(--font-mono)', fontSize: 10 }}>{event.impact_score.toFixed(2)}</text>
                        <text x={pos.x} y={pos.y + 38} textAnchor="middle" fill="var(--text)" style={{ fontFamily: 'var(--font-sans)', fontSize: 10 }}>{event.title.length > 17 ? `${event.title.slice(0, 17)}…` : event.title}</text>
                        <text x={pos.x} y={pos.y + 52} textAnchor="middle" fill="var(--text-3)" style={{ fontFamily: 'var(--font-mono)', fontSize: 9 }}>{event.independent_source_count} 源 · {event.age_hours.toFixed(1)}h</text>
                      </g>
                    )
                  })}
                </svg>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', marginTop: 8 }}>
                {graphNodes.slice(0, 4).map((event, index) => (
                  <div key={event.event_id} style={{ display: 'grid', gridTemplateColumns: '34px minmax(0, 1fr) auto', gap: 10, alignItems: 'start', padding: '9px 0', borderBottom: index < Math.min(3, graphNodes.length - 1) ? '1px solid var(--line)' : 0 }}>
                    <span className="mono" style={{ color: 'var(--accent)', fontSize: 10 }}>{event.impact_score.toFixed(2)}</span>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ color: 'var(--text)', fontSize: 12.5, lineHeight: 1.45 }}>{event.title}</div>
                      <div className="mono" style={{ color: 'var(--text-3)', fontSize: 10, marginTop: 3 }}>{event.published_at || '发布时间未知'} · {event.article_count} 篇聚类</div>
                    </div>
                    <span className="mono" style={{ color: event.cross_source_confirmed ? 'var(--long)' : 'var(--text-3)', fontSize: 10 }}>
                      {event.independent_source_count} 源{event.cross_source_confirmed ? '确认' : ''}
                    </span>
                  </div>
                ))}
              </div>
              <div className="mono" style={{ marginTop: 8, color: 'var(--text-3)', fontSize: 10 }}>节点颜色：绿色 = 多源确认 · 琥珀 = 单源事件；虚线 = 共享主题关系</div>
            </>
          ) : (
            <div style={{ padding: '14px 0', color: 'var(--text-3)', fontSize: 12.5 }}>当前 24 小时窗口没有通过发布时间校验的新闻事件，图谱保持为空，不使用过期或未验证新闻。</div>
          )}
        </Section>
      )}

      {/* 政府资产事件证据链 */}
      {governmentConfirmation && Array.isArray(governmentConfirmation.events) && governmentConfirmation.events.length > 0 && (
        <Section label="政府资产事件确认链">
          <div style={{ color: 'var(--text-2)', fontSize: 12, lineHeight: 1.5, marginBottom: 9 }}>
            仅在“政府钱包→交易所链上入金”、官方拍卖公告或第二独立新闻源出现时提升状态；钱包转移本身不等于已出售。
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
            <span className="mono" style={{ fontSize: 10.5, color: governmentConfirmation.confirmed_sale_count > 0 ? 'var(--short)' : 'var(--text-3)' }}>
              链上确认 {governmentConfirmation.confirmed_sale_count}
            </span>
            <span className="mono" style={{ fontSize: 10.5, color: governmentConfirmation.probable_sale_count > 0 ? 'var(--accent)' : 'var(--text-3)' }}>
              多源确认 {governmentConfirmation.probable_sale_count}
            </span>
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
              链上入金笔数 {governmentConfirmation.chain_deposit_count ?? 0}
            </span>
            {(governmentConfirmation.chain_deposit_value_eth ?? 0) > 0 && (
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
                ETH 入金 {governmentConfirmation.chain_deposit_value_eth} ETH
              </span>
            )}
            {(governmentConfirmation.chain_deposit_value_btc ?? 0) > 0 && (
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)' }}>
                BTC 入金 {governmentConfirmation.chain_deposit_value_btc} BTC
              </span>
            )}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {governmentConfirmation.events.map((event) => {
              const color = governmentStatusColor[event.status] || 'var(--text-3)'
              const title = event.title || '政府资产事件'
              return (
                <div key={event.event_id} style={{ padding: '10px 12px', background: 'var(--surface-2)', borderLeft: `2px solid ${color}`, borderRadius: 'var(--r-ctrl)' }}>
                  <div style={{ display: 'flex', gap: 10, alignItems: 'baseline', flexWrap: 'wrap' }}>
                    <span style={{ color: 'var(--text)', fontSize: 12.5, lineHeight: 1.45, flex: 1, minWidth: 180 }}>{title}</span>
                    <span className="mono" style={{ color, fontSize: 10.5 }}>{governmentStatusLabel[event.status] || event.label}</span>
                  </div>
                  <div className="mono" style={{ color: 'var(--text-3)', fontSize: 10, marginTop: 5 }}>
                    {event.published_at || '发布时间未知'} · {event.independent_source_count ?? 0} 独立来源
                    {event.sources && event.sources.length > 0 ? ` · ${event.sources.join('、')}` : ''}
                  </div>
                  {event.confirmed_by.length > 0 && (
                    <div style={{ color: 'var(--long)', fontSize: 11, lineHeight: 1.45, marginTop: 5 }}>已具备：{event.confirmed_by.join('、')}</div>
                  )}
                  {event.missing_confirmations.length > 0 && (
                    <div style={{ color: 'var(--text-3)', fontSize: 11, lineHeight: 1.45, marginTop: 4 }}>待补证据：{event.missing_confirmations.join('、')}</div>
                  )}
                </div>
              )
            })}
          </div>
        </Section>
      )}

      {/* 反事实解释 */}
      {report.counterfactuals && report.counterfactuals.length > 0 && (
        <Section label="方向翻转条件">
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            {report.counterfactuals.map((scenario, index) => {
              const resultDir = DIR[scenario.result_direction] || DIR.neutral
              return (
                <div key={`${scenario.agent}-${index}`} style={{ display: 'grid', gridTemplateColumns: '92px minmax(0, 1fr) auto', gap: 12, padding: '10px 0', borderBottom: index < report.counterfactuals!.length - 1 ? '1px solid var(--line)' : 0 }}>
                  <span className="mono" style={{ color: 'var(--accent)', fontSize: 10.5 }}>{scenario.agent}</span>
                  <div>
                    <div style={{ color: 'var(--text)', fontSize: 12.5 }}>{scenario.condition}</div>
                    <div className="mono" style={{ color: 'var(--text-3)', fontSize: 10, marginTop: 3 }}>{scenario.change}</div>
                  </div>
                  <span className="mono" style={{ color: resultDir.color, fontSize: 11, textAlign: 'right' }}>
                    {resultDir.label}<br />{scenario.result_score.toFixed(1)}
                  </span>
                </div>
              )
            })}
          </div>
        </Section>
      )}

      {/* 历史绩效校准 */}
      {Object.keys(dynamicMultipliers).length > 0 && (
        <Section label="本次融合的动态 Agent 权重">
          <div style={{ color: 'var(--text-2)', fontSize: 12, lineHeight: 1.5, marginBottom: 9 }}>
            权重乘数来自已结算影子交易的准确率与 Brier 校准；全部为 1.00 时表示本次没有足够历史样本改变默认权重。
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {Object.entries(dynamicMultipliers).map(([agent, multiplier]) => (
              <span key={agent} className="mono" style={{ fontSize: 10.5, color: multiplier >= 1 ? 'var(--long)' : 'var(--short)', border: '1px solid var(--line-strong)', padding: '4px 8px', borderRadius: 'var(--r-pill)' }}>
                {AGENT_LABELS[agent] || agent} ×{multiplier.toFixed(2)}
              </span>
            ))}
          </div>
          <div className="mono" style={{ color: 'var(--text-3)', fontSize: 10, marginTop: 8 }}>
            方法：{calibration?.method || 'deterministic_evidence_v2'} · 已结算样本：{report.agent_performance_calibration?.total_samples || 0}
          </div>
        </Section>
      )}

      {/* 历史绩效校准 */}
      {performanceAgents.length > 0 && (
        <Section label={`历史绩效校准 · ${report.agent_performance_calibration?.total_samples || 0} 样本`}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {performanceAgents.map(([agent, value]) => (
              <span key={agent} className="mono" title={`准确率 ${value.accuracy == null ? '未知' : Math.round(value.accuracy * 100) + '%'} · Brier ${value.brier_score ?? '-'}`} style={{ fontSize: 10.5, color: value.weight_multiplier >= 1 ? 'var(--long)' : 'var(--short)', border: '1px solid var(--line-strong)', padding: '4px 8px', borderRadius: 'var(--r-pill)' }}>
                {agent} ×{value.weight_multiplier.toFixed(2)} · n={value.sample_count}
              </span>
            ))}
          </div>
        </Section>
      )}

      {/* 关键价位 */}
      {(supports.length > 0 || resistances.length > 0) && (
        <Section label="关键价位">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 16 }}>
            {resistances.length > 0 && (
              <div>
                <div style={{ color: 'var(--short)', fontSize: 12, marginBottom: 8, fontWeight: 600 }}>阻力位</div>
                {resistances.map((r, i) => <LevelRow key={i} {...r} color="var(--short)" />)}
              </div>
            )}
            {supports.length > 0 && (
              <div>
                <div style={{ color: 'var(--long)', fontSize: 12, marginBottom: 8, fontWeight: 600 }}>支撑位</div>
                {supports.map((s, i) => <LevelRow key={i} {...s} color="var(--long)" />)}
              </div>
            )}
          </div>
        </Section>
      )}

      {/* 风险评估 */}
      {(report.risk_assessment || report.risk_boundary || (report.black_swan_risks && report.black_swan_risks.length > 0)) && (
        <Section label="风险评估">
          <div style={{
            padding: '14px 16px', background: 'var(--short-soft)',
            border: '1px solid rgba(240,85,107,0.3)', borderRadius: 'var(--r-ctrl)',
          }}>
            {report.risk_assessment && <p style={{ margin: '0 0 8px', color: 'var(--text)', fontSize: 13, lineHeight: 1.6 }}>{report.risk_assessment}</p>}
            {report.risk_boundary && (
              <p style={{ margin: 0, color: 'var(--short)', fontSize: 13, fontWeight: 600 }}>
                风险边界：{report.risk_boundary}
              </p>
            )}
            {report.black_swan_risks && report.black_swan_risks.length > 0 && (
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 10 }}>
                {report.black_swan_risks.map((r, i) => (
                  <span key={i} className="mono" style={{
                    fontSize: 11, color: 'var(--short)', border: '1px solid rgba(240,85,107,0.4)',
                    padding: '3px 9px', borderRadius: 'var(--r-pill)',
                  }} title={toDisplayText(r)}>{toShortLabel(r)}</span>
                ))}
              </div>
            )}
          </div>
        </Section>
      )}

      {/* 仓位建议 */}
      {report.position_advice && (
        <Section label="仓位建议">
          <div style={{
            padding: '14px 16px', background: 'var(--surface-2)',
            border: '1px solid var(--line)', borderRadius: 'var(--r-ctrl)',
          }}>
            <span className="mono" style={{
              fontSize: 12.5, color: 'var(--accent)', background: 'var(--accent-soft)',
              padding: '4px 12px', borderRadius: 'var(--r-pill)', fontWeight: 700,
            }}>{report.position_advice.action}</span>
            <p style={{ margin: '10px 0 0', color: 'var(--text-2)', fontSize: 13, lineHeight: 1.6 }}>{report.position_advice.reason}</p>
            {report.position_advice.stop_loss && (
              <p style={{ margin: '8px 0 0', color: 'var(--short)', fontSize: 13 }}>
                <span className="mono">止损位 ${report.position_advice.stop_loss.toLocaleString()}</span>
              </p>
            )}
          </div>
        </Section>
      )}

      {/* 详细分析 */}
      {report.analysis && (
        <Section label="详细分析">
          <div className="markdown-content">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                a: ({ href, children }) => (
                  <a href={href} target="_blank" rel="noopener noreferrer">
                    {children}
                  </a>
                ),
              }}
            >
              {report.analysis}
            </ReactMarkdown>
          </div>
        </Section>
      )}

      {report.disclaimer && (
        <p style={{ marginTop: 20, color: 'var(--text-3)', fontSize: 11.5, lineHeight: 1.5 }}>{report.disclaimer}</p>
      )}
    </motion.div>
  )
}

export default ReportCard
