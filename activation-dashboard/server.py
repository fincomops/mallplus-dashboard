#!/usr/bin/env python3
"""MallPlus Alpha 1 Activation Dashboard — Data Server
Fetches activation + test scenario data from Google Sheets.
Port: 8081
"""

import json, csv, io, os, time, urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────
PORT       = 8081
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_TTL  = 60  # seconds

# MallPlus Alpha Tracker (progress + activation tasks)
MAIN_SHEET_ID = "10czUOytN3KB6mybGWQzyB_v6Zm2tqoITGgcOAJhZzKk"
GID_PROGRESS  = "1809860840"
GID_ACTIVATION= "2059087416"

# Alpha 1 Live Tracker (UAT features)
LIVE_SHEET_ID = "1BsX1v_QCWmVe8JUY2T8sRBKzNOq6wrd4yT4I9Oegqc8"
GID_UAT       = "1390827425"

# Reference sheet — Test Scenarios (private, uses Sheets API)
REF_SHEET_ID      = "1xaa1hIsSMGrpDd-S71NgMi4VxGu0ljIC722kA4WclQM"
GOOGLE_TOKEN_FILE = os.path.join(SCRIPT_DIR, "..", "secrets", "google-token.json")

def csv_url(sid, gid):
    return f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"

# ── Cache ────────────────────────────────────────────────────────────────
_cache = {"data": None, "ts": 0.0}

# ── Helpers ──────────────────────────────────────────────────────────────
def _int(v):
    try:
        s = str(v).strip().replace(",", "")
        return int(float(s)) if s.lstrip("-").replace(".","",1).isdigit() else 0
    except: return 0

def _float(v):
    try: return float(str(v).strip().replace("%","").replace(",","."))
    except: return 0.0

def _get(row, idx, default=""):
    return row[idx].strip() if idx < len(row) else default

