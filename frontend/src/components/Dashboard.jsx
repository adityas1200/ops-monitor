import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import ChatFab from './ChatFab'
import { KpiSkeleton, LoadingOverlay, TableSkeleton } from './LoadingIndicator'

const DQ_KPI_DEFS = [
  ['success', 'Passed'], ['failed', 'Failed'], ['warning', 'Warning'],
  ['skipped', 'Skipped'], ['running', 'Running'], ['total', 'Total'],
]

const TASK_KPI_DEFS = [
  ['success', 'Successful'], ['failed', 'Failed'], ['delayed', 'Delayed'],
  ['skipped', 'Skipped'], ['running', 'Running'], ['total', 'Total'],
]

/** Default lookback: past 3 days including today. */
function defaultDateFromISO() {
  const d = new Date(Date.now() - 3 * 24 * 3600 * 1000)
  return d.toISOString().slice(0, 10)
}

const STATUS_ALIASES = {
  SUCCESS: ['SUCCESS', 'SUCCEEDED', 'PASS', 'PASSED', 'OK'],
  FAILED: ['FAILED', 'FAIL', 'TIMEOUT', 'FAILED_AND_AUTO_SUSPENDED', 'ERROR'],
  SKIPPED: ['SKIPPED', 'SKIP', 'CANCELLED'],
  RUNNING: ['RUNNING', 'EXECUTING', 'SCHEDULED', 'PENDING'],
  DELAYED: ['DELAYED'],
  WARNING: ['WARNING', 'WARN'],
}

const STATUS_PRIORITY = { FAILED: 0, WARNING: 1, DELAYED: 1, RUNNING: 2, SKIPPED: 3, SUCCESS: 4 }

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

/** Display form for run_at, e.g. "2026-07-19 23:31:46". */
function formatRunAt(runAt) {
  if (!runAt) return ''
  return String(runAt).replace('T', ' ').slice(0, 19)
}

function matchesDqTextFilter(check, nameFilter) {
  const needle = (nameFilter || '').trim().toLowerCase()
  if (!needle) return true
  // A purely numeric query (e.g. "1", "17") is a QC ID search. Match the QC ID exactly so
  // "3" returns only QC 3 (not 23/31), and date-filled run-at timestamps don't pollute it.
  if (/^\d+$/.test(needle)) {
    return String(check.name ?? '').trim().toLowerCase() === needle
  }
  const haystack = [
    check.name,
    check.table_name,
    check.column_name,
  ]
    .filter(Boolean)
    .map((v) => String(v).toLowerCase())
  return haystack.some((v) => v.includes(needle))
}

function matchesTaskTextFilter(pipeline, nameFilter) {
  const needle = (nameFilter || '').trim().toLowerCase()
  if (!needle) return true
  const haystack = [pipeline.name, pipeline.platform]
    .filter(Boolean)
    .map((v) => String(v).toLowerCase())
  return haystack.some((v) => v.includes(needle))
}

