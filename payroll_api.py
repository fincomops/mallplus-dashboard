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
from datetime import datetime

SECRET = os.environ.get("PAYROLL_SECRET", "pay_fcos_2026_3b7e")
RUNS_DIR = os.environ.get(
    "PAYROLL_RUNS_DIR",
    os.path.expanduser("~/.openclaw/workspace-finance/payroll/runs"),
)
CORS = {"Access-Control-Allow-Origin": "*"}


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
