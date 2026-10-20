#!/usr/bin/env python3
"""Payroll runs — read-only endpoint for FCOS.

The payroll engine runs in the Finance workspace (`workspace-finance/payroll/
run_cutoff.py`) and writes `payroll/runs/<period>_<cutoff>/run.json`. This
module serves those artefacts over HTTP so the FCOS payroll page can render a
register + GL journal without the engine living inside the web app.

Read-only by design: FCOS never computes payroll. Single writer per concern.

Reached only through the FCOS proxy, which supplies X-FCOS-Secret and X-FCOS-User.
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime

SECRET = os.environ.get("PAYROLL_SECRET", "pay_fcos_2026_3b7e")
RUNS_DIR = os.environ.get(
    "PAYROLL_RUNS_DIR",
    os.path.expanduser("~/.openclaw/workspace-finance/payroll/runs"),
)
CORS = {"Access-Control-Allow-Origin": "*"}
ENGINE = os.environ.get(
    "PAYROLL_ENGINE",
    os.path.expanduser("~/.openclaw/workspace-finance/payroll/run_from_csv.py"),
)
MAX_UPLOAD = 2 * 1024 * 1024


def _load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def list_runs():
    """Every completed run, newest first (folder name = <period>_<cutoff>)."""
    out = []
    try:
        names = sorted(os.listdir(RUNS_DIR), reverse=True)
    except FileNotFoundError:
        return out
    for name in names:
        path = os.path.join(RUNS_DIR, name, "run.json")
        if not os.path.isfile(path):
            continue
        try:
            payload = _load(path)
        except Exception as e:  # noqa: BLE001 — a broken run must not hide the rest
            out.append({"run": name, "error": str(e)[:200]})
            continue
        period, _, cutoff = name.partition("_")
        out.append({
            "run": name,
            "period": period,
            "cutoff": cutoff or payload.get("cutoff", ""),
            "headcount": payload.get("headcount"),
            "rule_version": payload.get("rule_version"),
            "totals": payload.get("totals", {}),
            "balanced": (payload.get("journal_check") or {}).get("balanced"),
            "tie": payload.get("tie_vs_workbook", {}).get("diff"),
            "generated_at": datetime.fromtimestamp(
                os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M"),
        })
    return out


def handle_payroll_api(method, body, email="", query=None):
    """Returns (status, content_type, body_bytes, cors)."""
    if method != "GET":
        return 405, "application/json", json.dumps(
            {"ok": False, "error": "read-only endpoint"}).encode(), CORS

    runs = list_runs()
    if not runs:
        return 200, "application/json", json.dumps({
            "ok": True,
            "viewer": email,
            "runs": [],
            "message": "No payroll runs yet — nothing in payroll/runs.",
        }).encode(), CORS

    q = query or {}
    name = (q.get("run") or [None])[0]
    if not name:
        first = runs[0]
        name = first.get("run")
    safe = os.path.basename(name)
    path = os.path.join(RUNS_DIR, safe, "run.json")
    if not os.path.isfile(path):
        return 404, "application/json", json.dumps(
            {"ok": False, "error": "run not found", "run": safe, "runs": [r.get("run") for r in runs]}).encode(), CORS

    try:
        payload = _load(path)
    except Exception as e:  # noqa: BLE001
        return 500, "application/json", json.dumps(
            {"ok": False, "error": f"unreadable run: {e}"}).encode(), CORS

    payload.update({
        "ok": True,
        "run": safe,
        "viewer": email,
        "runs": runs,
        "source": "live",
    })
    return 200, "application/json", json.dumps(payload).encode(), CORS


def run_upload(csv_text, period, cutoff, email=""):
    """Price an uploaded attendance CSV by calling the engine (subprocess, no import)."""
    if not period:
        return 400, "application/json", json.dumps(
            {"ok": False, "error": "period is required (e.g. 2026-10-15)"}).encode(), CORS
    if cutoff not in ("first", "second"):
        return 400, "application/json", json.dumps(
            {"ok": False, "error": "cutoff must be first or second"}).encode(), CORS
    raw = (csv_text or "").strip()
    if not raw:
        return 400, "application/json", json.dumps({"ok": False, "error": "empty upload"}).encode(), CORS
    if len(raw.encode()) > MAX_UPLOAD:
        return 413, "application/json", json.dumps(
            {"ok": False, "error": "file larger than 2 MB"}).encode(), CORS
    if not os.path.isfile(ENGINE):
        return 500, "application/json", json.dumps(
            {"ok": False, "error": f"engine not found at {ENGINE}"}).encode(), CORS

    fd, path = tempfile.mkstemp(suffix=".csv", prefix="payroll-upload-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(raw + "\n")
    try:
        proc = subprocess.run(
            [sys.executable, ENGINE, "--attendance", path, "--period", period, "--cutoff", cutoff],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        os.unlink(path)
        return 504, "application/json", json.dumps(
            {"ok": False, "error": "engine timed out after 5 minutes"}).encode(), CORS
    finally:
        if os.path.exists(path):
            os.unlink(path)

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return 422, "application/json", json.dumps({
            "ok": False,
            "error": "upload rejected — nothing was computed",
            "detail": out or err,
        }).encode(), CORS

    run_name = f"{period}_{cutoff}"
    payload = {}
    rp = os.path.join(RUNS_DIR, run_name, "run.json")
    if os.path.isfile(rp):
        payload = _load(rp)
    return 200, "application/json", json.dumps({
        "ok": True,
        "run": run_name,
        "ran_by": email,
        "log": out[-2000:],
        "summary": {
            "headcount": payload.get("headcount"),
            "totals": payload.get("totals", {}),
            "journal_check": payload.get("journal_check", {}),
            "warnings": payload.get("warnings", []),
        },
    }).encode(), CORS


def payslip_pdf(run):
    """(status, content_type, bytes, cors) for a run's combined payslip PDF."""
    safe = os.path.basename(run or "")
    path = os.path.join(RUNS_DIR, safe, "payslips.pdf")
    if not os.path.isfile(path):
        return 404, "application/json", json.dumps(
            {"ok": False, "error": "no payslip PDF for that run",
             "hint": "CSV runs generate payslips; workbook runs do not"}).encode(), CORS
    with open(path, "rb") as fh:
        return 200, "application/pdf", fh.read(), CORS


