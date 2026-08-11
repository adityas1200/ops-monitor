import React, { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { api, chatStream } from '../api/client'

function buildWelcome(context) {
  const dash = context.dashboard || {}
  const tab = (context.tab || '').toLowerCase()
  const view = dash.view === 'dq' ? 'Data Quality' : 'Tasks'
  const failedT = dash.failedTasks?.length ?? 0
  const failedD = dash.failedDqChecks?.length ?? 0
  const range = dash.dateFrom && dash.dateTo ? `${dash.dateFrom} → ${dash.dateTo}` : 'your date range'

  let intro = `Hi! I'm synced with the Dashboard (${view} view, ${range}).`
  if (tab === 'dashboard') {
    if (dash.view === 'dq') {
      intro += failedD
        ? ` I see ${failedD} failed DQ check(s) — ask "failed DQ checks" for details or click a row for resolution help.`
        : ' No failed DQ checks in this range. Ask for a "DQ summary" or change the date filter.'
    } else {
      intro += failedT
        ? ` I see ${failedT} failed/delayed task(s) — ask "failed tasks" for details, or select a row for RCA.`
        : ' No failed tasks in this range. Ask for a "task summary" or adjust filters.'
    }
  } else if (tab === 'workbench') {
    intro += ' Use quick actions below for RCA, fixes, and validation on the selected pipeline.'
  } else if (tab === 'settings') {
    intro += ' I can help with Snowflake/AWS connectivity and monitoring configuration.'
  } else {
    intro += ' Ask about failed tasks, DQ checks, RCA, or resolution plans.'
  }
  return intro
}

function getQuickActions(context, activePipeline) {
  const tab = (context.tab || '').toLowerCase()
  const dash = context.dashboard || {}
  const isDq = dash.view === 'dq'
  const hasSelection = Boolean(activePipeline?.id)
  const isDqRow = hasSelection && String(activePipeline.id).startsWith('dq_')

  if (tab === 'workbench' || (hasSelection && !isDqRow)) {
    return [
      { label: 'Run RCA', text: 'Run RCA on the selected pipeline' },
      { label: 'Resolution plan', text: 'Give me a resolution plan with RCA and suggested fix' },
      { label: 'Suggest fix', text: 'Suggest a fix for the selected failure' },
      { label: 'Validate fix', text: 'Validate the fix on zero-copy clone' },
    ]
  }

  if (tab === 'dashboard' && isDq) {
    const actions = [
      { label: 'Failed DQ checks', text: 'Show failed data quality checks with details' },
      { label: 'DQ summary', text: 'DQ status summary for the current date range' },
    ]
    if (isDqRow) {
      actions.push(
        { label: 'Explain failure', text: 'Explain this DQ check failure in detail' },
        { label: 'Resolution plan', text: 'How should I resolve this DQ failure?' },
      )
    } else {
      actions.push({ label: 'DQ resolution', text: 'How should I resolve DQ failures?' })
    }
    return actions
  }

  if (tab === 'dashboard') {
    const actions = [
      { label: 'Failed tasks', text: 'List failed and delayed tasks with error details' },
      { label: 'Task summary', text: 'Task monitoring KPI summary' },
    ]
    if (hasSelection && !isDqRow) {
      actions.push(
        { label: 'Run RCA', text: 'Run RCA on the selected task' },
        { label: 'Resolution plan', text: 'Give me a resolution plan for the selected task' },
      )
    }
    return actions
  }

  if (tab === 'settings') {
    return [
      { label: 'Help', text: 'What can you help me with?' },
      { label: 'Failed tasks', text: 'List failed tasks from the dashboard' },
      { label: 'Failed DQ checks', text: 'List failed DQ checks from the dashboard' },
    ]
  }

  return [
    { label: 'Failed tasks', text: 'List failed and delayed tasks with error details' },
    { label: 'Failed DQ checks', text: 'Show failed data quality checks with details' },
    { label: 'Help', text: 'What can you help me with on this dashboard?' },
  ]
}

function agentLabel(agent) {
  const labels = {
    chat: 'assistant',
    rca: 'RCA',
    fix: 'fix',
    test: 'validation',
    monitoring: 'monitoring',
    memory: 'memory',
    error: 'error',
    connectivity: 'connectivity',
  }
  return labels[agent] || agent
}

export default function ChatWindow({
  activePipeline,
  notices = [],
  activityContext = {},
  collapsed: _collapsed = false,
  onToggleCollapsed,
}) {
  const welcome = useMemo(() => buildWelcome(activityContext), [
    activityContext.tab,
    activityContext.dashboard?.view,
    activityContext.dashboard?.dateFrom,
    activityContext.dashboard?.dateTo,
    activityContext.dashboard?.failedTasks?.length,
    activityContext.dashboard?.failedDqChecks?.length,
  ])

  const [msgs, setMsgs] = useState([{ role: 'bot', agent: 'chat', text: welcome }])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [streamingMsg, setStreamingMsg] = useState(null)
  const endRef = useRef(null)
  const seenNotices = useRef(new Set())
  const lastPipelineId = useRef(null)
  const welcomeKey = useRef('')
  const [sessionId] = useState(() => {
    const stored = sessionStorage.getItem('opsmon_session')
    if (stored) return stored
    const id = crypto.randomUUID()
    sessionStorage.setItem('opsmon_session', id)
    return id
  })

  const quickActions = useMemo(
    () => getQuickActions(activityContext, activePipeline),
    [activityContext, activePipeline],
  )

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [msgs, streamingMsg])

  useEffect(() => {
    const key = `${activityContext.tab}:${activityContext.dashboard?.view}`
    if (welcomeKey.current && welcomeKey.current !== key) {
      setMsgs((m) => [...m, { role: 'bot', agent: 'chat', text: buildWelcome(activityContext) }])
    }
    welcomeKey.current = key
  }, [activityContext.tab, activityContext.dashboard?.view])

  useEffect(() => {
    notices.forEach((n) => {
      if (!n?.id || seenNotices.current.has(n.id)) return
      seenNotices.current.add(n.id)
      setMsgs((m) => [...m, { role: 'bot', agent: n.agent || 'connectivity', text: n.text }])
    })
  }, [notices])

  useEffect(() => {
    if (!activePipeline?.id || lastPipelineId.current === activePipeline.id) return
    lastPipelineId.current = activePipeline.id
    const isDq = String(activePipeline.id).startsWith('dq_')
    const hint = isDq
      ? 'Ask: explain failure · resolution plan'
      : 'Ask: Run RCA · resolution plan · suggest fix'
    setMsgs((m) => [...m, {
      role: 'bot',
      agent: 'chat',
      text: `Context: "${activePipeline.name}" (${activePipeline.status}).\n${activePipeline.error ? `Error: ${String(activePipeline.error).slice(0, 180)}` : ''}\n${hint}`,
    }])
  }, [activePipeline?.id])

  const sendText = (raw) => {
    const text = (raw ?? '').trim()
    if (!text) return
    setMsgs((m) => [...m, { role: 'user', text }])
    setInput('')
    setBusy(true)
    setStreamingMsg({ role: 'bot', agent: 'chat', text: '' })

    chatStream(text, activePipeline?.id, activityContext, sessionId, {
      onMeta: (meta) => {
        setStreamingMsg((prev) => ({ ...prev, agent: meta.agent }))
      },
      onChunk: (chunk) => {
        setStreamingMsg((prev) => ({ ...prev, text: prev.text + chunk }))
      },
      onDone: (payload) => {
        setStreamingMsg((prev) => {
          if (prev) {
            let finalText = prev.text
            if (payload && payload.summary && payload.summary.passed != null) {
              const s = payload.summary
              finalText += `\n\nTest cases: ${s.passed}/${s.total} passed.`
            }
            setMsgs((m) => [...m, { role: 'bot', agent: prev.agent, text: finalText }])
          }
          return null
        })
        setBusy(false)
      },
      onError: (err) => {
        setStreamingMsg(null)
        // Fallback to synchronous API
        api.chat(text, activePipeline?.id, { ...activityContext, session_id: sessionId })
          .then((r) => {
            setMsgs((m) => [...m, { role: 'bot', agent: r.agent, text: r.reply }])
          })
          .catch((e) => {
            setMsgs((m) => [...m, { role: 'bot', agent: 'error', text: 'Error: ' + e.message }])
            api.reportActivityError({
              tab: activityContext.tab || 'Chat',
              action: 'send message',
              error: e.message,
              details: activityContext,
            }).then((r) => setMsgs((m) => [...m, { role: 'bot', agent: 'chat', text: r.reply }]))
              .catch(() => {})
          })
          .finally(() => setBusy(false))
      },
    })
  }

  const send = () => sendText(input)
  const quick = (t) => sendText(t)

  const contextHint = useMemo(() => {
    const dash = activityContext.dashboard
    if (!dash) return null
    const parts = []
    if (dash.view === 'dq' && dash.failedDqChecks?.length) {
      parts.push(`${dash.failedDqChecks.length} failed DQ`)
    }
    if (dash.view !== 'dq' && dash.failedTasks?.length) {
      parts.push(`${dash.failedTasks.length} failed/delayed tasks`)
    }
    if (dash.dateFrom && dash.dateTo) parts.push(`${dash.dateFrom} → ${dash.dateTo}`)
    return parts.length ? parts.join(' · ') : null
  }, [activityContext.dashboard])

  return (
    <div className="chat-window">
      <div className="chat-head">
        <div className="chat-head-row">
          <div className="chat-title">Agent Chat</div>
          <button
            type="button"
            className="chat-collapse-btn"
            onClick={onToggleCollapsed}
            title="Close Agent Chat"
            aria-label="Close Agent Chat"
          >
            ×
          </button>
        </div>
        {contextHint && <div className="chat-context-hint">{contextHint}</div>}
        {activePipeline && (
          <div className="chat-context-hint">
            Selected: {activePipeline.name}
          </div>
        )}
      </div>
      <div className="chat-msgs">
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            {m.role === 'bot' && <div className="agent-tag">{agentLabel(m.agent)}</div>}
            {m.role === 'bot'
              ? <div className="msg-markdown"><ReactMarkdown>{m.text}</ReactMarkdown></div>
              : m.text}
          </div>
        ))}
        {streamingMsg && (
          <div className="msg bot">
            <div className="agent-tag">{agentLabel(streamingMsg.agent)}</div>
            <div className="msg-markdown">
              <ReactMarkdown>{streamingMsg.text || '…'}</ReactMarkdown>
            </div>
          </div>
        )}
        {busy && !streamingMsg && <div className="msg bot"><span className="spinner">thinking…</span></div>}
        <div ref={endRef} />
      </div>
      <div className="chat-quick-actions">
        {quickActions.map((q) => (
          <button
            key={q.label}
            type="button"
            className="btn sec chat-chip"
            disabled={busy}
            onClick={() => quick(q.text)}
          >
            {q.label}
          </button>
        ))}
      </div>
      <div className="chat-input">
        <input
          type="text"
          placeholder="Ask about failed tasks, DQ checks, RCA, fixes…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
        />
        <button type="button" className="btn" disabled={busy} onClick={send}>Send</button>
      </div>
    </div>
  )
}
