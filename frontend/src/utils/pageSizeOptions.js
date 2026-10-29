const PAGE_SIZE_STEPS = [10, 25, 50, 100, 500]

/** Per-page choices; first/default is 10, last is 500. */
export function buildPageSizeOptions() {
  return [...PAGE_SIZE_STEPS]
}

/**
 * Footer label: current page slice vs total records.
 * page is 0-based. Prefer totalCount when provided (server pagination).
 */
export function pageRangeLabel({ rowCount, page, pageSize, totalCount }) {
  const size = Math.max(1, Number(pageSize) || 10)
  const parsedTotal = Number(totalCount)
  const total = Number.isFinite(parsedTotal) && parsedTotal >= 0
    ? parsedTotal
    : Math.max(0, Number(rowCount) || 0)
  if (total === 0) return '0 of 0'
  const pageCount = Math.max(1, Math.ceil(total / size))
  const safePage = Math.min(Math.max(0, Number(page) || 0), pageCount - 1)
  const start = safePage * size
  const end = Math.min(start + size, total)
  return `${start + 1}-${end} of ${total}`
}
