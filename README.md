# CIVICLENS

**See the Issue. Map the Problem. Inspire the Change.**

**Public app:** Deployment setup is ready below. The live address will appear here after the first Render deployment.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/hirdyanshsahni/Civiclens)

CIVICLENS is a civic issue reporting and municipal response application. Citizens can register, share a photo and location, follow a report through its status history, and confirm a nearby issue. City staff can triage, assign, update, merge duplicates, record resolution details, review the audit trail, export reports, and view analytics calculated from the SQLite database.

The application uses a small FastAPI service, SQLite persistence, private local photo storage, a responsive browser interface, and Leaflet with OpenStreetMap. It runs on Windows with Python; Node.js is not required.

## Included

- Citizen accounts, sign-in, profile updates, and role-based administrator access.
- A public-service portal style with an explicit independent-platform notice and a persistent language selector for English plus ten Indian languages: Hindi, Bengali, Tamil, Telugu, Marathi, Gujarati, Kannada, Malayalam, Punjabi, and Odia.
- Reporting requires an authenticated citizen session in both the browser flow and the report API.
- A report workflow covering Submitted, Pending Verification, Verified, Assigned, In Progress, Resolved, Rejected, and Reopened.
- Photo uploads with server-side type, signature, and size checks. Photos are private to the report author and administrators.
- GPS, interactive pin selection, and manual address and coordinate entry.
- A clustered report map and list with category, severity, status, date, open-report, address search, and text search filters.
- An optional vision provider integration. With no AI key, analysis is clearly marked **Demo mode** and uses description words only. It does not inspect the photo or produce a confidence score.
- CIVICLENS ImpactScore from 0–100 with visible factors: severity (up to 30), potential safety impact (up to 30), community corroboration (up to 20), and report age (up to 20). Staff overrides require a reason and are written to the audit log.
- Potential duplicate suggestions using a photo perceptual hash, category, distance, report text, and a 30-day time window. Administrators choose whether to merge; both records' histories are kept.
- Community confirmation with one confirmation per account per report.
- Records-based dashboard metrics, report volume, category and status counts, unresolved hotspots, and resolution time.
- Staff-only notes, audit records, and an administrator-only CSV export.
- Seven synthetic reports and demo accounts on a fresh database when demo mode is enabled. Synthetic records are visibly labeled in the interface.

## Requirements

- Windows 10 or 11
- Python 3.10 or newer
- Visual Studio Code (optional, recommended)
- Internet access for OpenStreetMap tiles, address search, and the optional configured vision provider

The map and address search depend on the availability and usage policy of public OpenStreetMap services. Smartphone location and photo capture use standard browser capabilities; ESP32 and fixed-camera integrations are not currently implemented.

## Public hosting on Render

The repository includes a Render Blueprint in `render.yaml`. It deploys the full FastAPI application in Singapore, runs the production configuration, and attaches a 1 GB persistent disk for the SQLite database and private report photos. The Blueprint generates the session secret automatically and asks you to enter the administrator email and a unique password in Render's setup screen. Do not share those credentials or commit them to Git.

