# 🔗 Google Drive + Sheets + Gmail — Connect Guide (TraceAI / SCAMNET)

Ye guide tumhe **exact steps** batata hai Drive, Sheets aur Gmail connect karne ke,
aur confirm karta hai ki **workflow code me wired hai** — jab scammer se saari
details fetch ho jaati hain, to:

| App | Kya hota hai (automatically, har `/analyze` turn pe) |
|---|---|
| **Google Sheets** | Case ki **ek row upsert** hoti hai (case_id se de-dup) — IOCs, risk score, verdict |
| **Google Drive** | Markdown **report upload** hoti hai; baad ke turns pe **same file update** (duplicate nahi) |
| **Gmail** | Report ka **e-mail** configured recipient(s) ko chala jaata hai (default: case me ek hi baar) |

> Sab kuch **best-effort** hai: koi bhi app connect nahi hai to wo `skipped`
> hota hai, investigation kabhi break nahi hoti. Code: `tools/evidence_archive.py`.

---

## ⚡ Sabse fast tareeka (ek OAuth file — teeno apps)

Ek hi Google account se **Drive + Sheets + Gmail** teeno chal sakte hain ek
`authorized_user` credentials file se. Repo me helper script hai:

### Step 1 — Google Cloud project + OAuth client
1. https://console.cloud.google.com/ kholo → ek project banao (ya select karo).
2. **APIs & Services → Library** me jaake ye 3 APIs **Enable** karo:
   - Google Drive API
   - Google Sheets API
   - Gmail API
3. **APIs & Services → OAuth consent screen**:
   - User type: **External** → app ka naam do → Save.
   - **Test users** me apna Gmail address add karo (publishing se pehle zaroori).
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Desktop app** → Create.
   - **Download JSON** button se client JSON download karo
     (isme `client_id` + `client_secret` hota hai). Ise `credentials/oauth_client.json` pe daal do.

### Step 2 — authorized-user token file generate karo
Repo root se:

```bash
python scripts/google_oauth_setup.py \
  --client-secrets credentials/oauth_client.json \
  --apps gmail,drive,sheets \
  --out credentials/google_token.json
```

- Browser khulega → Google consent screen → **Allow**.
- Terminal pe likha aayega: `Credentials written → credentials/google_token.json`.
- Headless server pe ho (browser nahi khulta)? `--no-browser` lagao — URL khud
  kisi bhi machine pe kholo, redirect URL paste kar do.

> Ye file `credentials/` aur `*token*.json` me git-ignored hai — kabhi commit nahi hogi.

### Step 3 — `.env` me point karo
`.env.example` ko `.env` banao (agar nahi hai) aur **ek shared file** teeno ke liye set karo:

```dotenv
GOOGLE_CREDENTIALS_FILE=credentials/google_token.json

# Gmail report delivery — ye zaroori hai warna mail nahi jayegi:
GOOGLE_GMAIL_REPORT_RECIPIENTS=your-analyst@gmail.com, soc@company.com
# Optional:
# GOOGLE_GMAIL_REPORT_SUBJECT_PREFIX=[TraceAI] Scam Investigation Report
# GOOGLE_GMAIL_SEND_EVERY_TURN=0   # 0 = case me ek hi baar mail (default), 1 = har turn
```

### Step 4 — backend restart + Connect
```bash
./run_all.sh          # ya: python scripts/run_all.py
```
Dashboard → **Connected Apps** (left rail) → har card pe **Connect** dabao.
Har card `Connected` dikhaye ga jab Google ne real API call verify kar di
(Drive `about.get`, Sheets spreadsheet read/create, Gmail `getProfile`).

**Ho gaya.** Ab jab bhi scammer message daaloge, teeno apps automatically update honge.

---

## 🔀 Alternative: Service Account (sirf Drive + Sheets, Gmail nahi)

Agar tumhe **Gmail nahi** chahiye aur server-to-server setup karna hai:

