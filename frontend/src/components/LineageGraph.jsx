import React from 'react'

const COLORS = {
  root_cause: '#ff5c6c', failed: '#ff8a3d', impacted: '#ffb547', healthy: '#2ecc71',
}

// layered left->right layout using longest-path levels
function layout(nodes, edges) {
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]))
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
  const pos = {}
  const COLW = 200, ROWH = 84
  Object.entries(cols).forEach(([lvl, ids]) => {
    ids.forEach((id, i) => {
      pos[id] = { x: 30 + Number(lvl) * COLW, y: 30 + i * ROWH }
    })
  })
  const width = 60 + (Math.max(...Object.values(level)) + 1) * COLW
  const height = 60 + Math.max(...Object.values(cols).map((c) => c.length)) * ROWH
  return { pos, width, height }
}

export default function LineageGraph({ lineage }) {
  if (!lineage?.nodes?.length) return <div className="muted">No lineage data.</div>
  const { nodes, edges } = lineage
  const { pos, width, height } = layout(nodes, edges)
  const W = 150, H = 46

  return (
    <div>
      <div className="node-legend">
        <span><span className="dot" style={{ background: COLORS.root_cause }} />Root cause</span>
        <span><span className="dot" style={{ background: COLORS.failed }} />Failed</span>
        <span><span className="dot" style={{ background: COLORS.impacted }} />Impacted (downstream)</span>
        <span><span className="dot" style={{ background: COLORS.healthy }} />Healthy</span>
      </div>
      <svg className="lineage" width="100%" viewBox={`0 0 ${Math.max(width, 400)} ${Math.max(height, 200)}`}>
        <defs>
          <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L7,3 L0,6 Z" fill="#5a6b85" />
          </marker>
        </defs>
        {edges.map((e, i) => {
          const a = pos[e.from], b = pos[e.to]
          if (!a || !b) return null
          const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2
          const mx = (x1 + x2) / 2
          return <path key={i} d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
            fill="none" stroke="#5a6b85" strokeWidth="1.5" markerEnd="url(#arrow)" />
        })}
        {nodes.map((n) => {
          const p = pos[n.id]
          if (!p) return null
          const c = COLORS[n.state] || '#6b7689'
          return (
            <g key={n.id}>
              <rect x={p.x} y={p.y} width={W} height={H} rx="8"
                fill="#161d2e" stroke={c} strokeWidth={n.state === 'root_cause' ? 3 : 1.6} />
              <text x={p.x + 10} y={p.y + 18} fill="#e6ecf5" fontSize="11" fontWeight="600">
                {n.label.length > 22 ? n.label.slice(0, 21) + '…' : n.label}
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
