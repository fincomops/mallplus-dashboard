"""Reconciliation Portal — Shipping Fee Recon API"""
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
    COALESCE(oe.order_sn, o.id) AS order_id,
    o.created_at AT TIME ZONE 'Asia/Manila' AS order_date,
    COALESCE(s.name, 'Unknown') AS seller_name,
    CASE
        WHEN pp_origin.region_name IS NOT NULL THEN 
            CASE
                WHEN SUBSTRING(pp_origin.region_name, 1, 6) = 'Luzon ' THEN 'Luzon'
                WHEN SUBSTRING(pp_origin.region_name, 1, 8) = 'Visayas' THEN 'Visayas'
                WHEN SUBSTRING(pp_origin.region_name, 1, 9) = 'Mindanao' THEN 'Mindanao'
                WHEN pp_origin.region_name = 'GMA' OR pp_origin.region_name = 'NCR' THEN 'GMA'
                ELSE pp_origin.region_name
            END
        WHEN pp_seller.region_name IS NOT NULL THEN
            CASE
                WHEN SUBSTRING(pp_seller.region_name, 1, 6) = 'Luzon ' THEN 'Luzon'
                WHEN SUBSTRING(pp_seller.region_name, 1, 8) = 'Visayas' THEN 'Visayas'
                WHEN SUBSTRING(pp_seller.region_name, 1, 9) = 'Mindanao' THEN 'Mindanao'
                WHEN pp_seller.region_name = 'GMA' OR pp_seller.region_name = 'NCR' THEN 'GMA'
                ELSE pp_seller.region_name
            END
        ELSE '—'
    END AS origin_address,
    TRIM(COALESCE(fa.first_name, '') || ' ' || COALESCE(fa.last_name, '')) AS buyer_name,
    CASE
        WHEN SUBSTRING(pp_dest.region_name, 1, 6) = 'Luzon ' THEN 'Luzon'
        WHEN SUBSTRING(pp_dest.region_name, 1, 8) = 'Visayas' THEN 'Visayas'
        WHEN SUBSTRING(pp_dest.region_name, 1, 9) = 'Mindanao' THEN 'Mindanao'
        WHEN pp_dest.region_name = 'GMA' THEN 'GMA'
        ELSE COALESCE(pp_dest.region_name, '—')
    END AS destination_address,
    COALESCE(oli.product_title, fi.title, '—') AS product,
    COALESCE(fi.quantity, 0) AS quantity,
    js.weight_kg,
    js.dimensions,
    COALESCE(osm2.name, '—') AS shipping_method,
    COALESCE((o.metadata->>'estimated_shipping_amount')::numeric, 0) AS estimated_shipping_fee,
    COALESCE(osm2.amount, 0) AS actual_shipping_fee,
    'J&T Express' AS carrier,
    COALESCE(fl.tracking_number, '—') AS tracking_number,
    COALESCE(fl.tracking_url, '—') AS tracking_url,
    js.logistics_status,
    COALESCE(js.jt_status_code, '—') AS jt_status_code,
    COALESCE(js.jt_status_desc, '—') AS jt_status_desc,
    COALESCE(js.bill_code, '—') AS bill_code,
    COALESCE(js.tx_logistic_id, '—') AS tx_logistic_id,
    js.booked_at AT TIME ZONE 'Asia/Manila' AS booked_at,
    js.picked_up_at AT TIME ZONE 'Asia/Manila' AS picked_up_at,
    js.delivered_at AT TIME ZONE 'Asia/Manila' AS delivered_at,
    js.failed_at AT TIME ZONE 'Asia/Manila' AS failed_at,
    js.cancelled_at AT TIME ZONE 'Asia/Manila' AS cancelled_at,
    COALESCE(ret.received_at AT TIME ZONE 'Asia/Manila', NULL) AS returned_at,
    js.created_at AT TIME ZONE 'Asia/Manila' AS js_created_at
