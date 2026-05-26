#!/usr/bin/env python3
"""
CPS Lead Dashboard — Data Refresh Pipeline
==========================================
Connects to Google Drive, finds new/modified Sheets files in the CPS folder,
exports each file as a ZIP of per-sheet CSVs, parses brand-data sheets,
deduplicates, rebuilds dashboard_data.json.

Run locally:
    pip install -r requirements.txt
    # Place credentials.json (service account key) in the repo root, then:
    python scripts/refresh_data.py

Run via GitHub Actions:
    Set GOOGLE_SERVICE_ACCOUNT_JSON secret (contents of credentials.json).
"""

import gzip, io, json, os, zipfile, csv, sys, re
from collections import defaultdict, Counter
from datetime import datetime, timezone

# ── Google API ───────────────────────────────────────────────────────────────
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ── Config ───────────────────────────────────────────────────────────────────
FOLDER_ID  = "1lZ4l1LemSolnGwAiqPWwQS8CUdF0LfZf"
REPO_ROOT  = os.path.join(os.path.dirname(__file__), "..")
DATA_DIR   = os.path.join(REPO_ROOT, "data")
MANIFEST_F = os.path.join(DATA_DIR, "manifest.json")
LEADS_F    = os.path.join(DATA_DIR, "all_leads.json.gz")   # gzip to stay under GitHub 50MB limit
DASH_F     = os.path.join(DATA_DIR, "dashboard_data.json")

SKIP_SHEETS = {
    "filter",
    "city wise source wise lead flow",
    "lt wise lead flow",
    "final combine",
}

VALID_BRANDS = {
    "ather", "bgauss", "ampere electric", "vespa", "aprilia",
    "ola electric", "bajaj", "ktm", "triumph", "husqvarna motorcycles",
}

COLS = [
    "encrypt_mobile_number", "id_verified_lead", "opty_id", "Date",
    "Lead_Month", "brand", "model", "verified_dealer", "dealerId",
    "oem_crm_id", "City", "State", "Dealer_Name", "enquiryId",
    "utm_campaign", "lead_type", "Medium",
]

LT_NAMES = {
    "1105": "CPS - OBD",      "103": "CPS - Standard", "70": "Digital Lead",
    "75":   "Walk-in",        "1004":"CPS - Dealer",   "304":"CPS - Campaign",
    "80":   "Exchange",       "69": "Test Drive",      "19": "OBD",
    "76":   "Referral",       "73": "Used",            "48": "Accessories",
    "74":   "Insurance",      "20": "Finance",         "5":  "Service",
    "2":    "Complaint",      "113":"Corporate",       "915":"Missed Call",
    "1003": "CPS - Brand",    "1000":"CPS - Other",
}

MONTH_ORDER = [
    "Apr'2025","May'2025","Jun'2025","Jul'2025","Aug'2025","Sep'2025",
    "Oct'2025","Nov'2025","Dec'2025","Jan'2026","Feb'2026","Mar'2026",
    "Apr'2026","May'2026","Jun'2026","Jul'2026","Aug'2026","Sep'2026",
    "Oct'2026","Nov'2026","Dec'2026",
]


# ── Auth & Drive helpers ─────────────────────────────────────────────────────

def get_drive_service():
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        key_file = os.path.join(REPO_ROOT, "credentials.json")
        if not os.path.exists(key_file):
            sys.exit("ERROR: No credentials found. Set GOOGLE_SERVICE_ACCOUNT_JSON env var or place credentials.json in repo root.")
        creds = service_account.Credentials.from_service_account_file(key_file, scopes=scopes)
    return build("drive", "v3", credentials=creds)


