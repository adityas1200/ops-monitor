import React, { useState } from 'react'

const COLORS = {
  root_cause: '#ff5c6c',
  failed:     '#ff8a3d',
  impacted:   '#ffb547',
  healthy:    '#2ecc71',
  source:     '#4f8cff',   // raw / seed tables — blue
}

const NODE_SIZES = {
  table:     { w: 170, h: 40 },
  task:      { w: 155, h: 38 },
  view:      { w: 170, h: 40 },
  procedure: { w: 170, h: 40 },
}

function layout(nodes, edges) {
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]))
  const incoming = {}
  const adj = {}
  nodes.forEach((n) => { incoming[n.id] = 0; adj[n.id] = [] })
  edges.forEach((e) => {
    if (byId[e.from] && byId[e.to]) {
      adj[e.from].push(e.to)
      incoming[e.to]++
    }
  })

  const level = {}
  const queue = nodes.filter((n) => incoming[n.id] === 0).map((n) => n.id)
  queue.forEach((id) => (level[id] = 0))
  const indeg = { ...incoming }
  const q = [...queue]
  while (q.length) {
    const id = q.shift()
    adj[id].forEach((to) => {
      level[to] = Math.max(level[to] ?? 0, (level[id] ?? 0) + 1)
      if (--indeg[to] === 0) q.push(to)
    })
  }
  nodes.forEach((n) => { if (level[n.id] == null) level[n.id] = 0 })

  const cols = {}
  nodes.forEach((n) => { (cols[level[n.id]] ||= []).push(n.id) })
  const pos = {}
  const COLW = 210
  const ROWH = 64
  Object.entries(cols).forEach(([lvl, ids]) => {
    ids.forEach((id, i) => {
      pos[id] = { x: 30 + Number(lvl) * COLW, y: 30 + i * ROWH }
    })
  })
  const maxLevel = Math.max(0, ...Object.values(level))
  const maxCol = Math.max(0, ...Object.values(cols).map((c) => c.length))
  const width = 60 + (maxLevel + 1) * COLW
  const height = 60 + maxCol * ROWH
  return { pos, width, height }
}

function LineageGraphSvg({ nodes, edges }) {
  const { pos, width, height } = layout(nodes, edges)

  return (
    <svg
      className="lineage table-lineage"
      width="100%"
      viewBox={`0 0 ${Math.max(width, 400)} ${Math.max(height, 160)}`}
    >
      <defs>
        <marker id="tl-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" fill="#5a6b85" />
        </marker>
      </defs>

      {edges.map((e, i) => {
        const fromNode = nodes.find((n) => n.id === e.from)
        const toNode   = nodes.find((n) => n.id === e.to)
        if (!fromNode || !toNode) return null
        const a = pos[e.from]; const b = pos[e.to]
        if (!a || !b) return null
        const fSize = NODE_SIZES[fromNode.type] || NODE_SIZES.table
        const tSize = NODE_SIZES[toNode.type]   || NODE_SIZES.table
        const x1 = a.x + fSize.w; const y1 = a.y + fSize.h / 2
        const x2 = b.x;           const y2 = b.y + tSize.h / 2
        const mx = (x1 + x2) / 2
        return (
          <path key={i}
            d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
            fill="none" stroke="#5a6b85" strokeWidth="1.2"
            markerEnd="url(#tl-arrow)"
            strokeDasharray={
              fromNode.type === 'task' || toNode.type === 'task' ||
              toNode.type === 'view' || toNode.type === 'procedure' ? '4,3' : 'none'
            }
          />
        )
      })}

      {nodes.map((n) => {
        const p = pos[n.id]
        if (!p) return null
        const c = COLORS[n.state] || '#6b7689'
        const size = NODE_SIZES[n.type] || NODE_SIZES.table
        // Use label (task-graph nodes) or name (table-graph nodes)
        const text = n.label || n.name || n.id
        const label = text.length > 24 ? text.slice(0, 23) + '…' : text

        if (n.type === 'table') {
          return (
            <g key={n.id}>
              <rect x={p.x} y={p.y} width={size.w} height={size.h} rx={size.h / 2}
                fill="var(--code-bg)" stroke={c}
                strokeWidth={n.state === 'root_cause' ? 2.5 : 1.4} />
              <text x={p.x + 14} y={p.y + size.h / 2 + 4}
                fill="var(--text)" fontSize="10.5" fontWeight="600">{label}</text>
            </g>
          )
        }
        if (n.type === 'view') {
          return (
            <g key={n.id}>
              <rect x={p.x} y={p.y} width={size.w} height={size.h} rx={size.h / 2}
                fill="var(--code-bg)" stroke={c} strokeWidth={1.4} strokeDasharray="5,3" />
              <text x={p.x + 14} y={p.y + size.h / 2 + 4}
                fill="var(--text)" fontSize="10.5" fontWeight="500">{label}</text>
            </g>
          )
        }
        if (n.type === 'procedure') {
          return (
            <g key={n.id}>
              <rect x={p.x} y={p.y} width={size.w} height={size.h} rx="6"
                fill="var(--code-bg)" stroke={c} strokeWidth={1.4} strokeDasharray="2,2" />
              <text x={p.x + 14} y={p.y + size.h / 2 + 4}
                fill="var(--text)" fontSize="10.5" fontWeight="500">{label}</text>
            </g>
          )
        }
        // task node
        return (
          <g key={n.id}>
            <rect x={p.x} y={p.y} width={size.w} height={size.h} rx="8"
              fill="var(--code-bg)" stroke={c}
              strokeWidth={n.state === 'root_cause' ? 2.5 : 1.4} />
            <text x={p.x + 10} y={p.y + size.h / 2 - 3}
              fill="var(--text)" fontSize="10.5" fontWeight="600">{label}</text>
            <text x={p.x + 10} y={p.y + size.h / 2 + 10}
              fill={c} fontSize="9">{n.platform || 'snowflake'} · {(n.state || '').replace('_', ' ')}</text>
          </g>
        )
      })}
    </svg>
  )
}

