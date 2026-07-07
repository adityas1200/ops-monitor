import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import { KpiSkeleton, LoadingOverlay, TableSkeleton } from './LoadingIndicator'

const KPI_DEFS = [
  ['success', 'Passed'], ['failed', 'Failed'], ['delayed', 'Warning'],
  ['skipped', 'Skipped'], ['total', 'Total'],
]

const TASK_KPI_LABELS = {
  success: 'Successful', failed: 'Failed', delayed: 'Delayed', skipped: 'Skipped', total: 'Total',
}

function twoDaysAgoISO() {
  const d = new Date(Date.now() - 2 * 24 * 3600 * 1000)
  return d.toISOString().slice(0, 10)
}

const STATUS_ALIASES = {
  SUCCESS: ['SUCCESS', 'SUCCEEDED', 'PASS', 'PASSED', 'OK'],
  FAILED: ['FAILED', 'FAIL', 'TIMEOUT', 'FAILED_AND_AUTO_SUSPENDED', 'ERROR'],
  SKIPPED: ['SKIPPED', 'SKIP', 'CANCELLED'],
  RUNNING: ['RUNNING', 'EXECUTING', 'SCHEDULED', 'PENDING'],
  DELAYED: ['DELAYED', 'WARN', 'WARNING'],
}

const STATUS_PRIORITY = { FAILED: 0, DELAYED: 1, RUNNING: 2, SKIPPED: 3, SUCCESS: 4 }

function getStatusPriority(s) {
  const upper = (s || '').toUpperCase()
  for (const [group, aliases] of Object.entries(STATUS_ALIASES)) {
    if (aliases.includes(upper)) return STATUS_PRIORITY[group] ?? 5
  }
  return 5
}

function sortRows(rows, sortCol, sortDir) {
  if (!sortCol) return rows
  const dir = sortDir === 'asc' ? 1 : -1
  return [...rows].sort((a, b) => {
    let av, bv
    if (sortCol === 'status') {
      av = getStatusPriority(a.status)
      bv = getStatusPriority(b.status)
    } else if (sortCol === 'duration_s') {
      av = a.duration_s ?? -1
      bv = b.duration_s ?? -1
    } else if (sortCol === 'run_at') {
      av = a.run_at || ''
      bv = b.run_at || ''
    } else {
      av = (a[sortCol] || '').toLowerCase()
      bv = (b[sortCol] || '').toLowerCase()
    }
    if (av < bv) return -1 * dir
    if (av > bv) return 1 * dir
    return 0
  })
}

function SortableHeader({ label, colKey, sortCol, sortDir, onSort }) {
  const active = sortCol === colKey
  return (
    <th onClick={() => onSort(colKey)} style={{ cursor: 'pointer', userSelect: 'none' }}>
      {label} {active ? (sortDir === 'asc' ? '▲' : '▼') : '⇅'}
    </th>
  )
}

function matchesStatus(row, filter) {
  if (filter === 'ALL') return true
  const s = (row.status || '').toUpperCase()
  const aliases = STATUS_ALIASES[filter] || [filter]
  return aliases.includes(s)
}

function KpiBar({ kpis, labels }) {
  return (
    <div className="kpis">
      {KPI_DEFS.map(([k, defaultLabel]) => (
        <div key={k} className={`kpi ${k}`}>
          <div className="v">{kpis[k] ?? '—'}</div>
          <div className="l">{labels?.[k] || defaultLabel}</div>
        </div>
      ))}
    </div>
  )
}

