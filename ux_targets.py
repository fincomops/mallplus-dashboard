"""MallPlus UX Scorecard — portal-managed TARGETS config.

Mirrors the Gateway MDR config pattern (gateway_fees.py): a versioned,
effective-dated JSON file owned by the portal, edited through an in-portal
config page. Forward-only effectivity: a saved change applies at the next week
boundary and never restates a past week.

Owner-scoped editing:
    CS         -> ish@fincom.asia   (Ish Castaneda)
    Logistics  -> ralph@fincom.asia (Ralph Santos)
    Payments   -> shaun@fincom.asia (Shaun Ochia)
Admins (can edit any pillar): see _meta.admins.

Security: the module is served by the local recon dashboard and reached ONLY
through the FCOS proxy, which injects the caller's identity (X-FCOS-User) and a
shared secret (X-FCOS-Secret). Direct access without the secret is refused.
"""
import json
import os
from datetime import date, datetime, timedelta

_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.environ.get("UX_TARGETS_CONFIG", os.path.join(_DIR, "ux_targets.json"))
AUDIT_PATH = os.environ.get("UX_TARGETS_AUDIT", os.path.join(_DIR, "ux_targets_audit.jsonl"))
SECRET = os.environ.get("UX_TARGETS_SECRET", "uxt_fcos_2026_9f3a")  # shared with FCOS


# ── storage ──────────────────────────────────────────────────────────────
def load_config():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"_meta": {"owners": {}, "owner_names": {}, "admins": [], "notes": []}, "rules": []}


def save_config(cfg):
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, CFG_PATH)


def _audit(entry):
    try:
        entry["at"] = datetime.now().isoformat(timespec="seconds")
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ── helpers ──────────────────────────────────────────────────────────────
def _parse_date(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


def week_boundary(ref=None):
    """The coming Sunday (ISO). Weeks end Sunday; a change takes effect no
    earlier than this boundary, so no already-scored week is ever restated."""
    d = ref or date.today()
    return (d + timedelta(days=(6 - d.weekday()))).isoformat()


def _norm_target(v):
    if v is None:
        return ""
    return str(v).strip()


def editable_pillars(email, meta):
    email = (email or "").strip().lower()
    if email and email in [a.lower() for a in meta.get("admins", [])]:
        return set(meta.get("owners", {}).keys())
    return {p for p, e in meta.get("owners", {}).items() if (e or "").lower() == email}


def _rule_sig(r):
    return (r.get("card", ""), r.get("pillar", ""), r.get("metric", ""),
            _norm_target(r.get("target")), (r.get("effective_from") or "").strip(),
            bool(r.get("active", True)), (r.get("note") or "").strip())


# ── API ──────────────────────────────────────────────────────────────────
def handle_ux_targets_api(method, body, email=""):
    cfg = load_config()
    meta = cfg.get("_meta", {})
    if method == "GET":
        return 200, "application/json", json.dumps({
            "ok": True,
            "config": cfg,
            "user": email,
            "editable_pillars": sorted(editable_pillars(email, meta)),
            "week_boundary": week_boundary(),
        }).encode(), True

    # POST — save
    try:
        incoming = json.loads((body or b"{}").decode("utf-8") if isinstance(body, (bytes, bytearray)) else (body or "{}"))
    except Exception as e:
        return 400, "application/json", json.dumps({"ok": False, "error": f"bad JSON: {e}"}).encode(), True

    if not isinstance(incoming.get("rules"), list):
        return 400, "application/json", json.dumps({"ok": False, "error": "rules[] required"}).encode(), True

    owners = meta.get("owners", {})
    allowed = editable_pillars(email, meta)
    min_eff = _parse_date(week_boundary())
    old = {(r.get("id"), (r.get("effective_from") or "").strip()): r for r in cfg.get("rules", [])}

    new_rules = []
    seen = set()
    for r in incoming["rules"]:
        rid = (r.get("id") or "").strip()
        pillar = (r.get("pillar") or "").strip()
        eff = (r.get("effective_from") or "").strip()
        if not rid or not pillar:
            return 400, "application/json", json.dumps({"ok": False, "error": "each rule needs id + pillar"}).encode(), True
        if pillar not in owners:
            return 400, "application/json", json.dumps({"ok": False, "error": f"unknown pillar '{pillar}'"}).encode(), True
        if (rid, eff) in seen:
            return 400, "application/json", json.dumps({"ok": False, "error": f"duplicate rule {rid} @ {eff}"}).encode(), True
        seen.add((rid, eff))

        changed = (rid, eff) not in old or _rule_sig(r) != _rule_sig(old[(rid, eff)])
        if changed:
            if pillar not in allowed:
                return 403, "application/json", json.dumps({
                    "ok": False, "error": f"you can only edit your own pillar ({sorted(allowed) or 'none'}); '{pillar}' is not yours"
                }).encode(), True
            d = _parse_date(eff)
            if d is None:
                return 400, "application/json", json.dumps({"ok": False, "error": f"rule {rid}: invalid Effective from"}).encode(), True
            if d < min_eff:
                return 400, "application/json", json.dumps({
                    "ok": False,
                    "error": f"rule {rid}: Effective from {eff} is in the past. Forward-only — use {week_boundary()} or later (past weeks are never restated)."
                }).encode(), True
        new_rules.append({
            "id": rid, "card": (r.get("card") or "").strip(), "pillar": pillar,
            "metric": (r.get("metric") or "").strip(), "target": _norm_target(r.get("target")),
            "effective_from": eff, "active": bool(r.get("active", True)),
            "note": (r.get("note") or "").strip(),
        })

    # deletions: any old future row the user owns may be removed; past rows are frozen
    for (rid, eff), r in old.items():
        if (rid, eff) in seen:
            continue
        d = _parse_date(eff)
        if d and d < min_eff:
            return 400, "application/json", json.dumps({
                "ok": False, "error": f"cannot delete historical rule {rid} @ {eff} (frozen)"
            }).encode(), True
        if r.get("pillar") not in allowed:
            return 403, "application/json", json.dumps({
                "ok": False, "error": f"cannot delete rule {rid}: '{r.get('pillar')}' is not your pillar"
            }).encode(), True

    cfg["rules"] = new_rules
    save_config(cfg)
    _audit({"by": email, "allowed": sorted(allowed), "count": len(new_rules),
            "changed": [k[0] + "@" + k[1] for k in seen
                        if (k not in old) or _rule_sig(next((x for x in new_rules if (x["id"], x["effective_from"]) == k), {})) != _rule_sig(old[k])]})
    return 200, "application/json", json.dumps({"ok": True}).encode(), True


# ── portal page ────────────────────────────────────────────────────────────
def _attr(v):
    if v is None:
        return ""
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("'", "&#39;").replace('"', "&quot;"))


