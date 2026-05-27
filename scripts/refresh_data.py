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

import gzip, io, json, os, zipfile, csv, sys, re, socket, time
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
import openpyxl

IST = timezone(timedelta(hours=5, minutes=30))

# Large Bajaj files need longer timeout (default is ~60s)
socket.setdefaulttimeout(600)   # 10 minutes

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

# Sheets to ALWAYS skip regardless of name (summary/pivot sheets)
SKIP_SHEETS = {
    "filter",
    "city wise source wise lead flow",
    "lt wise lead flow",
    "final combine",
    "city wise",
    "lt wise",
    "source wise",
    "summary",
    "overview",
    "roi",
    "model wise",
    "dealer wise",
    "state wise",
}

VALID_BRANDS = {
    "ather", "bgauss", "ampere electric", "ampere", "vespa", "aprilia", "piaggio",
    "ola electric", "ola", "bajaj", "bajaj chetak", "ktm", "triumph",
    "husqvarna motorcycles", "husqvarna",
}

# Normalize short brand names → canonical display names (for consistent aggregation)
BRAND_NORMALIZE = {
    "ampere": "Ampere Electric",
    "ampere electric": "Ampere Electric",
    "ola": "Ola Electric",
    "ola electric": "Ola Electric",
    "bajaj chetak": "Bajaj",
    "husqvarna": "Husqvarna Motorcycles",
}

def is_brand_sheet(sheet_label: str) -> bool:
    """Return True only if this sheet name looks like a brand data sheet.

    Rules (applied to lowercased, stripped sheet label):
      1. Must contain at least one valid brand name   — OR —
         be a plain month label (e.g. "apr'25", "feb 2026")
      2. Must NOT be in the hard SKIP_SHEETS list
    """
    sl = sheet_label.lower().strip()

    # Hard skip list wins
    if sl in SKIP_SHEETS:
        return False
    # Also partial-match skip list (e.g. "city wise source wise …")
    for skip in SKIP_SHEETS:
        if skip in sl:
            return False

    # Accept if the sheet name contains a brand (or first word of multi-word brand)
    for brand in VALID_BRANDS:
        if brand in sl:
            return True
        # e.g. sheet "Ampere" should match brand "ampere electric"
        first_word = brand.split()[0]
        if len(first_word) >= 4 and first_word in sl:
            return True

    # Accept plain month sheets: "apr'25", "feb'26", "jan 2026", etc.
    import re as _re
    if _re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", sl):
        return True

    return False

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
    "Jan'2025","Feb'2025","Mar'2025","Apr'2025","May'2025","Jun'2025",
    "Jul'2025","Aug'2025","Sep'2025","Oct'2025","Nov'2025","Dec'2025",
    "Jan'2026","Feb'2026","Mar'2026","Apr'2026","May'2026","Jun'2026",
    "Jul'2026","Aug'2026","Sep'2026","Oct'2026","Nov'2026","Dec'2026",
]

# Months to exclude from all aggregations and dashboard output
SKIP_MONTHS = {"Mar'2025"}


# ── Auth & Drive helpers ─────────────────────────────────────────────────────

def get_services():
    """Return (drive_service, sheets_service) both authenticated."""
    scopes = [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/spreadsheets.readonly",
    ]
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        key_file = os.path.join(REPO_ROOT, "credentials.json")
        if not os.path.exists(key_file):
            sys.exit("ERROR: No credentials found. Set GOOGLE_SERVICE_ACCOUNT_JSON env var or place credentials.json in repo root.")
        creds = service_account.Credentials.from_service_account_file(key_file, scopes=scopes)
    drive_svc  = build("drive",   "v3", credentials=creds)
    sheets_svc = build("sheets",  "v4", credentials=creds)
    return drive_svc, sheets_svc


def get_drive_service():
    """Backward-compat wrapper — returns only the Drive service."""
    drive_svc, _ = get_services()
    return drive_svc


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


class FileTooLargeError(Exception):
    """Raised when Google Drive rejects the ZIP export due to file size."""
    pass


