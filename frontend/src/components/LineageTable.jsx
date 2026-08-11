import React from 'react'

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
  if (!rows?.length && !affectedTables?.length) {
    return <div className="muted">No lineage or table data resolved.</div>
  }

  return (
    <div className="lineage-tables">
      {affectedTables?.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <h4>Affected Tables ({affectedTables.length})</h4>
          <table className="lineage-table">
            <thead>
              <tr>
                <th>Table</th><th>Source</th><th>Role</th><th>Related to</th><th>Status</th>
              </tr>
            </thead>
            <tbody>
              {affectedTables.map((t) => (
                <tr key={t.table}>
                  <td><code>{t.table}</code></td>
                  <td><span className="pill">{t.source}</span></td>
                  <td>{t.role?.replace(/_/g, ' ')}</td>
                  <td className="muted">{t.related_entity || '—'}</td>
                  <td><span className={`badge ${t.status || 'WARNING'}`}>{t.status}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {rows?.length > 0 && (
        <div>
          <h4>Lineage Detail</h4>
          <table className="lineage-table">
            <thead>
              <tr>
                <th>Type</th><th>Name</th><th>Status</th><th>Role</th>
                <th>Tables ({rows.filter((r) => r.kind === 'table').length || rows.reduce((n, r) => n + (r.table_count || 0), 0)})</th>
                <th>Upstream</th><th>Error / detail</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.kind}-${r.id}`}>
                  <td><span className="pill">{kindLabel(r.kind)}</span></td>
                  <td style={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis' }} title={r.name}>
                    {r.name}
                  </td>
                  <td>
                    <span className="badge" style={{ borderColor: STATE_COLOR[r.state] || '#6b7689' }}>
                      {r.status}
                    </span>
                  </td>
                  <td className="muted">{r.role?.replace(/_/g, ' ')}</td>
                  <td style={{ maxWidth: 280, fontSize: 12 }}>
                    {r.kind === 'table' ? <code>{r.tables}</code> : (r.tables || '—')}
                  </td>
                  <td className="muted" style={{ maxWidth: 160, fontSize: 11 }}>{r.upstream}</td>
                  <td className="muted" style={{ maxWidth: 200, fontSize: 11 }}>{r.error}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
