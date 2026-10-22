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
_real_load_ask_data = rd.load_ask_data     # main() is driven with a stub below; RUN 27 tests the real one

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


# ============ RUN 25: Jawa counts = unique phone numbers per month =================
print("\n== RUN 25: unique_phone_per_month (Jawa = Jawa + Yezdi + BSA, one lead per phone per month) ==")
def _jw(opty, mob, month, date, model="Jawa 42", brand="Jawa", **kw):
    r = {"opty_id": opty, "encrypt_mobile_number": mob, "brand": brand, "model": model, "Lead_Month": month, "Date": date,
         "Medium": "Organic", "State": "Delhi", "City": "Delhi"}
    r.update(kw); return r
base = [
    _jw("o1", "pA", "Oct'2026", "2026-10-05", "Jawa 42", Medium="Google"),
    _jw("o2", "pA", "Oct'2026", "2026-10-02", "Yezdi Roadster", Medium="Facebook"),     # same person, other sub-brand, EARLIER
    _jw("o3", "pA", "Oct'2026", "2026-10-09", "BSA Gold Star"),                          # ...and a third enquiry
    _jw("o4", "pA", "Sep'2026", "2026-09-30", "Jawa 42"),                                # same person, previous month -> separate lead
    _jw("o5", "pB", "Oct'2026", "2026-10-03"),
    _jw("o6", "", "Oct'2026", "2026-10-03"), _jw("o7", "", "Oct'2026", "2026-10-03"),    # no phone: nothing to match on, both stay
    _jw("t1", "pA", "Oct'2026", "2026-10-02", "TVS Raider", brand="TVS"),                # other brands untouched even if same phone
    _jw("t2", "pA", "Oct'2026", "2026-10-02", "TVS Raider", brand="TVS"),
]
st = {}
kept = rd.unique_phone_per_month(base, stats=st)
ids = sorted(r["opty_id"] for r in kept)
check("one Jawa lead per phone per month: pA keeps only the earliest Oct enquiry (o2, a Yezdi)", [i for i in ids if i in ("o1", "o2", "o3")] == ["o2"], ids)
check("the same phone in another month is a separate lead (o4 kept)", "o4" in ids)
check("different phone (pB) kept", "o5" in ids)
check("rows with no phone number all pass through", "o6" in ids and "o7" in ids)
check("other brands are untouched, duplicates included", "t1" in ids and "t2" in ids)
check("exactly 2 repeat-phone leads left out, both in Oct'2026", sum(st["by_month"].values()) == 2 and st["by_month"] == {"Oct'2026": 2}, st)
check("the input list is not modified (the cache keeps every distinct lead)", len(base) == 9 and [r["opty_id"] for r in base][:3] == ["o1", "o2", "o3"])
import random as _rnd
shuffled_ok = True
for seed in range(20):
    b2 = base[:]; _rnd.Random(seed).shuffle(b2)
    shuffled_ok &= sorted(r["opty_id"] for r in rd.unique_phone_per_month(b2)) == ids
check("which row survives does not depend on input order (20 shuffles)", shuffled_ok)
tie = rd.unique_phone_per_month([_jw("o9", "pC", "Oct'2026", "2026-10-04"), _jw("o8", "pC", "Oct'2026", "2026-10-04")])
check("same date -> lowest opty_id wins", [r["opty_id"] for r in tie] == ["o8"], tie)
nd = rd.unique_phone_per_month([_jw("o1", "pD", "Oct'2026", ""), _jw("o2", "pD", "Oct'2026", "2026-Oct-07")])
check("a parseable date beats a missing one (and '2026-Oct-07' style dates parse)", [r["opty_id"] for r in nd] == ["o2"], nd)
check("a 'Mon' style date sorts correctly against ISO dates", [r["opty_id"] for r in rd.unique_phone_per_month([_jw("a", "pE", "Oct'2026", "2026-10-09"), _jw("b", "pE", "Oct'2026", "2026-Oct-03")])] == ["b"])
check("brands= overrides the default set (TVS dedup on request: its 2 same-phone rows collapse to 1, Jawa untouched)", len(rd.unique_phone_per_month(base, brands={"TVS"})) == 8, len(rd.unique_phone_per_month(base, brands={"TVS"})))
check("empty input", rd.unique_phone_per_month([]) == [])
# end to end through the aggregation: Jawa total counts people, not enquiries
d25 = rd.build_aggregations(kept)
check("dashboard Jawa total = unique phone-months (pA Oct, pA Sep, pB Oct, 2 no-phone rows) = 5", d25["by_brand"]["Jawa"] == 5, d25["by_brand"])
check("...and TVS keeps both of its repeat-phone rows", d25["by_brand"]["TVS"] == 2)

