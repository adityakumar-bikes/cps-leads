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

import base64, gzip, io, json, os, zipfile, csv, sys, re, socket, time, zlib
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

# Values a brand cell might contain that are clearly NOT a real brand name
# (placeholder text, blank-ish cells, stray numbers from a misaligned column).
# Anything else is accepted as-is — this is intentionally NOT a fixed brand
# whitelist, so a brand-new OEM's data is picked up automatically the moment
# it appears in Drive, without needing a code change here.
_INVALID_BRAND_VALUES = {"", "n/a", "na", "-", "none", "null", "test", "brand", "0"}

def _looks_like_brand(s: str) -> bool:
    s = (s or "").strip()
    if not s or s.lower() in _INVALID_BRAND_VALUES:
        return False
    if len(s) > 60:
        return False
    if s.replace('.', '', 1).isdigit():
        return False
    return True

# Normalize short brand names → canonical display names (for consistent aggregation)
BRAND_NORMALIZE = {
    "ampere": "Ampere Electric",
    "ampere electric": "Ampere Electric",
    "ola": "Ola Electric",
    "ola electric": "Ola Electric",
    "bajaj chetak": "Bajaj",
    "husqvarna": "Husqvarna Motorcycles",
    "jawa": "Jawa",
    "yezdi": "Jawa",
    "bsa": "Jawa",
}

# Merge sub-brands into parent brand. Applied as each row is read from Drive, so every stored lead already says
# "Bajaj" -- the original "Triumph" / "KTM" is gone by the time a BU is chosen (see resolve_bu).
BRAND_ALIASES = {
    "ktm":                   "Bajaj",
    "triumph":               "Bajaj",
    "husqvarna motorcycles": "Bajaj",
}

def is_brand_sheet(sheet_label: str) -> bool:
    """Return True unless this sheet is a known utility/summary tab.

    Deliberately name-agnostic beyond the SKIP_SHEETS deny-list: a brand-new
    OEM's tab (whatever it's named — a new brand, BU code, or month label we
    haven't seen before) is accepted here and only actually ingested if its
    header row contains real 'brand' + 'Medium' columns (checked in
    parse_zip_rows / export_via_sheets_api) — that content check is the real
    gate, so this function doesn't need a brand-name whitelist.
    """
    sl = sheet_label.lower().strip()
    if not sl:
        return False
    if sl in SKIP_SHEETS:
        return False
    for skip in SKIP_SHEETS:
        if skip in sl:
            return False
    return True

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

# Chronological month labels, generated rather than hand-listed: this used to stop
# at Dec'2026, after which any newer month would have tied at the "unknown" sort key
# and come out in arbitrary order in months_present.
MONTH_ORDER = [
    f"{m}'{y}"
    for y in range(2025, 2036)
    for m in ("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")
]

# Months to exclude from all aggregations and dashboard output
SKIP_MONTHS = {"Mar'2025"}

# The Ask vs Actual tab needs Actual leads cut by State / Model / BU / Source and by DAY, all at
# once, and none of the other pre-aggregated cross-sections in the dashboard data can answer
# that exactly. So the most recent months are also kept as one compact cube of counts per
# (model, state, medium, lead month, lead date) -- see `ask_cube` in build_aggregations().
ASK_CUBE_MONTHS = 8

# A file that held at least this many rows on its last read but reads as completely
# empty now is treated as a bad/mid-write read (its existing rows are kept).
EMPTY_GUARD_ROWS = 1000
# Warn when a re-read file comes back with fewer than this fraction of its last row count.
SHRINK_WARN_RATIO = 0.7
# The source team's daily job rewrites the rolling files in batches (observed 2026-10-06:
# one TVS file went 9,000 -> 114,000 -> 177,077 rows over ~45 minutes, bumping
# modifiedTime every few minutes). Reading mid-rewrite captures a partial sheet, so a
# rolling file must have been untouched for this long before its read is trusted...
SETTLE_SECS = 300
# ...but a run never spends more than this in total waiting on files that won't settle.
SETTLE_BUDGET = 1800
# A run takes ~20 min while the source job's writes are spread over ~90 min, so a file can
# change after we've read it but before the run ends. After the main pass the folder is
# listed again and anything that moved is re-read, at most this many times over.
MAX_SWEEPS = 2
# Drive's ZIP export refuses sheets beyond ~10 MB of CSV (roughly 20K rows of lead data) —
# but only after grinding on it for 45-135 s, and sometimes by returning HTTP 500s that get
# retried three times on top (measured 2026-10-06: 13 of the 14 minutes spent reading the six
# rolling files went on ZIP attempts that failed; the Sheets API reads themselves took ~75 s
# in total). A file whose last read was at least this many rows goes straight to the
# Sheets API.
ZIP_SKIP_ROWS = 15000


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


# ── Rolling source files ─────────────────────────────────────────────────────
# The source team keeps three generations of each file live and edits them every
# day, rotating the names at each month boundary:
#
#   "<base>"                                -> current month    (T)
#   "<base> - Previous Month"               -> one month back   (T-1)
#   "<base> - Previous to Previous Month"   -> two months back  (T-2)
#
# Anything older gets a dated name ("... - Aug'26") and is static. The rolling
# files are edited in place — by scripts that can be mid-write when we read, and
# sometimes by formulas that don't reliably bump Drive's modifiedTime — so the
# modifiedTime check used to skip untouched files can't be trusted for them.
# They're re-read on EVERY run (scheduled, or the dashboard's Refresh button).
#
# File IDs are NOT stable per role: at each rollover the automation copies the
# oldest month into a brand-new file, overwrites "Previous Month" with the month
# that just ended, and clears the current file for the new month. So rolling files
# are identified by their *current name*, never by ID.
ROLLING_BASES = ("TVS CPS Triggered LD LMS Status", "Bike CPS Triggered LD LMS Status")
ROLLING_SUFFIX_ROLE = {
    "":                              "T",
    " - Previous Month":             "T-1",
    " - Previous to Previous Month": "T-2",
}


def _norm_name(s):
    """Case/whitespace/dash-variant-insensitive form of a Drive file name."""
    s = re.sub(r"[‐-―−]", "-", s or "")
    return re.sub(r"\s+", " ", s).strip().casefold()


_ROLLING_BY_NAME = {
    _norm_name(base + suffix): (base.split()[0], role)
    for base in ROLLING_BASES for suffix, role in ROLLING_SUFFIX_ROLE.items()
}


def rolling_role(name):
    """('TVS'|'Bike', 'T'|'T-1'|'T-2') if `name` is one of the rolling files, else None."""
    return _ROLLING_BY_NAME.get(_norm_name(name))


def expected_rolling_names():
    return [base + suffix for base in ROLLING_BASES for suffix in ROLLING_SUFFIX_ROLE]


def _now_utc():
    return datetime.now(timezone.utc)


def _parse_drive_time(s):
    """Drive's RFC 3339 modifiedTime ('2026-10-06T08:20:41.507Z') -> aware datetime."""
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def current_modified_time(drive_svc, file_id):
    return drive_svc.files().get(fileId=file_id, fields="modifiedTime").execute()["modifiedTime"]


def wait_until_settled(drive_svc, file_id, name, listed_mtime, budget):
    """Block until the file has been untouched for SETTLE_SECS (or the run's shared
    wait budget runs out). Returns (latest modifiedTime, settled).

    The listing at the start of the run can be minutes stale by the time we reach a
    file, so this always re-asks Drive for the live modifiedTime first. A failed
    metadata lookup never blocks the run — we just carry on with what we have.
    """
    mtime, announced = listed_mtime, False
    while True:
        try:
            mtime = current_modified_time(drive_svc, file_id)
        except Exception as e:
            print(f"    (couldn't re-check modifiedTime: {e})")
            return mtime, True
        age = (_now_utc() - _parse_drive_time(mtime)).total_seconds()
        if age >= SETTLE_SECS:
            return mtime, True
        if age < -120:
            # "Modified in the future" can only be clock skew, not an active writer —
            # don't burn the wait budget on it.
            print(f"    (modifiedTime is {int(-age)}s ahead of this machine's clock — ignoring the skew)")
            return mtime, True
        pause = min(SETTLE_SECS - age + 5, 60)
        if budget["left"] < pause:
            return mtime, False
        if not announced:
            print(f"    … modified {int(age)}s ago — the source job is probably still writing it; "
                  f"waiting for it to go quiet")
            announced = True
        time.sleep(pause)
        budget["left"] -= pause


