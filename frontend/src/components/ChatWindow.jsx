import React, { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { api, chatStream } from '../api/client'

const WELCOME_MSG =
  'Hi! I\'m AMQ Assist. Ask about failed tasks, DQ checks, RCA, resolution plans, or suggested fixes.'

function getQuickActions(context, activePipeline) {
  const tab = (context.tab || '').toLowerCase()
  const dash = context.dashboard || {}
  const isDq = dash.view === 'dq'
  const hasSelection = Boolean(activePipeline?.id)
  const isDqRow = hasSelection && String(activePipeline.id).startsWith('dq_')

  if (tab === 'workbench' || (hasSelection && !isDqRow)) {
    return [
      { label: 'Run RCA', text: 'Run RCA on the selected pipeline' },
      { label: 'Correct RCA', text: 'Correct RCA with my suggestion: ', fillOnly: true },
      { label: 'Resolution plan', text: 'Give me a resolution plan with RCA and suggested fix' },
      { label: 'Suggest fix', text: 'Suggest a fix for the selected failure' },
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
        { label: 'Correct RCA', text: 'Correct RCA with my suggestion: ', fillOnly: true },
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
    chat: 'AMQ Assist',
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

function isRcaPayload(payload) {
  return Boolean(
    payload
    && !payload.error
    && payload.summary
    && (payload.root_cause_name || payload.analysis_type),
  )
}

function isRcaRequestText(text) {
  return /\b(rca|root cause)\b/i.test(text)
    && /\b(run|rerun|re-run|correct|refine|again)\b/i.test(text)
}

export default function ChatWindow({
  activePipeline,
  activityContext = {},
  collapsed: _collapsed = false,
  onToggleCollapsed,
  onRcaFromChat,
  onRcaPending,
}) {
  const [msgs, setMsgs] = useState([{ role: 'bot', agent: 'chat', text: WELCOME_MSG }])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [streamingMsg, setStreamingMsg] = useState(null)
  const endRef = useRef(null)
  const inputRef = useRef(null)
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
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    const styles = window.getComputedStyle(el)
    const lineHeight = parseFloat(styles.lineHeight) || 20
    const padY = (parseFloat(styles.paddingTop) || 0) + (parseFloat(styles.paddingBottom) || 0)
    const maxHeight = lineHeight * 2 + padY
    const next = Math.min(el.scrollHeight, maxHeight)
    el.style.height = `${next}px`
    el.style.overflowY = el.scrollHeight > maxHeight ? 'auto' : 'hidden'
  }, [input])

  const sendText = (raw) => {
    const text = (raw ?? '').trim()
    if (!text) return
    const expectsRca = isRcaRequestText(text)
    let executesRca = expectsRca
    setMsgs((m) => [...m, { role: 'user', text }])
    setInput('')
    setBusy(true)
    setStreamingMsg({ role: 'bot', agent: 'chat', text: '' })
    // The server emits intent metadata only after it has performed the RCA.
    // Start the Workbench loader from the submitted text so it is visible
    // during the actual processing period.
    if (expectsRca) onRcaPending?.(true)

    const targetId = activePipeline?.id
      || activityContext?.rca?.pipeline_id
      || activityContext?.rca?.qc_id
    chatStream(text, targetId, activityContext, sessionId, {
      onMeta: (meta) => {
        setStreamingMsg((prev) => ({ ...prev, agent: meta.agent }))
        if (meta?.intent === 'run_rca') {
          executesRca = true
          onRcaPending?.(true)
        }
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
        if (isRcaPayload(payload) && executesRca) onRcaFromChat?.(payload)
        else onRcaPending?.(false)
        setBusy(false)
      },
      onError: (err) => {
        setStreamingMsg(null)
        // Fallback to synchronous API
        api.chat(text, targetId, { ...activityContext, session_id: sessionId })
          .then((r) => {
            setMsgs((m) => [...m, { role: 'bot', agent: r.agent, text: r.reply }])
            if (isRcaPayload(r.payload) && executesRca) onRcaFromChat?.(r.payload)
            else onRcaPending?.(false)
          })
          .catch((e) => {
            onRcaPending?.(false)
            setMsgs((m) => [...m, { role: 'bot', agent: 'error', text: 'Error: ' + e.message }])
          })
          .finally(() => setBusy(false))
      },
    })
  }

  const send = () => sendText(input)
  const quick = (t) => sendText(t)
  const fillInput = (t) => {
    setInput(t)
    requestAnimationFrame(() => {
      const el = inputRef.current
      if (!el) return
      el.focus()
      const len = t.length
      el.setSelectionRange(len, len)
    })
  }

  const onInputKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className="chat-window">
      <div className="chat-head">
        <div className="chat-head-row">
          <div className="chat-title">AMQ Assist</div>
          <button
            type="button"
            className="chat-collapse-btn"
            onClick={onToggleCollapsed}
            title="Close AMQ Assist"
            aria-label="Close AMQ Assist"
          >
            ×
          </button>
        </div>
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
            onClick={() => (q.fillOnly ? fillInput(q.text) : quick(q.text))}
          >
            {q.label}
          </button>
        ))}
      </div>
      <div className="chat-input">
        <textarea
          ref={inputRef}
          rows={1}
          placeholder="Ask about failed tasks, DQ checks, RCA, or correct a root cause…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onInputKeyDown}
          aria-label="Chat message"
        />
        <button type="button" className="btn" disabled={busy} onClick={send}>Send</button>
      </div>
    </div>
  )
}
