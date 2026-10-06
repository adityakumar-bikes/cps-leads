"""
Regression test for scripts/refresh_data.py: drives the real main() against a simulated
Drive folder, over many consecutive "runs" that share one cache + manifest (like the
GitHub Actions cache does), and asserts on what ends up in the dashboard data.

    python3 scripts/test_refresh_pipeline.py

No network or credentials needed; everything is written to a temp dir and the clock is
faked, so the 30-minute "wait for the source job" scenarios finish instantly. Covers the
rolling T / T-1 / T-2 files (re-read every run, rollover with file-ID churn, partial /
empty / failed / torn reads, never-settling files, clock skew, files changing or
vanishing while a run is in progress) plus orphan purge and the warnings shown in the
dashboard header. Exit code is non-zero if any check fails.
"""
import sys, os, json, gzip, shutil, tempfile, io, contextlib, copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh_data as rd

TMP = tempfile.mkdtemp(prefix="simmain_")
os.makedirs(os.path.join(TMP, "data"))
with open(os.path.join(TMP, "index.html"), "w") as f:
    f.write('<script>\nconst D={"x":1};\nconst BCOL={};\n</script>')
rd.REPO_ROOT = TMP
rd.DATA_DIR = os.path.join(TMP, "data")
rd.MANIFEST_F = os.path.join(rd.DATA_DIR, "manifest.json")
rd.LEADS_F = os.path.join(rd.DATA_DIR, "all_leads.json.gz")
rd.DASH_F = os.path.join(rd.DATA_DIR, "dashboard_data.json")

# ---- simulated Drive -------------------------------------------------------
_n = [0]
def mk_rows(fid, brand, month, count, medium="Organic", model=None, start=None):
    rows = []
    for i in range(count):
        _n[0] += 1
        k = _n[0] if start is None else start + i
        rows.append({"opty_id": f"26{k:016d}", "encrypt_mobile_number": f"m{k}", "brand": brand,
                     "model": model or f"{brand} M1", "Medium": medium, "Lead_Month": month,
                     "Date": "2026-10-03", "City": "Pune", "State": "Maharashtra", "_file_id": fid})
    return rows

LISTING = []          # [{id,name,modifiedTime}]
CONTENT = {}          # fid -> rows (what a successful read returns)
FAIL = {}             # fid -> Exception to raise on read
READS = []            # fids actually read, in order

def fake_export_via_sheets_api(svc, fid, name):
    READS.append(fid)
    if fid in FAIL: raise FAIL[fid]
    return [dict(r, _file_id=fid) for r in CONTENT.get(fid, [])]

ZIP_CALLS = []        # fids for which the (always-too-large) ZIP export was attempted

def fake_export_as_zip(svc, fid, max_retries=3):
    ZIP_CALLS.append(fid)
    raise rd.FileTooLargeError("sim")

import types, datetime as _dt
BASE = _dt.datetime(2026, 10, 7, 0, 0, 0, tzinfo=_dt.timezone.utc)
class Clock:
    def __init__(self): self.t = BASE; self.events = []        # [(when, fn)]
    def at(self, delta_s, fn): self.events.append((self.t + _dt.timedelta(seconds=delta_s), fn))
    def sleep(self, s):
        self.t += _dt.timedelta(seconds=s)
        due = [e for e in self.events if e[0] <= self.t]
        self.events = [e for e in self.events if e[0] > self.t]
        for _, fn in sorted(due, key=lambda e: e[0]): fn()
clock = Clock()
rd._now_utc = lambda: clock.t
rd.time = types.SimpleNamespace(sleep=clock.sleep, time=lambda: clock.t.timestamp())
MT = {}                                    # fid -> live modifiedTime (what Drive would say right now)
def iso(dt): return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
class _Get:
    def __init__(self, fid): self.fid = fid
    def execute(self): return {"modifiedTime": MT[self.fid]}
class _Files:
    def get(self, fileId, fields=None): return _Get(fileId)
class FakeDrive:
    def files(self): return _Files()
rd.get_services = lambda: (FakeDrive(), object())
rd.list_folder_sheets = lambda svc: [dict(f, modifiedTime=MT.get(f["id"], f["modifiedTime"])) for f in LISTING]
rd.export_as_zip = fake_export_as_zip
rd.export_via_sheets_api = fake_export_via_sheets_api
rd.load_model_bu_mapping = lambda s: {}
rd.load_oem_data = lambda r: {}
rd.load_ims_data = lambda r: {}
rd.load_ask_data = lambda s: {}

def run():
    for x in LISTING: MT.setdefault(x["id"], x["modifiedTime"])
    READS.clear(); ZIP_CALLS.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rd.main()
    log = buf.getvalue()
    dash = json.load(open(rd.DASH_F))
    man = json.load(open(rd.MANIFEST_F))
    return log, dash, man

