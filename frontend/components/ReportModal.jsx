// ReportModal.jsx
// ===============
// Modal dialog that previews the markdown investigation report and
// lets the analyst download it as a .md file.
//
// The report object comes from the backend response:
//   { title: string, markdown: string }
// markdown is rendered with the "marked" library; download builds a
// Blob client-side (no server round-trip needed).
// -------------------------------------------------------------------

import React from 'react';
import { marked } from 'marked';

export default function ReportModal({
  isOpen,
  report,
  onClose
}) {
  // Render nothing unless the modal was opened AND a report exists.
  if (!isOpen || !report) return null;

  // Create a markdown file on the fly and trigger a browser download.
  const handleDownload = () => {
    if (!report?.markdown) return;
    const blob = new Blob([report.markdown], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    // Slugify the title so the filename is filesystem-safe.
    const safeTitle = (report.title || 'investigation-report')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-');
    a.download = `${safeTitle}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  // Convert the LLM-generated markdown to HTML for preview.
  const htmlContent = report.markdown ? marked.parse(report.markdown) : '';

  return (
    <div className="modal-overlay" id="reportModal" onClick={onClose}>
      {/* Stop propagation so clicks inside the card don't close it */}
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3 id="reportModalTitle">{report.title || "Investigation Report Preview"}</h3>
          <button className="modal-close-btn" onClick={onClose} type="button">
            &times;
          </button>
        </div>

        {/* Rendered markdown body (content originates from our own LLM) */}
        <div
          className="modal-body"
          id="reportModalBody"
          dangerouslySetInnerHTML={{ __html: htmlContent }}
        />

        <div className="modal-footer">
          <button className="btn-outline" onClick={handleDownload} type="button">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" style={{ width: 13, height: 13 }}>
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <polyline points="7 10 12 15 17 10" />
              <line x1="12" y1="15" x2="12" y2="3" />
            </svg>
            Download Markdown
          </button>
          <button className="btn-send" onClick={onClose} type="button" style={{ transform: 'none' }}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
