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

export const api = {
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
  dqSummary: ({ date_from, date_to } = {}) => {
    const q = new URLSearchParams()
    if (date_from) q.set('date_from', date_from)
    if (date_to) q.set('date_to', date_to)
    return req('/dq/summary?' + q.toString())
  },
  logs: (id) => req(`/pipelines/${id}/logs`),
  rca: (pipeline_id, extra_context) =>
    req('/rca', { method: 'POST', body: JSON.stringify({ pipeline_id, extra_context }) }),
  fix: (pipeline_id, user_edit, incident_id) =>
    req('/fix', { method: 'POST', body: JSON.stringify({ pipeline_id, user_edit, incident_id }) }),
  validate: (pipeline_id, fix_id) =>
    req('/validate', { method: 'POST', body: JSON.stringify({ pipeline_id, fix_id }) }),
  chat: (message, pipeline_id, context) =>
    req('/chat', { method: 'POST', body: JSON.stringify({ message, pipeline_id, context }) }),
  reportActivityError: (body) =>
    req('/chat/activity-error', { method: 'POST', body: JSON.stringify(body) }),
  incidents: () => req('/incidents'),
  addIssue: (body) => req('/issues', { method: 'POST', body: JSON.stringify(body) }),
}
