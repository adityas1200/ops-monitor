import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { buildPageSizeOptions, pageRangeLabel } from './pageSizeOptions.js'

describe('buildPageSizeOptions', () => {
  it('ends with 500 and defaults start at 10', () => {
    assert.deepEqual(buildPageSizeOptions(), [10, 25, 50, 100, 500])
  })
})

describe('pageRangeLabel', () => {
  it('uses totalCount for server pagination', () => {
    assert.equal(
      pageRangeLabel({ rowCount: 10, page: 0, pageSize: 10, totalCount: 1684 }),
      '1-10 of 1684',
    )
  })

  it('advances the range on later pages', () => {
    assert.equal(
      pageRangeLabel({ rowCount: 10, page: 1, pageSize: 10, totalCount: 1684 }),
      '11-20 of 1684',
    )
  })

  it('falls back to rowCount when totalCount is omitted', () => {
    assert.equal(
      pageRangeLabel({ rowCount: 600, page: 0, pageSize: 10 }),
      '1-10 of 600',
    )
  })
})