def total(dash, brand=None, month=None):
    if brand and month: return dash["brand_month"].get(brand, {}).get(month, 0)
    if brand: return dash["by_brand"].get(brand, 0)
    return dash["total"]

PASS = FAIL_N = 0
def check(label, cond, extra=""):
    global PASS, FAIL_N
    if cond: PASS += 1; print(f"  PASS  {label}")
    else:    FAIL_N += 1; print(f"  FAIL  {label}  {extra}")

B = "TVS CPS Triggered LD LMS Status"
M = "Sep'2026"; O = "Oct'2026"; A = "Aug'2026"; J = "Jul'2026"

# ============ RUN 1: cold start, 7 files (1 static + 6 rolling) =============
print("\n== RUN 1: cold start ==")
LISTING[:] = [
  {"id":"sJul", "name":f"{B} - Jul'26",                         "modifiedTime":"2026-09-03T00:00:00Z"},
  {"id":"tvsT", "name":B,                                         "modifiedTime":"2026-10-06T08:00:00Z"},
  {"id":"tvsT1","name":f"{B} - Previous Month",                  "modifiedTime":"2026-10-06T07:30:00Z"},
  {"id":"tvsT2","name":f"{B} - Previous to Previous Month",      "modifiedTime":"2026-10-06T07:00:00Z"},
  {"id":"bkT",  "name":"Bike CPS Triggered LD LMS Status",        "modifiedTime":"2026-10-06T08:00:00Z"},
  {"id":"bkT1", "name":"Bike CPS Triggered LD LMS Status - Previous Month", "modifiedTime":"2026-10-06T08:10:00Z"},
  {"id":"bkT2", "name":"Bike CPS Triggered LD LMS Status - Previous to Previous Month", "modifiedTime":"2026-10-06T07:05:00Z"},
]
CONTENT["sJul"]  = mk_rows("sJul","TVS",J,100)
CONTENT["tvsT"]  = mk_rows("tvsT","TVS",O,300)
CONTENT["tvsT1"] = mk_rows("tvsT1","TVS",M,1500)
CONTENT["tvsT2"] = mk_rows("tvsT2","TVS",A,400)
CONTENT["bkT"]   = mk_rows("bkT","Bajaj",O,200)
CONTENT["bkT1"]  = mk_rows("bkT1","Bajaj",M,1200)
CONTENT["bkT2"]  = mk_rows("bkT2","Bajaj",A,900)
log, dash, man = run()
check("all 7 files read on a cold start", len(READS) == 7, READS)
check("dashboard total = 100+300+1500+400+200+1200+900 = 4600", total(dash) == 4600, total(dash))
check("roster lists all 6 rolling files", len(dash["source_files"]) == 6, dash["source_files"])
check("roster roles T/T-1/T-2 for both brands", sorted((r['brand'],r['role']) for r in dash["source_files"]) ==
      sorted([(b,r) for b in ("TVS","Bike") for r in ("T","T-1","T-2")]))
check("roster reports the month each file holds", {(r['brand'],r['role']):r['month'] for r in dash["source_files"]}[("TVS","T-1")] == M)
check("no warnings on a clean run", dash["source_warnings"] == [], dash["source_warnings"])
check("static file is NOT flagged rolling", all(r['file'] != f"{B} - Jul'26" for r in dash["source_files"]))

# ============ RUN 2: nothing changed in Drive ================================
print("\n== RUN 2: nothing changed in Drive (same modifiedTimes) ==")
log, dash, man = run()
check("the 6 rolling files were re-read even though modifiedTime is unchanged", set(READS) == {"tvsT","tvsT1","tvsT2","bkT","bkT1","bkT2"}, READS)
check("the static dated file was NOT re-read", "sJul" not in READS, READS)
check("total unchanged (no double counting from re-reads)", total(dash) == 4600, total(dash))
check("static file still counted", total(dash,"TVS",J) == 100, total(dash,"TVS",J))

# ============ RUN 3: content changes but modifiedTime does NOT ===============
print("\n== RUN 3: rows removed from T-1 with no modifiedTime change (formula-style edit) ==")
CONTENT["tvsT1"] = CONTENT["tvsT1"][:1400]          # 100 leads dropped at the source
log, dash, man = run()
check("dashboard mirrors the sheet: TVS Sep = 1400", total(dash,"TVS",M) == 1400, total(dash,"TVS",M))
check("total = 4500", total(dash) == 4500, total(dash))