Render currently lists the smallest always-on web service at $7/month and disk storage at $0.25 per GB per month, so this 1 GB configuration is about $7.25/month before tax or any usage overages. Free web services cannot attach persistent disks; they also lose local SQLite databases and uploads on restart or redeploy, so this project does not use the free plan for its live data. See [Render pricing](https://render.com/pricing) and [Render persistent disk behavior](https://render.com/docs/disks).

To deploy, sign in to Render, open the **Deploy to Render** button above, connect the GitHub repository, and provide your administrator email and a password of at least 12 characters when prompted. Review the service plan and disk in Render's confirmation screen before creating them. Render then builds the repository and assigns the live `onrender.com` address; add that address to the **Public app** line at the top of this README once deployment is live.

AI vision and email receipts remain in their clearly labeled fallback states until you add the corresponding provider keys and SMTP settings under the Render service's environment settings. Never put these secrets in `render.yaml` or GitHub.

## Languages and Aadhaar sign-in

The language selector translates the primary navigation, key landing-page copy, report form labels, sign-in controls, map filters, and report statuses. The selected language is remembered in this browser. Citizen-written report titles and descriptions stay in the language submitted; CIVICLENS does not send them to an outside translation service.

The sign-in panel explains that Aadhaar verification is not connected in this demonstration. CIVICLENS does not ask for or store Aadhaar numbers, and the current sign-in options are email and password. An Aadhaar number by itself is not an authentication step. UIDAI authentication is handled by an authorized Requesting Entity through UIDAI's defined authentication flow and informed consent. Enabling it for a real service requires an authorized integration, operational approval, and its prescribed security and audit controls; a normal CIVICLENS account cannot perform UIDAI authentication. See the [UIDAI authentication documents](https://uidai.gov.in/en/authentication-documents) and [UIDAI guidance for Requesting Entities](https://uidai.gov.in/images/Dos_and_Donots_for_Requesting_Entities.pdf).

CIVICLENS uses a public-service portal visual style, but it is an independent civic reporting platform and is not an official government website.

## Run on Windows

1. Open the CIVICLENS folder in Visual Studio Code.
2. Open **Terminal → New Terminal** and make sure the terminal is in the project folder.
3. Create and activate a virtual environment:

   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   If PowerShell blocks activation, use this for the current terminal only, then activate again:

   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   .\.venv\Scripts\Activate.ps1
   ```

4. Install the API requirements:

   ```powershell
   python -m pip install --upgrade pip
   python -m pip install -r .\backend\requirements.txt
   ```

5. Copy the environment template and set a local signing key:

   ```powershell
   Copy-Item .env.example .env
   ```

   The template is already configured for a clearly labeled demonstration. Change `CIVICLENS_SECRET_KEY` before sharing the app beyond your own computer. For a quick local key, create one with:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Paste the generated value into `.env`. Do not commit `.env` or send it with a public project.

6. Start the application:

   ```powershell
   .\start_civiclens.ps1
   ```

   The browser opens at <http://127.0.0.1:8000>. The first start creates the SQLite database and synthetic demonstration reports. The launcher runs the server in the background.

   To start without the launcher, use:

   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn server:app --app-dir .\backend --host 127.0.0.1 --port 8000
   ```

7. Stop the background server with:

   ```powershell
   .\stop_civiclens.ps1
   ```

If port 8000 is already in use, stop the other server or launch CIVICLENS with `.\start_civiclens.ps1 -Port 8001`, then open the matching local address. The citizen-only convenience launcher expects port 8000.

### Demo sign-ins

| Role | Email | Password |
| --- | --- | --- |
| Citizen | `citizen@civiclens.demo` | `CitizenDemo#2026` |
| City administrator | `admin@civiclens.demo` | `AdminDemo#2026` |

These accounts are seeded only with `CIVICLENS_DEMO_MODE=true`. They are intentionally visible to support a local hackathon demonstration. Do not use demo mode or these credentials for a public deployment.

## Optional configured image analysis

With no AI key, CIVICLENS runs in text-only demo mode. The photo is **not** analyzed, confidence is shown as unavailable, and no image-based result is claimed. Users can still submit a report by choosing the category and severity themselves.

To enable image analysis, configure an OpenAI-compatible Chat Completions vision endpoint in `.env`:

```dotenv
CIVICLENS_AI_API_KEY=replace-with-your-secret
CIVICLENS_AI_API_URL=https://api.openai.com/v1/chat/completions
CIVICLENS_AI_MODEL=gpt-5.4-mini
```

Restart the server. The default model is GPT-5.4 Mini; OpenAI's current documentation lists it for image input and the Chat Completions endpoint ([model details](https://developers.openai.com/api/docs/models/gpt-5.4-mini), [image and vision guide](https://developers.openai.com/api/docs/guides/images-vision)). The UI then identifies analysis as a configured vision provider and shows the provider response's confidence estimate and explanation. Treat these as model suggestions: citizens must review and can change the category and severity before submitting. When configured, the selected photo is sent to that provider for analysis. The server never returns the API key to the browser.

If a provider key is set but the endpoint fails, analysis reports an error rather than switching to a simulated AI result.

## Configure a non-demo deployment

Edit `.env` before starting the service:

```dotenv
CIVICLENS_DEMO_MODE=false
CIVICLENS_SECRET_KEY=replace-with-a-long-random-secret
CIVICLENS_ADMIN_EMAIL=operations@example.org
CIVICLENS_ADMIN_PASSWORD=replace-with-a-unique-long-password
CIVICLENS_ADMIN_NAME=City Operations
CIVICLENS_ALLOWED_ORIGIN=http://127.0.0.1:8000
```

The bootstrap administrator is created on first start. Citizen registration remains enabled. Demo records and demo accounts are not created for a new database in this mode. Configure a different administrator password and signing key before launch. Use HTTPS behind a trusted reverse proxy for a network deployment, restrict access to the server and its data folders, and back up the database and uploads together.

SQLite data defaults to `backend/data/civiclens.sqlite3`. Uploaded images default to `backend/data/uploads/`. Optional `CIVICLENS_DB_PATH`, `CIVICLENS_DATA_DIR`, and `CIVICLENS_UPLOAD_DIR` variables can move them to a protected persistent volume. Do not put the upload directory under a public static web root.

### Email receipts and status alerts

CIVICLENS can send a receipt after a report is stored and a status email after a staff update. Configure `CIVICLENS_SMTP_HOST`, `CIVICLENS_SMTP_PORT`, `CIVICLENS_EMAIL_FROM`, and the SMTP username/password in the deployment's secret settings. SMTP credentials must never be committed to Git. STARTTLS is enabled by default on port 587; set `CIVICLENS_SMTP_USE_SSL=true` for an SSL SMTP endpoint such as port 465. If mail is not configured or delivery fails, the report is still saved and the interface says the email was not delivered.

Every report starts a 48-hour CIVICLENS first-review target. The due date appears in the citizen report view and overdue items appear in the administrator queue. A staff update records the first response and triggers an email if SMTP is configured. This is a CIVICLENS service target, not a guarantee of an official department's investigation or final decision.

### Private official-conduct concerns

Citizens can submit a confidential concern involving a government official, police officer, or other public official. The report is stored in SQLite but excluded from public report lists, map markers, public counters, community verification, and duplicate suggestions. The reporter and signed-in CIVICLENS administrators can access it. These reports should be written as factual concerns; they are allegations for review, not findings of wrongdoing. The application is an independent platform, not an official complaint channel or emergency service.

### Offline and resolution workflows

The installable web app caches its local interface shell and can save ordinary report drafts with photos in the current browser's IndexedDB. A report submitted while offline is queued and sent after the same account reconnects. Drafts are device-local, not encrypted backups; avoid using shared devices. Confidential official-conduct reports are deliberately not stored offline. Staff can attach completion photos to resolved reports, and the original reporter can confirm the resolution or request that a report be reopened.

## API and data

FastAPI publishes interactive API documentation at <http://127.0.0.1:8000/docs> and OpenAPI JSON at <http://127.0.0.1:8000/openapi.json>.

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `GET /api/health`, `GET /api/config`, `GET /api/stats` | Public | Service mode and aggregate counters |
| `POST /api/auth/register`, `POST /api/auth/login` | Public | Citizen registration and bearer-token login |
| `GET/PATCH /api/profile` | Signed in | View or update the current profile |
| `POST /api/ai/analyze` | Signed in, rate-limited | Configured vision analysis or labeled text-only demo hint |
| `GET /api/reports` | Public | PII-free map/list records with filters |
| `GET /api/reports/{id}` | Public; private fields for owner/admin | Report details and public status timeline |
| `POST /api/reports` | Signed in | Save a report, private official-conduct concern, or offline retry; SMTP receipt when configured |
| `GET /api/reports/mine`, `GET /api/notifications` | Signed in | Citizen history and updates |
| `POST /api/reports/{id}/verify` | Signed in | One community confirmation per user |
| `POST /api/reports/{id}/reopen` | Original reporter | Request another review of a resolved report |
| `POST /api/reports/{id}/confirm-resolution` | Original reporter | Confirm that a resolved issue is fixed |
| `GET /api/duplicates?report_id={id}` | Administrator | Potential duplicate suggestions, including photo similarity |
| `GET /api/admin/reports`, `PATCH /api/admin/reports/{id}` | Administrator | Queue and report updates |
| `POST /api/admin/reports/{id}/notes` | Administrator | Staff-only internal note |
| `POST /api/admin/reports/{id}/resolution-evidence` | Administrator | Attach a completion photo and caption to a resolved report |
| `POST /api/admin/reports/{id}/merge` | Administrator | Link a duplicate while retaining its record/history |
| `GET /api/analytics`, `GET /api/admin/audit` | Administrator | Stored-record analytics and audit history |
| `GET /api/admin/export.csv` | Administrator | Download a CSV report export |
| `GET /api/reports/{id}/photo` | Owner or administrator | Read a private uploaded image |

Authentication uses signed bearer tokens with a configurable expiry. Passwords are stored as salted PBKDF2 hashes. Photo uploads are decoded and re-encoded as JPEG before storage, which removes embedded metadata such as GPS tags. AI analysis requires sign-in and has per-address and per-account rate limits. Report filters reject unknown categories, severities, statuses, malformed dates, and reversed date ranges. The public map and public statistics omit account emails and names. This demo stores data on the host machine; protect `.env`, `backend/data/`, database backups, and photo uploads.

## Manual acceptance walkthrough

1. Sign in as the demo citizen. Open **Report an issue**, add a photo, and run **Analyze photo**. Confirm that the result explicitly says demo mode and that confidence is unavailable. Choose category and severity, enter a description and landmark, set coordinates on the map or type them, then submit.
2. Open **My reports** and check the new report ID, submitted status, and timeline. Open the report and check that your photo is accessible while signed in.
3. Sign out and sign in as the demo administrator. Open **City operations**, verify the new report, change its status and assignment, add an internal note, and save an ImpactScore override with a reason.
4. Open the report as an administrator and inspect its timeline and audit trail. Try the duplicate check, analytics, and CSV export.
5. Return to **Civic map** as a signed-out visitor. Filter the map and list; confirm the report appears without its reporter's name or email and the private photo cannot be opened.

This repository does not include an automated test suite. The walkthrough above is a manual functional check to run after setup.

## Project layout

```text
civiclens/
├── index.html                 # CIVICLENS citizen and city operations UI
├── styles.css                 # Responsive design system and map/dashboard styling
├── app.js                     # Browser interactions and API client
├── locales.js                 # Localized interface strings and language preference
├── backend/
│   ├── server.py              # FastAPI routes, authentication, SQLite, scoring and uploads
│   ├── schema.sql             # Persistent database schema
│   ├── requirements.txt       # Python dependencies
│   └── data/                  # Created at runtime: SQLite database and private photos
├── .env.example               # Local configuration template
├── .gitignore
├── start_civiclens.ps1        # Windows launcher
└── stop_civiclens.ps1         # Stops the background API server
```

No ESP32, fixed-camera, messaging, or municipal dispatch integration is presented as operational. Those can be added behind the API as future integrations.
