/*
 * ErrorBoundary —— 兜底捕获未预料的渲染异常。
 *
 * 背景：此前分析流程中若 LLM 返回的字段类型与前端 TS 类型声明不符
 * （例如 black_swan_risks 期望是 string[]，但 LLM 偶尔会返回对象数组
 * [{type, description}, ...]），直接渲染会触发 React 报错
 * "Objects are not valid as a React child"。由于全局没有 ErrorBoundary，
 * 该异常会导致 React 直接卸载整棵组件树，露出接近纯黑的 <body> 背景，
 * 表现为"分析过程中界面突然变黑屏"。
 *
 * 本组件作为最后一道防线：捕获渲染异常后展示可操作的错误提示（而非黑屏），
 * 并提供"重新加载"按钮恢复应用，避免用户误以为程序彻底崩溃/失去所有进度。
 */

import React from 'react'

interface Props {
  children: React.ReactNode
}

interface State {
  hasError: boolean
  message: string
}

class ErrorBoundary extends React.Component<Props, State> {
  state: State = { hasError: false, message: '' }

  static getDerivedStateFromError(error: unknown): State {
    return {
      hasError: true,
      message: error instanceof Error ? error.message : String(error),
    }
  }

  componentDidCatch(error: unknown, info: React.ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error('[CryptoSage] 渲染异常被 ErrorBoundary 捕获:', error, info.componentStack)
  }

  handleReload = () => {
    this.setState({ hasError: false, message: '' })
    window.location.reload()
  }

  render() {
    if (this.state.hasError) {
      return (
        <div
          style={{
            minHeight: '100dvh',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 16,
            padding: 24,
            textAlign: 'center',
          }}
        >
          <div style={{ fontSize: 40 }}>⚠️</div>
          <div style={{ fontSize: 18, fontWeight: 700, color: 'var(--text)' }}>
            界面渲染出现异常
          </div>
          <div style={{ fontSize: 13, color: 'var(--text-2)', maxWidth: 480, lineHeight: 1.6 }}>
            通常是本次分析结果中的某个字段格式异常导致（如 LLM 返回的风险清单为对象而非文本）。
            后台分析可能仍在正常进行，点击下方按钮重新加载页面即可恢复。
          </div>
          <div className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)', maxWidth: 480, wordBreak: 'break-all' }}>
            {this.state.message}
          </div>
          <button
            onClick={this.handleReload}
            style={{
              marginTop: 8,
              border: '1px solid var(--accent-line)',
              borderRadius: 'var(--r-ctrl)',
              background: 'var(--accent-soft)',
              color: 'var(--accent)',
              padding: '8px 20px',
              fontSize: 13,
              fontWeight: 600,
              cursor: 'pointer',
              fontFamily: 'var(--font-mono)',
            }}
          >
            重新加载
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

export default ErrorBoundary