# ============ RUN 4: month rollover (names + IDs shuffle) ====================
print("\n== RUN 4: rollover — new copy file, rename, overwrite, clear, one file vanishes ==")
# Before: tvsT=Oct(300) tvsT1=Sep(1400) tvsT2=Aug(400)
# After (what the automation does): copy Aug into a NEW id as T-2; overwrite T-1 with Oct's content
# (pretend rolling forward a month); clear T; the old T-2 id is deleted.
LISTING[:] = [x for x in LISTING if x["id"] not in ("tvsT2",)]
LISTING.append({"id":"tvsT2new","name":f"{B} - Previous to Previous Month","modifiedTime":"2026-10-06T07:00:00Z"})
CONTENT["tvsT2new"] = mk_rows("tvsT2new","TVS",M,1400, start=10_000_000)   # Sep leads (previous T-1 content)
CONTENT["tvsT1"]    = mk_rows("tvsT1","TVS",O,300, start=20_000_000)       # T-1 now holds October
CONTENT["tvsT"]     = mk_rows("tvsT","TVS","Nov'2026",50, start=30_000_000)  # T cleared, new month started
for x in LISTING:
    if x["id"] in ("tvsT","tvsT1"): x["modifiedTime"] = "2026-10-06T08:00:00Z"
log, dash, man = run()
check("vanished file (tvsT2) reported as removed", "no longer in the Drive folder" in log and "tvsT2" not in man["processed"])
check("its Aug rows are gone (orphan purge): TVS Aug = 0", total(dash,"TVS",A) == 0, total(dash,"TVS",A))
check("no stale Sep rows left from old T-1: TVS Sep = 1400 (from the new T-2 only)", total(dash,"TVS",M) == 1400, total(dash,"TVS",M))
check("Oct = 300 (now in T-1)", total(dash,"TVS",O) == 300, total(dash,"TVS",O))
check("Nov = 50", total(dash,"TVS","Nov'2026") == 50, total(dash,"TVS","Nov'2026"))
check("static Jul untouched", total(dash,"TVS",J) == 100, total(dash,"TVS",J))

# ============ RUN 5: a rolling file reads as EMPTY while holding thousands ====
print("\n== RUN 5: T-1 reads empty (mid-write) ==")
CONTENT["bkT1"] = []
log, dash, man = run()
check("existing rows kept: Bajaj Sep still 1200", total(dash,"Bajaj",M) == 1200, total(dash,"Bajaj",M))
check("warning raised", any("read 0 rows" in w for w in dash["source_warnings"]), dash["source_warnings"])
CONTENT["bkT1"] = mk_rows("bkT1","Bajaj",M,1200, start=40_000_000)   # write finishes
log, dash, man = run()
check("next run picks up the finished write (still 1200, no duplicates)", total(dash,"Bajaj",M) == 1200, total(dash,"Bajaj",M))
check("warning cleared", dash["source_warnings"] == [], dash["source_warnings"])

# ============ RUN 6: read error ==============================================
print("\n== RUN 6: a read raises ==")
FAIL["bkT2"] = RuntimeError("Sheets API 500")
log, dash, man = run()
check("old rows kept: Bajaj Aug still 900", total(dash,"Bajaj",A) == 900, total(dash,"Bajaj",A))
check("warning names the file", any("bkT2" in w or "Previous to Previous Month" in w for w in dash["source_warnings"]), dash["source_warnings"])
check("roster marks it not-ok", [r for r in dash["source_files"] if r["brand"]=="Bike" and r["role"]=="T-2"][0]["ok"] is False)
del FAIL["bkT2"]

# ============ RUN 7: shrink + round-number warnings ==========================
print("\n== RUN 7: truncation signatures ==")
CONTENT["bkT1"] = mk_rows("bkT1","Bajaj",M,30000, start=50_000_000)    # grows to a round 30,000
log, dash, man = run()
check("round-number warning at exactly 30,000", any("round number" in w for w in dash["source_warnings"]), dash["source_warnings"])
CONTENT["bkT1"] = mk_rows("bkT1","Bajaj",M,12345, start=60_000_000)    # then shrinks to 41%
log, dash, man = run()
check("shrink warning (30,000 -> 12,345)", any("shrank" in w for w in dash["source_warnings"]), dash["source_warnings"])
CONTENT["bkT1"] = mk_rows("bkT1","Bajaj",M,1200, start=70_000_000)

# ============ RUN 8: listing glitch (everything "vanishes") ==================
print("\n== RUN 8: listing returns nothing ==")
saved = list(LISTING); LISTING[:] = []
before_total = total(json.load(open(rd.DASH_F)))
log, dash, man = run()
check("no orphan purge on an empty listing", total(dash) == before_total, (total(dash), before_total))
check("warning raised", any("too many to be real deletions" in w for w in dash["source_warnings"]), dash["source_warnings"])
LISTING[:] = saved

# ============ RUN 9: static file renamed, mtime unchanged ====================
print("\n== RUN 9: dated file renamed, modifiedTime unchanged ==")
for x in LISTING:
    if x["id"] == "sJul": x["name"] = f"{B} - Jul'26 (final)"
