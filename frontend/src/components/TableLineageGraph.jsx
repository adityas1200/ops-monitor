import React, { useMemo, useState } from 'react'

const COLORS = {
  root_cause: '#ff5c6c',
  failed:     '#ff8a3d',
  impacted:   '#ffb547',
  healthy:    '#2ecc71',
  source:     '#4f8cff',
}

const COLORS_UPSTREAM = {
  root_cause: '#ff5c6c',
  failed:     '#8b95a5',
  impacted:   '#8b95a5',
  healthy:    '#8b95a5',
  source:     '#b0b8c4',
}

const COLORS_DOWNSTREAM = {
  root_cause: '#ff5c6c',
  failed:     '#e74c3c',
  impacted:   '#e74c3c',
  healthy:    '#ffb547',
  source:     '#8b95a5',
}

const NODE_SIZES = {
  table:     { w: 132, h: 28 },
  task:      { w: 138, h: 34 },
  view:      { w: 132, h: 28 },
  procedure: { w: 132, h: 28 },
}

/** Prefer the last segment of an FQN / task name for readable chips. */
function shortLabel(raw, max = 20) {
  if (!raw) return ''
  let text = String(raw).replace(/^SF Task:\s*/i, '').trim()
  if (text.includes('.')) {
    const parts = text.split('.').filter(Boolean)
    text = parts[parts.length - 1] || text
  }
  if (text.length <= max) return text
  return `${text.slice(0, max - 1)}…`
}

function fullLabel(n) {
  return n.label || n.name || n.id || ''
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
  queue.forEach((id) => { level[id] = 0 })
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
  const COLW = 158
  const ROWH = 40
  Object.entries(cols).forEach(([lvl, ids]) => {
    ids.forEach((id, i) => {
      pos[id] = { x: 16 + Number(lvl) * COLW, y: 14 + i * ROWH }
    })
  })
  const maxLevel = Math.max(0, ...Object.values(level))
  const maxCol = Math.max(0, ...Object.values(cols).map((c) => c.length))
  const width = 32 + (maxLevel + 1) * COLW
  const height = 28 + maxCol * ROWH
  return { pos, width, height }
}

