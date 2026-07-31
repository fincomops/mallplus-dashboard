"""Reconciliation Portal — Seller Wallet Withdrawal Recon API"""
import json, csv, io
import psycopg2
import psycopg2.extras

DB_CONFIG = {
    "host": "8.216.88.209",
    "port": 5432,
    "user": "mpbi_fcro_so",
    "password": "3a&AuWieNtAgEE97Sw2D8F2",
    "dbname": "mallplus",
}

def get_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    return conn

_BASE_SQL = """
SELECT
    wr.id AS withdrawal_id,
    wr.short_id,
    COALESCE(s.name, 'Unknown') AS seller_name,
    COALESCE(s.email, '—') AS seller_email,
    COALESCE(s.phone, '—') AS seller_phone,
    wr.amount,
    wr.currency_code AS currency,
    wr.status,
    COALESCE(wr.metadata->>'xendit_reference_id', '—') AS xendit_reference,
    COALESCE(wr.bank_details->>'bank_name', '—') AS bank_name,
    COALESCE(wr.bank_details->>'account_number', wr.bank_details->>'masked_account_number', '—') AS account_number,
    COALESCE(wr.bank_details->>'account_holder', '—') AS account_holder,
    COALESCE(wr.external_reference, '—') AS external_reference,
    COALESCE(wr.idempotency_key, '—') AS idempotency_key,
    COALESCE(wr.rejection_reason, '—') AS rejection_reason,
    wr.created_at AT TIME ZONE 'Asia/Manila' AS requested_at,
    wr.processed_at AT TIME ZONE 'Asia/Manila' AS processed_at,
    COALESCE(wa.current_balance, 0) AS wallet_balance
FROM public.withdrawal_request wr
LEFT JOIN public.seller s ON s.id = wr.seller_id AND s.deleted_at IS NULL
LEFT JOIN LATERAL (
    SELECT wa2.current_balance
    FROM public.wallet_account wa2
    WHERE wa2.owner_id = wr.seller_id
      AND wa2.owner_type = 'seller'
      AND wa2.deleted_at IS NULL
    LIMIT 1
) wa ON true
WHERE wr.deleted_at IS NULL
"""

_STATS_SQL = """
SELECT
    COUNT(*) AS total_withdrawals,
    COALESCE(SUM(wr.amount), 0) AS total_amount,
    COALESCE(SUM(CASE WHEN wr.status = 'completed' THEN 1 ELSE 0 END), 0) AS completed_count,
    COALESCE(SUM(CASE WHEN wr.status = 'processing' THEN 1 ELSE 0 END), 0) AS processing_count,
    COALESCE(SUM(CASE WHEN wr.status = 'failed' THEN 1 ELSE 0 END), 0) AS failed_count
FROM public.withdrawal_request wr
LEFT JOIN public.seller s ON s.id = wr.seller_id AND s.deleted_at IS NULL
LEFT JOIN LATERAL (
    SELECT wa2.current_balance
    FROM public.wallet_account wa2
    WHERE wa2.owner_id = wr.seller_id
      AND wa2.owner_type = 'seller'
      AND wa2.deleted_at IS NULL
    LIMIT 1
) wa ON true
WHERE wr.deleted_at IS NULL
"""


