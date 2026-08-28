#!/usr/bin/env python3
"""MallPlus Launch Dashboard — Local Data Server
Serves the dashboard HTML and proxies Google Sheets data.
Run: python3 server.py
"""

import json, csv, io, os, sys, time, urllib.request, re, base64, hashlib, hmac, http.client
from urllib.parse import urlparse, parse_qs, urlsplit
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime
from recon_api import serve_recon_portal, handle_recon_api, handle_order_reconcile_api, handle_order_reconcile_anchor_api
from shipping_api import serve_shipping_portal, handle_shipping_api, handle_shipping_reconcile_api, handle_shipping_reconcile_anchor_api
from withdrawals_api import serve_withdrawals_portal, handle_withdrawals_api, handle_withdrawals_reconcile_api, handle_withdrawals_reconcile_anchor_api
from refunds_api import serve_refunds_portal, handle_refunds_api, handle_refunds_reconcile_api, handle_refunds_reconcile_anchor_api, handle_refunds_escrow_only_api
from reimbursement_api import (
    serve_reimbursement_portal, handle_reimbursement_api,
    _validate_session, _create_session, _find_employee,
    _can_access,
    AUTH_SECRET, FINANCE_TEAM_EMAILS, FS_VISIBLE_EMAILS,
)
from disbursement_api import serve_disbursement_portal, handle_disbursement_api

# ── Config ─────────────────────────────────────────────────────────────
PORT       = int(os.environ.get("PORT", 8080))
SHEET_ID   = "10czUOytN3KB6mybGWQzyB_v6Zm2tqoITGgcOAJhZzKk"
GID        = "1809860840"
CSV_URL    = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"
CACHE_TTL  = 60
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Activation Dashboard Config ─────────────────────────────────────────
ACTIVATION_DIR  = os.path.join(SCRIPT_DIR, "activation-dashboard")
ACT_SHEET_ID    = "10czUOytN3KB6mybGWQzyB_v6Zm2tqoITGgcOAJhZzKk"
NEW_SHEET_ID    = "1BsX1v_QCWmVe8JUY2T8sRBKzNOq6wrd4yT4I9Oegqc8"
GID_PROGRESS    = "1809860840"
GID_ACTIVATION  = "2059087416"
GID_SCENARIOS   = "501389690"
GID_UAT         = "1390827425"
_act_cache = {"data": None, "ts": 0.0}

# ── UAT Scenarios Config ─────────────────────────────────────────────────────
UAT_SHEET_ID    = "1ZhNDj80oHZ-EcmNDN-0O1q8TXtF-pAcblbRiamllpDg"
UAT_GID         = "1419090506"
_uat_cache = {"data": None, "ts": 0.0}

# ── Charm Scenarios Config (hero card role breakdown) ─────────────────────────
CHARM_SHEET_ID  = "1KP1d0TWgzJndjxehAGY5ZwsuJRDUi0FGsZFhI2MlTHE"
CHARM_GID       = "1540966367"  # Charm List tab
_charm_cache = {"data": None, "ts": 0.0}

# ── Buyer/Seller Scenario Config ──────────────────────────────────────────────
BS_SHEET_ID  = "1KP1d0TWgzJndjxehAGY5ZwsuJRDUi0FGsZFhI2MlTHE"
BS_GID       = "2045343270"
_bs_cache = {"data": None, "ts": 0.0}

# ── Operations Scenarios Config ───────────────────────────────────────────────
OPS_SHEET_ID = "1KP1d0TWgzJndjxehAGY5ZwsuJRDUi0FGsZFhI2MlTHE"
OPS_GID      = "1917142607"
_ops_cache = {"data": None, "ts": 0.0}

# ── Cache ───────────────────────────────────────────────────────────────
_cache = {"data": None, "ts": 0.0}

# ── Recon Portal Auth — SSO gate (signed employee session, 2026-08-19 refactor) ─
# RECON_PASSWORD retired; now uses AUTH_SECRET-signed employee sessions.
# Finance department only — roster-driven (_is_finance_employee), 2026-08-20.
RECON_SESSION_TTL = int(os.environ.get("RECON_SESSION_TTL", str(86400)))  # 24h
RECON_MAX_ATTEMPTS = 10
RECON_LOCKOUT_SEC = 900
# Staging prefix mode: the staging instance (STAGING=1) mounts the recon portal
# under /recon-staging instead of /recon. Requests are stripped for routing and
# every outgoing URL/cookie/redirect is rewritten back to the prefix, so the
# whole portal (pages + JS fetches + login/logout) stays on the staging URL.
RECON_URL_PREFIX = os.environ.get("RECON_URL_PREFIX", "").rstrip("/")
# Prod-instance relay: /recon-staging* is forwarded verbatim to the local
# staging instance (which owns the prefix and rewrites its own URLs). This
# avoids needing a second public tunnel on the free ngrok plan. Only active
# when THIS instance is NOT the staging instance (no RECON_URL_PREFIX).
STAGING_RELAY_TARGET = os.environ.get("STAGING_RELAY_TARGET", "http://127.0.0.1:8090")
_recon_attempts = {}  # ip -> [count, lockout_until]