def export_as_zip(service, file_id, max_retries=3):
    """Export a Google Sheets file as ZIP (each sheet becomes a separate CSV).
    Retries up to max_retries times on timeout or transient errors.
    Raises FileTooLargeError immediately (no retry) when exportSizeLimitExceeded."""
    for attempt in range(max_retries):
        try:
            req = service.files().export_media(fileId=file_id, mimeType="application/zip")
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, req, chunksize=20 * 1024 * 1024)  # 20 MB chunks
            done = False
            while not done:
                _, done = dl.next_chunk()
            buf.seek(0)
            return buf
        except Exception as e:
            err_str = str(e)
            if "exportSizeLimitExceeded" in err_str or "too large to be exported" in err_str.lower():
                raise FileTooLargeError(f"exportSizeLimitExceeded for {file_id}") from e
            if attempt < max_retries - 1:
                wait = 15 * (attempt + 1)
                print(f"    ⚠ Attempt {attempt+1} failed ({e}), retrying in {wait}s…")
                time.sleep(wait)
            else:
                # After all retries, fall back to Sheets API for any server error on Sheets files
                raise FileTooLargeError(f"ZIP export failed after {max_retries} attempts — trying Sheets API") from e


# ── Sheets API v4 fallback ───────────────────────────────────────────────────

def export_via_sheets_api(sheets_svc, file_id, file_name):
    """
    Read all brand-data sheets from a Spreadsheet using the Sheets API v4.
    Used as a fallback when the Drive ZIP export fails with exportSizeLimitExceeded.
    Returns list of row dicts with COLS keys, same as parse_zip_rows().
    """
    rows = []
    cols_lower = {c.lower(): c for c in COLS}

    # Step 1: list all sheets in the workbook (with retries)
    meta = None
    for attempt in range(4):
        try:
            meta = sheets_svc.spreadsheets().get(
                spreadsheetId=file_id,
                fields="sheets.properties.title",
            ).execute()
            break
        except Exception as e:
            if attempt < 3:
                wait = 20 * (attempt + 1)
                print(f"    ⚠ Sheets API metadata attempt {attempt+1} failed ({e}), retrying in {wait}s…")
                time.sleep(wait)
            else:
                raise
    sheet_titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    print(f"    Sheets API: found {len(sheet_titles)} sheets in '{file_name}'")

    for title in sheet_titles:
        if not is_brand_sheet(title):
            print(f"    skip sheet: {title}")
            continue
        print(f"    reading sheet: {title}")

        # Step 2: read all data from this sheet (with retries for transient errors)
        result = None
        for attempt in range(4):
            try:
                result = sheets_svc.spreadsheets().values().get(
                    spreadsheetId=file_id,
                    range=f"'{title}'",   # no column limit → full sheet
                    valueRenderOption="FORMATTED_VALUE",
                    dateTimeRenderOption="FORMATTED_STRING",
                ).execute()
                break
            except Exception as e:
                if attempt < 3:
                    wait = 20 * (attempt + 1)
                    print(f"    ⚠ Sheets API attempt {attempt+1} failed for '{title}' ({e}), retrying in {wait}s…")
                    time.sleep(wait)
                else:
                    print(f"    ERROR reading sheet '{title}' after 4 attempts: {e}")
        if result is None:
            continue

        values = result.get("values", [])
        if not values or len(values) < 2:
            print(f"    sheet '{title}': empty or header-only, skipping")
            continue

        # First non-empty row is the header
        header_row = values[0]
        header = [str(h).strip() for h in header_row]

        # Map header → canonical COLS (first occurrence wins — sheets can have duplicate col names)
        hdr_map = {}   # canonical_col → column_index
        for idx, h in enumerate(header):
            h_l = h.lower()
            if h_l in cols_lower and cols_lower[h_l] not in hdr_map:
                hdr_map[cols_lower[h_l]] = idx

        if "brand" not in hdr_map or "Medium" not in hdr_map:
            print(f"    skip sheet '{title}': missing columns (found: {list(hdr_map.keys())[:6]})")
            continue

        brand_idx  = hdr_map["brand"]
        sheet_rows = 0
        for data_row in values[1:]:
            # Pad row to at least brand column width
            if len(data_row) <= brand_idx:
                continue
            brand = str(data_row[brand_idx]).strip()
            if not brand or brand.lower() not in VALID_BRANDS:
                continue
            row = {}
            for col, idx in hdr_map.items():
                row[col] = str(data_row[idx]).strip() if idx < len(data_row) else ""
            # Normalize brand name in stored row
            row["brand"] = BRAND_NORMALIZE.get(row["brand"].lower(), row["brand"])
            rows.append(row)
            sheet_rows += 1

        if sheet_rows:
            print(f"    sheet '{title}': {sheet_rows:,} rows")

    return rows


