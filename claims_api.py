"""Claims Reconciliation — MallPlus Recon Portal.

3PL claims (three_pl_claim) for lost / damaged / breached parcels.
Board + CSV export, mirroring the other recon tools (refunds/shipping).

Status = exception type: Lost / Damaged / Breached (from
metadata.package_exception_type, falling back to claim_type mapping).
"""
import json, csv, io
import psycopg2.extras
from recon_db import get_db

# Shared status expression: Lost / Damaged / Breached (Shaun, Sep 2, 2026)
_STATUS_EXPR = """
CASE
    WHEN LOWER(COALESCE(c.metadata->>'package_exception_type','')) = 'lost' THEN 'Lost'
    WHEN LOWER(COALESCE(c.metadata->>'package_exception_type','')) = 'damaged' THEN 'Damaged'
    WHEN LOWER(COALESCE(c.metadata->>'package_exception_type','')) = 'breached' THEN 'Breached'
    WHEN c.claim_type = 'package_lost' THEN 'Lost'
    WHEN c.claim_type = 'package_damaged' THEN 'Damaged'
    WHEN c.claim_type = 'package_breached' THEN 'Breached'
    ELSE 'Unknown'
END
"""

_BASE_SQL = f"""
SELECT
    c.id AS claim_id,
    COALESCE(oe.order_sn, o.id) AS order_id,
    c.created_at AT TIME ZONE 'Asia/Manila' AS claim_date,
    COALESCE(s.name, 'Unknown') AS merchant,
    COALESCE(c.provider, '—') AS provider,
    COALESCE(c.tracking_number, '—') AS tracking_number,
    {_STATUS_EXPR} AS status,
    COALESCE(c.status, '—') AS claim_status,
    COALESCE(c.claim_type, '—') AS claim_type,
    COALESCE((c.metadata->'insurance'->>'is_insured')::text, '') AS is_insured,
    COALESCE((c.metadata->'insurance'->>'insurance_premium')::numeric, 0) AS insurance_premium,
    COALESCE(pc.amount, 0) AS order_payment,
    c.submitted_at AT TIME ZONE 'Asia/Manila' AS submitted_at,
    c.resolved_at AT TIME ZONE 'Asia/Manila' AS resolved_at,
    COALESCE(c.resolution_note, '—') AS resolution_note
FROM public.three_pl_claim c
LEFT JOIN public.order_extension oe ON oe.order_id = c.order_id
LEFT JOIN public.seller s ON s.id = c.seller_id
LEFT JOIN public."order" o ON o.id = c.order_id
LEFT JOIN LATERAL (
    SELECT pc2.amount
    FROM public.order_payment_collection opc2
    JOIN public.payment_collection pc2 ON pc2.id = opc2.payment_collection_id AND pc2.deleted_at IS NULL
    WHERE opc2.order_id = o.id AND opc2.deleted_at IS NULL
    LIMIT 1
) pc ON true
WHERE c.deleted_at IS NULL
"""

_CSV_COL_MAP = {
    "claim_id": "Claim ID", "order_id": "Order #", "claim_date": "Claim Date",
    "merchant": "Merchant", "provider": "Provider", "tracking_number": "Tracking #",
    "status": "Status", "claim_status": "Claim Workflow Status", "claim_type": "Claim Type",
    "order_payment": "Order Payment", "is_insured": "Insured",
    "insurance_premium": "Insurance Premium",
    "submitted_at": "Submitted At", "resolved_at": "Resolved At",
    "resolution_note": "Resolution Note",
}


def _build_where(filters):
    conditions = ["c.deleted_at IS NULL"]
    params = []

    date_from = filters.get("dateFrom", "")
    date_to = filters.get("dateTo", "")
    status = filters.get("status", "")
    search = filters.get("search", "")

    if date_from:
        conditions.append("(c.created_at AT TIME ZONE 'Asia/Manila')::date >= %s")
        params.append(date_from)
    if date_to:
        conditions.append("(c.created_at AT TIME ZONE 'Asia/Manila')::date <= %s")
        params.append(date_to)
    if status:
        conditions.append(f"({_STATUS_EXPR}) = %s")
        params.append(status)
    if search:
        conditions.append("""
            (LOWER(COALESCE(oe.order_sn, o.id)) LIKE LOWER(%s)
             OR LOWER(COALESCE(s.name, '')) LIKE LOWER(%s)
             OR LOWER(COALESCE(c.tracking_number, '')) LIKE LOWER(%s)
             OR LOWER(c.id) LIKE LOWER(%s))
        """)
        st = f"%{search}%"
        params.extend([st, st, st, st])

    return " AND ".join(conditions), params


