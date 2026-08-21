import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import TableLineageGraph from './TableLineageGraph'
import RichRootCause from './RichRootCause'

// Survive StrictMode remount so RCA is not double-fired in dev.
const _rcaInflight = new Map()

function TestSummary({ test }) {
  const [open, setOpen] = useState(null)
  const s = test.summary
  return (
    <div className="card">
      <h3>Validation Test Summary · <span className="muted">{test.environment}</span></h3>
      <div className="summary-bar">
        <span className="pill">Clone: {test.clone_name}</span>
        <span className="pill" style={{ color: '#2ecc71' }}>Passed {s.passed}</span>
        <span className="pill" style={{ color: '#ff5c6c' }}>Failed {s.failed}</span>
        <span className="pill">Skipped {s.skipped}</span>
        <span className={`badge ${test.overall === 'PASS' ? 'SUCCESS' : 'FAILED'}`}>{test.overall}</span>
      </div>
      <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
        Zero-copy clone DDL: <code>{test.clone_ddl}</code>
      </div>
      {test.cases.map((c) => (
        <div key={c.id}>
          <div className="test-row">
            <div>
              <strong>{c.id}</strong> · {c.name} <span className="pill">{c.type}</span>
            </div>
            <div className="row" style={{ alignItems: 'center' }}>
              <span className={`badge ${c.status === 'PASS' ? 'SUCCESS' : c.status === 'FAIL' ? 'FAILED' : 'SKIPPED'}`}>{c.status}</span>
              <button className="btn sec" onClick={() => setOpen(open === c.id ? null : c.id)}>
                {open === c.id ? 'Hide' : 'View details'}
              </button>
            </div>
          </div>
          {open === c.id && (
            <div className="test-detail">
              <div><strong>SQL / step run:</strong><pre>{c.sql}</pre></div>
              <div className="row">
                <span className="pill">Expected: {c.expected}</span>
                <span className="pill">Actual: {c.actual}</span>
              </div>
              <div style={{ marginTop: 6 }}><strong>Evidence:</strong> <span className="muted">{c.evidence}</span></div>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function slimRcaContext(rca) {
  if (!rca) return null
  return {
    incident_id: rca.incident_id,
    pipeline_id: rca.pipeline_id,
    analysis_type: rca.analysis_type,
    category: rca.category,
    summary: rca.summary,
    detailed_analysis: rca.detailed_analysis,
    root_cause_node: rca.root_cause_node,
    root_cause_name: rca.root_cause_name,
    root_cause: rca.root_cause,
    evidence: rca.evidence,
    remediation: rca.remediation,
    code_analysis: rca.code_analysis,
    diagnostic_results: rca.diagnostic_results,
    dq_execution_result: rca.dq_execution_result,
    // Needed so Fix can emit before/after against real procedure DDL (not invent ETL).
    procedure_chain: rca.procedure_chain,
    affected_tables: rca.affected_tables,
  }
}

function FixDiff({ fix, onEdit, onValidate, busy }) {
  const [edit, setEdit] = useState('')
  const groundingColor = {
    evidence_seeded: '#2ecc71',
    rca_guided: '#2ecc71',
    llm_polished: '#4f8cff',
    template_fallback: '#ffb547',
  }
  const groundingLabel = {
    evidence_seeded: 'Evidence-seeded',
    rca_guided: 'RCA-guided',
    llm_polished: 'LLM-polished',
    template_fallback: 'Template fallback',
  }
  const facts = (fix.evidence_facts || []).slice(0, 5)
  const hints = fix.validation_hints || []
  return (
    <div className="card">
      <h3>Identified Fix · {fix.title}</h3>
      <div className="row" style={{ marginBottom: 8 }}>
        <span className="pill">Target: {fix.target?.platform} / {fix.target?.artifact}</span>
        <span className="pill">Risk: {fix.risk}</span>
        <span className="pill">Category: {fix.category}</span>
        {fix.grounding && (
          <span className="pill" style={{ color: groundingColor[fix.grounding] || 'var(--muted)' }}>
            {groundingLabel[fix.grounding] || fix.grounding}
          </span>
        )}
        {fix.reused_from_memory && <span className="pill" style={{ color: '#7c5cff' }}>reused from memory</span>}
      </div>
      <p className="muted">{fix.rationale}</p>
      {fix.evidence_summary && (
        <div style={{ marginBottom: 10, padding: '8px 10px', background: 'var(--panel2)', borderRadius: 8, fontSize: 13 }}>
          <strong>Evidence:</strong> <span className="muted">{fix.evidence_summary}</span>
          {facts.length > 0 && (
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
              {facts.map((f, i) => (
                <li key={i} className="muted" style={{ fontSize: 12 }}>{typeof f === 'string' ? f : JSON.stringify(f)}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      <div className="diff">
        <div>
          <h4 style={{ color: '#ff5c6c' }}>Before</h4>
          <pre className="before">{fix.before}</pre>
        </div>
        <div>
          <h4 style={{ color: '#2ecc71' }}>After (proposed)</h4>
          <pre className="after">{fix.after}</pre>
        </div>
      </div>
      <p className="muted" style={{ fontSize: 12 }}>Rollback: {fix.rollback}</p>
      {hints.length > 0 && (
        <div style={{ marginTop: 8, fontSize: 12 }}>
          <strong>Validation hints:</strong>
          <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
            {hints.map((h, i) => <li key={i} className="muted">{h}</li>)}
          </ul>
        </div>
      )}
      <div className="chat-input" style={{ padding: 0, borderTop: 'none', marginTop: 10 }}>
        <input type="text" placeholder="Modify the fix (e.g. use MEDIUM warehouse, coalesce to 0)"
          value={edit} onChange={(e) => setEdit(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && edit) { onEdit(edit); setEdit('') } }} />
        <button className="btn sec" disabled={!edit} onClick={() => { onEdit(edit); setEdit('') }}>Modify</button>
        <button className="btn warn" disabled={busy} onClick={onValidate}>Validate fix</button>
      </div>
    </div>
  )
}

function reportApiError(onReportError, action, err) {
  onReportError?.({ tab: 'Workbench', action, error: err?.message || String(err) })
}


const SEVERITY_COLOR = { Critical: '#e74c3c', High: '#e67e22', Medium: '#f1c40f', Low: '#2ecc71' }
const CONFIDENCE_COLOR = { High: '#2ecc71', Medium: '#f1c40f', Low: '#e74c3c' }
const PROPAGATION_COLOR = { root_cause: '#ff5c6c', failed: '#ff8a3d', impacted: '#ffb547' }
const EVIDENCE_ICON = {
  error_message: '!', task_metadata: 'T', query_id: 'Q', table_resolution: '#',
  dq_correlation: 'D', log_entry: 'L', dq_check: 'D', task_correlation: 'C',
}
const STRENGTH_COLOR = { high: '#2ecc71', medium: '#f1c40f', low: '#e74c3c' }

function InvestigationJourney({ journey }) {
  if (!journey?.length) return null
  return (
    <div className="investigation-journey">
      {journey.map((step, i) => (
        <div key={i} className="ij-step">
          <div className="ij-dot" />
          <div className="ij-content">
            <span className="ij-label">{step.step}</span>
            {step.detail && <span className="ij-detail">{step.detail}</span>}
          </div>
        </div>
      ))}
    </div>
  )
}

function FailurePropagation({ chain }) {
  if (!chain?.length) return null
  return (
    <div className="propagation-chain">
      {chain.map((node, i) => (
        <React.Fragment key={i}>
          <div className="prop-node" style={{ borderColor: PROPAGATION_COLOR[node.status] || '#8b95a5' }}>
            <span className="prop-type">{node.type}</span>
            <span className="prop-name">{node.name}</span>
          </div>
          {i < chain.length - 1 && <div className="prop-arrow">↓</div>}
        </React.Fragment>
      ))}
    </div>
  )
}

function EvidenceCards({ structured, fallback }) {
  if (structured?.length > 0) {
    return (
      <div className="evidence-cards">
        {structured.map((ev, i) => (
          <div key={i} className="evidence-card">
            <span className="ev-icon" style={{ background: STRENGTH_COLOR[ev.strength] || '#8b95a5' }}>
              {EVIDENCE_ICON[ev.type] || 'E'}
            </span>
            <div className="ev-body">
              <div className="ev-summary">{ev.summary}</div>
              <div className="ev-meta">
                <span className="ev-source">{ev.source}</span>
                <span className="ev-strength" style={{ color: STRENGTH_COLOR[ev.strength] }}>{ev.strength}</span>
              </div>
            </div>
          </div>
        ))}
      </div>
    )
  }
  if (fallback?.length > 0) {
    return <ul className="evidence">{fallback.map((e, i) => <li key={i}>{e}</li>)}</ul>
  }
  return null
}

function ImpactCards({ ia }) {
  if (!ia) return null
  const cards = [
    { label: 'Tables', value: ia.impacted_tables?.length || 0 },
    { label: 'Pipelines', value: ia.impacted_pipelines?.length || 0 },
    { label: 'Reports', value: ia.impacted_reports?.length || 0 },
    { label: 'Severity', value: ia.business_severity || 'Unknown', isSeverity: true },
  ]
  return (
    <div className="impact-cards">
      {cards.map((c) => (
        <div key={c.label} className="impact-card">
          <div className={`impact-card__value ${c.isSeverity ? 'severity' : ''}`}
            style={c.isSeverity ? { color: SEVERITY_COLOR[c.value] || 'var(--text)' } : undefined}>
            {c.value}
          </div>
          <div className="impact-card__label">{c.label}</div>
        </div>
      ))}
    </div>
  )
}

function ConfidenceSection({ confidence, confidenceLevel, drivers }) {
  const confColor = CONFIDENCE_COLOR[confidenceLevel] || 'var(--border)'
  return (
    <div className="confidence-section">
      <div className="conf-score" style={{ borderColor: confColor }}>
        <span className="conf-value" style={{ color: confColor }}>{Math.round((confidence || 0) * 100)}%</span>
        <span className="conf-label">{confidenceLevel || 'Unknown'}</span>
      </div>
      {drivers?.length > 0 && (
        <div className="conf-factors">
          {drivers.map((d, i) => (
            <div key={i} className={`conf-factor ${d.met ? 'met' : 'unmet'}`}>
              <span className="conf-check">{d.met ? '✓' : '✗'}</span>
              <span>{d.factor}</span>
            </div>
          ))}
        </div>
      )}
      {confidenceLevel === 'Low' && (
        <div className="conf-warning">Human validation recommended</div>
      )}
    </div>
  )
}

function RcaReport({ rca, selected, busy, onSuggestFix, onAddKnowledge, onOpenKnowledge }) {
  const ia = rca.impact_assessment || {}
  const inc = rca.incident_summary || {}
  const sevColor = SEVERITY_COLOR[ia.business_severity] || 'var(--border)'
  const confColor = CONFIDENCE_COLOR[rca.confidence_level] || 'var(--border)'

  return (
    <div className="card rca-simplified">
      {/* ═══ SECTION 1: INCIDENT SUMMARY ═════════════════════════════════════ */}
      <div className="incident-header">
        <div className="incident-header__top">
          <span className="pill" style={{ background: 'var(--border)', fontWeight: 600 }}>
            {inc.failure_type || rca.failure_type || rca.category}
          </span>
          <span className="pill" style={{ background: confColor, color: '#fff', fontWeight: 700 }}>
            {rca.confidence_level} ({inc.confidence_score || Math.round((rca.confidence || 0) * 100)}%)
          </span>
          {ia.business_severity && (
            <span className="pill" style={{ background: sevColor, color: '#fff', fontWeight: 700 }}>
              {ia.business_severity}
            </span>
          )}
          <span className="pill muted" style={{ marginLeft: 'auto' }}>{rca.incident_id}</span>
        </div>
        <div className="incident-header__details">
          <div className="ih-field">
            <span className="ih-label">Failure</span>
            <span className="ih-value">{inc.failure_name || rca.root_cause_name}</span>
          </div>
          <div className="ih-field">
            <span className="ih-label">Environment</span>
            <span className="ih-value">{inc.environment || 'Production'}</span>
          </div>
          <div className="ih-field">
            <span className="ih-label">Detected</span>
            <span className="ih-value">{inc.detection_time || '—'}</span>
          </div>
          <div className="ih-field">
            <span className="ih-label">Status</span>
            <span className={`badge ${inc.current_status || rca.category}`}>{inc.current_status || 'FAILED'}</span>
          </div>
        </div>
      </div>

      {/* ═══ SECTION 2: ROOT CAUSE ═══════════════════════════════════════════ */}
      <div className="rca-section">
        {rca.root_cause ? (
          <RichRootCause rootCause={rca.root_cause} />
        ) : (
          <>
            <h4 className="rca-section__title">Root Cause</h4>
            <p style={{ marginBottom: 6 }}>{rca.summary}</p>
            {rca.detailed_analysis && rca.detailed_analysis !== rca.summary && (
              <p className="muted" style={{ fontSize: 13 }}>{rca.detailed_analysis}</p>
            )}
          </>
        )}

        {rca.upstream_lineage?.nodes?.length > 0 && (
          <div style={{ marginTop: 12 }}>
            <TableLineageGraph
              tableLineage={rca.upstream_lineage}
              title="Upstream lineage"
              direction="upstream"
              defaultOpen={false}
              colorScheme="upstream"
            />
          </div>
        )}
      </div>

      {/* ═══ SECTION 5: LINEAGE & IMPACT ═════════════════════════════════════ */}
      {rca.downstream_lineage?.nodes?.length > 0 && (
        <div className="rca-section">
          <TableLineageGraph
            tableLineage={rca.downstream_lineage}
            title="Downstream impact"
            direction="downstream"
            defaultOpen={false}
            colorScheme="downstream"
          />
        </div>
      )}

      {/* ═══ SECTION 7: IMPACT SUMMARY ═══════════════════════════════════════ */}
      <div className="rca-section">
        <h4 className="rca-section__title">Impact Summary</h4>
        <p className="rca-impact-summary">
          {rca.impact_summary || `${ia.impacted_tables?.length || 0} table(s), ${ia.impacted_pipelines?.length || 0} pipeline(s) impacted`}
        </p>
        <ImpactCards ia={ia} />
      </div>

      {/* ═══ SECTION 8: AI CONFIDENCE EXPLANATION ════════════════════════════ */}
      <div className="rca-section">
        <h4 className="rca-section__title">AI Confidence</h4>
        <ConfidenceSection
          confidence={rca.confidence}
          confidenceLevel={rca.confidence_level}
          drivers={rca.confidence_drivers}
        />
      </div>

      {/* ═══ ACTIONS ════════════════════════════════════════════════════════ */}
      <div className="rca-section">
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          <button className="btn purple" disabled={busy === 'fix'}
            onClick={onSuggestFix}>
            Identify &amp; Suggest Fix
          </button>
          <button className="btn sec" onClick={() => onAddKnowledge?.(rca)}>
            + Add to Knowledge Base
          </button>
          <button className="btn sec" onClick={() => onOpenKnowledge?.()}>
            View Knowledge Base
          </button>
        </div>
      </div>

      {/* ═══ CODE ANALYSIS (inline, not collapsible) ═════════════════════════ */}
      {rca.code_analysis?.llm_explanation && (
        <div className="rca-section">
          <h4 className="rca-section__title">Code Analysis</h4>
          <p style={{ fontSize: 13, margin: 0, whiteSpace: 'pre-line' }}>{rca.code_analysis.llm_explanation}</p>
          {rca.code_analysis?.task_sql && (
            <pre style={{
              fontSize: 11, lineHeight: 1.5, margin: '8px 0 0', whiteSpace: 'pre-wrap',
              background: 'var(--code-bg)', padding: 10, borderRadius: 6, maxHeight: 220, overflow: 'auto',
            }}>{rca.code_analysis.task_sql}</pre>
          )}
        </div>
      )}
    </div>
  )
}

function KnowledgeForm({ rca, onClose, onSaved }) {
  const [pattern, setPattern] = useState((rca?.evidence?.[2] || rca?.summary || '').slice(0, 200))
  const [category, setCategory] = useState(rca?.category || 'Unknown Failure')
  const [rootCause, setRootCause] = useState(rca?.summary || '')
  const [fix, setFixText] = useState(rca?.remediation?.immediate_fix || '')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  const submit = () => {
    setSaving(true)
    api.addRcaKnowledge({ pattern, category, root_cause: rootCause, fix, added_by: 'user' })
      .then(() => {
        setSaved(true)
        setTimeout(() => {
          onClose()
          onSaved?.()
        }, 800)
      })
      .catch(() => setSaving(false))
  }

  return (
    <div className="card" style={{ border: '2px solid var(--accent)' }}>
      <h3 style={{ margin: '0 0 12px' }}>Add to Knowledge Base</h3>
      <p className="muted" style={{ fontSize: 12, margin: '0 0 10px' }}>
        This knowledge will be used by the RCA agent in future analyses when a similar failure occurs.
      </p>
      <div className="field">
        <label>Error Pattern (matched against future failures)</label>
        <input type="text" value={pattern} onChange={(e) => setPattern(e.target.value)} />
      </div>
      <div className="field">
        <label>Category</label>
        <select value={category} onChange={(e) => setCategory(e.target.value)}>
          {['Code Failure', 'Data Quality Failure', 'Dependency Failure', 'Infrastructure Failure', 'Data Availability Failure', 'Unknown Failure']
            .map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </div>
      <div className="field">
        <label>Root Cause</label>
        <textarea rows={2} value={rootCause} onChange={(e) => setRootCause(e.target.value)} />
      </div>
      <div className="field">
        <label>Fix / Resolution</label>
        <textarea rows={2} value={fix} onChange={(e) => setFixText(e.target.value)} />
      </div>
      <div style={{ display: 'flex', gap: 10 }}>
        <button className="btn" disabled={saving || !pattern} onClick={submit}>
          {saved ? 'Saved!' : saving ? 'Saving...' : 'Save Knowledge'}
        </button>
        <button className="btn sec" onClick={onClose}>Cancel</button>
      </div>
    </div>
  )
}

export default function Workbench({
  activePipeline, onSelect, onReportError, onBackToDashboard, onOpenKnowledge, dateFrom, dateTo,
  onRcaChange, externalRca,
}) {
  const [selected, setSelected] = useState(activePipeline)
  const [rca, setRca] = useState(null)
  const [fix, setFix] = useState(null)
  const [test, setTest] = useState(null)
  const [busy, setBusy] = useState('')
  const [hasActioned, setHasActioned] = useState(!!activePipeline)
  const [showKnowledgeForm, setShowKnowledgeForm] = useState(false)

  // Keep latest dates/handlers without re-binding runRCA (avoids re-running RCA on date filter changes).
  const dateFromRef = useRef(dateFrom)
  const dateToRef = useRef(dateTo)
  const onReportErrorRef = useRef(onReportError)
  const onRcaChangeRef = useRef(onRcaChange)
  const rcaTargetIdRef = useRef(null)
  const rcaEpochRef = useRef(0)
  dateFromRef.current = dateFrom
  dateToRef.current = dateTo
  onReportErrorRef.current = onReportError
  onRcaChangeRef.current = onRcaChange

  const runRCA = useCallback((id) => {
    if (!id) return
    const df = dateFromRef.current
    const dt = dateToRef.current
    const key = `${id}|${df || ''}|${dt || ''}`
    rcaTargetIdRef.current = id
    const epoch = ++rcaEpochRef.current
    const existing = _rcaInflight.get(key)
    if (existing) {
      setBusy('rca')
      existing
        .then((r) => {
          if (epoch !== rcaEpochRef.current || rcaTargetIdRef.current !== id) return
          if (r?.error) throw new Error(r.error)
          if (r) {
            setRca(r)
            onRcaChangeRef.current?.(r)
          }
        })
        .catch((e) => reportApiError(onReportErrorRef.current, 'run RCA', e))
        .finally(() => { if (rcaTargetIdRef.current === id && epoch === rcaEpochRef.current) setBusy('') })
      return
    }
    setBusy('rca'); setRca(null); setFix(null); setTest(null)
    onRcaChangeRef.current?.(null)
    const date_from = df ? `${df}T00:00:00+00:00` : undefined
    const date_to = dt ? `${dt}T23:59:59+00:00` : undefined
    const promise = api.rca(id, undefined, { date_from, date_to })
      .then((r) => {
        if (epoch !== rcaEpochRef.current || rcaTargetIdRef.current !== id) return r
        if (r.error) throw new Error(r.error)
        setRca(r)
        onRcaChangeRef.current?.(r)
        return r
      })
      .catch((e) => {
        if (rcaTargetIdRef.current === id && epoch === rcaEpochRef.current) {
          reportApiError(onReportErrorRef.current, 'run RCA', e)
        }
        return null
      })
      .finally(() => {
        if (_rcaInflight.get(key) === promise) _rcaInflight.delete(key)
        if (rcaTargetIdRef.current === id && epoch === rcaEpochRef.current) setBusy('')
      })
    _rcaInflight.set(key, promise)
  }, [])

  // Only start RCA when the selected pipeline/check changes — NOT when Dashboard date filters change
  // (Workbench stays mounted under display:none, so date props used to re-fire runRCA for the old id).
  useEffect(() => {
    if (!activePipeline?.id) return
    setSelected(activePipeline)
    setHasActioned(true)
    runRCA(activePipeline.id)
  }, [activePipeline?.id, runRCA])

  useEffect(() => {
    if (!externalRca?.rca || externalRca.rca.error) return
    const pid = externalRca.pipelineId
    if (pid && activePipeline?.id && pid !== activePipeline.id) return
    rcaEpochRef.current += 1
    rcaTargetIdRef.current = activePipeline?.id || pid || rcaTargetIdRef.current
    setRca(externalRca.rca)
    setFix(null)
    setTest(null)
    setHasActioned(true)
    setBusy('')
    onRcaChangeRef.current?.(externalRca.rca)
  }, [externalRca?.seq])
  const suggestFix = () => {
    setBusy('fix')
    const ctx = slimRcaContext(rca)
    api.fix(selected.id, undefined, rca?.incident_id || ctx?.incident_id, ctx)
      .then((r) => { if (r.error) throw new Error(r.error); setFix(r) })
      .catch((e) => reportApiError(onReportError, 'suggest fix', e))
      .finally(() => setBusy(''))
  }
  const editFix = (text) => {
    setBusy('fix')
    const ctx = slimRcaContext(rca)
    api.fix(selected.id, text, rca?.incident_id || ctx?.incident_id, ctx)
      .then(setFix)
      .catch((e) => reportApiError(onReportError, 'modify fix', e))
      .finally(() => setBusy(''))
  }
  const validate = () => {
    setBusy('test')
    api.validate(selected.id, fix?.fix_id)
      .then((r) => { if (r.error) throw new Error(r.error); setTest(r) })
      .catch((e) => reportApiError(onReportError, 'validate fix', e))
      .finally(() => setBusy(''))
  }

  return (
    <div>
      {!hasActioned ? (
        <div className="card" style={{ textAlign: 'center', padding: '60px 30px' }}>
          <h3 style={{ marginBottom: 12 }}>Workbench</h3>
          <p className="muted" style={{ fontSize: 15 }}>
            Go to <strong>Dashboard</strong> to see if any DQ check, task, or pipeline has failed.
          </p>
          <p className="muted" style={{ fontSize: 13 }}>
            Click <strong>Run RCA</strong> on a failed item to start the investigation here.
          </p>
        </div>
      ) : (
        <div style={{ marginBottom: 12 }}>
          <button className="btn sec" onClick={onBackToDashboard}>← Back to Dashboard</button>
        </div>
      )}

      {busy === 'rca' && <div className="card spinner">Running RCA agent…</div>}

      {rca && !rca.error && (
        <RcaReport
          rca={rca}
          selected={selected}
          busy={busy}
          onSuggestFix={suggestFix}
          onAddKnowledge={() => setShowKnowledgeForm(true)}
          onOpenKnowledge={onOpenKnowledge}
        />
      )}

      {showKnowledgeForm && rca && (
        <KnowledgeForm
          rca={rca}
          onClose={() => setShowKnowledgeForm(false)}
          onSaved={onOpenKnowledge}
        />
      )}

      {busy === 'fix' && <div className="card spinner">Fix agent working…</div>}
      {fix && <FixDiff fix={fix} onEdit={editFix} onValidate={validate} busy={busy === 'test'} />}
      {busy === 'test' && <div className="card spinner">Validation agent: cloning &amp; testing on pre-prod…</div>}
      {test && <TestSummary test={test} />}
    </div>
  )
}
