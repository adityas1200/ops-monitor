import React, { useCallback, useEffect, useState } from 'react'
import { api } from './api/client'
import Dashboard from './components/Dashboard'
import Workbench from './components/Workbench'
import Settings from './components/Settings'
import ChatWindow from './components/ChatWindow'

export default function App() {
  const [tab, setTab] = useState('dashboard')
  const [platforms, setPlatforms] = useState({ snowflake: false, aws: false })
  const [activePipeline, setActivePipeline] = useState(null)
  const [chatNotices, setChatNotices] = useState([])
  const [dashboardContext, setDashboardContext] = useState(null)
  const [theme, setTheme] = useState(() => localStorage.getItem('ops-monitor-theme') || 'light')

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('ops-monitor-theme', theme)
  }, [theme])

  const toggleTheme = () => setTheme((t) => (t === 'light' ? 'dark' : 'light'))

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
    } catch (e) {
      setChatNotices((prev) => [...prev, {
        id: `activity-fb:${Date.now()}`,
        agent: 'chat',
        text: `Something went wrong during ${payload.action}: ${payload.error || e.message}`,
      }])
    }
  }, [tab, platforms])

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

  const onChatContextChange = useCallback((ctx) => setDashboardContext(ctx), [])

  return (
    <div className="app">
      <div className="topbar">
        <div className="logo">ops<span>·</span>monitor</div>
        <div className="tabs">
          {['dashboard', 'workbench', 'settings'].map((t) => (
            <button key={t} className={`tab ${tab === t ? 'active' : ''}`} onClick={() => setTab(t)}>
              {t[0].toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>
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

      <div className="body">
        <div className="main">
          {tab === 'dashboard' && (
            <Dashboard
              platforms={platforms}
              onRunRCA={openInWorkbench}
              onSelect={setActivePipeline}
              onReportError={reportActivityError}
              onChatContextChange={onChatContextChange}
            />
          )}
          {tab === 'workbench' && (
            <Workbench
              activePipeline={activePipeline}
              onSelect={setActivePipeline}
              onReportError={reportActivityError}
            />
          )}
          {tab === 'settings' && (
            <Settings
              onSaved={(p) => setPlatforms(p)}
              onReportError={reportActivityError}
            />
          )}
        </div>
        <div className="chat-pane">
          <ChatWindow
            activePipeline={activePipeline}
            notices={chatNotices}
            activityContext={chatContext}
          />
        </div>
      </div>
    </div>
  )
}