READS.clear()
log, dash, man = run()
check("renamed static file re-read", "sJul" in READS, READS)
check("no double counting after re-read: Jul = 100", total(dash,"TVS",J) == 100, total(dash,"TVS",J))

# ============ RUN 10: expected rolling file missing ==========================
print("\n== RUN 10: an expected rolling file is missing from the folder ==")
LISTING[:] = [x for x in LISTING if x["id"] != "bkT"]
log, dash, man = run()
check("missing-file warning", any("expected file not found" in w and "Bike CPS Triggered LD LMS Status'" in w for w in dash["source_warnings"]), dash["source_warnings"])
LISTING.append({"id":"bkT","name":"Bike CPS Triggered LD LMS Status","modifiedTime":"2026-10-06T08:00:00Z"})
log, dash, man = run()   # file is back -> nothing else changes

# ============ RUN 11: a rolling file is mid-rewrite when the run starts =======
print("\n== RUN 11: TVS T-1 is being rewritten (modified 60s ago, partial) ==")
full = mk_rows("tvsT1","TVS",M,1500, start=80_000_000)
CONTENT["tvsT1"] = full[:500]; MT["tvsT1"] = iso(clock.t - _dt.timedelta(seconds=60))
def finish_write():
    CONTENT["tvsT1"] = full; MT["tvsT1"] = iso(clock.t)
clock.at(150, finish_write)
t0 = clock.t
log, dash, man = run()
check("run waited for the writer instead of capturing the partial file: TVS Sep = 1400 (T-2 copy) + 1500 (T-1, complete) = 2900, not 1900", total(dash,"TVS",M) == 2900, total(dash,"TVS",M))
check("tvsT1 was read exactly once, after it went quiet", READS.count("tvsT1") == 1, READS)
check("it did wait (clock advanced > 5 min)", (clock.t - t0).total_seconds() > 300, (clock.t - t0))
check("no 'still being modified' warning", not any("still being modified" in w for w in dash["source_warnings"]), dash["source_warnings"])
check("roster says settled", [r for r in dash["source_files"] if r["brand"]=="TVS" and r["role"]=="T-1"][0]["settled"] is True)

# ============ RUN 12: a file that never goes quiet ===========================
print("\n== RUN 12: file keeps being modified (never settles) ==")
MT["tvsT1"] = iso(clock.t - _dt.timedelta(seconds=10))
def keep_writing():
    MT["tvsT1"] = iso(clock.t); clock.at(100, keep_writing)
clock.at(50, keep_writing)
t0 = clock.t
log, dash, man = run()
waited = (clock.t - t0).total_seconds()
check("wait is capped by the run budget (<= ~31 min), not forever", waited <= rd.SETTLE_BUDGET + 120, waited)
check("run still completed and reads the file", "tvsT1" in READS, READS)
check("warning: figures may be partial", any("still being modified" in w and "Previous Month" in w for w in dash["source_warnings"]), dash["source_warnings"])
check("roster says NOT settled", [r for r in dash["source_files"] if r["brand"]=="TVS" and r["role"]=="T-1"][0]["settled"] is False)
clock.events.clear(); MT["tvsT1"] = iso(clock.t - _dt.timedelta(hours=2))

# ============ RUN 13: a write lands DURING the read (torn read) ==============
print("\n== RUN 13: file is modified while being read ==")
v2 = mk_rows("bkT2","Bajaj",A,950, start=90_000_000)
CONTENT["bkT2"] = mk_rows("bkT2","Bajaj",A,900, start=91_000_000)
state = {"torn": False}
orig = rd.export_via_sheets_api
def torn_read(svc, fid, name):
    out = orig(svc, fid, name)
    if fid == "bkT2" and not state["torn"]:
        state["torn"] = True
        CONTENT["bkT2"] = v2; MT["bkT2"] = iso(clock.t)      # writer lands mid-read
    return out
rd.export_via_sheets_api = torn_read
log, dash, man = run()
check("torn read was detected and the file read again", READS.count("bkT2") == 2, READS)
check("final figure is the post-write content: Bajaj Aug = 950", total(dash,"Bajaj",A) == 950, total(dash,"Bajaj",A))
rd.export_via_sheets_api = orig; MT["bkT2"] = iso(clock.t - _dt.timedelta(hours=2))

