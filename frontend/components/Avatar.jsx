// Avatar.jsx
// ===========
// Renders one round avatar in four modes:
//
//  * isScammer -> static scammer PNG (/assets/avatar_scammer.png)
//  * isMascot  -> inline SVG shield-mascot (used in the top bar)
//  * persona   -> occupation-matched avatar PNG selected by
//                 getPersonaRoleKey() (professional, retiree, ...)
//  * fallback  -> same PNG path as persona with "student" default
//
// PNG assets live in frontend/public/assets/avatar_<role>.png
// -------------------------------------------------------------------

import React from 'react';
import { getPersonaRoleKey } from '@/lib/constants';

export default function Avatar({
  initials = "TA",
  seedName = "default",
  occupation,
  isScammer = false,
  isMascot = false,
  className = ""
}) {
  // Scammer bubbles always show the scammer avatar image.
  if (isScammer) {
    return (
      <img
        src="/assets/avatar_scammer.png"
        className={`avatar-img-el ${className}`}
        alt="Scammer"
        style={{ width: '100%', height: '100%', objectFit: 'cover', borderRadius: '50%', display: 'block' }}
      />
    );
  }

  // Mascot: hand-drawn SVG shield character (no asset download).
  if (isMascot) {
    return (
      <svg viewBox="0 0 100 100" className={`avatar-svg-el ${className}`}>
        <defs>
          <clipPath id="clip_masc_def">
            <circle cx="50" cy="50" r="48" />
          </clipPath>
          {/* Emerald gradient used by the mascot's shield body */}
          <linearGradient id="masc_grad_def" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="#16A34A" />
            <stop offset="100%" stopColor="#15803D" />
          </linearGradient>
        </defs>
        {/* Shield body */}
        <circle cx="50" cy="50" r="48" fill="url(#masc_grad_def)" stroke="#16A34A" strokeWidth="2" />
        <g clipPath="url(#clip_masc_def)">
          {/* Monitor base */}
          <rect x="42" y="70" width="16" height="24" fill="#E2E8F0" rx="3" />
          <rect x="46" y="74" width="8" height="15" fill="#16A34A" rx="1" />
          <rect x="46" y="60" width="8" height="12" fill="#94A3B8" />
          {/* Screen bezel + display */}
          <rect x="28" y="28" width="44" height="34" rx="10" fill="#FFFFFF" stroke="#E2E8F0" strokeWidth="2" />
          <rect x="34" y="34" width="32" height="20" rx="6" fill="#0F172A" />
          {/* "Eyes" = green status dots on the dark display */}
          <circle cx="42" cy="44" r="3" fill="#16A34A" />
          <circle cx="58" cy="44" r="3" fill="#16A34A" />
          {/* Antenna arms */}
          <rect x="23" y="38" width="5" height="12" rx="1" fill="#94A3B8" />
          <rect x="72" y="38" width="5" height="12" rx="1" fill="#94A3B8" />
          {/* Little signal wave on top */}
          <path d="M50 30 L53 32 L53 35 C53 37, 50 38, 50 38 C50 38, 47 37, 47 35 L47 32 Z" fill="#16A34A" />
        </g>
      </svg>
    );
  }

  // Persona avatars: occupation -> role key -> matching PNG asset.
  // (occupation usually comes from backend persona.occupation;
  // seedName is a fallback for avatar-only contexts.)
  const role = getPersonaRoleKey(occupation || seedName);
  return (
    <img
      src={`/assets/avatar_${role}.png`}
      className={`avatar-img-el ${className}`}
      alt={role}
      style={{ width: '100%', height: '100%', objectFit: 'cover', borderRadius: '50%', display: 'block' }}
    />
  );
}