# ── payslip portal (employee self-service + HR/Finance) ─────────────────────
IDENTITY = os.environ.get(
    "PAYROLL_IDENTITY",
    os.path.expanduser("~/.openclaw/workspace-finance/payroll/master/identity_map.json"),
)
PRIVILEGED = {e.strip().lower() for e in os.environ.get(
    "PAYROLL_PRIVILEGED", "shaun@fincom.asia,gen@fincom.asia").split(",") if e.strip()}
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def identity_map():
    try:
        with open(IDENTITY, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {"mapped": [], "unmapped": [], "emails": {}}


def _employee_for(email):
    m = identity_map().get("emails", {}).get((email or "").lower())
    return m


def _runs_with_status():
    out = []
    for entry in list_runs():
        path = os.path.join(RUNS_DIR, entry["run"], "run.json")
        try:
            payload = _load(path)
        except Exception:  # noqa: BLE001
            continue
        entry["status"] = payload.get("status", "draft")
        entry["released_at"] = payload.get("released_at")
        entry["released_by"] = payload.get("released_by")
        entry["has_payslips"] = os.path.isdir(os.path.join(RUNS_DIR, entry["run"], "slips"))
        out.append(entry)
    return out


def payslip_index(email):
    """What this viewer may see. Employees: their released slips only."""
    email = (email or "").lower()
    emp = _employee_for(email)
    privileged = email in PRIVILEGED
    runs = _runs_with_status()
    if not privileged and not emp:
        return {"ok": True, "viewer": email, "role": "unmapped", "slips": [], "runs": [],
                "message": "Your login is not linked to a payroll record yet — contact HR."}

    slips = []
    for r in runs:
        if r["status"] != "released" or not r["has_payslips"]:
            continue
        if privileged:
            slips.append({"run": r["run"], "period": r["period"], "cutoff": r["cutoff"],
                          "released_at": r["released_at"], "headcount": r["headcount"],
                          "net_pay": (r.get("totals") or {}).get("net_pay")})
        else:
            try:
                payload = _load(os.path.join(RUNS_DIR, r["run"], "run.json"))
            except Exception:  # noqa: BLE001
                continue
            line = next((l for l in payload.get("lines", [])
                         if str(l["employee_id"]) == str(emp["payroll_id"])), None)
            if line:
                slips.append({"run": r["run"], "period": r["period"], "cutoff": r["cutoff"],
                              "released_at": r["released_at"], "employee_id": emp["payroll_id"],
                              "name": line["name"], "net_pay": line["net_pay"],
                              "gross": line["total_salary"]})
    return {
        "ok": True, "viewer": email,
        "role": "privileged" if privileged else "employee",
        "employee": emp,
        "slips": slips,
        "runs": [{"run": r["run"], "period": r["period"], "cutoff": r["cutoff"],
                  "status": r["status"], "released_at": r["released_at"],
                  "headcount": r["headcount"]} for r in runs] if privileged else [],
        "privileged": privileged,
    }


def run_roster(run):
    """Employee list for a released run (privileged view)."""
    payload = _load(os.path.join(RUNS_DIR, os.path.basename(run or ""), "run.json"))
    return [{"employee_id": l["employee_id"], "name": l["name"],
             "net_pay": l["net_pay"], "gross": l["total_salary"]}
            for l in payload.get("lines", [])]


def set_release(run, action, email):
    """release | revoke. Stamps who/when + a hash of the register, so a released
    payslip is reproducible. Never touches the money."""
    import hashlib
    safe = os.path.basename(run or "")
    path = os.path.join(RUNS_DIR, safe, "run.json")
    if not os.path.isfile(path):
        return 404, {"ok": False, "error": "run not found"}
    payload = _load(path)
    reg = os.path.join(RUNS_DIR, safe, "register.csv")
    digest = ""
    if os.path.isfile(reg):
        with open(reg, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
    if action == "release":
        if not os.path.isdir(os.path.join(RUNS_DIR, safe, "slips")):
            return 400, {"ok": False, "error": "this run has no payslips (workbook runs cannot be released)"}
        payload.update({"status": "released",
                        "released_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "released_by": email or "",
                        "register_sha256": digest})
    else:
        payload.update({"status": "draft", "released_at": None, "revoked_by": email or ""})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    return 200, {"ok": True, "run": safe, "status": payload["status"],
                 "released_at": payload.get("released_at")}


def payslip_for(run, employee_id, viewer=""):
    """Render (and cache) one employee's payslip PDF. Employees may only fetch their own."""
    safe_run = os.path.basename(run or "")
    emp = os.path.basename(str(employee_id or ""))
    viewer = (viewer or "").lower()
    if viewer not in PRIVILEGED:
        mine = _employee_for(viewer)
        if not mine or str(mine["payroll_id"]) != emp:
            return 403, "application/json", json.dumps(
                {"ok": False, "error": "not your payslip"}).encode(), CORS
    base = os.path.join(RUNS_DIR, safe_run, "slips")
    html_path = os.path.join(base, f"{emp}.html")
    pdf_path = os.path.join(base, f"{emp}.pdf")
    if not os.path.isfile(html_path):
        return 404, "application/json", json.dumps(
            {"ok": False, "error": "payslip not found"}).encode(), CORS
    if not os.path.isfile(pdf_path):
        if not os.path.exists(CHROME):
            return 500, "application/json", json.dumps(
                {"ok": False, "error": "PDF renderer unavailable"}).encode(), CORS
        try:
            subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                            f"--print-to-pdf={pdf_path}", "file://" + html_path],
                           check=True, capture_output=True, timeout=90)
        except Exception as e:  # noqa: BLE001
            return 500, "application/json", json.dumps(
                {"ok": False, "error": f"render failed: {e}"}).encode(), CORS
    with open(pdf_path, "rb") as fh:
        return 200, "application/pdf", fh.read(), CORS