def fetch_csv(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")

def fetch_sheet_values(sheet_id, range_name):
    """Fetch values from a private sheet via Google Sheets API."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    with open(GOOGLE_TOKEN_FILE) as f:
        tok = json.load(f)
    creds = Credentials(
        token=tok.get('access_token') or tok.get('token'),
        refresh_token=tok.get('refresh_token'),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=tok.get('client_id'),
        client_secret=tok.get('client_secret'),
        scopes=tok.get('scopes') or tok.get('scope','').split(),
    )
    svc = build('sheets', 'v4', credentials=creds)
    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=range_name,
        valueRenderOption='FORMATTED_VALUE'
    ).execute()
    return result.get('values', [])

# ── Parsers ──────────────────────────────────────────────────────────────

def parse_progress(raw):
    """Extract Activation pre-alpha stats + countdown from PROGRESS tab."""
    reader = csv.reader(io.StringIO(raw))
    rows   = list(reader)
    days_to_alpha1 = None
    alpha1_date    = "Jul 15, 2026"
    activation_row = None
    for row in rows:
        if not row: continue
        text = " ".join(row)
        if "Days to Alpha Live" in text and days_to_alpha1 is None:
            import re
            m = re.search(r"Days to Alpha Live[^:]*:\s*(\d+)", text)
            if m: days_to_alpha1 = int(m.group(1))
            m2 = re.search(r"Alpha Live \(([^)]+)\)", text)
            if m2: alpha1_date = m2.group(1)
        if row[0].strip().upper() == "ACTIVATION":
            activation_row = row
    result = {"days_to_alpha1": days_to_alpha1, "alpha1_date": alpha1_date}
    if activation_row and len(activation_row) >= 7:
        result.update({
            "act_phase":       _get(activation_row, 1) or "Pre-Alpha",
            "act_done":        _int(_get(activation_row, 2)),
            "act_in_progress": _int(_get(activation_row, 3)),
            "act_not_started": _int(_get(activation_row, 4)),
            "act_total":       _int(_get(activation_row, 5)),
            "act_pct":         _float(_get(activation_row, 6)),
            "act_rag":         _get(activation_row, 7),
        })
    else:
        result.update({"act_phase":"Pre-Alpha","act_done":0,"act_in_progress":0,
                        "act_not_started":0,"act_total":0,"act_pct":0.0,"act_rag":""})
    return result

def parse_activation(raw):
    """Parse ACTIVATION tab — tasks list + blockers."""
    reader = csv.reader(io.StringIO(raw))
    rows   = list(reader)
    tasks, blockers = [], []
    SKIP_VALS = {"","MONTH","WEEK NUMBER","DATE","METRICS","PRE-ALPHA","ALPHA 1","ALPHA 2"}
    STATUSES  = {"DONE","IN-PROGRESS","NOT STARTED","BLOCKED"}
    for row in rows:
        if not row or len(row) < 2: continue
        desc   = row[0].strip()
        status = row[5].strip().upper() if len(row) > 5 else ""
        if not desc or desc.upper() in SKIP_VALS: continue
        if status not in STATUSES: continue
        if desc.startswith("202") or desc.startswith("May") or desc.startswith("Jun") or desc.startswith("Jul"): continue
        interdept  = _get(row, 6)
        depends_on = _get(row, 7)
        task = {
            "desc": desc, "metrics": _get(row,1), "owner": _get(row,2),
            "phase": _get(row,3), "deadline": _get(row,4),
            "status": _get(row,5), "interdept": interdept,
            "depends_on": depends_on, "needed_by": _get(row,8),
        }
        tasks.append(task)
        if status in ("NOT STARTED","BLOCKED") and interdept.upper()=="YES" and depends_on:
            blockers.append(task)
    return {"tasks": tasks, "blockers": blockers}

def parse_uat(raw):
    """Parse UAT Features tab."""
    reader = csv.reader(io.StringIO(raw))
    rows   = list(reader)
    if not rows: return {"headers":[],"rows":[]}
    headers = rows[0]
    data_rows = [row for row in rows[1:] if row and any(c.strip() for c in row)]
    return {"headers": headers, "rows": data_rows}

def parse_testing_plan(rows):
    """Parse Testing Plan tab — main scenario list + Alpha 1/2 scope comparison."""
    main_scenarios = []
    alpha_compare  = {"headers": ["", "Alpha 1", "Alpha 2"], "rows": []}
    in_compare = False
    for row in rows:
        if not row or not any(c.strip() for c in row): continue
        c1 = _get(row, 1)
        c2 = _get(row, 2)
        c3 = _get(row, 3)
        if c1 == "Main Scenario": continue
        # Main scenario rows (e.g. "0. Order Cancellation")
        if c1 and ". " in c1 and c1[0].isdigit():
            parts = c1.split(". ", 1)
            main_scenarios.append({"id": parts[0].strip(), "name": parts[1].strip(), "subs": c2, "tests": c3})
            continue
        # Start of Alpha 1 / Alpha 2 comparison table
        if c2 == "Alpha 1" and c3 == "Alpha 2":
            in_compare = True
            continue
        if in_compare and c1:
            alpha_compare["rows"].append([c1, c2, c3])
    return {"main_scenarios": main_scenarios, "alpha_compare": alpha_compare}

def parse_detailed_testing(rows):
    """Parse Detailed Testing tab — 14 scenario rows with full step breakdown."""
    scenarios = []
    SKIP = {"scenario", "scenario description", ""}
    for row in rows:
        if not row or len(row) < 2: continue
        sid  = _get(row, 0).lower()
        name = _get(row, 1).lower()
        if sid in SKIP or name in SKIP: continue
        sid  = _get(row, 0)
        name = _get(row, 1)
        if not sid or not name: continue
        scenarios.append({
            "id":       sid,
            "name":     name,
            "expected": _get(row, 2),
            "package":  _get(row, 3),
            "payment":  _get(row, 4),
            "buyer":    _get(row, 5),
            "seller":   _get(row, 6),
            "ops":      _get(row, 7),
            "escrow":   _get(row, 8),
            "features": _get(row, 9),
        })
    return scenarios

def parse_test_scenario(rows):
    """Parse Test Scenario tab — regional PIC assignments."""
    if not rows or len(rows) < 2:
        return {"headers": [], "rows": []}
    hdrs = ["Region", "Province", "PIC", "Scenarios (Ltd)", "Scenarios (Unli)"]
    data_rows = []
    for row in rows[2:]:
        if not row: continue
        region = _get(row, 0)
        if not region or region.lower() in ("region", "okay", ""): continue
        data_rows.append([region, _get(row,1), _get(row,2), _get(row,3), _get(row,4)])
    return {"headers": hdrs, "rows": data_rows}

# ── Main fetch ────────────────────────────────────────────────────────────
def fetch_data():
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]
    try:
        # Public sheets via CSV export
        progress_raw   = fetch_csv(csv_url(MAIN_SHEET_ID, GID_PROGRESS))
        activation_raw = fetch_csv(csv_url(MAIN_SHEET_ID, GID_ACTIVATION))
        uat_raw        = fetch_csv(csv_url(LIVE_SHEET_ID, GID_UAT))

        # Reference sheet (private) via Sheets API
        testing_plan_rows  = fetch_sheet_values(REF_SHEET_ID, "'Testing Plan'")
        detailed_test_rows = fetch_sheet_values(REF_SHEET_ID, "'Detailed Testing'")
        test_scenario_rows = fetch_sheet_values(REF_SHEET_ID, "'Test Scenario'")

        progress     = parse_progress(progress_raw)
        activation   = parse_activation(activation_raw)
        uat          = parse_uat(uat_raw)
        testing_plan = parse_testing_plan(testing_plan_rows)
        detailed     = parse_detailed_testing(detailed_test_rows)
        regional     = parse_test_scenario(test_scenario_rows)

        scenarios = {
            "main_scenarios": testing_plan["main_scenarios"],
            "alpha_compare":  testing_plan["alpha_compare"],
            "detailed":       detailed,
            "regions":        regional,
        }

        data = {
            **progress,
            "tasks":          activation["tasks"],
            "blockers":       activation["blockers"],
            "blocker_count":  len(activation["blockers"]),
            "scenarios":      scenarios,
            "uat":            uat,
            "scenario_count": len(detailed),
            "uat_count":      len(uat["rows"]),
            "last_updated":   datetime.now().strftime("%b %d, %Y %H:%M:%S"),
        }
        _cache["data"] = data
        _cache["ts"]   = now
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Data refreshed — "
              f"{len(activation['tasks'])} tasks, {len(activation['blockers'])} blockers, "
              f"{len(detailed)} scenarios")
        return data

    except Exception as e:
        print(f"[WARN] Fetch failed: {e}")
        import traceback; traceback.print_exc()
        if _cache["data"]: return _cache["data"]
        return {
            "error": str(e), "last_updated": "—", "tasks": [], "blockers": [],
            "scenarios": {
                "main_scenarios": [], "detailed": [], "regions": {"headers":[],"rows":[]},
                "alpha_compare": {"headers":["","Alpha 1","Alpha 2"],"rows":[]}
            },
            "uat": {"headers":[],"rows":[]},
            "act_done":0, "act_in_progress":0, "act_not_started":0,
            "act_total":0, "act_pct":0.0, "act_rag": "",
            "days_to_alpha1":None, "alpha1_date":"Jul 15, 2026",
            "scenario_count":0, "uat_count":0, "blocker_count":0
        }

# ── HTTP Handler ──────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/data":
            body = json.dumps(fetch_data()).encode()
            self._send(200, "application/json", body, cors=True)
        elif path in ("/", "/index.html"):
            with open(os.path.join(SCRIPT_DIR, "index.html"), "rb") as f:
                body = f.read()
            self._send(200, "text/html; charset=utf-8", body)
        elif path == "/logo.png":
            with open(os.path.join(SCRIPT_DIR, "logo.png"), "rb") as f:
                body = f.read()
            self._send(200, "image/png", body)
        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code, ct, body, cors=False):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(body))
        if cors: self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_): pass

if __name__ == "__main__":
    print("📊 Pre-loading activation data...")
    fetch_data()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"\n🚀 MallPlus Activation Dashboard is LIVE")
    print(f"   Local  → http://localhost:{PORT}")
    print(f"   LAN    → http://192.168.1.71:{PORT}")
    print(f"   (Ctrl+C to stop)\n")
    server.serve_forever()
