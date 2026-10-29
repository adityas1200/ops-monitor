import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import ChatFab from './ChatFab'
import DataTable, { buildPageSizeOptions } from './DataTable'
import { KpiSkeleton, LoadingOverlay, TableSkeleton } from './LoadingIndicator'

const DQ_KPI_DEFS = [
  ['total', 'Total'],
  ['success', 'Success'], ['failed', 'Failed'], ['warning', 'Warning'],
  ['skipped', 'Skipped'], ['running', 'Running'],
]

const TASK_KPI_DEFS = [
  ['total', 'Total'],
  ['success', 'Success'], ['failed', 'Failed'], ['delayed', 'Delayed'],
  ['skipped', 'Skipped'], ['running', 'Running'], ['scheduled', 'Scheduled'],
]

/** Map KPI card keys → backend status filter values. */
const KPI_KEY_TO_STATUS = {
  total: 'ALL',
  success: 'SUCCESS',
  failed: 'FAILED',
  delayed: 'DELAYED',
  warning: 'WARNING',
  skipped: 'SKIPPED',
  running: 'RUNNING',
  scheduled: 'SCHEDULED',
}

/** Default lookback: past 3 days including today. */
function defaultDateFromISO() {
  const d = new Date(Date.now() - 3 * 24 * 3600 * 1000)
  return d.toISOString().slice(0, 10)
}

const STATUS_ALIASES = {
  SUCCESS: ['SUCCESS', 'SUCCEEDED', 'PASS', 'PASSED', 'OK'],
  FAILED: ['FAILED', 'FAIL', 'TIMEOUT', 'FAILED_AND_AUTO_SUSPENDED', 'ERROR'],
  SKIPPED: ['SKIPPED', 'SKIP', 'CANCELLED'],
  RUNNING: ['RUNNING', 'EXECUTING', 'PENDING'],
  SCHEDULED: ['SCHEDULED', 'PLANNED'],
  DELAYED: ['DELAYED'],
  WARNING: ['WARNING', 'WARN'],
}

const STATUS_PRIORITY = {
  SUCCESS: 0, FAILED: 1, DELAYED: 2, SKIPPED: 3, RUNNING: 4, SCHEDULED: 5, WARNING: 6,
}

function getStatusPriority(s) {
  const upper = (s || '').toUpperCase()
  for (const [group, aliases] of Object.entries(STATUS_ALIASES)) {
    if (aliases.includes(upper)) return STATUS_PRIORITY[group] ?? 5
  }
  return 5
}