# ============ RUN 26: the lead-level fact table (every tab's filters count from it) =============
print("\n== RUN 26: fact table (one row per counted lead, run-length coded + deflated) ==")
import random as _r26, zlib as _z26, base64 as _b26
from datetime import datetime as _dt26, timedelta as _td26
def _fx_rows():
    out = []
    def add(brand, model, state, city, crm, medium, lt, month, date, k, **kw):
        for _ in range(k):
            n = len(out)
            r = {"opty_id": f"f{n:06d}", "encrypt_mobile_number": f"fm{n}", "brand": brand, "model": model, "State": state,
                 "City": city, "oem_crm_id": crm, "Medium": medium, "lead_type": lt, "Lead_Month": month, "Date": date}
            r.update(kw); out.append(r)
    add("TVS",  "TVS Raider",  "Maharashtra", "Pune",   "C100", "Google",   "1105", "Oct'2026", "2026-10-01", 3)
    add("TVS",  "TVS Raider",  "Maharashtra", "Pune",   "C100", "Google",   "1105", "Oct'2026", "2026-10-02", 2)
    add("TVS",  "TVS Raider",  "Maharashtra", "Pune",   "C101", "Facebook", "103",  "Oct'2026", "2026-Oct-03", 4)   # other date format
    add("TVS",  "tvs raider",  "Maharashtra", "Pune",   "C101", "Facebook", "103",  "Oct'2026", "",            1)   # no date, model spelt differently
    add("TVS",  "TVS iQube S", "Bihar",       "Patna",  "",     "Organic",  "70",   "Sep'2026", "2026-09-30",  5)   # no CRM id -> dealer Unknown
    add("Hero", "Hero Glamour","",            "",       "H1",   "Non-MS",   "",     "Sep'2026", "2026-09-15",  6)   # blank state/city/lead type
    add("Ampere","Ampere Magnus","Delhi",     "Delhi",  "A1",   "Whatsapp", "1105", "Aug'2026", "2024-01-01",  2)   # date before the epoch
    add("Ampere","Ampere Magnus","Delhi",     "Delhi",  "A1",   "Whatsapp", "1105", "Aug'2026", "2040-01-01",  1)   # date far beyond the 12-bit range
    add("Hero", "Hero Glamour","Bihar",       "Patna",  "H2",   "Organic",  "70",   "Mar'2025", "2025-03-10",  4)   # SKIP_MONTHS: not counted
    return out
d26 = rd.build_aggregations(_fx_rows())
f26 = d26["fact"]
check("blob carries the fact table", isinstance(f26, dict) and f26["v"] == rd.FACT_FORMAT and f26["n"] == d26["total"], f26.get("n"))
check("payload is zlib-deflated and starts with the magic bytes", _z26.decompress(_b26.b64decode(f26["data"]))[:2] == b"F" + bytes([rd.FACT_FORMAT]))
dec26 = rd.decode_fact(f26)
want26 = _C()
for r in _fx_rows():
    if r["Lead_Month"] == "Mar'2025":
        continue
    st_ = r["State"] or "Unknown"; ci_ = r["City"] or "Unknown"
    dl_ = f'{r["oem_crm_id"]} \u00b7 {r["City"]}, {r["State"]}' if r["oem_crm_id"] else "Unknown"
    mo_ = "TVS Raider" if r["model"].lower() == "tvs raider" else r["model"]
    bu_ = {"TVS": "TVS", "Hero": "Hero", "Ampere": "Ampere Electric"}[r["brand"]]
    br_ = {"TVS": "TVS", "Hero": "Hero", "Ampere": "Ampere Electric"}[r["brand"]]
    dt_ = rd.parse_lead_date(r["Date"]) if hasattr(rd, "parse_lead_date") else None
    if not dt_:
        for fmt in ("%Y-%m-%d", "%Y-%b-%d"):
            try: dt_ = _dt26.strptime(r["Date"], fmt); break
            except ValueError: pass
    iso_ = dt_.strftime("%Y-%m-%d") if dt_ else ""
    if dt_ and (dt_ < rd.FACT_DATE_EPOCH or (dt_ - rd.FACT_DATE_EPOCH).days + 1 >= 1 << rd.FACT_DATE_BITS):
        iso_ = ""                                                       # unusable date -> stored as "no date"
    want26[(st_, ci_, dl_, (br_, bu_, mo_), r["Lead_Month"], r["Medium"], r["lead_type"] or "Unknown", iso_)] += 1