function KpiBar({ kpis, defs }) {
  const items = defs || DQ_KPI_DEFS
  return (
    <div className="kpis">
      {items.map(([k, label]) => (
        <div key={k} className={`kpi ${k}`}>
          <div className="v">{kpis[k] ?? '—'}</div>
          <div className="l">{label}</div>
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
    const filtered = allPipelines.filter((p) =>
      matchesStatus(p, status) && matchesTaskTextFilter(p, nameFilter)
    )
    return sortRows(filtered, sortCol, sortDir)
  }, [allPipelines, status, nameFilter, sortCol, sortDir])
  const showSkeleton = loading && !data

  return (
    <LoadingOverlay active={loading} label="Loading task monitoring data">
      {showSkeleton ? <KpiSkeleton /> : <KpiBar kpis={data?.kpis || {}} defs={TASK_KPI_DEFS} />}
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

function DQDetailPopup({ qcId, subjectArea, onClose }) {
  const [details, setDetails] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    // Always re-execute the rule live so the detail panel reflects current data.
    setLoading(true)
    api.dqDetails(qcId, subjectArea)
      .then((d) => setDetails(d))
      .catch((e) => setDetails({ error: e.message }))
      .finally(() => setLoading(false))
  }, [qcId, subjectArea])

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

function DQView({ data, status, nameFilter, loading, configured, onSelect, onRunRCA }) {
  const allChecks = data?.all_checks || []
  const [sortCol, setSortCol] = useState('status')
  const [sortDir, setSortDir] = useState('asc')
  const [detailCheck, setDetailCheck] = useState(null)

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
    const filtered = allChecks.filter((c) =>
      matchesStatus(c, status) && matchesDqTextFilter(c, nameFilter)
    )
    return sortRows(filtered, sortCol, sortDir)
  }, [allChecks, status, nameFilter, sortCol, sortDir])
  const showSkeleton = loading && !data

  return (
    <LoadingOverlay active={loading} label="Loading data quality results">
      {data?.table && !showSkeleton && (
        <p className="muted" style={{ margin: '0 0 12px', fontSize: 12 }}>
          Source: <code>{data.table}</code>
        </p>
      )}
      {showSkeleton ? <KpiSkeleton /> : <KpiBar kpis={data?.kpis || {}} defs={DQ_KPI_DEFS} />}
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
              <td>{c.run_at ? formatRunAt(c.run_at) : '—'}</td>
              <td className="muted" style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {c.error || '—'}
              </td>
              <td style={{ whiteSpace: 'nowrap' }}>
                <button className="btn sec" style={{ marginRight: 6, padding: '5px 10px', fontSize: 11 }}
                  onClick={(e) => { e.stopPropagation(); setDetailCheck({ qcId: c.name, subjectArea: c.table_name }) }}>View Details</button>
                {(c.status === 'FAILED' || c.status === 'WARNING' || c.status === 'DELAYED') && (
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
          onClose={() => setDetailCheck(null)}
        />
      )}
    </LoadingOverlay>
  )
}

export default function Dashboard({
  onRunRCA, onSelect, onReportError, platforms, onChatContextChange,
  chatOpen = false, onOpenChat,
}) {
  const [subTab, setSubTab] = useState('dq')
  const [dateFrom, setDateFrom] = useState(defaultDateFromISO())
  const [dateTo, setDateTo] = useState(new Date().toISOString().slice(0, 10))
  const [taskStatus, setTaskStatus] = useState('ALL')
  const [dqStatus, setDqStatus] = useState('ALL')
  const [nameFilter, setNameFilter] = useState('')
  const [debouncedNameFilter, setDebouncedNameFilter] = useState('')

  const [taskData, setTaskData] = useState(null)
  const [dqData, setDqData] = useState(null)
  const [taskLoadError, setTaskLoadError] = useState(null)
  const [dqLoadError, setDqLoadError] = useState(null)
  const [taskLoading, setTaskLoading] = useState(false)
  const [dqLoading, setDqLoading] = useState(false)
  const [sfReady, setSfReady] = useState(false)

  // Keep callbacks in refs so loaders stay stable (avoids re-fetch when parent recreates them).
  const onReportErrorRef = useRef(onReportError)
  onReportErrorRef.current = onReportError
  const taskInflightRef = useRef(null)
  const dqInflightRef = useRef(null)

  const configured = Object.values(platforms || {}).some(Boolean)
  const dateFromIso = dateFrom ? dateFrom + 'T00:00:00+00:00' : undefined
  const dateToIso = dateTo ? dateTo + 'T23:59:59+00:00' : undefined
  const status = subTab === 'tasks' ? taskStatus : dqStatus
  const busy = taskLoading || dqLoading

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedNameFilter(nameFilter), 300)
    return () => clearTimeout(timer)
  }, [nameFilter])

  // Wait for shared Snowflake session before first summary/DQ fan-out.
  // ensureSnowflakeSession dedupes App/Dashboard/StrictMode and uses a
  // short-lived sessionStorage hint so remounts do not re-block on SSO.
  useEffect(() => {
    let cancelled = false
    api.snowflakeWarmup()
      .then(() => { if (!cancelled) setSfReady(true) })
      .catch(() => { if (!cancelled) setSfReady(true) })
    return () => { cancelled = true }
  }, [])

  const loadTasks = useCallback(() => {
    const key = `${dateFromIso || ''}|${dateToIso || ''}`
    if (taskInflightRef.current?.key === key) {
      return taskInflightRef.current.promise
    }
    setTaskLoading(true)
    setTaskLoadError(null)
    const promise = api.summary({ status: 'ALL', date_from: dateFromIso, date_to: dateToIso })
      .then((d) => {
        if (!d.live_only) {
          setTaskData({ ...d, all_pipelines: [], kpis: { success: 0, failed: 0, delayed: 0, skipped: 0, running: 0, total: 0 } })
          setTaskLoadError('Stale backend — restart the server on port 8001.')
          onReportErrorRef.current?.({ tab: 'Dashboard', action: 'load task data', error: 'Stale backend — restart on port 8001.' })
          return
        }
        setTaskData(d)
        ;(d.errors || []).forEach((err) => onReportErrorRef.current?.({
          tab: 'Dashboard', action: 'load task data', error: `${err.platform}: ${err.error}`,
        }))
      })
      .catch((e) => {
        setTaskLoadError(e.message || String(e))
        onReportErrorRef.current?.({ tab: 'Dashboard', action: 'load task data', error: e.message })
      })
      .finally(() => {
        if (taskInflightRef.current?.key === key) taskInflightRef.current = null
        setTaskLoading(false)
      })
    taskInflightRef.current = { key, promise }
    return promise
  }, [dateFromIso, dateToIso])

  const loadDq = useCallback((opts = {}) => {
    // Default / date change / refresh: revalidate=false (backend may return cache).
    // Pass revalidate:true only from Revalidate live.
    const revalidate = opts.revalidate === true
    const key = `${dateFromIso || ''}|${dateToIso || ''}|rv:${revalidate ? 1 : 0}`
    if (dqInflightRef.current?.key === key) {
      return dqInflightRef.current.promise
    }
    setDqLoading(true)
    setDqLoadError(null)
    const promise = api.dqSummary({ date_from: dateFromIso, date_to: dateToIso, revalidate })
      .then((d) => {
        setDqData(d)
        ;(d.errors || []).forEach((err) => onReportErrorRef.current?.({
          tab: 'Dashboard', action: 'load DQ data', error: `${err.platform}: ${err.error}`,
        }))
      })
      .catch((e) => {
        setDqLoadError(e.message || String(e))
        onReportErrorRef.current?.({ tab: 'Dashboard', action: 'load DQ data', error: e.message })
      })
      .finally(() => {
        if (dqInflightRef.current?.key === key) dqInflightRef.current = null
        setDqLoading(false)
      })
    dqInflightRef.current = { key, promise }
    return promise
  }, [dateFromIso, dateToIso])

  const refresh = useCallback(() => {
    taskInflightRef.current = null
    dqInflightRef.current = null
    loadTasks()
    loadDq({ revalidate: false })
  }, [loadTasks, loadDq])

  const revalidateDq = useCallback(() => {
    dqInflightRef.current = null
    loadDq({ revalidate: true })
  }, [loadDq])

  // First load + date changes: always revalidate=false. Live only via Revalidate live.
  useEffect(() => {
    if (!sfReady) return undefined
    let cancelled = false
    ;(async () => {
      await loadTasks()
      if (!cancelled) await loadDq({ revalidate: false })
    })()
    return () => { cancelled = true }
  }, [sfReady, loadTasks, loadDq])

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
      .filter((c) => c.status === 'FAILED' || c.status === 'WARNING' || c.status === 'DELAYED')
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
      dqSource: dqData ? { table: dqData.table } : null,
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
        <label htmlFor="dash-search">Search</label>
        <div className={`toolbar__search${subTab === 'dq' ? ' toolbar__search--dq' : ' toolbar__search--tasks'}`}>
          <input
            id="dash-search"
            type="search"
            placeholder={subTab === 'dq'
              ? 'QC ID, subject area, check type'
              : 'Pipeline / task, platform'}
            value={nameFilter}
            onChange={(e) => setNameFilter(e.target.value)}
            aria-label={subTab === 'dq'
              ? 'Search DQ checks by QC ID, subject area, or check type'
              : 'Search tasks by pipeline, task, or platform'}
          />
        </div>
        <label>Status</label>
        <select
          value={status}
          disabled={busy}
          onChange={(e) => (subTab === 'tasks' ? setTaskStatus : setDqStatus)(e.target.value)}
        >
          {(subTab === 'tasks'
            ? ['FAILED', 'DELAYED', 'SUCCESS', 'SKIPPED', 'RUNNING', 'ALL']
            : ['FAILED', 'WARNING', 'SUCCESS', 'SKIPPED', 'RUNNING', 'ALL']
          ).map((s) => <option key={s}>{s}</option>)}
        </select>
        <label>From</label>
        <input type="date" value={dateFrom} disabled={busy} onChange={(e) => setDateFrom(e.target.value)} />
        <label>To</label>
        <input type="date" value={dateTo} disabled={busy} onChange={(e) => setDateTo(e.target.value)} />
        <button className={`btn sec${busy ? ' is-loading' : ''}`} onClick={refresh} disabled={busy}>
          Refresh
        </button>
        {subTab === 'dq' && (
          <button
            className={`btn${dqLoading ? ' is-loading' : ''}`}
            onClick={revalidateDq}
            disabled={busy}
            title="Re-run failing DQ rules live and save statuses for this date range"
          >
            Revalidate
          </button>
        )}
      </div>
      {subTab === 'dq' && dqData && (dqData.status_mode === 'live' || dqData.status_mode === 'cached_live') && (
        <p className="muted" style={{ margin: '6px 0 0', fontSize: 12 }}>
          {dqData.status_mode === 'live' && (
            <>Live statuses just revalidated{dqData.cache_updated_at ? ` · saved ${new Date(dqData.cache_updated_at).toLocaleString()}` : ''} · valid 30 min</>
          )}
          {dqData.status_mode === 'cached_live' && (
            <>From revalidate cache{dqData.cache_updated_at ? ` · ${new Date(dqData.cache_updated_at).toLocaleString()}` : ''} · TTL 30 min</>
          )}
        </p>
      )}

      <div hidden={subTab !== 'tasks'}>
        {taskLoadError && (
          <div className="card" style={{ borderColor: '#ff5c6c', marginBottom: 12 }}>
            <p className="muted" style={{ margin: 0 }}>Task data failed to load: {taskLoadError}</p>
          </div>
        )}
        <TasksView
          data={taskData}
          status={taskStatus}
          nameFilter={debouncedNameFilter}
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
          nameFilter={debouncedNameFilter}
          data={dqData}
          status={dqStatus}
          loading={dqLoading}
          configured={configured}
          onSelect={onSelect}
          onRunRCA={onRunRCA}
        />
      </div>

      {!chatOpen && onOpenChat && <ChatFab onOpen={onOpenChat} />}
    </div>
  )
}