# ── Parsing ──────────────────────────────────────────────────────────────────

def parse_zip_rows(zip_buf, file_name):
    """
    Open a ZIP of CSVs exported from Google Sheets.
    Only process sheets whose name matches a brand name or month label.
    Skips all summary/pivot sheets (Filter, City Wise, LT Wise, etc.).
    Return list of row dicts with COLS keys, filtered to VALID_BRANDS.
    """
    rows = []
    cols_lower = {c.lower(): c for c in COLS}

    with zipfile.ZipFile(zip_buf) as zf:
        members = sorted(zf.namelist())
        # If Google returned HTML instead of CSV (happens with some older/large files),
        # fall back to Sheets API rather than trying to parse HTML as CSV.
        has_csv  = any(m.lower().endswith(".csv")  for m in members)
        has_html = any(m.lower().endswith(".html") for m in members)
        if has_html and not has_csv:
            raise FileTooLargeError(f"ZIP contains HTML not CSV — needs Sheets API fallback")

        for member in members:
            # Google exports as "filename - SheetName.csv"
            sheet_label = member
            if sheet_label.lower().endswith(".csv"):
                sheet_label = sheet_label[:-4]
            # Strip "filename - " prefix if present
            if " - " in sheet_label:
                sheet_label = sheet_label.split(" - ", 1)[-1]

            if not is_brand_sheet(sheet_label):
                print(f"    skip sheet: {sheet_label}")
                continue
            print(f"    reading sheet: {sheet_label}")

            with zf.open(member) as f:
                raw = f.read().decode("utf-8-sig", errors="replace")

            reader = csv.DictReader(io.StringIO(raw))
            if not reader.fieldnames:
                continue

            # Map CSV header names → our canonical COLS (first occurrence wins)
            hdr_map = {}   # canonical_col → csv_fieldname
            fns = [fn.strip() for fn in reader.fieldnames]
            for fn in fns:
                fn_l = fn.lower()
                if fn_l in cols_lower and cols_lower[fn_l] not in hdr_map:
                    hdr_map[cols_lower[fn_l]] = fn

            # Need at least brand + Medium to be useful
            if "brand" not in hdr_map or "Medium" not in hdr_map:
                print(f"    skip sheet '{sheet_label}': missing columns (found: {list(hdr_map.keys())[:6]})")
                continue

            sheet_rows = 0
            for raw_row in reader:
                brand = str(raw_row.get(hdr_map.get("brand", ""), "") or "").strip()
                if not brand or brand.lower() not in VALID_BRANDS:
                    continue
                row = {}
                for col, fn in hdr_map.items():
                    row[col] = str(raw_row.get(fn, "") or "").strip()
                # Normalize brand name
                row["brand"] = BRAND_NORMALIZE.get(row["brand"].lower(), row["brand"])
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
        key = (
            r.get("brand")                  or "",
            r.get("encrypt_mobile_number")  or "",
            r.get("Lead_Month")             or "",
            r.get("opty_id")                or "",
        )
        if any(key):
            if key in seen:
                continue
            seen.add(key)
        out.append(r)
    return out


# ── BU Mapping ────────────────────────────────────────────────────────────────

# Canonical BU names — any key (case-insensitive) maps to the value
BU_ALIASES = {
    'pb-trm':  'TRM',
    'triumph': 'TRM',
    'ktm':     'PB',
}