function TasksView({ data, status, nameFilter, loading, configured, onRunRCA, onSelect }) {
  const allPipelines = data?.all_pipelines || data?.pipelines || []
  const [sortCol, setSortCol] = useState('status')
  const [sortDir, setSortDir] = useState('asc')

  const handleSort = useCallback((col) => {
    setSortCol((prev) => {
      if (prev === col) {
        setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
      } else {
        setSortDir('asc')
      }
      return col
    })
  }, [])

  const rows = useMemo(() => {
    const needle = (nameFilter || '').toLowerCase()
    const filtered = allPipelines.filter((p) =>
      matchesStatus(p, status) && (!needle || (p.name || '').toLowerCase().includes(needle))
    )
    return sortRows(filtered, sortCol, sortDir)
  }, [allPipelines, status, nameFilter, sortCol, sortDir])
  const showSkeleton = loading && !data

  return (
    <LoadingOverlay active={loading} label="Loading task monitoring data">
      {showSkeleton ? <KpiSkeleton /> : <KpiBar kpis={data?.kpis || {}} labels={TASK_KPI_LABELS} />}
      {showSkeleton ? (
        <TableSkeleton rows={8} cols={6} />
      ) : (
        <table>
        <thead>
          <tr>
            <SortableHeader label="Pipeline / Task" colKey="name" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
            <SortableHeader label="Platform" colKey="platform" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
            <SortableHeader label="Status" colKey="status" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
            <SortableHeader label="Duration" colKey="duration_s" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
            <th>Error</th><th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.id} onClick={() => onSelect?.(p)}>
              <td>{p.name}</td>
              <td><span className="pill">{p.platform}</span></td>
              <td><span className={`badge ${p.status}`}>{p.status}</span></td>
              <td>{p.duration_s != null ? `${p.duration_s}s` : '—'}</td>
              <td className="muted" style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {p.error || '—'}
              </td>
              <td>
                {(p.status === 'FAILED' || p.status === 'DELAYED') && (
                  <button className="btn" onClick={(e) => { e.stopPropagation(); onRunRCA?.(p) }}>Run RCA</button>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && !loading && (
            <tr>
              <td colSpan={6} className="muted">
                {configured
                  ? status === 'ALL'
                    ? 'No tasks in the selected date range.'
                    : `No tasks with status ${status} (${allPipelines.length} total in date range).`
                  : 'Configure Snowflake or AWS in Settings to load task data.'}
              </td>
            </tr>
          )}
        </tbody>
        </table>
      )}
    </LoadingOverlay>
  )
}

