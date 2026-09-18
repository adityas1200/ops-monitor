import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api/client'
import Dashboard from './components/Dashboard'
import Workbench from './components/Workbench'
import KnowledgeBase from './components/KnowledgeBase'
import Settings from './components/Settings'
import ChatWindow from './components/ChatWindow'
import ChatFab from './components/ChatFab'

const DEFAULT_CHAT_WIDTH = 440
const MIN_CHAT_WIDTH = 320
const MAX_CHAT_WIDTH = 720

function clampChatWidth(width) {
  const viewportMax = typeof window === 'undefined'
    ? MAX_CHAT_WIDTH
    : Math.max(MIN_CHAT_WIDTH, window.innerWidth - 620)
  return Math.min(Math.max(width, MIN_CHAT_WIDTH), Math.min(MAX_CHAT_WIDTH, viewportMax))
}

function slimRcaForChat(rca) {
  if (!rca || rca.error) return null
  return {
    incident_id: rca.incident_id,
    pipeline_id: rca.pipeline_id,
    qc_id: rca.qc_id || rca.check_id || rca.related_dq_failures?.[0]?.id,
    analysis_type: rca.analysis_type,
    category: rca.category,
    summary: rca.summary,
    detailed_analysis: (rca.detailed_analysis || '').slice(0, 800),
    root_cause_node: rca.root_cause_node,
    root_cause_name: rca.root_cause_name,
    confidence: rca.confidence,
    affected_tables: (rca.affected_tables || []).slice(0, 8),
  }
}