def normalize_bu(bu: str) -> str:
    """Apply BU_ALIASES to consolidate variant BU names into canonical ones."""
    return BU_ALIASES.get(bu.strip().lower(), bu)

def load_model_bu_mapping(repo_root):
    """Load Model Name → BU mapping from Bajaj Mapping.xlsx in repo root.
    Returns dict: {model_name_lowercase: BU_string}
    """
    xlsx_path = os.path.join(repo_root, "Bajaj Mapping.xlsx")
    if not os.path.exists(xlsx_path):
        print("Warning: 'Bajaj Mapping.xlsx' not found — BU data will be absent")
        return {}
    try:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not rows:
            return {}
        header = [str(c).strip().lower() if c else "" for c in rows[0]]
        model_idx = next((i for i, h in enumerate(header) if "model" in h), None)
        bu_idx    = next((i for i, h in enumerate(header) if h == "bu"), None)
        if model_idx is None or bu_idx is None:
            print(f"Warning: Bajaj Mapping.xlsx missing 'Model Name' or 'BU' column (found: {header})")
            return {}
        mapping = {}
        for row in rows[1:]:
            if len(row) <= max(model_idx, bu_idx):
                continue
            model = str(row[model_idx]).strip() if row[model_idx] else ""
            bu    = str(row[bu_idx]).strip()    if row[bu_idx]    else ""
            if model and bu:
                mapping[model.lower()] = bu
        print(f"Loaded BU mapping: {len(mapping)} model → BU entries")
        return mapping
    except Exception as e:
        print(f"Warning: Could not load Bajaj Mapping.xlsx: {e}")
        return {}


# ── Aggregation ───────────────────────────────────────────────────────────────

def norm_lt(raw):
    v = str(raw).strip() if raw else ""
    # Strip trailing .0 (e.g. "1105.0" → "1105")
    try:
        v = str(int(float(v))) if v else v
    except Exception:
        pass
    return v if v else "Unknown"


