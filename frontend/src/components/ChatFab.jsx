import React, { useCallback, useEffect, useRef, useState } from 'react'

const FAB_SIZE = 56
const DEFAULT_RIGHT = 28
/** Slightly lower than the previous 28px bottom offset. */
const DEFAULT_BOTTOM = 14
const DRAG_THRESHOLD_PX = 6
const STORAGE_KEY = 'amq-assist-fab-pos'

function clamp(n, min, max) {
  return Math.min(max, Math.max(min, n))
}

function defaultPos() {
  if (typeof window === 'undefined') return { x: 0, y: 0 }
  return {
    x: window.innerWidth - DEFAULT_RIGHT - FAB_SIZE,
    y: window.innerHeight - DEFAULT_BOTTOM - FAB_SIZE,
  }
}

function loadPos() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return defaultPos()
    const parsed = JSON.parse(raw)
    if (typeof parsed?.x !== 'number' || typeof parsed?.y !== 'number') return defaultPos()
    return clampPos(parsed)
  } catch {
    return defaultPos()
  }
}

function clampPos({ x, y }) {
  if (typeof window === 'undefined') return { x, y }
  const maxX = Math.max(0, window.innerWidth - FAB_SIZE)
  const maxY = Math.max(0, window.innerHeight - FAB_SIZE)
  return { x: clamp(x, 0, maxX), y: clamp(y, 0, maxY) }
}

function savePos(pos) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(pos))
  } catch {
    /* ignore quota / private mode */
  }
}

/**
 * Fixed circular AMQ Assist launcher (draggable).
 */
export default function ChatFab({ onOpen }) {
  const [pos, setPos] = useState(loadPos)
  const [dragging, setDragging] = useState(false)
  const dragRef = useRef(null)

  useEffect(() => {
    const onResize = () => setPos((p) => clampPos(p))
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const onPointerDown = useCallback((e) => {
    if (e.button != null && e.button !== 0) return
    const el = e.currentTarget
    el.setPointerCapture?.(e.pointerId)
    dragRef.current = {
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      originX: pos.x,
      originY: pos.y,
      moved: false,
    }
    setDragging(true)
  }, [pos.x, pos.y])

  const onPointerMove = useCallback((e) => {
    const d = dragRef.current
    if (!d || d.pointerId !== e.pointerId) return
    const dx = e.clientX - d.startX
    const dy = e.clientY - d.startY
    if (!d.moved && (Math.abs(dx) > DRAG_THRESHOLD_PX || Math.abs(dy) > DRAG_THRESHOLD_PX)) {
      d.moved = true
    }
    if (!d.moved) return
    setPos(clampPos({ x: d.originX + dx, y: d.originY + dy }))
  }, [])

  const endDrag = useCallback((e) => {
    const d = dragRef.current
    if (!d || d.pointerId !== e.pointerId) return
    dragRef.current = null
    setDragging(false)
    try {
      e.currentTarget.releasePointerCapture?.(e.pointerId)
    } catch {
      /* already released */
    }
    if (d.moved) {
      setPos((p) => {
        const next = clampPos(p)
        savePos(next)
        return next
      })
      return
    }
    onOpen?.()
  }, [onOpen])

  return (
    <button
      type="button"
      className={`chat-fab${dragging ? ' is-dragging' : ''}`}
      style={{ left: pos.x, top: pos.y, right: 'auto', bottom: 'auto' }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      title="Open AMQ Assist (drag to move)"
      aria-label="Open AMQ Assist"
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