def read_source_file(drive_svc, sheets_svc, file_id, name, expected_rows=0):
    """All brand rows in one Drive file: ZIP export first, Sheets API when it's too big.
    `expected_rows` is how many rows the file had when last read (0 = never read); a file
    that big skips the ZIP attempt, which would only fail slowly (see ZIP_SKIP_ROWS)."""
    if expected_rows >= ZIP_SKIP_ROWS:
        return export_via_sheets_api(sheets_svc, file_id, name)
    try:
        zip_buf = export_as_zip(drive_svc, file_id)
        return parse_zip_rows(zip_buf, name, file_id=file_id)
    except FileTooLargeError:
        print(f"    File too large for ZIP export — using Sheets API v4 fallback…")
        return export_via_sheets_api(sheets_svc, file_id, name)


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
    Each row is tagged with _file_id so stale rows can be purged on re-processing.

    Data rows are fetched in row-range chunks rather than one unbounded
    values().get() call, and the fetched chunks are checked for internal gaps
    (a chunk with no rows followed by a later chunk that has rows again) before
    being accepted. This was added chasing what first looked like an API
    response-size limit — an unbounded fetch of a sheet whose grid declared
    157,674 rows came back with only ~90,000 data rows on 2026-09-29 — but
    turned out to be the sheet's real content genuinely ending at row ~90,000,
    with the rest of the declared grid just unused blank padding (a common
    spreadsheet pattern: pre-formatted empty rows left below the data to grow
    into). gridProperties.rowCount is therefore NOT a reliable "how much real
    data is here" figure — chunking still guards against a genuine truncated
    response (which shows up as a gap: empty then non-empty again), while
    tolerating the normal case of real data simply ending before the grid does.
    """
    rows = []
    cols_lower = {c.lower(): c for c in COLS}
    # There's no known hard row/size limit motivating a small chunk size here — the
    # 2026-09-29 "truncation" that prompted chunking turned out to be real sparse
    # data, not an API limit (confirmed: an unbounded fetch of a 157K-row sheet
    # correctly returned all of it when that many rows were genuinely populated).
    # Chunking is kept as defense-in-depth via the gap check below, but a large
    # chunk size keeps the round-trip count (and runtime) down — measured at ~220s
    # for one ~300K-row multi-sheet file at 20,000 rows/chunk, which is worth
    # avoiding when it isn't buying real safety.
    CHUNK_ROWS = 50000

    # Step 1: list all sheets in the workbook, with each one's declared grid row
    # count (needed for chunking + the completeness check below), with retries.
    meta = None
    for attempt in range(5):
        try:
            meta = sheets_svc.spreadsheets().get(
                spreadsheetId=file_id,
                fields="sheets.properties(title,gridProperties.rowCount)",
            ).execute()
            break
        except Exception as e:
            if attempt < 4:
                wait = 20 * (attempt + 1)
                print(f"    ⚠ Sheets API metadata attempt {attempt+1} failed ({e}), retrying in {wait}s…")
                time.sleep(wait)
            else:
                raise
    sheets_info = [
        (s["properties"]["title"], s["properties"].get("gridProperties", {}).get("rowCount", 0))
        for s in meta.get("sheets", [])
    ]
    print(f"    Sheets API: found {len(sheets_info)} sheets in '{file_name}'")

    def fetch_range(a1_range, label):
        """One ranged values().get() call, with retries for transient errors.
        Raises (rather than silently returning nothing) once retries are
        exhausted — main()'s except-block around this whole function already
        handles that by skipping the manifest write for this file, so it gets
        retried next run instead of being recorded as successfully processed
        with less data than it actually has."""
        for attempt in range(5):
            try:
                result = sheets_svc.spreadsheets().values().get(
                    spreadsheetId=file_id,
                    range=a1_range,
                    valueRenderOption="FORMATTED_VALUE",
                    dateTimeRenderOption="FORMATTED_STRING",
                ).execute()
                return result.get("values", [])
            except Exception as e:
                if attempt < 4:
                    wait = 20 * (attempt + 1)
                    print(f"    ⚠ Sheets API attempt {attempt+1} failed for {label} ({e}), retrying in {wait}s…")
                    time.sleep(wait)
                else:
                    raise RuntimeError(
                        f"Failed to read {label} in '{file_name}' after 5 attempts"
                    ) from e

    for title, grid_rows in sheets_info:
        if not is_brand_sheet(title):
            print(f"    skip sheet: {title}")
            continue
        print(f"    reading sheet: {title} (grid has {grid_rows:,} rows)")

        header_chunk = fetch_range(f"'{title}'!1:1", f"{title!r} header row")
        if not header_chunk:
            print(f"    sheet '{title}': empty (no header), skipping")
            continue
        header = [str(h).strip() for h in header_chunk[0]]

        chunks = []   # (row_cursor, end_row, chunk_values) per fetched range
        row_cursor = 2   # first data row; row 1 is the header just fetched above
        last_row = max(grid_rows, 1)
        while row_cursor <= last_row:
            end_row = min(row_cursor + CHUNK_ROWS - 1, last_row)
            chunk = fetch_range(f"'{title}'!{row_cursor}:{end_row}", f"{title!r} rows {row_cursor}-{end_row}")
            chunks.append((row_cursor, end_row, chunk))
            row_cursor = end_row + 1

        # gridProperties.rowCount is the sheet's allocated grid size, which is
        # usually much larger than its real data (spreadsheets are commonly
        # pre-formatted with a big block of blank rows below the data, ready to
        # grow into) — so a chunk coming back short, or even fully empty, is
        # normal once we've reached the end of real data, not a sign of trouble
        # on its own. The actual truncation signature is a GAP: a chunk with no
        # rows followed by a later chunk that has rows again. Real lead exports
        # don't have holes in the middle, so that combination means some chunk's
        # response was cut short, not that the sheet legitimately ended there.
        first_empty = next((i for i, (_, _, c) in enumerate(chunks) if not c), None)
        if first_empty is not None:
            for rc, er, c in chunks[first_empty + 1:]:
                if c:
                    er0, ec0, _ = chunks[first_empty]
                    raise RuntimeError(
                        f"Sheet '{title}' in '{file_name}': rows {er0}-{ec0} came back empty but "
                        f"rows {rc}-{er} afterward still have data — looks like a gap from a "
                        f"truncated fetch, not the sheet legitimately ending, refusing to ship "
                        f"a partial sheet."
                    )

        values = [row for _, _, c in chunks for row in c]
        if not values:
            print(f"    sheet '{title}': empty or header-only, skipping")
            continue
        print(f"    sheet '{title}': fetched {len(values):,} data rows (grid declares {grid_rows:,})")

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
        for data_row in values:
            # Pad row to at least brand column width
            if len(data_row) <= brand_idx:
                continue
            brand = str(data_row[brand_idx]).strip()
            if not _looks_like_brand(brand):
                continue
            row = {}
            for col, idx in hdr_map.items():
                row[col] = str(data_row[idx]).strip() if idx < len(data_row) else ""
            # Normalize brand name in stored row (BRAND_NORMALIZE then BRAND_ALIASES)
            b = BRAND_NORMALIZE.get(row["brand"].lower(), row["brand"])
            row["brand"] = BRAND_ALIASES.get(b.lower(), b)
            row["_file_id"] = file_id
            rows.append(row)
            sheet_rows += 1

        if sheet_rows:
            print(f"    sheet '{title}': {sheet_rows:,} rows")

    return rows


# ── Parsing ──────────────────────────────────────────────────────────────────

def parse_zip_rows(zip_buf, file_name, file_id=None):
    """
    Open a ZIP of CSVs exported from Google Sheets.
    Skips all summary/pivot sheets (Filter, City Wise, LT Wise, etc.) via
    is_brand_sheet(); every other sheet is ingested if its header row has
    real 'brand' + 'Medium' columns — no fixed brand whitelist, so a new
    OEM's tab is picked up automatically.
    Each row is tagged with _file_id so stale rows can be purged on re-processing.
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
                if not _looks_like_brand(brand):
                    continue
                row = {}
                for col, fn in hdr_map.items():
                    row[col] = str(raw_row.get(fn, "") or "").strip()
                # Normalize brand name (BRAND_NORMALIZE then BRAND_ALIASES)
                b = BRAND_NORMALIZE.get(row["brand"].lower(), row["brand"])
                row["brand"] = BRAND_ALIASES.get(b.lower(), b)
                if file_id:
                    row["_file_id"] = file_id
                rows.append(row)
                sheet_rows += 1

            if sheet_rows:
                print(f"    sheet '{sheet_label}': {sheet_rows:,} rows")

    return rows


# ── Deduplication ─────────────────────────────────────────────────────────────

_OPTY_SCI_NOTATION_RE = re.compile(r'^\d\.\d+E\+\d+$', re.IGNORECASE)


def deduplicate(rows):
    """
    Two-pass dedup:

    Pre-pass — some rows' opty_id arrives mangled into scientific notation
      (e.g. "2.60822E+17" instead of the real 18-digit CRM id) because of a
      cell-format inconsistency in the source sheet, not because the lead is
      a duplicate. Left as-is, that rounded value would collide with every
      other lead whose real opty_id happens to share the same leading digits,
      and Pass 1 below would wrongly collapse many distinct leads into one
      (seen concretely: 148 raw TVS rows sharing just 3 corrupted opty_id
      strings, silently dropping 145 real leads). Treat these as if opty_id
      were blank so they fall through to Pass 2's mobile-based dedup instead,
      which doesn't depend on numeric precision.

    Pass 1 — rows WITH opty_id: deduplicated by opty_id alone.
      Trusts the CRM's globally-unique lead identifier. Two rows with different
      opty_ids are always treated as distinct leads (e.g. the same customer
      submitting two enquiries in the same month is legitimate and both are kept).
      Also records the (brand, mobile, month) combo for every opty-tracked row.

    Pass 2 — rows WITHOUT opty_id: deduplicated by (brand, mobile, month) combo.
      Any combo already covered by an opty row (from Pass 1) is dropped — this
      handles the stale-cache edge case where the same lead existed in the old
      cache with an empty opty_id and was re-downloaded with a filled opty_id.
      Within this pass, duplicate combos are also collapsed.
    """
    for r in rows:
        if _OPTY_SCI_NOTATION_RE.match(r.get("opty_id") or ""):
            r["opty_id"] = ""

    # ── Pass 1: opty rows ────────────────────────────────────────────────────
    seen_opty   = set()
    opty_combos = set()   # combos already covered by an opty row
    opty_out    = []
    for r in rows:
        opty = r.get("opty_id") or ""
        if not opty:
            continue
        if opty in seen_opty:
            continue
        seen_opty.add(opty)
        mob = r.get("encrypt_mobile_number") or ""
        if mob:
            opty_combos.add((r.get("brand") or "", mob, r.get("Lead_Month") or ""))
        opty_out.append(r)

    # ── Pass 2: no-opty rows ─────────────────────────────────────────────────
    seen_nopty = set()
    nopty_out  = []
    for r in rows:
        if r.get("opty_id"):
            continue
        mob   = r.get("encrypt_mobile_number") or ""
        brand = r.get("brand")     or ""
        month = r.get("Lead_Month") or ""
        if mob:
            combo = (brand, mob, month)
            if combo in opty_combos:
                continue   # covered by an opty row — drop the no-opty duplicate
            if combo in seen_nopty:
                continue
            seen_nopty.add(combo)
        nopty_out.append(r)

    return opty_out + nopty_out


# ── Unique-people counting for selected brands ───────────────────────────────────────────
# Everywhere else a "lead" is a CRM opportunity (opty_id): the same customer enquiring twice in
# a month is two leads (see deduplicate() above). For the brands below the business counts
# PEOPLE instead — at most ONE lead per encrypted mobile number per month — so a customer who
# enquires repeatedly, or about several of the group's models, counts once.
# "Jawa" is a single brand in the data that covers Jawa, Yezdi and BSA (see BRAND_NORMALIZE), so
# the rule spans all three: someone who asked about a Jawa and a Yezdi in the same month is one lead.
UNIQUE_PHONE_BRANDS = frozenset({"Jawa"})


