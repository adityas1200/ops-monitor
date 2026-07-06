import React from 'react'

export function LoadingOverlay({ active, label, children }) {
  return (
    <div className="loading-shell">
      {active && (
        <div className="loading-overlay" aria-live="polite" aria-busy="true">
          <div className="loading-card">
            <div className="loading-ring" aria-hidden="true" />
            <p className="loading-card-title">{label || 'Loading data'}</p>
            <p className="loading-card-sub">Fetching from Snowflake…</p>
          </div>
        </div>
      )}
      <div className={`loading-shell__content${active ? ' is-dimmed' : ''}`}>
        {children}
      </div>
    </div>
  )
}

export function KpiSkeleton() {
  return (
    <div className="kpis" aria-hidden="true">
      {Array.from({ length: 5 }, (_, i) => (
        <div key={i} className="kpi kpi-skeleton">
          <div className="skeleton-block skeleton-value" />
          <div className="skeleton-block skeleton-label" />
        </div>
      ))}
    </div>
  )
}

export function TableSkeleton({ rows = 6, cols = 5 }) {
  return (
    <div className="table-skeleton" aria-hidden="true">
      <div className="table-skeleton-head">
        {Array.from({ length: cols }, (_, i) => (
          <div key={i} className="skeleton-block skeleton-th" />
        ))}
      </div>
      {Array.from({ length: rows }, (_, r) => (
        <div key={r} className="table-skeleton-row">
          {Array.from({ length: cols }, (_, c) => (
            <div key={c} className="skeleton-block skeleton-td" style={{ width: `${55 + ((r + c) % 4) * 10}%` }} />
          ))}
        </div>
      ))}
    </div>
  )
}