# ============ RUN 14: processing order ======================================
print("\n== RUN 14: most-recently-modified files are read last ==")
MT["tvsT"] = iso(clock.t - _dt.timedelta(minutes=30)); MT["bkT"] = iso(clock.t - _dt.timedelta(minutes=40))
MT["tvsT1"] = iso(clock.t - _dt.timedelta(minutes=20)); MT["bkT1"] = iso(clock.t - _dt.timedelta(minutes=50))
MT["tvsT2new"] = iso(clock.t - _dt.timedelta(minutes=60)); MT["bkT2"] = iso(clock.t - _dt.timedelta(minutes=70))
log, dash, man = run()
order = [fid for fid in READS]
exp = sorted(order, key=lambda f: MT[f])
check("reads happen oldest-modified first", order == exp, order)

# ============ RUN 15: clock skew — file "modified" in the future =============
print("\n== RUN 15: modifiedTime ahead of the runner's clock ==")
MT["tvsT"] = iso(clock.t + _dt.timedelta(days=1))
t0 = clock.t
log, dash, man = run()
check("did not burn the wait budget on skew", (clock.t - t0).total_seconds() < 60, (clock.t - t0))
check("skew noted in the log and file still read", "ahead of this machine's clock" in log and "tvsT" in READS)
check("no spurious 'partial snapshot' warning", not any("partial snapshot" in w for w in dash["source_warnings"]), dash["source_warnings"])


# ======================================================================================
# Late-change sweep + in-run retry
# ======================================================================================
def quiet_all(hours=2):
    """Every file untouched for `hours` -> nothing to wait for."""
    clock.events.clear()
    for x in LISTING: MT[x["id"]] = iso(clock.t - _dt.timedelta(hours=hours))

# ============ RUN 16: the source job rewrites a file AFTER we read it, mid-run ======
print("\n== RUN 16: file is completed by the source job after we read it but before the run ends ==")
quiet_all()
log, dash, man = run()                                    # stable baseline
base_sep = total(dash, "TVS", M)
full_t2 = list(CONTENT["tvsT2new"])                       # 1400 Sep leads
CONTENT["tvsT2new"] = full_t2[:900]                       # source left it truncated (stable for hours)
MT["bkT2"] = iso(clock.t - _dt.timedelta(minutes=20))     # newest mtime -> read LAST in the main pass
state16 = {"fired": False}
orig16 = rd.export_via_sheets_api
def writer_lands_late(svc, fid, name):
    out = orig16(svc, fid, name)
    if fid == "bkT2" and not state16["fired"]:
        state16["fired"] = True
        CONTENT["tvsT2new"] = full_t2; MT["tvsT2new"] = iso(clock.t)      # job finishes the write
    return out
rd.export_via_sheets_api = writer_lands_late
log, dash, man = run()
rd.export_via_sheets_api = orig16
check("first read saw the truncated file (shrink warning printed in the log)", "shrank from 1,400 to 900" in log)
check("the file was read a second time, in the same run", READS.count("tvsT2new") == 2, READS)
check("final figure = finished file (TVS Sep back to baseline)", total(dash, "TVS", M) == base_sep, (total(dash, "TVS", M), base_sep))
check("stale 'shrank' warning from the superseded read is gone from the header", not any("shrank" in w for w in dash["source_warnings"]), dash["source_warnings"])
check("manifest holds the post-write modifiedTime and 1,400 rows", man["processed"]["tvsT2new"]["modifiedTime"] == MT["tvsT2new"] and man["processed"]["tvsT2new"]["rows"] == 1400, man["processed"]["tvsT2new"])
check("log says why it was read again", "after this run had read it" in log)
check("only that one file was re-read (no blanket second pass)", sum(READS.count(f) for f in ("tvsT","tvsT1","bkT","bkT1","bkT2")) == 5, READS)

# ============ RUN 17: a transient read failure is retried inside the same run =======
print("\n== RUN 17: a read fails once (Google 5xx), the retry later in the run succeeds ==")
quiet_all()
CONTENT["bkT1"] = mk_rows("bkT1", "Bajaj", M, 1200, start=70_000_000)
log, dash, man = run()
bk_sep = total(dash, "Bajaj", M)
state17 = {"n": 0}
orig17 = rd.export_via_sheets_api
def flaky(svc, fid, name):
    if fid == "bkT1" and state17["n"] == 0:
        state17["n"] += 1; READS.append(fid)
        raise RuntimeError("Sheets API 503")
    return orig17(svc, fid, name)
rd.export_via_sheets_api = flaky
log, dash, man = run()
rd.export_via_sheets_api = orig17
check("failed file was read again in the same run", READS.count("bkT1") == 2, READS)
check("data intact: Bajaj Sep unchanged", total(dash, "Bajaj", M) == bk_sep, (total(dash, "Bajaj", M), bk_sep))
check("no leftover 'could not be read' warning", dash["source_warnings"] == [], dash["source_warnings"])
check("roster says ok", [r for r in dash["source_files"] if r["brand"] == "Bike" and r["role"] == "T-1"][0]["ok"] is True)
check("log shows the retry reason", "trying again" in log)