def _html(v):
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def serve_ux_targets_portal(email=""):
    cfg = load_config()
    meta = cfg.get("_meta", {})
    allowed = editable_pillars(email, meta)
    owners = meta.get("owners", {})
    names = meta.get("owner_names", {})
    boundary = week_boundary()
    rules = sorted(cfg.get("rules", []),
                   key=lambda r: (r.get("card", ""), r.get("metric", ""), r.get("effective_from", "")))

    rows = []
    for r in rules:
        pil = r.get("pillar", "")
        own = pil in allowed
        ro = "" if own else " disabled"
        trcls = "rule" if own else "rule ro"
        rows.append(
            "<tr class='%s' data-pillar='%s'>"
            "<td class='k'>%s</td>"
            "<td>%s</td>"
            "<td><span class='ow%s'>%s</span></td>"
            "<td class='m'>%s</td>"
            "<td><input class='f-target' value='%s'%s></td>"
            "<td><input class='f-eff' type='date' value='%s'%s></td>"
            "<td><input class='f-note' value='%s'%s></td>"
            "<td style='text-align:center'><input class='f-active' type='checkbox'%s%s></td>"
            "<td>%s</td></tr>" % (
                trcls, _attr(pil),
                _attr(r.get("id", "")),
                _attr(r.get("card", "")),
                "" if own else " lock",
                _attr(names.get(pil, pil)),
                _attr(r.get("metric", "")),
                _attr(r.get("target", "")), ro,
                _attr(r.get("effective_from", "")), ro,
                _attr(r.get("note", "")), ro,
                " checked" if r.get("active", True) else "", ro,
                "" if own else "<span>\U0001F512</span>",
            ))

    owner_line = " \u00b7 ".join("<b>%s</b> \u2192 %s" % (_html(p), _html(n)) for p, n in sorted(names.items()))
    you = "admin (all pillars)" if set(owners.keys()) <= allowed else (", ".join(sorted(allowed)) or "read-only")
    notes = "<br>".join(_html(x) for x in meta.get("notes", []))
    tmpl = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UX Scorecard \u2014 Targets</title>
