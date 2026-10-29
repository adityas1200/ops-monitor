import React, { useEffect, useMemo, useRef, useState } from 'react'
import { buildPageSizeOptions, pageRangeLabel } from '../utils/pageSizeOptions'

export { buildPageSizeOptions, pageRangeLabel }

function SortCue({ active, dir }) {
  if (active) return <span className="data-table__sort-cue" aria-hidden="true">{dir === 'asc' ? '▲' : '▼'}</span>
  return <span className="data-table__sort-cue data-table__sort-cue--idle" aria-hidden="true">▼</span>
}

/**
 * Shared grid shell: card border, optional row checkboxes, pagination footer.
 *
 * @param {'client'|'server'} [pagination='client']
 *   client — slice `rows` locally; server — `rows` is already one page, parent owns page fetch.
 * @param {'hidden'|'enabled'|'disabled'} [checkboxes='hidden']
 */
export default function DataTable({
  columns,
  rows,
  rowKey,
  sortCol,
  sortDir,
  onSort,
  pageSizeOptions,
  maxRows,
  /** Filtered/source total for the footer (required for useful server pagination). */
  totalCount,
  pagination = 'client',
  /** 0-based page when pagination='server' (controlled). */
  page: pageControlled,
  /** Page size when pagination='server' (controlled). */
  pageSize: pageSizeControlled,
  onPageChange,
  onPageSizeChange,
  /** Change when search/status filters change so client mode returns to page 1. */
  pageResetKey,
  checkboxes = 'hidden',
  selectedIds,
  onSelectionChange,
  onRowClick,
  rowClassName,
  emptyContent,
  className = '',
  compact = false,
}) {
  const server = pagination === 'server'
  const options = useMemo(
    () => (pageSizeOptions?.length ? pageSizeOptions : buildPageSizeOptions(maxRows)),
    [pageSizeOptions, maxRows],
  )
  const [pageSizeLocal, setPageSizeLocal] = useState(() => options[0] || 10)
  const [pageLocal, setPageLocal] = useState(0)
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef(null)

  const pageSize = server ? (pageSizeControlled || options[0] || 10) : pageSizeLocal
  const page = server ? (pageControlled || 0) : pageLocal

  const showChecks = checkboxes === 'enabled' || checkboxes === 'disabled'
  const checksDisabled = checkboxes !== 'enabled'

  useEffect(() => {
    if (server) return
    if (!options.includes(pageSizeLocal)) {
      setPageSizeLocal(options[0] || 10)
      setPageLocal(0)
    }
  }, [server, options, pageSizeLocal])

  useEffect(() => {
    if (server) return
    setPageLocal(0)
  }, [server, pageResetKey])

  useEffect(() => {
    if (!menuOpen) return undefined
    const onDoc = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [menuOpen])

  const matchTotal = Number.isFinite(Number(totalCount))
    ? Math.max(0, Number(totalCount))
    : (rows?.length || 0)
  const pageCount = Math.max(1, Math.ceil(matchTotal / pageSize) || 1)
  const safePage = Math.min(page, pageCount - 1)
  const start = matchTotal === 0 ? 0 : safePage * pageSize
  const end = Math.min(start + pageSize, matchTotal)
  const pageRows = useMemo(() => {
    if (server) return rows || []
    return (rows || []).slice(start, end)
  }, [server, rows, start, end])

  useEffect(() => {
    if (server) return
    if (pageLocal !== safePage) setPageLocal(safePage)
  }, [server, pageLocal, safePage])

  const setPage = (next) => {
    if (server) onPageChange?.(next)
    else setPageLocal(next)
  }

  const setPageSize = (next) => {
    if (server) onPageSizeChange?.(next)
    else {
      setPageSizeLocal(next)
      setPageLocal(0)
    }
  }

  const selected = useMemo(() => new Set(selectedIds || []), [selectedIds])
  const pageIds = pageRows.map((r, i) => (rowKey ? rowKey(r, start + i) : start + i))
  const allPageSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id))
  const somePageSelected = pageIds.some((id) => selected.has(id))

  const toggleAllPage = () => {
    if (checksDisabled || !onSelectionChange) return
    const next = new Set(selected)
    if (allPageSelected) pageIds.forEach((id) => next.delete(id))
    else pageIds.forEach((id) => next.add(id))
    onSelectionChange([...next])
  }

  const toggleOne = (id) => {
    if (checksDisabled || !onSelectionChange) return
    const next = new Set(selected)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    onSelectionChange([...next])
  }

  const colSpan = columns.length + (showChecks ? 1 : 0)
  const empty = server ? matchTotal === 0 : (rows?.length || 0) === 0

  return (
    <div className={`data-table ${compact ? 'data-table--compact' : ''} ${className}`.trim()}>
      <div className="data-table__scroll">
        <table className="data-table__grid">
          <thead>
            <tr>
              {showChecks && (
                <th className="data-table__check-col" scope="col">
                  <input
                    type="checkbox"
                    checked={allPageSelected}
                    ref={(el) => {
                      if (el) el.indeterminate = !allPageSelected && somePageSelected
                    }}
                    disabled={checksDisabled || pageIds.length === 0}
                    onChange={toggleAllPage}
                    aria-label="Select all rows on this page"
                  />
                </th>
              )}
              {columns.map((col) => {
                const sortable = col.sortable !== false && !!onSort && !!col.key
                const active = sortable && sortCol === col.key
                return (
                  <th
                    key={col.key || col.label}
                    scope="col"
                    className={sortable ? 'data-table__th--sortable' : undefined}
                    style={col.headerStyle}
                    onClick={sortable ? () => onSort(col.key) : undefined}
                    aria-sort={active ? (sortDir === 'asc' ? 'ascending' : 'descending') : undefined}
                  >
                    <span className="data-table__th-inner">
                      {col.label}
                      {sortable && <SortCue active={active} dir={sortDir} />}
                    </span>
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {pageRows.map((row, i) => {
              const absoluteIndex = start + i
              const id = rowKey ? rowKey(row, absoluteIndex) : absoluteIndex
              const extraClass = typeof rowClassName === 'function' ? rowClassName(row) : (rowClassName || '')
              return (
                <tr
                  key={String(id)}
                  className={String(extraClass || '').trim()}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                >
                  {showChecks && (
                    <td className="data-table__check-col" onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        checked={selected.has(id)}
                        disabled={checksDisabled}
                        onChange={() => toggleOne(id)}
                        aria-label={`Select row ${absoluteIndex + 1}`}
                      />
                    </td>
                  )}
                  {columns.map((col) => (
                    <td key={col.key || col.label} style={col.cellStyle} className={col.cellClassName}>
                      {col.render ? col.render(row, absoluteIndex) : (row[col.key] ?? '—')}
                    </td>
                  ))}
                </tr>
              )
            })}
            {empty && (
              <tr>
                <td colSpan={colSpan} className="muted data-table__empty">
                  {emptyContent || 'No rows.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="data-table__footer">
        <div className="data-table__pager" ref={menuRef}>
          <button
            type="button"
            className="data-table__page-size"
            aria-haspopup="listbox"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((o) => !o)}
          >
            {pageSize} per page
            <span aria-hidden="true">▾</span>
          </button>
          {menuOpen && (
            <ul className="data-table__page-menu" role="listbox" aria-label="Rows per page">
              {options.map((n) => (
                <li key={n} role="option" aria-selected={n === pageSize}>
                  <button
                    type="button"
                    className={n === pageSize ? 'is-selected' : undefined}
                    onClick={() => {
                      setPageSize(n)
                      setMenuOpen(false)
                    }}
                  >
                    <span className="data-table__page-check" aria-hidden="true">
                      {n === pageSize ? '✓' : ''}
                    </span>
                    {n} per page
                  </button>
                </li>
              ))}
            </ul>
          )}
          <span className="data-table__range" aria-live="polite">
            {pageRangeLabel({
              rowCount: matchTotal,
              page: safePage,
              pageSize,
              totalCount: matchTotal,
            })}
          </span>
          <button
            type="button"
            className="data-table__nav"
            disabled={safePage <= 0}
            onClick={() => setPage(Math.max(0, safePage - 1))}
            aria-label="Previous page"
          >
            ‹
          </button>
          <button
            type="button"
            className="data-table__nav"
            disabled={safePage >= pageCount - 1 || matchTotal === 0}
            onClick={() => setPage(Math.min(pageCount - 1, safePage + 1))}
            aria-label="Next page"
          >
            ›
          </button>
        </div>
      </div>
    </div>
  )
}
