"""Refunds Portal API — importable module for the MallPlus Dashboard server"""
import json, csv, io
import psycopg2
import psycopg2.extras
from datetime import datetime

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

_BASE_SQL_TEMPLATE = """
SELECT
    COALESCE(oe.order_sn, o.id) AS order_id,
    r.id AS refund_id,
    r.amount AS refund_amount,
    r.created_at AT TIME ZONE 'Asia/Manila' AS refund_date,
    COALESCE(rr.label, 'N/A') AS refund_reason,
    r.note AS refund_note,
    o.created_at AT TIME ZONE 'Asia/Manila' AS order_date,
    COALESCE(s.name, 'Unknown') AS merchant,
    COALESCE(c.first_name || ' ' || c.last_name, 'Unknown') AS buyer_name,
    COALESCE(c.email, '—') AS buyer_email,
    COALESCE(pc.amount, 0) AS payment_amount,
    CASE
        WHEN ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife') THEN 'GCash'
        WHEN ps.provider_id = 'pp_xendit' THEN 'Xendit'
        WHEN ps.provider_id = 'pp_card_stripe-connect' THEN 'Stripe'
        WHEN ps.provider_id = 'pp_system_default' THEN 'System'
        ELSE COALESCE(ps.provider_id, 'Unknown')
    END AS payment_provider,
    CASE
        WHEN ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife') THEN
            CASE COALESCE(gcl.data->'response'->'paymentViews'->0->'payOptionInfos'->0->>'payMethod', '')
                WHEN 'BALANCE' THEN 'Wallet'
                WHEN 'GCredit' THEN 'GCredit'
                WHEN 'GGives' THEN 'GGives'
                ELSE COALESCE(gcl.data->'response'->'paymentViews'->0->'payOptionInfos'->0->>'payMethod', 'GCash')
            END
        WHEN ps.provider_id = 'pp_xendit' THEN
            CASE COALESCE(pmt.data->>'method', '')
                WHEN 'GCASH' THEN 'GCash'
                WHEN 'MAYA' THEN 'Maya'
                WHEN 'CARD' THEN 'Credit Card'
                ELSE COALESCE(pmt.data->>'method', 'Xendit')
            END
        WHEN ps.provider_id = 'pp_card_stripe-connect' THEN 'Card'
        ELSE '—'
    END AS payment_method,
    COALESCE(pc.status, 'N/A') AS payment_status,
    o.status AS order_status
FROM public.refund r
JOIN public.payment p ON p.id = r.payment_id AND p.deleted_at IS NULL
LEFT JOIN public.refund_reason rr ON rr.id = r.refund_reason_id AND rr.deleted_at IS NULL
JOIN public.payment_session ps ON ps.id = p.payment_session_id AND ps.deleted_at IS NULL
LEFT JOIN public.order_payment_collection opc ON opc.payment_collection_id = ps.payment_collection_id
LEFT JOIN public."order" o ON o.id = opc.order_id AND o.deleted_at IS NULL
LEFT JOIN public.order_extension oe ON oe.order_id = o.id
LEFT JOIN public.seller s ON s.id = (o.metadata->>'seller_id')
LEFT JOIN public.customer c ON c.id = o.customer_id AND c.deleted_at IS NULL
LEFT JOIN public.order_payment_collection opc2 ON opc2.payment_collection_id = ps.payment_collection_id
LEFT JOIN public.payment_collection pc ON pc.id = ps.payment_collection_id AND pc.deleted_at IS NULL
LEFT JOIN LATERAL (
    SELECT gcl2.data
    FROM public.payment_gcash_logs gcl2
    WHERE gcl2.payment_session_id = ps.id AND gcl2.deleted_at IS NULL
    LIMIT 1
) gcl ON true
LEFT JOIN LATERAL (
    SELECT p2.data
    FROM public.payment p2
    WHERE p2.payment_session_id = ps.id AND p2.deleted_at IS NULL
    LIMIT 1
) pmt ON true
WHERE r.deleted_at IS NULL
"""