FROM public.jt_shipment js
JOIN public."order" o ON o.id = js.order_id AND o.deleted_at IS NULL
LEFT JOIN public.order_extension oe ON oe.order_id = o.id
LEFT JOIN public.fulfillment f ON f.id = js.fulfillment_id AND f.deleted_at IS NULL
LEFT JOIN LATERAL (
    SELECT fi2.title, fi2.sku, fi2.quantity, fi2.line_item_id
    FROM public.fulfillment_item fi2
    WHERE fi2.fulfillment_id = f.id AND fi2.deleted_at IS NULL
    LIMIT 1
) fi ON true
LEFT JOIN public.order_line_item oli ON oli.id = fi.line_item_id AND oli.deleted_at IS NULL
LEFT JOIN public.product p ON p.id = oli.product_id AND p.deleted_at IS NULL
LEFT JOIN public.seller_seller_product_product ssp ON ssp.product_id = p.id
LEFT JOIN public.seller s ON s.id = ssp.seller_id AND s.deleted_at IS NULL
LEFT JOIN public.stock_location sl ON sl.id = f.location_id AND sl.deleted_at IS NULL
LEFT JOIN public.stock_location_address sla ON sla.id = sl.address_id AND sla.deleted_at IS NULL
LEFT JOIN public.ph_province pp_origin ON pp_origin.name = COALESCE(sla.province, s.state) AND pp_origin.deleted_at IS NULL
LEFT JOIN public.ph_city_municipality pcm_seller ON (
    pcm_seller.name = s.city
    OR pcm_seller.name ILIKE 'City of ' || s.city
    OR pcm_seller.name ILIKE s.city || ' City'
) AND pcm_seller.deleted_at IS NULL
LEFT JOIN public.ph_province pp_seller ON pp_seller.name = COALESCE(pcm_seller.province_name, 'Metro Manila') AND pp_seller.deleted_at IS NULL
LEFT JOIN public.fulfillment_address fa ON fa.id = f.delivery_address_id AND fa.deleted_at IS NULL
LEFT JOIN public.ph_province pp_dest ON pp_dest.name = fa.province AND pp_dest.deleted_at IS NULL
LEFT JOIN LATERAL (
    SELECT fl2.tracking_number, fl2.tracking_url, fl2.label_url
    FROM public.fulfillment_label fl2
    WHERE fl2.fulfillment_id = f.id AND fl2.deleted_at IS NULL
    LIMIT 1
) fl ON true
LEFT JOIN LATERAL (
    SELECT osm3.name, osm3.amount
    FROM public.order_shipping os3
    JOIN public.order_shipping_method osm3 ON osm3.id = os3.shipping_method_id AND osm3.deleted_at IS NULL
    WHERE os3.order_id = o.id AND os3.deleted_at IS NULL
    LIMIT 1
) osm2 ON true
LEFT JOIN LATERAL (
    SELECT received_at
    FROM public.return r
    WHERE r.order_id = o.id AND r.deleted_at IS NULL
    ORDER BY r.received_at DESC
    LIMIT 1
) ret ON true
WHERE js.deleted_at IS NULL
"""

_STATS_SQL = """
SELECT
    COUNT(*) AS total_shipments,
    COALESCE(SUM(js.weight_kg), 0) AS total_weight_kg,
    COALESCE(SUM(osm2.amount), 0) AS total_shipping_fees,
    COALESCE(SUM(CASE WHEN js.logistics_status = 'LOGISTICS_DELIVERY_DONE' THEN 1 ELSE 0 END), 0) AS delivered_count,
    COALESCE(SUM(CASE WHEN js.logistics_status IN ('LOGISTICS_DELIVERY_FAILED', 'LOGISTICS_LOST', 'LOGISTICS_RETURNED') THEN 1 ELSE 0 END), 0) AS failed_return_count,
    COALESCE(SUM(CASE WHEN js.logistics_status NOT IN ('LOGISTICS_DELIVERY_DONE', 'LOGISTICS_DELIVERY_FAILED', 'LOGISTICS_LOST', 'LOGISTICS_RETURNED', 'LOGISTICS_REQUEST_CANCELLED') THEN 1 ELSE 0 END), 0) AS in_transit_count
