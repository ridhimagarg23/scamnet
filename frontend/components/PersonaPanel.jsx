// PersonaPanel.jsx
// =================
// Left panel: the undercover persona "identity card".
//
// Before the first /analyze response the panel shows a skeleton
// ("Waiting for Persona"); afterwards it renders the cover identity
// chosen by the backend (name, occupation, confidence, traits grid,
// AI strategy tip) with a matching avatar.
//
// Data source: dashboardData.persona (backend get_persona_profile).
// -------------------------------------------------------------------

import React from 'react';
import Avatar from './Avatar';
import { DynamicTraitIcon } from './Icons';

export default function PersonaPanel({
  persona,
  confidenceScore = 0,
  isSkeleton = false,
  onViewPersona
}) {
  // A persona is "assigned" once the backend returned a real name.
  const isAssigned = persona && persona.name && persona.name !== "—" && persona.name !== "Not Assigned";
  const showSkeleton = isSkeleton || !isAssigned;

  return (
    <div className={`persona-panel ${showSkeleton ? 'skeleton' : ''}`} id="personaPanel">
      {/* ---------- Skeleton / initial (no persona yet) ---------- */}
      <div className="persona-skeleton-content">
        <div className="waiting-persona">
          <h3>Waiting for Persona</h3>
          <p>TraceAI will generate a realistic undercover identity after analyzing the first suspicious message.</p>
        </div>
        {/* Pulsing skeleton placeholders */}
        <div className="skeleton-avatar" style={{ margin: '10px auto' }} />
        <div className="skeleton-text" style={{ width: '80%', margin: '8px auto' }} />
        <div className="skeleton-text" style={{ width: '50%', margin: '4px auto 15px' }} />
        <div className="skeleton-text" style={{ width: '100%' }} />
        <div className="skeleton-text" style={{ width: '90%' }} />
        <div className="skeleton-text" style={{ width: '95%' }} />
        <div className="skeleton-text" style={{ width: '85%' }} />
      </div>

      {/* ---------- Active persona card ---------- */}
      <div className="persona-real-content">
        <div className="panel-heading">Active Persona</div>

        <div className="persona-avatar-block" style={{ marginTop: 8 }}>
          <div className="persona-avatar" id="personaAvatarWrap">
            {isAssigned ? (
              <>
                {/* Occupation-matched avatar + "online" pulse dot */}
                <Avatar
                  initials={persona.initials || "TA"}
                  seedName={persona.name}
                  occupation={persona.occupation}
                />
                <div className="persona-online-dot" />
              </>
            ) : (
              <>
                {/* Generic user silhouette until a persona is assigned */}
                <div className="persona-avatar-placeholder" id="personaPlaceholder">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                    <circle cx="12" cy="7" r="4" />
                  </svg>
                </div>
                <div className="persona-online-dot" />
              </>
            )}
          </div>

          {/* Identity header */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 8 }}>
            <div className="persona-name-el" id="personaName">
              {persona?.name || '—'}
            </div>
            {isAssigned && (
              <span
                id="personaActiveBadge"
                style={{
                  fontSize: 9,
                  fontWeight: 700,
                  color: 'var(--brand-mid)',
                  background: 'var(--brand-light)',
                  borderRadius: 4,
                  padding: '1px 6px'
                }}
              >
                Active
              </span>
            )}
          </div>

          <div className="persona-role-el" id="personaOccupation">
            {persona?.occupation || '—'}
          </div>

          {/* Quick-read chips (backend risk/confidence data) */}
          <div className="persona-badge" id="personaConfidenceBadge">
            Confidence: {confidenceScore || 0}%
          </div>

          <div className="persona-badge-sec" id="personaObjectiveBadge">
            Objective: {persona?.traits?.[5]?.value || '—'}
          </div>
        </div>

        <hr className="section-divider" />

        {/* Trait grid (index-aligned with backend persona.traits) */}
        <div className="persona-traits" id="personaTraitsEl">
          {persona?.traits?.map((trait, index) => (
            <div className="trait-row" key={index}>
              <div className="trait-icon" id={`traitIcon_${index}`}>
                <DynamicTraitIcon iconName={trait.icon} />
              </div>
              <div className="trait-content">
                <div className="trait-label">{trait.label}</div>
                <div className="trait-value" id={`traitVal_${index}`}>
                  {trait.value || '—'}
                </div>
              </div>
            </div>
          ))}
        </div>

        <hr className="section-divider" />

        {/* Manual editing is a future feature - currently an alert stub */}
        <button className="btn-view-persona" onClick={onViewPersona} type="button">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ width: 13, height: 13 }}>
            <path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
          </svg>
          View / Edit Persona
        </button>

        {/* Strategy hint generated by the backend each turn */}
        <div className="ai-tip">
          <div className="ai-tip-header">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
            AI Strategy Tip
          </div>
          <p id="aiTipText">{persona?.aiTip || '—'}</p>
        </div>
      </div>
    </div>
  );
}
