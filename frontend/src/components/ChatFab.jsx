import React from 'react'

/**
 * Fixed circular AMQ Assist launcher.
 */
export default function ChatFab({ onOpen }) {
  return (
    <button
      type="button"
      className="chat-fab"
      onClick={onOpen}
      title="Open AMQ Assist"
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