def handle_withdrawals_api(path, query_params):
    try:
        date_from = query_params.get("date_from", [""])[0]
        date_to = query_params.get("date_to", [""])[0]
        status = query_params.get("status", [""])[0]
        seller = query_params.get("seller", [""])[0]
        search = query_params.get("search", [""])[0].strip()
        page = int(query_params.get("page", ["1"])[0])
        page_size = int(query_params.get("page_size", ["50"])[0])
        export_csv = query_params.get("export", [""])[0] == "csv"

        conditions = []
        params = []

        if date_from:
            conditions.append("wr.created_at >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("wr.created_at < %s::date + interval '1 day'")
            params.append(date_to)
        if status:
            conditions.append("wr.status = %s")
            params.append(status)
        if seller:
            conditions.append("s.name ILIKE %s")
            params.append(f"%{seller}%")
        if search:
            conditions.append(
                "(wr.id ILIKE %s OR wr.short_id ILIKE %s OR s.name ILIKE %s "
                "OR s.email ILIKE %s OR wr.external_reference ILIKE %s)"
            )
            params.extend([f"%{search}%"] * 5)

        extra_where = " AND ".join(conditions) if conditions else "true"

        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if export_csv:
            data_sql = f"{_BASE_SQL} AND {extra_where} ORDER BY wr.created_at DESC LIMIT 5000"
            cur.execute(data_sql, params)
            rows = cur.fetchall()
            if not rows:
                return 200, "text/csv", b"", True
            output = io.StringIO()
            writer = csv.writer(output)
            # nice column names
            col_map = {
                "withdrawal_id": "Withdrawal ID",
                "short_id": "Short ID",
                "seller_name": "Seller",
                "seller_email": "Email",
                "seller_phone": "Phone",
                "amount": "Amount",
                "currency": "Currency",
                "status": "Status",
                "xendit_reference": "Xendit Reference",
                "bank_name": "Bank",
                "account_number": "Account Number",
                "account_holder": "Account Holder",
                "external_reference": "External Ref",
                "idempotency_key": "Idempotency Key",
                "rejection_reason": "Rejection Reason",
                "requested_at": "Requested At",
                "processed_at": "Processed At",
                "wallet_balance": "Wallet Balance",
            }
            headers = [col_map.get(d[0], d[0]) for d in cur.description]
            writer.writerow(headers)
            for r in rows:
                writer.writerow([str(v) if v is not None else "" for v in r.values()])
            return 200, "text/csv", output.getvalue().encode(), True

        # Count
        count_sql = f"SELECT COUNT(*) AS total FROM ({_BASE_SQL} AND {extra_where}) sub"
        cur.execute(count_sql, params)
        total = cur.fetchone()["total"]

        # Data
        data_sql = f"{_BASE_SQL} AND {extra_where} ORDER BY wr.created_at DESC LIMIT %s OFFSET %s"
        offset = (page - 1) * page_size
        cur.execute(data_sql, params + [page_size, offset])
        rows = _serialize(cur.fetchall())

        # Stats
        stats_sql = f"{_STATS_SQL} AND {extra_where}"
        cur.execute(stats_sql, params)
        stats = _serialize([cur.fetchone()])[0]

        # Seller list for dropdown
        cur.execute("SELECT name FROM seller WHERE deleted_at IS NULL ORDER BY name")
        sellers = [r["name"] for r in cur.fetchall()]

        body = json.dumps({
            "rows": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "stats": stats,
            "sellers": sellers,
        })
        return 200, "application/json", body.encode(), True

    except Exception as e:
        return 500, "application/json", json.dumps({"error": str(e)}).encode(), True


def _serialize(rows):
    result = []
    for r in rows:
        row = {}
        for k, v in dict(r).items():
            if v is None:
                row[k] = None
            elif type(v).__name__ == "Decimal":
                row[k] = float(v)
            elif hasattr(v, "isoformat"):
                row[k] = v.strftime("%Y-%m-%d %H:%M")
            else:
                row[k] = v
        result.append(row)
    return result


def serve_withdrawals_portal():
    import os
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "withdrawals_portal.html")
    if os.path.exists(html_path):
        with open(html_path, "rb") as f:
            return f.read()
    return _WITHDRAWALS_HTML.encode()


