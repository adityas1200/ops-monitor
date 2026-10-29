import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'

const CATEGORIES = [
  'Code Failure',
  'Data Quality Failure',
  'Dependency Failure',
  'Infrastructure Failure',
  'Data Availability Failure',
  'Unknown Failure',
]

function emptyForm() {
  return {
    pattern: '',
    category: 'Unknown Failure',
    root_cause: '',
    fix: '',
    sql_code: '',
    notes: '',
    added_by: 'user',
  }
}

function formFromRule(rule) {
  return {
    pattern: rule.PATTERN || rule.pattern || '',
    category: rule.CATEGORY || rule.category || 'Unknown Failure',
    root_cause: rule.ROOT_CAUSE || rule.root_cause || '',
    fix: rule.FIX || rule.fix || '',
    sql_code: rule.SQL_CODE || rule.sql_code || '',
    notes: rule.NOTES || rule.notes || '',
    added_by: rule.ADDED_BY || rule.added_by || 'user',
  }
}

function KnowledgeForm({ mode, initial, onClose, onSaved, onReportError }) {
  const [form, setForm] = useState(() => initial || emptyForm())
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState(null)

  const setField = (key) => (e) => setForm((prev) => ({ ...prev, [key]: e.target.value }))

  const submit = () => {
    const pattern = (form.pattern || '').trim()
    if (!pattern) {
      setError('Pattern is required.')
      return
    }
    setSaving(true)
    setError(null)
    const body = {
      pattern,
      category: form.category,
      root_cause: form.root_cause,
      fix: form.fix,
      sql_code: form.sql_code,
      notes: form.notes,
      added_by: form.added_by || 'user',
    }
    const req = mode === 'edit'
      ? api.updateRcaKnowledge(initial.index, body)
      : api.addRcaKnowledge(body)
    req
      .then(() => {
        setSaved(true)
        setTimeout(() => {
          onClose()
          onSaved?.()
        }, 600)
      })
      .catch((e) => {
        const msg = e.message || String(e)
        setError(msg)
        setSaving(false)
        onReportError?.({
          tab: 'Knowledge',
          action: mode === 'edit' ? 'update knowledge' : 'add knowledge',
          error: msg,
        })
      })
  }

  return (
    <div className="card" style={{ border: '2px solid var(--accent)', marginBottom: 16 }}>
      <h3 style={{ margin: '0 0 12px' }}>
        {mode === 'edit' ? 'Edit Knowledge Rule' : 'Add to Knowledge Base'}
      </h3>
      <p className="muted" style={{ fontSize: 12, margin: '0 0 10px' }}>
        This knowledge is matched by the RCA agent when a similar failure occurs.
      </p>
      <div className="field">
        <label>Error Pattern (matched against future failures)</label>
        <input type="text" value={form.pattern} onChange={setField('pattern')} />
      </div>
      <div className="field">
        <label>Category</label>
        <select value={form.category} onChange={setField('category')}>
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
      </div>
      <div className="field">
        <label>Root Cause</label>
        <textarea rows={3} value={form.root_cause} onChange={setField('root_cause')} />
      </div>
      <div className="field">
        <label>Fix / Resolution</label>
        <textarea rows={3} value={form.fix} onChange={setField('fix')} />
      </div>
      <div className="field">
        <label>SQL Code (optional)</label>
        <textarea
          rows={6}
          value={form.sql_code}
          onChange={setField('sql_code')}
          placeholder="Paste the fixed / resolution SQL here"
          style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', fontSize: 12 }}
        />
      </div>
      <div className="field">
        <label>Notes (optional)</label>
        <textarea rows={2} value={form.notes} onChange={setField('notes')} />
      </div>
      {error && (
        <p style={{ color: 'var(--danger, #c44)', fontSize: 13, margin: '0 0 10px' }}>{error}</p>
      )}
      <div style={{ display: 'flex', gap: 10 }}>
        <button className="btn" disabled={saving || !(form.pattern || '').trim()} onClick={submit}>
          {saved ? 'Saved!' : saving ? 'Saving…' : mode === 'edit' ? 'Save Changes' : 'Save Knowledge'}
        </button>
        <button className="btn sec" onClick={onClose} disabled={saving && !saved}>Cancel</button>
      </div>
    </div>
  )
}

