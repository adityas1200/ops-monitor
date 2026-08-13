import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'

function RuleCard({ rule, index }) {
  const [open, setOpen] = useState(false)
  const pattern = rule.PATTERN || rule.pattern || '—'
  const category = rule.CATEGORY || rule.category || 'Unknown Failure'
  const rootCause = rule.ROOT_CAUSE || rule.root_cause || ''
  const fix = rule.FIX || rule.fix || ''
  const addedOn = rule.ADDED_ON || rule.added_on || ''
  const addedBy = rule.ADDED_BY || rule.added_by || ''
  const notes = rule.NOTES || rule.notes || ''

  return (
    <div className="card knowledge-rule">
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 6 }}>
            <span className="badge" style={{ fontSize: 11 }}>{category}</span>
            {(addedOn || addedBy) && (
              <span className="muted" style={{ fontSize: 11 }}>
                {addedOn ? `Added ${addedOn}` : ''}
                {addedOn && addedBy ? ' · ' : ''}
                {addedBy ? `by ${addedBy}` : ''}
              </span>
            )}
          </div>
          <h4 style={{ margin: '0 0 6px', fontSize: 14, lineHeight: 1.4 }}>{pattern}</h4>
          {!open && rootCause && (
            <p className="muted" style={{ margin: 0, fontSize: 13, lineHeight: 1.45 }}>
              {rootCause.length > 160 ? `${rootCause.slice(0, 160)}…` : rootCause}
            </p>
          )}
        </div>
        <button className="btn sec" style={{ flexShrink: 0 }} onClick={() => setOpen((v) => !v)}>
          {open ? 'Collapse' : 'Details'}
        </button>
      </div>
      {open && (
        <div style={{ marginTop: 12, displayTop: '1px solid var(--border)', paddingTop: 12 }}>
          <div className="field" style={{ marginBottom: 10 }}>
            <label>Root Cause</label>
            <p style={{ margin: 0, fontSize: 13, whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>{rootCause || '—'}</p>
          </div>
          <div className="field" style={{ marginBottom: 10 }}>
            <label>Fix / Resolution</label>
            <p style={{ margin: 0, fontSize: 13, whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>{fix || '—'}</p>
          </div>
          {notes ? (
            <div className="field">
              <label>Notes</label>
              <p style={{ margin: 0, fontSize: 13, whiteSpace: 'pre-wrap' }}>{notes}</p>
            </div>
          ) : null}
          <p className="muted" style={{ margin: '8px 0 0', fontSize: 11 }}>Rule #{index + 1}</p>
        </div>
      )}
    </div>
  )
}

export default function KnowledgeBase({ active = true, onReportError }) {
  const [rules, setRules] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const load = useCallback(() => {
    setLoading(true)
    setError(null)
    api.getRcaKnowledge()
      .then((r) => setRules(Array.isArray(r?.rules) ? r.rules : []))
      .catch((e) => {
        setError(e.message || String(e))
        onReportError?.({ tab: 'Knowledge', action: 'load knowledge', error: e.message || String(e) })
      })
      .finally(() => setLoading(false))
  }, [onReportError])

  useEffect(() => {
    if (active) load()
  }, [active, load])

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 16 }}>
        <div>
          <h3 style={{ margin: 0 }}>RCA Knowledge Base</h3>
          <p className="muted" style={{ margin: '6px 0 0', fontSize: 13 }}>
            Rules saved from Workbench. The RCA agent matches these patterns on future failures.
          </p>
        </div>
        <button className="btn sec" disabled={loading} onClick={load}>
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {loading && !rules.length && <div className="card spinner">Loading saved knowledge…</div>}
      {error && (
        <div className="card" style={{ borderColor: 'var(--danger, #c44)' }}>
          <p style={{ margin: 0 }}>Could not load knowledge: {error}</p>
        </div>
      )}
      {!loading && !error && rules.length === 0 && (
        <div className="card" style={{ textAlign: 'center', padding: '48px 24px' }}>
          <h4 style={{ margin: '0 0 8px' }}>No saved knowledge yet</h4>
          <p className="muted" style={{ margin: 0, fontSize: 13 }}>
            Run RCA in Workbench, then use <strong>+ Add to Knowledge Base</strong> to save a rule.
          </p>
        </div>
      )}
      {rules.map((rule, i) => (
        <RuleCard key={`${rule.PATTERN || ''}-${rule.ADDED_ON || ''}-${i}`} rule={rule} index={i} />
      ))}
    </div>
  )
}