def _date_sort_key(raw):
    """'YYYY-MM-DD' for a parseable lead Date ('2026-10-03' or '2026-Oct-03'); '9999' (sorts last) if not."""
    raw = (raw or "").strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-" and raw[:4].isdigit() and raw[5:7].isdigit() and raw[8:10].isdigit():
        return raw[:10]
    try:
        return datetime.strptime(raw, "%Y-%b-%d").strftime("%Y-%m-%d")
    except ValueError:
        return "9999"


def unique_phone_per_month(rows, brands=None, stats=None):
    """
    Rows of `brands` (default UNIQUE_PHONE_BRANDS) reduced to one lead per (brand, encrypt_mobile_number,
    Lead_Month) — the month the dashboard counts the lead in. The survivor is the person's FIRST enquiry
    of the month (earliest Date, then lowest opty_id), so which row represents them — and therefore
    its source / model / state / dealer — never depends on the order files were loaded in. Rows of other
    brands, and rows with no mobile number (nothing to match on), pass through untouched.

    Returns a NEW list; `rows` is not modified, so the cache can keep every distinct lead and this
    rule can be changed later without re-reading old files. If `stats` is a dict it receives
    {"by_month": Counter of the rows left out, per Lead_Month}.
    """
    brands = UNIQUE_PHONE_BRANDS if brands is None else frozenset(brands)
    best = {}   # (brand, mobile, month) -> (date key, opty_id, row index) of the earliest enquiry
    for i, r in enumerate(rows):
        if r.get("brand") not in brands:
            continue
        mob = (r.get("encrypt_mobile_number") or "").strip()
        if not mob:
            continue
        key = (r["brand"], mob, (r.get("Lead_Month") or "").strip())
        cand = (_date_sort_key(r.get("Date")), r.get("opty_id") or "", i)
        cur = best.get(key)
        if cur is None or cand < cur:
            best[key] = cand
    out, left_out = [], Counter()
    for i, r in enumerate(rows):
        if r.get("brand") in brands:
            mob = (r.get("encrypt_mobile_number") or "").strip()
            if mob:
                key = (r["brand"], mob, (r.get("Lead_Month") or "").strip())
                if best[key][2] != i:
                    left_out[key[2]] += 1
                    continue
        out.append(r)
    if stats is not None:
        stats["by_month"] = left_out
    return out


# ── BU Mapping ────────────────────────────────────────────────────────────────

# Canonical BU names — any key (case-insensitive) maps to the value
BU_ALIASES = {
    'pb-trm':  'TRM',
    'triumph': 'TRM',
    'ktm':     'PB',
    'bajaj':   'MC',
}

def normalize_bu(bu: str) -> str:
    """Apply BU_ALIASES to consolidate variant BU names into canonical ones."""
    return BU_ALIASES.get(bu.strip().lower(), bu)

# BU of the sub-brands BRAND_ALIASES folds into Bajaj. A lead no longer says "Triumph" or "KTM" by the time its BU is
# chosen (it says "Bajaj", which would file it under MC), but its model name still starts with the sub-brand.
SUBBRAND_BU = {'triumph': 'TRM', 'ktm': 'PB'}

def resolve_bu(model: str, brand: str, model_to_bu) -> str:
    """BU a lead is filed under. The Model Mapping tab decides when it lists the model. For a model it doesn't list:
    the BU of the sub-brand its name starts with (Triumph -> TRM, KTM -> PB), else the lead's brand (TVS -> TVS,
    Bajaj -> MC)."""
    bu = (model_to_bu or {}).get(model.lower())
    if not bu:
        bu = SUBBRAND_BU.get(model.strip().split(' ', 1)[0].lower()) or brand
    return normalize_bu(bu)

# City name normalization — merges alternate spellings into one canonical form
CITY_NORMALIZE = {
    "bengaluru": "Bangalore",
    "bangalore": "Bangalore",
    "gurugram":  "Gurgaon",
    "gurgaon":   "Gurgaon",
}

# State name normalization
STATE_NORMALIZE = {
    "orissa":  "Odisha",
    "orrisa":  "Odisha",
    "odisha":  "Odisha",
}

def normalize_city(city: str) -> str:
    """Normalize city name to canonical form (e.g. Bengaluru → Bangalore)."""
    if not city:
        return city
    return CITY_NORMALIZE.get(city.strip().lower(), city.strip())

def normalize_state(state: str) -> str:
    """Normalize state name to canonical form (e.g. Orissa → Odisha)."""
    if not state:
        return state
    return STATE_NORMALIZE.get(state.strip().lower(), state.strip())

MODEL_MAP_SHEET_TAB = "Model Mapping"