check("decodes to exactly the counted leads (state, city, dealer, brand/BU/model, month, source, lead type, date)", _C(dec26) == want26, (_C(dec26) - want26, want26 - _C(dec26)))
check("model spellings are folded to one canonical name, like every other aggregate", {p[2] for p in f26["products"] if p[0] == "TVS"} == {"TVS Raider", "TVS iQube S"}, f26["products"])
check("both date formats parse; missing dates are stored as none", any(r[-1] == "2026-10-03" for r in dec26) and any(r[-1] == "" for r in dec26))
check("dates outside the representable range are counted as unusable, not crashed on", sum(1 for r in dec26 if r[1] == "Delhi" and r[-1] == "") == 3)
check("rows from skipped months are not in the table", all(r[4] != "Mar'2025" for r in dec26) and f26["n"] == len(dec26))
tot26 = {"brand": d26["by_brand"], "bu": d26["by_bu"], "model": d26["by_model"], "state": d26["by_state"], "city": d26["by_city"],
         "dealer": dict(_C(r[2] for r in dec26)), "medium": d26["by_medium"], "month": d26["by_month"], "lt": d26["by_lt"]}
check("fact_check: every 1-D aggregate reconciles with the table", rd.fact_check(f26, tot26) == [], rd.fact_check(f26, tot26))
bad26 = {k: dict(v) for k, v in tot26.items()}; bad26["medium"]["Google"] += 1
check("fact_check notices a single-lead discrepancy", len(rd.fact_check(f26, bad26)) == 1)
e26 = rd.build_aggregations([])["fact"]
check("no leads -> an empty but valid table", e26["n"] == 0 and rd.decode_fact(e26) == [])
# a bigger random round trip, including repeated dealers across cities and every run-length edge (len 1, long runs)
rnd = _r26.Random(26)
T = rd.FactTable(); exp26 = _C()
sts_ = [f"S{i}" for i in range(9)]; cts_ = [f"C{i}" for i in range(60)]; dls_ = [f"{i} \u00b7 C{i % 60}, S{(i % 60) % 9}" for i in range(500)] + ["Unknown"]
prs_ = [(f"B{i % 3}", f"U{i % 4}", f"M{i}") for i in range(30)]
for _ in range(30000):
    d = rnd.choice(dls_); c = rnd.choice(cts_) if d == "Unknown" else d.split(" \u00b7 ")[1].split(",")[0]
    dt = None if rnd.random() < .05 else _dt26(2025, 4, 1) + _td26(days=rnd.randint(0, 600))
    row = (rnd.choice(sts_), c, d, rnd.choice(prs_), rnd.choice(["Aug'2026", "Sep'2026", "Oct'2026"]), rnd.choice(["Organic", "Google", "Facebook"]), rnd.choice(["1105", "70", "103"]), dt)
    T.add(*row); exp26[row[:7] + (dt.strftime("%Y-%m-%d") if dt else "",)] += 1
check("30,000 random leads round-trip exactly", _C(rd.decode_fact(T.encode())) == exp26)
# a column drifting against another leaves every 1-D total intact; the joint check against the Ask cube must see it
_orig_add = rd.FactTable.add
_seen = {"a": False, "b": False}
def _swap_states(self, state, city, dealer, product, month, medium, lt, lead_dt):
    if state == "Maharashtra" and medium == "Google" and not _seen["a"]:
        _seen["a"] = True; state = "Bihar"
    elif state == "Bihar" and medium == "Organic" and not _seen["b"]:
        _seen["b"] = True; state = "Maharashtra"
    return _orig_add(self, state, city, dealer, product, month, medium, lt, lead_dt)
rd.FactTable.add = _swap_states
try:
    d26t = rd.build_aggregations(_fx_rows())
finally:
    rd.FactTable.add = _orig_add
_probs = d26t["fact"].get("problems", [])
check("two leads swapping states (1-D totals unchanged) are caught by the joint check against the Ask cube",
      len(_probs) == 1 and "model x state x source x month" in _probs[0], _probs)
