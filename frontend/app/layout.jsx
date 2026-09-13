// layout.jsx
// ===========
// Root layout: sets global <html>/<body> shell, metadata and fonts.
//
// The dashboard is a single page (app/page.jsx); this layout only
// loads the Inter font family and declares SEO metadata.
// -------------------------------------------------------------------

import './globals.css';

export const metadata = {
  title: 'TraceAI – Undercover scam investigation dashboard',
  description: 'Automated undercover AI scam investigation and threat intelligence platform.',
  icons: {
    icon: '/assets/avatar_scammer.png'
  }
};

export const viewport = {
  width: 'device-width',
  initialScale: 1.0
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <head>
        {/* Inter font (self-hosted fallbacks come from globals.css) */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>
        {children}
      </body>
    </html>
  );
}