print("\n== RUN 17b: a file that keeps failing is retried a bounded number of times ==")
quiet_all()
FAIL["bkT1"] = RuntimeError("Sheets API 500")
log, dash, man = run()
check("attempts bounded: 1 + MAX_SWEEPS", READS.count("bkT1") == 1 + rd.MAX_SWEEPS, READS)
check("exactly one warning for it in the header (not one per attempt)", sum("bkT1" in w or "Bike CPS Triggered LD LMS Status - Previous Month" in w for w in dash["source_warnings"]) == 1, dash["source_warnings"])
check("old rows kept: Bajaj Sep unchanged", total(dash, "Bajaj", M) == bk_sep, (total(dash, "Bajaj", M), bk_sep))
del FAIL["bkT1"]

print("\n== RUN 17c: empty read of a previously-full file is retried, and the finished file is picked up ==")
quiet_all()
good = CONTENT["bkT1"]
CONTENT["bkT1"] = []
state17c = {"n": 0}
orig17c = rd.export_via_sheets_api
def empty_then_full(svc, fid, name):
    out = orig17c(svc, fid, name)
    if fid == "bkT1":
        state17c["n"] += 1
        if state17c["n"] == 1:
            CONTENT["bkT1"] = good           # source job finishes writing before the retry
    return out
rd.export_via_sheets_api = empty_then_full
log, dash, man = run()
rd.export_via_sheets_api = orig17c
check("retried after the empty read", READS.count("bkT1") == 2, READS)
check("Bajaj Sep = finished content", total(dash, "Bajaj", M) == bk_sep, (total(dash, "Bajaj", M), bk_sep))
check("no warning left", dash["source_warnings"] == [], dash["source_warnings"])

# ============ RUN 18: a file that never stops changing can't loop the run ===========
print("\n== RUN 18: file modified again after EVERY read (never stable) ==")
quiet_all()
orig18 = rd.export_via_sheets_api
def always_touched(svc, fid, name):
    out = orig18(svc, fid, name)
    if fid == "tvsT1":
        MT[fid] = iso(clock.t + _dt.timedelta(seconds=1))      # a writer lands during every read
    return out
rd.export_via_sheets_api = always_touched
t0 = clock.t
log, dash, man = run()
rd.export_via_sheets_api = orig18
n18 = READS.count("tvsT1")
check(f"reads bounded (<= 3 per pass x (1 + MAX_SWEEPS) = {3 * (1 + rd.MAX_SWEEPS)}), got {n18}", n18 <= 3 * (1 + rd.MAX_SWEEPS), READS)
check("run finished within the wait budget", (clock.t - t0).total_seconds() <= rd.SETTLE_BUDGET + 120, clock.t - t0)
check("flagged as a possibly-partial snapshot", any("still being modified" in w for w in dash["source_warnings"]), dash["source_warnings"])
quiet_all()

# ============ RUN 19: a file disappears from the folder while the run is going ======
print("\n== RUN 19: temporary copy read early in the run is deleted by the source job before it ends ==")
quiet_all()
log, dash, man = run()
aug_before = total(dash, "TVS", A)
LISTING.append({"id": "tmpX", "name": f"{B} - Copy of Previous to Previous Month", "modifiedTime": iso(clock.t - _dt.timedelta(hours=3))})
CONTENT["tmpX"] = mk_rows("tmpX", "TVS", A, 300, start=95_000_000)
MT["bkT2"] = iso(clock.t - _dt.timedelta(minutes=20))
state19 = {"fired": False}
orig19 = rd.export_via_sheets_api
def delete_copy_late(svc, fid, name):
    out = orig19(svc, fid, name)
    if fid == "bkT2" and not state19["fired"]:
        state19["fired"] = True
        LISTING[:] = [x for x in LISTING if x["id"] != "tmpX"]
    return out
rd.export_via_sheets_api = delete_copy_late
log, dash, man = run()
rd.export_via_sheets_api = orig19
check("the copy WAS read (it existed at the start)", "tmpX" in READS, READS)
check("its 300 leads are not in the dashboard (it was gone by the end)", total(dash, "TVS", A) == aug_before, (total(dash, "TVS", A), aug_before))
check("it is not tracked in the manifest", "tmpX" not in man["processed"])
quiet_all()

# ============ RUN 20: the second folder listing misbehaves ===========================
print("\n== RUN 20: the second Drive listing fails / comes back empty ==")
quiet_all()
log, dash, man = run()
tot20 = total(dash)
orig_list = rd.list_folder_sheets
calls = {"n": 0}
def second_call_raises(svc):
    calls["n"] += 1
    if calls["n"] == 2: raise RuntimeError("Drive 503")
    return orig_list(svc)