_STATS_SQL_TEMPLATE = """
SELECT
    COUNT(DISTINCT r.id) AS total_refunds,
    COUNT(DISTINCT o.id) AS total_orders_refunded,
    COALESCE(SUM(r.amount), 0) AS total_refund_amount,
    COALESCE(AVG(r.amount), 0) AS avg_refund_amount
FROM public.refund r
JOIN public.payment p ON p.id = r.payment_id AND p.deleted_at IS NULL
LEFT JOIN public.payment_session ps ON ps.id = p.payment_session_id AND ps.deleted_at IS NULL
LEFT JOIN public.order_payment_collection opc ON opc.payment_collection_id = ps.payment_collection_id
LEFT JOIN public."order" o ON o.id = opc.order_id
WHERE r.deleted_at IS NULL
"""

def _build_where(filters):
    conditions = []
    params = []
    
    date_from = filters.get('dateFrom', '')
    date_to = filters.get('dateTo', '')
    refund_reason = filters.get('refundReason', '')
    payment_status = filters.get('paymentStatus', '')
    search = filters.get('search', '')
    
    if date_from:
        conditions.append("r.created_at::date >= %s")
        params.append(date_from)
    
    if date_to:
        conditions.append("r.created_at::date <= %s")
        params.append(date_to)
    
    if refund_reason:
        conditions.append("rr.id = %s")
        params.append(refund_reason)
    
    if payment_status:
        conditions.append("pc.status = %s")
        params.append(payment_status)
    
    if search:
        conditions.append("""
            (LOWER(COALESCE(oe.order_sn, o.id)) LIKE LOWER(%s)
             OR LOWER(s.name) LIKE LOWER(%s)
             OR LOWER(c.email) LIKE LOWER(%s)
             OR LOWER(c.first_name || ' ' || c.last_name) LIKE LOWER(%s))
        """)
        search_term = f"%{search}%"
        params.extend([search_term, search_term, search_term, search_term])
    
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    return where_clause, params

def serve_refunds_portal(path):
    """Serve the refunds portal HTML page"""
    return _HTML_TEMPLATE.encode() if isinstance(_HTML_TEMPLATE, str) else _HTML_TEMPLATE

def handle_refunds_api(path, query_dict):
    """API endpoint: return refunds data as JSON"""
    try:
        page = int(query_dict.get('page', ['1'])[0])
        page_size = int(query_dict.get('page_size', ['50'])[0])
        export = query_dict.get('export', [''])[0]
        
        # Build filters
        filters = {
            'dateFrom': query_dict.get('dateFrom', [''])[0],
            'dateTo': query_dict.get('dateTo', [''])[0],
            'refundReason': query_dict.get('refundReason', [''])[0],
            'paymentStatus': query_dict.get('paymentStatus', [''])[0],
            'search': query_dict.get('search', [''])[0],
        }
        
        where_clause, where_params = _build_where(filters)
        
        # Count query
        count_sql = f"SELECT COUNT(*) FROM ({_BASE_SQL_TEMPLATE} AND {where_clause}) AS cnt"
        
        # Data query
        offset = (page - 1) * page_size
        data_sql = f"{_BASE_SQL_TEMPLATE} AND {where_clause} ORDER BY r.created_at DESC LIMIT %s OFFSET %s"
        
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Get total count
        cur.execute(count_sql, where_params)
        total = cur.fetchone()['count']
        
        # Get page data
        cur.execute(data_sql, where_params + [page_size, offset])
        rows = cur.fetchall()
        
        # Get stats
        stats_where_clause, stats_where_params = _build_where(filters)
        stats_sql = f"{_STATS_SQL_TEMPLATE} AND {stats_where_clause}"
        cur.execute(stats_sql, stats_where_params)
        stats = cur.fetchone()
        
        cur.close()
        conn.close()
        
        # Serialize
        from decimal import Decimal
        rows_dict = [dict(r) for r in rows]
        for r in rows_dict:
            for k, v in r.items():
                if isinstance(v, (datetime,)):
                    r[k] = str(v)
                elif isinstance(v, Decimal):
                    r[k] = float(v)
        
        # Convert stats values
        if stats:
            stats_dict = dict(stats)
            for k, v in stats_dict.items():
                if isinstance(v, Decimal):
                    stats_dict[k] = float(v)
        
        response = {
            'rows': rows_dict,
            'total': total,
            'page': page,
            'page_size': page_size,
            'stats': stats_dict if stats else {},
        }
        
        body = json.dumps(response).encode()
        return 200, "application/json", body, True
    
    except Exception as e:
        print(f"[refunds_api ERROR] {str(e)}")
        import traceback
        print(traceback.format_exc())
        error_msg = f"{str(e)} | Query dict keys: {list(query_dict.keys())}"
        return 500, "application/json", json.dumps({"error": error_msg}).encode(), True

