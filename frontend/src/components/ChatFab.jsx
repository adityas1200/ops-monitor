import React, { useRef, useState } from 'react'

const FAB_SIZE = 56
const MARGIN = 20

/**
 * Draggable circular Agent Chat launcher (fixed, high z-index).
 * Click opens chat; drag moves the button without opening.
 */
export default function ChatFab({ onOpen }) {
  const [pos, setPos] = useState(null) // { left, top } after first drag; null = CSS bottom-right
  const drag = useRef({
    active: false,
    moved: false,
    pointerId: null,
    startX: 0,
    startY: 0,
    originLeft: 0,
    originTop: 0,
  })

  const resolveOrigin = (el) => {
    if (pos) return { left: pos.left, top: pos.top }
    const rect = el.getBoundingClientRect()
    return { left: rect.left, top: rect.top }
  }

  const clamp = (left, top) => {
    const maxL = Math.max(MARGIN, window.innerWidth - FAB_SIZE - MARGIN)
    const maxT = Math.max(MARGIN, window.innerHeight - FAB_SIZE - MARGIN)
    return {
      left: Math.min(maxL, Math.max(MARGIN, left)),
      top: Math.min(maxT, Math.max(MARGIN, top)),
    }
  }

  const onPointerDown = (e) => {
    if (e.button != null && e.button !== 0) return
    const el = e.currentTarget
    const origin = resolveOrigin(el)
    drag.current = {
      active: true,
      moved: false,
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      originLeft: origin.left,
      originTop: origin.top,
    }
    el.setPointerCapture?.(e.pointerId)
  }

  const onPointerMove = (e) => {
    const d = drag.current
    if (!d.active || d.pointerId !== e.pointerId) return
    const dx = e.clientX - d.startX
    const dy = e.clientY - d.startY
    if (!d.moved && (Math.abs(dx) > 4 || Math.abs(dy) > 4)) d.moved = true
    if (!d.moved) return
    setPos(clamp(d.originLeft + dx, d.originTop + dy))
  }

  const endPointer = (e) => {
    const d = drag.current
    if (!d.active || (e.pointerId != null && d.pointerId !== e.pointerId)) return
    const wasDrag = d.moved
    d.active = false
    d.pointerId = null
    e.currentTarget.releasePointerCapture?.(e.pointerId)
    if (!wasDrag) onOpen?.()
  }

  const style = pos
    ? { left: pos.left, top: pos.top, right: 'auto', bottom: 'auto' }
    : undefined

  return (
    <button
      type="button"
      className={`chat-fab${pos ? ' chat-fab--placed' : ''}`}
      style={style}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endPointer}
      onPointerCancel={endPointer}
      title="Open Agent Chat"
      aria-label="Open Agent Chat"
    >
      <span className="chat-fab__icon" aria-hidden="true">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none">
          {/* Chatbot / agent: antenna + head + face */}
          <path
            d="M12 3.2v2.1"
            stroke="currentColor"
            strokeWidth="1.7"
            strokeLinecap="round"
          />
          <circle cx="12" cy="2.6" r="1.1" fill="currentColor" />
          <rect
            x="5"
            y="5.5"
            width="14"
            height="11"
            rx="4"
            stroke="currentColor"
            strokeWidth="1.7"
          />
          <circle cx="9.2" cy="10.2" r="1.15" fill="currentColor" />
          <circle cx="14.8" cy="10.2" r="1.15" fill="currentColor" />
          <path
            d="M9.2 13.4c.7.7 1.6 1.05 2.8 1.05s2.1-.35 2.8-1.05"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
          />
          <path
            d="M9.5 19.2 12 16.8l2.5 2.4"
            stroke="currentColor"
            strokeWidth="1.7"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </span>
    </button>
  )
}
