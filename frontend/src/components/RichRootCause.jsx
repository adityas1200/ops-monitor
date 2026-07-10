import React from 'react'

const ENTITY_ICONS = {
  table: '⊞',
  view: '⊟',
  procedure: '⊡',
  column: '⊘',
}

function parseExplanation(text) {
  if (!text) return [{ type: 'text', value: '' }]
  const parts = []
  const regex = /\{\{(table|view|procedure|code|column):([^}]+)\}\}/g
  let lastIndex = 0
  let match

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push({ type: 'text', value: text.slice(lastIndex, match.index) })
    }
    parts.push({ type: match[1], value: match[2] })
    lastIndex = match.index + match[0].length
  }
  if (lastIndex < text.length) {
    parts.push({ type: 'text', value: text.slice(lastIndex) })
  }
  return parts
}

export default function RichRootCause({ rootCause }) {
  if (!rootCause) return null

  const parts = parseExplanation(rootCause.explanation)

  return (
    <div className="rich-root-cause">
      <h4 className="rrc-heading">Root Cause</h4>
      <p className="rrc-explanation">
        {parts.map((part, i) => {
          if (part.type === 'text') {
            return <span key={i}>{part.value}</span>
          }
          if (part.type === 'code') {
            return (
              <code key={i} className="code-highlight code-highlight--problem">
                {part.value}
              </code>
            )
          }
          // entity types: table, view, procedure, column
          const icon = ENTITY_ICONS[part.type] || ENTITY_ICONS.table
          return (
            <span key={i} className={`entity-badge entity-badge--${part.type}`}>
              <span className="entity-badge__icon">{icon}</span>
              {part.value}
            </span>
          )
        })}
      </p>

      {rootCause.code_snippets?.length > 0 && (
        <div className="rrc-snippets">
          {rootCause.code_snippets.map((snippet, i) => (
            <div key={i} className={`rrc-snippet rrc-snippet--${snippet.status || 'neutral'}`}>
              <code>{snippet.code}</code>
              {snippet.context && (
                <span className="rrc-snippet__context">{snippet.context}</span>
              )}
            </div>
          ))}
        </div>
      )}

      {rootCause.comparison && (
        <div className="rrc-comparison">
          <div className="rrc-comparison__expected">
            <span className="rrc-comparison__label">Expected:</span>
            {rootCause.comparison.expected}
          </div>
          <div className="rrc-comparison__actual">
            <span className="rrc-comparison__label">Actual:</span>
            {rootCause.comparison.actual}
          </div>
        </div>
      )}
    </div>
  )
}
