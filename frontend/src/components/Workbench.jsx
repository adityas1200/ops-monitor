import React, { useEffect, useState } from 'react'
import { api } from '../api/client'
import LineageTable from './LineageTable'
import TableLineageGraph from './TableLineageGraph'

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

function FixDiff({ fix, onEdit, onValidate, busy }) {
  const [edit, setEdit] = useState('')
  return (
    <div className="card">
      <h3>Identified Fix · {fix.title}</h3>
      <div className="row" style={{ marginBottom: 8 }}>
        <span className="pill">Target: {fix.target.platform} / {fix.target.artifact}</span>
        <span className="pill">Risk: {fix.risk}</span>
        <span className="pill">Category: {fix.category}</span>
        {fix.reused_from_memory && <span className="pill" style={{ color: '#7c5cff' }}>♺ reused from memory</span>}
      </div>
      <p className="muted">{fix.rationale}</p>
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
      <div className="chat-input" style={{ padding: 0, borderTop: 'none', marginTop: 10 }}>
        <input type="text" placeholder="Modify the fix (e.g. 'use MEDIUM warehouse', 'coalesce to 0')…"
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

function Collapsible({ title, badge, badgeColor, defaultOpen = false, children }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div style={{ marginTop: 14, border: '1px solid var(--border)', borderRadius: 8 }}>
      <div
        onClick={() => setOpen((v) => !v)}
        style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '8px 12px', cursor: 'pointer',
          background: 'var(--bg-secondary)', borderRadius: open ? '8px 8px 0 0' : 8,
        }}
      >
        <span style={{ fontWeight: 600, fontSize: 13 }}>
          {title}
          {badge && (
            <span style={{
              marginLeft: 8, fontSize: 11, padding: '2px 8px', borderRadius: 99,
              background: badgeColor || 'var(--border)', color: '#fff', fontWeight: 700,
            }}>{badge}</span>
          )}
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{open ? '▲ collapse' : '▼ expand'}</span>
      </div>
      {open && <div style={{ padding: '10px 14px' }}>{children}</div>}
    </div>
  )
}

const SEVERITY_COLOR = { Critical: '#e74c3c', High: '#e67e22', Medium: '#f1c40f', Low: '#2ecc71' }
const CONFIDENCE_COLOR = { High: '#2ecc71', Medium: '#f1c40f', Low: '#e74c3c' }