FROM public.jt_shipment js
JOIN public."order" o ON o.id = js.order_id AND o.deleted_at IS NULL
LEFT JOIN public.order_extension oe ON oe.order_id = o.id
LEFT JOIN public.fulfillment f ON f.id = js.fulfillment_id AND f.deleted_at IS NULL
LEFT JOIN LATERAL (
    SELECT fi2.line_item_id
    FROM public.fulfillment_item fi2
    WHERE fi2.fulfillment_id = f.id AND fi2.deleted_at IS NULL
    LIMIT 1
) fi ON true
LEFT JOIN public.order_line_item oli ON oli.id = fi.line_item_id AND oli.deleted_at IS NULL
LEFT JOIN public.product p ON p.id = oli.product_id AND p.deleted_at IS NULL
LEFT JOIN public.seller_seller_product_product ssp ON ssp.product_id = p.id
LEFT JOIN public.seller s ON s.id = ssp.seller_id AND s.deleted_at IS NULL
LEFT JOIN LATERAL (
    SELECT fl2.tracking_number
    FROM public.fulfillment_label fl2
    WHERE fl2.fulfillment_id = f.id AND fl2.deleted_at IS NULL
    LIMIT 1
) fl ON true
LEFT JOIN LATERAL (
    SELECT osm3.amount
    FROM public.order_shipping os3
    JOIN public.order_shipping_method osm3 ON osm3.id = os3.shipping_method_id AND osm3.deleted_at IS NULL
    WHERE os3.order_id = o.id AND os3.deleted_at IS NULL
    LIMIT 1
) osm2 ON true
LEFT JOIN LATERAL (
    SELECT received_at
    FROM public.return r
    WHERE r.order_id = o.id AND r.deleted_at IS NULL
    ORDER BY r.received_at DESC
    LIMIT 1
) ret ON true
WHERE js.deleted_at IS NULL
"""

# Status → date column mapping for date anchor
_STATUS_DATE_COLUMN = {
    "LOGISTICS_DELIVERY_DONE":      "js.delivered_at",
    "LOGISTICS_PICKUP_DONE":        "js.picked_up_at",
    "LOGISTICS_DELIVERY_FAILED":    "js.failed_at",
    "LOGISTICS_REQUEST_CANCELLED":  "js.cancelled_at",
    # All others (including empty/"all") → js.created_at
}

_STATUS_LABELS = [
    ("", "All Statuses"),
    ("LOGISTICS_NOT_STARTED", "Not Started"),
    ("LOGISTICS_PICKUP_DONE", "Pickup Done"),
    ("LOGISTICS_PICKUP_RETRY", "Pickup Retry"),
    ("LOGISTICS_DEPARTURE", "Departure"),
    ("LOGISTICS_ARRIVAL", "Arrival"),
    ("LOGISTICS_OUT_FOR_DELIVERY", "Out for Delivery"),
    ("LOGISTICS_DELIVERY_DONE", "Delivery Done"),
    ("LOGISTICS_DELIVERY_FAILED", "Delivery Failed"),
    ("LOGISTICS_LOST", "Lost"),
    ("LOGISTICS_RETURNED", "Returned"),
    ("LOGISTICS_REQUEST_CANCELLED", "Request Cancelled"),
]


def handle_shipping_api(path, query_params):
    try:
        date_from = query_params.get("date_from", [""])[0]
        date_to = query_params.get("date_to", [""])[0]
        carrier = query_params.get("carrier", [""])[0]
        logistics_status = query_params.get("logistics_status", [""])[0]
        search = query_params.get("search", [""])[0].strip()
        page = int(query_params.get("page", ["1"])[0])
        page_size = int(query_params.get("page_size", ["50"])[0])
        export_csv = query_params.get("export", [""])[0] == "csv"

        conditions = []
        params = []

        # Date range — anchored to status-selected date column
        if date_from or date_to:
            date_col = _STATUS_DATE_COLUMN.get(logistics_status, "js.created_at")

        if date_from:
            conditions.append(f"{date_col} >= %s")
            params.append(date_from)
        if date_to:
            conditions.append(f"{date_col} < %s::date + interval '1 day'")
            params.append(date_to)

        if logistics_status:
            conditions.append("js.logistics_status = %s")
            params.append(logistics_status)

        # Carrier filter — currently only J&T (all rows qualify)
        # When more 3PLs are onboarded, this will filter by fulfillment.provider_id
        if carrier:
            if carrier == "J&T":
                # All jt_shipment rows are J&T — no additional filter needed
                # Future: JOIN to fulfillment_provider and match by provider name
                pass
            else:
                conditions.append("js.carrier = %s")
                params.append(carrier)

        if search:
            conditions.append(
                "(js.id ILIKE %s OR oe.order_sn ILIKE %s OR o.id ILIKE %s "
                "OR s.name ILIKE %s OR fl.tracking_number ILIKE %s "
                "OR js.bill_code ILIKE %s)"
            )
            params.extend([f"%{search}%"] * 6)

        extra_where = " AND ".join(conditions) if conditions else "true"

        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if export_csv:
            data_sql = f"{_BASE_SQL} AND {extra_where} ORDER BY js.created_at DESC LIMIT 5000"
            cur.execute(data_sql, params)
            rows = cur.fetchall()
            if not rows:
                return 200, "text/csv", b"", True
            output = io.StringIO()
            writer = csv.writer(output)
            col_map = {
                "order_id": "Order #", "order_date": "Order Date",
                "seller_name": "Seller", "origin_address": "Origin Address",
                "buyer_name": "Buyer", "destination_address": "Destination Address",
                "product": "Product", "quantity": "Qty",
                "weight_kg": "Weight (kg)", "dimensions": "Dimensions",
                "shipping_method": "Shipping Method", "estimated_shipping_fee": "Est. Ship Fee", "actual_shipping_fee": "Actual Ship Fee",
                "carrier": "Carrier", "tracking_number": "Tracking #",
                "tracking_url": "Tracking URL",
                "logistics_status": "Logistics Status", "jt_status_code": "JT Status Code",
                "jt_status_desc": "JT Status Desc", "bill_code": "Bill Code",
                "tx_logistic_id": "TX Logistic ID",
                "booked_at": "Booked At", "picked_up_at": "Picked Up At",
                "delivered_at": "Delivered At", "failed_at": "Failed At",
                "cancelled_at": "Cancelled At", "returned_at": "Returned At",
            }
            headers = [col_map.get(d[0], d[0]) for d in cur.description]
            writer.writerow(headers)
            for r in rows:
                vals = []
                for k, v in r.items():
                    if v is None:
                        vals.append("")
                    elif isinstance(v, dict):
                        vals.append(json.dumps(v))
                    else:
                        vals.append(str(v))
                writer.writerow(vals)
            return 200, "text/csv", output.getvalue().encode(), True

        # Count
        count_sql = f"SELECT COUNT(*) AS total FROM ({_BASE_SQL} AND {extra_where}) sub"
        cur.execute(count_sql, params)
        total = cur.fetchone()["total"]

        # Data
        data_sql = f"{_BASE_SQL} AND {extra_where} ORDER BY js.created_at DESC LIMIT %s OFFSET %s"
        offset = (page - 1) * page_size
        cur.execute(data_sql, params + [page_size, offset])
        rows = _serialize(cur.fetchall())

        # Stats
        stats_sql = f"{_STATS_SQL} AND {extra_where}"
        cur.execute(stats_sql, params)
        stats = _serialize([cur.fetchone()])[0]

        body = json.dumps({
            "rows": rows, "total": total, "page": page, "page_size": page_size,
            "stats": stats, "statuses": [{"value": v, "label": l} for v, l in _STATUS_LABELS],
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


def serve_shipping_portal():
    import os
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shipping_portal.html")
    if os.path.exists(html_path):
        with open(html_path, "rb") as f:
            return f.read()
    return _SHIPPING_HTML.encode()


_SHIPPING_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Shipping Fee Reconciliation — MallPlus</title>
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
  .filter-group input, .filter-group select { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 8px 12px; border-radius: 6px; font-size: 13px; min-width: 180px; }
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
  .status-done { background: rgba(60,205,92,.15); color: var(--green); }
  .status-failed { background: rgba(255,71,87,.15); color: var(--red); }
  .status-returned { background: rgba(255,71,87,.15); color: var(--red); }
  .status-lost { background: rgba(255,71,87,.15); color: var(--red); }
  .status-cancelled { background: rgba(255,71,87,.15); color: var(--red); }
  .status-transit { background: rgba(108,140,255,.15); color: var(--accent); }
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
  .address { max-width: 200px; overflow: hidden; text-overflow: ellipsis; }
  .hint { font-size: 10px; color: var(--dim); font-style: italic; }
</style>
</head>
<body>
<header>
  <h1>📦 Shipping Fee Reconciliation</h1>
  <div class="nav"><a href="/recon/" class="btn btn-secondary btn-sm">← Back to Portal</a><span class="badge">Production DB</span></div>
</header>
<div class="container">
  <div class="filters">
    <div class="filter-group"><label>Carrier</label><select id="carrier"><option value="">All Carriers</option><option value="J&T">J&T Express</option></select></div>
    <div class="filter-group"><label>Logistics Status</label><select id="logisticsStatus"></select></div>
    <div class="filter-group"><label>Date From <span class="hint" id="dateHint">(by created date)</span></label><input type="date" id="dateFrom"></div>
    <div class="filter-group"><label>Date To</label><input type="date" id="dateTo"></div>
    <div class="filter-group"><label>Search</label><input type="text" id="search" placeholder="Order ID, tracking #, bill code"></div>
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
        <th>Order #</th><th>Order Date</th>
        <th>Seller</th><th>Origin</th><th>Buyer</th><th>Destination</th>
        <th>Product</th><th class="amount">Qty</th>
        <th class="amount">Weight (kg)</th><th>Dimensions</th>
        <th>Shipping Method</th><th class="amount">Est. Ship Fee</th><th class="amount">Actual Ship Fee</th>
        <th>Carrier</th><th>Tracking #</th>
        <th>Logistics Status</th><th>JT Code</th><th>JT Desc</th>
        <th>Bill Code</th><th>TX Logistic ID</th>
        <th>Booked</th><th>Picked Up</th><th>Delivered</th><th>Failed</th><th>Cancelled</th><th>Returned</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
    <div class="pagination" id="pagination" style="display:none"></div>
  </div>
</div>
<script>
var currentPage=1,PAGE_SIZE=50;
var STATUS_LABELS={};
function f(n){return n||'0.00';}
function getFilters(){return{
  date_from:document.getElementById('dateFrom').value,
  date_to:document.getElementById('dateTo').value,
  carrier:document.getElementById('carrier').value,
  logistics_status:document.getElementById('logisticsStatus').value,
  search:document.getElementById('search').value,
  page:currentPage,page_size:PAGE_SIZE
};}
function statusClass(s){
  if(!s)return'';
  var done=['LOGISTICS_DELIVERY_DONE'];
  var failed=['LOGISTICS_DELIVERY_FAILED','LOGISTICS_LOST','LOGISTICS_RETURNED','LOGISTICS_REQUEST_CANCELLED'];
  if(done.indexOf(s)>=0)return'status-done';
  if(failed.indexOf(s)>=0)return s==='LOGISTICS_REQUEST_CANCELLED'?'status-cancelled':(s==='LOGISTICS_LOST'?'status-lost':'status-failed');
  return'status-transit';
}
function statusLabel(s){return STATUS_LABELS[s]||s;}
function getTodayDate(){var today=new Date();var y=today.getFullYear();var m=String(today.getMonth()+1).padStart(2,'0');var d=String(today.getDate()).padStart(2,'0');return y+'-'+m+'-'+d;}
function loadStatuses(){
  fetch('/recon/shipping/api/orders?page=1').then(function(r){return r.json();}).then(function(d){
    if(d.statuses){var sel=document.getElementById('logisticsStatus');d.statuses.forEach(function(s){STATUS_LABELS[s.value]=s.label;var o=document.createElement('option');o.value=s.value;o.textContent=s.label;sel.appendChild(o);});}
  }).catch(function(){});
  setTimeout(function(){var today=getTodayDate();document.getElementById('dateFrom').value=today;document.getElementById('dateTo').value=today;loadData();},150);
}
// Update date hint when status changes
document.getElementById('logisticsStatus').addEventListener('change',function(){
  var s=this.value;var hint='(by created date)';
  if(s==='LOGISTICS_DELIVERY_DONE')hint='(by delivered date)';
  else if(s==='LOGISTICS_PICKUP_DONE')hint='(by pickup date)';
  else if(s==='LOGISTICS_DELIVERY_FAILED')hint='(by failed date)';
  else if(s==='LOGISTICS_REQUEST_CANCELLED')hint='(by cancelled date)';
  document.getElementById('dateHint').textContent=hint;
});
function fetchData(){currentPage=1;loadData();}
function loadData(){
  document.getElementById('loading').style.display='block';
  document.getElementById('results').style.display='none';
  document.getElementById('error').style.display='none';
  var p=new URLSearchParams(getFilters());
  fetch('/recon/shipping/api/orders?'+p).then(function(r){return r.json();}).then(function(d){
    if(d.error){document.getElementById('error').innerHTML='<div class="error">'+d.error+'</div>';document.getElementById('error').style.display='block';document.getElementById('loading').style.display='none';return;}
    renderStats(d.stats);renderTable(d.rows);renderPagination(d.total,d.page,d.page_size);
    document.getElementById('loading').style.display='none';document.getElementById('results').style.display='table';document.getElementById('pagination').style.display='flex';
  }).catch(function(e){document.getElementById('error').innerHTML='<div class="error">Error: '+e.message+'</div>';document.getElementById('error').style.display='block';document.getElementById('loading').style.display='none';});
}
function renderStats(s){if(!s)return;document.getElementById('stats').innerHTML=
  '<div class="stat-card"><div class="value">'+(s.total_shipments||0)+'</div><div class="label">Total Shipments</div></div>'+
  '<div class="stat-card"><div class="value">'+(s.total_weight_kg||0)+' kg</div><div class="label">Total Weight</div></div>'+
  '<div class="stat-card"><div class="value green">₱'+fmtNum(s.total_shipping_fees||0)+'</div><div class="label">Total Shipping Fees</div></div>'+
  '<div class="stat-card"><div class="value green">'+(s.delivered_count||0)+'</div><div class="label">Delivered</div></div>'+
  '<div class="stat-card"><div class="value amber">'+(s.in_transit_count||0)+'</div><div class="label">In Transit</div></div>'+
  '<div class="stat-card"><div class="value red">'+(s.failed_return_count||0)+'</div><div class="label">Failed / Returned</div></div>';
}
function renderTable(rows){
  var tb=document.getElementById('tbody');
  if(!rows||rows.length===0){tb.innerHTML='<tr><td colspan="26" class="empty">No shipments found</td></tr>';return;}
  tb.innerHTML=rows.map(function(r){
    var dim=typeof r.dimensions==='object'?JSON.stringify(r.dimensions):(r.dimensions||'—');
    return'<tr>'+
      '<td><code>'+esc(r.order_id)+'</code> <span class="copy-btn" data-copy="'+esc(r.order_id)+'" onclick="copyToClipboard(this)" title="Copy">📋</span></td><td>'+esc(r.order_date||'—')+'</td>'+
      '<td>'+esc(r.seller_name)+'</td><td class="address" title="'+esc(r.origin_address||'').replace(/"/g,'&quot;')+'">'+esc(r.origin_address||'—')+'</td>'+
      '<td>'+esc(r.buyer_name||'—')+'</td><td class="address" title="'+esc(r.destination_address||'').replace(/"/g,'&quot;')+'">'+esc(r.destination_address||'—')+'</td>'+
      '<td>'+esc(r.product)+'</td><td class="amount">'+esc(r.quantity)+'</td>'+
      '<td class="amount">'+esc(r.weight_kg)+'</td><td style="max-width:120px;overflow:hidden;text-overflow:ellipsis" title="'+esc(String(dim)).replace(/"/g,'&quot;')+'">'+esc(String(dim))+'</td>'+
      '<td>'+esc(r.shipping_method)+'</td><td class="amount">₱'+fmtNum(r.estimated_shipping_fee)+'</td><td class="amount"><b>₱'+fmtNum(r.actual_shipping_fee)+'</b></td>'+
      '<td>'+esc(r.carrier)+'</td><td><code>'+esc(r.tracking_number)+'</code></td>'+
      '<td><span class="status '+statusClass(r.logistics_status)+'">'+statusLabel(r.logistics_status)+'</span></td>'+
      '<td><code>'+esc(r.jt_status_code)+'</code></td><td>'+esc(r.jt_status_desc)+'</td>'+
      '<td><code>'+esc(r.bill_code)+'</code></td><td><code>'+esc(r.tx_logistic_id)+'</code></td>'+
      '<td>'+esc(r.booked_at||'—')+'</td><td>'+esc(r.picked_up_at||'—')+'</td><td>'+esc(r.delivered_at||'—')+'</td><td>'+esc(r.failed_at||'—')+'</td><td>'+esc(r.cancelled_at||'—')+'</td><td>'+esc(r.returned_at||'—')+'</td></tr>';
  }).join('');}
function renderPagination(t,p,ps){var tp=Math.ceil(t/ps);document.getElementById('pagination').innerHTML=
  '<div class="info">Showing '+((p-1)*ps+1)+'–'+Math.min(p*ps,t)+' of '+t+' shipments</div>'+
  '<div class="btns"><button class="btn btn-secondary btn-sm" onclick="goPage(1)" '+(p<=1?'disabled':'')+'>««</button>'+
  '<button class="btn btn-secondary btn-sm" onclick="goPage('+(p-1)+')" '+(p<=1?'disabled':'')+'>« Prev</button>'+
  '<span style="padding:4px 12px;color:var(--dim)">Page '+p+' / '+tp+'</span>'+
  '<button class="btn btn-secondary btn-sm" onclick="goPage('+(p+1)+')" '+(p>=tp?'disabled':'')+'>Next »</button>'+
  '<button class="btn btn-secondary btn-sm" onclick="goPage('+tp+')" '+(p>=tp?'disabled':'')+'>»»</button></div>';}
function goPage(p){currentPage=p;loadData();}
function resetFilters(){document.getElementById('dateFrom').value='';document.getElementById('dateTo').value='';document.getElementById('carrier').value='';document.getElementById('logisticsStatus').value='';document.getElementById('search').value='';document.getElementById('dateHint').textContent='(by created date)';currentPage=1;loadData();}
function exportCSV(){var p=new URLSearchParams(getFilters());p.delete('page');p.delete('page_size');p.set('export','csv');window.open('/recon/shipping/api/orders?'+p,'_blank');}
function fmtNum(n){if(n===null||n===undefined)return'0.00';return Number(n).toLocaleString('en-PH',{minimumFractionDigits:2,maximumFractionDigits:2});}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function copyToClipboard(el){var text=el.getAttribute('data-copy');navigator.clipboard.writeText(text).then(function(){el.textContent='✅';setTimeout(function(){el.textContent='📋';},1500);}).catch(function(){prompt('Copy:',text);});}
loadStatuses();
</script>
</body>
</html>"""
