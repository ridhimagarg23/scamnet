// constants.js
// =============
// Shared UI constants + tiny helpers for the TraceAI dashboard.
//
// The values here mirror the JSON contract produced by the backend
// (backend/api.py / POST /analyze). Whenever you change the shape of
// an API response, keep INITIAL_DASHBOARD_DATA in sync - the empty
// state is what the dashboard renders before the first /analyze call.
// -------------------------------------------------------------------

// Full empty-state dashboard payload. Spreading this object into the
// API response gives every panel a safe default shape so the UI never
// crashes while waiting for real data.
export const INITIAL_DASHBOARD_DATA = {
  session: {
    status: "Protected Session",
    alert: "Undercover session in progress",
    alertSub: "TraceAI is actively analyzing and extracting threat intelligence."
  },
  persona: {
    name: "Not Assigned",
    occupation: "Undercover Agent",
    avatar: null,
    initials: "TA",
    traits: [
      { icon: "globe", label: "Language", value: "—" },
      { icon: "message", label: "Communication Style", value: "—" },
      { icon: "alert", label: "Risk Approach", value: "—" },
      { icon: "user", label: "Strategy", value: "—" },
      { icon: "bar", label: "Digital Literacy", value: "—" },
      { icon: "shield", label: "Current Objective", value: "—" }
    ],
    aiTip: "Provide scammer message payload inside dialogue channel to begin honeypot routing."
  },
  investigation: {
    riskScore: 0,
    riskLevel: "PENDING",
    threatType: "Not Investigated",
    threatSeverity: "None",
    confidenceScore: 0,
    progress: [
      { label: "Threat\nDetected", state: "locked" },
      { label: "IOC\nExtracted", state: "locked" },
      { label: "Undercover\nEngagement", state: "locked" },
      { label: "Evidence\nSecured", state: "locked" },
      { label: "Report\nReady", state: "locked" }
    ],
    evidence: [
      { type: "website", name: "Website URL", status: "pending" },
      { type: "phone", name: "Phone Number", status: "pending" },
      { type: "email", name: "Email Addr", status: "pending" },
      { type: "upi", name: "UPI ID", status: "pending" }
    ],
    activity: [
      { time: "—", text: "Dashboard loaded. Awaiting message." }
    ]
  },
  messages: [],
  report: null
};

// One-line behavioural summary shown per persona occupation in the
// chat panel's summary card. Keys match getPersonaRoleKey() output.
export const ROLE_SUMMARY_MAP = {
  student: "A young student profile. Displays curious and cautious digital behaviors, focusing on standard verification techniques.",
  graduate: "A recent graduate seeking remote job opportunities. Focused on gathering information before sharing personal details.",
  seeker: "An active job seeker. Eager to explore options but cautious of non-verified channels and payment requests.",
  professional: "A practical and analytical working professional. Exercises analytical thinking and detailed verification standards.",
  freelancer: "An independent freelancer looking for project contracts. Highly detail-oriented and protective of payment procedures.",
  entrepreneur: "A driven entrepreneur. Strategic-minded and protective of organizational identities and assets.",
  retiree: "A senior citizen or retiree. Experienced yet cautious, relying on standard safety practices and personal networks.",
  worker: "A part-time employee or worker. Hardworking, flexible, yet cautious about personal details and external claims."
};

// Rotating status lines shown in the "TraceAI is analyzing..." banner
// while a request is in flight.
export const THINKING_STEPS = [
  "Checking patterns...",
  "Extracting entities...",
  "Assessing risk signatures...",
  "Formulating undercover agent response..."
];

// Maps an evidence item type to the trait icon shown in the overview.
// NOTE: keep in sync with tools/entity_extractor.py IOC families.
export const EV_ICON_MAP = {
  website: "globe",
  phone: "phone",
  email: "mail",
  upi: "card",
  bank: "bank"
};

// Maps the backend's free-text occupation (e.g. "Working Professional")
// to a fixed persona role key. The role key selects the avatar PNG
// (frontend/public/assets/avatar_<role>.png) and the summary text.
export function getPersonaRoleKey(occupation) {
  const occ = (occupation || '').toLowerCase();
  if (occ.includes('retired') || occ.includes('retiree') || occ.includes('senior')) return 'retiree';
  if (occ.includes('graduate')) return 'graduate';
  if (occ.includes('student') || occ.includes('college')) return 'student';
  if (occ.includes('seeker') || occ.includes('job')) return 'seeker';
  if (occ.includes('professional') || occ.includes('working') || occ.includes('manager')) return 'professional';
  if (occ.includes('freelancer')) return 'freelancer';
  if (occ.includes('entrepreneur') || occ.includes('business')) return 'entrepreneur';
  if (occ.includes('worker') || occ.includes('part-time') || occ.includes('apron')) return 'worker';
  return 'student';
}

// Uppercase-first helper for status labels ("pending" -> "Pending").
export function cap(str) {
  if (!str) return 'Pending';
  return str.charAt(0).toUpperCase() + str.slice(1);
}

// Wraps every IOC found inside a chat message in a coloured
// <span class="ioc-highlight ..."> so analysts can spot indicators
// at a glance. Runs on the client side AFTER the backend extracted
// them server-side (tools/entity_extractor.py) - keep both sets of
// regexes in sync.
//
// IMPORTANT: the return value is injected via dangerouslySetInnerHTML
// (see ChatPanel). The input is analyst-pasted scam text - the regexes
// only wrap text in spans and never execute it, but treat this as the
// trust boundary it is.
export function highlightIOCs(text) {
  if (!text) return "";
  const phonePattern = /(?:\+91[-\s]?)?[6-9]\d{9}/g;
  const emailPattern = /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}/g;
  const upiPattern = /[a-zA-Z0-9._-]{2,}@[a-zA-Z]{2,}/g;
  const urlPattern = /(?:https?:\/\/|www\.)[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:\/[A-Za-z0-9\-._~:\/?#\[\]@!$&'()*+,;=%]*)?/g;

  let result = text;
  // Order matters: longest matches first so a URL containing an email
  // or phone-like tail is not split by the shorter patterns.
  result = result.replace(urlPattern, match => `<span class="ioc-highlight url">${match}</span>`);
  result = result.replace(emailPattern, match => `<span class="ioc-highlight email">${match}</span>`);
  result = result.replace(upiPattern, match => `<span class="ioc-highlight upi">${match}</span>`);
  result = result.replace(phonePattern, match => `<span class="ioc-highlight phone">${match}</span>`);
  return result;
}