rd.list_folder_sheets = second_call_raises
log, dash, man = run()
check("run completes; first-pass data kept", total(dash) == tot20, (total(dash), tot20))
check("warning says the late-change check couldn't run", any("re-list the Drive folder" in w for w in dash["source_warnings"]), dash["source_warnings"])
calls["n"] = 0
def second_call_empty(svc):
    calls["n"] += 1
    return [] if calls["n"] == 2 else orig_list(svc)
rd.list_folder_sheets = second_call_empty
log, dash, man = run()
check("empty second listing ignored (no purge): total unchanged", total(dash) == tot20, (total(dash), tot20))
check("warning raised about the vanished files", any("ignoring the second listing" in w for w in dash["source_warnings"]), dash["source_warnings"])
check("roster still lists all six rolling files", len(dash["source_files"]) == 6, dash["source_files"])
rd.list_folder_sheets = orig_list

# ============ RUN 21: nothing changes during a normal run -> no extra reads =========
print("\n== RUN 21: a normal run costs no extra reads ==")
quiet_all()
log, dash, man = run()
check("each rolling file read exactly once", sorted(READS) == sorted(["tvsT","tvsT1","tvsT2new","bkT","bkT1","bkT2"]), READS)
check("sweep reported nothing changed", "nothing changed while this run was in progress" in log)
check("clean header", dash["source_warnings"] == [], dash["source_warnings"])

# ============ RUN 22: brand-new file appears mid-run ================================
print("\n== RUN 22: a new dated file lands in the folder during the run ==")
quiet_all()
before = total(dash)
state22 = {"fired": False}
orig22 = rd.export_via_sheets_api
def new_file_appears(svc, fid, name):
    out = orig22(svc, fid, name)
    if fid == "bkT2" and not state22["fired"]:
        state22["fired"] = True
        LISTING.append({"id": "sAug", "name": f"{B} - Aug'26 (archive)", "modifiedTime": iso(clock.t)})
        CONTENT["sAug"] = mk_rows("sAug", "TVS", "Aug'2026", 50, start=96_000_000)
    return out
MT["bkT2"] = iso(clock.t - _dt.timedelta(minutes=20))
rd.export_via_sheets_api = new_file_appears
log, dash, man = run()
rd.export_via_sheets_api = orig22
check("the new file was picked up in the same run", "sAug" in READS, READS)
check("its 50 leads are counted", total(dash) == before + 50, (total(dash), before))
check("log explains it", "new in the folder since this run started" in log)


# ============ RUN 23: big files skip the ZIP export that would only fail slowly ======
print("\n== RUN 23: a file whose last read was big goes straight to the Sheets API ==")
quiet_all()
big = mk_rows("bkT1", "Bajaj", M, rd.ZIP_SKIP_ROWS + 1000, start=100_000_000)
CONTENT["bkT1"] = big
log, dash, man = run()                                   # previous read was small -> ZIP still tried first
check("first sight of the grown file still tries ZIP first (its last read was small)", "bkT1" in ZIP_CALLS, ZIP_CALLS)
check("manifest now records the big row count", man["processed"]["bkT1"]["rows"] == len(big), man["processed"]["bkT1"])
sep_big = total(dash, "Bajaj", M)
log, dash, man = run()                                   # previous read was big -> skip ZIP
check("ZIP export NOT attempted for the big file", "bkT1" not in ZIP_CALLS, ZIP_CALLS)
check("...but still attempted for the small ones", {"tvsT", "tvsT1", "bkT", "bkT2"} <= set(ZIP_CALLS), ZIP_CALLS)
check("big file read exactly once, via the Sheets API", READS.count("bkT1") == 1, READS)
check("figures unchanged by the different read path", total(dash, "Bajaj", M) == sep_big, (total(dash, "Bajaj", M), sep_big))
before = len(ZIP_CALLS)
rd.read_source_file(FakeDrive(), object(), "bkT1", "x", rd.ZIP_SKIP_ROWS - 1)
check("boundary: one row under the threshold -> ZIP tried", len(ZIP_CALLS) == before + 1)
rd.read_source_file(FakeDrive(), object(), "bkT1", "x", rd.ZIP_SKIP_ROWS)
check("boundary: exactly at the threshold -> ZIP skipped", len(ZIP_CALLS) == before + 1)
CONTENT["bkT1"] = mk_rows("bkT1", "Bajaj", M, 1200, start=70_000_000)