def load_model_bu_mapping(sheets_svc):
    """Load Model Name → BU mapping from the live 'Model Mapping' tab of the shared Google Sheet.
    Returns dict: {model_name_lowercase: BU_string}
    """
    try:
        resp = sheets_svc.spreadsheets().values().get(
            spreadsheetId=ASK_SHEET_ID,
            range=f"'{MODEL_MAP_SHEET_TAB}'",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        rows = resp.get("values", [])
        if not rows:
            print(f"Note: '{MODEL_MAP_SHEET_TAB}' tab is empty — BU will fall back to brand name (intentional)")
            return {}
        max_len = max(len(r) for r in rows)
        rows = [r + [None] * (max_len - len(r)) for r in rows]
        header = [str(c).strip().lower() if c else "" for c in rows[0]]
        model_idx = next((i for i, h in enumerate(header) if "model" in h), None)
        bu_idx    = next((i for i, h in enumerate(header) if h == "bu"), None)
        if model_idx is None or bu_idx is None:
            print(f"Warning: '{MODEL_MAP_SHEET_TAB}' tab missing 'Model Name' or 'BU' column (found: {header})")
            return {}
        mapping = {}
        for row in rows[1:]:
            model = str(row[model_idx]).strip() if row[model_idx] else ""
            bu    = str(row[bu_idx]).strip()    if row[bu_idx]    else ""
            if model and bu:
                mapping[model.lower()] = bu
        print(f"Loaded BU mapping from Sheet '{MODEL_MAP_SHEET_TAB}': {len(mapping)} model → BU entries")
        return mapping
    except Exception as e:
        print(f"Warning: Could not load Model Mapping from Sheet: {e}")
        import traceback; traceback.print_exc()
        return {}


OEM_DIST_FILE  = "All Brand - All BU -OEM sales distribution.xlsx"

def load_oem_data(repo_root):
    """Load OEM city-level sales distribution from Classification sheet.
    Returns nested dict: {Brand: {BU: {city: {p: priority_label, s: oem_share}}}}
    Brand aliases are applied so names match the dashboard (e.g. 'Bajaj' stays 'Bajaj').
    """
    xlsx_path = os.path.join(repo_root, OEM_DIST_FILE)
    if not os.path.exists(xlsx_path):
        return {}
    try:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
        if "Classification" not in wb.sheetnames:
            print(f"Warning: '{OEM_DIST_FILE}' has no 'Classification' sheet — skipping OEM data.")
            wb.close()
            return {}
        ws = wb["Classification"]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not rows:
            return {}
        header = [str(c).strip() if c else "" for c in rows[0]]
        # Expected columns: Brand, BU, City, Priority, OEM Sale Share
        col = {h: i for i, h in enumerate(header)}
        if not all(k in col for k in ("Brand", "BU", "City", "Priority", "OEM Sale Share")):
            print(f"Warning: Classification sheet missing expected columns (found: {header})")
            return {}
        result = {}
        for row in rows[1:]:
            if len(row) <= max(col.values()):
                continue
            raw_brand = str(row[col["Brand"]]).strip() if row[col["Brand"]] else ""
            bu        = str(row[col["BU"]]).strip()    if row[col["BU"]]    else ""
            city      = normalize_city(str(row[col["City"]]).strip() if row[col["City"]] else "")
            pri       = str(row[col["Priority"]]).strip() if row[col["Priority"]] else ""
            try:
                share = float(row[col["OEM Sale Share"]])
            except (TypeError, ValueError):
                continue
            if not raw_brand or not bu or not city:
                continue
            # Apply same brand + BU aliases used in lead aggregation
            brand = BRAND_NORMALIZE.get(raw_brand.lower(), raw_brand)
            brand = BRAND_ALIASES.get(brand.lower(), brand)
            bu    = normalize_bu(bu)
            if brand not in result:
                result[brand] = {}
            if bu not in result[brand]:
                result[brand][bu] = {}
            result[brand][bu][city] = {"p": pri, "s": round(share, 6)}
        total = sum(len(cities) for bus in result.values() for cities in bus.values())
        summary = {b: list(bus.keys()) for b, bus in result.items()}
        print(f"Loaded OEM distribution: {total} city records | {summary}")
        return result
    except Exception as e:
        print(f"Warning: Could not load OEM distribution data: {e}")
        return {}


def load_ims_data(repo_root):
    """Load IMS state mapping from 'Classification IMS' sheet.
    Returns: { brand: { state: "Y" | "N" } }
    """
    xlsx_path = os.path.join(repo_root, OEM_DIST_FILE)
    if not os.path.exists(xlsx_path):
        return {}
    try:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
        if "Classification IMS" not in wb.sheetnames:
            wb.close()
            return {}
        ws = wb["Classification IMS"]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not rows:
            return {}
        header = [str(c).strip() if c else "" for c in rows[0]]
        col = {h: i for i, h in enumerate(header)}
        if not all(k in col for k in ("Brand", "State", "IMS State")):
            print(f"Warning: Classification IMS sheet missing expected columns (found: {header})")
            return {}
        result = {}
        for row in rows[1:]:
            if len(row) <= max(col.values()):
                continue
            raw_brand = str(row[col["Brand"]]).strip() if row[col["Brand"]] else ""
            state     = str(row[col["State"]]).strip()      if row[col["State"]]      else ""
            ims       = str(row[col["IMS State"]]).strip().upper() if row[col["IMS State"]] else ""
            if not raw_brand or not state or ims not in ("Y", "N"):
                continue
            brand = BRAND_NORMALIZE.get(raw_brand.lower(), raw_brand)
            brand = BRAND_ALIASES.get(brand.lower(), brand)
            if brand not in result:
                result[brand] = {}
            result[brand][state] = ims
        brands_mapped = {b: sum(1 for v in sv.values() if v == "Y") for b, sv in result.items()}
        print(f"Loaded IMS mapping: {brands_mapped}")
        return result
    except Exception as e:
        print(f"Warning: Could not load IMS data: {e}")
        return {}


ASK_SHEET_ID  = "17PSrhhMEQ-kvvi434jVsuXKgmVrGBUBDJsmIPgAazo0"
ASK_SHEET_TAB = "Consol Ask"

_MONTH_ABBR = {'jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'}

def _find_ask_header_row(rows):
    """Scan rows for the one containing 'OEM' and 'BU' header cells (case-insensitive).
    Returns (row_index, oem_col, bu_col, model_col, month_starts) or None if not found.
    month_starts = {"Apr'2026": start_col_index, ...} — each month's block start; block
    WIDTH varies (some months have a 'Paid' sub-column inserted, some don't), so callers
    must resolve each month's actual t/o/g/f sub-column offsets from the sub-header row
    (see _resolve_ask_month_subcols) rather than assuming a fixed width.
    """
    month_re = re.compile(r"^([A-Za-z]{3})'?\s*(\d{2,4})$")
    for ridx, row in enumerate(rows):
        cells = [str(c).strip() if c is not None else '' for c in row]
        oem_col = next((i for i, c in enumerate(cells) if c.lower() == 'oem'), None)
        bu_col  = next((i for i, c in enumerate(cells) if c.lower() == 'bu'), None)
        if oem_col is None or bu_col is None:
            continue
        model_col = next((i for i, c in enumerate(cells) if 'model' in c.lower()), None)
        if model_col is None:
            continue
        month_starts = {}
        for i, c in enumerate(cells):
            m = month_re.match(c)
            if not m:
                continue
            abbr, yr = m.group(1).title(), m.group(2)
            if abbr.lower() not in _MONTH_ABBR:
                continue
            year_full = ('20' + yr) if len(yr) == 2 else yr
            month_starts[f"{abbr}'{year_full}"] = i
        if month_starts:
            return ridx, oem_col, bu_col, model_col, month_starts
    return None


def _resolve_ask_month_subcols(sub_header_row, month_starts):
    """For each month's block, find its actual t/o/g/f/p column offsets by matching the
    sub-header row's cell text (Total Leads/Organic/Google/FB/Paid), scanning only within
    that month's own column range. Handles blocks of different widths (e.g. a 'Paid' column
    inserted between Organic and Google in some months but not others) instead of assuming
    every block is a fixed 4 columns wide.

    'Paid' is tracked (not discarded) because some brands enter a single lump paid figure
    there instead of splitting Google/FB separately (e.g. before channel-level attribution
    is set up) — a row can have Organic + Paid filled in with Google/FB left completely
    blank. The dashboard falls back to 'Paid' only when Google+FB are both empty, so a
    brand using the split convention is unaffected.
    """
    starts_sorted = sorted(month_starts.items(), key=lambda kv: kv[1])
    month_cols = {}
    for idx, (month, start) in enumerate(starts_sorted):
        end = starts_sorted[idx + 1][1] if idx + 1 < len(starts_sorted) else len(sub_header_row)
        tc = oc = gc = fc = pc = None
        for i in range(start, end):
            label = str(sub_header_row[i]).strip().lower() if i < len(sub_header_row) and sub_header_row[i] else ''
            if 'total' in label and tc is None:
                tc = i
            elif label == 'organic' and oc is None:
                oc = i
            elif label == 'google' and gc is None:
                gc = i
            elif label in ('fb', 'facebook') and fc is None:
                fc = i
            elif label == 'paid' and pc is None:
                pc = i
        # Fall back to the old fixed-offset assumption for any sub-column not found by name,
        # so a month whose sub-header text doesn't match still degrades gracefully.
        if tc is None: tc = start
        if oc is None: oc = start + 1
        if gc is None: gc = start + 2
        if fc is None: fc = start + 3
        month_cols[month] = (tc, oc, gc, fc, pc)
    return month_cols


def load_ask_data(sheets_svc):
    """Load monthly ask targets from the live 'Consol Ask' tab of the shared Google Sheet.
    Returns: { "Apr'2026": { brand: { model: { t, o, g, f } } }, ... }
    Header row (OEM/BU/Model + month columns) and data start are detected dynamically
    since the sheet layout/column count changes as months are added by hand.
    """
    try:
        resp = sheets_svc.spreadsheets().values().get(
            spreadsheetId=ASK_SHEET_ID,
            range=f"'{ASK_SHEET_TAB}'",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        rows = resp.get("values", [])
        # Pad ragged rows so index access (row[N]) never raises
        max_len = max((len(r) for r in rows), default=0)
        rows = [r + [None] * (max_len - len(r)) for r in rows]
        if len(rows) < 3:
            print(f"Note: '{ASK_SHEET_TAB}' tab has <3 rows — skipping ask_data")
            return {}

        hdr = _find_ask_header_row(rows)
        if hdr is None:
            print(f"Warning: '{ASK_SHEET_TAB}' tab — could not find OEM/BU/Model/month header row")
            return {}
        hdr_row_idx, oem_col, bu_col, model_col, month_starts = hdr
        # Data starts 2 rows below the header row (row+1 is the Total/Organic/Google/FB sub-header)
        sub_header_row = rows[hdr_row_idx + 1]
        data_rows = rows[hdr_row_idx + 2:]
        month_cols = _resolve_ask_month_subcols(sub_header_row, month_starts)
        model_map = {
            'Apache RR310': 'TVS Apache RR 310', 'Apache RTR 310': 'TVS Apache RTR 310',
            'Apache RTR 160': 'TVS Apache RTR 160', 'Apache RTR 180': 'TVS Apache RTR 180',
            'Apache RTR 200 4V': 'TVS Apache RTR 200 4V', 'Ronin': 'TVS Ronin',
            'Jupiter 110': 'TVS Jupiter', 'Jupiter 125': 'TVS Jupiter 125',
            'Ntorq 125': 'TVS NTORQ 125', 'Ntorq 150': 'TVS NTorq 150',
            'Scooty Zest 110': 'TVS Scooty Zest',
            'Radeon': 'TVS Radeon', 'Raider 125': 'TVS Raider',
            'Sport': 'TVS Sport', 'XL100': 'TVS XL100', 'iQube': 'TVS iQube',
            'Magnus Neo': 'Ampere Magnus Neo', 'New Magnus Neo': 'New Ampere Magnus Neo',
            'Nexus': 'Ampere Nexus', 'Reo': 'Ampere Reo',
            'Magnus G Max': 'Ampere Magnus G Max', 'Magnus Grand': 'Ampere Magnus Grand',
            'OLA S1 Pro': 'Ola S1 Pro', 'OLA S1 Pro Plus': 'Ola S1 Pro Plus',
            'OLA S1 X': 'Ola S1 X', 'OLA S1 X Plus': 'Ola S1 X Plus',
            'Bgauss C12i': 'BGauss C12i', 'Bgauss RUV 350': 'BGauss RUV 350',
            'Bajaj Gogo P5009': 'Bajaj GoGo P5009', 'Bajaj Gogo P7012': 'Bajaj GoGo P7012',
            'Bajaj Gogo P5012': 'Bajaj GoGo P5012',
            'Bajaj Maxima Xl Cargo E-TEC 9.0': 'Bajaj Maxima XL Cargo E-TEC 9.0',
            'Bajaj Maxima Xl Cargo E-TEC 12.0': 'Bajaj Maxima XL Cargo E-TEC 12.0',
            'Bajaj Riki P40': 'Bajaj Riki P40 05 E Rickshaw',
            'Bajaj Riki C40': 'Bajaj Riki C40 05 E Cart',
            'Bajaj Wego P9018': 'Bajaj WEGO P9018', 'Bajaj Wego P70': 'Bajaj WEGO P70',
            'Bajaj Wego C90': 'Bajaj WEGO C90', 'Bajaj Wego P50': 'Bajaj WEGO P50',
            'HUSQVARNA SVARTPILEN 401': 'Husqvarna Svartpilen 401',
            'HUSQVARNA VITPILEN 250': 'Husqvarna Vitpilen 250',
            'Ather 450 S': 'Ather 450S', 'Ather 450 X': 'Ather 450X',
            'Aprilia Storm': 'Aprilia SR Storm', 'Aprilia SR 175': 'Aprilia SR 175 hp.e',
            'Bajaj Chetak 2501': 'Bajaj Chetak C2501',
            'Jawa 42FJ': 'Jawa 42 FJ',
            'Hero Super Splendor': 'Hero Super Splendor XTEC',
            # The Ask sheet says 'Hero Karizma'; the lead data (Hero's only Karizma) says 'Hero Karizma XMR'.
            # Unmapped, the Ask row showed Actual 0 / 0% while the real leads sat on a 'no ask' row.
            'Hero Karizma': 'Hero Karizma XMR',
            'BSA Scrambler': 'BSA Scrambler 650',
            # Same product, spelt differently in the Ask sheet and in the lead data. Unmapped, the Ask row shows
            # Actual 0 while the real leads sit on a '· no ask' row (found by comparing every Ask row with the
            # lead-data model names, 2026-10-19).
            'Apache RTR 160 4V': 'TVS Apache RTR 160 4V',          # the Ask sheet drops the 'TVS ' prefix, like the other Apache rows
            'BSA GoldStar': 'BSA Gold Star',
            'Bajaj Avenger Street 220': 'Bajaj Avenger 220 Street',
        }
        # OEM column values → dashboard brand names
        brand_map = {'Ampere': 'Ampere Electric', 'OLA': 'Ola Electric', 'Jawa/Yezdi': 'Jawa'}

        result = {}
        last_oem = None
        for row in data_rows:
            if not any(c for c in row if c is not None):
                continue
            if row[oem_col]:
                last_oem = str(row[oem_col]).strip()
            oem = last_oem
            if not oem:
                continue
            bu        = str(row[bu_col]).strip() if row[bu_col] else ''
            model_raw = str(row[model_col]).strip() if row[model_col] else ''
            if not model_raw or model_raw == 'None':
                continue
            # Piaggio brand comes from BU (Aprilia / Vespa)
            brand = bu if oem == 'Piaggio' else brand_map.get(oem, oem)
            model = model_map.get(model_raw, model_raw)

            for month, (tc, oc, gc, fc, pc) in month_cols.items():
                t = int(row[tc] or 0) if len(row) > tc and row[tc] else 0
                o = int(row[oc] or 0) if len(row) > oc and row[oc] else 0
                g = int(row[gc] or 0) if len(row) > gc and row[gc] else 0
                f = int(row[fc] or 0) if len(row) > fc and row[fc] else 0
                p = int(row[pc] or 0) if pc is not None and len(row) > pc and row[pc] else 0
                if t == 0 and o == 0 and g == 0 and f == 0 and p == 0:
                    continue
                cell = {'t': t, 'o': o, 'g': g, 'f': f, 'p': p}
                prev = result.setdefault(month, {}).setdefault(brand, {}).get(model)
                if prev:
                    # two sheet rows end up on one lead-data model (e.g. a renamed row left beside the new one): add
                    # them up rather than letting the later row silently replace the earlier one
                    cell = {k: prev[k] + cell[k] for k in cell}
                result[month][brand][model] = cell

        months_loaded = {m: sum(len(b) for b in brands.values()) for m, brands in result.items()}
        print(f"Loaded ask_data from Sheet '{ASK_SHEET_TAB}': {months_loaded}")
        return result
    except Exception as e:
        print(f"Warning: Could not load ask data from Sheet: {e}")
        import traceback; traceback.print_exc()
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


# ── Lead-level fact table ──────────────────────────────────────────────────────────────
# Every number on the dashboard is a count of leads cut by some mix of Brand / BU / Model / Source /
# Month / State / City / Dealer / Lead type / Date, and the top bar lets the user filter by almost all
# of those at once. Pre-aggregating each mix separately (what this file used to do alone) can never
# cover every combination, so tabs ended up scaling one cross-section by another and drifted from the
# sheets. So the leads themselves are shipped too, compactly, and the page counts whatever the current
# filters ask for -- every tab then reconciles with every other by construction.
#
# One row per counted lead (the same rows that feed build_aggregations), with these columns:
#     state, city, dealer, product (= brand + BU + model), month (Lead_Month), medium (Source),
#     lt (lead type), date (index of the lead's Date; 0 = none)
# Rows are sorted by the columns in that order, so each column is long runs of equal values; the
# runs are coded as (delta of value, length) varints, the date column as varint deltas, and the lot
# is deflated (zlib) and base64'd. The browser (index.html, "fact engine") inflates it with
# DecompressionStream and keeps the columns as typed arrays.
FACT_FORMAT  = 1
FACT_FIELDS  = ("state", "city", "dealer", "product", "month", "medium", "lt")   # run-length coded, in sort order
FACT_BITS    = {"state": 8, "city": 15, "dealer": 20, "product": 13, "month": 7, "medium": 5, "lt": 8}
FACT_DATE_BITS  = 12
FACT_DATE_EPOCH = datetime(2025, 1, 1)   # date index 1 = this day; 0 = lead has no usable date


def _put_varint(buf, n):
    while n >= 0x80:
        buf.append((n & 0x7F) | 0x80)
        n >>= 7
    buf.append(n)


def _zigzag(n):
    return (n << 1) if n >= 0 else ((-n) << 1) - 1


class FactTable:
    """Collects one row per counted lead, then encode() turns them into the compact payload."""

    def __init__(self):
        self.ids = {f: {} for f in FACT_FIELDS}       # field -> {value: provisional id}
        self.keys = []                                  # one packed int per lead
        self._shift = {}
        s = FACT_DATE_BITS
        for f in reversed(FACT_FIELDS):
            self._shift[f] = s
            s += FACT_BITS[f]
        self.bad_dates = 0

    def add(self, state, city, dealer, product, month, medium, lt, lead_dt):
        key = 0
        for f, v in (("state", state), ("city", city), ("dealer", dealer), ("product", product),
                     ("month", month), ("medium", medium), ("lt", lt)):
            d = self.ids[f]
            i = d.get(v)
            if i is None:
                i = d[v] = len(d)
                if i >> FACT_BITS[f]:
                    raise ValueError(f"fact table: more than {1 << FACT_BITS[f]} distinct {f} values "
                                     f"-- widen FACT_BITS['{f}']")
            key |= i << self._shift[f]
        di = 0
        if lead_dt is not None:
            di = (lead_dt - FACT_DATE_EPOCH).days + 1
            if di < 1 or di >> FACT_DATE_BITS:
                self.bad_dates += 1
                di = 0
        self.keys.append(key | di)

    def encode(self):
        """-> dict for the dashboard blob: dictionaries (in the order the payload refers to them) + payload."""
        keys = self.keys
        keys.sort()
        n = len(keys)
        masks = {f: (1 << FACT_BITS[f]) - 1 for f in FACT_FIELDS}
        shifts = [self._shift[f] for f in FACT_FIELDS]
        mask_l = [masks[f] for f in FACT_FIELDS]
        nf = len(FACT_FIELDS)
        remap = [dict() for _ in range(nf)]            # provisional id -> output id (order of first appearance)
        order = [[] for _ in range(nf)]                # output id -> provisional id
        run_val = [None] * nf                          # output id of the current run, per field
        run_len = [0] * nf
        vals = [bytearray() for _ in range(nf)]        # zigzag(delta of run value) varints
        lens = [bytearray() for _ in range(nf)]        # run length varints
        nruns = [0] * nf
        prev_val = [0] * nf
        dates = bytearray()
        prev_date = 0
        date_mask = (1 << FACT_DATE_BITS) - 1

        def close_run(f):
            _put_varint(vals[f], _zigzag(run_val[f] - prev_val[f]))
            prev_val[f] = run_val[f]
            _put_varint(lens[f], run_len[f])
            nruns[f] += 1

        prev_prefix = -1
        cell_n = 0
        for k in keys:
            prefix = k >> FACT_DATE_BITS
            if prefix != prev_prefix:
                if cell_n:
                    for f in range(nf):
                        run_len[f] += cell_n
                cell_n = 0
                for f in range(nf):
                    pid = (k >> shifts[f]) & mask_l[f]
                    oid = remap[f].get(pid)
                    if oid is None:
                        oid = remap[f][pid] = len(order[f])
                        order[f].append(pid)
                    if oid != run_val[f]:
                        if run_len[f]:
                            close_run(f)
                        run_val[f] = oid
                        run_len[f] = 0
                prev_prefix = prefix
            cell_n += 1
            d = k & date_mask
            dz = d - prev_date
            _put_varint(dates, (dz << 1) if dz >= 0 else ((-dz) << 1) - 1)
            prev_date = d
        if cell_n:
            for f in range(nf):
                run_len[f] += cell_n
        for f in range(nf):
            if run_len[f]:
                close_run(f)

        payload = bytearray(b"F" + bytes([FACT_FORMAT]))
        _put_varint(payload, n)
        for f in range(nf):
            _put_varint(payload, nruns[f])
            payload += vals[f]
            payload += lens[f]
        payload += dates
        packed = zlib.compress(bytes(payload), 9)         # zlib format: what DecompressionStream('deflate') reads

        def names(fi):
            inv = {i: v for v, i in self.ids[FACT_FIELDS[fi]].items()}
            return [inv[p] for p in order[fi]]

        return {
            "v": FACT_FORMAT, "n": n, "epoch": FACT_DATE_EPOCH.strftime("%Y-%m-%d"),
            "states": names(0), "cities": names(1), "dealers": names(2),
            "products": [list(p) for p in names(3)],
            "months": names(4), "mediums": names(5), "lts": names(6),
            "bytes_raw": len(payload), "bytes_packed": len(packed),
            "data": base64.b64encode(packed).decode("ascii"),
        }


def _fact_read(fact):
    """-> (n, runs, raw, date_pos): runs[f] = (absolute output ids, run lengths) for FACT_FIELDS[f]; the date
    varints start at raw[date_pos:]."""
    raw = zlib.decompress(base64.b64decode(fact["data"]))
    assert raw[0:1] == b"F" and raw[1] == FACT_FORMAT, "unknown fact payload"
    pos = [2]

    def varint():
        r = s = 0
        while True:
            b = raw[pos[0]]; pos[0] += 1
            r |= (b & 0x7F) << s
            if b < 0x80:
                return r
            s += 7

    def unzig(z):
        return (z >> 1) if not (z & 1) else -((z + 1) >> 1)

    n = varint()
    runs = []
    for _ in FACT_FIELDS:
        nr = varint()
        vs, v = [], 0
        for _ in range(nr):
            v += unzig(varint())
            vs.append(v)
        ls = [varint() for _ in range(nr)]
        assert sum(ls) == n, "run lengths do not add up to the row count"
        runs.append((vs, ls))
    return n, runs, raw, pos[0]


def decode_fact(fact):
    """Inverse of FactTable.encode() -> list of (state, city, dealer, (brand, bu, model), month, medium, lt,
    'YYYY-MM-DD' or ''). Used by the regression tests and for spot checks; the dashboard has its own JS decoder."""
    n, runs, raw, p = _fact_read(fact)
    cols = []
    for vs, ls in runs:
        col = []
        for v, ln in zip(vs, ls):
            col.extend([v] * ln)
        cols.append(col)
    epoch = datetime.strptime(fact["epoch"], "%Y-%m-%d")
    names = [fact["states"], fact["cities"], fact["dealers"], [tuple(x) for x in fact["products"]],
             fact["months"], fact["mediums"], fact["lts"]]
    out, d = [], 0
    for i in range(n):
        z = r = 0
        while True:
            b = raw[p]; p += 1
            r |= (b & 0x7F) << z
            if b < 0x80:
                break
            z += 7
        d += (r >> 1) if not (r & 1) else -((r + 1) >> 1)
        out.append(tuple(names[f][cols[f][i]] for f in range(len(FACT_FIELDS)))
                   + ((epoch + timedelta(days=d - 1)).strftime("%Y-%m-%d") if d else "",))
    return out


def fact_check(fact, totals):
    """Cross-check an encoded fact table against the 1-D aggregates built from the very same rows.
    `totals` maps field name ('state', 'city', 'dealer', 'month', 'medium', 'lt', 'brand', 'bu', 'model') to
    {value: leads}. Works on the run lists, so it costs a second or two, not a full row decode.
    Returns a list of human-readable problems (empty = every total reconciles)."""
    n, runs, _, _ = _fact_read(fact)
    names = {"state": fact["states"], "city": fact["cities"], "dealer": fact["dealers"],
             "month": fact["months"], "medium": fact["mediums"], "lt": fact["lts"]}
    got = {}
    for fi, f in enumerate(FACT_FIELDS):
        if f == "product":
            continue
        c = Counter()
        for v, ln in zip(*runs[fi]):
            c[names[f][v]] += ln
        got[f] = c
    pc = Counter()
    for v, ln in zip(*runs[FACT_FIELDS.index("product")]):
        pc[v] += ln
    for key, idx in (("brand", 0), ("bu", 1), ("model", 2)):
        c = Counter()
        for v, ln in pc.items():
            c[fact["products"][v][idx]] += ln
        got[key] = c
    problems = []
    for f, want in totals.items():
        have = got[f]
        if sum(have.values()) != n:
            problems.append(f"{f}: fact rows {sum(have.values()):,} != {n:,}")
        bad = {k: (have.get(k, 0), want.get(k, 0)) for k in set(have) | set(want) if have.get(k, 0) != want.get(k, 0)}
        if bad:
            sample = dict(list(bad.items())[:3])
            problems.append(f"{f}: {len(bad)} value(s) differ from the aggregates (fact, aggregate), e.g. {sample}")
    return problems


def build_aggregations(all_rows, model_to_bu=None, oem_data=None):
    # oem_data is accepted for compatibility but no longer used here: the page derives each lead's OEM priority
    # tier from D.oem_data and the lead table's brand / BU / city.
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

    # Counters. Everything the tabs filter and cut goes through the lead table (FactTable, below); what is kept
    # here is only what the dashboard still reads directly: totals, the small lookup maps behind the filter
    # drop-downs, and the month/model/day sections the Ask vs Actual tab reads.
    by_brand = {}; by_medium = {}; by_state = {}; by_city = {}
    by_model = {}; by_month = {}; by_dealer = {}; by_lt = {}
    brand_month = defaultdict(dict); brand_model = defaultdict(dict)
    city_state_ctr = defaultdict(Counter)
    model_brand = defaultdict(dict)
    model_medium_month = defaultdict(lambda: defaultdict(dict))  # model → medium → month → count
                                      # (mirrors brand_medium_month — powers the Ask vs Actual
                                      # tab's model-level Actual column when Source is filtered)
    model_date = defaultdict(dict)   # model → date (YYYY-MM-DD) → count — trimmed to a
                                      # rolling recent window below (see model_date trim)
    model_medium_date = defaultdict(lambda: defaultdict(dict))  # model → medium → date → count
                                      # (same rolling window trim — powers the Ask vs Actual
                                      # daily-trend table when the top-bar Source filter is set)
    model_month  = defaultdict(dict)
    brand_medium_month = defaultdict(lambda: defaultdict(dict))  # brand → medium → month → count

    # BU counters
    by_bu = {}
    bu_brand  = defaultdict(dict); bu_month  = defaultdict(dict)
    bu_model  = defaultdict(dict)
    model_bu_map = {}   # model (canonical case) → BU string

    dealer_code_map = {}  # dealer_key → verified_dealer code
    dealer_name_map = {}  # dealer_key → Dealer_Name
    brand_date_min = {}   # brand → earliest lead date (datetime)
    brand_date_max = {}   # brand → latest lead date (datetime)

    months_seen = set()

    # Months that get the full (model, state, medium, month, date) cube: the most recent
    # ASK_CUBE_MONTHS that actually have leads (chosen by Lead_Month, so a month's cube always
    # reconciles to that month's totals even for the few rows dated outside the month).
    _cube_month_rank = {m: i for i, m in enumerate(MONTH_ORDER)}
    _cube_present = sorted(
        {m for m in (str(r.get("Lead_Month", "") or "").strip() for r in all_rows)
         if m in _cube_month_rank and m not in SKIP_MONTHS},
        key=_cube_month_rank.get)
    ask_cube_months = set(_cube_present[-ASK_CUBE_MONTHS:])
    ask_cube = defaultdict(int)   # (model, state, medium, month, "YYYY-MM-DD" or "") -> leads
    fact = FactTable()            # one row per counted lead -- see FactTable

    def parse_lead_date(raw):
        """Parse date string in either yyyy-mm-dd or yyyy-Mon-dd format."""
        if not raw:
            return None
        raw = raw.strip()
        for fmt in ('%Y-%m-%d', '%Y-%b-%d'):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                pass
        return None

    for r in all_rows:
        brand   = str(r.get("brand",  "") or "").strip()
        brand   = BRAND_NORMALIZE.get(brand.lower(), brand)   # idempotent — stored rows already normalized
        brand_raw = brand                                       # fallback BU for a model the Model Mapping tab lacks
        brand   = BRAND_ALIASES.get(brand.lower(), brand)     # idempotent — stored rows already aliased
        medium  = str(r.get("Medium", "") or "").strip() or "Unknown"
        state   = normalize_state(str(r.get("State",  "") or "").strip()) or "Unknown"
        city    = normalize_city(str(r.get("City",   "") or "").strip()) or "Unknown"
        crm_id  = str(r.get("oem_crm_id","") or "").strip()
        d_city  = normalize_city(str(r.get("City","")  or "").strip())
        d_state = normalize_state(str(r.get("State","") or "").strip())
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

        if not _looks_like_brand(brand_raw):
            continue
        if month in SKIP_MONTHS:
            continue

        brands_set.add(brand)
        months_seen.add(month)

        # Track min/max lead date per brand
        lead_dt = parse_lead_date(r.get("Date", ""))
        if lead_dt:
            if brand not in brand_date_min or lead_dt < brand_date_min[brand]:
                brand_date_min[brand] = lead_dt
            if brand not in brand_date_max or lead_dt > brand_date_max[brand]:
                brand_date_max[brand] = lead_dt

        inc(by_brand,  brand);  inc(by_medium, medium)
        inc(by_state,  state);  inc(by_city,   city)
        inc(by_model,  model);  inc(by_month,  month)
        inc(by_dealer, dealer); inc(by_lt,      lt)

        inc(brand_month[brand],   month)
        inc(brand_medium_month[brand][medium], month)
        inc(brand_model[brand],   model)
        city_state_ctr[city][state] += 1
        inc(model_brand[model],   brand)
        inc(model_month[model],   month)
        inc(model_medium_month[model][medium], month)

        # BU aggregations — the Model Mapping tab's BU; for models it lacks, the sub-brand in the model name, else the brand
        bu = resolve_bu(model, brand_raw, model_to_bu)
        inc(by_bu,          bu)
        inc(bu_brand[bu],   brand)
        inc(bu_month[bu],   month)
        if lead_dt:
            date_str = lead_dt.strftime("%Y-%m-%d")
            inc(model_date[model], date_str)
            inc(model_medium_date[model][medium], date_str)
        if month in ask_cube_months:
            ask_cube[(model, state, medium, month, date_str if lead_dt else "")] += 1
        inc(bu_model[bu],   model)
        model_bu_map[model] = bu
        fact.add(state, city, dealer, (brand, bu, model), month, medium, lt, lead_dt)

    # Sort months by canonical order
    month_key = {m: i for i, m in enumerate(MONTH_ORDER)}
    months_present = sorted(months_seen, key=lambda x: month_key.get(x, 999))

    # Sort dicts by value desc
    def srt(d):
        return dict(sorted(d.items(), key=lambda x: -x[1]))

    brands_all = sorted(brands_set)

    # Flatten the Ask cube. Dimensions are dictionary-encoded, and cells that share
    # (model, state, medium, month) are stored as one group:
    #     rows = [model, state, medium, month, k, (date delta, leads) x k, ...repeated per group]
    # with the group's dates ascending and delta-coded from index 0 (date index 0 is "", rows with no
    # parseable date, so month totals still reconcile). Same information as one row per cell
    # at roughly a third of the size (3.3 MB instead of 9.5 MB for ~600K cells on 2026-10-06).
    ask_cube_out = None
    if ask_cube:
        _keys = sorted(ask_cube)
        _models  = sorted({k[0] for k in _keys})
        _states  = sorted({k[1] for k in _keys})
        _media   = sorted({k[2] for k in _keys})
        _months  = sorted({k[3] for k in _keys}, key=lambda m: month_key.get(m, 999))
        _dates   = [""] + sorted({k[4] for k in _keys if k[4]})
        _mi = {v: i for i, v in enumerate(_models)}; _si = {v: i for i, v in enumerate(_states)}
        _ei = {v: i for i, v in enumerate(_media)};  _oi = {v: i for i, v in enumerate(_months)}
        _di = {v: i for i, v in enumerate(_dates)}
        _groups = {}
        for k in _keys:   # sorted, so each group's cells are adjacent and its dates ascend
            _groups.setdefault((_mi[k[0]], _si[k[1]], _ei[k[2]], _oi[k[3]]), []).append((_di[k[4]], ask_cube[k]))
        _flat = []
        for _g, _cells in _groups.items():
            _flat.extend(_g)
            _flat.append(len(_cells))
            _prev = 0
            for _d, _n in _cells:
                _flat.extend((_d - _prev, _n))
                _prev = _d
        ask_cube_out = {"models": _models, "states": _states, "mediums": _media,
                        "months": _months, "dates": _dates, "rows": _flat}
        # Cheap self-check: each month's cube must add up to that month's dashboard total.
        _cube_by_month = Counter()
        for k, n in ask_cube.items():
            _cube_by_month[k[3]] += n
        _bad = {m: (n, by_month.get(m, 0)) for m, n in _cube_by_month.items() if n != by_month.get(m, 0)}
        print(f"  Ask cube: {len(_keys):,} cells over {len(_months)} months "
              f"({_months[0]} - {_months[-1]}); " + ("reconciles with every month's total" if not _bad
              else f"MISMATCH vs month totals (cube, total): {_bad}"))

    fact_out = fact.encode()
    _problems = fact_check(fact_out, {
        "brand": by_brand, "bu": by_bu, "model": by_model, "state": by_state, "city": by_city, "dealer": by_dealer,
        "medium": by_medium, "month": by_month, "lt": by_lt})
    # The 1-D totals can't see one column drifting against another, so also check the table grouped by
    # (model, state, medium, month) against the Ask cube, which is built separately from the same rows.
    if ask_cube:
        sh = fact._shift
        bits = FACT_BITS
        blk_bits = bits["product"] + bits["month"] + bits["medium"]       # product | month | medium sit side by side
        blk_mask = (1 << blk_bits) - 1
        st_mask = (1 << bits["state"]) - 1
        s_med, s_state = sh["medium"], sh["state"]
        joint = Counter()
        for k in fact.keys:
            joint[(((k >> s_state) & st_mask) << blk_bits) | ((k >> s_med) & blk_mask)] += 1
        inv = {f: {i: v for v, i in fact.ids[f].items()} for f in ("state", "product", "month", "medium")}
        pm, mm, em = (1 << bits["product"]) - 1, (1 << bits["month"]) - 1, (1 << bits["medium"]) - 1
        got4 = Counter()
        for key, n in joint.items():
            mo = inv["month"][(key >> bits["medium"]) & mm]
            if mo in ask_cube_months:
                got4[(inv["product"][(key >> (bits["medium"] + bits["month"])) & pm][2], inv["state"][key >> blk_bits],
                      inv["medium"][key & em], mo)] += n
        want4 = Counter()
        for (mdl, st_, med_, mon_, _d), n in ask_cube.items():
            want4[(mdl, st_, med_, mon_)] += n
        if got4 != want4:
            _diff = {k: (got4.get(k, 0), want4.get(k, 0)) for k in set(got4) | set(want4) if got4.get(k, 0) != want4.get(k, 0)}
            _problems.append(f"model x state x source x month: {len(_diff)} cell(s) differ from the Ask cube (table, cube), e.g. {dict(list(_diff.items())[:2])}")
    print(f"  Fact table: {fact_out['n']:,} leads, {fact_out['bytes_raw'] / 1e6:.1f} MB raw -> "
          f"{fact_out['bytes_packed'] / 1e6:.1f} MB deflated" + (f" ({fact.bad_dates:,} unusable dates)" if fact.bad_dates else "")
          + ("; reconciles with every aggregate" if not _problems else "; MISMATCH vs aggregates: " + "; ".join(_problems)))
    del fact.keys[:]   # free the packed keys
    if _problems:
        fact_out["problems"] = _problems   # main() turns these into header warnings

    # model_date trim: day-level granularity for the *entire* multi-year history would
    # balloon the embedded JSON (~30x the rows of model_month) for no real benefit — the
    # only consumer is the Ask vs Actual tab's daily-trend table, whose month picker is
    # capped to whatever months the "Consol Ask" sheet currently covers (a 5-month rolling
    # window today). Keep a generous ~8-month rolling window instead of full history.
    _model_dates_flat = [d for dates in model_date.values() for d in dates]
    if _model_dates_flat:
        _md_cutoff = (datetime.strptime(max(_model_dates_flat), "%Y-%m-%d")
                      - timedelta(days=240)).strftime("%Y-%m-%d")
        model_date = {
            m: {d: v for d, v in dates.items() if d >= _md_cutoff}
            for m, dates in model_date.items()
        }
        model_date = {m: dates for m, dates in model_date.items() if dates}
        # Same cutoff, same reasoning, for the medium-broken-out counterpart.
        model_medium_date = {
            m: {med: {d: v for d, v in dates.items() if d >= _md_cutoff}
                for med, dates in meds.items()}
            for m, meds in model_medium_date.items()
        }
        model_medium_date = {
            m: {med: dates for med, dates in meds.items() if dates}
            for m, meds in model_medium_date.items()
        }
        model_medium_date = {m: meds for m, meds in model_medium_date.items() if meds}

    # Display names / CRM codes of the dealers travel with the lead table, aligned with its dealer list
    fact_out["dealer_names"] = [dealer_name_map.get(d, "") for d in fact_out["dealers"]]
    fact_out["dealer_codes"] = [dealer_code_map.get(d, "") for d in fact_out["dealers"]]

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
        "by_lt":         srt(by_lt),
        "brand_month":   {k: {m: v.get(m,0) for m in months_present} for k,v in brand_month.items()},
        "brand_medium_month": {b: {med: {m: v.get(m,0) for m in months_present}
                               for med, v in meds.items()}
                               for b, meds in brand_medium_month.items()},
        "brand_model":   dict(brand_model),
        "city_state":    {c: s.most_common(1)[0][0] for c, s in city_state_ctr.items()},
        "model_brand":   dict(model_brand),
        "model_medium_month": {mo: {med: {m: v.get(m,0) for m in months_present}
                               for med, v in meds.items()}
                               for mo, meds in model_medium_month.items()},
        "model_date":    model_date,
        "model_medium_date": model_medium_date,
        "ask_cube":      ask_cube_out,
        "fact":          fact_out,
        "by_bu":         srt(by_bu),
        "bu_brand":      dict(bu_brand),
        "bu_month":      {k: {m: v.get(m,0) for m in months_present} for k,v in bu_month.items()},
        "bu_model":      dict(bu_model),
        "model_bu":      model_bu_map,
        "model_month":   {k: {m: v.get(m,0) for m in months_present} for k,v in model_month.items()},
        "last_updated":  datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
        "brand_date_range": {
            b: {
                "min": brand_date_min[b].strftime("%m/%d/%Y"),
                "max": brand_date_max[b].strftime("%m/%d/%Y"),
            }
            for b in brands_set if b in brand_date_min
        },
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

    # (This used to wipe manifest.processed on the 1st of every month to force a full
    # re-download of every file. Month rollovers are now handled precisely instead:
    # the rolling T / T-1 / T-2 files are re-read on every run, a renamed file is
    # re-read because its name changed, and files that vanish from the folder are
    # dropped as orphans — see the loop below. The blanket wipe re-read ALL ~29
    # files on every run that day (~40 min each, queued behind each other), and
    # because it also emptied the manifest it skipped the purge of each file's
    # old rows, so stale copies survived alongside the fresh ones.)
    now_ist = datetime.now(IST)

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

    # Things worth a human's attention this run; shown in the dashboard header.
    # Warnings about one file are kept per file ID, and replaced whenever that file is
    # read again later in the run, so what ends up in the header describes the read that
    # actually went into the dashboard rather than an earlier, superseded one.
    run_warnings = []
    file_warnings = {}   # fid -> [msg]
    def warn(msg, fid=None):
        print(f"  ⚠ {msg}")
        if fid:
            file_warnings.setdefault(fid, []).append(msg)
        else:
            run_warnings.append(msg)

    # ── Rolling-file roster ────────────────────────────────────────────────────
    # All six (TVS/Bike × T/T-1/T-2) are read every run, so say up front which
    # ones exist, and flag any that don't — a rename we haven't been told about
    # would otherwise just quietly stop being tracked as "rolling".
    by_norm = {}
    for f in files:
        if rolling_role(f["name"]):
            by_norm.setdefault(_norm_name(f["name"]), []).append(f)
    print("Rolling files (re-read on every run):")
    for nm in expected_rolling_names():
        found = by_norm.get(_norm_name(nm), [])
        print(f"    {'✓' if found else '✗'} {nm}" + (f"  [{len(found)} files with this name]" if len(found) > 1 else ""))
        if not found:
            warn(f"expected file not found in the Drive folder: '{nm}'")
        elif len(found) > 1:
            warn(f"{len(found)} files are all named '{nm}' — all of them are read; worth checking the folder")

    # fid -> rows read this run. A file read a second time (see the sweep below) simply
    # replaces its entry, so its earlier read can't linger next to the new one.
    fresh_by_fid = {}
    retry_ids = set()   # files whose read failed (or came back suspiciously empty) — tried again in the sweep
    run_info = {}       # fid -> {"checked": str, "ok": bool, "settled": bool}  for the roster
    settle_budget = {"left": SETTLE_BUDGET}

    def ingest(f, role, prev):
        """Read one Drive file and fold the result into the manifest and this run's rows."""
        fid, name, mtime = f["id"], f["name"], f["modifiedTime"]
        file_warnings.pop(fid, None)     # a file's warnings are those of its most recent read
        retry_ids.discard(fid)
        if role:
            run_info[fid] = {"checked": None, "ok": False, "settled": True}

        # Read it. For a rolling file, first make sure the source job isn't mid-write,
        # and afterwards that it didn't start writing while we were reading (a read
        # that overlapped a write can be a torn mix of old and new).
        settled, new_rows, read_err = True, None, None
        for attempt in range(3 if role else 1):
            if role:
                mtime, settled = wait_until_settled(drive_svc, fid, name, mtime, settle_budget)
            try:
                new_rows = read_source_file(drive_svc, sheets_svc, fid, name, prev.get("rows", 0))
            except Exception as e:
                read_err = e
                break
            if not role:
                break
            try:
                after = current_modified_time(drive_svc, fid)
            except Exception:
                break
            if after == mtime:
                break
            print(f"    modified while it was being read ({mtime[11:19]} → {after[11:19]} UTC)"
                  + (" — reading it again" if attempt < 2 else ""))
            mtime, settled = after, False
        if read_err is not None:
            warn(f"{name}: could not be read this run ({read_err}) — keeping the last successful read", fid)
            retry_ids.add(fid)
            return
        if role and not settled:
            warn(f"{name}: was still being modified by the source job when it was read, so these "
                 f"figures may be a partial snapshot — the next refresh will pick up the finished file", fid)
        if role:
            run_info[fid]["settled"] = settled

        prev_rows = prev.get("rows", 0)
        top_month = (Counter(r.get("Lead_Month") for r in new_rows).most_common(1) or [(None, 0)])[0][0]
        checked = datetime.now(IST).strftime("%d %b %H:%M")

        if not new_rows:
            if prev_rows >= EMPTY_GUARD_ROWS:
                # A file that held thousands of rows a run ago and reads as empty now is
                # far more likely mid-write (or its headers changed) than genuinely wiped.
                # Keep what we have and leave the manifest alone so it's retried.
                warn(f"{name}: read 0 rows but held {prev_rows:,} on the last read — treating it as a "
                     f"bad/mid-write read and keeping the existing rows", fid)
                retry_ids.add(fid)
                return
            print(f"    (no brand rows found)")
            manifest["processed"][fid] = {"name": name, "modifiedTime": mtime, "rows": 0,
                                          "top_month": None, "checked": checked}
            fresh_by_fid[fid] = []      # drop any leftover rows from an earlier, fuller version
            if role:
                run_info[fid].update(checked=checked, ok=True)
            return

        n = len(new_rows)
        if prev_rows and n < prev_rows * SHRINK_WARN_RATIO and prev.get("top_month") in (None, top_month):
            warn(f"{name}: shrank from {prev_rows:,} to {n:,} rows ({n / prev_rows:.0%}) since the last read", fid)
        if role and n >= 20000 and n % 10000 == 0:
            warn(f"{name}: has exactly {n:,} rows — a suspiciously round number, which usually means a "
                 f"partially written or truncated sheet", fid)

        fresh_by_fid[fid] = new_rows
        manifest["processed"][fid] = {
            "name": name,
            "modifiedTime": mtime,
            "rows": n,
            "top_month": top_month,
            "checked": checked,
        }
        if role:
            run_info[fid].update(checked=checked, ok=True)
        print(f"    → {n:,} rows read" + (f"  (mostly {top_month})" if top_month else ""))

    def process(file_list, first_pass):
        """Read every file in `file_list` that needs it; returns how many were read.
        On the first pass that's everything changed since the last run plus ALL rolling
        files. On a later sweep it's only what has changed since this run read it."""
        done = 0
        # Most recently modified last: the files a source job is still writing get the
        # longest possible head start while we read everything else.
        for f in sorted(file_list, key=lambda x: x["modifiedTime"]):
            fid, name, mtime = f["id"], f["name"], f["modifiedTime"]
            prev = manifest["processed"].get(fid, {})
            role = rolling_role(name)                   # ("TVS"|"Bike", "T"|"T-1"|"T-2") or None
            renamed = bool(prev) and prev.get("name") != name
            if prev.get("modifiedTime") == mtime and not (role and first_pass) and not renamed \
                    and fid not in retry_ids:
                if first_pass:
                    print(f"  ✓ unchanged: {name}")
                continue

            why = (f"rolling {role[1]} — re-read every run" if role and first_pass
                   else "renamed since last run" if renamed
                   else f"modified {mtime[:10]}" if first_pass
                   else "trying again — it couldn't be read properly earlier in this run" if fid in retry_ids
                   else f"changed at {mtime[11:19]} UTC, after this run had read it" if prev
                   else "new in the folder since this run started")
            print(f"  ↓ processing: {name}  ({why})")
            ingest(f, role, prev)
            done += 1
        return done

    process(files, first_pass=True)

    # ── Late changes ───────────────────────────────────────────────────────────
    # The run above takes ~20 min and the source job's writes are spread over ~90 min
    # each morning, so a file can change after we've read it but before the run ends
    # (seen 2026-10-06: TVS "Previous to Previous Month" went from 110,000 to 157,672
    # rows 7 min after a run had read it, and the dashboard kept the 110,000 until the
    # next run). So list the folder again and re-read whatever moved on in the meantime.
    for sweep in range(1, MAX_SWEEPS + 1):
        print(f"\nLate-change check {sweep}/{MAX_SWEEPS}: looking at the Drive folder again…")
        try:
            latest = list_folder_sheets(drive_svc)
        except Exception as e:
            warn(f"could not re-list the Drive folder to look for late changes ({e})")
            break
        gone = {f["id"] for f in files} - {f["id"] for f in latest}
        if gone and len(gone) > max(3, len(files) // 4):
            # Same guard as for orphans: this looks like a listing glitch, not real deletions.
            warn(f"{len(gone)} of {len(files)} files vanished from the folder listing between "
                 f"the start and end of this run — ignoring the second listing")
            break
        files = latest
        if process(files, first_pass=False) == 0:
            print("  ✓ nothing changed while this run was in progress")
            break

    # ── Orphans ────────────────────────────────────────────────────────────────
    # Files we ingested earlier that are no longer in the folder (deleted, trashed
    # or moved — e.g. the temporary dated copies the source automation makes and
    # removes around each month rollover). Their rows would otherwise sit in the
    # cache forever as ghost copies of leads, shadowing the live files whenever
    # those change. Only IDs recorded in the manifest count, so rows with no
    # _file_id (very old ingests) are never touched, and a guard refuses to act if
    # an implausible share of files "vanished" at once (a listing glitch, not
    # real deletions) — they'd just be re-read next run if they reappear.
    # Judged against the folder as it stands at the END of the run.
    drive_ids = {f["id"] for f in files}
    orphan_ids = [i for i in manifest["processed"] if i not in drive_ids]
    if orphan_ids:
        tracked = len(manifest["processed"])
        if not files or len(orphan_ids) > max(3, tracked // 4):
            warn(f"{len(orphan_ids)} of {tracked} tracked files are missing from the Drive listing — "
                 f"too many to be real deletions, so leaving them alone this run")
            orphan_ids = []
        else:
            for i in orphan_ids:
                o = manifest["processed"].pop(i)
                fresh_by_fid.pop(i, None)       # read earlier in this run, gone by its end
                print(f"  🗑 no longer in the Drive folder: {o.get('name')} ({o.get('rows', 0):,} rows) — removing its rows")

    fresh_ids  = set(fresh_by_fid)              # IDs of files successfully (re-)read this run
    fresh_rows = [r for rows in fresh_by_fid.values() for r in rows]
    new_file_count = sum(1 for rows in fresh_by_fid.values() if rows)
    new_row_count  = len(fresh_rows)
    # (a warning about a file that has since left the folder is no longer worth showing)
    warnings = run_warnings + [w for fid, ws in file_warnings.items() if fid in drive_ids for w in ws]

    # Swap in the fresh reads. Every previously-cached row tagged with a file we just
    # re-read (or one that left the folder) is dropped first, unconditionally — not
    # only when the manifest remembers a non-zero row count — so a file's old rows can
    # never survive alongside its new ones.
    drop_ids = fresh_ids | set(orphan_ids)
    if drop_ids:
        before = len(all_rows)
        all_rows = [r for r in all_rows if r.get("_file_id") not in drop_ids]
        print(f"\nDropped {before - len(all_rows):,} cached rows from {len(fresh_ids)} re-read "
              f"file(s)" + (f" and {len(orphan_ids)} removed file(s)" if orphan_ids else ""))
    all_rows.extend(fresh_rows)

    # Roster for the dashboard header: what each rolling file looked like this run.
    role_order = {"T": 0, "T-1": 1, "T-2": 2}
    roster = []
    for f in files:
        role = rolling_role(f["name"])
        if not role:
            continue
        ent = manifest["processed"].get(f["id"], {})
        info = run_info.get(f["id"], {})
        roster.append({
            "brand": role[0], "role": role[1], "file": f["name"],
            "month": ent.get("top_month"), "rows": ent.get("rows", 0),
            "modified": f["modifiedTime"], "checked": info.get("checked") or ent.get("checked"),
            "ok": bool(info.get("ok")), "settled": info.get("settled", True),
        })
    roster.sort(key=lambda r: (r["brand"], role_order.get(r["role"], 9)))

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
    model_to_bu = load_model_bu_mapping(sheets_svc)
    print("Active nomenclature rules:")
    if BRAND_ALIASES:
        for src, tgt in BRAND_ALIASES.items(): print(f"  Brand  : '{src}' → '{tgt}'")
    if BU_ALIASES:
        for src, tgt in BU_ALIASES.items():    print(f"  BU     : '{src}' → '{tgt}'")
    # Load OEM data before aggregations so bu_pri_date can use city→priority mapping
    oem_data = load_oem_data(REPO_ROOT)
    print("Building aggregations...")
    # Counts for UNIQUE_PHONE_BRANDS are by unique mobile number per month. That is applied to what the
    # dashboard counts, not to the cache saved above, which keeps every distinct lead.
    up_stats = {}
    agg_rows = unique_phone_per_month(all_rows, stats=up_stats)
    left_out = len(all_rows) - len(agg_rows)
    print(f"Unique-phone rule ({', '.join(sorted(UNIQUE_PHONE_BRANDS))}): {left_out:,} repeat-phone leads left out of the counts")
    dash = build_aggregations(agg_rows, model_to_bu=model_to_bu, oem_data=oem_data)
    dash["unique_phone"] = {"brands": sorted(UNIQUE_PHONE_BRANDS), "left_out": left_out,
                            "left_out_by_month": {m: n for m, n in sorted(up_stats.get("by_month", {}).items())}}
    print(f"Total: {dash['total']:,} | Brands: {list(dash['by_brand'].keys())}")
    for prob in dash["fact"].get("problems", []):
        warnings.append(f"Lead table does not reconcile with the aggregates — {prob}")

    # What this run read from the rolling source files, and anything that looked off —
    # shown in the dashboard header so it's visible without opening the Actions log.
    dash["source_files"] = roster
    dash["source_warnings"] = warnings
    if warnings:
        print(f"\n⚠ {len(warnings)} source warning(s) this run:")
        for w in warnings:
            print(f"    - {w}")

    if oem_data:
        dash["oem_data"] = oem_data
    ims_data = load_ims_data(REPO_ROOT)
    if ims_data:
        dash["ims_mapping"] = ims_data
    ask_data = load_ask_data(sheets_svc)
    if ask_data:
        dash["ask_data"] = ask_data

    # Stamp version + deployment timestamp (IST)
    new_version = manifest.get("version", 0) + 1
    now_ist = datetime.now(IST)
    dash["version"]     = new_version
    dash["deployed_at"] = now_ist.strftime("%d %b %Y, %H:%M IST")
    print(f"Version: v{new_version}  |  Deployed at: {dash['deployed_at']}")

    with open(DASH_F, "w") as f:
        json.dump(dash, f)
    print(f"Saved dashboard_data.json")

    # Re-embed data into index.html
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