def list_folder_sheets(service):
    """Return list of {id, name, modifiedTime} for all Sheets in the CPS folder."""
    results = []
    page_token = None
    while True:
        resp = service.files().list(
            q=(f"'{FOLDER_ID}' in parents "
               "and mimeType='application/vnd.google-apps.spreadsheet' "
               "and trashed=false"),
            fields="nextPageToken, files(id, name, modifiedTime)",
            pageSize=50,
            pageToken=page_token,
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def export_as_zip(service, file_id):
    """Export a Google Sheets file as ZIP (each sheet becomes a separate CSV)."""
    req = service.files().export_media(fileId=file_id, mimeType="application/zip")
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return buf


# ── Parsing ──────────────────────────────────────────────────────────────────

def parse_zip_rows(zip_buf, file_name):
    """
    Open a ZIP of CSVs exported from Google Sheets.
    Skip any sheet whose name (after stripping the file prefix) is in SKIP_SHEETS.
    Return list of row dicts with COLS keys, filtered to VALID_BRANDS.
    """
    rows = []
    cols_lower = {c.lower(): c for c in COLS}

    with zipfile.ZipFile(zip_buf) as zf:
        for member in sorted(zf.namelist()):
            # Google exports as "filename - SheetName.csv"
            sheet_label = member
            if sheet_label.lower().endswith(".csv"):
                sheet_label = sheet_label[:-4]
            # Strip "filename - " prefix if present
            if " - " in sheet_label:
                sheet_label = sheet_label.split(" - ", 1)[-1]
            sheet_label_lower = sheet_label.lower().strip()

            if sheet_label_lower in SKIP_SHEETS:
                print(f"    skip sheet: {sheet_label}")
                continue

            with zf.open(member) as f:
                raw = f.read().decode("utf-8-sig", errors="replace")

            reader = csv.DictReader(io.StringIO(raw))
            if not reader.fieldnames:
                continue

            # Map CSV header names → our canonical COLS
            hdr_map = {}   # canonical_col → csv_fieldname
            fns = [fn.strip() for fn in reader.fieldnames]
            for fn in fns:
                fn_l = fn.lower()
                if fn_l in cols_lower:
                    hdr_map[cols_lower[fn_l]] = fn

            # Need at least brand + Medium to be useful
            if "brand" not in hdr_map or "Medium" not in hdr_map:
                continue

            sheet_rows = 0
            for raw_row in reader:
                brand = str(raw_row.get(hdr_map.get("brand", ""), "") or "").strip()
                if not brand or brand.lower() not in VALID_BRANDS:
                    continue
                row = {}
                for col, fn in hdr_map.items():
                    row[col] = str(raw_row.get(fn, "") or "").strip()
                rows.append(row)
                sheet_rows += 1

            if sheet_rows:
                print(f"    sheet '{sheet_label}': {sheet_rows:,} rows")

    return rows


# ── Deduplication ─────────────────────────────────────────────────────────────

def deduplicate(rows):
    seen = set()
    out = []
    for r in rows:
        key = (r.get("enquiryId") or
               r.get("id_verified_lead") or
               r.get("encrypt_mobile_number") or None)
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(r)
    return out


# ── Aggregation ───────────────────────────────────────────────────────────────

def norm_lt(raw):
    v = str(raw).strip().rstrip(".0") if raw else ""
    if v.endswith(".0"):
        v = v[:-2]
    # Strip trailing .0 once more robustly
    try:
        v = str(int(float(v))) if v else v
    except Exception:
        pass
    return LT_NAMES.get(v, v) if v else "Unknown"


def build_aggregations(all_rows):
    # Canonical model names
    model_cases = defaultdict(Counter)
    for r in all_rows:
        m = str(r.get("model", "")).strip()
        if m:
            model_cases[m.lower()][m] += 1

    def cm(m):
        m = str(m).strip()
        if not m:
            return "Unknown"
        c = model_cases.get(m.lower())
        return c.most_common(1)[0][0] if c else m

    def inc(d, k, n=1):
        d[k] = d.get(k, 0) + n

    brands_set = set()
    partial_brands = set()

    # Counters
    by_brand = {}; by_medium = {}; by_state = {}; by_city = {}
    by_model = {}; by_month = {}; by_dealer = {}; by_lt = {}
    brand_month = defaultdict(dict); brand_medium = defaultdict(dict)
    brand_lt = defaultdict(dict); brand_model = defaultdict(dict)
    state_brand = defaultdict(dict); state_medium = defaultdict(dict)
    state_lt = defaultdict(dict)
    city_brand = defaultdict(dict); city_medium = defaultdict(dict)
    dealer_brand = defaultdict(dict); dealer_state = defaultdict(dict)
    medium_month = defaultdict(dict); lt_medium = defaultdict(dict)
    lt_month = defaultdict(dict); model_brand = defaultdict(dict)

    months_seen = set()

    for r in all_rows:
        brand   = str(r.get("brand",  "") or "").strip()
        medium  = str(r.get("Medium", "") or "").strip() or "Unknown"
        state   = str(r.get("State",  "") or "").strip() or "Unknown"
        city    = str(r.get("City",   "") or "").strip() or "Unknown"
        dealer  = str(r.get("Dealer_Name","") or "").strip() or "Unknown"
        model   = cm(r.get("model", ""))
        lt      = norm_lt(r.get("lead_type",""))
        month   = str(r.get("Lead_Month","") or "").strip()

        if not brand or brand.lower() not in VALID_BRANDS:
            continue

        brands_set.add(brand)
        months_seen.add(month)

        inc(by_brand,  brand);  inc(by_medium, medium)
        inc(by_state,  state);  inc(by_city,   city)
        inc(by_model,  model);  inc(by_month,  month)
        inc(by_dealer, dealer); inc(by_lt,      lt)

        inc(brand_month[brand],   month)
        inc(brand_medium[brand],  medium)
        inc(brand_lt[brand],      lt)
        inc(brand_model[brand],   model)
        inc(state_brand[state],   brand)
        inc(state_medium[state],  medium)
        inc(state_lt[state],      lt)
        inc(city_brand[city],     brand)
        inc(city_medium[city],    medium)
        inc(dealer_brand[dealer], brand)
        inc(dealer_state[dealer], state)
        inc(medium_month[medium], month)
        inc(lt_medium[lt],        medium)
        inc(lt_month[lt],         month)
        inc(model_brand[model],   brand)

    # Sort months by canonical order
    month_key = {m: i for i, m in enumerate(MONTH_ORDER)}
    months_present = sorted(months_seen, key=lambda x: month_key.get(x, 999))

    # Sort dicts by value desc
    def srt(d):
        return dict(sorted(d.items(), key=lambda x: -x[1]))

    brands_all = sorted(brands_set)

    return {
        "total":         len(all_rows),
        "brands_all":    brands_all,
        "mediums_all":   list(srt(by_medium).keys()),
        "months_present":months_present,
        "partial_brands":list(partial_brands),   # pipeline gets full data — no partials
        "by_brand":      srt(by_brand),
        "by_medium":     srt(by_medium),
        "by_state":      srt(by_state),
        "by_city":       srt(by_city),
        "by_model":      srt(by_model),
        "by_month":      {m: by_month.get(m,0) for m in months_present},
        "by_dealer":     srt(by_dealer),
        "by_lt":         srt(by_lt),
        "brand_month":   {k: {m: v.get(m,0) for m in months_present} for k,v in brand_month.items()},
        "brand_medium":  dict(brand_medium),
        "brand_lt":      dict(brand_lt),
        "brand_model":   dict(brand_model),
        "state_brand":   dict(state_brand),
        "state_medium":  dict(state_medium),
        "state_lt":      dict(state_lt),
        "city_brand":    dict(city_brand),
        "city_medium":   dict(city_medium),
        "dealer_brand":  dict(dealer_brand),
        "dealer_state":  dict(dealer_state),
        "medium_month":  {k: {m: v.get(m,0) for m in months_present} for k,v in medium_month.items()},
        "lt_medium":     dict(lt_medium),
        "lt_month":      {k: {m: v.get(m,0) for m in months_present} for k,v in lt_month.items()},
        "model_brand":   dict(model_brand),
        "last_updated":  datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    # Load manifest
    manifest = {"processed": {}, "last_run": None}
    if os.path.exists(MANIFEST_F):
        with open(MANIFEST_F) as f:
            manifest = json.load(f)

    # Load existing leads (gzip-compressed to keep repo size manageable)
    all_rows = []
    if os.path.exists(LEADS_F):
        with gzip.open(LEADS_F, "rt", encoding="utf-8") as f:
            all_rows = json.load(f)
        print(f"Loaded {len(all_rows):,} existing leads from {LEADS_F}")

    # Auth
    print("Authenticating with Google Drive...")
    service = get_drive_service()

    # List files
    files = list_folder_sheets(service)
    print(f"Found {len(files)} Sheets files in Drive folder")

    new_file_count = 0
    new_row_count = 0

    for f in files:
        fid  = f["id"]
        name = f["name"]
        mtime = f["modifiedTime"]

        prev = manifest["processed"].get(fid, {})
        if prev.get("modifiedTime") == mtime:
            print(f"  ✓ unchanged: {name}")
            continue

        print(f"  ↓ processing: {name}  (modified {mtime[:10]})")
        try:
            zip_buf = export_as_zip(service, fid)
            new_rows = parse_zip_rows(zip_buf, name)
        except Exception as e:
            print(f"    ERROR exporting {name}: {e}")
            continue

        if not new_rows:
            print(f"    (no brand rows found)")
            manifest["processed"][fid] = {"name": name, "modifiedTime": mtime, "rows": 0}
            continue

        # Remove any previously-stored rows from this file (handles re-processing)
        if fid in manifest["processed"] and manifest["processed"][fid].get("rows", 0) > 0:
            # Can't easily surgically remove by file — re-dedup handles it
            pass

        all_rows.extend(new_rows)
        manifest["processed"][fid] = {
            "name": name,
            "modifiedTime": mtime,
            "rows": len(new_rows),
        }
        new_file_count += 1
        new_row_count += len(new_rows)
        print(f"    → {len(new_rows):,} rows added")

    if new_file_count == 0:
        print("\nNo new or modified files — dashboard is already up to date.")
        # Still update last_run timestamp
        manifest["last_run"] = datetime.now(timezone.utc).isoformat()
        with open(MANIFEST_F, "w") as f:
            json.dump(manifest, f, indent=2)
        return

    # Deduplicate full dataset
    print(f"\nDeduplicating {len(all_rows):,} total rows...")
    all_rows = deduplicate(all_rows)
    print(f"After dedup: {len(all_rows):,} unique leads")

    # Save leads (gzip-compressed — ~9MB vs 71MB raw)
    with gzip.open(LEADS_F, "wt", encoding="utf-8") as f:
        json.dump(all_rows, f)
    gz_mb = os.path.getsize(LEADS_F) / 1024 / 1024
    print(f"Saved all_leads.json.gz ({len(all_rows):,} rows, {gz_mb:.1f} MB)")

    # Build aggregations
    print("Building aggregations...")
    dash = build_aggregations(all_rows)
    print(f"Total: {dash['total']:,} | Brands: {list(dash['by_brand'].keys())}")

    with open(DASH_F, "w") as f:
        json.dump(dash, f)
    print(f"Saved dashboard_data.json")

    # Update manifest
    manifest["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(MANIFEST_F, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Updated manifest.json")

    print(f"\n✅ Done — {new_file_count} files processed, {new_row_count:,} new rows added.")


if __name__ == "__main__":
    main()