1. Cloud Console → **IAM & Admin → Service Accounts** → create.
2. Service account → **Keys → Add key → JSON** → download → `credentials/sa.json`.
3. `.env`:
   ```dotenv
   GOOGLE_DRIVE_CREDENTIALS_FILE=credentials/sa.json
   GOOGLE_SHEETS_CREDENTIALS_FILE=credentials/sa.json
   # Optional: apna existing spreadsheet / folder use karna ho to:
   # GOOGLE_SHEETS_SPREADSHEET_ID=.........
   # GOOGLE_DRIVE_FOLDER_ID=.........
   ```
4. **Zaroori:** service account ki `client_email` (JSON me hoti hai) ko:
   - apna **spreadsheet share** karo (Editor), aur/ya id khali chhod do →
     SCAMNET khud "SCAMNET Investigation Evidence" spreadsheet bana dega;
   - apna **Drive folder share** karo (Editor) agar `GOOGLE_DRIVE_FOLDER_ID` set hai.
5. Backend restart → Connected Apps → Connect.

> ⚠️ **Gmail service account se nahi chalta** (plain). Gmail ke liye hamesha
> **authorized_user** file chahiye (upar wala fast tareeka), ya Google Workspace
> domain-wide delegation. Isliye Gmail ke liye Step 1–4 wala OAuth flow hi use karo.

---

## 🧪 Verify karo (connect ke baad)

1. **Status check:** `GET /api/integrations` — teeno `connected: true` hon.
2. **Sheets/Drive:** ek scammer message dashboard me paste karo → `/analyze` ke
   baad response ke `archive` field me dekho:
   ```json
   "archive": {
     "google_drive":  {"status": "uploaded", "file_id": "...", "link": "..."},
     "google_sheets": {"status": "created", "spreadsheet_id": "..."},
     "gmail":         {"status": "sent", "recipients": ["..."]}
   }
   ```
   `skipped` ka matlab wo app connect nahi hai (reason bhi dikhega:
   `not_connected` / `no_recipients` / `no_report`).
3. **Gmail:** apne inbox me report mail check karo (subject prefix `[TraceAI] ...`).

### On-demand mail (workflow ke alawa)
Report already bani ho aur dobara mail karni ho:
```bash
curl -X POST http://localhost:8001/api/integrations/gmail/send-report \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"session_abc","to":"analyst@gmail.com"}'
```

---

## 🩺 Troubleshooting (honest errors — kabhi fake "connected" nahi)

| Symptom (Connect pe) | Matlab | Fix |
|---|---|---|
| **409 not_configured** | credentials file set nahi / path galat | `.env` me sahi path daalo, backend restart |
| **502 connection_failed: 401/403** | Google ne credentials reject kiye | authorized_user file purani/revoked — script dobara chalao |
| **502 ... 404 (Sheets)** | service account ko spreadsheet share nahi kiya | spreadsheet/folder `client_email` ko Editor do |
| **Gmail 502 "400 ... delegate"** | service account se Gmail try kar rahe ho | Gmail ke liye authorized_user (OAuth) file use karo |
| Card **"Status unknown"** | backend unreachable | `run_all.sh` chal raha hai? port 8001 up hai? |
| Mail nahi aayi, Sheets/Drive chale | recipients khali | `GOOGLE_GMAIL_REPORT_RECIPIENTS` set karo |

Har error message **sanitised** hota hai — token/secret/path kabhi leak nahi hota.

---

## 🔒 Security (short)
- Credentials **sirf server-side `.env`** me; browser tak kabhi nahi jaate.
- `GET /api/integrations` sirf **naam + status** deta hai, secret values nahi.
- `.env`, `credentials/`, `*token*.json`, `*service_account*.json` git-ignored hain.
- Connected Apps modal kabhi **fake "Connected"** nahi dikhata — sirf tab jab
  Google ne real API call verify ki ho.