function RcaReport({ rca, selected, busy, onSuggestFix }) {
  const ia = rca.impact_assessment || {}
  const rem = rca.remediation || {}
  const sevColor = SEVERITY_COLOR[ia.business_severity] || 'var(--border)'
  const confColor = CONFIDENCE_COLOR[rca.confidence_level] || 'var(--border)'

  return (
    <div className="card">
      {/* ── Header row ─────────────────────────────────────────────────────── */}
      <h3>RCA · {selected?.name}</h3>
      <div className="row" style={{ flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
        <span className="pill">Incident: {rca.incident_id}</span>
        <span className="pill" style={{ background: 'var(--border)', fontWeight: 600 }}>
          {rca.failure_type || rca.category}
        </span>
        <span className="pill">Root cause: {rca.root_cause_name}</span>
        {rca.confidence_level && (
          <span className="pill" style={{ background: confColor, color: '#fff', fontWeight: 700 }}>
            {rca.confidence_level} confidence ({(rca.confidence * 100).toFixed(0)}%)
          </span>
        )}
        {ia.business_severity && (
          <span className="pill" style={{ background: sevColor, color: '#fff', fontWeight: 700 }}>
            {ia.business_severity} severity
          </span>
        )}
        {rca.seen_before && <span className="pill" style={{ color: '#7c5cff' }}>♺ seen before</span>}
      </div>

      {/* ── Summary ──────────────────────────────────────────────────────────── */}
      <p style={{ marginBottom: 6 }}>{rca.summary}</p>
      {rca.detailed_analysis && rca.detailed_analysis !== rca.summary && (
        <p className="muted" style={{ fontSize: 13, marginBottom: 10 }}>{rca.detailed_analysis}</p>
      )}

      {/* ── Evidence (Metadata First) ─────────────────────────────────────── */}
      <strong>Evidence</strong>
      <ul className="evidence">{(rca.evidence || []).map((e, i) => <li key={i}>{e}</li>)}</ul>

      {/* ── Impact Assessment ──────────────────────────────────────────────── */}
      {(ia.impacted_tables?.length > 0 || ia.impacted_pipelines?.length > 0) && (
        <Collapsible
          title="Impact Assessment"
          badge={ia.business_severity}
          badgeColor={sevColor}
          defaultOpen
        >
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px 16px' }}>
            <div>
              <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 4 }}>Impacted Tables ({ia.impacted_tables?.length || 0})</div>
              {(ia.impacted_tables || []).length === 0
                ? <span className="muted">None identified</span>
                : <ul style={{ margin: 0, paddingLeft: 18 }}>{(ia.impacted_tables || []).map((t, i) => <li key={i} style={{ fontSize: 12 }}>{t}</li>)}</ul>
              }
            </div>
            <div>
              <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 4 }}>Impacted Pipelines ({ia.impacted_pipelines?.length || 0})</div>
              {(ia.impacted_pipelines || []).length === 0
                ? <span className="muted">None identified</span>
                : <ul style={{ margin: 0, paddingLeft: 18 }}>{(ia.impacted_pipelines || []).map((p, i) => <li key={i} style={{ fontSize: 12 }}>{p}</li>)}</ul>
              }
            </div>
            {ia.impacted_reports?.length > 0 && (
              <div style={{ gridColumn: '1 / -1' }}>
                <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 4 }}>Impacted Reports ({ia.impacted_reports.length})</div>
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {ia.impacted_reports.map((r, i) => <li key={i} style={{ fontSize: 12 }}>{r}</li>)}
                </ul>
              </div>
            )}
          </div>
          <p className="muted" style={{ fontSize: 11, marginTop: 6 }}>
            {rca.impacted_nodes?.length || 0} downstream task(s)
            {rca.downstream_consumers?.views > 0 && ` · ${rca.downstream_consumers.views} view(s)`}
            {rca.downstream_consumers?.procedures > 0 && ` · ${rca.downstream_consumers.procedures} procedure(s)`}
          </p>
        </Collapsible>
      )}

      {/* ── Remediation ────────────────────────────────────────────────────── */}
      {(rem.immediate_fix || rem.permanent_fix) && (
        <Collapsible title="Remediation" defaultOpen>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {rem.immediate_fix && (
              <div>
                <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 3, color: '#e74c3c' }}>Immediate Fix</div>
                <p style={{ fontSize: 13, margin: 0 }}>{rem.immediate_fix}</p>
              </div>
            )}
            {rem.permanent_fix && (
              <div>
                <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 3, color: '#2ecc71' }}>Permanent Fix</div>
                <p style={{ fontSize: 13, margin: 0 }}>{rem.permanent_fix}</p>
              </div>
            )}
            {rem.monitoring_recommendation && (
              <div>
                <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 3, color: '#3498db' }}>Monitoring Recommendation</div>
                <p style={{ fontSize: 13, margin: 0 }}>{rem.monitoring_recommendation}</p>
              </div>
            )}
          </div>
        </Collapsible>
      )}

      {/* ── Upstream / Downstream lineage ──────────────────────────────────── */}
      <Collapsible title="Upstream Lineage" defaultOpen={false}>
        {rca.upstream_lineage_text
          ? <pre style={{ fontSize: 12, lineHeight: 1.8, margin: 0, whiteSpace: 'pre-wrap' }}>{rca.upstream_lineage_text}</pre>
          : <span className="muted">No upstream lineage resolved</span>
        }
        {rca.upstream_lineage?.nodes?.length > 0 && (
          <div style={{ marginTop: 10 }}>
            <TableLineageGraph tableLineage={rca.upstream_lineage} direction="upstream" hideTitle />
          </div>
        )}
      </Collapsible>

      <Collapsible title="Downstream Lineage" defaultOpen={false}>
        {rca.downstream_lineage_text
          ? <pre style={{ fontSize: 12, lineHeight: 1.8, margin: 0, whiteSpace: 'pre-wrap' }}>{rca.downstream_lineage_text}</pre>
          : <span className="muted">No downstream lineage resolved</span>
        }
        {rca.downstream_lineage?.nodes?.length > 0 && (
          <div style={{ marginTop: 10 }}>
            <TableLineageGraph tableLineage={rca.downstream_lineage} direction="downstream" hideTitle />
          </div>
        )}
      </Collapsible>

      {/* ── Full RCA report ────────────────────────────────────────────────── */}
      {rca.rca_report && (
        <Collapsible title="Full RCA Report" defaultOpen={false}>
          <pre style={{
            fontSize: 11, lineHeight: 1.7, margin: 0, whiteSpace: 'pre-wrap',
            fontFamily: 'monospace', background: 'var(--code-bg)', padding: 12, borderRadius: 6,
          }}>{rca.rca_report}</pre>
        </Collapsible>
      )}

      {/* ── Detailed lineage table ─────────────────────────────────────────── */}
      <Collapsible title="Lineage Detail Table" defaultOpen={false}>
        <LineageTable rows={rca.lineage_table} affectedTables={rca.affected_tables} />
        {rca.related_dq_failures?.length > 0 && (
          <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
            Related DQ failures: {rca.related_dq_failures.map((d) => d.name).join(', ')}
          </p>
        )}
      </Collapsible>

      <div style={{ marginTop: 16 }}>
        <button className="btn purple" disabled={busy === 'fix' || selected?.id?.startsWith('dq_')}
          onClick={onSuggestFix}>
          Identify &amp; Suggest Fix
        </button>
      </div>
    </div>
  )
}

export default function Workbench({ activePipeline, onSelect, onReportError, onBackToDashboard }) {
  const [selected, setSelected] = useState(activePipeline)
  const [rca, setRca] = useState(null)
  const [fix, setFix] = useState(null)
  const [test, setTest] = useState(null)
  const [busy, setBusy] = useState('')
  const [hasActioned, setHasActioned] = useState(!!activePipeline)

  useEffect(() => { if (activePipeline) { setSelected(activePipeline); setHasActioned(true); runRCA(activePipeline.id) } }, [activePipeline])

  const runRCA = (id) => {
    setBusy('rca'); setRca(null); setFix(null); setTest(null)
    api.rca(id)
      .then((r) => { if (r.error) throw new Error(r.error); setRca(r) })
      .catch((e) => reportApiError(onReportError, 'run RCA', e))
      .finally(() => setBusy(''))
  }
  const suggestFix = () => {
    setBusy('fix')
    api.fix(selected.id)
      .then((r) => { if (r.error) throw new Error(r.error); setFix(r) })
      .catch((e) => reportApiError(onReportError, 'suggest fix', e))
      .finally(() => setBusy(''))
  }
  const editFix = (text) => {
    setBusy('fix')
    api.fix(selected.id, text)
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
        />
      )}

      {busy === 'fix' && <div className="card spinner">Fix agent working…</div>}
      {fix && <FixDiff fix={fix} onEdit={editFix} onValidate={validate} busy={busy === 'test'} />}
      {busy === 'test' && <div className="card spinner">Validation agent: cloning &amp; testing on pre-prod…</div>}
      {test && <TestSummary test={test} />}
    </div>
  )
}
