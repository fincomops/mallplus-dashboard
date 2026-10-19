#!/usr/bin/env python3
"""Payout Guard — read-only snapshot endpoint for FCOS.

The detection itself runs in the Finance workspace as a 15-minute cron
(`tools/payout_guard.py`), which writes `memory/payout_guard_snapshot.json`.
This module serves that file over HTTP so FCOS can render the case desk.

Read-only by design: clearing a case writes to the Anomaly Case Ledger and is
handled by FCOS, never here. Single writer per concern.

Reached only through the FCOS proxy, which supplies X-FCOS-Secret and
X-FCOS-User.
"""
import json
import os
from datetime import datetime, timezone

SECRET = os.environ.get("PAYOUT_GUARD_SECRET", "pg_fcos_2026_7c1d")
SNAPSHOT = os.environ.get(
    "PAYOUT_GUARD_SNAPSHOT",
    os.path.expanduser("~/.openclaw/workspace-finance/memory/payout_guard_snapshot.json"),
)
STALE_AFTER_MIN = int(os.environ.get("PAYOUT_GUARD_STALE_MIN", "45"))
CORS = {"Access-Control-Allow-Origin": "*"}


def _age_minutes(iso):
    try:
        ts = datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - ts).total_seconds() / 60, 1)
    except Exception:
        return None


def load_snapshot():
    try:
        with open(SNAPSHOT) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)[:200]}


def handle_payout_guard_api(method, body, email=""):
    """Returns (status, content_type, body_bytes, cors)."""
    if method != "GET":
        return 405, "application/json", json.dumps(
            {"ok": False, "error": "read-only endpoint"}).encode(), CORS

    snap = load_snapshot()
    if snap is None:
        return 200, "application/json", json.dumps({
            "ok": True, "stale": True, "missing": True,
            "viewer": email,
            "message": "No snapshot yet — the guard has not completed a scan.",
        }).encode(), CORS
    if "_error" in snap:
        return 500, "application/json", json.dumps(
            {"ok": False, "error": snap["_error"]}).encode(), CORS

    age = _age_minutes(snap.get("generated_at"))
    snap["stale"] = (age is None) or (age > STALE_AFTER_MIN)
    snap["age_minutes"] = age
    snap["viewer"] = email
    snap["ok"] = True
    return 200, "application/json", json.dumps(snap).encode(), CORS
