import React from 'react'

const COLORS = {
  root_cause: '#ff5c6c', failed: '#ff8a3d', impacted: '#ffb547', healthy: '#2ecc71',
}

const PAD_X = 16
const CHAR_W = 7.2
const MIN_W = 140
const H = 46
const COL_GAP = 40
const ROW_H = 84

function fullLabel(n) {
  return String(n.label || n.name || n.id || '').replace(/^SF Task:\s*/i, '').trim()
}

function nodeWidth(n) {
  const label = fullLabel(n)
  return Math.max(MIN_W, Math.ceil(label.length * CHAR_W) + PAD_X * 2)
}

// layered left->right layout using longest-path levels
function layout(nodes, edges) {
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]))
  const widths = Object.fromEntries(nodes.map((n) => [n.id, nodeWidth(n)]))
  const incoming = {}, adj = {}
  nodes.forEach((n) => { incoming[n.id] = 0; adj[n.id] = [] })
  edges.forEach((e) => { if (byId[e.from] && byId[e.to]) { adj[e.from].push(e.to); incoming[e.to]++ } })

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

  const colWidths = {}
  Object.entries(cols).forEach(([lvl, ids]) => {
    colWidths[lvl] = Math.max(...ids.map((id) => widths[id]), MIN_W)
  })

  const colX = {}
  let xCursor = 30
  const maxLevel = Math.max(0, ...Object.values(level))
  for (let lvl = 0; lvl <= maxLevel; lvl++) {
    colX[lvl] = xCursor
    xCursor += (colWidths[lvl] || MIN_W) + COL_GAP
  }

  const pos = {}
  Object.entries(cols).forEach(([lvl, ids]) => {
    ids.forEach((id, i) => {
      pos[id] = { x: colX[lvl], y: 30 + i * ROW_H, w: widths[id] }
    })
  })
  const width = xCursor + 20
  const height = 60 + Math.max(...Object.values(cols).map((c) => c.length), 1) * ROW_H
  return { pos, width, height }
}

export default function LineageGraph({ lineage }) {
  if (!lineage?.nodes?.length) return <div className="muted">No lineage data.</div>
  const { nodes, edges } = lineage
  const { pos, width, height } = layout(nodes, edges)

  return (
    <div style={{ overflow: 'auto', maxWidth: '100%' }}>
      <div className="node-legend">
        <span><span className="dot" style={{ background: COLORS.root_cause }} />Root cause</span>
        <span><span className="dot" style={{ background: COLORS.failed }} />Failed</span>
        <span><span className="dot" style={{ background: COLORS.impacted }} />Impacted (downstream)</span>
        <span><span className="dot" style={{ background: COLORS.healthy }} />Healthy</span>
      </div>
      <svg className="lineage" width={Math.max(width, 400)} height={Math.max(height, 200)}
        viewBox={`0 0 ${Math.max(width, 400)} ${Math.max(height, 200)}`}>
        <defs>
          <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L7,3 L0,6 Z" fill="#5a6b85" />
          </marker>
        </defs>
        {edges.map((e, i) => {
          const a = pos[e.from], b = pos[e.to]
          if (!a || !b) return null
          const x1 = a.x + a.w, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2
          const mx = (x1 + x2) / 2
          return <path key={i} d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
            fill="none" stroke="#5a6b85" strokeWidth="1.5" markerEnd="url(#arrow)" />
        })}
        {nodes.map((n) => {
          const p = pos[n.id]
          if (!p) return null
          const c = COLORS[n.state] || '#6b7689'
          const label = fullLabel(n)
          return (
            <g key={n.id}>
              <title>{label}</title>
              <rect x={p.x} y={p.y} width={p.w} height={H} rx="8"
                fill="#161d2e" stroke={c} strokeWidth={n.state === 'root_cause' ? 3 : 1.6} />
              <text x={p.x + 10} y={p.y + 18} fill="#e6ecf5" fontSize="11" fontWeight="600">
                {label}
              </text>
              <text x={p.x + 10} y={p.y + 34} fill={c} fontSize="9.5">
                {n.platform} · {n.state.replace('_', ' ')}
              </text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}