function LineageGraphSvg({ nodes, edges, colorMap, markerId }) {
  const colors = colorMap || COLORS
  const { pos, width, height } = useMemo(() => layout(nodes, edges), [nodes, edges])
  const mid = markerId || 'tl-arrow'

  return (
    <svg
      className="lineage table-lineage"
      width={Math.max(width, 280)}
      height={Math.max(height, 80)}
      viewBox={`0 0 ${Math.max(width, 280)} ${Math.max(height, 80)}`}
    >
      <defs>
        <marker id={mid} markerWidth="6" markerHeight="6" refX="5" refY="2.5" orient="auto">
          <path d="M0,0 L5,2.5 L0,5 Z" fill="#5a6b85" />
        </marker>
      </defs>

      {edges.map((e, i) => {
        const fromNode = nodes.find((n) => n.id === e.from)
        const toNode = nodes.find((n) => n.id === e.to)
        if (!fromNode || !toNode) return null
        const a = pos[e.from]
        const b = pos[e.to]
        if (!a || !b) return null
        const fSize = NODE_SIZES[fromNode.type] || NODE_SIZES.table
        const tSize = NODE_SIZES[toNode.type] || NODE_SIZES.table
        const x1 = a.x + fSize.w
        const y1 = a.y + fSize.h / 2
        const x2 = b.x
        const y2 = b.y + tSize.h / 2
        const mx = (x1 + x2) / 2
        const dashed = fromNode.type === 'task' || toNode.type === 'task'
          || toNode.type === 'view' || toNode.type === 'procedure'
        return (
          <path
            key={i}
            d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
            fill="none"
            stroke="#5a6b85"
            strokeWidth="1"
            markerEnd={`url(#${mid})`}
            strokeDasharray={dashed ? '3,2' : 'none'}
            opacity={0.75}
          />
        )
      })}

      {nodes.map((n) => {
        const p = pos[n.id]
        if (!p) return null
        const c = colors[n.state] || '#6b7689'
        const size = NODE_SIZES[n.type] || NODE_SIZES.table
        const full = fullLabel(n)
        const label = shortLabel(full, n.type === 'task' ? 18 : 18)
        const title = full
        const isRoot = n.state === 'root_cause'

        if (n.type === 'task') {
          return (
            <g key={n.id}>
              <title>{title}</title>
              <rect
                x={p.x} y={p.y} width={size.w} height={size.h} rx="6"
                fill="var(--code-bg)" stroke={c}
                strokeWidth={isRoot ? 2 : 1.2}
              />
              <text x={p.x + 8} y={p.y + 13}
                fill="var(--text)" fontSize="10" fontWeight="600">{label}</text>
              <text x={p.x + 8} y={p.y + 25}
                fill={c} fontSize="8">
                {(n.platform || 'snowflake')} · {(n.state || '').replace('_', ' ')}
              </text>
            </g>
          )
        }

        const dash = n.type === 'view' ? '4,2' : n.type === 'procedure' ? '2,2' : undefined
        const rx = n.type === 'procedure' ? 5 : size.h / 2
        return (
          <g key={n.id}>
            <title>{title}</title>
            <rect
              x={p.x} y={p.y} width={size.w} height={size.h} rx={rx}
              fill="var(--code-bg)" stroke={c}
              strokeWidth={isRoot ? 2 : 1.2}
              strokeDasharray={dash}
            />
            <text
              x={p.x + 10} y={p.y + size.h / 2 + 3.5}
              fill="var(--text)" fontSize="10" fontWeight={n.type === 'table' ? 600 : 500}
            >
              {label}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

/**
 * Compact, scrollable lineage graph for upstream / downstream RCA sections.
 */
export default function TableLineageGraph({
  tableLineage,
  title,
  direction,
  hideTitle = false,
  defaultOpen = true,
  colorScheme,
}) {
  const [open, setOpen] = useState(defaultOpen)

  const colorMap = colorScheme === 'upstream' ? COLORS_UPSTREAM
    : colorScheme === 'downstream' ? COLORS_DOWNSTREAM
    : COLORS

  if (!tableLineage?.nodes?.length) return null

  const { nodes, edges } = tableLineage
  const nodeCount = nodes.length
  const markerId = `tl-arrow-${direction || colorScheme || 'default'}`

  const directionLabel =
    direction === 'upstream'
      ? `${nodeCount} upstream`
      : direction === 'downstream'
      ? `${nodeCount} downstream`
      : `${nodeCount} nodes`

  if (hideTitle) {
    return (
      <div className="table-lineage-scroll">
        <LineageGraphSvg nodes={nodes} edges={edges} colorMap={colorMap} markerId={markerId} />
      </div>
    )
  }

  const sectionTitle = title
    || (direction === 'upstream' ? 'Upstream lineage'
      : direction === 'downstream' ? 'Downstream impact'
      : 'Table Lineage')

  return (
    <div className="table-lineage-section">
      <div className="table-lineage-head" onClick={() => setOpen((o) => !o)}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontWeight: 600, fontSize: 13 }}>{sectionTitle}</span>
          <span className="muted" style={{ fontSize: 12 }}>{directionLabel}</span>
        </div>
        <span className="muted" style={{ fontSize: 13 }}>{open ? '∨' : '›'}</span>
      </div>

      {open && (
        <div className="table-lineage-body">
          <div className="node-legend node-legend--compact">
            <span><span className="dot" style={{ background: COLORS.root_cause }} />Root</span>
            <span><span className="dot" style={{ background: COLORS.failed }} />Failed</span>
            <span><span className="dot" style={{ background: COLORS.impacted }} />Impacted</span>
            <span><span className="dot" style={{ background: COLORS.source }} />Source</span>
            <span className="muted" style={{ fontSize: 11 }}>Hover a node for full name · scroll if needed</span>
          </div>
          <div className="table-lineage-scroll">
            <LineageGraphSvg nodes={nodes} edges={edges} colorMap={colorMap} markerId={markerId} />
          </div>
        </div>
      )}
    </div>
  )
}
