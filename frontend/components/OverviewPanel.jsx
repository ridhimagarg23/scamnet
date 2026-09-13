// OverviewPanel.jsx
// ==================
// Right panel: real-time investigation readouts.
//
//  * Threat level badge + animated risk-score gauge (SVG ring)
//  * Confidence score bar
//  * 5-step investigation progress stepper
//  * Evidence tracker (IOCs collected per family)
//  * Recent activity timeline
//
// Data source: dashboardData.investigation (backend POST /analyze).
// -------------------------------------------------------------------

import React, { useState, useEffect } from 'react';
import { CheckIcon, SearchIcon, LockIcon, ChevronIcon, DynamicEvidenceIcon } from './Icons';
import { cap } from '@/lib/constants';

export default function OverviewPanel({
  investigation = {},
  onViewAllActivity
}) {
  const {
    riskScore = 0,
    riskLevel = "PENDING",
    threatType = "Not Investigated",
    threatSeverity = "None",
    confidenceScore = 0,
    progress = [],
    evidence = [],
    activity = []
  } = investigation;

  // ---------------------------------------------------------------
  // Animated risk-score counter: counts up/down to the target value
  // with an adaptive tick rate (snappier for big jumps).
  // ---------------------------------------------------------------
  const [displayScore, setDisplayScore] = useState(0);

  useEffect(() => {
    const target = parseInt(riskScore) || 0;
    if (displayScore === target) return;

    const step = target > displayScore ? 1 : -1;
    const diff = Math.abs(target - displayScore);
    const stepTime = Math.max(Math.floor(1000 / (diff || 1)), 15);

    const timer = setInterval(() => {
      setDisplayScore((prev) => {
        const next = prev + step;
        if ((step === 1 && next >= target) || (step === -1 && next <= target)) {
          clearInterval(timer);
          return target;
        }
        return next;
      });
    }, stepTime);

    return () => clearInterval(timer);
  }, [riskScore]);

  // Risk badge colours by level (fall back to green/success style
  // for non-threatening or unknown states).
  const rLevel = (riskLevel || '').toUpperCase();
  let badgeStyle = {
    background: 'var(--success-bg)',
    color: 'var(--success)',
    borderColor: 'var(--success-border)'
  };
  if (rLevel.includes('HIGH') || rLevel.includes('CRITICAL')) {
    badgeStyle = {
      background: 'var(--danger-bg)',
      color: 'var(--danger)',
      borderColor: 'var(--danger-border)'
    };
  } else if (rLevel.includes('MEDIUM') || rLevel.includes('MODERATE')) {
    badgeStyle = {
      background: 'var(--warning-bg)',
      color: 'var(--warning)',
      borderColor: 'var(--warning-border)'
    };
  }

  // Severity pill styles (Critical / High / Medium / None)
  const showSeverity = threatSeverity && threatSeverity !== 'None';
  let sevClass = 'badge-success';
  if (threatSeverity?.toLowerCase() === 'critical') sevClass = 'badge-critical';
  else if (threatSeverity?.toLowerCase() === 'high') sevClass = 'badge-warning';

  // ---------------------------------------------------------------
  // Gauge math: SVG circle radius 15.9 -> circumference ~99.9 units.
  // strokeDasharray shows the filled arc proportional to the score.
  // ---------------------------------------------------------------
  const circumference = 2 * Math.PI * 15.9; // ~99.9
  const filled = (displayScore / 100) * circumference;
  const strokeDash = `${filled.toFixed(1)} ${(circumference - filled).toFixed(1)}`;

  // Stepper icons per state.
  const stepIcons = {
    done: <CheckIcon style={{ width: 11, height: 11 }} />,
    current: <SearchIcon style={{ width: 11, height: 11 }} />,
    locked: <LockIcon style={{ width: 11, height: 11 }} />
  };

  return (
    <div className="overview-panel" id="overviewPanel">
      {/* ============ Header ============ */}
      <div className="overview-head">
        <h3>Investigation Overview</h3>
        <span className="risk-badge-pill" id="riskBadge" style={badgeStyle}>
          {riskLevel || '—'}
        </span>
      </div>

      {/* ============ Risk card (level + gauge) ============ */}
      <div className="risk-card">
        <div className="risk-card-left">
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <div
              className="risk-icon-wrap"
              style={{ color: 'var(--danger)', width: 22, height: 22, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" style={{ width: 18, height: 18 }}>
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
              </svg>
            </div>
            <div>
              <div className="risk-lbl">Threat Level</div>
              <div className="risk-val" id="riskLevel" style={{ fontWeight: 800, fontSize: 14, marginTop: 2 }}>
                {riskLevel || '—'}
              </div>
            </div>
          </div>

          <div style={{ marginTop: 10, minWidth: 0 }}>
            <div className="risk-lbl">Threat Type</div>
            <div className="info-value-lg" id="threatType" style={{ marginTop: 3, lineHeight: 1.25 }}>
              {threatType || 'Not Investigated'}
            </div>
            {showSeverity && (
              <span className={sevClass} id="threatSeverity" style={{ display: 'inline-block', marginTop: 6 }}>
                {threatSeverity}
              </span>
            )}
          </div>
        </div>

        {/* SVG ring gauge */}
        <div className="gauge-wrap">
          <svg viewBox="0 0 36 36">
            {/* Track (grey full ring) */}
            <circle cx="18" cy="18" r="15.9" fill="none" stroke="var(--border-light)" strokeWidth="3" />
            {/* Filled arc (progress proportion of the ring) */}
            <circle
              id="gaugeArc"
              cx="18"
              cy="18"
              r="15.9"
              fill="none"
              stroke="var(--danger)"
              strokeWidth="3"
              strokeDasharray={strokeDash}
              strokeLinecap="round"
              style={{ transition: 'stroke-dasharray 0.3s ease' }}
            />
          </svg>
          {/* Score in the middle of the ring */}
          <div className="gauge-score">
            <span id="riskScore">{displayScore}</span>
            <span className="gauge-unit">/100</span>
          </div>
        </div>
      </div>

      <hr className="section-divider" />

      {/* ============ Confidence bar ============ */}
      <div className="info-block">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span className="info-sub-label">Confidence Score</span>
          <span className="info-value-lg" id="confidenceScore">{confidenceScore || 0}%</span>
        </div>
        <div className="conf-bar-track">
          <div className="conf-bar-fill" id="confidenceFill" style={{ width: `${confidenceScore || 0}%` }} />
        </div>
      </div>

      <hr className="section-divider" />

      {/* ============ Progress stepper ============ */}
      <div>
        <div className="section-label mb-2">Investigation Progress</div>
        <div className="progress-steps" id="progressSteps">
          {/* Connector line behind the circles */}
          <div className="steps-line" />
          {progress?.map((s, idx) => (
            <div className={`step ${s.state}`} key={idx}>
              <div className="step-circle">
                {stepIcons[s.state] || stepIcons.locked}
              </div>
              {/* Label with manual line break (backend sends \n) */}
              <div
                className="step-name"
                dangerouslySetInnerHTML={{ __html: s.label.replace('\n', '<br>') }}
              />
            </div>
          ))}
        </div>
      </div>

      <hr className="section-divider" />

      {/* ============ Evidence tracker ============ */}
      <div>
        <div className="section-label mb-2">Evidence Tracker</div>
        <div className="evidence-list" id="evidenceList">
          {!evidence || evidence.length === 0 ? (
            <p style={{ color: 'var(--text-4)', fontSize: 11.5, padding: 5 }}>No indicators found.</p>
          ) : (
            evidence.map((item, idx) => {
              // Pending rows show only the family name; collected /
              // verified rows carry "Type: value" and get split.
              let displayTitle = item.type?.toUpperCase() || 'EVIDENCE';
              let displayValue = 'Pending';
              if (item.status === 'collected' || item.status === 'verified') {
                const parts = item.name?.split(': ') || [];
                if (parts.length > 1) {
                  displayTitle = parts[0];
                  displayValue = parts.slice(1).join(': ');
                } else {
                  displayValue = item.name;
                }
              }

              return (
                <div className={`evidence-item ${item.status}`} key={idx}>
                  <div className="ev-left">
                    <div className={`ev-icon ${item.type}`}>
                      <DynamicEvidenceIcon type={item.type} />
                    </div>
                    <div>
                      <span className="ev-name">{displayTitle}</span>
                      <span className="ev-value">{displayValue}</span>
                    </div>
                  </div>
                  <div className="ev-right">
                    <span className={`status-pill ${item.status}`}>
                      {cap(item.status)}
                    </span>
                    <div className="chevron">
                      <ChevronIcon />
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      <hr className="section-divider" />

      {/* ============ Recent activity ============ */}
      <div>
        <div className="activity-head mb-2">
          <div className="section-label">Recent Activity</div>
          {/* Full trace viewer is a future feature (alert stub) */}
          <button className="view-all-btn" onClick={onViewAllActivity} type="button">
            View All
          </button>
        </div>
        <div className="activity-list" id="activityList">
          {!activity || activity.length === 0 ? (
            <p style={{ color: 'var(--text-4)', fontSize: 11.5, padding: 5 }}>No events recorded yet.</p>
          ) : (
            activity.map((a, idx) => (
              <div className="activity-item" key={idx}>
                <span className="activity-time">{a.time || '—'}</span>
                <div className="activity-dot" />
                <span className="activity-text" title={a.text}>{a.text}</span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