def _render_csv(rows):
    """CSV export"""
    cols = ['order_id', 'refund_id', 'refund_date', 'refund_amount', 'refund_reason', 'refund_note', 'order_date', 'merchant', 'buyer_name', 'buyer_email', 'payment_amount', 'payment_provider', 'payment_method', 'payment_status', 'order_status']
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=cols)
    writer.writeheader()
    for row in rows:
        writer.writerow({col: row.get(col, '') for col in cols})
    return output.getvalue()

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Refunds Reconciliation — MallPlus</title>
<style>
  :root { --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a; --text: #e1e4ed; --dim: #8b8fa3; --accent: #6c8cff; --green: #3ccd5c; --amber: #ffa502; --red: #ff4757; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); font-size: 14px; }
  .container { max-width: 1600px; margin: 0 auto; padding: 20px; }
  header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
  h1 { font-size: 28px; font-weight: 700; }
  .nav { display: flex; gap: 12px; }
  .btn { padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 500; transition: all .2s; }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { opacity: 0.9; }
  .btn-secondary { background: var(--card); color: var(--text); border: 1px solid var(--border); }
  .btn-secondary:hover { border-color: var(--accent); }
  .btn-sm { padding: 6px 12px; font-size: 12px; }
  .badge { display: inline-block; padding: 4px 10px; border-radius: 10px; font-size: 11px; background: var(--card); color: var(--dim); border: 1px solid var(--border); }
  .filters { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 20px; display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
  .filter-group { display: flex; flex-direction: column; gap: 4px; }
  .filter-group label { font-size: 12px; color: var(--dim); font-weight: 500; text-transform: uppercase; }
  .filter-group input, .filter-group select { padding: 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg); color: var(--text); font-size: 13px; }
  .stats { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px 18px; min-width: 140px; }
  .stat-card .value { font-size: 22px; font-weight: 700; }
  .stat-card .value.red { color: var(--red); }
  .stat-card .label { font-size: 11px; color: var(--dim); }
  .table-wrap { overflow: auto; max-height: 70vh; background: var(--card); border: 1px solid var(--border); border-radius: 8px; }
  table { width: 100%; border-collapse: collapse; }
  th { background: var(--bg); padding: 10px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: .4px; color: var(--dim); text-align: left; white-space: nowrap; position: sticky; top: 0; z-index: 1; }
  td { padding: 8px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  tr:hover td { background: rgba(108,140,255,.05); }
  .status { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; }
  .status-pending { background: rgba(255,165,2,.15); color: var(--amber); }
  .status-completed { background: rgba(60,205,92,.15); color: var(--green); }
  .status-canceled { background: rgba(255,71,87,.15); color: var(--red); }
  .amount { text-align: right; font-variant-numeric: tabular-nums; }
  .pagination { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-top: 1px solid var(--border); }
  .pagination .info { color: var(--dim); font-size: 12px; }
  .pagination .btns { display: flex; gap: 6px; }
  code { font-size: 11px; color: var(--accent); font-family: 'Monaco', monospace; }
  .copy-btn { cursor: pointer; font-size: 12px; opacity: 0.5; transition: opacity .15s; user-select: none; }
  .copy-btn:hover { opacity: 1; }
  .empty { text-align: center; padding: 40px; color: var(--dim); font-size: 14px; }
  .loading { text-align: center; padding: 40px; color: var(--dim); }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>↩️ Refunds Reconciliation</h1>
    <div class="nav"><a href="/recon/" class="btn btn-secondary btn-sm">← Portal Home</a><span class="badge">Production DB</span></div>
  </header>
  
  <div class="filters">
    <div class="filter-group"><label>Date From</label><input type="date" id="dateFrom"></div>
    <div class="filter-group"><label>Date To</label><input type="date" id="dateTo"></div>
    <div class="filter-group"><label>Refund Reason</label><select id="refundReason"><option value="">All</option></select></div>
    <div class="filter-group"><label>Payment Status</label><select id="paymentStatus"><option value="">All</option><option value="completed">Completed</option><option value="authorized">Authorized</option><option value="canceled">Canceled</option><option value="not_paid">Not Paid</option></select></div>
    <div class="filter-group"><label>Search (Order / Merchant / Email)</label><input type="text" id="search" placeholder="e.g. order ID, merchant, email"></div>
    <button class="btn btn-primary" onclick="fetchData()">🔍 Filter</button>
    <button class="btn btn-secondary" onclick="resetFilters()">↺ Reset</button>
    <button class="btn btn-secondary" onclick="exportCSV()">⬇️ CSV</button>
  </div>
  
  <div id="stats" class="stats"></div>
  
  <div class="table-wrap">
    <table><thead><tr><th>Refund ID <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Order # <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Refund Date <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Order Date <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Merchant <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Buyer <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Email <span style="font-size:9px; color:var(--dim)">📋</span></th><th class="amount">Payment Amt <span style="font-size:9px; color:var(--dim)">📋</span></th><th class="amount">Refund Amt <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Reason <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Note <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Provider <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Method <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Payment Status <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Order Status <span style="font-size:9px; color:var(--dim)">📋</span></th></tr></thead>
    <tbody id="tbody"><tr><td colspan="15" class="loading">Loading data...</td></tr></tbody>
    </table>
  </div>
  
  <div class="pagination" id="pagination"></div>
</div>

<script>
let currentPage = 1;
function getTodayDate(){var today=new Date();var y=today.getFullYear();var m=String(today.getMonth()+1).padStart(2,'0');var d=String(today.getDate()).padStart(2,'0');return y+'-'+m+'-'+d;}
function getFilters(){return{dateFrom:document.getElementById('dateFrom').value||'',dateTo:document.getElementById('dateTo').value||'',refundReason:document.getElementById('refundReason').value||'',paymentStatus:document.getElementById('paymentStatus').value||'',search:document.getElementById('search').value||''};}
function fetchData(){currentPage=1;loadData();}
function loadData(){var f=getFilters();var p=new URLSearchParams(Object.entries(f).filter(([k,v])=>v!==''));p.set('page',currentPage);p.set('page_size',50);fetch('/recon/refunds/api/orders?'+p).then(r=>r.json()).then(d=>{renderStats(d.stats);renderTable(d.rows);renderPagination(d.total,currentPage,50);}).catch(e=>alert('Error: '+e));}
function renderStats(s){if(!s)return;document.getElementById('stats').innerHTML='<div class="stat-card"><div class="value red">₱'+fmtNum(s.total_refund_amount||0)+'</div><div class="label">Total Refunded</div></div><div class="stat-card"><div class="value">'+(s.total_refunds||0)+'</div><div class="label">Refund Count</div></div><div class="stat-card"><div class="value">'+(s.total_orders_refunded||0)+'</div><div class="label">Orders Refunded</div></div><div class="stat-card"><div class="value">₱'+fmtNum(s.avg_refund_amount||0)+'</div><div class="label">Avg Refund</div></div>';}
function renderTable(rows){var tb=document.getElementById('tbody');if(!rows||rows.length===0){tb.innerHTML='<tr><td colspan="15" class="empty">No refunds found</td></tr>';return;}tb.innerHTML=rows.map(r=>'<tr><td><code>'+esc(r.refund_id)+'</code> <span class=\"copy-btn\" data-copy=\"'+esc(r.refund_id)+'\" onclick=\"copyToClipboard(this)\" title=\"Copy\">\U0001f4cb</span></td><td><code>'+esc(r.order_id||'—')+'</code> <span class=\"copy-btn\" data-copy=\"'+esc(r.order_id||'')+'\" onclick=\"copyToClipboard(this)\" title=\"Copy\">\U0001f4cb</span></td><td>'+esc(r.refund_date)+'</td><td>'+esc(r.order_date||'—')+'</td><td>'+esc(r.merchant)+'</td><td>'+esc(r.buyer_name)+'</td><td>'+esc(r.buyer_email)+'</td><td class="amount">₱'+fmtNum(r.payment_amount)+'</td><td class="amount"><b>₱'+fmtNum(r.refund_amount)+'</b></td><td>'+esc(r.refund_reason||'—')+'</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis" title="'+esc(r.refund_note||'')+'">'+esc(r.refund_note||'—')+'</td><td>'+esc(r.payment_provider||'—')+'</td><td>'+esc(r.payment_method||'—')+'</td><td><span class="status status-'+(r.payment_status||'na')+'">'+esc(r.payment_status||'N/A')+'</span></td><td><span class="status status-'+esc(r.order_status||'pending')+'">'+esc(r.order_status||'pending')+'</span></td></tr>').join('');}
function renderPagination(t,p,ps){var tp=Math.ceil(t/ps);document.getElementById('pagination').innerHTML='<div class="info">Showing '+((p-1)*ps+1)+'–'+Math.min(p*ps,t)+' of '+t+' refunds</div><div class="btns"><button class="btn btn-secondary btn-sm" onclick="goPage(1)" '+(p<=1?'disabled':'')+'>««</button><button class="btn btn-secondary btn-sm" onclick="goPage('+(p-1)+')" '+(p<=1?'disabled':'')+'>« Prev</button><span style="padding:4px 12px;color:var(--dim)">Page '+p+' / '+tp+'</span><button class="btn btn-secondary btn-sm" onclick="goPage('+(p+1)+')" '+(p>=tp?'disabled':'')+'>Next »</button><button class="btn btn-secondary btn-sm" onclick="goPage('+tp+')" '+(p>=tp?'disabled':'')+'>»»</button></div>';}
function goPage(p){currentPage=p;loadData();}
function resetFilters(){document.getElementById('dateFrom').value='';document.getElementById('dateTo').value='';document.getElementById('refundReason').value='';document.getElementById('paymentStatus').value='';document.getElementById('search').value='';currentPage=1;loadData();}
function exportCSV(){var p=new URLSearchParams(getFilters());p.set('export','csv');window.open('/recon/refunds/api/orders?'+p,'_blank');}
function fmtNum(n){if(n===null||n===undefined)return'0.00';return Number(n).toLocaleString('en-PH',{minimumFractionDigits:2,maximumFractionDigits:2});}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function copyToClipboard(el){var text=el.getAttribute('data-copy');navigator.clipboard.writeText(text).then(function(){el.textContent='✅';setTimeout(function(){el.textContent='📋';},1500);}).catch(function(){prompt('Copy:',text);});}
setTimeout(function(){var today=getTodayDate();document.getElementById('dateFrom').value=today;document.getElementById('dateTo').value=today;loadData();},100);
</script>
</body>
</html>"""