check("a clean build reports no problems", "problems" not in d26["fact"])
big = rd.FactTable()
try:
    for i in range(1 << rd.FACT_BITS["state"]): big.add(f"S{i}", "c", "d", ("b", "u", "m"), "m", "x", "1", None)
    big.add("one too many", "c", "d", ("b", "u", "m"), "m", "x", "1", None)
    overflow_ok = False
except ValueError as e:
    overflow_ok = "widen FACT_BITS" in str(e)
check("a dictionary outgrowing its bit width fails loudly instead of corrupting the table", overflow_ok)

# ============ RUN 27: Consol Ask sheet names are mapped onto the lead data's model names =============
print("\n== RUN 27: load_ask_data (Ask sheet model names -> lead-data model names) ==")
class _FakeSheets:
    """Just enough of the Sheets client for spreadsheets().values().get(...).execute()."""
    def __init__(self, rows): self.rows = rows
    def spreadsheets(self): return self
    def values(self): return self
    def get(self, **kw): return self
    def execute(self): return {"values": self.rows}
_ask_rows = [
    ["Consol Ask"],
    [],
    ["OEM", "BU", "Model", "Oct'26", None, None, None, "Nov'26", None, None, None],
    [None, None, None, "Total Leads", "Organic", "Google", "FB", "Total Leads", "Organic", "Google", "FB"],
    ["TVS", "TVS", "Apache RTR 160 4V", 17969, 2300, 7000, 8669, 0, 0, 0, 0],            # no 'TVS ' prefix in the sheet
    [None, None, "Apache RTR 160", 30779, 6700, 10000, 14079, 20000, 4000, 6000, 10000],
    ["Jawa/Yezdi", "Jawa", "BSA GoldStar", 300, 60, 100, 140, 300, 60, 100, 140],
    ["Bajaj", "MC", "Bajaj Avenger Street 220", 300, 100, 100, 100, 300, 100, 100, 100],
    ["Hero", "Hero", "Hero Super Splendor XTEC", 1000, 200, 300, 500, 0, 0, 0, 0],
    [None, None, "Hero Super Splendor", 50, 10, 15, 25, 0, 0, 0, 0],                       # maps onto the XTEC row: summed, not overwritten
    ["OLA", "Ola Electric", "OLA S1 Pro", 700, 100, 200, 400, 0, 0, 0, 0],
]
ask27 = _real_load_ask_data(_FakeSheets(_ask_rows))
check("sheet 'Apache RTR 160 4V' becomes the lead data's 'TVS Apache RTR 160 4V' (and the unprefixed name is gone)",
      ask27["Oct'2026"]["TVS"].get("TVS Apache RTR 160 4V") == {"t": 17969, "o": 2300, "g": 7000, "f": 8669, "p": 0} and "Apache RTR 160 4V" not in ask27["Oct'2026"]["TVS"],
      ask27["Oct'2026"]["TVS"])
check("the existing Apache mappings still work", "TVS Apache RTR 160" in ask27["Oct'2026"]["TVS"] and ask27["Nov'2026"]["TVS"]["TVS Apache RTR 160"]["t"] == 20000)
check("'BSA GoldStar' -> 'BSA Gold Star' under brand Jawa", ask27["Oct'2026"]["Jawa"].get("BSA Gold Star", {}).get("t") == 300, ask27["Oct'2026"]["Jawa"])
check("'Bajaj Avenger Street 220' -> 'Bajaj Avenger 220 Street'", ask27["Oct'2026"]["Bajaj"].get("Bajaj Avenger 220 Street", {}).get("t") == 300, ask27["Oct'2026"]["Bajaj"])
check("a row with no target in a month is left out of that month (Nov has no Apache RTR 160 4V)", "TVS Apache RTR 160 4V" not in ask27["Nov'2026"].get("TVS", {}), ask27["Nov'2026"])
check("two sheet rows that land on one lead-data model are added up, not overwritten",
      ask27["Oct'2026"]["Hero"]["Hero Super Splendor XTEC"] == {"t": 1050, "o": 210, "g": 315, "f": 525, "p": 0}, ask27["Oct'2026"]["Hero"])
check("brand names are mapped too (OLA -> Ola Electric)", "Ola S1 Pro" in ask27["Oct'2026"]["Ola Electric"], list(ask27["Oct'2026"]))