function RuleCard({ rule, index, onEdit, onDelete, deleting }) {
  const [open, setOpen] = useState(false)
  const pattern = rule.PATTERN || rule.pattern || '—'
  const category = rule.CATEGORY || rule.category || 'Unknown Failure'
  const rootCause = rule.ROOT_CAUSE || rule.root_cause || ''
  const fix = rule.FIX || rule.fix || ''
  const sqlCode = rule.SQL_CODE || rule.sql_code || ''
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
        <div style={{ display: 'flex', gap: 8, flexShrink: 0, alignItems: 'center' }}>
          <button className="btn sec" onClick={() => onEdit?.(index, rule)}>Edit</button>
          <button className="btn sec" onClick={() => setOpen((v) => !v)}>
            {open ? 'Collapse' : 'Details'}
          </button>
          <button
            type="button"
            className="btn sec icon-btn"
            title={deleting ? 'Deleting…' : 'Delete rule'}
            aria-label={deleting ? 'Deleting…' : 'Delete rule'}
            disabled={deleting}
            onClick={() => onDelete?.(index, rule)}
            style={{
              color: 'var(--red)',
              padding: '6px 8px',
              minWidth: 32,
              opacity: deleting ? 0.55 : 1,
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path
                d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              <path
                d="M10 11v6M14 11v6"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
              />
            </svg>
          </button>
        </div>
      </div>
      {open && (
        <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
          <div className="field" style={{ marginBottom: 10 }}>
            <label>Root Cause</label>
            <p style={{ margin: 0, fontSize: 13, whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>{rootCause || '—'}</p>
          </div>
          <div className="field" style={{ marginBottom: 10 }}>
            <label>Fix / Resolution</label>
            <p style={{ margin: 0, fontSize: 13, whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>{fix || '—'}</p>
          </div>
          {sqlCode ? (
            <div className="field" style={{ marginBottom: 10 }}>
              <label>SQL Code</label>
              <pre style={{
                margin: 0,
                fontSize: 12,
                whiteSpace: 'pre-wrap',
                background: 'var(--code-bg)',
                color: 'var(--code-text)',
                padding: 10,
                borderRadius: 6,
                overflow: 'auto',
              }}>{sqlCode}</pre>
            </div>
          ) : null}
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
  const [formMode, setFormMode] = useState(null) // 'add' | 'edit' | null
  const [editInitial, setEditInitial] = useState(null)
  const [deletingIndex, setDeletingIndex] = useState(null)

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

  const closeForm = () => {
    setFormMode(null)
    setEditInitial(null)
  }

  const openAdd = () => {
    setEditInitial(emptyForm())
    setFormMode('add')
  }

  const openEdit = (index, rule) => {
    setEditInitial({ ...formFromRule(rule), index })
    setFormMode('edit')
  }

  const handleDelete = (index, rule) => {
    const pattern = rule?.PATTERN || rule?.pattern || `rule #${index + 1}`
    if (!window.confirm(`Delete this knowledge rule?\n\n${pattern}`)) return
    setDeletingIndex(index)
    api.deleteRcaKnowledge(index)
      .then(() => {
        if (formMode === 'edit' && editInitial?.index === index) closeForm()
        load()
      })
      .catch((e) => {
        const msg = e.message || String(e)
        setError(msg)
        onReportError?.({ tab: 'Knowledge', action: 'delete knowledge', error: msg })
      })
      .finally(() => setDeletingIndex(null))
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 16 }}>
        <div>
          <h3 style={{ margin: 0 }}>RCA Knowledge Base</h3>
          <p className="muted" style={{ margin: '6px 0 0', fontSize: 13 }}>
            Add, edit, or delete reusable RCA rules. The agent matches these patterns on future failures.
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn" disabled={loading || formMode === 'add'} onClick={openAdd}>
            + Add Knowledge
          </button>
          <button className="btn sec" disabled={loading} onClick={load}>
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </div>

      {formMode && (
        <KnowledgeForm
          key={`${formMode}-${editInitial?.index ?? 'new'}`}
          mode={formMode}
          initial={editInitial}
          onClose={closeForm}
          onSaved={load}
          onReportError={onReportError}
        />
      )}

      {loading && !rules.length && <div className="card spinner">Loading saved knowledge…</div>}
      {error && (
        <div className="card" style={{ borderColor: 'var(--danger, #c44)' }}>
          <p style={{ margin: 0 }}>{error}</p>
        </div>
      )}
      {!loading && !error && rules.length === 0 && !formMode && (
        <div className="card" style={{ textAlign: 'center', padding: '48px 24px' }}>
          <h4 style={{ margin: '0 0 8px' }}>No saved knowledge yet</h4>
          <p className="muted" style={{ margin: '0 0 16px', fontSize: 13 }}>
            Add a rule here, or save one from Workbench after running RCA.
          </p>
          <button className="btn" onClick={openAdd}>+ Add Knowledge</button>
        </div>
      )}
      {rules.map((rule, i) => (
        <RuleCard
          key={`${rule.PATTERN || ''}-${rule.ADDED_ON || ''}-${i}`}
          rule={rule}
          index={i}
          onEdit={openEdit}
          onDelete={handleDelete}
          deleting={deletingIndex === i}
        />
      ))}
    </div>
  )
}