# ============ RUN 24: the Ask vs Actual cube in the dashboard data =================
print("\n== RUN 24: ask_cube (model x state x medium x lead month x lead date) ==")
from collections import Counter as _C
def _cube_rows():
    out = []
    def add(brand, model, state, medium, month, date, k):
        for _ in range(k):
            n = len(out)
            out.append({"opty_id": f"c{n:06d}", "encrypt_mobile_number": f"cm{n}", "brand": brand, "model": model,
                        "State": state, "Medium": medium, "Lead_Month": month, "Date": date, "City": "Pune"})
    add("TVS", "TVS Raider",  "Maharashtra", "Google",   "Oct'2026", "2026-10-01", 3)
    add("TVS", "TVS Raider",  "Maharashtra", "Google",   "Oct'2026", "2026-10-02", 2)
    add("TVS", "TVS Raider",  "Bihar",       "Facebook", "Oct'2026", "2026-10-01", 4)
    add("TVS", "TVS iQube S", "Maharashtra", "Organic",  "Oct'2026", "2026-Oct-03", 5)      # the other date format
    add("TVS", "TVS iQube S", "Maharashtra", "Organic",  "Oct'2026", "",            2)      # no parseable date
    add("TVS", "TVS Raider",  "Maharashtra", "Google",   "Oct'2026", "2026-09-30",  1)      # Oct lead dated Sep 30
    add("Hero","Hero Glamour","",            "Non-MS",   "Sep'2026", "2026-09-15",  6)      # blank state -> Unknown
    add("Hero","Hero Glamour","Bihar",       "Whatsapp", "Aug'2026", "2026-08-20",  7)
    add("Hero","Hero Glamour","Bihar",       "Whatsapp", "Jan'2026", "2026-01-20",  9)      # outside a 3-month cube
    return out
old_months = rd.ASK_CUBE_MONTHS
rd.ASK_CUBE_MONTHS = 3
dash24 = rd.build_aggregations(_cube_rows())
cube = dash24["ask_cube"]
rd.ASK_CUBE_MONTHS = old_months
def _decode_cube(cube):
    """rows = [model, state, medium, month, k, (date delta, leads) x k, ...] per group."""
    R, out, i = cube["rows"], _C(), 0
    while i < len(R):
        m, s_, e, mo, k = R[i:i+5]; i += 5; d = 0
        for _ in range(k):
            d += R[i]
            out[(cube["models"][m], cube["states"][s_], cube["mediums"][e], cube["months"][mo], cube["dates"][d])] += R[i+1]
            i += 2
    return out, i == len(R)
dec, consumed_exactly = _decode_cube(cube)
exp = _C({
    ("TVS Raider", "Maharashtra", "Google", "Oct'2026", "2026-10-01"): 3,
    ("TVS Raider", "Maharashtra", "Google", "Oct'2026", "2026-10-02"): 2,
    ("TVS Raider", "Bihar", "Facebook", "Oct'2026", "2026-10-01"): 4,
    ("TVS iQube S", "Maharashtra", "Organic", "Oct'2026", "2026-10-03"): 5,
    ("TVS iQube S", "Maharashtra", "Organic", "Oct'2026", ""): 2,
    ("TVS Raider", "Maharashtra", "Google", "Oct'2026", "2026-09-30"): 1,
    ("Hero Glamour", "Unknown", "Non-MS", "Sep'2026", "2026-09-15"): 6,
    ("Hero Glamour", "Bihar", "Whatsapp", "Aug'2026", "2026-08-20"): 7,
})
check("cube decodes to exactly the expected (model, state, medium, month, date) counts", dec == exp, (dec - exp, exp - dec))
check("the grouped list is well-formed (decoding consumes it exactly)", consumed_exactly)
check("only the most recent ASK_CUBE_MONTHS months are kept (Jan'2026 excluded)", cube["months"] == ["Aug'2026", "Sep'2026", "Oct'2026"], cube["months"])
check("rows with no parseable date are kept under date '' (index 0), so month totals reconcile", cube["dates"][0] == "" and dec[("TVS iQube S", "Maharashtra", "Organic", "Oct'2026", "")] == 2)
check("cube month totals equal the dashboard's month totals", all(sum(n for k, n in dec.items() if k[3] == m) == dash24["by_month"][m] for m in cube["months"]))
check("an Oct lead dated Sep 30 stays in the Oct month cell (month = Lead_Month, day = Date)", dec[("TVS Raider", "Maharashtra", "Google", "Oct'2026", "2026-09-30")] == 1)
def _all_deltas_nonneg(cube):
    R, i = cube["rows"], 0
    while i < len(R):
        k = R[i+4]; i += 5
        for _ in range(k):
            if R[i] < 0 or R[i+1] <= 0: return False
            i += 2
    return True
check("every date delta is >= 0 and every cell has leads > 0 (dates ascend within a group)", _all_deltas_nonneg(cube) and cube["dates"] == sorted(cube["dates"]))
check("no leads at all -> ask_cube is null, not a crash", rd.build_aggregations([])["ask_cube"] is None)

print(f"\n{'='*60}\n{PASS} passed, {FAIL_N} failed")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL_N else 0)
