// Topbar.jsx
// ===========
// Session header bar shown above the 3-panel content row:
//  * Left: "session alert" status text (danger banner look)
//  * Middle: Change Persona / End Investigation actions
//  * Right: protected-session badge + live timer, "Generate Report"
//    button and the user avatar mascot
// -------------------------------------------------------------------

import React from 'react';
import { ShieldIcon } from './Icons';
import Avatar from './Avatar';

export default function Topbar({
  session,
  liveTimerText,
  hasReport,
  onChangePersona,
  onEndInvestigation,
  onGenerateReport
}) {
  return (
    <div className="topbar" id="topbar">
      {/* Status blurb */}
      <div className="topbar-left">
        <div className="warn-icon-wrap">
          <ShieldIcon style={{ width: 16, height: 16 }} />
        </div>
        <div className="topbar-text">
          <h3 id="topbarTitle">{session?.alert || "Undercover session in progress"}</h3>
          <p id="topbarSub">{session?.alertSub || "TraceAI is actively analyzing and extracting threat intelligence."}</p>
        </div>
      </div>

      {/* Session actions */}
      <div className="topbar-actions">
        {/* Persona is auto-configured per threat; this button is a stub */}
        <button className="btn-outline" onClick={onChangePersona} type="button">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
            <circle cx="9" cy="7" r="4" />
            <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
            <path d="M16 3.13a4 4 0 0 1 0 7.75" />
          </svg>
          Change Persona
        </button>

        <button className="btn-danger-outline" onClick={onEndInvestigation} type="button">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <line x1="15" y1="9" x2="9" y2="15" />
            <line x1="9" y1="9" x2="15" y2="15" />
          </svg>
          End Investigation
        </button>
      </div>

      {/* Session readouts + report CTA */}
      <div className="topbar-right">
        <div className="protected-badge">
          <div className="pulse-dot" />
          <span id="sessionStatus">{session?.status || "Protected Session"}</span>
          <span id="liveTimer" style={{ marginLeft: 8, fontWeight: 600, opacity: 0.85 }}>
            {liveTimerText}
          </span>
        </div>

        {/* Disabled until the backend produced a report for this session */}
        <button
          className="btn-report"
          id="btnReportHeader"
          onClick={onGenerateReport}
          disabled={!hasReport}
          type="button"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
          </svg>
          Generate Report
        </button>

        {/* Analyst mascot */}
        <div className="avatar-ring" id="userAvatar">
          <Avatar isMascot={true} />
        </div>
      </div>
    </div>
  );
}