function formatDuration(seconds) {
  if (seconds == null) return '—'
  const s = Math.round(seconds)
  if (s < 60) return `${s}s`
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}h ${m}m ${sec}s`
  return `${m}m ${sec}s`
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
    } else if (sortCol === 'run_at' || sortCol === 'started_at' || sortCol === 'ended_at') {
      av = a[sortCol] || ''
      bv = b[sortCol] || ''
    } else {
      av = (a[sortCol] || '').toLowerCase()
      bv = (b[sortCol] || '').toLowerCase()
    }
    if (av < bv) return -1 * dir
    if (av > bv) return 1 * dir
    return 0
  })
}

/** Display form for run_at, e.g. "2026-07-19 23:31:46". */
function formatRunAt(runAt) {
  if (!runAt) return ''
  return String(runAt).replace('T', ' ').slice(0, 19)
}

function KpiIcon({ type }) {
  const common = {
    width: 30,
    height: 30,
    viewBox: '0 0 24 24',
    fill: 'none',
    'aria-hidden': true,
  }
  switch (type) {
    case 'success':
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" />
          <path d="M8 12.5l2.5 2.5L16 9.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      )
    case 'failed':
      return (
        <svg {...common}>
          <path
            d="M7.86 3h8.28L21 7.86v8.28L16.14 21H7.86L3 16.14V7.86L7.86 3z"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinejoin="round"
          />
          <path d="M12 8v5M12 16.5v.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      )
    case 'warning':
      return (
        <svg {...common}>
          <path d="M12 4l9 16H3L12 4z" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" />
          <path d="M12 10v4M12 17.5v.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      )
    case 'delayed':
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" />
          <path d="M12 7v5l3.5 2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      )
    case 'skipped':
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" />
          <path d="M8 12h8M14 9l3 3-3 3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      )
    case 'running':
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" />
          <path d="M10 8.5l6 3.5-6 3.5V8.5z" fill="currentColor" />
        </svg>
      )
    case 'scheduled':
      return (
        <svg {...common}>
          <rect x="3.5" y="5" width="17" height="15" rx="2" stroke="currentColor" strokeWidth="2" />
          <path d="M8 3.5V7M16 3.5V7M3.5 10h17" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      )
    case 'total':
    default:
      return (
        <svg {...common}>
          <rect x="4" y="4" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="2" />
          <rect x="13" y="4" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="2" />
          <rect x="4" y="13" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="2" />
          <rect x="13" y="13" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="2" />
        </svg>
      )
  }
}

function KpiBar({ kpis, defs, status = 'ALL', onStatusChange }) {
  const items = defs || DQ_KPI_DEFS

  return (
    <div className="kpis" role="group" aria-label="Status filters">
      {items.map(([k, label]) => {
        const filterStatus = KPI_KEY_TO_STATUS[k] || 'ALL'
        const selected = status === filterStatus
        return (
          <button
            key={k}
            type="button"
            className={`kpi ${k}${selected ? ' is-selected' : ''}`}
            aria-pressed={selected}
            title={filterStatus === 'ALL' ? 'Show all statuses' : `Filter: ${label}`}
            onClick={() => {
              if (!onStatusChange) return
              // Re-click active KPI → clear filter (ALL / Total).
              if (status === filterStatus && filterStatus !== 'ALL') {
                onStatusChange('ALL')
                return
              }
              onStatusChange(filterStatus)
            }}
          >
            <div className="kpi__body">
              <div className="v">{kpis[k] ?? '—'}</div>
              <div className="l">{label}</div>
            </div>
            <span className="kpi__icon">
              <KpiIcon type={k} />
            </span>
          </button>
        )
      })}
    </div>
  )
}

function TasksView({
  data, status, onStatusChange, platformFilter, onPlatformFilterChange,
  loading, configured, onRunRCA, onSelect,
  nameFilter, onNameFilterChange, draftDateFrom, onDraftDateFromChange,
  draftDateTo, onDraftDateToChange, onSearch, onRefresh,
  busy, datesValid, searchDirty,
}) {
  // Backend already applied status/search/platform → table_pipelines.
  const tablePipelines = data?.table_pipelines || data?.pipelines || []
  const inventoryCount = (data?.all_pipelines || []).length
  const [sortCol, setSortCol] = useState('started_at')
  const [sortDir, setSortDir] = useState('desc')
  const pageSizeOptions = useMemo(() => buildPageSizeOptions(), [])

  useEffect(() => {
    if (status === 'ALL') {
      setSortCol('status')
      setSortDir('asc')
    } else {
      setSortCol('started_at')
      setSortDir('desc')
    }
  }, [status])

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

  const rows = useMemo(
    () => sortRows(tablePipelines, sortCol, sortDir),
    [tablePipelines, sortCol, sortDir],
  )
  const showSkeleton = loading && !data

  const columns = useMemo(() => [
    { key: 'name', label: 'Pipeline / Task' },
    {
      key: 'platform',
      label: 'Platform',
      render: (p) => <span className="pill">{p.platform}</span>,
    },
    {
      key: 'status',
      label: 'Status',
      render: (p) => <span className={`badge ${p.status}`}>{p.status}</span>,
    },
    {
      key: 'started_at',
      label: 'Started',
      cellClassName: 'muted',
      cellStyle: { whiteSpace: 'nowrap' },
      render: (p) => (p.started_at ? formatRunAt(p.started_at) : '—'),
    },
    {
      key: 'ended_at',
      label: 'Ended',
      cellClassName: 'muted',
      cellStyle: { whiteSpace: 'nowrap' },
      render: (p) => (p.ended_at ? formatRunAt(p.ended_at) : '—'),
    },
    {
      key: 'duration_s',
      label: 'Duration',
      render: (p) => formatDuration(p.duration_s),
    },
    {
      key: 'error',
      label: 'Error',
      sortable: false,
      cellClassName: 'muted',
      cellStyle: { maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
      render: (p) => p.error || '—',
    },
    {
      key: '_actions',
      label: '',
      sortable: false,
      render: (p) => {
        const canInvestigate = p.status === 'FAILED' || p.status === 'DELAYED' || p.status === 'WARNING'
        if (!canInvestigate) return null
        return (
          <button className="btn" onClick={(e) => { e.stopPropagation(); onRunRCA?.(p) }}>Run RCA</button>
        )
      },
    },
  ], [onRunRCA])

  const emptyContent = configured
    ? platformFilter !== 'ALL'
      ? `No ${platformFilter === 'glue' ? 'AWS Glue' : 'Snowflake'} tasks match the selected filters.`
      : status === 'ALL'
        ? 'No tasks in the selected date range.'
        : `No tasks with status ${status} (${inventoryCount} total in date range).`
    : 'Configure Snowflake or AWS in Settings to load task data.'

  return (
    <LoadingOverlay active={loading} label="Loading task monitoring data">
      {showSkeleton ? (
        <KpiSkeleton count={TASK_KPI_DEFS.length} />
      ) : (
        <KpiBar
          kpis={data?.kpis || {}}
          defs={TASK_KPI_DEFS}
          status={status}
          onStatusChange={onStatusChange}
        />
      )}
      <div className={`table-toolbar${busy ? ' is-busy' : ''}`}>
        <div className="table-toolbar__source" />
        <div className="table-toolbar__filters">
          <SearchField
            id="dash-task-search"
            className="search-field--tasks"
            placeholder="Pipeline / task, platform"
            value={nameFilter}
            onChange={(e) => onNameFilterChange?.(e.target.value)}
            onSubmit={onSearch}
            aria-label="Search tasks by pipeline, task, or platform"
          />
          <label htmlFor="task-platform-filter">Platform</label>
          <select
            id="task-platform-filter"
            value={platformFilter}
            disabled={busy}
            onChange={(e) => onPlatformFilterChange?.(e.target.value)}
          >
            <option value="ALL">All platforms</option>
            <option value="snowflake">Snowflake</option>
            <option value="glue">AWS Glue</option>
          </select>
          <label htmlFor="task-date-from">From</label>
          <input
            id="task-date-from"
            type="date"
            value={draftDateFrom}
            onChange={(e) => onDraftDateFromChange?.(e.target.value)}
            aria-label="Date range from"
          />
          <label htmlFor="task-date-to">To</label>
          <input
            id="task-date-to"
            type="date"
            value={draftDateTo}
            onChange={(e) => onDraftDateToChange?.(e.target.value)}
            aria-label="Date range to"
          />
          <button
            type="button"
            className="btn"
            disabled={busy || !datesValid || !searchDirty}
            onClick={onSearch}
            title="Apply search text and date range"
          >
            Search
          </button>
          <button
            type="button"
            className="btn sec dashboard-refresh icon-btn"
            onClick={onRefresh}
            disabled={busy}
            title="Refresh"
            aria-label="Refresh"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path
                d="M21 12a9 9 0 1 1-2.64-6.36"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
              <path
                d="M21 3v6h-6"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>
      </div>
      {showSkeleton ? (
        <TableSkeleton rows={8} cols={8} />
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(p) => p.id}
          sortCol={sortCol}
          sortDir={sortDir}
          onSort={handleSort}
          pageSizeOptions={pageSizeOptions}
          totalCount={typeof data?.match_count === 'number' ? data.match_count : rows.length}
          pageResetKey={`${status}|${data?.q || ''}|${platformFilter}`}
          checkboxes="hidden"
          onRowClick={(p) => {
            const canInvestigate = p.status === 'FAILED' || p.status === 'DELAYED' || p.status === 'WARNING'
            if (canInvestigate) onSelect?.(p)
          }}
          rowClassName={(p) => (
            (p.status === 'FAILED' || p.status === 'DELAYED' || p.status === 'WARNING') ? 'clickable' : ''
          )}
          emptyContent={!loading ? emptyContent : null}
        />
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
                  <DataTable
                    compact
                    checkboxes="hidden"
                    maxRows={100}
                    columns={sqlResults.columns.map((col) => ({
                      key: col,
                      label: col,
                      sortable: false,
                      render: (row) => row[col] ?? '—',
                    }))}
                    rows={sqlResults.rows}
                    rowKey={(_, i) => i}
                    emptyContent="No rows."
                  />
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

function SearchField({
  id,
  value,
  onChange,
  onSubmit,
  placeholder,
  'aria-label': ariaLabel,
  className = '',
}) {
  return (
    <div className={`search-field ${className}`.trim()}>
      <input
        id={id}
        type="search"
        placeholder={placeholder}
        value={value}
        onChange={onChange}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            onSubmit?.()
          }
        }}
        aria-label={ariaLabel || placeholder}
      />
      <span className="search-field__icon" aria-hidden="true">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
          <circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="2" />
          <path d="M20 20l-3.5-3.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      </span>
    </div>
  )
}

function ActionIconBtn({
  title, disabled, busy, onClick, children, tone = 'sec',
}) {
  return (
    <button
      type="button"
      className={`btn ${tone} action-icon-btn${busy ? ' is-loading' : ''}`}
      title={title}
      aria-label={title}
      disabled={disabled || busy}
      onClick={(e) => {
        e.stopPropagation()
        onClick?.(e)
      }}
    >
      {children}
    </button>
  )
}

function DQView({
  data, status, onStatusChange, loading, configured, onSelect, onRunRCA,
  page, pageSize, onPageChange, onPageSizeChange,
  nameFilter, onNameFilterChange, draftDateFrom, onDraftDateFromChange,
  draftDateTo, onDraftDateToChange, onSearch, onRefresh, onRevalidate,
  busy, datesValid, searchDirty, revalidating, onRowRevalidate, rowRevalidatingId,
}) {
  const inventoryCount = data?.inventory_count ?? data?.kpis?.total ?? 0
  // Backend already filtered + paginated → table_checks is the current page only.
  const tableChecks = data?.table_checks || []
  const [sortCol, setSortCol] = useState('status')
  const [sortDir, setSortDir] = useState('asc')
  const [detailCheck, setDetailCheck] = useState(null)
  const pageSizeOptions = useMemo(() => buildPageSizeOptions(), [])

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

  // Sort only the current server page (full-set sort would need backend sort params).
  const rows = useMemo(
    () => sortRows(tableChecks, sortCol, sortDir),
    [tableChecks, sortCol, sortDir],
  )
  const tableTotalCount = typeof data?.match_count === 'number'
    ? data.match_count
    : rows.length
  // API page is 1-based; DataTable server mode uses 0-based.
  const pageIndex = Math.max(0, (Number(page) || 1) - 1)
  const showSkeleton = loading && !data

  const columns = useMemo(() => [
    { key: 'name', label: 'QC ID' },
    {
      key: 'table_name',
      label: 'Subject Area',
      render: (c) => c.table_name || '—',
    },
    {
      key: 'column_name',
      label: 'Check Type',
      render: (c) => c.column_name || '—',
    },
    {
      key: 'status',
      label: 'Status',
      render: (c) => <span className={`badge ${c.status}`}>{c.status}</span>,
    },
    {
      key: 'error',
      label: 'Details',
      sortable: false,
      cellClassName: 'muted',
      cellStyle: { maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
      render: (c) => c.error || '—',
    },
    {
      key: '_actions',
      label: 'Actions',
      sortable: false,
      cellClassName: 'dq-actions-cell',
      cellStyle: { whiteSpace: 'nowrap', width: 1 },
      render: (c) => {
        const canAct = c.status === 'FAILED' || c.status === 'WARNING'
        const rowKey = c.id || `${c.name}|${c.table_name || ''}`
        const rowBusy = Boolean(rowRevalidatingId && rowRevalidatingId === rowKey)
        return (
          <div className="dq-actions">
            <ActionIconBtn
              title="View Details"
              onClick={() => setDetailCheck({ qcId: c.name, subjectArea: c.table_name })}
            >
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M2.5 12s3.5-6.5 9.5-6.5S21.5 12 21.5 12s-3.5 6.5-9.5 6.5S2.5 12 2.5 12z"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinejoin="round"
                />
                <circle cx="12" cy="12" r="2.6" stroke="currentColor" strokeWidth="1.8" />
              </svg>
            </ActionIconBtn>
            <ActionIconBtn
              title={canAct ? 'Run RCA' : 'Run RCA (Failed / Warning only)'}
              disabled={!canAct}
              tone="sec"
              onClick={() => onRunRCA?.(c)}
            >
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.8" />
                <path d="M10 8.5v7l6-3.5-6-3.5z" fill="currentColor" />
              </svg>
            </ActionIconBtn>
            <ActionIconBtn
              title={canAct ? 'Revalidate' : 'Revalidate (Failed / Warning only)'}
              disabled={!canAct}
              busy={rowBusy}
              onClick={() => onRowRevalidate?.(c)}
            >
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M21 12a9 9 0 1 1-2.64-6.36"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                />
                <path
                  d="M21 3v6h-6"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </ActionIconBtn>
          </div>
        )
      },
    },
  ], [onRunRCA, onRowRevalidate, rowRevalidatingId])

  const emptyContent = !configured
    ? 'Configure Snowflake in Settings to load DQ results.'
    : data?.errors?.length
      ? `Could not load DQ data: ${data.errors[0].error}`
      : status === 'ALL'
        ? 'No DQ checks in the selected date range.'
        : `No DQ checks with status ${status} (${inventoryCount} total in date range).`

  return (
    <LoadingOverlay active={loading} label="Loading data quality results">
      {showSkeleton ? (
        <KpiSkeleton count={DQ_KPI_DEFS.length} />
      ) : (
        <KpiBar
          kpis={data?.kpis || {}}
          defs={DQ_KPI_DEFS}
          status={status}
          onStatusChange={onStatusChange}
        />
      )}
      <div className={`table-toolbar${busy ? ' is-busy' : ''}`}>
        <div className="table-toolbar__source">
          {data?.table && !showSkeleton ? (
            <span className="table-source">
              Table Source: <code>{data.table}</code>
            </span>
          ) : (
            <span className="table-source table-source--placeholder" aria-hidden="true" />
          )}
        </div>
        <div className="table-toolbar__filters">
          <SearchField
            id="dash-dq-search"
            className="search-field--dq"
            placeholder="QC ID, Subject Area, Check Type"
            value={nameFilter}
            onChange={(e) => onNameFilterChange?.(e.target.value)}
            onSubmit={onSearch}
            aria-label="Search DQ checks by QC ID, Subject Area, or Check Type"
          />
          <label htmlFor="dq-date-from">From</label>
          <input
            id="dq-date-from"
            type="date"
            value={draftDateFrom}
            onChange={(e) => onDraftDateFromChange?.(e.target.value)}
            aria-label="Date range from"
          />
          <label htmlFor="dq-date-to">To</label>
          <input
            id="dq-date-to"
            type="date"
            value={draftDateTo}
            onChange={(e) => onDraftDateToChange?.(e.target.value)}
            aria-label="Date range to"
          />
          <button
            type="button"
            className="btn"
            disabled={busy || !datesValid || !searchDirty}
            onClick={onSearch}
            title="Apply search text and date range"
          >
            Search
          </button>
          <button
            type="button"
            className="btn sec dashboard-refresh icon-btn"
            onClick={onRefresh}
            disabled={busy}
            title="Refresh"
            aria-label="Refresh"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path
                d="M21 12a9 9 0 1 1-2.64-6.36"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
              <path
                d="M21 3v6h-6"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
          <button
            type="button"
            className={`btn${revalidating ? ' is-loading' : ''}`}
            onClick={onRevalidate}
            disabled={busy || revalidating}
            title="Re-run failing DQ rules live and save statuses for this date range"
          >
            Revalidate
          </button>
        </div>
      </div>
      {showSkeleton ? (
        <TableSkeleton rows={8} cols={5} />
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(c) => c.id}
          sortCol={sortCol}
          sortDir={sortDir}
          onSort={handleSort}
          pageSizeOptions={pageSizeOptions}
          pagination="server"
          page={pageIndex}
          pageSize={pageSize || 10}
          totalCount={tableTotalCount}
          onPageChange={(idx) => onPageChange?.(idx + 1)}
          onPageSizeChange={(size) => onPageSizeChange?.(size)}
          checkboxes="hidden"
          onRowClick={(c) => onSelect?.(c)}
          rowClassName="clickable"
          emptyContent={!loading ? emptyContent : null}
        />
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

function revalidateConfigFromSettings(settings) {
  const dq = settings?.monitoring?.dq || {}
  const mode = String(dq.revalidate_mode || 'default').toLowerCase() === 'custom'
    ? 'custom'
    : 'default'
  const lim = dq.revalidate_limit
  const n = lim == null || lim === '' ? null : Number(lim)
  return {
    revalidate_mode: mode,
    revalidate_limit: Number.isFinite(n) && n >= 1 ? n : null,
  }
}

export default function Dashboard({
  onRunRCA, onSelect, onReportError, platforms, onChatContextChange,
  chatOpen = false, onOpenChat, settingsVersion = 0,
}) {
  const [subTab, setSubTab] = useState('dq')
  const [dateFrom, setDateFrom] = useState(defaultDateFromISO())
  const [dateTo, setDateTo] = useState(new Date().toISOString().slice(0, 10))
  const [draftDateFrom, setDraftDateFrom] = useState(dateFrom)
  const [draftDateTo, setDraftDateTo] = useState(dateTo)
  const [taskStatus, setTaskStatus] = useState('ALL')
  const [dqStatus, setDqStatus] = useState('ALL')
  const [platformFilter, setPlatformFilter] = useState('ALL')
  const [nameFilter, setNameFilter] = useState('')
  /** Applied search text — only updated when Search is pressed. */
  const [appliedSearch, setAppliedSearch] = useState('')
  const [dqPage, setDqPage] = useState(1)
  const [dqPageSize, setDqPageSize] = useState(10)

  const [taskData, setTaskData] = useState(null)
  const [dqData, setDqData] = useState(null)
  const [taskLoadError, setTaskLoadError] = useState(null)
  const [dqLoadError, setDqLoadError] = useState(null)
  const [taskLoading, setTaskLoading] = useState(false)
  const [dqLoading, setDqLoading] = useState(false)
  const [dqRevalidating, setDqRevalidating] = useState(false)
  const [revalidateConfirmOpen, setRevalidateConfirmOpen] = useState(false)
  const [revalidateConfig, setRevalidateConfig] = useState({
    revalidate_mode: 'default',
    revalidate_limit: null,
  })
  const [rowRevalidatingId, setRowRevalidatingId] = useState(null)
  const [toast, setToast] = useState(null) // { type: 'success'|'error', message }
  const [sfReady, setSfReady] = useState(false)
  const toastTimerRef = useRef(null)

  const revalidateConfigRef = useRef(revalidateConfig)
  revalidateConfigRef.current = revalidateConfig

  const refreshRevalidateConfig = useCallback(() => (
    api.getSettings()
      .then((s) => {
        const cfg = revalidateConfigFromSettings(s)
        setRevalidateConfig(cfg)
        revalidateConfigRef.current = cfg
        // Keep cached DQ payload metadata aligned with Settings (confirm dialog + UI).
        setDqData((prev) => (prev ? { ...prev, ...cfg } : prev))
        return cfg
      })
      .catch(() => null)
  ), [])

  useEffect(() => {
    refreshRevalidateConfig()
  }, [refreshRevalidateConfig, settingsVersion])

  // Keep callbacks in refs so loaders stay stable (avoids re-fetch when parent recreates them).
  const onReportErrorRef = useRef(onReportError)
  onReportErrorRef.current = onReportError
  const taskInflightRef = useRef(null)
  const dqInflightRef = useRef(null)

  const configured = Object.values(platforms || {}).some(Boolean)
  const dateFromIso = dateFrom ? dateFrom + 'T00:00:00+00:00' : undefined
  const dateToIso = dateTo ? dateTo + 'T23:59:59+00:00' : undefined
  const busy = taskLoading || dqLoading
  const datesChanged = draftDateFrom !== dateFrom || draftDateTo !== dateTo
  const searchChanged = nameFilter.trim() !== appliedSearch
  const datesValid = Boolean(
    draftDateFrom && draftDateTo && draftDateFrom <= draftDateTo
  )
  const searchDirty = datesChanged || searchChanged

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

  const loadTasks = useCallback((opts = {}) => {
    const fromIso = opts.date_from !== undefined ? opts.date_from : dateFromIso
    const toIso = opts.date_to !== undefined ? opts.date_to : dateToIso
    const status = opts.status !== undefined ? opts.status : taskStatus
    const q = opts.q !== undefined ? opts.q : appliedSearch
    const platform = opts.platform !== undefined ? opts.platform : platformFilter
    const key = `${fromIso || ''}|${toIso || ''}|st:${status}|q:${q}|pf:${platform}`
    if (taskInflightRef.current?.key === key) {
      return taskInflightRef.current.promise
    }
    setTaskLoading(true)
    setTaskLoadError(null)
    const promise = api.summary({
      status,
      date_from: fromIso,
      date_to: toIso,
      q,
      platform,
    })
      .then((d) => {
        if (!d.live_only) {
          setTaskData({ ...d, all_pipelines: [], table_pipelines: [], kpis: { success: 0, failed: 0, delayed: 0, skipped: 0, running: 0, scheduled: 0, total: 0 } })
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
  }, [dateFromIso, dateToIso, taskStatus, appliedSearch, platformFilter])

  const showToast = useCallback((type, message) => {
    if (toastTimerRef.current) {
      clearTimeout(toastTimerRef.current)
      toastTimerRef.current = null
    }
    setToast({ type, message })
    toastTimerRef.current = setTimeout(() => {
      setToast(null)
      toastTimerRef.current = null
    }, 4500)
  }, [])

  useEffect(() => () => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current)
  }, [])

  const loadDq = useCallback((opts = {}) => {
    // Default / date change / refresh: revalidate=false (backend may return cache).
    // Pass revalidate:true only from confirmed Revalidate (quiet — no full-page overlay).
    const revalidate = opts.revalidate === true
    const quiet = opts.quiet === true || revalidate
    const fromIso = opts.date_from !== undefined ? opts.date_from : dateFromIso
    const toIso = opts.date_to !== undefined ? opts.date_to : dateToIso
    const status = opts.status !== undefined ? opts.status : dqStatus
    const q = opts.q !== undefined ? opts.q : appliedSearch
    const page = opts.page !== undefined ? opts.page : dqPage
    const pageSize = opts.page_size !== undefined ? opts.page_size : dqPageSize
    const key = `${fromIso || ''}|${toIso || ''}|rv:${revalidate ? 1 : 0}|st:${status}|q:${q}|p:${page}|ps:${pageSize}`
    if (dqInflightRef.current?.key === key) {
      return dqInflightRef.current.promise
    }
    if (!quiet) {
      setDqLoading(true)
      setDqLoadError(null)
    }
    const promise = api.dqSummary({
      date_from: fromIso,
      date_to: toIso,
      revalidate,
      status,
      q,
      page,
      page_size: pageSize,
    })
      .then((d) => {
        // Prefer live Settings config over any stale fields embedded in dq summary/cache.
        const cfg = revalidateConfigRef.current || {}
        const merged = {
          ...d,
          revalidate_mode: cfg.revalidate_mode || d?.revalidate_mode || 'default',
          revalidate_limit: cfg.revalidate_mode === 'custom'
            ? (cfg.revalidate_limit ?? d?.revalidate_limit ?? null)
            : null,
        }
        setDqData(merged)
        if (Number.isFinite(Number(d?.page)) && Number(d.page) >= 1) {
          setDqPage(Number(d.page))
        }
        ;(d.errors || []).forEach((err) => onReportErrorRef.current?.({
          tab: 'Dashboard', action: 'load DQ data', error: `${err.platform}: ${err.error}`,
        }))
        return merged
      })
      .catch((e) => {
        onReportErrorRef.current?.({
          tab: 'Dashboard',
          action: revalidate ? 'revalidate DQ data' : 'load DQ data',
          error: e.message,
        })
        // Quiet/revalidate callers handle the snackbar; normal loads keep overlay error.
        if (quiet) throw e
        setDqLoadError(e.message || String(e))
        return null
      })
      .finally(() => {
        if (dqInflightRef.current?.key === key) dqInflightRef.current = null
        if (!quiet) setDqLoading(false)
      })
    dqInflightRef.current = { key, promise }
    return promise
  }, [dateFromIso, dateToIso, dqStatus, appliedSearch, dqPage, dqPageSize])

  const refresh = useCallback(() => {
    taskInflightRef.current = null
    dqInflightRef.current = null
    loadTasks()
    loadDq({ revalidate: false })
  }, [loadTasks, loadDq])

  const runSearch = useCallback(() => {
    if (!datesValid || busy) return
    const nextQ = nameFilter.trim()
    const same =
      draftDateFrom === dateFrom
      && draftDateTo === dateTo
      && nextQ === appliedSearch
      && dqPage === 1
    setDateFrom(draftDateFrom)
    setDateTo(draftDateTo)
    setAppliedSearch(nextQ)
    setDqPage(1)
    // Same applied values → force a reload (state alone would not re-trigger effects).
    if (same) {
      taskInflightRef.current = null
      dqInflightRef.current = null
      loadTasks()
      loadDq({ revalidate: false, page: 1 })
    }
  }, [
    datesValid, busy, nameFilter, draftDateFrom, draftDateTo, dateFrom, dateTo,
    appliedSearch, dqPage, loadTasks, loadDq,
  ])

  // KPI status change → first page.
  const onDqStatusChange = useCallback((next) => {
    setDqStatus(next)
    setDqPage(1)
  }, [])

  const onDqPageChange = useCallback((nextPage) => {
    setDqPage(Math.max(1, nextPage))
  }, [])

  const onDqPageSizeChange = useCallback((nextSize) => {
    setDqPageSize(nextSize)
    setDqPage(1)
  }, [])

  const openRevalidateConfirm = useCallback(() => {
    if (busy || dqRevalidating) return
    // Always pull latest Settings before showing the confirm copy.
    refreshRevalidateConfig().finally(() => setRevalidateConfirmOpen(true))
  }, [busy, dqRevalidating, refreshRevalidateConfig])

  const cancelRevalidateConfirm = useCallback(() => {
    setRevalidateConfirmOpen(false)
  }, [])

  const proceedRevalidate = useCallback(() => {
    setRevalidateConfirmOpen(false)
    dqInflightRef.current = null
    setDqRevalidating(true)
    Promise.resolve(loadDq({ revalidate: true, quiet: true }))
      .then(() => {
        showToast('success', 'Revalidation is successfully completed')
      })
      .catch((e) => {
        showToast('error', e?.message || 'Revalidation failed')
      })
      .finally(() => setDqRevalidating(false))
  }, [loadDq, showToast])

  const revalidateRow = useCallback((check) => {
    if (!check?.name || rowRevalidatingId) return
    const status = (check.status || '').toUpperCase()
    if (status !== 'FAILED' && status !== 'WARNING') return
    const key = check.id || `${check.name}|${check.table_name || ''}`
    setRowRevalidatingId(key)
    api.dqRevalidateOne({
      qc_id: check.name,
      subject_area: check.table_name || null,
      id: check.id || null,
      status: check.status || null,
      error: check.error || null,
      pass_count: check.pass_count ?? null,
      fail_count: check.fail_count ?? null,
    })
      .then((res) => {
        setDqData((prev) => {
          if (!prev) return prev
          const patch = (row) => {
            const sameId = check.id && row.id === check.id
            const sameKey = row.name === check.name
              && (row.table_name || '') === (check.table_name || '')
            if (!sameId && !sameKey) return row
            return {
              ...row,
              status: res.status || row.status,
              error: res.error ?? row.error,
              pass_count: res.pass_count ?? row.pass_count,
              fail_count: res.fail_count ?? row.fail_count,
              stale: res.stale,
              live_checked: res.live_checked,
              recorded_status: res.recorded_status || row.status,
            }
          }
          return {
            ...prev,
            table_checks: (prev.table_checks || []).map(patch),
            all_checks: (prev.all_checks || []).map(patch),
          }
        })
        showToast('success', 'Revalidation is successfully completed')
      })
      .catch((e) => {
        showToast('error', e?.message || 'Revalidation failed')
      })
      .finally(() => setRowRevalidatingId(null))
  }, [rowRevalidatingId, showToast])

  // First load + applied Search (dates/text) / KPI status / platform: revalidate=false.
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
        <button
          className={`subtab ${subTab === 'dq' ? 'active' : ''}`}
          onClick={() => setSubTab('dq')}
          aria-pressed={subTab === 'dq'}
        >
          <span className="subtab-icon" aria-hidden="true">✓</span>
          <span>DQ Status</span>
        </button>
        <button
          className={`subtab ${subTab === 'tasks' ? 'active' : ''}`}
          onClick={() => setSubTab('tasks')}
          aria-pressed={subTab === 'tasks'}
        >
          <span className="subtab-icon" aria-hidden="true">▤</span>
          <span>Tasks</span>
        </button>
      </div>

      {!configured && (
        <div className="card" style={{ borderColor: '#ff5c6c', marginTop: 12 }}>
          <p className="muted" style={{ margin: 0 }}>
            No data platforms configured. Open <strong>Settings</strong> and add Snowflake credentials.
          </p>
        </div>
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
          onStatusChange={setTaskStatus}
          platformFilter={platformFilter}
          onPlatformFilterChange={setPlatformFilter}
          loading={taskLoading}
          configured={configured}
          onRunRCA={onRunRCA}
          onSelect={onSelect}
          nameFilter={nameFilter}
          onNameFilterChange={setNameFilter}
          draftDateFrom={draftDateFrom}
          onDraftDateFromChange={setDraftDateFrom}
          draftDateTo={draftDateTo}
          onDraftDateToChange={setDraftDateTo}
          onSearch={runSearch}
          onRefresh={refresh}
          busy={busy}
          datesValid={datesValid}
          searchDirty={searchDirty}
        />
      </div>
      <div hidden={subTab !== 'dq'}>
        {dqLoadError && (
          <div className="card" style={{ borderColor: '#ff5c6c', marginBottom: 12 }}>
            <p className="muted" style={{ margin: 0 }}>DQ data failed to load: {dqLoadError}</p>
          </div>
        )}
        <DQView
          data={dqData}
          status={dqStatus}
          onStatusChange={onDqStatusChange}
          loading={dqLoading}
          configured={configured}
          onSelect={onSelect}
          onRunRCA={onRunRCA}
          page={dqPage}
          pageSize={dqPageSize}
          onPageChange={onDqPageChange}
          onPageSizeChange={onDqPageSizeChange}
          nameFilter={nameFilter}
          onNameFilterChange={setNameFilter}
          draftDateFrom={draftDateFrom}
          onDraftDateFromChange={setDraftDateFrom}
          draftDateTo={draftDateTo}
          onDraftDateToChange={setDraftDateTo}
          onSearch={runSearch}
          onRefresh={refresh}
          onRevalidate={openRevalidateConfirm}
          busy={busy}
          datesValid={datesValid}
          searchDirty={searchDirty}
          revalidating={dqRevalidating}
          onRowRevalidate={revalidateRow}
          rowRevalidatingId={rowRevalidatingId}
        />
      </div>

      {revalidateConfirmOpen && (
        <div
          className="confirm-dialog-backdrop"
          role="presentation"
          onClick={cancelRevalidateConfirm}
        >
          <div
            className="confirm-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="revalidate-confirm-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="confirm-dialog__icon" aria-hidden="true">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none">
                <path
                  d="M21 12a9 9 0 1 1-2.64-6.36"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                />
                <path
                  d="M21 3v6h-6"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </div>
            <h3 id="revalidate-confirm-title" className="confirm-dialog__title">
              Revalidation Confirmation
            </h3>
            <p className="confirm-dialog__body">
              Are you sure you want to revalidate? This will validate{' '}
              {(revalidateConfig.revalidate_mode === 'custom' && revalidateConfig.revalidate_limit)
                ? (
                  <>
                    up to <strong>{revalidateConfig.revalidate_limit}</strong> failures
                  </>
                )
                : 'all the failures'}
              {' '}that come between the selected date range from{' '}
              <strong>{dateFrom}</strong>
              {' '}to{' '}
              <strong>{dateTo}</strong>
              {' '}and may take time to respond.
            </p>
            <div className="confirm-dialog__actions">
              <button type="button" className="btn sec" onClick={cancelRevalidateConfirm}>
                Cancel
              </button>
              <button type="button" className="btn" onClick={proceedRevalidate}>
                Proceed
              </button>
            </div>
          </div>
        </div>
      )}

      {toast && (
        <div
          className={`app-toast app-toast--${toast.type}`}
          role="status"
          aria-live="polite"
        >
          {toast.type === 'success' && (
            <span className="app-toast__icon app-toast__icon--success" aria-hidden="true">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                <path
                  d="M6.5 12.5l3.5 3.5 7.5-7.5"
                  stroke="currentColor"
                  strokeWidth="2.4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </span>
          )}
          <span className="app-toast__msg">{toast.message}</span>
          <button
            type="button"
            className="app-toast__close"
            aria-label="Dismiss"
            onClick={() => setToast(null)}
          >
            ×
          </button>
        </div>
      )}

      {!chatOpen && onOpenChat && <ChatFab onOpen={onOpenChat} />}
    </div>
  )
}