_WITHDRAWALS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wallet Withdrawal Reconciliation — MallPlus</title>
<style>
  :root { --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a; --text: #e1e4ed; --dim: #8b8fa3; --accent: #6c8cff; --green: #3ccd5c; --red: #ff4757; --amber: #ffa502; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); font-size: 13px; min-height: 100vh; }
  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
  header h1 { font-size: 18px; font-weight: 600; }
  header .nav { display: flex; gap: 8px; align-items: center; }
  header .badge { font-size: 11px; color: var(--accent); }
  .container { max-width: 1900px; margin: 0 auto; padding: 16px 24px; }
  .filters { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 16px; display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; }
  .filter-group { display: flex; flex-direction: column; gap: 4px; }
  .filter-group label { font-size: 11px; color: var(--dim); text-transform: uppercase; letter-spacing: .5px; }
  .filter-group input, .filter-group select { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 8px 12px; border-radius: 6px; font-size: 13px; min-width: 160px; }
  .filter-group input:focus, .filter-group select:focus { outline: none; border-color: var(--accent); }
  .btn { padding: 8px 20px; border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer; border: none; transition: all .15s; }
  .btn-primary { background: var(--accent); color: #fff; } .btn-primary:hover { background: #5b7de0; }
  .btn-secondary { background: var(--border); color: var(--text); } .btn-secondary:hover { background: #3a3d4a; }
  .btn-sm { padding: 4px 10px; font-size: 11px; }
  .stats { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px 18px; min-width: 150px; }
  .stat-card .value { font-size: 22px; font-weight: 700; } .stat-card .label { font-size: 11px; color: var(--dim); }
  .green { color: var(--green); } .amber { color: var(--amber); } .red { color: var(--red); }
  .table-wrap { overflow: auto; max-height: 70vh; background: var(--card); border: 1px solid var(--border); border-radius: 8px; }
  table { width: 100%; border-collapse: collapse; }
  th { background: var(--bg); padding: 10px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: .4px; color: var(--dim); text-align: left; white-space: nowrap; position: sticky; top: 0; z-index: 1; }
  td { padding: 8px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  tr:hover td { background: rgba(108,140,255,.05); }
  .status { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; }
  .status-completed { background: rgba(60,205,92,.15); color: var(--green); }
  .status-processing { background: rgba(108,140,255,.15); color: var(--accent); }
  .status-failed { background: rgba(255,71,87,.15); color: var(--red); }
  .amount { text-align: right; font-variant-numeric: tabular-nums; }
  .loading { text-align: center; padding: 40px; color: var(--dim); }
  .empty { text-align: center; padding: 40px; color: var(--dim); font-size: 14px; }
  .pagination { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-top: 1px solid var(--border); }
  .pagination .info { color: var(--dim); font-size: 12px; }
  .pagination .btns { display: flex; gap: 6px; }
  .error { color: var(--red); padding: 12px; background: rgba(255,71,87,.1); border-radius: 6px; margin-bottom: 12px; }
  code { font-size: 11px; color: var(--accent); }
  .copy-btn { cursor: pointer; font-size: 12px; opacity: 0.5; transition: opacity .15s; user-select: none; }
  .copy-btn:hover { opacity: 1; }
</style>
</head>
<body>
<header>
  <h1>🏦 Wallet Withdrawal Reconciliation</h1>
  <div class="nav"><a href="/recon/" class="btn btn-secondary btn-sm">← Back to Portal</a><span class="badge">Production DB</span></div>
</header>
<div class="container">
  <div class="filters">
    <div class="filter-group"><label>Date From</label><input type="date" id="dateFrom"></div>
    <div class="filter-group"><label>Date To</label><input type="date" id="dateTo"></div>
    <div class="filter-group"><label>Status</label><select id="status"><option value="">All</option><option value="completed">Completed</option><option value="processing">Processing</option><option value="failed">Failed</option></select></div>
    <div class="filter-group"><label>Seller</label><select id="seller"><option value="">All Sellers</option></select></div>
    <div class="filter-group"><label>Search</label><input type="text" id="search" placeholder="ID, reference, seller name"></div>
    <button class="btn btn-primary" onclick="fetchData()">🔍 Filter</button>
    <button class="btn btn-secondary" onclick="resetFilters()">↺ Reset</button>
    <button class="btn btn-secondary btn-sm" onclick="exportCSV()">📥 Export CSV</button>
  </div>
  <div class="stats" id="stats"></div>
  <div id="error" style="display:none"></div>
  <div class="table-wrap">
    <div id="loading" class="loading">Loading data...</div>
    <table id="results" style="display:none">
      <thead><tr>
        <th>Withdrawal ID</th><th>Short ID</th><th>Seller</th><th>Email</th><th>Phone</th>
        <th class="amount">Amount</th><th>Currency</th><th>Status</th><th>Xendit Reference</th>
        <th>Bank</th><th>Account #</th><th>Account Holder</th>
        <th>External Ref</th><th>Idempotency Key</th><th>Rejection Reason</th>
        <th>Requested At</th><th>Processed At</th><th class="amount">Wallet Balance</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
    <div class="pagination" id="pagination" style="display:none"></div>
  </div>
</div>
<script>
var currentPage=1,PAGE_SIZE=50;
function f(n){return n||'0.00';}
function getFilters(){return{
  date_from:document.getElementById('dateFrom').value,
  date_to:document.getElementById('dateTo').value,
  status:document.getElementById('status').value,
  seller:document.getElementById('seller').value,
  search:document.getElementById('search').value,
  page:currentPage,page_size:PAGE_SIZE
};}
function fetchData(){currentPage=1;loadData();}
function loadData(){
  document.getElementById('loading').style.display='block';
  document.getElementById('results').style.display='none';
  document.getElementById('error').style.display='none';
  var p=new URLSearchParams(getFilters());
  fetch('/recon/withdrawals/api/orders?'+p).then(function(r){return r.json();}).then(function(d){
    if(d.error){document.getElementById('error').innerHTML='<div class="error">'+d.error+'</div>';document.getElementById('error').style.display='block';document.getElementById('loading').style.display='none';return;}
    renderStats(d.stats);renderTable(d.rows);renderPagination(d.total,d.page,d.page_size);
    document.getElementById('loading').style.display='none';document.getElementById('results').style.display='table';document.getElementById('pagination').style.display='flex';
  }).catch(function(e){document.getElementById('error').innerHTML='<div class="error">Error: '+e.message+'</div>';document.getElementById('error').style.display='block';document.getElementById('loading').style.display='none';});
}
function renderStats(s){if(!s)return;document.getElementById('stats').innerHTML=
  '<div class="stat-card"><div class="value">'+(s.total_withdrawals||0)+'</div><div class="label">Total Withdrawals</div></div>'+
  '<div class="stat-card"><div class="value green">₱'+fmtNum(s.total_amount||0)+'</div><div class="label">Total Amount</div></div>'+
  '<div class="stat-card"><div class="value green">'+(s.completed_count||0)+'</div><div class="label">Completed</div></div>'+
  '<div class="stat-card"><div class="value amber">'+(s.processing_count||0)+'</div><div class="label">Processing</div></div>'+
  '<div class="stat-card"><div class="value red">'+(s.failed_count||0)+'</div><div class="label">Failed</div></div>';
}
function renderTable(rows){
  var tb=document.getElementById('tbody');
  if(!rows||rows.length===0){tb.innerHTML='<tr><td colspan="18" class="empty">No withdrawals found</td></tr>';return;}
  tb.innerHTML=rows.map(function(r){return'<tr>'+
    '<td><code>'+esc(r.withdrawal_id)+'</code> <span class="copy-btn" data-copy="'+esc(r.withdrawal_id)+'" onclick="copyToClipboard(this)" title="Copy">📋</span></td><td><code>'+esc(r.short_id||'—')+'</code></td>'+
    '<td>'+esc(r.seller_name)+'</td><td>'+esc(r.seller_email)+'</td><td>'+esc(r.seller_phone)+'</td>'+
    '<td class="amount"><b>₱'+fmtNum(r.amount)+'</b></td><td>'+esc(r.currency)+'</td>'+
    '<td><span class="status status-'+esc(r.status)+'">'+esc(r.status)+'</span></td>'+
    '<td><code>'+esc(r.xendit_reference||'—')+'</code></td>'+
    '<td>'+esc(r.bank_name)+'</td><td><code>'+esc(r.account_number)+'</code></td><td>'+esc(r.account_holder)+'</td>'+
    '<td>'+esc(r.external_reference)+'</td><td><code>'+esc(r.idempotency_key)+'</code></td>'+
    '<td style="max-width:200px;overflow:hidden;text-overflow:ellipsis" title="'+esc(r.rejection_reason||'').replace(/"/g,'&quot;')+'">'+esc(r.rejection_reason)+'</td>'+
    '<td>'+esc(r.requested_at)+'</td><td>'+esc(r.processed_at||'—')+'</td>'+
    '<td class="amount">₱'+fmtNum(r.wallet_balance)+'</td></tr>';
  }).join('');}
function renderPagination(t,p,ps){var tp=Math.ceil(t/ps);document.getElementById('pagination').innerHTML=
  '<div class="info">Showing '+((p-1)*ps+1)+'–'+Math.min(p*ps,t)+' of '+t+' withdrawals</div>'+
  '<div class="btns"><button class="btn btn-secondary btn-sm" onclick="goPage(1)" '+(p<=1?'disabled':'')+'>««</button>'+
  '<button class="btn btn-secondary btn-sm" onclick="goPage('+(p-1)+')" '+(p<=1?'disabled':'')+'>« Prev</button>'+
  '<span style="padding:4px 12px;color:var(--dim)">Page '+p+' / '+tp+'</span>'+
  '<button class="btn btn-secondary btn-sm" onclick="goPage('+(p+1)+')" '+(p>=tp?'disabled':'')+'>Next »</button>'+
  '<button class="btn btn-secondary btn-sm" onclick="goPage('+tp+')" '+(p>=tp?'disabled':'')+'>»»</button></div>';}
function goPage(p){currentPage=p;loadData();}
function resetFilters(){document.getElementById('dateFrom').value='';document.getElementById('dateTo').value='';document.getElementById('status').value='';document.getElementById('seller').value='';document.getElementById('search').value='';currentPage=1;loadData();}
function exportCSV(){var p=new URLSearchParams(getFilters());p.delete('page');p.delete('page_size');p.set('export','csv');window.open('/recon/withdrawals/api/orders?'+p,'_blank');}
function fmtNum(n){if(n===null||n===undefined)return'0.00';return Number(n).toLocaleString('en-PH',{minimumFractionDigits:2,maximumFractionDigits:2});}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function copyToClipboard(el){var text=el.getAttribute('data-copy');navigator.clipboard.writeText(text).then(function(){el.textContent='✅';setTimeout(function(){el.textContent='📋';},1500);}).catch(function(){prompt('Copy:',text);});}
function getTodayDate(){var today=new Date();var y=today.getFullYear();var m=String(today.getMonth()+1).padStart(2,'0');var d=String(today.getDate()).padStart(2,'0');return y+'-'+m+'-'+d;}
// Load sellers on page load
fetch('/recon/withdrawals/api/orders?page=1').then(function(r){return r.json();}).then(function(d){
  if(d.sellers){var sel=document.getElementById('seller');d.sellers.forEach(function(s){var o=document.createElement('option');o.value=s;o.textContent=s;sel.appendChild(o);});}
}).catch(function(){});
setTimeout(function(){var today=getTodayDate();document.getElementById('dateFrom').value=today;document.getElementById('dateTo').value=today;loadData();},100);
</script>
</body>
</html>"""