def _fetch_rows(query_dict):
    page = int(query_dict.get("page", ["1"])[0])
    page_size = int(query_dict.get("page_size", ["50"])[0])
    export_csv = query_dict.get("export", [""])[0] == "csv"

    filters = {
        "dateFrom": query_dict.get("dateFrom", [""])[0],
        "dateTo": query_dict.get("dateTo", [""])[0],
        "status": query_dict.get("status", [""])[0],
        "search": query_dict.get("search", [""])[0],
    }
    where_clause, params = _build_where(filters)

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if export_csv:
        sql = f"{_BASE_SQL} AND {where_clause} ORDER BY c.created_at DESC LIMIT 5000"
        cur.execute(sql, params)
        rows = cur.fetchall()
        conn.close()
        return rows, None, None

    count_sql = f"SELECT COUNT(*) AS total FROM ({_BASE_SQL} AND {where_clause}) sub"
    cur.execute(count_sql, params)
    total = cur.fetchone()["total"]

    data_sql = f"{_BASE_SQL} AND {where_clause} ORDER BY c.created_at DESC LIMIT %s OFFSET %s"
    cur.execute(data_sql, params + [page_size, (page - 1) * page_size])
    rows = cur.fetchall()

    # Stats
    stats_sql = f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE status = 'Lost') AS lost,
               COUNT(*) FILTER (WHERE status = 'Damaged') AS damaged,
               COUNT(*) FILTER (WHERE status = 'Breached') AS breached,
               COUNT(*) FILTER (WHERE claim_status = 'pending') AS pending,
               COUNT(*) FILTER (WHERE is_insured = 'true') AS insured,
               COALESCE(SUM(order_payment), 0) AS total_order_payment
        FROM ({_BASE_SQL} AND {where_clause}) sub
    """
    cur.execute(stats_sql, params)
    stats = cur.fetchone()
    conn.close()
    return rows, total, stats


def _render_csv(rows):
    if not rows:
        return b""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([_CSV_COL_MAP.get(h, h) for h in rows[0].keys()])
    for r in rows:
        writer.writerow([str(r[h]) if r[h] is not None else "" for h in r.keys()])
    return output.getvalue().encode()


def _serialize(rows, stats=None):
    """Convert datetime/Decimal to JSON-safe values (mirrors refunds_api)."""
    from datetime import datetime
    from decimal import Decimal
    out = [dict(r) for r in rows]
    for r in out:
        for k, v in r.items():
            if isinstance(v, datetime):
                r[k] = str(v)
            elif isinstance(v, Decimal):
                r[k] = float(v)
    stats_dict = {}
    if stats:
        stats_dict = dict(stats)
        for k, v in stats_dict.items():
            if isinstance(v, Decimal):
                stats_dict[k] = float(v)
            elif isinstance(v, datetime):
                stats_dict[k] = str(v)
    return out, stats_dict


def handle_claims_api(path, query_dict):
    """API endpoint: JSON rows or CSV export."""
    try:
        rows, total, stats = _fetch_rows(query_dict)
        if query_dict.get("export", [""])[0] == "csv":
            return 200, "text/csv", _render_csv(rows), True
        rows_dict, stats_dict = _serialize(rows, stats)
        return 200, "application/json", json.dumps({
            "rows": rows_dict, "total": total, "stats": stats_dict,
        }).encode(), True
    except Exception as e:
        return 500, "application/json", json.dumps({"error": str(e)}).encode(), True


def serve_claims_portal():
    """Serve the Claims Reconciliation page."""
    return _HTML_TEMPLATE


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claims Reconciliation — MallPlus</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Quicksand:wght@500;600;700&display=swap" rel="stylesheet"/>
<link href="https://fonts.cdnfonts.com/css/garet" rel="stylesheet"/>
<style>
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:'Space Grotesk',system-ui,sans-serif;background:linear-gradient(135deg,#3724ED 0%,#1A9FD8 45%,#00AFA0 100%);background-attachment:fixed;color:var(--dark);font-size:14px;min-height:100vh;}
  :root{--dark:#1A1035;--dim:#5B6B7C;--dimlt:#A0AEC0;--card:#FFFFFF;--border:rgba(0,175,160,.13);--accent:#00AFA0;--teal-dk:#007A73;--red:#EF4444;--amber:#F59E0B;--purple:#7C3AED;--green:#22C55E;--shadow-sm:0 2px 12px rgba(0,175,160,.10);--shadow-md:0 8px 32px rgba(0,175,160,.16);--r-lg:16px;--r-sm:10px;}
  header{background:rgba(255,255,255,.92);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;justify-content:space-between;gap:12px;position:sticky;top:0;z-index:10;}
  header h1{font-family:'Garet','Space Grotesk',sans-serif;font-size:1.15rem;font-weight:700;color:var(--dark);}
  .nav{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
  .badge{background:var(--purple);color:#fff;font-size:.65rem;font-weight:700;letter-spacing:1px;text-transform:uppercase;border-radius:999px;padding:4px 10px;}
  .container{max-width:1400px;margin:0 auto;padding:24px;}
  .btn{display:inline-block;border:none;border-radius:var(--r-sm);padding:9px 18px;font-family:'Quicksand',sans-serif;font-size:.85rem;font-weight:700;cursor:pointer;text-decoration:none;transition:all .15s;}
  .btn-primary{background:var(--accent);color:#fff;}
  .btn-primary:hover{background:var(--teal-dk);}
  .btn-secondary{background:#fff;color:var(--dark);border:1.5px solid var(--border);}
  .btn-secondary:hover{border-color:var(--accent);}
  .btn-sm{padding:6px 12px;font-size:.78rem;}
  .filters{background:#fff;border:1.5px solid var(--border);border-radius:var(--r-lg);box-shadow:var(--shadow-sm);padding:18px;margin-bottom:16px;display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;}
  .filter-group{display:flex;flex-direction:column;gap:5px;}
  .filter-group label{font-family:'Quicksand',sans-serif;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--dim);}
  .filter-group input,.filter-group select{padding:9px 12px;border:1.5px solid var(--border);border-radius:var(--r-sm);font-family:'Space Grotesk',sans-serif;font-size:.85rem;outline:none;background:#fff;}
  .filter-group input:focus,.filter-group select:focus{border-color:var(--accent);}
  .stats{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:16px;}
  .stat-card{background:#fff;border:1.5px solid var(--border);border-radius:var(--r-lg);box-shadow:var(--shadow-sm);padding:14px 20px;flex:1;min-width:130px;}
  .stat-card .value{font-family:'Garet','Space Grotesk',sans-serif;font-size:1.3rem;font-weight:700;color:var(--dark);}
  .stat-card .value.red{color:var(--red);} .stat-card .value.amber{color:var(--amber);} .stat-card .value.purple{color:var(--purple);} .stat-card .value.green{color:var(--green);}
  .stat-card .label{font-family:'Quicksand',sans-serif;font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--dim);margin-top:2px;}
  .table-wrap{background:#fff;border:1.5px solid var(--border);border-radius:var(--r-lg);box-shadow:var(--shadow-sm);overflow:auto;}
  table{width:100%;border-collapse:collapse;min-width:1100px;}
  thead th{background:#F8FAFC;color:var(--dim);font-family:'Quicksand',sans-serif;font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.7px;padding:12px 14px;text-align:left;border-bottom:1.5px solid var(--border);white-space:nowrap;}
  tbody td{padding:11px 14px;border-bottom:1px solid var(--border);font-size:.83rem;vertical-align:middle;}
  tbody tr:hover{background:#F8FAFC;}
  td.amount{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;}
  th.amount{text-align:right;}
  code{background:#F1F5F9;border-radius:6px;padding:2px 6px;font-size:.76rem;color:#334155;}
  .copy-btn{cursor:pointer;opacity:.55;font-size:.8rem;margin-left:2px;}
  .copy-btn:hover{opacity:1;}
  .status{display:inline-block;border-radius:999px;padding:4px 12px;font-size:.72rem;font-weight:700;letter-spacing:.4px;text-transform:uppercase;}
  .status-lost{background:rgba(239,68,68,.12);color:var(--red);}
  .status-damaged{background:rgba(245,158,11,.14);color:var(--amber);}
  .status-breached{background:rgba(124,58,237,.12);color:var(--purple);}
  .status-pending{background:rgba(100,116,139,.12);color:var(--dim);}
  .status-resolved{background:rgba(34,197,94,.14);color:var(--green);}
  .status-unknown{background:rgba(100,116,139,.12);color:var(--dim);}
  .empty{text-align:center;color:var(--dim);padding:40px;font-family:'Quicksand',sans-serif;}
  .loading{padding:40px;text-align:center;color:var(--dim);font-family:'Quicksand',sans-serif;}
  .pagination{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;font-size:.82rem;color:var(--dim);flex-wrap:wrap;gap:10px;}
  .pagination .btns{display:flex;gap:6px;align-items:center;}
  #error{display:none;background:#FDECEA;color:#C0392B;border:1px solid #F5C6C0;border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:.85rem;}
</style>
</head>
<body>
<header>
  <div class="nav"><a href="/recon/" class="btn btn-secondary btn-sm">← Portal Home</a><span class="badge">Claims</span></div>
  <h1>💼 Claims Reconciliation</h1>
  <div class="nav"><span class="badge">Production DB</span></div>
</header>
<div class="container">
  <div class="filters">
    <div class="filter-group"><label>Status</label>
      <select id="status"><option value="">All</option><option value="Lost">Lost</option><option value="Damaged">Damaged</option><option value="Breached">Breached</option></select>
    </div>
    <div class="filter-group"><label>Date From</label><input type="date" id="dateFrom"></div>
    <div class="filter-group"><label>Date To</label><input type="date" id="dateTo"></div>
    <div class="filter-group" style="flex:1;min-width:220px"><label>Search (Order / Merchant / Tracking / Claim ID)</label><input type="text" id="search" placeholder="e.g. order #, merchant, tracking"></div>
    <button class="btn btn-primary" onclick="fetchData()">🔍 Filter</button>
    <button class="btn btn-secondary" onclick="resetFilters()">↺ Reset</button>
    <button class="btn btn-secondary" onclick="exportCSV()">📥 Export CSV</button>
  </div>
  <div id="error"></div>
  <div class="stats" id="stats"></div>
  <div class="table-wrap">
    <div id="loading" class="loading">Loading claims…</div>
    <table id="results" style="display:none">
      <thead><tr>
        <th>Claim ID</th><th>Order #</th><th>Claim Date</th><th>Merchant</th><th>Provider</th><th>Tracking #</th>
        <th>Status</th><th>Workflow</th><th>Claim Type</th><th class="amount">Order Payment</th><th>Insured</th><th class="amount">Insurance Premium</th>
        <th>Submitted At</th><th>Resolved At</th><th>Resolution Note</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
  <div class="pagination" id="pagination" style="display:none"></div>
</div>

<script>
var currentPage=1;
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function fmtNum(n){var v=Number(n||0);return v.toLocaleString('en-PH',{minimumFractionDigits:2,maximumFractionDigits:2});}
function getFilters(){return {status:document.getElementById('status').value,dateFrom:document.getElementById('dateFrom').value,dateTo:document.getElementById('dateTo').value,search:document.getElementById('search').value.trim()};}
function resetFilters(){document.getElementById('status').value='';document.getElementById('dateFrom').value='';document.getElementById('dateTo').value='';document.getElementById('search').value='';fetchData();}
function fetchData(){currentPage=1;loadData();}
function loadData(){var f=getFilters();var p=new URLSearchParams(Object.entries(f).filter(function(kv){return kv[1]!=='';}));p.set('page',currentPage);p.set('page_size',50);
  document.getElementById('error').style.display='none';
  fetch('/recon/claims/api/orders?'+p).then(function(r){return r.json();}).then(function(d){
    if(d.error){document.getElementById('error').textContent='Error: '+d.error;document.getElementById('error').style.display='block';return;}
    document.getElementById('loading').style.display='none';
    document.getElementById('results').style.display='table';
    renderStats(d.stats||{});renderTable(d.rows||[]);renderPagination(d.total||0,currentPage,50);
  }).catch(function(e){document.getElementById('error').textContent='Network error: '+e;document.getElementById('error').style.display='block';});}
function renderStats(s){
  var cards=[
    {v:s.total||0,l:'Total Claims',c:''},
    {v:s.lost||0,l:'Lost',c:'red'},
    {v:s.damaged||0,l:'Damaged',c:'amber'},
    {v:s.breached||0,l:'Breached',c:'purple'},
    {v:s.pending||0,l:'Pending Resolution',c:''},
    {v:'₱'+fmtNum(s.total_order_payment||0),l:'Order Value at Risk',c:''},
    {v:s.insured||0,l:'Insured',c:'green'}
  ];
  document.getElementById('stats').innerHTML=cards.map(function(c){return '<div class="stat-card"><div class="value '+c.c+'">'+c.v+'</div><div class="label">'+c.l+'</div></div>';}).join('');
}
function statusBadge(s){
  var cls=(s||'unknown').toLowerCase();
  var map={'lost':'status-lost','damaged':'status-damaged','breached':'status-breached','pending':'status-pending','resolved':'status-resolved','submitted':'status-pending'};
  return '<span class="status '+(map[cls]||'status-unknown')+'">'+esc(s||'Unknown')+'</span>';
}
function workflowBadge(s){
  var cls=(s||'unknown').toLowerCase();
  var map={'pending':'status-pending','submitted':'status-pending','resolved':'status-resolved','denied':'status-lost'};
  return '<span class="status '+(map[cls]||'status-unknown')+'">'+esc(s||'—')+'</span>';
}
function renderTable(rows){
  var tb=document.getElementById('tbody');
  if(!rows||rows.length===0){tb.innerHTML='<tr><td colspan="15" class="empty">No claims found</td></tr>';return;}
  tb.innerHTML=rows.map(function(r){
    var insured=String(r.is_insured||'').toLowerCase()==='true'?'✅ Yes':'—';
    return '<tr>'+
      '<td><code>'+esc(r.claim_id)+'</code> <span class="copy-btn" data-copy="'+esc(r.claim_id)+'" onclick="copyToClipboard(this)" title="Copy">📋</span></td>'+
      '<td><code>'+esc(r.order_id||'—')+'</code> <span class="copy-btn" data-copy="'+esc(r.order_id||'')+'" onclick="copyToClipboard(this)" title="Copy">📋</span></td>'+
      '<td>'+esc(r.claim_date||'—')+'</td><td>'+esc(r.merchant||'—')+'</td><td>'+esc(r.provider||'—')+'</td>'+
      '<td>'+esc(r.tracking_number||'—')+'</td>'+
      '<td>'+statusBadge(r.status)+'</td>'+
      '<td>'+workflowBadge(r.claim_status)+'</td>'+
      '<td>'+esc(r.claim_type||'—')+'</td>'+
      '<td class="amount">₱'+fmtNum(r.order_payment)+'</td>'+
      '<td>'+insured+'</td>'+
      '<td class="amount">₱'+fmtNum(r.insurance_premium)+'</td>'+
      '<td>'+esc(r.submitted_at||'—')+'</td><td>'+esc(r.resolved_at||'—')+'</td>'+
      '<td style="max-width:220px;overflow:hidden;text-overflow:ellipsis" title="'+esc(r.resolution_note||'')+'">'+esc(r.resolution_note||'—')+'</td>'+
    '</tr>';
  }).join('');
}
function renderPagination(t,p,ps){
  if(t<=ps&&p===1){document.getElementById('pagination').style.display='none';return;}
  document.getElementById('pagination').style.display='flex';
  var tp=Math.max(1,Math.ceil(t/ps));
  document.getElementById('pagination').innerHTML='<div class="info">Showing '+((p-1)*ps+1)+'–'+Math.min(p*ps,t)+' of '+t+' claims</div><div class="btns">'+
    '<button class="btn btn-secondary btn-sm" onclick="goPage(1)" '+(p<=1?'disabled':'')+'>««</button>'+
    '<button class="btn btn-secondary btn-sm" onclick="goPage('+(p-1)+')" '+(p<=1?'disabled':'')+'>« Prev</button>'+
    '<span style="padding:4px 12px">Page '+p+' / '+tp+'</span>'+
    '<button class="btn btn-secondary btn-sm" onclick="goPage('+(p+1)+')" '+(p>=tp?'disabled':'')+'>Next »</button>'+
    '<button class="btn btn-secondary btn-sm" onclick="goPage('+tp+')" '+(p>=tp?'disabled':'')+'>»»</button></div>';
}
function goPage(p){currentPage=p;loadData();}
function copyToClipboard(el){var text=el.getAttribute('data-copy');navigator.clipboard.writeText(text).then(function(){el.textContent='✅';setTimeout(function(){el.textContent='📋';},1500);}).catch(function(){prompt('Copy:',text);});}
function exportCSV(){var p=new URLSearchParams(getFilters());p.delete('page');p.delete('page_size');p.set('export','csv');window.open('/recon/claims/api/orders?'+p,'_blank');}
function getTodayDate(){var today=new Date();var y=today.getFullYear();var m=String(today.getMonth()+1).padStart(2,'0');var d=String(today.getDate()).padStart(2,'0');return y+'-'+m+'-'+d;}
setTimeout(function(){var today=getTodayDate();document.getElementById('dateFrom').value=today;document.getElementById('dateTo').value=today;loadData();},100);
</script>
</body>
</html>"""
