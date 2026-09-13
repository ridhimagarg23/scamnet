// Sidebar.jsx
// ===========
// Left navigation rail:
//  * Logo + product name
//  * "New Investigation" primary action
//  * Nav links (Dashboard / History / Saved Cases / Reports)
//  * Footer with a safety shield card + dark-mode toggle
//
// History / Saved Cases are future features: clicking them bubbles
// up through onShowNotImplemented (alert stub in page.jsx).
// -------------------------------------------------------------------

import React from 'react';
import { ShieldIcon } from './Icons';

export default function Sidebar({
  onNewInvestigation,
  onGenerateReport,
  onShowNotImplemented,
  isDark,
  onToggleDark
}) {
  return (
    <aside className="sidebar">
      {/* Branding */}
      <div className="logo">
        <div className="logo-icon">
          <ShieldIcon style={{ width: 20, height: 20, stroke: '#fff', strokeWidth: 2.2 }} />
        </div>
        <div className="logo-text">
          <h2>TraceAI</h2>
          <p>Undercover Investigation</p>
        </div>
      </div>

      {/* Primary action: wipe the session and start over */}
      <button className="btn-new" onClick={onNewInvestigation} type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 5v14M5 12h14" />
        </svg>
        <span>New Investigation</span>
      </button>

      {/* Active nav item: the dashboard is currently the only screen */}
      <button className="nav-item active" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="7" height="7" rx="1" />
          <rect x="14" y="3" width="7" height="7" rx="1" />
          <rect x="3" y="14" width="7" height="7" rx="1" />
          <rect x="14" y="14" width="7" height="7" rx="1" />
        </svg>
        <span>Dashboard</span>
      </button>

      {/* Future features - shown as stubs */}
      <button className="nav-item" type="button" onClick={() => onShowNotImplemented('History')}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10" />
          <polyline points="12 6 12 12 16 14" />
        </svg>
        <span>History</span>
      </button>

      <button className="nav-item" type="button" onClick={() => onShowNotImplemented('Saved Cases')}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
          <line x1="16" y1="13" x2="8" y2="13" />
          <line x1="16" y1="17" x2="8" y2="17" />
        </svg>
        <span>Saved Cases</span>
      </button>

      {/* Reports: opens the current report modal (disabled until one exists) */}
      <button className="nav-item" type="button" onClick={onGenerateReport}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round">
          <path d="M9 17H7A5 5 0 0 1 7 7h2" />
          <path d="M15 7h2a5 5 0 0 1 0 10h-2" />
          <line x1="8" y1="12" x2="16" y2="12" />
        </svg>
        <span>Reports</span>
      </button>

      <div className="sidebar-footer">
        {/* Static awareness card */}
        <div className="shield-card">
          <div className="sc-icon-wrap">
            <ShieldIcon style={{ width: 16, height: 16, strokeWidth: 2.5 }} />
          </div>
          <h4>Stay Safe, Stay Smart</h4>
          <p>TraceAI investigates scams while you stay protected.</p>
        </div>

        {/* Dark mode switch (adds .dark to <body>, see page.jsx) */}
        <div className="dark-toggle-row">
          <div className="dtlabel">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
            </svg>
            <span>Dark Mode</span>
          </div>
          <button
            className={`toggle-switch ${isDark ? 'on' : ''}`}
            id="darkToggle"
            onClick={onToggleDark}
            type="button"
            aria-label="Toggle Dark Mode"
          />
        </div>
      </div>
    </aside>
  );
}
