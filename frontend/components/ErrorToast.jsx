// ErrorToast.jsx
// ==============
// Small floating error banner rendered when an /analyze request
// fails (network error, 5xx, ...). Auto-dismisses in page.jsx after
// 5 seconds; click anywhere on it to dismiss sooner.
// -------------------------------------------------------------------

import React from 'react';

export default function ErrorToast({ message, onClose }) {
  if (!message) return null;

  return (
    <div className="error-toast" id="errorToast" onClick={onClose} style={{ cursor: 'pointer' }}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ width: 18, height: 18 }}>
        <circle cx="12" cy="12" r="10" />
        <line x1="12" y1="8" x2="12" y2="12" />
        <line x1="12" y1="16" x2="12.01" y2="16" />
      </svg>
      <span id="errorToastMessage">{message}</span>
    </div>
  );
}