def build_aggregations(all_rows, model_to_bu=None):
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
    city_state_ctr = defaultdict(Counter)
    dealer_brand = defaultdict(dict); dealer_state = defaultdict(dict)
    dealer_medium = defaultdict(dict); dealer_month = defaultdict(dict)
    model_dealer = defaultdict(dict)
    medium_month = defaultdict(dict); lt_medium = defaultdict(dict)
    lt_month = defaultdict(dict); model_brand = defaultdict(dict)
    state_month  = defaultdict(dict); city_month  = defaultdict(dict)
    model_month  = defaultdict(dict)

    # BU counters
    by_bu = {}
    bu_brand  = defaultdict(dict); bu_medium = defaultdict(dict)
    bu_month  = defaultdict(dict); bu_state  = defaultdict(dict)
    bu_city   = defaultdict(dict); bu_dealer = defaultdict(dict)
    bu_lt     = defaultdict(dict); bu_model  = defaultdict(dict)
    model_bu_map = {}   # model (canonical case) → BU string
    dealer_code_map = {}  # dealer_key → verified_dealer code
    dealer_name_map = {}  # dealer_key → Dealer_Name

    months_seen = set()

    for r in all_rows:
        brand   = str(r.get("brand",  "") or "").strip()
        brand   = BRAND_NORMALIZE.get(brand.lower(), brand)   # normalize "Ampere"→"Ampere Electric" etc.
        medium  = str(r.get("Medium", "") or "").strip() or "Unknown"
        state   = str(r.get("State",  "") or "").strip() or "Unknown"
        city    = str(r.get("City",   "") or "").strip() or "Unknown"
        crm_id  = str(r.get("oem_crm_id","") or "").strip()
        d_city  = str(r.get("City","")       or "").strip()
        d_state = str(r.get("State","")      or "").strip()
        dealer  = f"{crm_id} · {d_city}, {d_state}" if crm_id else "Unknown"
        if dealer != "Unknown":
            d_code = str(r.get("verified_dealer","") or "").strip()
            d_name = str(r.get("Dealer_Name","")     or "").strip()
            if dealer not in dealer_code_map and d_code:
                dealer_code_map[dealer] = d_code
            if dealer not in dealer_name_map and d_name:
                dealer_name_map[dealer] = d_name
        model   = cm(r.get("model", ""))
        lt      = norm_lt(r.get("lead_type",""))
        month   = str(r.get("Lead_Month","") or "").strip()

        if not brand or brand.lower() not in VALID_BRANDS:
            continue
        if month in SKIP_MONTHS:
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
        city_state_ctr[city][state] += 1
        inc(dealer_brand[dealer],  brand)
        inc(dealer_state[dealer],  state)
        inc(dealer_medium[dealer], medium)
        inc(dealer_month[dealer],  month)
        inc(model_dealer[model],   dealer)
        inc(medium_month[medium], month)
        inc(lt_medium[lt],        medium)
        inc(lt_month[lt],         month)
        inc(model_brand[model],   brand)
        inc(state_month[state],   month)
        inc(city_month[city],     month)
        inc(model_month[model],   month)

        # BU aggregations — mapped models use the xlsx BU, all others fall back to brand
        bu = normalize_bu(model_to_bu.get(model.lower(), brand) if model_to_bu else brand)
        inc(by_bu,          bu)
        inc(bu_brand[bu],   brand)
        inc(bu_medium[bu],  medium)
        inc(bu_month[bu],   month)
        inc(bu_state[bu],   state)
        inc(bu_city[bu],    city)
        inc(bu_dealer[bu],  dealer)
        inc(bu_lt[bu],      lt)
        inc(bu_model[bu],   model)
        model_bu_map[model] = bu

    # Sort months by canonical order
    month_key = {m: i for i, m in enumerate(MONTH_ORDER)}
    months_present = sorted(months_seen, key=lambda x: month_key.get(x, 999))

    # Sort dicts by value desc
    def srt(d):
        return dict(sorted(d.items(), key=lambda x: -x[1]))

    brands_all = sorted(brands_set)

    return {
        "total":         sum(by_brand.values()),
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
        "city_state":    {c: s.most_common(1)[0][0] for c, s in city_state_ctr.items()},
        "dealer_brand":  dict(dealer_brand),
        "dealer_state":  dict(dealer_state),
        "dealer_medium": dict(dealer_medium),
        "dealer_month":  {k: {m: v.get(m,0) for m in months_present} for k,v in dealer_month.items()},
        "model_dealer":  dict(model_dealer),
        "medium_month":  {k: {m: v.get(m,0) for m in months_present} for k,v in medium_month.items()},
        "lt_medium":     dict(lt_medium),
        "lt_month":      {k: {m: v.get(m,0) for m in months_present} for k,v in lt_month.items()},
        "model_brand":   dict(model_brand),
        "by_bu":         srt(by_bu),
        "bu_brand":      dict(bu_brand),
        "bu_medium":     dict(bu_medium),
        "bu_month":      {k: {m: v.get(m,0) for m in months_present} for k,v in bu_month.items()},
        "bu_state":      dict(bu_state),
        "bu_city":       dict(bu_city),
        "bu_dealer":     dict(bu_dealer),
        "bu_lt":         dict(bu_lt),
        "bu_model":      dict(bu_model),
        "model_bu":      model_bu_map,
        "dealer_code":   dealer_code_map,
        "dealer_name":   dealer_name_map,
        "state_month":   {k: {m: v.get(m,0) for m in months_present} for k,v in state_month.items()},
        "city_month":    {k: {m: v.get(m,0) for m in months_present} for k,v in city_month.items()},
        "model_month":   {k: {m: v.get(m,0) for m in months_present} for k,v in model_month.items()},
        "last_updated":  datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
        # version and deployed_at are injected by main() after this returns
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    # Load manifest
    manifest = {"processed": {}, "last_run": None, "version": 0}
    if os.path.exists(MANIFEST_F):
        with open(MANIFEST_F) as f:
            manifest = json.load(f)
        manifest.setdefault("version", 0)

    # Load existing leads (gzip-compressed to keep repo size manageable)
    all_rows = []
    if os.path.exists(LEADS_F):
        with gzip.open(LEADS_F, "rt", encoding="utf-8") as f:
            all_rows = json.load(f)
        print(f"Loaded {len(all_rows):,} existing leads from {LEADS_F}")
    else:
        print("all_leads.json.gz not in cache — forcing full reprocess of all Drive files.")
        manifest["processed"] = {}   # treat every file as new so they all get re-downloaded

    # Auth
    print("Authenticating with Google Drive + Sheets...")
    drive_svc, sheets_svc = get_services()

    # List files
    files = list_folder_sheets(drive_svc)
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
            zip_buf = export_as_zip(drive_svc, fid)
            new_rows = parse_zip_rows(zip_buf, name)
        except FileTooLargeError:
            print(f"    File too large for ZIP export — using Sheets API v4 fallback…")
            try:
                new_rows = export_via_sheets_api(sheets_svc, fid, name)
            except Exception as e2:
                print(f"    ERROR (Sheets API fallback) for {name}: {e2}")
                continue
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

    if new_file_count == 0 and new_row_count == 0:
        print("\nNo new Drive files — rebuilding aggregations from cached leads.")
        if not all_rows:
            print("No cached leads found either — nothing to do.")
            manifest["last_run"] = datetime.now(timezone.utc).isoformat()
            with open(MANIFEST_F, "w") as f:
                json.dump(manifest, f, indent=2)
            return
        # Fall through to rebuild aggregations + update index.html even with no new data
        # (catches code changes to aggregation logic, new mapping files, etc.)

    # Deduplicate full dataset
    print(f"\nDeduplicating {len(all_rows):,} total rows...")
    all_rows = deduplicate(all_rows)
    print(f"After dedup: {len(all_rows):,} unique leads")

    # Save leads (gzip-compressed — ~9MB vs 71MB raw)
    with gzip.open(LEADS_F, "wt", encoding="utf-8") as f:
        json.dump(all_rows, f)
    gz_mb = os.path.getsize(LEADS_F) / 1024 / 1024
    print(f"Saved all_leads.json.gz ({len(all_rows):,} rows, {gz_mb:.1f} MB)")

    # Load BU mapping and build aggregations
    model_to_bu = load_model_bu_mapping(REPO_ROOT)
    print("Building aggregations...")
    dash = build_aggregations(all_rows, model_to_bu=model_to_bu)
    print(f"Total: {dash['total']:,} | Brands: {list(dash['by_brand'].keys())}")

    # Stamp version + deployment timestamp (IST)
    new_version = manifest.get("version", 0) + 1
    now_ist = datetime.now(IST)
    dash["version"]     = new_version
    dash["deployed_at"] = now_ist.strftime("%d %b %Y, %H:%M IST")
    print(f"Version: v{new_version}  |  Deployed at: {dash['deployed_at']}")

    with open(DASH_F, "w") as f:
        json.dump(dash, f)
    print(f"Saved dashboard_data.json")

    # Re-embed data into index.html so it works without a fetch
    html_f = os.path.join(REPO_ROOT, "index.html")
    if os.path.exists(html_f):
        with open(html_f) as f:
            html_src = f.read()
        start_marker = "\nconst D={"
        end_marker   = ";\nconst BCOL"
        if start_marker in html_src and end_marker in html_src:
            s = html_src.index(start_marker) + 1
            e = html_src.index(end_marker)
            new_blob = "const D=" + json.dumps(dash, separators=(',',':')) + ";"
            html_src = html_src[:s] + new_blob + html_src[e:]
            with open(html_f, "w") as f:
                f.write(html_src)
            print(f"Updated index.html with fresh embedded data")
        else:
            print(f"Warning: could not find D block in index.html to update")

    # Update manifest (persist new version number)
    manifest["last_run"] = datetime.now(timezone.utc).isoformat()
    manifest["version"]  = new_version
    with open(MANIFEST_F, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Updated manifest.json")

    print(f"\n✅ Done — {new_file_count} files processed, {new_row_count:,} new rows added.")


if __name__ == "__main__":
    main()
