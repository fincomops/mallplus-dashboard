"""
Gateway MDR (merchant discount rate) config — Finance-owned, platform cost.

MDR = the gateway's fee charged to Fincom (GCash / Xendit), NOT the fees charged
to sellers. Effective-dated, editable from the recon tool (/recon/gateway-fees).

The config is a local JSON file (mallplus-dashboard/gateway_fees.json) so no
writes to the read-only prod DB are required. recon_api.py injects the generated
SQL CASE expression as a `gateway_mdr` column.
"""
import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway_fees.json")

_cache = {"mtime": None, "cfg": None}

# Order Recon friendly labels -> provider_id predicate
_PROVIDER_PRED = {
    "Xendit": "ps.provider_id = 'pp_xendit'",
    "GCash":  "ps.provider_id IN ('pp_gcash_webpay','pp_gcashmp_glife')",
    "Stripe": "ps.provider_id = 'pp_card_stripe-connect'",
    "System": "ps.provider_id = 'pp_system_default'",
}

# Xendit method label -> raw method value in pmt.data->>'method'
_XENDIT_METHOD = {
    "GCash": "GCASH", "Maya": "MAYA", "Credit Card": "CARD", "Card": "CARD",
    "QRPH": "QRPH", "GrabPay": "GRABPAY", "ShopeePay": "SHOPEEPAY",
    "Direct Debit": "DIRECT_DEBIT",
}


def load_config(force=False):
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return {"rules": []}
    if force or _cache["mtime"] != mtime or _cache["cfg"] is None:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            _cache["cfg"] = json.load(fh)
        _cache["mtime"] = mtime
    return _cache["cfg"]


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    _cache["mtime"] = None
    return load_config(force=True)


def _active_rules(cfg):
    rules = [r for r in cfg.get("rules", []) if r.get("active")]
    # most specific first: provider-specific + method-specific, then wildcards
    rules.sort(key=lambda r: ((r.get("provider") != "*"), (r.get("method") != "*")), reverse=True)
    return rules


def _q(v):
    return str(v).replace("'", "''")


def _provider_pred(provider):
    return _PROVIDER_PRED.get(provider, "false" if provider != "*" else "true")


def _method_pred(provider, method):
    if method == "*":
        return "true"
    if provider == "Xendit":
        val = _XENDIT_METHOD.get(method, method.upper())
        return "COALESCE(pmt.data->>'method','') = '%s'" % _q(val)
    return "true"  # GCash / Stripe rails: method is implicit in the provider


def _date_guard(rule):
    g = []
    if rule.get("start"):
        g.append("o.created_at >= DATE '%s'" % _q(rule["start"]))
    if rule.get("end"):
        g.append("o.created_at < DATE '%s' + INTERVAL '1 day'" % _q(rule["end"]))
    return g


def _fee_expr(rule):
    rate = rule.get("rate_pct")
    mn = rule.get("min_fee") or 0
    fixed = rule.get("fixed_fee") or 0
    proc = rule.get("processing_fee") or 0
    parts = []
    if rate:
        pct = "ROUND(COALESCE(pc.amount, 0) * %s / 100, 2)" % rate
        if mn and mn > 0:
            pct = "GREATEST(%s, %s)" % (pct, mn)
        parts.append(pct)
    elif mn and mn > 0:
        parts.append(str(mn))
    if fixed:
        parts.append(str(fixed))
    if proc:
        parts.append(str(proc))
    if not parts:
        return "NULL::numeric"
    if len(parts) == 1:
        return "(%s)::numeric" % parts[0]
    return "(%s)::numeric" % " + ".join(parts)


