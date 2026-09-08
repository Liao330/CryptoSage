/**
 * CryptoSage API Client —— REST + WebSocket。
 */

const API_BASE = '/api'

export interface AnalyzeRequest {
  symbol: string
  query: string
}

export interface AnalyzeResponse {
  task_id: string
  status: string
}

export interface KlineData {
  ts: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface SignalAgent {
  agent: string
  symbol: string
  bias: string
  score: number
  confidence: number
  evidence: string[]
  caveats: string[]
  data_source?: string
  data_quality?: string
  // 模型思考过程与工具编排轨迹（Function Calling 数据 Agent 产出）
  thinking?: string
  tools?: string[]
  raw_metrics?: Record<string, unknown>
  options_snapshot?: {
    data_quality?: string
    hours_to_expiry?: number
    delivery_risk?: string
    call_put_oi_ratio?: number | null
    max_pain?: number | null
    max_pain_distance_pct?: number | null
    call_wall?: number | null
    put_wall?: number | null
    positioning_mode?: string
    interpretation?: string
    bias?: string
    confidence?: number
    error?: string
    network_hint?: string
  }
  news_event_graph?: NewsEventGraph
  government_event_confirmation?: GovernmentEventConfirmationReport
}

export interface NewsEventNode {
  event_id: string
  title: string
  published_at: string
  age_hours: number
  topics: string[]
  article_count: number
  independent_source_count: number
  sources: string[]
  cross_source_confirmed: boolean
  verified_independent_source_count?: number
  source_quality?: number
  aggregator_sources?: string[]
  novelty_score: number
  freshness_decay: number
  impact_score: number
  url?: string
}

export type GovernmentEventStatus =
  | 'confirmed_sale'
  | 'probable_sale'
  | 'auction_announced'
  | 'unconfirmed_transfer'
  | 'unconfirmed_event'

export interface GovernmentEventConfirmation {
  event_id: string
  title?: string
  published_at?: string
  age_hours?: number
  sources?: string[]
  independent_source_count?: number
  status: GovernmentEventStatus | string
  label: string
  exchange_transfer_claim: boolean
  auction_claim: boolean
  official_auction: boolean
  multi_source_confirmed: boolean
  chain_confirmed: boolean
  confirmed_by: string[]
  missing_confirmations: string[]
  topics?: string[]
}

export interface GovernmentEventConfirmationReport {
  events: GovernmentEventConfirmation[]
  confirmed_sale_count: number
  probable_sale_count: number
  chain_deposit_count: number
  chain_deposit_value_eth?: number
  chain_deposit_value_btc?: number
  method: string
}

export interface NewsEventGraph {
  generated_at?: string
  article_count: number
  event_count: number
  confirmed_event_count: number
  nodes: NewsEventNode[]
  edges: { source: string; target: string; type: string; topics: string[] }[]
}

export interface SynthesisResult {
  direction: string
  confidence: number
  forecast_mode?: 'directional' | 'neutral' | string
  execution_mode?: 'execute' | 'observe' | string
  direction_score?: number
  primary_horizon?: string
  execution_metrics?: {
    duration_ms?: number
    backend_trace_steps?: number
    backend_event_count?: number
    tool_call_count?: number
    observe_shadow?: boolean
    shadow_track_status?: string
    latest_news_fetched_at?: string
    news_fetch_lag_ms?: number
    news_freshness_status?: string
  }
  news_focus_profile?: string
  model_direction?: string
  model_confidence?: number
  confidence_calibration?: {
    method?: string
    data_coverage?: number
    signal_agreement?: number
    average_source_confidence?: number
    net_signal?: number
    neutral_band?: number
    critic_penalty?: number
    mass_agreement?: number
    directional_agent_count?: number
    aligned_agent_count?: number
    bullish_agent_count?: number
    bearish_agent_count?: number
    direction_blocked?: boolean
    directional_evidence_weak?: boolean
    direction_block_reason?: string
    direction_confidence_cap?: number | null
    real_dimension_count?: number
    usable_dimension_count?: number
    limited_data?: boolean
    dynamic_weight_multipliers?: Record<string, number>
  }
  timeframe_outlook?: Record<string, {
    label?: string
    focus?: string
    risk?: string
    thesis?: string
    direction: string
    confidence: number
    track_type?: 'execute' | 'observe' | string
    direction_score: number
    data_coverage: number
    dominant_agents: string[]
    drivers?: {
      agent: string
      label: string
      bias: string
      contribution_points: number
    }[]
    weight_profile?: Record<string, number>
  }>
  counterfactuals?: {
    agent: string
    condition: string
    change: string
    result_direction: string
    result_score: number
    score_change: number
    crosses_direction_boundary: boolean
  }[]
  agent_contributions?: {
    agent: string
    bias: string
    score: number
    source_confidence: number
    effective_weight: number
    contribution_points: number
  }[]
  agent_performance_calibration?: {
    method?: string
    total_samples?: number
    agents?: Record<string, {
      sample_count: number
      accuracy: number | null
      brier_score: number | null
      weight_multiplier: number
    }>
  }
  news_event_graph?: NewsEventGraph
  government_event_confirmation?: GovernmentEventConfirmationReport
  weighted_scores?: Record<string, number>
  key_findings: string[]
  market_state: string
  key_levels: {
    supports?: { level: number; confluence: number; reason?: string }[]
    resistances?: { level: number; confluence: number; reason?: string }[]
  }
  risk_assessment?: string
  risk_boundary?: string
  black_swan_risks?: string[]
  position_advice?: {
    action: string
    reason: string
    stop_loss?: number
  }
  analysis?: string
  disclaimer?: string
  // Synthesis 慢思考回传的融合推理思维链（仅过程展示，不入报告主体）
  thinking?: string
}

export type WSMessageType = 'agent_start' | 'signal' | 'synthesis' | 'critic' | 'final' | 'echo' | 'ping'

export interface WSMessage {
  type: WSMessageType
  data: any
  task_id: string
  seq: number
}

export async function startAnalysis(req: AnalyzeRequest): Promise<AnalyzeResponse> {
  const resp = await fetch(`${API_BASE}/analyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
  if (!resp.ok) throw new Error(`API error: ${resp.status}`)
  return resp.json()
}

export async function cancelAnalysis(taskId: string): Promise<{ task_id: string; status: string }> {
  const resp = await fetch(`${API_BASE}/analyze/${encodeURIComponent(taskId)}`, {
    method: 'DELETE',
  })
  if (!resp.ok) throw new Error(`API error: ${resp.status}`)
  return resp.json()
}

export async function getKlines(
  symbol: string = 'BTC-USDT',
  bar: string = '4H',
  limit: number = 200
): Promise<{ symbol: string; bar: string; count: number; data: KlineData[] }> {
  const params = new URLSearchParams({ symbol, bar, limit: String(limit) })
  const resp = await fetch(`${API_BASE}/klines?${params}`)
  if (!resp.ok) throw new Error(`API error: ${resp.status}`)
  return resp.json()
}

export interface AgentPerformanceReport {
  symbol: string
  method: string
  total_samples: number
  agents: Record<string, {
    sample_count: number
    accuracy: number | null
    brier_score: number | null
    weight_multiplier: number
  }>
}

export interface DriftReport {
  symbol: string
  agents: Record<string, {
    status: 'insufficient_data' | 'stable' | 'improving' | 'degrading'
    accuracy_delta: number | null
    brier_delta: number | null
    recent?: { sample_count: number; accuracy: number | null; brier_score: number | null }
    baseline?: { sample_count: number; accuracy: number | null; brier_score: number | null }
  }>
  alerts: { agent: string; severity: string; message: string }[]
}

export interface ShadowStatsReport {
  symbol: string
  tracking_count: number
  observe_count?: number
  execute_count?: number
  metrics: {
    resolved_count: number
    win_count: number
    win_rate: number
    avg_pnl_pct: number
    cumulative_pnl_pct: number
    profit_factor: number | null
    max_drawdown_pct: number
    risk_adjusted_score: number
  }
  drift_alerts: { agent: string; severity: string; message: string }[]
  dynamic_weight_multipliers?: Record<string, number>
  calibration_method?: string
  settlement_horizon_hours?: number
  eligibility_policy?: string
  drift_method?: string
  records?: {
    task_id: string
    symbol: string
    direction: string
    confidence: number
    status: string
    pnl_pct?: number | null
    created_at?: string
    target_at?: string
    resolved_at?: string | null
  }[]
}

async function getJson<T>(path: string): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`)
  if (!resp.ok) throw new Error(`API error: ${resp.status}`)
  return resp.json()
}

export function getAgentPerformance(): Promise<AgentPerformanceReport> {
  return getJson<AgentPerformanceReport>('/calibration/agents')
}

export function getDriftReport(): Promise<DriftReport> {
  return getJson<DriftReport>('/monitor/drift')
}

export function getShadowStats(): Promise<ShadowStatsReport> {
  return getJson<ShadowStatsReport>('/shadow/stats')
}

export interface WSHandle {
  close: () => void
}

export function connectWebSocket(
  taskId: string,
  onMessage: (msg: WSMessage) => void,
  onError?: (e: Event) => void,
  onReconnecting?: () => void,
  onExpired?: () => void,
): WSHandle {
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const host = window.location.host
  const url = `${protocol}://${host}/ws/${taskId}`

  let currentWs: WebSocket | null = null
  let reconnectAttempts = 0
  let manualClose = false
  let receivedFinal = false // 收到 final 后视为任务完成，绝不再重连
  let lastSeq = 0
  const MAX_RECONNECT = 3

  const create = (): void => {
    const ws = new WebSocket(`${url}?after_seq=${lastSeq}`)
    currentWs = ws

    ws.onmessage = (event) => {
      try {
        const msg: WSMessage = JSON.parse(event.data)
        if (msg.type === 'ping') return // 服务端心跳，忽略
        if (typeof msg.seq !== 'number' || msg.seq <= lastSeq) return
        lastSeq = msg.seq
        reconnectAttempts = 0
        if (msg.type === 'final') receivedFinal = true
        onMessage(msg)
      } catch (e) {
        console.warn('WS 消息解析失败:', e)
      }
    }

    ws.onerror = (e) => {
      onError?.(e)
    }

    ws.onclose = (event) => {
      // 任务已正常完成，或用户主动关闭 —— 绝不重连（避免重放历史事件导致的重连风暴）
      if (manualClose || receivedFinal) return

      if (event.code === 4404) {
        onExpired?.()
        return
      }

      // 正常关闭码也不重连（1000=正常, 1005=无状态但多为正常结束）
      if (event.code === 1000 || event.code === 1005) return

      if (reconnectAttempts >= MAX_RECONNECT) {
        console.warn(`WS 重连已达最大次数(${MAX_RECONNECT})，停止重连`)
        onExpired?.()
        return
      }
      reconnectAttempts++
      const delay = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), 10000)
      onReconnecting?.()
      console.log(`WS 断线(code=${event.code})，${delay / 1000}s 后第 ${reconnectAttempts} 次重连...`)
      setTimeout(() => {
        if (!manualClose && !receivedFinal) create()
      }, delay)
    }
  }

  create()

  return {
    close: () => {
      manualClose = true
      currentWs?.close(1000)
    },
  }
}
