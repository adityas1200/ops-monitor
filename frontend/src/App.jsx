import React, { useCallback, useEffect, useState } from 'react'
import { api } from './api/client'
import Dashboard from './components/Dashboard'
import Workbench from './components/Workbench'
import KnowledgeBase from './components/KnowledgeBase'
import Settings from './components/Settings'
import ChatWindow from './components/ChatWindow'
import ChatFab from './components/ChatFab'

export default function App() {
  const [tab, setTab] = useState('dashboard')
  const [platforms, setPlatforms] = useState({ snowflake: false, aws: false })
  const [activePipeline, setActivePipeline] = useState(null)
  const [chatOpen, setChatOpen] = useState(false)
  const [chatNotices, setChatNotices] = useState([])
  const [dashboardContext, setDashboardContext] = useState(null)
  const [dashDates, setDashDates] = useState({ dateFrom: null, dateTo: null })
  const [theme, setTheme] = useState(() => localStorage.getItem('ops-monitor-theme') || 'light')

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('ops-monitor-theme', theme)
  }, [theme])

  const toggleTheme = () => setTheme((t) => (t === 'light' ? 'dark' : 'light'))
  const openChat = useCallback(() => setChatOpen(true), [])
  const closeChat = useCallback(() => setChatOpen(false), [])

  const reportActivityError = useCallback(async (payload) => {
    const ctx = { tab, platforms, ...payload.details }
    try {
      const r = await api.reportActivityError({
        tab: payload.tab || tab,
        action: payload.action || 'unknown action',
        error: payload.error || 'Unknown error',
        details: ctx,
      })
      const id = `activity:${payload.tab}:${payload.action}:${payload.error}`
      setChatNotices((prev) => [...prev, { id, agent: 'chat', text: r.reply }])
      setChatOpen(true)
    } catch (e) {
      setChatNotices((prev) => [...prev, {
        id: `activity-fb:${Date.now()}`,
        agent: 'chat',
        text: `Something went wrong during ${payload.action}: ${payload.error || e.message}`,
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
          />
        </div>

        {/* FAB on non-dashboard tabs; Dashboard renders its own for the requested placement */}
        {tab !== 'dashboard' && !chatOpen && <ChatFab onOpen={openChat} />}
      </div>
    </div>
  )
}