RECON_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MallPlus Recon Portal — Login</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Quicksand:wght@500;600;700&display=swap" rel="stylesheet"/>
<link href="https://fonts.cdnfonts.com/css/garet" rel="stylesheet"/>
<style>
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:'Space Grotesk',system-ui,sans-serif;
       background:linear-gradient(135deg,#3724ED 0%,#1A9FD8 45%,#00AFA0 100%);
       background-attachment:fixed;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;}
  .card{background:rgba(255,255,255,.96);border-radius:32px;box-shadow:0 18px 50px rgba(26,16,53,.35);
        width:100%;max-width:400px;padding:40px 36px;border:1px solid rgba(0,175,160,.13);}
  .lock{margin:0 auto 16px;width:52px;height:52px;border-radius:16px;background:#E0F5F3;
        display:flex;align-items:center;justify-content:center;font-size:24px;}
  .co{font-size:11px;letter-spacing:2.5px;text-transform:uppercase;color:#00AFA0;font-weight:700;text-align:center;font-family:'Quicksand',sans-serif;}
  h1{font-family:'Garet','Space Grotesk',sans-serif;font-size:20px;color:#1A1035;text-align:center;margin:8px 0 4px;font-weight:700;}
  .sub{font-size:12.5px;color:#6B7280;text-align:center;margin-bottom:24px;font-family:'Quicksand',sans-serif;}
  label{display:block;font-size:12px;font-weight:600;color:#1C2B39;margin-bottom:6px;}
  input{width:100%;padding:12px 14px;border:1.5px solid rgba(0,175,160,.3);border-radius:12px;font-size:15px;outline:none;font-family:'Space Grotesk',sans-serif;margin-bottom:12px;}
  input:focus{border-color:#00AFA0;box-shadow:0 0 0 3px #E0F5F3;}
  button{width:100%;margin-top:8px;padding:13px;background:#00AFA0;color:#fff;border:none;border-radius:999px;font-size:14.5px;font-weight:700;cursor:pointer;font-family:'Quicksand',sans-serif;transition:background .2s;}
  button:hover{background:#007A73;}
  .err{background:#FDECEA;color:#C0392B;border:1px solid #F5C6C0;border-radius:8px;padding:9px 12px;font-size:12.5px;margin-top:14px;text-align:center;}
  .foot{font-size:11px;color:#8A97A6;text-align:center;margin-top:20px;line-height:1.5;font-family:'Quicksand',sans-serif;}
</style>
</head>
<body>
  <form class="card" method="POST" action="/recon/login">
    <div class="lock">🔄</div>
    <div class="co">MallPlus · FinCom Technologies Inc.</div>
    <h1>Recon Portal</h1>
    <div class="sub">Finance team access only</div>
    <label for="recon-email">Email</label>
    <input type="email" id="recon-email" name="email" autofocus autocomplete="username" placeholder="you@fincom.asia">
    <label for="recon-pin">PIN</label>
    <input type="password" id="recon-pin" name="pin" autocomplete="current-password" placeholder="••••">
    <button type="submit">Sign In →</button>
    __ERROR_BLOCK__
    <div class="foot">Finance team only. Contact finance@fincom.asia for access.<br>All access attempts are logged.</div>
  </form>
</body>
</html>"""


RECON_LOGOUT_CHIP = (
    '<a href="/recon/logout" title="End session" '
    'style="position:fixed;bottom:14px;right:14px;z-index:9999;'
    'background:#ffffff;color:#1A1035;border:1px solid rgba(0,175,160,.55);'
    'border-radius:20px;padding:7px 16px;font-size:12px;text-decoration:none;'
    'font-family:\'Segoe UI\',sans-serif;font-weight:600;'
    'box-shadow:0 4px 16px rgba(0,0,0,.30);">Log out →</a>'
).encode("utf-8")


# [RECON_PASSWORD token functions removed — SSO gate uses signed employee sessions]

# ── Helpers ─────────────────────────────────────────────────────────────
def _int(v):
    try:
        s = v.strip().replace(",", "")
        return int(s) if s.lstrip("-").isdigit() else 0
    except:
        return 0

def _float(v):
    try:
        return float(v.strip().replace("%", "").replace(",", "."))
    except:
        return 0.0

# ── Parser ──────────────────────────────────────────────────────────────
def parse_csv(raw):
    reader = csv.reader(io.StringIO(raw))
    rows   = list(reader)

    departments  = {}
    dept_order   = []
    current_dept = None
    PHASES       = ["Alpha/UAT", "Ironborn", "Post-Launch"]

    for row in rows:
        if len(row) < 6:
            continue
        col0 = row[0].strip()
        col1 = row[1].strip() if len(row) > 1 else ""

        # Skip noise
        if col0 in ("DEPARTMENT", "ORG TOTAL", "PHASE"):
            continue
        if "Phase Untagged" in col0 or "Phase Untagged" in col1:
            continue
        if col1 in ("PHASE", ""):
            continue

        # Detect phase
        phase = next((p for p in PHASES if p.lower() in col1.lower()), None)
        if phase is None:
            continue

        # New department?
        clean = col0.replace("⚠️", "").strip()
        if clean and clean not in ("DEPARTMENT", "ORG TOTAL"):
            current_dept = clean
            if current_dept not in departments:
                departments[current_dept] = {}
                dept_order.append(current_dept)

        if not current_dept:
            continue

        done        = _int(row[2])  if len(row) > 2 else 0
        in_progress = _int(row[3])  if len(row) > 3 else 0
        not_started = _int(row[4])  if len(row) > 4 else 0
        total       = done + in_progress + not_started
        departments[current_dept][phase] = {
            "done":         done,
            "in_progress":  in_progress,
            "not_started":  not_started,
            "total":        total,
            "pct":          round(done / total * 100, 1) if total else 0.0,
            "rag":          row[5].strip() if len(row) > 5 else "",
            "deadline_rag": row[6].strip() if len(row) > 6 else "",
        }

    # Org totals
    org_totals = {}
    for dept, phases in departments.items():
        for phase, d in phases.items():
            if phase not in org_totals:
                org_totals[phase] = {"done": 0, "in_progress": 0, "not_started": 0, "total": 0, "pct": 0.0}
            for k in ("done", "in_progress", "not_started", "total"):
                org_totals[phase][k] += d[k]
    for d in org_totals.values():
        d["pct"] = round(d["done"] / d["total"] * 100, 1) if d["total"] else 0.0

    # ── Blockers ──
    overdue_blockers = []
    atrisk_blockers  = []
    blocker_mode     = None   # "overdue" | "atrisk" | None

    for row in rows:
        if not row or len(row) < 2:
            continue
        col0 = row[0].strip()

        if "OVERDUE BLOCKERS" in col0.upper():
            blocker_mode = "overdue"
            continue
        if "AT-RISK BLOCKERS" in col0.upper() or "AT RISK BLOCKERS" in col0.upper():
            blocker_mode = "atrisk"
            continue
        if blocker_mode and col0 in ("TASK / DELIVERABLE", "TASK", ""):
            continue  # skip header rows and blanks
        if blocker_mode and not any(c.strip() for c in row):
            continue  # skip empty rows

        # Detect next major section (non-blocker)
        if blocker_mode and col0 and col0 not in ("TASK / DELIVERABLE",) \
                and row[1].strip() in ("",) and "BLOCK" not in col0.upper() \
                and col0[0] not in ("🔴", "🟡", "🟢") \
                and len(col0) > 30:
            # Long task text — this is a blocker entry
            pass

        if blocker_mode:
            task     = row[0].strip() if len(row) > 0 else ""
            dept     = row[1].strip() if len(row) > 1 else ""
            phase    = row[2].strip() if len(row) > 2 else ""
            deadline = row[3].strip() if len(row) > 3 else ""
            days     = row[4].strip() if len(row) > 4 else ""
            status   = row[5].strip() if len(row) > 5 else ""
            blocked_by = row[6].strip() if len(row) > 6 else ""
            needed_by  = row[7].strip() if len(row) > 7 else ""

            # Skip if it looks like a header row
            if task.upper() in ("TASK / DELIVERABLE", "TASK", "BLOCKED TASK") or not task:
                continue
            # Skip cross-dept dependency rows (they have → in them)
            if "→" in task or "blocking" in task.lower():
                continue

            entry = {
                "task": task, "dept": dept, "phase": phase,
                "deadline": deadline, "days": days,
                "status": status, "blocked_by": blocked_by, "needed_by": needed_by
            }
            if blocker_mode == "overdue":
                overdue_blockers.append(entry)
            else:
                atrisk_blockers.append(entry)

    return {
        "departments":       departments,
        "dept_order":        dept_order,
        "org_totals":        org_totals,
        "overdue_blockers":  overdue_blockers,
        "atrisk_blockers":   atrisk_blockers[:15],  # cap at 15 for display
        "last_updated":      datetime.now().strftime("%b %d, %Y %H:%M:%S"),
    }

# ── Fetch ───────────────────────────────────────────────────────────────
def fetch_data():
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]
    try:
        req = urllib.request.Request(CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
        data = parse_csv(raw)
        _cache["data"] = data
        _cache["ts"]   = now
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Sheet refreshed — {len(data['departments'])} depts")
        return data
    except Exception as e:
        print(f"[WARN] Fetch failed: {e}")
        if _cache["data"]:
            return _cache["data"]
        return {"departments": {}, "dept_order": [], "org_totals": {}, "error": str(e), "last_updated": "—"}

# ══════════════════════════════════════════════════════
# ACTIVATION DASHBOARD DATA
# ══════════════════════════════════════════════════════
def _csv_url(sheet_id, gid):
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

def _fetch_csv(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")

def _parse_act_progress(raw):
    rows = list(csv.reader(io.StringIO(raw)))
    days_to_alpha1 = None; alpha1_date = "Jul 15, 2026"; activation_row = None
    for row in rows:
        if not row: continue
        text = " ".join(row)
        if "Days to Alpha Live" in text and days_to_alpha1 is None:
            m = re.search(r"Days to Alpha Live[^:]*:\s*(\d+)", text)
            if m: days_to_alpha1 = int(m.group(1))
            m2 = re.search(r"Alpha Live \(([^)]+)\)", text)
            if m2: alpha1_date = m2.group(1)
        if row[0].strip().upper() == "ACTIVATION" and len(row) >= 7:
            activation_row = row
    result = {"days_to_alpha1": days_to_alpha1, "alpha1_date": alpha1_date}
    if activation_row:
        def fi(v):
            try: return int(float(str(v).strip().replace(",","")))
            except: return 0
        def ff(v):
            try: return float(str(v).strip().replace("%","").replace(",","."))
            except: return 0.0
        result.update({"act_done":fi(activation_row[2]),"act_in_progress":fi(activation_row[3]),
            "act_not_started":fi(activation_row[4]),"act_total":fi(activation_row[5]),
            "act_pct":ff(activation_row[6]),"act_rag":activation_row[7].strip() if len(activation_row)>7 else ""})
    else:
        result.update({"act_done":0,"act_in_progress":0,"act_not_started":0,"act_total":0,"act_pct":0.0,"act_rag":""})
    return result

def _parse_act_tasks(raw):
    rows = list(csv.reader(io.StringIO(raw)))
    tasks=[]; blockers=[]
    SKIP={"MONTH","WEEK NUMBER","DATE","METRICS","PRE-ALPHA","ALPHA 1","ALPHA 2",""}
    VALID_STATUS={"DONE","IN-PROGRESS","NOT STARTED","BLOCKED"}
    for row in rows:
        if not row or len(row)<2: continue
        desc=row[0].strip(); status=(row[5].strip().upper() if len(row)>5 else "")
        if not desc or desc.upper() in SKIP: continue
        if status not in VALID_STATUS: continue
        if desc.startswith(("202","May","Jun","Jul")): continue
        t={"desc":desc,"metrics":row[1].strip() if len(row)>1 else "",
           "owner":row[2].strip() if len(row)>2 else "",
           "phase":row[3].strip() if len(row)>3 else "",
           "deadline":row[4].strip() if len(row)>4 else "",
           "status":row[5].strip() if len(row)>5 else "",
           "interdept":row[6].strip() if len(row)>6 else "",
           "depends_on":row[7].strip() if len(row)>7 else "",
           "needed_by":row[8].strip() if len(row)>8 else ""}
        tasks.append(t)
        if status in ("NOT STARTED","BLOCKED") and t["interdept"].upper()=="YES" and t["depends_on"]:
            blockers.append(t)
    return {"tasks":tasks,"blockers":blockers}

def _parse_sheet_tab(raw):
    rows=list(csv.reader(io.StringIO(raw)))
    if not rows: return {"headers":[],"rows":[]}
    headers=rows[0]
    data=[r for r in rows[1:] if r and any(c.strip() for c in r)]
    return {"headers":headers,"rows":data}

def fetch_activation_data():
    now=time.time()
    if _act_cache["data"] and (now-_act_cache["ts"])<CACHE_TTL:
        return _act_cache["data"]
    try:
        prog=_parse_act_progress(_fetch_csv(_csv_url(ACT_SHEET_ID,GID_PROGRESS)))
        act=_parse_act_tasks(_fetch_csv(_csv_url(ACT_SHEET_ID,GID_ACTIVATION)))
        sc=_parse_sheet_tab(_fetch_csv(_csv_url(NEW_SHEET_ID,GID_SCENARIOS)))
        ut=_parse_sheet_tab(_fetch_csv(_csv_url(NEW_SHEET_ID,GID_UAT)))
        data={**prog,"tasks":act["tasks"],"blockers":act["blockers"],
              "blocker_count":len(act["blockers"]),
              "scenarios":sc,"uat":ut,
              "scenario_count":len(sc["rows"]),"uat_count":len(ut["rows"]),
              "last_updated":datetime.now().strftime("%b %d, %Y %H:%M:%S")}
        _act_cache["data"]=data; _act_cache["ts"]=now
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Activation refreshed — {len(act['tasks'])} tasks, {len(act['blockers'])} blockers")
        return data
    except Exception as e:
        print(f"[WARN] Activation fetch failed: {e}")
        if _act_cache["data"]: return _act_cache["data"]
        return {"error":str(e),"last_updated":"—","tasks":[],"blockers":[],
                "scenarios":{"headers":[],"rows":[]},"uat":{"headers":[],"rows":[]},
                "act_done":0,"act_in_progress":0,"act_not_started":0,"act_total":0,"act_pct":0.0,
                "days_to_alpha1":None,"alpha1_date":"Jul 15, 2026","scenario_count":0,"uat_count":0,"blocker_count":0}

# ── UAT Scenarios Fetch ─────────────────────────────────────────────────
def fetch_uat_scenarios():
    now = time.time()
    if _uat_cache["data"] and (now - _uat_cache["ts"]) < CACHE_TTL:
        return _uat_cache["data"]
    try:
        url = f"https://docs.google.com/spreadsheets/d/{UAT_SHEET_ID}/export?format=csv&gid={UAT_GID}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
        rows = list(csv.reader(io.StringIO(raw)))
        items = []
        last_topic = ""
        for row in rows[1:]:  # skip header
            if not row or not any(c.strip() for c in row):
                continue
            topic = row[1].strip() if len(row) > 1 else ""
            if topic:
                last_topic = topic
            else:
                topic = last_topic  # fill-down
            items.append({
                "num":      row[0].strip() if len(row) > 0 else "",
                "topic":    topic,
                "subtopic": row[2].strip() if len(row) > 2 else "",
                "scenario": row[3].strip() if len(row) > 3 else "",
                "expected": row[4].strip() if len(row) > 4 else "",
                "seller":   row[5].strip() if len(row) > 5 else "",
                "admin":    row[6].strip() if len(row) > 6 else "",
                "buyer":    row[7].strip() if len(row) > 7 else "",
                "owner":    row[8].strip() if len(row) > 8 else "",
                "status":   row[9].strip() if len(row) > 9 else "",
            })
        total   = len(items)
        passed  = sum(1 for i in items if i["status"].lower() == "pass")
        failed  = sum(1 for i in items if i["status"].lower() == "fail")
        pending = total - passed - failed
        data = {
            "items": items,
            "total": total, "passed": passed, "failed": failed, "pending": pending,
            "last_updated": datetime.now().strftime("%b %d, %Y %H:%M:%S"),
        }
        _uat_cache["data"] = data
        _uat_cache["ts"]   = now
        print(f"[{datetime.now().strftime('%H:%M:%S')}] UAT scenarios refreshed — {total} items, {failed} failed, {pending} pending")
        return data
    except Exception as e:
        print(f"[WARN] UAT fetch failed: {e}")
        if _uat_cache["data"]:
            return _uat_cache["data"]
        return {"items": [], "total": 0, "passed": 0, "failed": 0, "pending": 0,
                "error": str(e), "last_updated": "—"}

# ── Operations Scenarios Fetch ───────────────────────────────────────────────
def fetch_ops_scenarios():
    now = time.time()
    if _ops_cache["data"] and (now - _ops_cache["ts"]) < CACHE_TTL:
        return _ops_cache["data"]
    try:
        url = f"https://docs.google.com/spreadsheets/d/{OPS_SHEET_ID}/export?format=csv&gid={OPS_GID}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
        rows = list(csv.reader(io.StringIO(raw)))
        items = []
        for row in rows[1:]:
            a = row[0].strip() if len(row) > 0 else ""
            b = row[1].strip() if len(row) > 1 else ""
            c = row[2].strip() if len(row) > 2 else ""
            d = row[3].strip() if len(row) > 3 else ""
            if not a and not b:
                continue
            items.append({"scenario": a, "description": b, "expected": c, "owner": d})
        data = {
            "items": items, "total": len(items),
            "last_updated": datetime.now().strftime("%b %d, %Y %H:%M:%S"),
        }
        _ops_cache["data"] = data
        _ops_cache["ts"]   = now
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Ops scenarios refreshed — {len(items)} items")
        return data
    except Exception as e:
        print(f"[WARN] Ops scenarios fetch failed: {e}")
        if _ops_cache["data"]: return _ops_cache["data"]
        return {"items": [], "total": 0, "error": str(e), "last_updated": "—"}

# ── Buyer/Seller Scenarios Fetch ─────────────────────────────────────────────
def fetch_buyer_seller():
    now = time.time()
    if _bs_cache["data"] and (now - _bs_cache["ts"]) < CACHE_TTL:
        return _bs_cache["data"]
    try:
        url = f"https://docs.google.com/spreadsheets/d/{BS_SHEET_ID}/export?format=csv&gid={BS_GID}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
        rows = list(csv.reader(io.StringIO(raw)))
        items = []
        for row in rows[1:]:
            a = row[0].strip() if len(row) > 0 else ""
            b = row[1].strip() if len(row) > 1 else ""
            c = row[2].strip() if len(row) > 2 else ""
            d = row[3].strip() if len(row) > 3 else ""
            if not a and not b:
                continue
            items.append({"scenario": a, "description": b, "expected": c, "owner": d})
        data = {
            "items": items, "total": len(items),
            "last_updated": datetime.now().strftime("%b %d, %Y %H:%M:%S"),
        }
        _bs_cache["data"] = data
        _bs_cache["ts"]   = now
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Buyer/Seller scenarios refreshed — {len(items)} items")
        return data
    except Exception as e:
        print(f"[WARN] Buyer/Seller fetch failed: {e}")
        if _bs_cache["data"]: return _bs_cache["data"]
        return {"items": [], "total": 0, "error": str(e), "last_updated": "—"}

# ── Charm Scenarios Fetch (role breakdown for hero card) ─────────────────────
def fetch_charm_scenarios():
    now = time.time()
    if _charm_cache["data"] and (now - _charm_cache["ts"]) < CACHE_TTL:
        return _charm_cache["data"]
    try:
        url = f"https://docs.google.com/spreadsheets/d/{CHARM_SHEET_ID}/export?format=csv&gid={CHARM_GID}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
        rows = list(csv.reader(io.StringIO(raw)))
        by_role = {}
        for row in rows[1:]:  # skip header
            if not row or not any(c.strip() for c in row):
                continue
            scenario_id = row[0].strip() if len(row) > 0 else ""
            if not scenario_id:
                continue
            role = row[1].strip() if len(row) > 1 else "Unknown"
            if not role:
                role = "Unknown"
            by_role[role] = by_role.get(role, 0) + 1
        total = sum(by_role.values())
        data = {
            "total": total,
            "byRole": by_role,
            "last_updated": datetime.now().strftime("%b %d, %Y %H:%M:%S"),
        }
        _charm_cache["data"] = data
        _charm_cache["ts"]   = now
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Charm scenarios refreshed — {total} total, {len(by_role)} roles: {by_role}")
        return data
    except Exception as e:
        print(f"[WARN] Charm scenarios fetch failed: {e}")
        if _charm_cache["data"]:
            return _charm_cache["data"]
        return {"total": 0, "byRole": {}, "error": str(e), "last_updated": "—"}

# ── HTTP Handler ─────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        # Prod-instance staging relay: forward /recon-staging* to the local
        # staging backend (8090) verbatim. Only when this instance is NOT the
        # staging instance (RECON_URL_PREFIX empty).
        if not RECON_URL_PREFIX and (path == "/recon-staging" or path.startswith("/recon-staging/")):
            self._relay_staging(self.path, body=None)
            return
        path = self._strip_recon_prefix(path)

        # ── Recon Portal auth gate ──
        if path == "/recon/logout":
            self.send_response(302)
            self.send_header("Location", self._recon_loc("/recon"))
            self.send_header("Set-Cookie",
                self._recon_cookie_out("recon_session=; Path=/recon; HttpOnly; SameSite=Lax; Max-Age=0"))
            self.end_headers()
            return
        if path == "/recon/login":
            self._send(200, "text/html; charset=utf-8",
                       RECON_LOGIN_PAGE.replace("__ERROR_BLOCK__", "").encode())
            return
        if path == "/recon" or path.startswith("/recon/"):
            # SSO: ?token= from landing page — validate signed session, set cookie, redirect
            token_param = (qs.get("token") or [None])[0]
            if token_param and AUTH_SECRET and path not in ("/recon/logout", "/recon/login"):
                session = _validate_session(token_param)
                if session and _can_access(session.get("email", ""), "recon"):
                    self._recon_clear_failures()
                    print(f"[RECON-AUTH] SSO token grant for {session.get('email')} from {self.client_address[0]}")
                    secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "http") == "https" else ""
                    dest = "/recon" if path in ("/recon", "/recon/") else path
                    self.send_response(302)
                    self.send_header("Location", self._recon_loc(dest))
                    self.send_header("Set-Cookie",
                        self._recon_cookie_out(
                            f"recon_session={token_param}; Path=/recon; HttpOnly; SameSite=Lax; "
                            f"Max-Age={RECON_SESSION_TTL}{secure}"))
                    self.end_headers()
                    return
                elif session:
                    # Valid session but not Finance
                    self._send(403, "text/html; charset=utf-8",
                               RECON_LOGIN_PAGE.replace("__ERROR_BLOCK__",
                                   '<div class="err">Recon access is limited to the Finance team.</div>').encode())
                    return
            if not self._recon_gate(path, "/api/" in path):
                return

        # ── Reconciliation Portal — Homepage ──
        if path == "/recon" or path == "/recon/":
            self._serve_recon_page(_RECON_HOMEPAGE)
            return

        # Legacy alias: /recon/api/orders → /recon/gcash/api/orders
        if path == "/recon/api/orders":
            status, ct, body, cors = handle_recon_api(path, qs)
            self._send(status, ct, body, cors=cors)
            return

        # ── Portal 1: Order Recon (existing) ──
        if path in ("/recon/order", "/recon/order/", "/recon/gcash", "/recon/gcash/"):
            self._serve_recon_page(serve_recon_portal())
            return
        if path in ("/recon/order/api/orders", "/recon/gcash/api/orders"):
            status, ct, body, cors = handle_recon_api(path, qs)
            self._send(status, ct, body, cors=cors)
            return

        # ── Portal 2: Shipping Fee Recon ──
        if path in ("/recon/shipping", "/recon/shipping/"):
            self._serve_recon_page(serve_shipping_portal())
            return
        if path == "/recon/shipping/api/orders":
            status, ct, body, cors = handle_shipping_api(path, qs)
            self._send(status, ct, body, cors=cors)
            return

        # ── Portal 3: Wallet Withdrawal Recon ──
        if path in ("/recon/withdrawals", "/recon/withdrawals/"):
            self._serve_recon_page(serve_withdrawals_portal())
            return
        if path == "/recon/withdrawals/api/orders":
            status, ct, body, cors = handle_withdrawals_api(path, qs)
            self._send(status, ct, body, cors=cors)
            return
        if path in ("/recon/refunds", "/recon/refunds/"):
            self._serve_recon_page(serve_refunds_portal(path))
            return
        if path == "/recon/refunds/api/orders":
            status, ct, body, cors = handle_refunds_api(path, qs)
            self._send(status, ct, body, cors=cors)
            return
        if path == "/recon/refunds/api/escrow-only":
            status, ct, body, cors = handle_refunds_escrow_only_api(qs)
            self._send(status, ct, body, cors=cors)
            return

        # ── Landing page widgets (calendar + announcements, parity with Railway) ──
        if path in ("/api/calendar", "/api/announcements"):
            req_headers = {k.lower(): self.headers[k] for k in self.headers}
            status, ct, body, cors = handle_reimbursement_api(path, qs, None, req_headers)
            self._send(status, ct, body, cors=cors)
            return

        # ── Reimbursement Portal ──
        if path in ("/reimbursements", "/reimbursements/"):
            self._send(200, "text/html; charset=utf-8", serve_reimbursement_portal())
            return
        if path.startswith("/reimbursements/api/"):
            req_headers = {k.lower(): self.headers[k] for k in self.headers}
            status, ct, body, cors = handle_reimbursement_api(path, qs, None, req_headers)
            self._send(status, ct, body, cors=cors)
            return

        # ── Disbursement Portal ──
        if path in ("/disbursements", "/disbursements/", "/disbursement", "/disbursement/"):
            self._send(200, "text/html; charset=utf-8", serve_disbursement_portal())
            return
        if path.startswith("/disbursements/api/"):
            req_headers = {k.lower(): self.headers[k] for k in self.headers}
            status, ct, body, cors = handle_disbursement_api(path, qs, None, req_headers)
            self._send(status, ct, body, cors=cors)
            return

        # ── Serve receipt files ──
        if path.startswith("/reimbursements/receipts/"):
            import mimetypes
            # Strip /reimbursements/ prefix to map to receipts/ dir
            rel = path[len('/reimbursements/'):]
            receipt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)
            if os.path.isfile(receipt_path):
                ct, _ = mimetypes.guess_type(receipt_path)
                with open(receipt_path, 'rb') as f:
                    body = f.read()
                self._send(200, ct or 'application/octet-stream', body, cors=True)
                return
            else:
                self._send(404, 'text/plain', b'Not found')
                return

        # ── Activation Dashboard routes ──
        if path in ("/activation", "/activation/"):
            self.send_response(301)
            self.send_header("Location", "/activation/index.html")
            self.end_headers()
            return
        elif path == "/activation/index.html":
            with open(os.path.join(ACTIVATION_DIR, "index.html"), "rb") as f:
                body = f.read()
            # Patch API path to use /activation/api/data
            body = body.replace(b"fetch('/api/data')", b"fetch('/activation/api/data')")
            self._send(200, "text/html; charset=utf-8", body)
            return
        elif path == "/activation/api/data":
            body = json.dumps(fetch_activation_data()).encode()
            self._send(200, "application/json", body, cors=True)
            return
        elif path == "/activation/logo.png":
            with open(os.path.join(ACTIVATION_DIR, "logo.png"), "rb") as f:
                body = f.read()
            self._send(200, "image/png", body)
            return
        # ── Main Dashboard routes ──
        if path == "/api/data":
            data = fetch_data()
            # Merge activation summary so browser needs only one fetch
            try:
                act = fetch_activation_data()
                data["act_done"]         = act.get("act_done", 0)
                data["act_in_progress"]  = act.get("act_in_progress", 0)
                data["act_not_started"]  = act.get("act_not_started", 0)
                data["act_total"]        = act.get("act_total", 0)
            except Exception:
                pass
            body = json.dumps(data).encode()
            self._send(200, "application/json", body, cors=True)
        elif path == "/api/ops-scenarios":
            body = json.dumps(fetch_ops_scenarios()).encode()
            self._send(200, "application/json", body, cors=True)
        elif path == "/api/buyer-seller-scenarios":
            body = json.dumps(fetch_buyer_seller()).encode()
            self._send(200, "application/json", body, cors=True)
        elif path == "/api/uat-scenarios":
            body = json.dumps(fetch_uat_scenarios()).encode()
            self._send(200, "application/json", body, cors=True)
        elif path == "/api/charm-scenarios":
            body = json.dumps(fetch_charm_scenarios()).encode()
            self._send(200, "application/json", body, cors=True)
        elif path in ("/", "/index.html"):
            with open(os.path.join(SCRIPT_DIR, "index.html"), "rb") as f:
                body = f.read()
            self._send(200, "text/html; charset=utf-8", body)
        elif path == "/logo.png":
            logo_path = os.path.join(SCRIPT_DIR, "logo.png")
            with open(logo_path, "rb") as f:
                body = f.read()
            self._send(200, "image/png", body)
        # ── Marketing deliverable: MallPlus Campaign Optimization Process ──
        elif path in ("/campaign-optimization", "/campaign-optimization/"):
            with open(os.path.join(SCRIPT_DIR, "campaign-optimization.html"), "rb") as f:
                body = f.read()
            self._send(200, "text/html; charset=utf-8", body)
        elif path == "/campaign-optimization.pdf":
            with open(os.path.join(SCRIPT_DIR, "campaign-optimization.pdf"), "rb") as f:
                body = f.read()
            self._send(200, "application/pdf", body)
        else:
            self._send(404, "text/plain", b"Not found")

    def _relay_staging(self, path, body=None):
        """Forward /recon-staging* to the local staging instance verbatim.
        Full path + query + method + body + headers are preserved; the staging
        instance (STAGING=1, RECON_URL_PREFIX=/recon-staging) rewrites its own
        URLs/cookies/bodies. Response status + key headers pass through."""
        u = urlsplit(STAGING_RELAY_TARGET)
        headers = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in ("host", "content-length", "connection",
                      "accept-encoding", "transfer-encoding"):
                continue
            headers[k] = v
        headers["Host"] = u.netloc
        headers["X-Forwarded-Host"] = self.headers.get("Host", "fcos.fincom.asia")
        headers["X-Forwarded-Proto"] = "https"
        conn = http.client.HTTPConnection(u.netloc, timeout=60)
        try:
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        except Exception as e:
            print(f"[RECON-STAGING-RELAY] {self.command} {path} -> {e}")
            conn.close()
            self._send_json(502, {"error": "Staging recon backend unreachable"})
            return
        pass_through = ("content-type", "content-disposition", "set-cookie",
                        "cache-control", "content-length", "location")
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in pass_through:
                self.send_header(k, v)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        try:
            self.wfile.write(data)
        finally:
            conn.close()

    def _strip_recon_prefix(self, path):
        """Staging prefix mode: /recon-staging/* routes internally as /recon/*.
        Sets self._prefix_mode so _send/_recon_loc/_recon_cookie_out rewrite
        outgoing URLs back to the prefix."""
        if RECON_URL_PREFIX and (path == RECON_URL_PREFIX
                                 or path.startswith(RECON_URL_PREFIX + "/")):
            self._prefix_mode = True
            return "/recon" + path[len(RECON_URL_PREFIX):]
        self._prefix_mode = False
        return path

    def _recon_loc(self, loc):
        """Rewrite an outgoing redirect Location to the staging prefix."""
        if getattr(self, "_prefix_mode", False) and RECON_URL_PREFIX \
                and loc.startswith("/recon"):
            return RECON_URL_PREFIX + loc[len("/recon"):]
        return loc

    def _recon_cookie_out(self, cookie):
        """Rewrite Set-Cookie Path=/recon -> Path=<prefix> (cookie NAME unchanged)."""
        if getattr(self, "_prefix_mode", False) and RECON_URL_PREFIX:
            return cookie.replace("Path=/recon", f"Path={RECON_URL_PREFIX}")
        return cookie

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        cl = int(self.headers.get('Content-Length', 0))
        body_raw = self.rfile.read(cl) if cl else b''
        # Prod-instance staging relay: forward /recon-staging* (incl. POST body)
        if not RECON_URL_PREFIX and (path == "/recon-staging" or path.startswith("/recon-staging/")):
            self._relay_staging(self.path, body=body_raw)
            return
        path = self._strip_recon_prefix(path)

        # Collect request headers for auth extraction
        req_headers = {}
        for key in self.headers:
            req_headers[key.lower()] = self.headers[key]

        # ── Recon Portal auth gate ──
        if path == "/recon/login":
            self._handle_recon_login(body_raw)
            return
        if path == "/recon" or path.startswith("/recon/"):
            if not self._recon_gate(path, True):
                return

        if path.startswith("/reimbursements/api/"):
            status, ct, body, cors = handle_reimbursement_api(path, qs, body_raw, req_headers)
            self._send(status, ct, body, cors=cors)
            return

        # ── Announcements API (POST create + image upload, parity with Railway) ──
        if path in ("/api/announcements", "/api/announcements/upload"):
            status, ct, body, cors = handle_reimbursement_api(path, qs, body_raw, req_headers)
            self._send(status, ct, body, cors=cors)
            return

        if path.startswith("/disbursements/api/"):
            status, ct, body, cors = handle_disbursement_api(path, qs, body_raw, req_headers)
            self._send(status, ct, body, cors=cors)
            return

        if path == "/recon/order/api/reconcile":
            try:
                j = json.loads(body_raw)
            except Exception:
                j = {}
            status, ct, body, cors = handle_order_reconcile_api(j)
            self._send(status, ct, body, cors=cors)
            return

        if path == "/recon/order/api/reconcile-anchor":
            try:
                j = json.loads(body_raw)
            except Exception:
                j = {}
            status, ct, body, cors = handle_order_reconcile_anchor_api(j)
            self._send(status, ct, body, cors=cors)
            return

        if path == "/recon/shipping/api/reconcile":
            try:
                j = json.loads(body_raw)
            except Exception:
                j = {}
            status, ct, body, cors = handle_shipping_reconcile_api(j)
            self._send(status, ct, body, cors=cors)
            return

        if path == "/recon/shipping/api/reconcile-anchor":
            try:
                j = json.loads(body_raw)
            except Exception:
                j = {}
            status, ct, body, cors = handle_shipping_reconcile_anchor_api(j)
            self._send(status, ct, body, cors=cors)
            return

        if path == "/recon/withdrawals/api/reconcile":
            try:
                j = json.loads(body_raw)
            except Exception:
                j = {}
            status, ct, body, cors = handle_withdrawals_reconcile_api(j)
            self._send(status, ct, body, cors=cors)
            return

        if path == "/recon/withdrawals/api/reconcile-anchor":
            try:
                j = json.loads(body_raw)
            except Exception:
                j = {}
            status, ct, body, cors = handle_withdrawals_reconcile_anchor_api(j)
            self._send(status, ct, body, cors=cors)
            return

        if path == "/recon/refunds/api/reconcile":
            try:
                j = json.loads(body_raw)
            except Exception:
                j = {}
            status, ct, body, cors = handle_refunds_reconcile_api(j)
            self._send(status, ct, body, cors=cors)
            return

        if path == "/recon/refunds/api/reconcile-anchor":
            try:
                j = json.loads(body_raw)
            except Exception:
                j = {}
            status, ct, body, cors = handle_refunds_reconcile_anchor_api(j)
            self._send(status, ct, body, cors=cors)
            return

        self._send(405, "application/json", b'{"error":"Method not allowed"}')

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def _serve_recon_page(self, html):
        """Serve a recon portal HTML page with the Log out chip injected."""
        if isinstance(html, str):
            html = html.encode("utf-8")
        # Staging instance: inject a prominent banner so test data is never
        # mistaken for production (env STAGING=1 on the staging dashboard only).
        if os.environ.get("STAGING") == "1":
            import re as _re
            m = _re.search(rb"<body[^>]*>", html)
            if m:
                banner = (b'<div style="position:sticky;top:0;z-index:9999;background:#B45309;color:#fff;'
                          b'text-align:center;padding:7px 12px;font:700 13px/1.4 system-ui,sans-serif;'
                          b'letter-spacing:.5px;">RECON PORTAL (STAGING) &mdash; TEST DATA ONLY, not production</div>')
                html = html[:m.end()] + banner + html[m.end():]
        idx = html.rfind(b"</body>")
        if idx != -1:
            html = html[:idx] + RECON_LOGOUT_CHIP + html[idx:]
        self._send(200, "text/html; charset=utf-8", html)

    def _recon_cookie(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k.strip() == "recon_session":
                return v
        return None

    def _recon_session_valid(self):
        """True if recon_session cookie is a valid signed employee session for Finance."""
        if not AUTH_SECRET:
            return False
        tok = self._recon_cookie()
        if not tok:
            return False
        session = _validate_session(tok)
        if not session:
            return False
        return _can_access(session.get("email", ""), "recon")

    def _recon_bearer_valid(self):
        """True if Authorization: Bearer token is a valid signed Finance employee session.
        Also sets self._pending_cookie so the response includes Set-Cookie recon_session."""
        if not AUTH_SECRET:
            return False
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        token = auth[7:]
        session = _validate_session(token)
        if not session:
            return False
        if not _can_access(session.get("email", ""), "recon"):
            return False
        # Set pending cookie so subsequent requests use cookie (no Bearer needed)
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "http") == "https" else ""
        self._pending_cookie = (
            f"recon_session={token}; Path=/recon; HttpOnly; SameSite=Lax; "
            f"Max-Age={RECON_SESSION_TTL}{secure}"
        )
        return True

    def _recon_rate_limited(self):
        rec = _recon_attempts.get(self.client_address[0])
        return bool(rec and rec[1] > time.time())

    def _recon_register_fail(self):
        ip = self.client_address[0]
        now = time.time()
        rec = _recon_attempts.get(ip)
        if not rec or rec[1] < now:
            _recon_attempts[ip] = [1, 0]
        else:
            rec[0] += 1
            if rec[0] >= RECON_MAX_ATTEMPTS:
                rec[1] = now + RECON_LOCKOUT_SEC
                rec[0] = 0
                print(f"[RECON-AUTH] lockout {ip} for {RECON_LOCKOUT_SEC}s")

    def _recon_clear_failures(self):
        _recon_attempts.pop(self.client_address[0], None)

    def _recon_gate(self, path, is_api):
        """True if request passes; otherwise sends login page / 401 and returns False."""
        if self._recon_session_valid():
            return True
        if self._recon_bearer_valid():
            return True
        if is_api:
            self._send(401, "application/json",
                       b'{"error":"Unauthorized - recon session required"}', cors=True)
        else:
            self._send(200, "text/html; charset=utf-8",
                       RECON_LOGIN_PAGE.replace("__ERROR_BLOCK__", "").encode())
        return False

    def _handle_recon_login(self, body_raw):
        """Handle POST /recon/login — email + PIN, Finance team only."""
        if self._recon_rate_limited():
            self._send(429, "text/html; charset=utf-8",
                       RECON_LOGIN_PAGE.replace("__ERROR_BLOCK__",
                           '<div class="err">Too many failed attempts. Try again later.</div>').encode())
            return
        form = parse_qs(body_raw.decode("utf-8")) if body_raw else {}
        email = (form.get("email") or [""])[0].strip().lower()
        pin   = (form.get("pin")   or [""])[0].strip()
        if not email or not pin:
            self._send(400, "text/html; charset=utf-8",
                       RECON_LOGIN_PAGE.replace("__ERROR_BLOCK__",
                           '<div class="err">Email and PIN are required.</div>').encode())
            return
        # Validate employee
        emp = _find_employee(email)
        if not emp or emp.get("pin", "") != pin:
            self._recon_register_fail()
            print(f"[RECON-AUTH] DENY (bad creds) from {self.client_address[0]} email={email}")
            self._send(401, "text/html; charset=utf-8",
                       RECON_LOGIN_PAGE.replace("__ERROR_BLOCK__",
                           '<div class="err">Invalid email or PIN.</div>').encode())
            return
        if emp.get("status", "Active").strip().lower() != "active":
            self._send(403, "text/html; charset=utf-8",
                       RECON_LOGIN_PAGE.replace("__ERROR_BLOCK__",
                           '<div class="err">Account is inactive. Contact admin.</div>').encode())
            return
        # Portal access check (sheet-driven — PortalAccess tab)
        if not _can_access(email, "recon"):
            print(f"[RECON-AUTH] DENY (not finance) from {self.client_address[0]} email={email}")
            self._send(403, "text/html; charset=utf-8",
                       RECON_LOGIN_PAGE.replace("__ERROR_BLOCK__",
                           '<div class="err">Recon access is limited to the Finance team.</div>').encode())
            return
        # Issue signed session token → set cookie
        self._recon_clear_failures()
        print(f"[RECON-AUTH] grant from {self.client_address[0]} email={email}")
        token = _create_session(email, emp.get("name", ""), emp.get("department", ""), emp.get("role", "employee"))
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "http") == "https" else ""
        self.send_response(302)
        self.send_header("Location", self._recon_loc("/recon"))
        self.send_header("Set-Cookie",
            self._recon_cookie_out(
                f"recon_session={token}; Path=/recon; HttpOnly; SameSite=Lax; "
                f"Max-Age={RECON_SESSION_TTL}{secure}"))
        self.end_headers()

    def _send(self, code, ct, body, cors=False):
        # Staging prefix mode: rewrite HTML bodies so every /recon URL inside
        # the page (JS fetches, form action, logout link) carries the prefix.
        # Boundary-aware: only URL paths (/recon followed by /, quote, ? or end),
        # never substrings like "reconcile".
        if getattr(self, "_prefix_mode", False) and RECON_URL_PREFIX and ct.startswith("text/html"):
            if isinstance(body, str):
                body = re.sub(r"/recon(?=/|\"|'|\?|$)", RECON_URL_PREFIX, body)
            else:
                body = re.sub(rb"/recon(?=/|\"|'|\?|$)", RECON_URL_PREFIX.encode(), body)
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(body))
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        # Flush pending Set-Cookie (set by _recon_bearer_valid for Bearer→cookie flow)
        pending = getattr(self, '_pending_cookie', None)
        if pending:
            self.send_header("Set-Cookie", pending)
            self._pending_cookie = None
        if "text/html" in ct:
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass

# ── Threaded Server ──────────────────────────────────────────────────────
class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

# ── Reconciliation Portal Homepage ───────────────────────────────────────

_RECON_HOMEPAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MallPlus Reconciliation Portal</title>
<style>
  :root { --bg: #E0F7F5; --card: #FFFFFF; --border: rgba(0,175,160,.25); --text: #1A1035; --dim: #6B7280; --accent: #00AFA0; --green: #00AFA0; --amber: #C4880A; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family: 'Space Grotesk',system-ui,sans-serif; background: linear-gradient(135deg,#3724ED 0%,#1A9FD8 45%,#00AFA0 100%); background-attachment: fixed; color: var(--text); font-size: 14px; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
  .hero { text-align: center; max-width: 800px; padding: 40px; }
  .hero h1 { font-family: 'Garet','Space Grotesk',sans-serif; font-size: 32px; font-weight: 700; margin-bottom: 8px; color: #fff; }
  .hero .sub { color: rgba(255,255,255,.85); font-size: 16px; margin-bottom: 48px; }
  .cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; }
  @media (max-width: 720px) { .cards { grid-template-columns: 1fr; } }
  .card { background: var(--card); border: 1.5px solid var(--border); border-radius: 16px; padding: 32px 24px; text-decoration: none; color: var(--text); transition: all .2s; display: flex; flex-direction: column; align-items: center; text-align: center; gap: 12px; box-shadow: 0 2px 12px rgba(0,175,160,.10); }
  .card:hover { border-color: var(--accent); transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,175,160,.16); }
  .card .icon { font-size: 40px; }
  .card h2 { font-family: 'Garet','Space Grotesk',sans-serif; font-size: 16px; font-weight: 600; }
  .card p { font-family: 'Quicksand',sans-serif; font-size: 13px; color: var(--dim); line-height: 1.5; }
  .footer { margin-top: 48px; font-size: 12px; color: rgba(255,255,255,.6); }
</style>
</head>
<body>
<div class="hero">
  <h1>💰 MallPlus Reconciliation Portal</h1>
  <p class="sub">Select a download board to extract data for reconciliation</p>
  <div class="cards">
    <a href="/recon/order/" class="card">
      <span class="icon">💳</span>
      <h2>Order Reconciliation</h2>
      <p>Order download board and Xendit/GCash settlement reconciliation.</p>
    </a>
    <a href="/recon/shipping/" class="card">
      <span class="icon">📦</span>
      <h2>Shipping Fee Reconciliation</h2>
      <p>3PL shipment data with parcel details, logistics status, and shipping fees for carrier billing reconciliation.</p>
    </a>
    <a href="/recon/withdrawals/" class="card">
      <span class="icon">🏦</span>
      <h2>Wallet Withdrawal Reconciliation</h2>
      <p>Seller withdrawal requests with bank details and statuses for seller disbursement reconciliation.</p>
    </a>
    <a href="/recon/refunds/" class="card">
      <span class="icon">↩️</span>
      <h2>Refunds Reconciliation</h2>
      <p>Customer refund requests with dates, amounts, reasons, and payment details for refund reconciliation.</p>
    </a>
  </div>
  <div class="footer">FinCom Technologies Inc. — Production DB</div>
</div>
</body>
</html>"""

# ── Main ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("📊 Pre-loading data...")
    fetch_data()
    fetch_activation_data()
    fetch_uat_scenarios()
    fetch_charm_scenarios()
    fetch_ops_scenarios()
    fetch_buyer_seller()
    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"\n🚀 MallPlus Dashboards are LIVE on port {PORT}")
    print(f"   Main Dashboard  → http://localhost:{PORT}")
    print(f"   Activation      → http://localhost:{PORT}/activation")
    print(f"   Reimbursements  → http://localhost:{PORT}/reimbursements")
    print(f"   LAN             → http://192.168.1.71:{PORT}/activation")
    print(f"   (Ctrl+C to stop)\n")
    server.serve_forever()