# ============ RUN 28: BU of Triumph / KTM models the Model Mapping tab doesn't list =============
print("\n== RUN 28: BU assignment (Model Mapping tab first, then the sub-brand in the model name, then the brand) ==")
_map28 = {"triumph speed 400": "PB-TRM", "bajaj pulsar ns200": "MC", "bajaj wego p70": "CV", "triumph oddball": "MC"}
check("model in the tab -> the tab's BU ('PB-TRM' reads as TRM)", rd.resolve_bu("Triumph Speed 400", "Bajaj", _map28) == "TRM")
check("the tab wins over the sub-brand in the name", rd.resolve_bu("Triumph Oddball", "Bajaj", _map28) == "MC")
check("Triumph model missing from the tab -> TRM (the Tracker 400 case)", rd.resolve_bu("Triumph Tracker 400", "Bajaj", _map28) == "TRM")
check("KTM model missing from the tab -> PB", rd.resolve_bu("KTM RC 125", "Bajaj", _map28) == "PB")
check("other model missing from the tab -> the brand's BU (Bajaj -> MC, TVS -> TVS)",
      rd.resolve_bu("Bajaj WEGO P50", "Bajaj", _map28) == "MC" and rd.resolve_bu("TVS Raider", "TVS", _map28) == "TVS")
check("no Model Mapping at all (tab failed to load) still files Triumph under TRM",
      rd.resolve_bu("Triumph Tracker 400", "Bajaj", {}) == "TRM" and rd.resolve_bu("Triumph Tracker 400", "Bajaj", None) == "TRM")

def _bu_rows():
    out = []
    def add(brand, model, k):
        for _ in range(k):
            n = len(out)
            out.append({"opty_id": f"b{n:06d}", "encrypt_mobile_number": f"bm{n}", "brand": brand, "model": model, "State": "Maharashtra",
                        "City": "Pune", "oem_crm_id": "C1", "Medium": "Organic", "lead_type": "1105", "Lead_Month": "Oct'2026", "Date": "2026-10-03"})
    # rows as main() stores them: BRAND_ALIASES has already turned "Triumph" / "KTM" into "Bajaj"
    add("Bajaj", "Triumph Tracker 400", 3)
    add("Bajaj", "Triumph Speed 400", 2)
    add("Bajaj", "KTM RC 125", 1)
    add("Bajaj", "Bajaj Pulsar NS200", 4)
    add("Bajaj", "Bajaj WEGO P50", 2)
    add("TVS", "TVS Raider", 5)
    return out
d28 = rd.build_aggregations(_bu_rows(), model_to_bu=_map28)
check("by_bu: TRM = Tracker 400 + Speed 400 (5), PB = the KTM (1), MC = Pulsar + WEGO P50 (6)",
      (d28["by_bu"]["TRM"], d28["by_bu"]["PB"], d28["by_bu"]["MC"]) == (5, 1, 6), d28["by_bu"])
check("model_bu / bu_model say the Tracker 400 is TRM", d28["model_bu"]["Triumph Tracker 400"] == "TRM" and "Triumph Tracker 400" in d28["bu_model"]["TRM"] and "Triumph Tracker 400" not in d28["bu_model"].get("MC", {}), d28["model_bu"])
check("the Tracker 400 stays a Bajaj model", d28["bu_brand"]["TRM"] == {"Bajaj": 5} and d28["model_brand"]["Triumph Tracker 400"] == {"Bajaj": 3}, d28["bu_brand"])
_prods28 = _C(r[3] for r in rd.decode_fact(d28["fact"]))
check("lead table files every lead under its BU (this is what every tab filters on)",
      _prods28 == _C({("Bajaj", "TRM", "Triumph Tracker 400"): 3, ("Bajaj", "TRM", "Triumph Speed 400"): 2, ("Bajaj", "PB", "KTM RC 125"): 1,
                      ("Bajaj", "MC", "Bajaj Pulsar NS200"): 4, ("Bajaj", "MC", "Bajaj WEGO P50"): 2, ("TVS", "TVS", "TVS Raider"): 5}), dict(_prods28))
_unaliased = [dict(r, brand="Triumph") if r["model"].startswith("Triumph") else r for r in _bu_rows()]   # same rows, brand still "Triumph"
check("rows that still carry brand 'Triumph' land in the same place", _C(r[3] for r in rd.decode_fact(rd.build_aggregations(_unaliased, model_to_bu=_map28)["fact"])) == _prods28)

print(f"\n{'='*60}\n{PASS} passed, {FAIL_N} failed")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL_N else 0)