def sql_case_expr():
    """SQL CASE computing gateway MDR per order row (uses pc.amount, ps, pmt, o).

    Gate (Shaun 2026-10-04): MDR is only incurred on CAPTURED funds. When
    config `gate_on_capture` is true (default), rows with no capture
    (`COALESCE(pc.captured_amount, 0) <= 0`) return 0 rather than blank — a clean,
    complete column that does not overstate platform cost.
    """
    rules = _active_rules(load_config())
    cfg = load_config()
    whens = []
    for r in rules:
        if not any(r.get(k) for k in ("rate_pct", "min_fee", "fixed_fee", "processing_fee")):
            continue
        conds = [_provider_pred(r.get("provider", "*")), _method_pred(r.get("provider", "*"), r.get("method", "*"))]
        conds += _date_guard(r)
        conds = [c for c in conds if c != "true"]
        cond = " AND ".join(conds) if conds else "true"
        whens.append("WHEN %s THEN %s" % (cond, _fee_expr(r)))
    if not whens:
        return "NULL::numeric"
    inner = "CASE " + " ".join(whens) + " ELSE NULL END"
    if cfg.get("gate_on_capture", True):
        return "CASE WHEN COALESCE(pc.captured_amount, 0) <= 0 THEN 0::numeric ELSE (%s) END" % inner
    return inner


def compute(provider, method, amount):
    """Python parity (used for spot checks / exports)."""
    for r in _active_rules(load_config()):
        if r.get("provider") not in ("*", provider):
            continue
        if r.get("method") not in ("*", method):
            continue
        rate = r.get("rate_pct")
        mn = r.get("min_fee") or 0
        fixed = r.get("fixed_fee") or 0
        proc = r.get("processing_fee") or 0
        base = 0.0
        if rate:
            base = float(amount or 0) * float(rate) / 100.0
        if mn and mn > 0:
            base = max(base, float(mn))
        total = base + float(fixed) + float(proc)
        return round(total, 2)
    return None


# ── Admin portal ─────────────────────────────────────────────
def handle_gateway_fees_api(method, body):
    if method == "GET":
        return 200, "application/json", json.dumps(load_config()).encode(), True
    try:
        cfg = json.loads(body or b"{}")
        if "rules" not in cfg or not isinstance(cfg["rules"], list):
            return 400, "application/json", json.dumps({"error": "rules[] required"}).encode(), True
        save_config(cfg)
        return 200, "application/json", json.dumps({"ok": True}).encode(), True
    except Exception as e:
        return 400, "application/json", json.dumps({"error": str(e)}).encode(), True


def _attr(v):
    if v is None:
        return ""
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("'", "&#39;").replace('"', "&quot;"))


def _gateway_rule_row(r):
    def inp(cls, val, extra=""):
        v = "" if val in (None, "") else val
        return "<input class='%s' value='%s' %s>" % (cls, _attr(v), extra)
    prov = r.get("provider", "*")
    prov_opts = "".join(
        "<option value='%s'%s>%s</option>" % (p, " selected" if p == prov else "", p)
        for p in ["*", "Xendit", "GCash", "Stripe", "System"])
    def num(cls, v):
        v = "" if v in (None, "") else v
        return "<input class='%s' type='number' step='0.01' value='%s'>" % (cls, _attr(v))
    chk = " checked" if r.get("active") else ""
    cls = "" if r.get("active") else " style='opacity:.55'"
    return ("<tr class='rule'%s>"
            "<td>%s</td>"
            "<td><select class='f-provider'>%s</select></td>"
            "<td>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td>"
            "<td><input class='f-active' type='checkbox'%s></td>"
            "<td>%s</td>"
            "<td><button class='del' type='button' onclick='this.closest(\"tr\").remove()'>\u2715</button></td>"
            "</tr>") % (
        cls,
        inp("f-id", r.get("id", "")),
        prov_opts,
        inp("f-method", r.get("method", "*"), "list='methods'"),
        num("f-rate", r.get("rate_pct")),
        num("f-min", r.get("min_fee")),
        num("f-fixed", r.get("fixed_fee")),
        num("f-proc", r.get("processing_fee")),
        inp("f-start", r.get("start", ""), "type='date'"),
        inp("f-end", r.get("end", ""), "type='date'"),
        chk,
        inp("f-note", r.get("note", "")),
    )


def serve_gateway_fees_portal():
    cfg = load_config()
    rows = "".join(_gateway_rule_row(r) for r in cfg.get("rules", []))
    src = cfg.get("source", "")
    notes = "\n".join(cfg.get("notes", []))
    gate = "checked" if cfg.get("gate_on_capture", True) else ""
    methods = ["*", "GCash", "Maya", "Credit Card", "QRPH", "GrabPay", "ShopeePay", "Direct Debit"]
    tmpl = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gateway MDR Config</title>