<style>
 :root{--bg:#f4f4f9;--ink:#1c1c28;--muted:#6b6b80;--line:#e6e6ef;--indigo:#3D2FD4}
 body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);padding:24px}
 .wrap{max-width:1400px;margin:0 auto}
 .tabs{display:flex;gap:6px;margin:16px 0 12px;border-bottom:2px solid var(--line);font-family:"Space Grotesk",-apple-system,"Segoe UI",sans-serif}
 .tabs a{padding:10px 20px;font-size:14px;text-decoration:none;color:var(--muted);font-weight:600}
 .tabs a.on{color:var(--indigo);border-bottom:2px solid var(--indigo);margin-bottom:-2px;font-weight:700}
 h1{font-size:22px;margin:10px 0 4px}
 .sub{color:var(--muted);font-size:13px;margin-bottom:2px}
 .who{color:var(--muted);font-size:12px;margin:10px 0 14px}
 .note{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:14px 0;font-size:13px;color:#3a3a4a}
 table{width:100%;border-collapse:collapse;font-size:13px;background:#fff;border-radius:10px;overflow:hidden;border:1px solid var(--line)}
 th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle;white-space:nowrap}
 th{background:#ececf4;color:#3a3a4a;font-weight:700;font-size:12px}
 tr.rule:hover td{background:#f7f7fb} tr.ro td{background:#fafafd}
 input{background:#fff;color:var(--ink);border:1px solid #cfcfdd;border-radius:6px;padding:5px 7px;font-size:12px;font-family:inherit;width:100%;box-sizing:border-box}
 input[type=date]{width:140px} input:disabled{background:#f0f0f6;color:#8b8b9b}
 input:focus{outline:none;border-color:var(--indigo)}
 .f-target{width:110px}.f-note{width:230px}.k{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:#6b6b80}.m{color:#3a3a4a}
 .ow{font-weight:600}.ow.lock{color:#8b8b9b;font-weight:400}
 .scroll{overflow-x:auto;border-radius:10px}
 button.act{background:var(--indigo);color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:14px;cursor:pointer;font-weight:600}
 button.act:hover{filter:brightness(.93)}
 .savetop{position:sticky;bottom:0;background:linear-gradient(0deg,var(--bg),rgba(244,244,249,.7));padding:12px 0 4px;display:flex;gap:10px;align-items:center;margin-top:10px}
 .ok{color:#0e9f6e;font-weight:600}.err{color:#e02424;font-weight:600}
</style></head><body><div class="wrap">
<nav class="tabs">
 <a href="/opsdecks/live/ux-scorecard">Scorecard</a>
 <a class="on" href="/opsdecks/live/ux-scorecard/targets">\u2699 Targets</a>
</nav>
<h1>UX Scorecard \u2014 Targets</h1>
<div class="sub"><b>Effective-dated &amp; forward-only.</b> Set a new target for your pillar; it applies from the <b>Effective from</b> date (must be __BOUNDARY__ or later). Past weeks are never restated.</div>
<div class="who">Owners: __OWNERLINE__ &nbsp;\u00b7&nbsp; You are editing as <b>__EMAIL__</b> (__YOU__).</div>
<div class="note">__NOTES__</div>
<div class="scroll"><table><thead><tr>
<th>Key</th><th>Card</th><th>Pillar (owner)</th><th>Metric</th><th>Target</th><th>Effective from</th><th>Note</th><th>Active</th><th></th>
</tr></thead><tbody id="tbody">__ROWS__</tbody></table></div>
<div class="savetop"><button class="act" onclick="save()">Save changes</button><span id="msg"></span></div>
<script>
function collect(){
 return [].slice.call(document.querySelectorAll("tr.rule")).map(function(tr){
  var q=function(s){return tr.querySelector(s);};
  return {id:tr.children[0].textContent.trim(),card:tr.children[1].textContent.trim(),
   pillar:tr.getAttribute("data-pillar"),metric:tr.children[3].textContent.trim(),
   target:q(".f-target").value.trim(),effective_from:q(".f-eff").value,
   note:q(".f-note").value.trim(),active:q(".f-active").checked};
 });
}
function save(){
 var m=document.getElementById("msg");
 fetch("targets/api",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({rules:collect()})})
 .then(function(r){return r.json();}).then(function(j){
   if(j.ok){m.className="ok";m.textContent="Saved \u2705 (takes effect from the effective date)";}
   else{m.className="err";m.textContent="Error: "+(j.error||"?");}
 }).catch(function(e){m.className="err";m.textContent="Error: "+e;});
}
</script>
</div></body></html>"""
    return (tmpl.replace("__BOUNDARY__", boundary)
                .replace("__OWNERLINE__", owner_line)
                .replace("__EMAIL__", _html(email or "\u2014"))
                .replace("__YOU__", _html(you))
                .replace("__NOTES__", notes)
                .replace("__ROWS__", "".join(rows)))
