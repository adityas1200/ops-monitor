import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api/client'
import Dashboard from './components/Dashboard'
import Workbench from './components/Workbench'
import KnowledgeBase from './components/KnowledgeBase'
import Settings from './components/Settings'
import ChatWindow from './components/ChatWindow'
import ChatFab from './components/ChatFab'

function slimRcaForChat(rca) {
  if (!rca || rca.error) return null
  return {
    pipeline_id: rca.pipeline_id,
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
  const [chatOpen, setChatOpen] = useState(false)
  const [chatNotices, setChatNotices] = useState([])
  const [dashboardContext, setDashboardContext] = useState(null)
  const [dashDates, setDashDates] = useState({ dateFrom: null, dateTo: null })
  const [theme, setTheme] = useState(() => localStorage.getItem('ops-monitor-theme') || 'light')
  const [rcaReport, setRcaReport] = useState(null)
  const [chatRca, setChatRca] = useState(null)
  const reportedActivityIdsRef = useRef(new Set())

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('ops-monitor-theme', theme)
  }, [theme])

  const toggleTheme = () => setTheme((t) => (t === 'light' ? 'dark' : 'light'))
  const openChat = useCallback(() => setChatOpen(true), [])
  const closeChat = useCallback(() => setChatOpen(false), [])

  const reportActivityError = useCallback(async (payload) => {
    const action = payload.action || 'unknown action'
    const error = payload.error || 'Unknown error'
    const noticeTab = payload.tab || tab
    const id = `activity:${noticeTab}:${action}:${error}`
    if (reportedActivityIdsRef.current.has(id)) {
      setChatOpen(true)
      return
    }
    reportedActivityIdsRef.current.add(id)

    const ctx = { tab, platforms, ...payload.details }
    try {
      const r = await api.reportActivityError({
        tab: noticeTab,
        action,
        error,
        details: ctx,
      })
      setChatNotices((prev) => {
        if (prev.some((n) => n.id === id)) return prev
        return [...prev, { id, agent: 'chat', text: r.reply }]
      })
      setChatOpen(true)
    } catch (e) {
      reportedActivityIdsRef.current.delete(id)
      setChatNotices((prev) => [...prev, {
        id: `activity-fb:${Date.now()}`,
        agent: 'chat',
        text: `Something went wrong during ${action}: ${error || e.message}`,
      }])
      setChatOpen(true)
    }
  }, [tab, platforms])

  useEffect(() => {
    // One shared warmup for the whole SPA (deduped in api.client).
    api.snowflakeWarmup().catch(() => {})
  }, [])

  useEffect(() => {
    api.health().then((h) => setPlatforms(h.platforms || {})).catch(() => {})
  }, [tab])

  const openInWorkbench = (pipeline) => {
    setActivePipeline(pipeline)
    setTab('workbench')
  }

  const tabLabel = tab.charAt(0).toUpperCase() + tab.slice(1)
  const configured = Object.entries(platforms).filter(([, v]) => v).map(([k]) => k)

  const applyChatRca = useCallback((payload) => {
    if (!payload || payload.error) return
    if (!payload.summary || !(payload.root_cause_name || payload.analysis_type)) return
    setRcaReport(payload)
    setChatRca({
      pipelineId: payload.pipeline_id,
      rca: payload,
      seq: Date.now(),
    })
  }, [])

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

      <div className="body">
        <div className="main">
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

        <div className={`chat-pane${chatOpen ? ' open' : ''}`} aria-hidden={!chatOpen}>
          <ChatWindow
            activePipeline={activePipeline}
            notices={chatNotices}
            activityContext={chatContext}
            onToggleCollapsed={closeChat}
            onRcaFromChat={applyChatRca}
          />
        </div>

        {/* FAB on non-dashboard tabs; Dashboard renders its own for the requested placement */}
        {tab !== 'dashboard' && !chatOpen && <ChatFab onOpen={openChat} />}
      </div>
    </div>
  )
}