<style>
 body{font-family:-apple-system,Segoe UI,sans-serif;background:#0f1115;color:#e6e9ef;margin:0;padding:24px}
 h1{font-size:20px;margin:0 0 4px} .sub{color:#8b93a7;font-size:13px;margin-bottom:16px}
 .wrap{max-width:1500px;margin:0 auto}
 table{width:100%;border-collapse:collapse;font-size:13px;background:#161922;border-radius:8px;overflow:hidden}
 th,td{padding:6px 8px;text-align:left;border-bottom:1px solid #232734;vertical-align:middle;white-space:nowrap}
 th{background:#1d2230;color:#aab2c5;font-weight:600;position:sticky;top:0;font-size:12px}
 tr.rule:hover td{background:#1a1e29}
 input,select{background:#0b0d12;color:#e6e9ef;border:1px solid #2b3040;border-radius:6px;padding:5px 7px;font-size:12px;font-family:inherit;width:100%;box-sizing:border-box}
 input[type=number]{width:82px} input[type=date]{width:140px} input[type=checkbox]{width:auto}
 input:focus,select:focus{outline:none;border-color:#2b6ef6;background:#0e1220}
 .f-id{width:120px} .f-method{width:120px} .f-note{width:230px}
 .del{background:#3a1d24;color:#f28b8b;border:1px solid #5a2a33;border-radius:6px;padding:5px 9px;cursor:pointer;font-size:13px}
 .del:hover{background:#5a2a33;color:#fff}
 .note{background:#161922;border:1px solid #232734;border-radius:8px;padding:12px 16px;margin:16px 0;font-size:13px;color:#c6ccd8}
 textarea{width:100%;background:#0b0d12;color:#d8deea;border:1px solid #232734;border-radius:8px;padding:10px;font-family:ui-monospace,Menlo,monospace;font-size:12px;box-sizing:border-box}
 button.act{background:#2b6ef6;color:#fff;border:0;border-radius:8px;padding:9px 16px;font-size:14px;cursor:pointer}
 button.act:hover{background:#1f5be0}
 a.back{color:#8b93a7;font-size:13px;text-decoration:none} .ok{color:#4ade80;margin-left:6px} .err{color:#f28b8b;margin-left:6px}
 .bar{display:flex;align-items:center;gap:10px;margin-top:14px;flex-wrap:wrap}
 .flag{display:flex;align-items:center;gap:8px;font-size:13px;color:#c6ccd8;background:#161922;border:1px solid #232734;border-radius:8px;padding:10px 14px;margin-top:14px;max-width:720px}
 .hint{color:#8b93a7;font-size:12px;margin-top:4px}
 .savetop{position:sticky;bottom:0;background:linear-gradient(0deg,#0f1115,rgba(15,17,21,.7));padding:12px 0 4px;display:flex;gap:10px;align-items:center;margin-top:10px}
 .scroll{overflow-x:auto;border-radius:8px}
</style></head><body><div class="wrap">
<h1>Gateway MDR — config (platform cost)</h1>
<div class="sub">Gateway fees charged to Fincom — <b>separate</b> from seller fees. <b>Edit any cell, then click Save.</b> Effective-dated; feeds the Order Recon “Gateway MDR” column. <a class="back" href="/recon/order">← Order Recon</a></div>
<div class="note"><b>Source:</b> __SRC__<br><span class="hint">Rate = % of captured amount · Min floor = minimum charge · Fixed + Processing = flat &#8369; add-ons (e.g. Xendit &#8369;11). Leave blank for none. Method “*” = any method on that provider.</span></div>
<div class="scroll"><table id="tbl"><thead><tr>
<th>ID</th><th>Provider</th><th>Method</th><th>Rate %</th><th>Min floor &#8369;</th><th>Fixed &#8369;</th><th>Processing &#8369;</th><th>Start</th><th>End</th><th>Active</th><th>Note</th><th></th>
</tr></thead><tbody id="tbody">__ROWS__</tbody></table></div>
<div class="bar"><button class="act" onclick="addRow()">+ Add rule</button></div>
<label class="flag"><input type="checkbox" id="gate" __GATE__> Gate on capture — return &#8369;0 when the order has no captured amount (recommended)</label>
<div style="margin-top:14px">
 <div class="hint">Source</div><input id="src" value="__SRC_ATTR__" style="max-width:520px">
 <div class="hint" style="margin-top:10px">Notes (one per line)</div><textarea id="notes" rows="3">__NOTES__</textarea>
</div>
<div class="savetop"><button class="act" onclick="save()">Save changes</button><span id="msg"></span></div>
<details style="margin-top:18px"><summary class="hint" style="cursor:pointer">Advanced — raw JSON (current file)</summary>
<textarea id="raw" rows="12" readonly>__CFG__</textarea></details>
<script>
var PROVIDERS=["*","Xendit","GCash","Stripe","System"];
var METHODS=__METHODS_JSON__;
function el(tag,cls,attrs){var e=document.createElement(tag);if(cls)e.className=cls;if(attrs)for(var k in attrs)e.setAttribute(k,attrs[k]);return e;}
function opt(list,val,cls){var s=el("select",cls);list.forEach(function(v){var o=el("option");o.value=v;o.textContent=v;if(v===val)o.selected=true;s.appendChild(o);});return s;}
function addRow(r){
 r=r||{};
 var tb=document.getElementById("tbody");
 var tr=el("tr","rule");
 function td(child){var c=el("td");c.appendChild(child);tr.appendChild(c);}
 td(el("input","f-id",{value:r.id||""}));
 td(opt(PROVIDERS,r.provider||"*","f-provider"));
 td(el("input","f-method",{value:r.method||"*",list:"methods"}));
 function num(cls,v){return el("input",cls,{type:"number",step:"0.01",value:(v===null||v===undefined||v==="")?"":v});}
 td(num("f-rate",r.rate_pct));td(num("f-min",r.min_fee));td(num("f-fixed",r.fixed_fee));td(num("f-proc",r.processing_fee));
 td(el("input","f-start",{type:"date",value:r.start||""}));
 td(el("input","f-end",{type:"date",value:r.end||""}));
 var cb=el("input","f-active",{type:"checkbox"});cb.checked=r.active!==false;td(cb);
 td(el("input","f-note",{value:r.note||""}));
 var b=el("button","del",{type:"button"});b.textContent="\u2715";b.onclick=function(){tr.remove();};var c2=el("td");c2.appendChild(b);tr.appendChild(c2);
 tb.appendChild(tr);
}
function numv(v){return v===""||v===null?null:Number(v);}
function collect(){
 var rules=[].slice.call(document.querySelectorAll("tr.rule")).map(function(tr){
  var q=function(s){return tr.querySelector(s);};
  return {id:q(".f-id").value.trim(),provider:q(".f-provider").value,method:(q(".f-method").value.trim()||"*"),
   rate_pct:numv(q(".f-rate").value),min_fee:numv(q(".f-min").value),fixed_fee:numv(q(".f-fixed").value),
   processing_fee:numv(q(".f-proc").value),start:q(".f-start").value||null,end:q(".f-end").value||null,
   active:q(".f-active").checked,note:q(".f-note").value.trim()};
 });
 return {rules:rules,gate_on_capture:document.getElementById("gate").checked,
   source:document.getElementById("src").value,notes:document.getElementById("notes").value.split("\\n").filter(Boolean)};
}
function apiBase(){return location.pathname.replace(/\\/$/,"")+"/api";}
function save(){
 var m=document.getElementById("msg");
 fetch(apiBase(),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(collect())})
 .then(function(r){return r.json();}).then(function(j){
   if(j.ok){m.className="ok";m.textContent="Saved \u2705";document.getElementById("raw").value=JSON.stringify(collect(),null,2);}
   else{m.className="err";m.textContent="Error: "+(j.error||"?");}
 }).catch(function(e){m.className="err";m.textContent="Error: "+e;});
}
</script>
<datalist id="methods">__METHODS__</datalist>
</div></body></html>"""
    return (tmpl.replace("__SRC_ATTR__", _attr(src))
               .replace("__SRC__", _html(src))
               .replace("__NOTES__", _html(notes))
               .replace("__ROWS__", rows)
               .replace("__GATE__", gate)
               .replace("__CFG__", _html(json.dumps(cfg, indent=2)))
               .replace("__METHODS_JSON__", json.dumps(methods))
               .replace("__METHODS__", "".join("<option value='%s'>" % _attr(m) for m in methods)))


def _html(v):
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
