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
    o.status AS order_status,
    CASE
        WHEN ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife') THEN
            CASE COALESCE(r.metadata->>'gcash_refund_status', '')
                WHEN 'SUCCESS' THEN 'SUCCESS'
                WHEN 'MANUAL_REQUIRED' THEN 'MANUAL_REQUIRED'
                ELSE 'UNCONFIRMED'
            END
        ELSE 'N/A'
    END AS execution_status,
    COALESCE(r.metadata->>'gcash_refund_status', '') AS gcash_refund_status
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
    COALESCE(AVG(r.amount), 0) AS avg_refund_amount,
    COALESCE(SUM(CASE WHEN ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife') AND COALESCE(r.metadata->>'gcash_refund_status', '') = 'SUCCESS' THEN 1 ELSE 0 END), 0) AS success_count,
    COALESCE(SUM(CASE WHEN ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife') AND COALESCE(r.metadata->>'gcash_refund_status', '') = 'MANUAL_REQUIRED' THEN 1 ELSE 0 END), 0) AS manual_count,
    COALESCE(SUM(CASE WHEN ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife') AND COALESCE(r.metadata->>'gcash_refund_status', '') = 'MANUAL_REQUIRED' THEN r.amount ELSE 0 END), 0) AS manual_amount,
    COALESCE(SUM(CASE WHEN ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife') AND COALESCE(r.metadata->>'gcash_refund_status', '') NOT IN ('SUCCESS', 'MANUAL_REQUIRED') THEN 1 ELSE 0 END), 0) AS unconfirmed_count,
    COALESCE(SUM(CASE WHEN ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife') AND COALESCE(r.metadata->>'gcash_refund_status', '') NOT IN ('SUCCESS', 'MANUAL_REQUIRED') THEN r.amount ELSE 0 END), 0) AS unconfirmed_amount
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
    execution_status = filters.get('executionStatus', '')
    search = filters.get('search', '')
    
    if date_from:
        conditions.append("(r.created_at AT TIME ZONE 'Asia/Manila')::date >= %s")
        params.append(date_from)
    
    if date_to:
        conditions.append("(r.created_at AT TIME ZONE 'Asia/Manila')::date <= %s")
        params.append(date_to)
    
    if refund_reason:
        conditions.append("rr.id = %s")
        params.append(refund_reason)
    
    if payment_status:
        conditions.append("pc.status = %s")
        params.append(payment_status)

    if execution_status:
        if execution_status == 'N/A':
            conditions.append("ps.provider_id NOT IN ('pp_gcash_webpay', 'pp_gcashmp_glife')")
        elif execution_status == 'UNCONFIRMED':
            conditions.append("ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife') AND COALESCE(r.metadata->>'gcash_refund_status', '') NOT IN ('SUCCESS', 'MANUAL_REQUIRED')")
        else:
            conditions.append("ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife') AND COALESCE(r.metadata->>'gcash_refund_status', '') = %s")
            params.append(execution_status)
    
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
            'executionStatus': query_dict.get('executionStatus', [''])[0],
            'search': query_dict.get('search', [''])[0],
        }
        
        where_clause, where_params = _build_where(filters)
        
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if export == 'csv':
            data_sql = f"{_BASE_SQL_TEMPLATE} AND {where_clause} ORDER BY r.created_at DESC LIMIT 5000"
            cur.execute(data_sql, where_params)
            rows = cur.fetchall()
            cur.close()
            conn.close()
            csv_body = _render_csv(rows)
            return 200, "text/csv", csv_body.encode(), True

        # Count query
        count_sql = f"SELECT COUNT(*) FROM ({_BASE_SQL_TEMPLATE} AND {where_clause}) AS cnt"
        
        # Data query
        offset = (page - 1) * page_size
        data_sql = f"{_BASE_SQL_TEMPLATE} AND {where_clause} ORDER BY r.created_at DESC LIMIT %s OFFSET %s"
        
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
    cols = ['order_id', 'refund_id', 'refund_date', 'refund_amount', 'refund_reason', 'refund_note', 'order_date', 'merchant', 'buyer_name', 'buyer_email', 'payment_amount', 'payment_provider', 'payment_method', 'payment_status', 'order_status', 'execution_status', 'gcash_refund_status']
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=cols)
    writer.writeheader()
    for row in rows:
        writer.writerow({col: row.get(col, '') for col in cols})
    return output.getvalue()


def handle_refunds_escrow_only_api(query_dict):
    """Orders refunded at escrow level with NO payment-level refund (no provider reversal).
    These are 'refunds not processed by GCash/Xendit' by definition."""
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT DISTINCT ON (er.order_id)
                COALESCE(oe.order_sn, o.id) AS order_id,
                o.created_at AT TIME ZONE 'Asia/Manila' AS order_date,
                COALESCE(s.name, 'Unknown') AS merchant,
                COALESCE(er.refunded_amount, 0) AS refunded_amount,
                er.status AS escrow_status
            FROM public.escrow_record er
            JOIN public."order" o ON o.id = er.order_id AND o.deleted_at IS NULL
            LEFT JOIN public.order_extension oe ON oe.order_id = o.id
            LEFT JOIN public.seller s ON s.id = (o.metadata->>'seller_id')
            WHERE er.deleted_at IS NULL AND er.status = 'refunded' AND COALESCE(er.refunded_amount, 0) > 0
              AND NOT EXISTS (
                  SELECT 1 FROM refund r
                  JOIN payment p ON p.id = r.payment_id AND p.deleted_at IS NULL
                  JOIN payment_session ps ON ps.id = p.payment_session_id AND ps.deleted_at IS NULL
                  JOIN order_payment_collection opc ON opc.payment_collection_id = ps.payment_collection_id
                  WHERE opc.order_id = er.order_id AND r.deleted_at IS NULL
              )
            ORDER BY er.order_id, er.created_at DESC
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d['order_date'] = d['order_date'].strftime('%Y-%m-%d %H:%M') if d['order_date'] else ''
            d['refunded_amount'] = float(d['refunded_amount'])
            out.append(d)
        return 200, "application/json", json.dumps({"rows": out}).encode(), True
    except Exception as e:
        import traceback; traceback.print_exc()
        return 500, "application/json", json.dumps({"error": str(e)}).encode(), True


def handle_refunds_reconcile_api(body_json):
    """Match uploaded GCash/Xendit refund/reversal CSV rows against the refund ledger.
    CSV columns: reference, amount, date. Reference = payment reference / session id / order #."""
    try:
        rows = body_json.get("rows", [])
        if not rows or not isinstance(rows, list):
            return 400, "application/json", json.dumps({"error": "rows array required"}).encode(), True
        if len(rows) > 20000:
            return 400, "application/json", json.dumps({"error": "max 20,000 rows"}).encode(), True
        refs = []
        for r in rows:
            ref = str(r.get("reference", "") or "").strip()
            if ref and ref not in refs:
                refs.append(ref)
        db_index = {}
        if refs:
            conn = get_db()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT
                    ps.id AS session_id,
                    COALESCE(p.data->>'payment_reference', '') AS payment_reference,
                    COALESCE(oe.order_sn, o.id) AS order_id,
                    COALESCE(SUM(r.amount), 0) AS total_refunded,
                    COUNT(DISTINCT r.id) AS refund_count,
                    CASE WHEN ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife') THEN 'GCash'
                         WHEN ps.provider_id = 'pp_xendit' THEN 'Xendit'
                         ELSE COALESCE(ps.provider_id, 'unknown') END AS provider,
                    ps.status AS payment_status
                FROM refund r
                JOIN payment p ON p.id = r.payment_id AND p.deleted_at IS NULL
                JOIN payment_session ps ON ps.id = p.payment_session_id AND ps.deleted_at IS NULL
                LEFT JOIN order_payment_collection opc ON opc.payment_collection_id = ps.payment_collection_id
                LEFT JOIN public."order" o ON o.id = opc.order_id AND o.deleted_at IS NULL
                LEFT JOIN order_extension oe ON oe.order_id = o.id
                WHERE r.deleted_at IS NULL
                  AND (p.data->>'payment_reference' = ANY(%s) OR ps.id = ANY(%s)
                       OR COALESCE(oe.order_sn, o.id) = ANY(%s))
                GROUP BY ps.id, p.data->>'payment_reference', oe.order_sn, o.id, ps.provider_id, ps.status
            """, (refs, refs, refs))
            for row in cur.fetchall():
                for k in (row.get("payment_reference"), row.get("session_id"), row.get("order_id")):
                    if k:
                        db_index.setdefault(k, row)
            cur.close()
            conn.close()
        results = []
        for r in rows:
            ref = str(r.get("reference", "") or "").strip()
            if not ref:
                continue
            csv_amt = float(r.get("amount") or 0)
            db = db_index.get(ref)
            if not db:
                results.append({"reference": ref, "csv_amount": csv_amt, "db_amount": None,
                                "diff": None, "match_type": "not_found", "date": r.get("date", ""),
                                "provider": "", "order_id": "", "payment_status": "", "refund_count": 0})
                continue
            db_amt = float(db["total_refunded"] or 0)
            diff = round(csv_amt - db_amt, 2)
            match_type = "matched" if abs(diff) < 0.01 else "amount_mismatch"
            results.append({"reference": ref, "csv_amount": csv_amt, "db_amount": db_amt,
                            "diff": diff, "match_type": match_type, "date": r.get("date", ""),
                            "provider": db["provider"], "order_id": db["order_id"],
                            "payment_status": db["payment_status"], "refund_count": db["refund_count"]})
        return 200, "application/json", json.dumps({"results": results}).encode(), True
    except Exception as e:
        import traceback; traceback.print_exc()
        return 500, "application/json", json.dumps({"error": str(e)}).encode(), True


_GCASH_PROVIDERS = ('pp_gcash_webpay', 'pp_gcashmp_glife')
_PROVIDER_MAP = {
    'GCash': _GCASH_PROVIDERS,
    'Xendit': ('pp_xendit',),
    'Stripe': ('pp_card_stripe-connect',),
    'System': ('pp_system_default',),
}
_ALLOWED_ANCHOR_STATUSES = ('ALL', 'SUCCESS', 'MANUAL_REQUIRED', 'UNCONFIRMED', 'N/A')


def handle_refunds_reconcile_anchor_api(body_json):
    """Ledger-anchored recon: OUR refund ledger for a date range (+ provider + execution
    status) is the completeness anchor. Uploaded CSV rows (optional) act as evidence:
    each anchor row is marked matched / amount_mismatch / missing_from_csv.
    CSV rows matching no ledger reference are returned as 'not_in_ledger' extras."""
    try:
        date_from = str(body_json.get('dateFrom', '') or '').strip()
        date_to = str(body_json.get('dateTo', '') or '').strip()
        provider = str(body_json.get('provider', 'all') or 'all').strip()
        status = str(body_json.get('executionStatus', 'SUCCESS') or 'SUCCESS').strip()
        rows = body_json.get('rows') or []

        if not date_from or not date_to:
            return 400, "application/json", json.dumps({"error": "dateFrom and dateTo required"}).encode(), True
        try:
            datetime.strptime(date_from, '%Y-%m-%d')
            datetime.strptime(date_to, '%Y-%m-%d')
        except ValueError:
            return 400, "application/json", json.dumps({"error": "dates must be YYYY-MM-DD"}).encode(), True
        if provider not in ('all',) + tuple(_PROVIDER_MAP.keys()):
            return 400, "application/json", json.dumps({"error": "invalid provider"}).encode(), True
        if status not in _ALLOWED_ANCHOR_STATUSES:
            return 400, "application/json", json.dumps({"error": "invalid executionStatus"}).encode(), True

        provider_clause = ""
        if provider != 'all':
            provider_clause = "AND ps.provider_id = ANY(%s)"
        status_clause = ""
        if status == 'SUCCESS':
            status_clause = "AND ps.provider_id = ANY(%s) AND r.metadata->>'gcash_refund_status' = 'SUCCESS'"
        elif status == 'MANUAL_REQUIRED':
            status_clause = "AND ps.provider_id = ANY(%s) AND r.metadata->>'gcash_refund_status' = 'MANUAL_REQUIRED'"
        elif status == 'UNCONFIRMED':
            status_clause = "AND ps.provider_id = ANY(%s) AND COALESCE(r.metadata->>'gcash_refund_status', '') NOT IN ('SUCCESS', 'MANUAL_REQUIRED')"
        elif status == 'N/A':
            status_clause = "AND ps.provider_id <> ALL(%s)"

        params = [date_from, date_to]
        if provider_clause:
            params.append(list(_PROVIDER_MAP[provider]))
        if status in ('SUCCESS', 'MANUAL_REQUIRED', 'UNCONFIRMED', 'N/A'):
            params.append(list(_GCASH_PROVIDERS))

        sql = """
            SELECT
                r.id AS refund_id,
                COALESCE(oe.order_sn, o.id) AS order_id,
                r.amount AS refund_amount,
                (r.created_at AT TIME ZONE 'Asia/Manila')::timestamp AS refund_date,
                COALESCE(s.name, 'Unknown') AS merchant,
                COALESCE(p.data->>'payment_reference', '') AS payment_reference,
                ps.id AS session_id,
                CASE WHEN ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife') THEN 'GCash'
                     WHEN ps.provider_id = 'pp_xendit' THEN 'Xendit'
                     WHEN ps.provider_id = 'pp_card_stripe-connect' THEN 'Stripe'
                     ELSE 'System' END AS provider,
                COALESCE(pc.status, 'N/A') AS payment_status,
                CASE WHEN ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife') THEN
                    CASE COALESCE(r.metadata->>'gcash_refund_status', '')
                        WHEN 'SUCCESS' THEN 'SUCCESS'
                        WHEN 'MANUAL_REQUIRED' THEN 'MANUAL_REQUIRED'
                        ELSE 'UNCONFIRMED' END
                ELSE 'N/A' END AS execution_status
            FROM public.refund r
            JOIN public.payment p ON p.id = r.payment_id AND p.deleted_at IS NULL
            JOIN public.payment_session ps ON ps.id = p.payment_session_id AND ps.deleted_at IS NULL
            LEFT JOIN public.order_payment_collection opc ON opc.payment_collection_id = ps.payment_collection_id
            LEFT JOIN public."order" o ON o.id = opc.order_id AND o.deleted_at IS NULL
            LEFT JOIN public.order_extension oe ON oe.order_id = o.id
            LEFT JOIN public.seller s ON s.id = (o.metadata->>'seller_id')
            LEFT JOIN public.payment_collection pc ON pc.id = ps.payment_collection_id AND pc.deleted_at IS NULL
            WHERE r.deleted_at IS NULL
              AND (r.created_at AT TIME ZONE 'Asia/Manila')::date BETWEEN %s AND %s
              {provider_clause}
              {status_clause}
            ORDER BY refund_date
        """.format(provider_clause=provider_clause, status_clause=status_clause)

        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        db_rows = cur.fetchall()
        cur.close()
        conn.close()

        # CSV evidence index (optional)
        csv_by_ref = {}
        if rows:
            for r in rows:
                ref = str(r.get('reference', '') or '').strip()
                if not ref:
                    continue
                amt = 0.0
                try:
                    amt = float(r.get('amount') or 0)
                except (TypeError, ValueError):
                    amt = 0.0
                entry = csv_by_ref.setdefault(ref, {'total': 0.0, 'n': 0})
                entry['total'] += amt
                entry['n'] += 1

        all_db_keys = set()
        for row in db_rows:
            for k in (row['payment_reference'], row['session_id'], row['order_id']):
                if k:
                    all_db_keys.add(k)

        # group DB refunds by primary key (payment_reference > session_id > order_sn)
        db_groups = {}
        for row in db_rows:
            pk = row['payment_reference'] or row['session_id'] or row['order_id']
            g = db_groups.setdefault(pk, {'total': 0.0, 'n': 0})
            g['total'] += float(row['refund_amount'] or 0)
            g['n'] += 1

        out_rows = []
        matched = missing = mismatch = 0
        matched_amt = missing_amt = mismatch_amt = 0.0
        for row in db_rows:
            d = {
                'refund_id': row['refund_id'],
                'order_id': row['order_id'],
                'refund_date': row['refund_date'].strftime('%Y-%m-%d %H:%M:%S') if row['refund_date'] else '',
                'amount': float(row['refund_amount'] or 0),
                'merchant': row['merchant'],
                'provider': row['provider'],
                'payment_status': row['payment_status'],
                'execution_status': row['execution_status'],
            }
            pk = row['payment_reference'] or row['session_id'] or row['order_id']
            g = db_groups[pk]
            csv_hit = None
            for k in (row['payment_reference'], row['session_id'], row['order_id']):
                if k and k in csv_by_ref:
                    csv_hit = csv_by_ref[k]
                    break
            if csv_hit is None:
                d['verdict'] = 'missing'
                d['csv_amount'] = None
                d['diff'] = None
                missing += 1
                missing_amt += d['amount']
            else:
                diff = round(csv_hit['total'] - g['total'], 2)
                d['csv_amount'] = round(csv_hit['total'], 2)
                d['diff'] = diff
                if abs(diff) < 0.01:
                    d['verdict'] = 'matched'
                    matched += 1
                    matched_amt += d['amount']
                else:
                    d['verdict'] = 'amount_mismatch'
                    mismatch += 1
                    mismatch_amt += d['amount']
            out_rows.append(d)

        extras = []
        for ref, info in csv_by_ref.items():
            if ref not in all_db_keys:
                extras.append({'reference': ref, 'csv_amount': round(info['total'], 2), 'csv_count': info['n']})

        anchor_total = len(out_rows)
        stats = {
            'anchor_total': anchor_total,
            'anchor_amount': round(sum(r['amount'] for r in out_rows), 2),
            'matched': matched,
            'matched_amount': round(matched_amt, 2),
            'missing': missing,
            'missing_amount': round(missing_amt, 2),
            'mismatch': mismatch,
            'mismatch_amount': round(mismatch_amt, 2),
            'extras': len(extras),
            'extras_amount': round(sum(e['csv_amount'] for e in extras), 2),
            'completeness_pct': round(matched / anchor_total * 100, 2) if anchor_total else 100.0,
            'csv_evidence': bool(rows),
        }
        return 200, "application/json", json.dumps({"stats": stats, "rows": out_rows, "extras": extras}).encode(), True
    except Exception as e:
        import traceback; traceback.print_exc()
        return 500, "application/json", json.dumps({"error": str(e)}).encode(), True


_HTML_TEMPLATE = r"""<!DOCTYPE html>
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
  .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
  .tab-btn { padding: 8px 18px; border-radius: 8px; font-size: 13px; font-weight: 500; cursor: pointer; border: 1px solid var(--border); background: var(--card); color: var(--dim); transition: all .15s; }
  .tab-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .tab-content { display: none; } .tab-content.active { display: block; }
  .upload-zone { border: 2px dashed var(--border); border-radius: 8px; padding: 40px; text-align: center; cursor: pointer; transition: all .15s; margin-bottom: 16px; background: var(--card); }
  .upload-zone:hover, .upload-zone.dragover { border-color: var(--accent); background: rgba(108,140,255,.05); }
  .upload-zone .upload-icon { font-size: 32px; margin-bottom: 8px; }
  .upload-zone .upload-title { font-size: 14px; font-weight: 500; margin-bottom: 4px; }
  .upload-zone .upload-hint { font-size: 11px; color: var(--dim); }
  .upload-zone input[type=file] { display: none; }
  .preview-box { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  .preview-box h3 { font-size: 14px; margin-bottom: 12px; }
  .mappings { display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 12px; }
  .mapping { font-size: 12px; } .mapping .mfield { color: var(--dim); text-transform: uppercase; font-size: 10px; letter-spacing: .4px; } .mapping .col { font-weight: 600; } .mapping .warn { color: var(--amber); } .mapping .check { color: var(--green); }
  .preview-table-wrap { overflow: auto; max-height: 220px; }
  .match-badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; }
  .match-matched { background: rgba(60,205,92,.15); color: var(--green); }
  .match-mismatch { background: rgba(255,165,2,.15); color: var(--amber); }
  .match-not-found { background: rgba(255,71,87,.15); color: var(--red); }
  .match-escrow { background: rgba(108,140,255,.15); color: var(--accent); }
  .blue { color: var(--accent); }
  .exec-badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>↩️ Refunds Reconciliation</h1>
    <div class="nav"><a href="/recon/" class="btn btn-secondary btn-sm">← Portal Home</a><span class="badge">Production DB</span></div>
  </header>
  
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('download')" id="tab-download">📋 Download Board</button>
    <button class="tab-btn" onclick="switchTab('reconcile')" id="tab-reconcile">🔄 Reconcile (GCash/Xendit)</button>
  </div>
  <div class="tab-content active" id="download-tab">
  <div class="filters">
    <div class="filter-group"><label>Date From</label><input type="date" id="dateFrom"></div>
    <div class="filter-group"><label>Date To</label><input type="date" id="dateTo"></div>
    <div class="filter-group"><label>Refund Reason</label><select id="refundReason"><option value="">All</option></select></div>
    <div class="filter-group"><label>Payment Status</label><select id="paymentStatus"><option value="">All</option><option value="completed">Completed</option><option value="authorized">Authorized</option><option value="canceled">Canceled</option><option value="not_paid">Not Paid</option></select></div>
    <div class="filter-group"><label>Execution Status</label><select id="executionStatus"><option value="">All</option><option value="UNCONFIRMED">❓ Unconfirmed (GCash)</option><option value="MANUAL_REQUIRED">⚠️ Manual Required</option><option value="SUCCESS">✅ Success</option><option value="N/A">— N/A (non-GCash)</option></select></div>
    <div class="filter-group"><label>Search (Order / Merchant / Email)</label><input type="text" id="search" placeholder="e.g. order ID, merchant, email"></div>
    <button class="btn btn-primary" onclick="fetchData()">🔍 Filter</button>
    <button class="btn btn-secondary" onclick="resetFilters()">↺ Reset</button>
    <button class="btn btn-secondary" onclick="exportCSV()">⬇️ CSV</button>
  </div>
  
  <div id="stats" class="stats"></div>
  
  <div class="table-wrap">
    <table><thead><tr><th>Refund ID <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Order # <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Refund Date <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Order Date <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Merchant <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Buyer <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Email <span style="font-size:9px; color:var(--dim)">📋</span></th><th class="amount">Payment Amt <span style="font-size:9px; color:var(--dim)">📋</span></th><th class="amount">Refund Amt <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Reason <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Note <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Provider <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Method <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Payment Status <span style="font-size:9px; color:var(--dim)">📋</span></th><th>Execution <span style="font-size:9px; color:var(--dim)">⚡</span></th><th>Order Status <span style="font-size:9px; color:var(--dim)">📋</span></th></tr></thead>
    <tbody id="tbody"><tr><td colspan="15" class="loading">Loading data...</td></tr></tbody>
    </table>
  </div>
  
  <div class="pagination" id="pagination"></div>
  </div><!-- /download-tab -->

  <!-- RECONCILE TAB -->
  <div class="tab-content" id="reconcile-tab">
    <div style="margin-bottom:14px;display:flex;gap:8px;flex-wrap:wrap;">
      <button class="btn btn-primary" id="modeCsvBtn" onclick="switchReconMode('csv')">📄 CSV-Based Recon</button>
      <button class="btn btn-secondary" id="modeAnchorBtn" onclick="switchReconMode('anchor')">📒 Ledger Anchor Recon</button>
    </div>
    <div id="anchorPanel" style="display:none;margin-bottom:14px;padding:14px;background:#1a2130;border:1px solid #2a3550;border-radius:10px;">
      <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;">
        <div class="filter-group"><label>Date From</label><input type="date" id="anchorDateFrom"></div>
        <div class="filter-group"><label>Date To</label><input type="date" id="anchorDateTo"></div>
        <div class="filter-group"><label>Provider</label><select id="anchorProvider">
          <option value="all" selected>All</option><option value="GCash">GCash</option><option value="Xendit">Xendit</option>
          <option value="Stripe">Stripe</option><option value="System">System</option></select></div>
        <div class="filter-group"><label>Anchor Status</label><select id="anchorStatus">
          <option value="SUCCESS" selected>✅ SUCCESS (default)</option>
          <option value="ALL">All statuses</option>
          <option value="MANUAL_REQUIRED">⚠️ MANUAL_REQUIRED</option>
          <option value="UNCONFIRMED">❓ Unconfirmed</option>
          <option value="N/A">— N/A (non-GCash)</option></select></div>
        <button class="btn btn-primary" id="runAnchorBtn" onclick="runAnchorRecon()">📒 Run Anchor Recon</button>
      </div>
      <div style="margin-top:10px;font-size:12px;color:var(--dim);line-height:1.6;">
        <b>Anchor</b> = every refund in <b>our</b> ledger for the date range + status — this is the completeness basis, not the CSV.<br>
        CSV upload above is <b>optional evidence</b>: refunds missing from the CSV are flagged ❌ (completeness gap), amount differences ⚠️, CSV rows with no ledger match ➕.<br>
        SUCCESS = GCash reversals confirmed by GCash. Use <i>All</i> / <i>Unconfirmed</i> / <i>Manual</i> to investigate other slices. N/A = non-GCash providers (Xendit/Stripe/System).
      </div>
    </div>
    <div class="upload-zone" id="uploadZone" onclick="document.getElementById('csvUpload').click()">
      <div class="upload-icon">📁</div>
      <div class="upload-title">Upload GCash / Xendit Refund CSV</div>
      <div class="upload-hint">Drag & drop or click. Needs: Reference (payment ref / session ID / order #), Amount. Matches against the refund ledger.</div>
      <input type="file" id="csvUpload" accept=".csv" onchange="handleCSVUpload(event)">
    </div>
    <div class="preview-box" id="previewBox" style="display:none">
      <h3>📊 CSV Preview & Column Mapping</h3>
      <div class="mappings" id="mappings"></div>
      <div class="preview-table-wrap" id="previewTable"></div>
      <div style="margin-top:12px;display:flex;gap:8px;">
        <button class="btn btn-primary" id="runReconcileBtn" onclick="runReconcile()">🔄 Run Reconciliation</button>
        <button class="btn btn-secondary" onclick="resetReconcile()">↺ Clear</button>
      </div>
    </div>
    <div style="margin-bottom:16px;">
      <button class="btn btn-secondary" onclick="loadEscrowOnly()">🚩 Load Escrow-Only Refunds (no provider reversal)</button>
    </div>
    <div id="reconcile-status" style="display:none"></div>
    <div class="stats" id="reconcileStats" style="display:none"></div>
    <div class="filters" id="reconcileFilters" style="display:none">
      <div class="filter-group"><label>Search</label><input type="text" id="reconSearch" placeholder="Reference, Order #" oninput="filterReconcileResults()"></div>
      <div class="filter-group"><label>Match Type</label><select id="reconMatchType" onchange="filterReconcileResults()"><option value="">All</option><option value="matched">✅ Matched</option><option value="amount_mismatch">⚠ Amount Mismatch</option><option value="not_found">❌ Not Found</option><option value="escrow_only">🚩 Escrow-Only</option></select></div>
      <span id="reconFilterCount" style="color:var(--dim);font-size:12px;align-self:flex-end;padding-bottom:8px;"></span>
      <button class="btn btn-secondary btn-sm" onclick="document.getElementById('reconSearch').value='';document.getElementById('reconMatchType').value='';filterReconcileResults();">↺ Clear</button>
    </div>
    <div class="table-wrap" id="reconcileTableWrap" style="display:none">
      <table id="reconcileResults">
        <thead id="reconcileHead"><tr>
          <th>Match</th><th>Reference</th><th class="amount">CSV Amount</th><th class="amount">DB Refunded</th><th class="amount">Diff</th><th>File Date</th><th>Provider</th><th>Order #</th><th>Pay Status</th><th>Refund Rows</th>
        </tr></thead>
        <tbody id="reconcileTbody"></tbody>
      </table>
    </div>
    <div id="reconcileExport" style="display:none;margin-top:12px;">
      <button class="btn btn-secondary btn-sm" onclick="exportReconcileCSV()">📥 Export Reconciliation Report</button>
    </div>
  </div><!-- /reconcile-tab -->
</div>

<script>
let currentPage = 1;
function getTodayDate(){var today=new Date();var y=today.getFullYear();var m=String(today.getMonth()+1).padStart(2,'0');var d=String(today.getDate()).padStart(2,'0');return y+'-'+m+'-'+d;}
function getFilters(){return{dateFrom:document.getElementById('dateFrom').value||'',dateTo:document.getElementById('dateTo').value||'',refundReason:document.getElementById('refundReason').value||'',paymentStatus:document.getElementById('paymentStatus').value||'',executionStatus:document.getElementById('executionStatus').value||'',search:document.getElementById('search').value||''};}
function fetchData(){currentPage=1;loadData();}
function loadData(){var f=getFilters();var p=new URLSearchParams(Object.entries(f).filter(([k,v])=>v!==''));p.set('page',currentPage);p.set('page_size',50);fetch('/recon/refunds/api/orders?'+p).then(r=>r.json()).then(d=>{renderStats(d.stats);renderTable(d.rows);renderPagination(d.total,currentPage,50);}).catch(e=>alert('Error: '+e));}
function renderStats(s){if(!s)return;document.getElementById('stats').innerHTML='<div class="stat-card"><div class="value red">₱'+fmtNum(s.total_refund_amount||0)+'</div><div class="label">Total Refunded</div></div><div class="stat-card"><div class="value">'+(s.total_refunds||0)+'</div><div class="label">Refund Count</div></div><div class="stat-card"><div class="value">'+(s.total_orders_refunded||0)+'</div><div class="label">Orders Refunded</div></div><div class="stat-card"><div class="value">₱'+fmtNum(s.avg_refund_amount||0)+'</div><div class="label">Avg Refund</div></div><div class="stat-card"><div class="value green">'+(s.success_count||0)+'</div><div class="label">✅ GCash Success</div></div><div class="stat-card"><div class="value amber">'+(s.manual_count||0)+'</div><div class="label">⚠️ Manual Required</div></div><div class="stat-card"><div class="value red">'+(s.unconfirmed_count||0)+'</div><div class="label">❓ Unconfirmed (₱'+fmtNum(s.unconfirmed_amount||0)+')</div></div>';}
function execBadge(r){
  var e=r.execution_status||'N/A';
  if(e==='SUCCESS')return'<span class="exec-badge" style="background:rgba(60,205,92,.15);color:var(--green)">✅ Success</span>';
  if(e==='MANUAL_REQUIRED')return'<span class="exec-badge" style="background:rgba(255,165,2,.15);color:var(--amber)">⚠️ Manual Required</span>';
  if(e==='UNCONFIRMED')return'<span class="exec-badge" style="background:rgba(255,71,87,.15);color:var(--red)">❓ Unconfirmed</span>';
  return'<span style="color:var(--dim)">—</span>';
}
function renderTable(rows){var tb=document.getElementById('tbody');if(!rows||rows.length===0){tb.innerHTML='<tr><td colspan="16" class="empty">No refunds found</td></tr>';return;}tb.innerHTML=rows.map(r=>'<tr><td><code>'+esc(r.refund_id)+'</code> <span class=\"copy-btn\" data-copy=\"'+esc(r.refund_id)+'\" onclick=\"copyToClipboard(this)\" title=\"Copy\">📋</span></td><td><code>'+esc(r.order_id||'—')+'</code> <span class=\"copy-btn\" data-copy=\"'+esc(r.order_id||'')+'\" onclick=\"copyToClipboard(this)\" title=\"Copy\">📋</span></td><td>'+esc(r.refund_date)+'</td><td>'+esc(r.order_date||'—')+'</td><td>'+esc(r.merchant)+'</td><td>'+esc(r.buyer_name)+'</td><td>'+esc(r.buyer_email)+'</td><td class="amount">₱'+fmtNum(r.payment_amount)+'</td><td class="amount"><b>₱'+fmtNum(r.refund_amount)+'</b></td><td>'+esc(r.refund_reason||'—')+'</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis" title="'+esc(r.refund_note||'')+'">'+esc(r.refund_note||'—')+'</td><td>'+esc(r.payment_provider||'—')+'</td><td>'+esc(r.payment_method||'—')+'</td><td><span class="status status-'+(r.payment_status||'na')+'">'+esc(r.payment_status||'N/A')+'</span></td><td>'+execBadge(r)+'</td><td><span class="status status-'+esc(r.order_status||'pending')+'">'+esc(r.order_status||'pending')+'</span></td></tr>').join('');}
function renderPagination(t,p,ps){var tp=Math.ceil(t/ps);document.getElementById('pagination').innerHTML='<div class="info">Showing '+((p-1)*ps+1)+'–'+Math.min(p*ps,t)+' of '+t+' refunds</div><div class="btns"><button class="btn btn-secondary btn-sm" onclick="goPage(1)" '+(p<=1?'disabled':'')+'>««</button><button class="btn btn-secondary btn-sm" onclick="goPage('+(p-1)+')" '+(p<=1?'disabled':'')+'>« Prev</button><span style="padding:4px 12px;color:var(--dim)">Page '+p+' / '+tp+'</span><button class="btn btn-secondary btn-sm" onclick="goPage('+(p+1)+')" '+(p>=tp?'disabled':'')+'>Next »</button><button class="btn btn-secondary btn-sm" onclick="goPage('+tp+')" '+(p>=tp?'disabled':'')+'>»»</button></div>';}
function goPage(p){currentPage=p;loadData();}
function resetFilters(){document.getElementById('dateFrom').value='';document.getElementById('dateTo').value='';document.getElementById('refundReason').value='';document.getElementById('paymentStatus').value='';document.getElementById('executionStatus').value='';document.getElementById('search').value='';currentPage=1;loadData();}
function exportCSV(){var p=new URLSearchParams(getFilters());p.set('export','csv');window.open('/recon/refunds/api/orders?'+p,'_blank');}
function fmtNum(n){if(n===null||n===undefined)return'0.00';return Number(n).toLocaleString('en-PH',{minimumFractionDigits:2,maximumFractionDigits:2});}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function copyToClipboard(el){var text=el.getAttribute('data-copy');navigator.clipboard.writeText(text).then(function(){el.textContent='✅';setTimeout(function(){el.textContent='📋';},1500);}).catch(function(){prompt('Copy:',text);});}
// ─── Reconcile ────────────────────────────────────────────
var csvData=[],csvHeaders=[],colMap={},reconcileResults=[],reconMode='csv',anchorStats=null;
function switchTab(t){
  document.querySelectorAll('.tab-btn').forEach(function(b){b.classList.remove('active');});
  document.querySelectorAll('.tab-content').forEach(function(c){c.classList.remove('active');});
  document.getElementById('tab-'+t).classList.add('active');
  document.getElementById(t+'-tab').classList.add('active');
}
function handleCSVUpload(e){
  var file=e.target.files[0];if(!file)return;
  var reader=new FileReader();
  reader.onload=function(ev){parseCSV(ev.target.result);};
  reader.readAsText(file);
}
var uzone=document.getElementById('uploadZone');
uzone.addEventListener('dragover',function(e){e.preventDefault();uzone.classList.add('dragover');});
uzone.addEventListener('dragleave',function(e){e.preventDefault();uzone.classList.remove('dragover');});
uzone.addEventListener('drop',function(e){e.preventDefault();uzone.classList.remove('dragover');var f=e.dataTransfer.files[0];if(f){var r=new FileReader();r.onload=function(ev){parseCSV(ev.target.result);};r.readAsText(f);}});
function parseCSVLine(l){var r=[],c='',q=false;for(var i=0;i<l.length;i++){var ch=l[i];if(q){if(ch=='"'){if(i+1<l.length&&l[i+1]=='"'){c+='"';i++;}else{q=false;}}else{c+=ch;}}else if(ch=='"'){q=true;}else if(ch===','){r.push(c);c='';}else{c+=ch;}}r.push(c);return r;}
function findCol(rx){for(var i=0;i<csvHeaders.length;i++){if(rx.test(csvHeaders[i]))return csvHeaders[i];}return null;}
function parseCSV(text){
  var lines=text.replace(/\r/g,'').split('\n').filter(function(l){return l.trim();});
  if(lines.length<2){alert('CSV must have a header row + at least 1 data row');return;}
  csvHeaders=parseCSVLine(lines[0]);csvData=[];
  for(var i=1;i<lines.length;i++){
    var vals=parseCSVLine(lines[i]);
    if(vals.length===csvHeaders.length){
      var row={};csvHeaders.forEach(function(h,j){row[h]=vals[j];});csvData.push(row);
    }
  }
  colMap={};
  colMap.reference=findCol(/reference|ref.?id|disbursement|payment.?id|order|transaction|reversal/i);
  colMap.amount=findCol(/amount|value|fee|net/i);
  colMap.date=findCol(/date|created|processed/i);
  var mapHtml='';
  var flds=[{k:'reference',l:'Reference'},{k:'amount',l:'Amount'},{k:'date',l:'Date'}];
  flds.forEach(function(f){
    var v=colMap[f.k];
    mapHtml+='<div class="mapping"><div class="mfield">'+f.l+'</div><div class="col">'+(v||'<span class="warn">⚠ not found</span>')+'</div>'+(v?'<span class="check">✅</span>':'')+'</div>';
  });
  document.getElementById('mappings').innerHTML=mapHtml;
  var previewRows=csvData.slice(0,5);
  var thHtml='<tr>'+csvHeaders.map(function(h){return'<th>'+esc(h)+'</th>';}).join('')+'</tr>';
  var trHtml=previewRows.map(function(r){return'<tr>'+csvHeaders.map(function(h){return'<td>'+esc(String(r[h]||''))+'</td>';}).join('')+'</tr>';}).join('');
  document.getElementById('previewTable').innerHTML='<table>'+thHtml+trHtml+'</table>';
  document.getElementById('previewBox').style.display='block';
  document.getElementById('uploadZone').style.display='none';
}
function resetReconcile(){
  csvData=[];csvHeaders=[];colMap={};reconcileResults=[];
  document.getElementById('previewBox').style.display='none';
  document.getElementById('uploadZone').style.display='block';
  document.getElementById('reconcileStats').style.display='none';
  document.getElementById('reconcileFilters').style.display='none';
  document.getElementById('reconcileTableWrap').style.display='none';
  document.getElementById('reconcileExport').style.display='none';
  document.getElementById('reconcile-status').style.display='none';
  document.getElementById('csvUpload').value='';
}
function runReconcile(){
  if(reconMode==='anchor'){runAnchorRecon();return;}
  var refCol=colMap.reference;
  if(!refCol){alert('No Reference column found in CSV');return;}
  var btn=document.getElementById('runReconcileBtn');
  btn.disabled=true;btn.textContent='⏳ Matching...';
  document.getElementById('reconcile-status').style.display='none';
  var amtCol=colMap.amount,dtCol=colMap.date;
  var rows=csvData.map(function(r){
    return {reference:String(r[refCol]||'').trim(),
            amount:amtCol?parseFloat(String(r[amtCol]).replace(/[^0-9.-]/g,''))||0:0,
            date:dtCol?(r[dtCol]||''):''};
  }).filter(function(r){return r.reference!=='';});
  fetch('/recon/refunds/api/reconcile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rows:rows})})
  .then(function(r){return r.json();})
  .then(function(d){
    if(d.error){document.getElementById('reconcile-status').innerHTML='<div class="error">'+esc(d.error)+'</div>';document.getElementById('reconcile-status').style.display='block';btn.disabled=false;btn.textContent='🔄 Run Reconciliation';return;}
    reconcileResults=d.results||[];
    document.getElementById('reconcileFilters').style.display='flex';
    filterReconcileResults();
    btn.disabled=false;btn.textContent='🔄 Run Reconciliation';
  })
  .catch(function(e){document.getElementById('reconcile-status').innerHTML='<div class="error">Error: '+esc(e.message)+'</div>';document.getElementById('reconcile-status').style.display='block';btn.disabled=false;btn.textContent='🔄 Run Reconciliation';});
}
function switchReconMode(mode){
  reconMode=mode;
  document.getElementById('modeCsvBtn').className='btn '+(mode==='csv'?'btn-primary':'btn-secondary');
  document.getElementById('modeAnchorBtn').className='btn '+(mode==='anchor'?'btn-primary':'btn-secondary');
  document.getElementById('anchorPanel').style.display=(mode==='anchor')?'block':'none';
  document.getElementById('runReconcileBtn').textContent=(mode==='anchor')?'📒 Run Anchor Recon':'🔄 Run Reconciliation';
  if(mode==='anchor'){
    var t=getTodayDate();
    if(!document.getElementById('anchorDateFrom').value){document.getElementById('anchorDateFrom').value=t;document.getElementById('anchorDateTo').value=t;}
  }
  resetReconcile();
}
function runAnchorRecon(){
  var df=document.getElementById('anchorDateFrom').value,dt=document.getElementById('anchorDateTo').value;
  if(!df||!dt){alert('Set Date From and Date To for the anchor');return;}
  var btn=document.getElementById('runAnchorBtn');
  btn.disabled=true;btn.textContent='⏳ Anchoring...';
  document.getElementById('reconcile-status').style.display='none';
  var payload={dateFrom:df,dateTo:dt,provider:document.getElementById('anchorProvider').value,executionStatus:document.getElementById('anchorStatus').value,rows:[]};
  if(csvData.length&&colMap.reference){
    var amtCol=colMap.amount,dtCol=colMap.date;
    payload.rows=csvData.map(function(r){return {reference:String(r[colMap.reference]||'').trim(),amount:amtCol?parseFloat(String(r[amtCol]).replace(/[^0-9.-]/g,''))||0:0,date:dtCol?(r[dtCol]||''):''};}).filter(function(r){return r.reference!=='';});
  }
  fetch('/recon/refunds/api/reconcile-anchor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
  .then(function(r){return r.json();})
  .then(function(d){
    btn.disabled=false;btn.textContent='📒 Run Anchor Recon';
    if(d.error){document.getElementById('reconcile-status').innerHTML='<div class="error">'+esc(d.error)+'</div>';document.getElementById('reconcile-status').style.display='block';return;}
    anchorStats=d.stats||null;
    reconcileResults=(d.rows||[]).map(function(x){return {match_type:x.verdict,reference:x.refund_id||'',refund_id:x.refund_id||'',csv_amount:x.csv_amount==null?null:x.csv_amount,db_amount:x.amount,diff:x.diff==null?null:x.diff,date:x.refund_date||'',provider:x.provider||'',order_id:x.order_id||'',payment_status:x.payment_status||'',exec_status:x.execution_status||'',refund_count:'',merchant:x.merchant||''};})
      .concat((d.extras||[]).map(function(x){return {match_type:'not_in_ledger',reference:x.reference||'',refund_id:'',csv_amount:x.csv_amount||0,db_amount:null,diff:null,date:'',provider:'',order_id:'',payment_status:'',exec_status:'',refund_count:x.csv_count||0,merchant:''};}));
    document.getElementById('reconcileFilters').style.display='flex';
    filterReconcileResults();
  })
  .catch(function(e){btn.disabled=false;btn.textContent='📒 Run Anchor Recon';document.getElementById('reconcile-status').innerHTML='<div class="error">Error: '+esc(e.message)+'</div>';document.getElementById('reconcile-status').style.display='block';});
}
function loadEscrowOnly(){
  if(reconMode==='anchor'){switchReconMode('csv');}
  var btn=event.target;btn.disabled=true;btn.textContent='⏳ Loading...';
  fetch('/recon/refunds/api/escrow-only').then(function(r){return r.json();}).then(function(d){
    btn.disabled=false;btn.textContent='🚩 Load Escrow-Only Refunds (no provider reversal)';
    if(d.error){document.getElementById('reconcile-status').innerHTML='<div class="error">'+esc(d.error)+'</div>';document.getElementById('reconcile-status').style.display='block';return;}
    reconcileResults=(d.rows||[]).map(function(x){
      return {reference:x.order_id||'',csv_amount:0,db_amount:x.refunded_amount,diff:null,match_type:'escrow_only',date:x.order_date||'',provider:'Escrow',order_id:x.order_id,payment_status:x.escrow_status||'',refund_count:0,seller:x.merchant||''};
    });
    document.getElementById('reconcileFilters').style.display='flex';
    filterReconcileResults();
  }).catch(function(e){btn.disabled=false;btn.textContent='🚩 Load Escrow-Only Refunds (no provider reversal)';document.getElementById('reconcile-status').innerHTML='<div class="error">Error: '+esc(e.message)+'</div>';document.getElementById('reconcile-status').style.display='block';});
}
function filterReconcileResults(){
  var opts=reconMode==='anchor'
    ?[['','All'],['matched','✅ Matched'],['amount_mismatch','⚠ Amount Mismatch'],['missing','❌ Missing from CSV'],['not_in_ledger','➕ CSV Not in Ledger']]
    :[['','All'],['matched','✅ Matched'],['amount_mismatch','⚠ Amount Mismatch'],['not_found','❌ Not Found'],['escrow_only','🚩 Escrow-Only']];
  var sel=document.getElementById('reconMatchType');
  var cur=sel.value;
  sel.innerHTML=opts.map(function(o){return'<option value="'+o[0]+'">'+o[1]+'</option>';}).join('');
  if(opts.some(function(o){return o[0]===cur;}))sel.value=cur;else sel.value='';
  var q=document.getElementById('reconSearch').value.toLowerCase();
  var mt=sel.value;
  var shown=reconcileResults.filter(function(r){
    if(mt&&r.match_type!==mt)return false;
    if(q&&!(String(r.reference).toLowerCase().indexOf(q)>=0||String(r.order_id||'').toLowerCase().indexOf(q)>=0))return false;
    return true;
  });
  renderReconcileStats(reconcileResults);
  renderReconcileTable(shown);
  document.getElementById('reconFilterCount').textContent=shown.length+' / '+reconcileResults.length+' rows';
  document.getElementById('reconcileStats').style.display='flex';
  document.getElementById('reconcileTableWrap').style.display='block';
  document.getElementById('reconcileExport').style.display='block';
}
function renderReconcileStats(r){
  if(reconMode==='anchor'&&anchorStats){
    var s=anchorStats;
    var csvTxt=s.csv_evidence?'':' <span style="font-size:11px;color:var(--dim)">(no CSV uploaded)</span>';
    var pctColor=s.completeness_pct>=100?'green':(s.completeness_pct>=90?'amber':'red');
    document.getElementById('reconcileStats').innerHTML=
      '<div class="stat-card"><div class="value">'+s.anchor_total+'</div><div class="label">Anchor Refunds</div></div>'+
      '<div class="stat-card"><div class="value green">'+s.matched+'</div><div class="label">✅ Matched (₱'+fmtNum(s.matched_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value red">'+s.missing+'</div><div class="label">❌ Missing from CSV (₱'+fmtNum(s.missing_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value amber">'+s.mismatch+'</div><div class="label">⚠ Amount Mismatch (₱'+fmtNum(s.mismatch_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value blue">'+s.extras+'</div><div class="label">➕ Not in Ledger (₱'+fmtNum(s.extras_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value '+pctColor+'">'+s.completeness_pct+'%</div><div class="label">Completeness'+csvTxt+'</div></div>'+
      '<div class="stat-card"><div class="value">₱'+fmtNum(s.anchor_amount)+'</div><div class="label">Anchor Total</div></div>';
    return;
  }
  var matched=r.filter(function(x){return x.match_type==='matched';}).length;
  var mismatch=r.filter(function(x){return x.match_type==='amount_mismatch';}).length;
  var notFound=r.filter(function(x){return x.match_type==='not_found';}).length;
  var escrow=r.filter(function(x){return x.match_type==='escrow_only';}).length;
  var tAmt=r.reduce(function(s,x){return s+x.csv_amount;},0);
  var tDiff=r.reduce(function(s,x){return s+Math.abs(x.diff||0);},0);
  var tEscrow=r.reduce(function(s,x){return s+((x.match_type==='escrow_only')?x.db_amount:0);},0);
  document.getElementById('reconcileStats').innerHTML=
    '<div class="stat-card"><div class="value">'+r.length+'</div><div class="label">Rows</div></div>'+
    '<div class="stat-card"><div class="value green">'+matched+'</div><div class="label">✅ Matched</div></div>'+
    '<div class="stat-card"><div class="value amber">'+mismatch+'</div><div class="label">⚠ Amount Mismatch</div></div>'+
    '<div class="stat-card"><div class="value red">'+notFound+'</div><div class="label">❌ Not Found</div></div>'+
    '<div class="stat-card"><div class="value blue">'+escrow+'</div><div class="label">🚩 Escrow-Only (₱'+fmtNum(tEscrow)+')</div></div>'+
    '<div class="stat-card"><div class="value blue">₱'+fmtNum(tAmt)+'</div><div class="label">Total CSV Amount</div></div>'+
    '<div class="stat-card"><div class="value red">₱'+fmtNum(tDiff)+'</div><div class="label">Total Variance</div></div>';
}
function renderReconcileTable(results){
  var tb=document.getElementById('reconcileTbody');
  var head=document.getElementById('reconcileHead');
  if(reconMode==='anchor'){
    head.innerHTML='<tr><th>Match</th><th>Refund ID</th><th>Order #</th><th>Refund Date</th><th class="amount">Amount</th><th class="amount">CSV Amt</th><th class="amount">Diff</th><th>Provider</th><th>Exec Status</th><th>Pay Status</th></tr>';
    if(results.length===0){tb.innerHTML='<tr><td colspan="10" class="empty">No results</td></tr>';return;}
    tb.innerHTML=results.map(function(r){
      var badge=r.match_type==='matched'?'<span class="match-badge match-matched">✅ Matched</span>'
        :r.match_type==='amount_mismatch'?'<span class="match-badge match-mismatch">⚠ Mismatch</span>'
        :r.match_type==='missing'?'<span class="match-badge match-not-found">❌ Missing from CSV</span>'
        :'<span class="match-badge match-escrow">➕ Not in Ledger</span>';
      var diffColor=Math.abs(r.diff||0)<0.01?'var(--green)':'var(--red)';
      return'<tr><td>'+badge+'</td><td><code>'+esc(r.refund_id||r.reference||'—')+'</code></td><td><code>'+esc(r.order_id||'—')+'</code></td>'+
        '<td>'+esc(r.date||'—')+'</td><td class="amount">'+(r.db_amount==null?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.db_amount))+'</td>'+
        '<td class="amount">'+(r.csv_amount==null?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.csv_amount))+'</td>'+
        '<td class="amount" style="color:'+diffColor+'">'+(r.diff==null?'—':((r.diff>=0?'+':'')+fmtNum(r.diff)))+'</td>'+
        '<td>'+esc(r.provider||'—')+'</td><td>'+esc(r.exec_status||'—')+'</td><td>'+esc(r.payment_status||'—')+'</td></tr>';
    }).join('');
    return;
  }
  head.innerHTML='<tr><th>Match</th><th>Reference</th><th class="amount">CSV Amount</th><th class="amount">DB Refunded</th><th class="amount">Diff</th><th>File Date</th><th>Provider</th><th>Order #</th><th>Pay Status</th><th>Refund Rows</th></tr>';
  if(results.length===0){tb.innerHTML='<tr><td colspan="10" class="empty">No results</td></tr>';return;}
  tb.innerHTML=results.map(function(r){
    var badge='';
    if(r.match_type==='matched')badge='<span class="match-badge match-matched">✅ Matched</span>';
    else if(r.match_type==='amount_mismatch')badge='<span class="match-badge match-mismatch">⚠ Mismatch</span>';
    else if(r.match_type==='escrow_only')badge='<span class="match-badge match-escrow">🚩 Escrow-Only</span>';
    else badge='<span class="match-badge match-not-found">❌ Not Found</span>';
    var diffColor=Math.abs(r.diff||0)<0.01?'var(--green)':'var(--red)';
    return'<tr><td>'+badge+'</td><td><code>'+esc(r.reference)+'</code></td>'+
      '<td class="amount">₱'+fmtNum(r.csv_amount)+'</td>'+
      '<td class="amount">'+(r.db_amount==null?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.db_amount))+'</td>'+
      '<td class="amount" style="color:'+diffColor+'">'+(r.diff==null?'—':((r.diff>=0?'+':'')+fmtNum(r.diff)))+'</td>'+
      '<td>'+esc(r.date||'—')+'</td><td>'+esc(r.provider||'—')+'</td><td><code>'+esc(r.order_id||'—')+'</code></td>'+
      '<td>'+esc(r.payment_status||'—')+'</td><td>'+(r.refund_count||0)+'</td></tr>';
  }).join('');
}
function exportReconcileCSV(){
  if(reconMode==='anchor'){
    var rows=[['Match','Refund ID','Order #','Refund Date','Amount','CSV Amount','Diff','Provider','Exec Status','Pay Status']];
    reconcileResults.forEach(function(r){rows.push([r.match_type,r.refund_id||r.reference||'',r.order_id||'',r.date||'',r.db_amount==null?'':r.db_amount,r.csv_amount==null?'':r.csv_amount,r.diff==null?'':r.diff,r.provider||'',r.exec_status||'',r.payment_status||'']);});
    var csv=rows.map(function(r){return r.map(function(c){return'"'+String(c==null?'':c).replace(/"/g,'""')+'"';}).join(',');}).join('\n');
    var a=document.createElement('a');a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);a.download='refunds-anchor-recon.csv';a.click();
    return;
  }
  var rows=[['Match','Reference','CSV Amount','DB Refunded','Diff','Date','Provider','Order #','Pay Status','Refund Rows']];
  reconcileResults.forEach(function(r){rows.push([r.match_type,r.reference,r.csv_amount,r.db_amount==null?'':r.db_amount,r.diff==null?'':r.diff,r.date||'',r.provider||'',r.order_id||'',r.payment_status||'',r.refund_count||0]);});
  var csv=rows.map(function(r){return r.map(function(c){return'"'+String(c==null?'':c).replace(/"/g,'""')+'"';}).join(',');}).join('\n');
  var a=document.createElement('a');a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);a.download='refunds-reconcile-report.csv';a.click();
}
setTimeout(function(){var today=getTodayDate();document.getElementById('dateFrom').value=today;document.getElementById('dateTo').value=today;loadData();},100);
</script>
</body>
</html>"""
