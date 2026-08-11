const BASE = '/api'

async function req(path, opts = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      detail = body.detail ?? body.error ?? detail
      if (Array.isArray(detail)) detail = detail.map((d) => d.msg || d).join('; ')
    } catch { /* ignore */ }
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
    err.status = res.status
    throw err
  }
  return res.json()
}

const SF_SESSION_KEY = 'opsmon_sf_session'
// Frontend hint TTL — must be <= backend _SESSION_TTL_S (50m). Used only to skip
// blocking waits on remount; backend still owns the real Snowflake session.
const SF_SESSION_HINT_TTL_MS = 45 * 60 * 1000

let _warmupPromise = null

function _readSessionHint() {
  try {
    const raw = sessionStorage.getItem(SF_SESSION_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (!parsed?.expiresAt || Date.now() > parsed.expiresAt) {
      sessionStorage.removeItem(SF_SESSION_KEY)
      return null
    }
    return parsed
  } catch {
    return null
  }
}

function _writeSessionHint(payload) {
  try {
    const ttlMs = Math.min(
      SF_SESSION_HINT_TTL_MS,
      ((payload?.ttl_s || 0) > 0 ? payload.ttl_s * 1000 : SF_SESSION_HINT_TTL_MS),
    )
    sessionStorage.setItem(SF_SESSION_KEY, JSON.stringify({
      warmed: !!payload?.warmed,
      expiresAt: Date.now() + ttlMs,
      detail: payload?.detail || '',
    }))
  } catch { /* ignore quota / private mode */ }
}

/**
 * Ensure Snowflake backend session is warm. Dedupes concurrent callers
 * (App + Dashboard + StrictMode). Uses sessionStorage only as a UX hint so
 * remounts do not block again while the backend session is still valid.
 * Does NOT store SSO tokens in the browser — those live on the backend / OS keyring.
 */
export function ensureSnowflakeSession() {
  const hint = _readSessionHint()
  if (hint?.warmed) {
    // Non-blocking refresh in background; UI can proceed immediately.
    if (!_warmupPromise) {
      _warmupPromise = req('/snowflake/warmup')
        .then((r) => {
          if (r?.warmed) _writeSessionHint(r)
          return r
        })
        .catch((e) => {
          try { sessionStorage.removeItem(SF_SESSION_KEY) } catch { /* ignore */ }
          throw e
        })
        .finally(() => { _warmupPromise = null })
    }
    return Promise.resolve({ warmed: true, reused: true, detail: hint.detail || 'session hint' })
  }

  if (_warmupPromise) return _warmupPromise

  _warmupPromise = req('/snowflake/warmup')
    .then((r) => {
      if (r?.warmed) _writeSessionHint(r)
      else {
        try { sessionStorage.removeItem(SF_SESSION_KEY) } catch { /* ignore */ }
      }
      return r
    })
    .finally(() => { _warmupPromise = null })

  return _warmupPromise
}

/**
 * Stream chat responses via SSE. Calls back on each event.
 * Returns an abort function.
 */
export function chatStream(message, pipeline_id, context, session_id, { onMeta, onChunk, onDone, onError }) {
  const controller = new AbortController()
  fetch(BASE + '/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, pipeline_id, context, session_id }),
    signal: controller.signal,
  })
    .then((res) => {
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      function pump() {
        return reader.read().then(({ done, value }) => {
          if (done) {
            onDone?.(null)
            return
          }
          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''
          let eventType = ''
          for (const line of lines) {
            if (line.startsWith('event: ')) {
              eventType = line.slice(7).trim()
            } else if (line.startsWith('data: ')) {
              const data = JSON.parse(line.slice(6))
              if (eventType === 'meta') onMeta?.(data)
              else if (eventType === 'chunk') onChunk?.(data.text)
              else if (eventType === 'done') onDone?.(data.payload)
            }
          }
          return pump()
        })
      }
      return pump()
    })
    .catch((err) => {
      if (err.name !== 'AbortError') onError?.(err)
    })
  return () => controller.abort()
}

export const api = {
  snowflakeWarmup: () => ensureSnowflakeSession(),
  snowflakeSession: () => req('/snowflake/session'),
  health: () => req('/health'),
  connectivity: () => req('/connectivity'),
  getSettings: () => req('/settings'),
  saveSettings: (body) => req('/settings', { method: 'POST', body: JSON.stringify(body) }),
  summary: ({ status = 'FAILED', date_from, date_to } = {}) => {
    const q = new URLSearchParams()
    if (status) q.set('status', status)
    if (date_from) q.set('date_from', date_from)
    if (date_to) q.set('date_to', date_to)
    return req('/summary?' + q.toString())
  },
  dqSummary: ({ date_from, date_to, revalidate } = {}) => {
    const q = new URLSearchParams()
    if (date_from) q.set('date_from', date_from)
    if (date_to) q.set('date_to', date_to)
    if (revalidate === false) q.set('revalidate', 'false')
    if (revalidate === true) q.set('revalidate', 'true')
    return req('/dq/summary?' + q.toString())
  },
  logs: (id) => req(`/pipelines/${id}/logs`),
  rca: (pipeline_id, extra_context, { date_from, date_to } = {}) =>
    req('/rca', { method: 'POST', body: JSON.stringify({ pipeline_id, extra_context, date_from, date_to }) }),
  fix: (pipeline_id, user_edit, incident_id, rca_context) =>
    req('/fix', { method: 'POST', body: JSON.stringify({ pipeline_id, user_edit, incident_id, rca_context }) }),
  validate: (pipeline_id, fix_id) =>
    req('/validate', { method: 'POST', body: JSON.stringify({ pipeline_id, fix_id }) }),
  chat: (message, pipeline_id, context, session_id) =>
    req('/chat', { method: 'POST', body: JSON.stringify({ message, pipeline_id, context, session_id }) }),
  reportActivityError: (body) =>
    req('/chat/activity-error', { method: 'POST', body: JSON.stringify(body) }),
  dqDetails: (qcId, subjectArea) => {
    const q = new URLSearchParams()
    if (subjectArea) q.set('subject_area', subjectArea)
    return req(`/dq/details/${encodeURIComponent(qcId)}?${q.toString()}`)
  },
  incidents: () => req('/incidents'),
  addIssue: (body) => req('/issues', { method: 'POST', body: JSON.stringify(body) }),
  getRcaKnowledge: () => req('/rca/knowledge'),
  addRcaKnowledge: (body) => req('/rca/knowledge', { method: 'POST', body: JSON.stringify(body) }),
}
