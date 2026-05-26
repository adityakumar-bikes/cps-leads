# Bike CPS Lead Delivery Dashboard

A self-refreshing dashboard for BikeDekho CPS lead data. Hosted on **GitHub Pages**, data pipeline runs on **GitHub Actions**, source files live in **Google Drive**.

---

## Architecture

```
Google Drive folder
  └── 13 Google Sheets files
         │
         │  (GitHub Actions: refresh.yml — daily + manual)
         ▼
  scripts/refresh_data.py
         │  exports each Sheet as ZIP → per-sheet CSVs
         │  parses brand data, deduplicates, aggregates
         ▼
  data/dashboard_data.json   ← committed back to repo
  data/manifest.json         ← tracks processed files
         │
         │  (GitHub Pages serves the repo)
         ▼
  index.html                 ← fetches dashboard_data.json on load
  [Refresh button]           ← triggers the workflow via GitHub API
```

---

## One-time Setup (15 minutes)

### Step 1 — Create a Google Service Account

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create a new project (e.g. `cps-dashboard`)
2. Enable the **Google Drive API**: APIs & Services → Library → search "Drive API" → Enable
3. Create a Service Account: APIs & Services → Credentials → Create Credentials → Service Account
   - Name: `cps-dashboard-reader`
   - Role: none needed (we'll share the folder directly)
4. On the Service Account page → Keys tab → Add Key → JSON → download the file
5. **Share the Drive folder** with the service account email (looks like `cps-dashboard-reader@your-project.iam.gserviceaccount.com`) — Viewer access is enough

### Step 2 — Create the GitHub repo

```bash
cd cps-lead-dashboard
git init
git add .
git commit -m "init: CPS lead dashboard"
# Create a new repo on GitHub, then:
git remote add origin https://github.com/YOUR_USERNAME/cps-lead-dashboard.git
git push -u origin main
```

### Step 3 — Add the Google credentials as a GitHub Secret

1. GitHub repo → Settings → Secrets and variables → Actions → New repository secret
2. Name: `GOOGLE_SERVICE_ACCOUNT_JSON`
3. Value: paste the **entire contents** of the JSON key file downloaded in Step 1

### Step 4 — Enable GitHub Pages

1. GitHub repo → Settings → Pages
2. Source: **Deploy from a branch** → branch: `main` → folder: `/ (root)`
3. Save — your dashboard will be live at `https://YOUR_USERNAME.github.io/cps-lead-dashboard/`

### Step 5 — Configure the Refresh button (optional but recommended)

The in-page **Refresh Data** button can trigger the GitHub Actions workflow directly from the browser.

1. Create a **fine-grained Personal Access Token** (PAT):
   - GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens
   - Repository access: **Only select repositories** → pick `cps-lead-dashboard`
   - Permissions: **Actions** → Read and write
   - Generate token → copy it

2. Open `index.html`, find this section near the bottom of the `<script>` block:
   ```js
   const GH_OWNER    = '';    // e.g. 'myusername'
   const GH_REPO     = '';    // e.g. 'cps-lead-dashboard'
   const GH_PAT      = '';    // paste your PAT here
   ```
   Fill in your GitHub username, repo name, and PAT. Commit and push.

> **Note:** The PAT is visible in the page source. This is acceptable for an internal tool, but don't use this on a public-facing site with sensitive data.

---

## Running the pipeline manually

```bash
pip install -r requirements.txt
# Place credentials.json (service account key) in the repo root
python scripts/refresh_data.py
```

---

## How new files get picked up

The pipeline tracks file IDs and `modifiedTime` in `data/manifest.json`. On each run:
- Files already in the manifest with the **same `modifiedTime`** are skipped
- **New files** or files with a **newer `modifiedTime`** are re-exported and reprocessed
- The entire dataset is re-deduplicated after ingesting new rows

This means you can simply **add a new monthly file** to the Google Drive folder and the next pipeline run will automatically pick it up.

---

## Fixing Bajaj / KTM / Triumph data

The original dashboard had incomplete Bajaj data because the in-browser tool could only read ~99 rows per file. **This pipeline has no such limitation** — it uses the Google Drive API's ZIP export (all sheets as CSV), so Bajaj and all other brands will have **complete** data after the first pipeline run.

---

## File structure

```
cps-lead-dashboard/
├── index.html                  ← dashboard (fetches data/dashboard_data.json)
├── data/
│   ├── dashboard_data.json     ← aggregated data (auto-committed by Actions)
│   ├── all_leads.json          ← raw deduplicated lead rows
│   └── manifest.json           ← pipeline state (processed files + timestamps)
├── scripts/
│   └── refresh_data.py         ← data pipeline
├── .github/
│   └── workflows/
│       └── refresh.yml         ← GitHub Actions workflow (daily + manual)
├── requirements.txt
└── README.md
```