/**
 * TableLineageGraph — renders a table/task lineage graph with an optional
 * collapsible header. Used for both the legacy "Table Lineage" section and
 * the new upstream / downstream sections.
 *
 * Props:
 *   tableLineage  {nodes, edges}   — graph data
 *   title         string           — section header text (default "Table Lineage")
 *   direction     "upstream"|"downstream"|undefined — optional badge
 *   hideTitle     bool             — render SVG directly, no collapsible wrapper
 *   defaultOpen   bool             — start expanded (default true)
 */
export default function TableLineageGraph({
  tableLineage,
  title,
  direction,
  hideTitle = false,
  defaultOpen = true,
}) {
  const [open, setOpen] = useState(defaultOpen)

  if (!tableLineage?.nodes?.length) return null

  const { nodes, edges } = tableLineage
  const nodeCount = nodes.length

  const directionLabel =
    direction === 'upstream'
      ? `${nodeCount} upstream assets`
      : direction === 'downstream'
      ? `${nodeCount} downstream assets`
      : null

  if (hideTitle) {
    return <LineageGraphSvg nodes={nodes} edges={edges} />
  }

  const sectionTitle = title || 'Table Lineage'

  return (
    <div
      className="table-lineage-section"
      style={{
        border: '1px solid var(--border)',
        borderRadius: 10,
        marginBottom: 10,
        overflow: 'hidden',
      }}
    >
      {/* Collapsible header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '10px 14px',
          cursor: 'pointer',
          background: 'var(--panel2)',
          userSelect: 'none',
        }}
        onClick={() => setOpen((o) => !o)}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <rect x="1" y="1" width="12" height="12" rx="2"
              stroke="var(--muted)" strokeWidth="1.4" />
            <line x1="1" y1="4.5" x2="13" y2="4.5"
              stroke="var(--muted)" strokeWidth="1.2" />
            <line x1="4.5" y1="1" x2="4.5" y2="13"
              stroke="var(--muted)" strokeWidth="1.2" />
          </svg>
          <span style={{ fontWeight: 600, fontSize: 13 }}>{sectionTitle}</span>
          {directionLabel && (
            <span style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 4 }}>
              {directionLabel}
            </span>
          )}
        </div>
        <span style={{ color: 'var(--muted)', fontSize: 13, lineHeight: 1 }}>
          {open ? '∨' : '›'}
        </span>
      </div>

      {open && (
        <div style={{ padding: '10px 14px 14px' }}>
          {/* Legend */}
          <div className="node-legend" style={{ marginBottom: 8 }}>
            <span><span className="dot" style={{ background: COLORS.root_cause }} />Root cause</span>
            <span><span className="dot" style={{ background: COLORS.failed }} />Failed</span>
            <span><span className="dot" style={{ background: COLORS.impacted }} />Impacted</span>
            <span><span className="dot" style={{ background: COLORS.source }} />Source / raw</span>
            <span><span className="dot" style={{ background: COLORS.healthy }} />Healthy</span>
            <span style={{ marginLeft: 12, fontSize: 11, color: 'var(--muted)' }}>
              scroll to zoom · drag to pan · double-click to reset
            </span>
          </div>
          <LineageGraphSvg nodes={nodes} edges={edges} />
        </div>
      )}
    </div>
  )
}