export default function App() {
  const [tab, setTab] = useState('dashboard')
  const [platforms, setPlatforms] = useState({ snowflake: false, aws: false })
  const [activePipeline, setActivePipeline] = useState(null)
  const [chatOpen, setChatOpen] = useState(true)
  const [dashboardContext, setDashboardContext] = useState(null)
  const [dashDates, setDashDates] = useState({ dateFrom: null, dateTo: null })
  const [theme, setTheme] = useState(() => localStorage.getItem('ops-monitor-theme') || 'light')
  const [rcaReport, setRcaReport] = useState(null)
  const [chatRca, setChatRca] = useState(null)
  const [chatRcaBusy, setChatRcaBusy] = useState(false)
  const [chatWidth, setChatWidth] = useState(() => {
    const saved = Number(localStorage.getItem('ops-monitor-chat-width'))
    return clampChatWidth(Number.isFinite(saved) && saved > 0 ? saved : DEFAULT_CHAT_WIDTH)
  })
  const [chatResizing, setChatResizing] = useState(false)
  const chatResizeRef = useRef({ active: false, pointerId: null, startX: 0, startWidth: 0 })

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('ops-monitor-theme', theme)
  }, [theme])

  useEffect(() => {
    localStorage.setItem('ops-monitor-chat-width', String(chatWidth))
  }, [chatWidth])

  useEffect(() => {
    const fitChatToViewport = () => setChatWidth((width) => clampChatWidth(width))
    window.addEventListener('resize', fitChatToViewport)
    return () => window.removeEventListener('resize', fitChatToViewport)
  }, [])

  const toggleTheme = () => setTheme((t) => (t === 'light' ? 'dark' : 'light'))
  const openChat = useCallback(() => setChatOpen(true), [])
  const closeChat = useCallback(() => {
    // A close can happen while the resize handle owns a pointer. Always leave
    // resize mode so its `transition: none` rule cannot freeze the collapse.
    chatResizeRef.current.active = false
    chatResizeRef.current.pointerId = null
    setChatResizing(false)
    setChatOpen(false)
  }, [])

  const reportActivityError = useCallback(async (_payload) => {
    // Keep errors on the originating page; do not inject proactive messages into AMQ Assist.
  }, [])

  useEffect(() => {
    // One shared warmup for the whole SPA (deduped in api.client).
    api.snowflakeWarmup().catch(() => {})
  }, [])

  useEffect(() => {
    api.health().then((h) => setPlatforms(h.platforms || {})).catch(() => {})
  }, [tab])

  useEffect(() => {
    if (tab === 'dashboard') setChatOpen(true)
  }, [tab])

  const openInWorkbench = (pipeline) => {
    // A click is an explicit RCA execution, even when it targets the same row.
    // The run id lets Workbench distinguish a re-run from a state re-render.
    setActivePipeline({ ...pipeline, rcaRunId: Date.now() })
    setRcaReport(null)
    setTab('workbench')
  }

  const tabLabel = tab.charAt(0).toUpperCase() + tab.slice(1)
  const configured = Object.entries(platforms).filter(([, v]) => v).map(([k]) => k)

  const onChatRcaPending = useCallback((pending) => {
    setChatRcaBusy(Boolean(pending))
    if (pending) setTab('workbench')
  }, [])

  const applyChatRca = useCallback((payload) => {
    setChatRcaBusy(false)
    if (!payload || payload.error) return
    if (!payload.summary || !(payload.root_cause_name || payload.analysis_type)) return
    setRcaReport(payload)
    setChatRca({
      pipelineId: payload.pipeline_id || activePipeline?.id,
      rca: payload,
      seq: Date.now(),
    })
    setTab('workbench')
  }, [activePipeline?.id])

  const chatContext = {
    tab: tabLabel,
    platforms,
    dashboard: dashboardContext,
    activePipeline: activePipeline ? {
      id: activePipeline.id,
      name: activePipeline.name,
      status: activePipeline.status,
      platform: activePipeline.platform,
      error: activePipeline.error,
    } : null,
    rca: slimRcaForChat(rcaReport),
  }

  const onChatContextChange = useCallback((ctx) => {
    setDashboardContext(ctx)
    if (ctx?.dateFrom || ctx?.dateTo) {
      setDashDates({ dateFrom: ctx.dateFrom || null, dateTo: ctx.dateTo || null })
    }
  }, [])

  const startChatResize = (e) => {
    if (e.button != null && e.button !== 0) return
    chatResizeRef.current = {
      active: true,
      pointerId: e.pointerId,
      startX: e.clientX,
      startWidth: chatWidth,
    }
    setChatResizing(true)
    e.currentTarget.setPointerCapture?.(e.pointerId)
    e.preventDefault()
  }

  const resizeChat = (e) => {
    const resize = chatResizeRef.current
    if (!resize.active || resize.pointerId !== e.pointerId) return
    setChatWidth(clampChatWidth(resize.startWidth + resize.startX - e.clientX))
  }

  const stopChatResize = (e) => {
    const resize = chatResizeRef.current
    if (!resize.active || (e.pointerId != null && resize.pointerId !== e.pointerId)) return
    chatResizeRef.current.active = false
    chatResizeRef.current.pointerId = null
    setChatResizing(false)
    e.currentTarget.releasePointerCapture?.(e.pointerId)
  }

  const resizeChatWithKeyboard = (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
    e.preventDefault()
    const delta = e.key === 'ArrowLeft' ? 16 : -16
    setChatWidth((width) => clampChatWidth(width + delta))
  }

  return (
    <div className="app">
      <div className="sidebar">
        <div className="sidebar-brand">
          <div className="logo">Agentic Ops<br/>Monitoring <span>&amp; QC</span></div>
        </div>
        <nav className="sidebar-nav">
          {['dashboard', 'workbench', 'knowledge', 'settings'].map((t) => (
            <button key={t} className={`nav-item ${tab === t ? 'active' : ''}`} onClick={() => setTab(t)}>
              <span className="nav-icon">
                {t === 'dashboard' ? '◉' : t === 'workbench' ? '⚙' : t === 'knowledge' ? '📘' : '⚡'}
              </span>
              {t[0].toUpperCase() + t.slice(1)}
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className={`mode-pill ${configured.length ? 'live' : ''}`}>
            {configured.length ? `● ${configured.join(' + ')}` : '● configure in Settings'}
          </div>
          <button
            className="theme-toggle"
            onClick={toggleTheme}
            title={theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}
            aria-label={theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}
          >
            {theme === 'light' ? '☾' : '☀'}
          </button>
        </div>
      </div>

      <div className={`body${chatOpen ? ' chat-open' : ' chat-closed'}`}>
        <div className={`main${chatOpen ? '' : ' main-expanded'}`}>
          <div style={{ display: tab === 'dashboard' ? 'block' : 'none' }}>
            <Dashboard
              platforms={platforms}
              onRunRCA={openInWorkbench}
              onSelect={setActivePipeline}
              onReportError={reportActivityError}
              onChatContextChange={onChatContextChange}
              chatOpen={chatOpen}
              onOpenChat={openChat}
            />
          </div>
          <div style={{ display: tab === 'workbench' ? 'block' : 'none' }}>
            <Workbench
              activePipeline={activePipeline}
              onSelect={setActivePipeline}
              onReportError={reportActivityError}
              onBackToDashboard={() => setTab('dashboard')}
              onOpenKnowledge={() => setTab('knowledge')}
              dateFrom={dashDates.dateFrom || dashboardContext?.dateFrom}
              dateTo={dashDates.dateTo || dashboardContext?.dateTo}
              onRcaChange={setRcaReport}
              externalRca={chatRca}
              externalRcaBusy={chatRcaBusy}
            />
          </div>
          <div style={{ display: tab === 'knowledge' ? 'block' : 'none' }}>
            <KnowledgeBase
              active={tab === 'knowledge'}
              onReportError={reportActivityError}
            />
          </div>
          <div style={{ display: tab === 'settings' ? 'block' : 'none' }}>
            <Settings
              onSaved={(p) => setPlatforms(p)}
              onReportError={reportActivityError}
            />
          </div>
        </div>

        <div
          className={`chat-pane${chatOpen ? ' open' : ''}${chatResizing ? ' resizing' : ''}`}
          style={{ '--chat-pane-width': `${chatWidth}px` }}
          aria-hidden={!chatOpen}
        >
          {chatOpen && (
            <div
              className="chat-pane-resize-handle"
              role="separator"
              aria-label="Resize AMQ Assist panel"
              aria-orientation="vertical"
              aria-valuemin={MIN_CHAT_WIDTH}
              aria-valuemax={MAX_CHAT_WIDTH}
              aria-valuenow={Math.round(chatWidth)}
              tabIndex={0}
              onPointerDown={startChatResize}
              onPointerMove={resizeChat}
              onPointerUp={stopChatResize}
              onPointerCancel={stopChatResize}
              onKeyDown={resizeChatWithKeyboard}
              onDoubleClick={() => setChatWidth(clampChatWidth(DEFAULT_CHAT_WIDTH))}
              title="Drag to resize; double-click to reset"
            />
          )}
          <ChatWindow
            activePipeline={activePipeline}
            activityContext={chatContext}
            onToggleCollapsed={closeChat}
            onRcaFromChat={applyChatRca}
            onRcaPending={onChatRcaPending}
          />
        </div>

        {/* FAB on non-dashboard tabs; Dashboard renders its own for the requested placement */}
        {tab !== 'dashboard' && !chatOpen && <ChatFab onOpen={openChat} />}
      </div>
    </div>
  )
}
