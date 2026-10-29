import React, { useMemo } from 'react'
import DataTable from './DataTable'

const STATE_COLOR = {
  root_cause: '#ff5c6c',
  failed: '#ff8a3d',
  impacted: '#ffb547',
  healthy: '#2ecc71',
}

function kindLabel(kind) {
  if (kind === 'dq') return 'DQ'
  if (kind === 'table') return 'Table'
  if (kind === 'view') return 'View'
  if (kind === 'procedure') return 'Proc'
  return 'Task'
}

export default function LineageTable({ rows, affectedTables }) {
  const affectedColumns = useMemo(() => [
    {
      key: 'table',
      label: 'Table',
      sortable: false,
      render: (t) => <code>{t.table}</code>,
    },
    {
      key: 'source',
      label: 'Source',
      sortable: false,
      render: (t) => <span className="pill">{t.source}</span>,
    },
    {
      key: 'role',
      label: 'Role',
      sortable: false,
      render: (t) => t.role?.replace(/_/g, ' '),
    },
    {
      key: 'related_entity',
      label: 'Related to',
      sortable: false,
      cellClassName: 'muted',
      render: (t) => t.related_entity || '—',
    },
    {
      key: 'status',
      label: 'Status',
      sortable: false,
      render: (t) => <span className={`badge ${t.status || 'WARNING'}`}>{t.status}</span>,
    },
  ], [])

  const tableCount = rows?.length
    ? (rows.filter((r) => r.kind === 'table').length
      || rows.reduce((n, r) => n + (r.table_count || 0), 0))
    : 0

  const detailColumns = useMemo(() => [
    {
      key: 'kind',
      label: 'Type',
      sortable: false,
      render: (r) => <span className="pill">{kindLabel(r.kind)}</span>,
    },
    {
      key: 'name',
      label: 'Name',
      sortable: false,
      cellStyle: { maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis' },
      render: (r) => <span title={r.name}>{r.name}</span>,
    },
    {
      key: 'status',
      label: 'Status',
      sortable: false,
      render: (r) => (
        <span className="badge" style={{ borderColor: STATE_COLOR[r.state] || '#6b7689' }}>
          {r.status}
        </span>
      ),
    },
    {
      key: 'role',
      label: 'Role',
      sortable: false,
      cellClassName: 'muted',
      render: (r) => r.role?.replace(/_/g, ' '),
    },
    {
      key: 'tables',
      label: `Tables (${tableCount})`,
      sortable: false,
      cellStyle: { maxWidth: 280, fontSize: 12 },
      render: (r) => (r.kind === 'table' ? <code>{r.tables}</code> : (r.tables || '—')),
    },
    {
      key: 'upstream',
      label: 'Upstream',
      sortable: false,
      cellClassName: 'muted',
      cellStyle: { maxWidth: 160, fontSize: 11 },
    },
    {
      key: 'error',
      label: 'Error / detail',
      sortable: false,
      cellClassName: 'muted',
      cellStyle: { maxWidth: 200, fontSize: 11 },
    },
  ], [tableCount])

  if (!rows?.length && !affectedTables?.length) {
    return <div className="muted">No lineage or table data resolved.</div>
  }

  return (
    <div className="lineage-tables">
      {affectedTables?.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <h4>Affected Tables ({affectedTables.length})</h4>
          <DataTable
            compact
            className="lineage-table"
            checkboxes="hidden"
            maxRows={100}
            columns={affectedColumns}
            rows={affectedTables}
            rowKey={(t) => t.table}
          />
        </div>
      )}

      {rows?.length > 0 && (
        <div>
          <h4>Lineage Detail</h4>
          <DataTable
            compact
            className="lineage-table"
            checkboxes="hidden"
            maxRows={100}
            columns={detailColumns}
            rows={rows}
            rowKey={(r) => `${r.kind}-${r.id}`}
          />
        </div>
      )}
    </div>
  )
}