function DQDetailPopup({ qcId, subjectArea, cached, onLoaded, onClose }) {
  const [details, setDetails] = useState(cached || null)
  const [loading, setLoading] = useState(!cached)

  useEffect(() => {
    if (cached) return
    api.dqDetails(qcId, subjectArea)
      .then((d) => { setDetails(d); onLoaded?.(qcId, subjectArea, d) })
      .catch((e) => setDetails({ error: e.message }))
      .finally(() => setLoading(false))
  }, [qcId, subjectArea, cached])

  const rule = details?.rule
  const sqlResults = details?.sql_results

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 100, display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'rgba(0,0,0,.5)', backdropFilter: 'blur(2px)',
    }} onClick={onClose}>
      <div className="card" style={{ width: '90vw', maxWidth: 1000, maxHeight: '90vh', overflow: 'auto', margin: 20 }}
        onClick={(e) => e.stopPropagation()}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, position: 'sticky', top: 0, background: 'var(--panel)', paddingBottom: 8 }}>
          <h3 style={{ margin: 0 }}>DQ Check Details — QC {qcId}</h3>
          <button className="btn sec" onClick={onClose} style={{ padding: '4px 10px' }}>✕</button>
        </div>
        {loading && <p className="spinner">Loading rule details and executing SQL...</p>}
        {details?.error && <p style={{ color: 'var(--red)' }}>Error: {details.error}</p>}
        {details && !details.error && !details.found && (
          <p className="muted">No rule definition found for QC ID "{qcId}" (subject area: {subjectArea || 'any'}) in the configured rules table.</p>
        )}
        {rule && (
          <div>
            {/* Rule metadata */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px 16px', marginBottom: 14, fontSize: 12 }}>
              {rule.QC_DESCRIPTION && <div style={{ gridColumn: '1 / -1' }}><strong>Description:</strong> {rule.QC_DESCRIPTION}</div>}
              {rule.CHECK_TYPE && <div><strong>Check Type:</strong> {rule.CHECK_TYPE}</div>}
              {rule.SOURCE_TABLE && <div><strong>Source Table:</strong> {rule.SOURCE_TABLE}</div>}
              {rule.FREQUENCY && <div><strong>Frequency:</strong> {rule.FREQUENCY}</div>}
              {rule.DATA_VENDOR && <div><strong>Data Vendor:</strong> {rule.DATA_VENDOR}</div>}
              {rule.LOWER_THRESHOLD && <div><strong>Lower Threshold:</strong> {rule.LOWER_THRESHOLD}</div>}
              {rule.UPPER_THRESHOLD && <div><strong>Upper Threshold:</strong> {rule.UPPER_THRESHOLD}</div>}
            </div>

            {/* SQL Code */}
            {rule.SQL_CODE && (
              <div style={{ marginBottom: 14 }}>
                <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 4 }}>SQL Code</div>
                <pre style={{
                  margin: 0, whiteSpace: 'pre-wrap', fontSize: 11, lineHeight: 1.5,
                  background: 'var(--code-bg)', padding: 10, borderRadius: 6, maxHeight: 220, overflow: 'auto',
                }}>{rule.SQL_CODE}</pre>
              </div>
            )}

            {/* SQL Execution Results */}
            {sqlResults && (
              <div>
                <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 6 }}>
                  SQL Results
                  {sqlResults.executed && <span className="muted" style={{ fontWeight: 400, marginLeft: 8 }}>({sqlResults.row_count} row{sqlResults.row_count !== 1 ? 's' : ''})</span>}
                  {cached && <span className="muted" style={{ fontWeight: 400, marginLeft: 8 }}>(cached)</span>}
                </div>
                {!sqlResults.executed && (
                  <p style={{ color: 'var(--red)', fontSize: 12 }}>SQL execution failed: {sqlResults.error}</p>
                )}
                {sqlResults.executed && sqlResults.rows?.length > 0 && (
                  <div style={{ overflow: 'auto' }}>
                    <table style={{ fontSize: 11 }}>
                      <thead>
                        <tr>
                          {sqlResults.columns.map((col) => <th key={col}>{col}</th>)}
                        </tr>
                      </thead>
                      <tbody>
                        {sqlResults.rows.map((row, i) => (
                          <tr key={i}>
                            {sqlResults.columns.map((col) => <td key={col}>{row[col] ?? '—'}</td>)}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {sqlResults.executed && sqlResults.rows?.length === 0 && (
                  <p className="muted" style={{ fontSize: 12 }}>Query returned 0 rows — check passed.</p>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

const dqDetailsCache = {}

function DQView({ data, status, nameFilter, loading, configured, onSelect, onRunRCA }) {
  const allChecks = data?.all_checks || []
  const [sortCol, setSortCol] = useState('status')
  const [sortDir, setSortDir] = useState('asc')
  const [detailCheck, setDetailCheck] = useState(null)

  const getCached = (qcId, subjectArea) => {
    const key = `${qcId}__${subjectArea || ''}`
    return dqDetailsCache[key] || null
  }
  const onDetailLoaded = (qcId, subjectArea, result) => {
    const key = `${qcId}__${subjectArea || ''}`
    dqDetailsCache[key] = result
  }

  const handleSort = useCallback((col) => {
    setSortCol((prev) => {
      if (prev === col) {
        setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
      } else {
        setSortDir('asc')
      }
      return col
    })
  }, [])

  const rows = useMemo(() => {
    const needle = (nameFilter || '').toLowerCase()
    const filtered = allChecks.filter((c) =>
      matchesStatus(c, status) && (!needle || (c.name || '').toLowerCase().includes(needle))
    )
    return sortRows(filtered, sortCol, sortDir)
  }, [allChecks, status, nameFilter, sortCol, sortDir])
  const showSkeleton = loading && !data

  return (
    <LoadingOverlay active={loading} label="Loading data quality results">
      {data?.table && !showSkeleton && (
        <p className="muted" style={{ margin: '0 0 12px', fontSize: 12 }}>
          Source: <code>{data.table}</code>
          {data.subject_area ? <> · Subject area: <strong>{data.subject_area}</strong></> : null}
        </p>
      )}
      {showSkeleton ? <KpiSkeleton /> : <KpiBar kpis={data?.kpis || {}} />}
      {showSkeleton ? (
        <TableSkeleton rows={8} cols={6} />
      ) : (
        <table>
        <thead>
          <tr>
            <SortableHeader label="QC ID" colKey="name" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
            <SortableHeader label="Subject Area" colKey="table_name" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
            <SortableHeader label="Check Type" colKey="column_name" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
            <SortableHeader label="Status" colKey="status" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
            <SortableHeader label="Run at" colKey="run_at" sortCol={sortCol} sortDir={sortDir} onSort={handleSort} />
            <th>Details</th><th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.id} onClick={() => onSelect?.(c)}>
              <td>{c.name}</td>
              <td>{c.table_name || '—'}</td>
              <td>{c.column_name || '—'}</td>
              <td><span className={`badge ${c.status}`}>{c.status}</span></td>
              <td>{c.run_at ? String(c.run_at).replace('T', ' ').slice(0, 19) : '—'}</td>
              <td className="muted" style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {c.error || '—'}
              </td>
              <td style={{ whiteSpace: 'nowrap' }}>
                <button className="btn sec" style={{ marginRight: 6, padding: '5px 10px', fontSize: 11 }}
                  onClick={(e) => { e.stopPropagation(); setDetailCheck({ qcId: c.name, subjectArea: c.table_name }) }}>View Details</button>
                {(c.status === 'FAILED' || c.status === 'DELAYED') && (
                  <button className="btn" onClick={(e) => { e.stopPropagation(); onRunRCA?.(c) }}>Run RCA</button>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && !loading && (
            <tr>
              <td colSpan={7} className="muted">
                {!configured
                  ? 'Configure Snowflake in Settings to load DQ results.'
                  : data?.errors?.length
                    ? `Could not load DQ data: ${data.errors[0].error}`
                    : status === 'ALL'
                      ? 'No DQ checks in the selected date range.'
                      : `No DQ checks with status ${status} (${allChecks.length} total in date range).`}
              </td>
            </tr>
          )}
        </tbody>
        </table>
      )}
      {detailCheck && (
        <DQDetailPopup
          qcId={detailCheck.qcId}
          subjectArea={detailCheck.subjectArea}
          cached={getCached(detailCheck.qcId, detailCheck.subjectArea)}
          onLoaded={onDetailLoaded}
          onClose={() => setDetailCheck(null)}
        />
      )}
    </LoadingOverlay>
  )
}

export default function Dashboard({ onRunRCA, onSelect, onReportError, platforms, onChatContextChange }) {
  const [subTab, setSubTab] = useState('dq')
  const [dateFrom, setDateFrom] = useState(twoDaysAgoISO())
  const [dateTo, setDateTo] = useState(new Date().toISOString().slice(0, 10))
  const [taskStatus, setTaskStatus] = useState('ALL')
  const [dqStatus, setDqStatus] = useState('ALL')
  const [nameFilter, setNameFilter] = useState('')

  const [taskData, setTaskData] = useState(null)
  const [dqData, setDqData] = useState(null)
  const [taskLoadError, setTaskLoadError] = useState(null)
  const [dqLoadError, setDqLoadError] = useState(null)
  const [taskLoading, setTaskLoading] = useState(false)
  const [dqLoading, setDqLoading] = useState(false)

  const configured = Object.values(platforms || {}).some(Boolean)
  const dateFromIso = dateFrom ? dateFrom + 'T00:00:00+00:00' : undefined
  const dateToIso = dateTo ? dateTo + 'T23:59:59+00:00' : undefined
  const status = subTab === 'tasks' ? taskStatus : dqStatus
  const busy = taskLoading || dqLoading

  const loadTasks = useCallback(() => {
    setTaskLoading(true)
    setTaskLoadError(null)
    return api.summary({ status: 'ALL', date_from: dateFromIso, date_to: dateToIso })
      .then((d) => {
        if (!d.live_only) {
          setTaskData({ ...d, all_pipelines: [], kpis: { success: 0, failed: 0, delayed: 0, skipped: 0, running: 0, total: 0 } })
          setTaskLoadError('Stale backend — restart the server on port 8001.')
          onReportError?.({ tab: 'Dashboard', action: 'load task data', error: 'Stale backend — restart on port 8001.' })
          return
        }
        setTaskData(d)
        ;(d.errors || []).forEach((err) => onReportError?.({
          tab: 'Dashboard', action: 'load task data', error: `${err.platform}: ${err.error}`,
        }))
      })
      .catch((e) => {
        setTaskLoadError(e.message || String(e))
        onReportError?.({ tab: 'Dashboard', action: 'load task data', error: e.message })
      })
      .finally(() => setTaskLoading(false))
  }, [dateFromIso, dateToIso])

  const loadDq = useCallback(() => {
    setDqLoading(true)
    setDqLoadError(null)
    return api.dqSummary({ date_from: dateFromIso, date_to: dateToIso })
      .then((d) => {
        setDqData(d)
        ;(d.errors || []).forEach((err) => onReportError?.({
          tab: 'Dashboard', action: 'load DQ data', error: `${err.platform}: ${err.error}`,
        }))
      })
      .catch((e) => {
        setDqLoadError(e.message || String(e))
        onReportError?.({ tab: 'Dashboard', action: 'load DQ data', error: e.message })
      })
      .finally(() => setDqLoading(false))
  }, [dateFromIso, dateToIso])

  const refresh = useCallback(() => {
    loadTasks()
    loadDq()
  }, [loadTasks, loadDq])

  // Initial load + reload only when dates change (not on tab or status change).
  useEffect(() => {
    loadTasks()
    loadDq()
  }, [loadTasks, loadDq])

  useEffect(() => {
    if (!onChatContextChange) return
    const failedTasks = (taskData?.all_pipelines || [])
      .filter((p) => p.status === 'FAILED' || p.status === 'DELAYED')
      .slice(0, 15)
      .map((p) => ({
        id: p.id,
        name: p.name,
        platform: p.platform,
        status: p.status,
        error: p.error,
        duration_s: p.duration_s,
      }))
    const failedDqChecks = (dqData?.all_checks || [])
      .filter((c) => c.status === 'FAILED')
      .slice(0, 15)
      .map((c) => ({
        id: c.id,
        name: c.name,
        status: c.status,
        table_name: c.table_name,
        column_name: c.column_name,
        error: c.error,
        run_at: c.run_at,
      }))
    onChatContextChange({
      view: subTab,
      dateFrom,
      dateTo,
      taskKpis: taskData?.kpis || null,
      dqKpis: dqData?.kpis || null,
      taskStatusFilter: taskStatus,
      dqStatusFilter: dqStatus,
      failedTasks,
      failedDqChecks,
      dqSource: dqData ? { table: dqData.table, subject_area: dqData.subject_area } : null,
      taskLoading,
      dqLoading,
    })
  }, [subTab, taskData, dqData, dateFrom, dateTo, taskStatus, dqStatus, taskLoading, dqLoading, onChatContextChange])

  return (
    <div>
      <div className="subtabs">
        <button className={`subtab ${subTab === 'dq' ? 'active' : ''}`} onClick={() => setSubTab('dq')}>
          DQ Status
        </button>
        <button className={`subtab ${subTab === 'tasks' ? 'active' : ''}`} onClick={() => setSubTab('tasks')}>
          Tasks
        </button>
      </div>

      {!configured && (
        <div className="card" style={{ borderColor: '#ff5c6c', marginTop: 12 }}>
          <p className="muted" style={{ margin: 0 }}>
            No data platforms configured. Open <strong>Settings</strong> and add Snowflake credentials.
          </p>
        </div>
      )}

      <div className={`toolbar${busy ? ' is-busy' : ''}`}>
        <label>Search</label>
        <input
          type="text"
          placeholder="Filter by name…"
          value={nameFilter}
          onChange={(e) => setNameFilter(e.target.value)}
          style={{ minWidth: 160 }}
        />
        <label>Status</label>
        <select
          value={status}
          disabled={busy}
          onChange={(e) => (subTab === 'tasks' ? setTaskStatus : setDqStatus)(e.target.value)}
        >
          {(subTab === 'tasks'
            ? ['FAILED', 'DELAYED', 'SUCCESS', 'SKIPPED', 'RUNNING', 'ALL']
            : ['FAILED', 'SUCCESS', 'DELAYED', 'SKIPPED', 'RUNNING', 'ALL']
          ).map((s) => <option key={s}>{s}</option>)}
        </select>
        <label>From</label>
        <input type="date" value={dateFrom} disabled={busy} onChange={(e) => setDateFrom(e.target.value)} />
        <label>To</label>
        <input type="date" value={dateTo} disabled={busy} onChange={(e) => setDateTo(e.target.value)} />
        <button className={`btn sec${busy ? ' is-loading' : ''}`} onClick={refresh} disabled={busy}>
          Refresh
        </button>
      </div>

      <div hidden={subTab !== 'tasks'}>
        {taskLoadError && (
          <div className="card" style={{ borderColor: '#ff5c6c', marginBottom: 12 }}>
            <p className="muted" style={{ margin: 0 }}>Task data failed to load: {taskLoadError}</p>
          </div>
        )}
        <TasksView
          data={taskData}
          status={taskStatus}
          nameFilter={nameFilter}
          loading={taskLoading}
          configured={configured}
          onRunRCA={onRunRCA}
          onSelect={onSelect}
        />
      </div>
      <div hidden={subTab !== 'dq'}>
        {dqLoadError && (
          <div className="card" style={{ borderColor: '#ff5c6c', marginBottom: 12 }}>
            <p className="muted" style={{ margin: 0 }}>DQ data failed to load: {dqLoadError}</p>
          </div>
        )}
        <DQView
          nameFilter={nameFilter}
          data={dqData}
          status={dqStatus}
          loading={dqLoading}
          configured={configured}
          onSelect={onSelect}
          onRunRCA={onRunRCA}
        />
      </div>
    </div>
  )
}
