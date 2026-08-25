"""Reconciliation Portal API — importable module for the MallPlus Dashboard server"""
import json, csv, io
import psycopg2.extras
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════════════
# TRANSACTION FEE: 5% of payment amount (standard GCash rate)
# WITHHOLDING TAX: stored per-escrow in escrow_record.withholding_tax_rate
#                  (percentage, e.g. 0.50 = 0.5%). Default 0.5% if NULL.
# ═══════════════════════════════════════════════════════════════════════════
TXN_FEE_RATE     = 5.0   # percent
WHT_DEFAULT_RATE = 0.5   # percent

from recon_db import get_db

_BASE_SQL_TEMPLATE = """
SELECT
    COALESCE(oe.order_sn, o.id) AS order_id,
    o.created_at AT TIME ZONE 'Asia/Manila' AS order_date,
    o.status AS order_status,
    COALESCE(s.name, 'Unknown') AS merchant,
    COALESCE(oli.product_title, '—') AS product,
    COALESCE(c.email, '—') AS buyer_username,
    TRIM(COALESCE(c.first_name, '') || ' ' || COALESCE(c.last_name, '')) AS buyer_name,
    COALESCE((o.metadata->>'estimated_shipping_amount')::numeric, 0) AS estimated_shipping_fee,
    COALESCE(osm.amount, 0) AS actual_shipping_fee,
    COALESCE(oi_subtotal.line_items_sum, 0) + COALESCE(osm.amount, 0) AS total_price,
    COALESCE(pc.amount, COALESCE(oi_subtotal.line_items_sum, 0) + COALESCE(osm.amount, 0), 0) AS payment_amount,
    GREATEST(COALESCE(ref_sum.total_refunds, 0), COALESCE(er.refunded_amount, 0)) AS refund_amount,
    COALESCE(pc.amount, 0) - GREATEST(COALESCE(ref_sum.total_refunds, 0), COALESCE(er.refunded_amount, 0)) AS net_payment,
    CASE WHEN o.status = 'canceled' OR er.status = 'refunded' THEN 0 ELSE COALESCE((o.metadata->>'commission_fee')::numeric, 0) END AS commission_fee,
    CASE WHEN o.status = 'canceled' OR er.status = 'refunded' THEN 0 ELSE COALESCE((o.metadata->'service_fees'->>'total_fees')::numeric, 0) END AS service_fee,
    CASE WHEN o.status = 'canceled' OR er.status = 'refunded' THEN 0 ELSE ROUND((COALESCE(pc.amount, 0) * COALESCE(tfr.rate, 5.0) / 100)::numeric, 2) END AS transaction_fee,
    COALESCE(er.withholding_tax_rate, {wht_rate}) AS withholding_tax_rate,
    CASE WHEN o.status = 'canceled' OR er.status = 'refunded' THEN 0 ELSE GREATEST(COALESCE((o.metadata->>'withholding_tax')::numeric,
        ROUND((
            (COALESCE(oi_subtotal.line_items_sum, 0)
             - COALESCE((o.metadata->>'commission_fee')::numeric, 0)
             - COALESCE((o.metadata->'service_fees'->>'total_fees')::numeric, 0)
             - ROUND((COALESCE(pc.amount, 0) * COALESCE(tfr.rate, 5.0) / 100)::numeric, 2)
             - COALESCE(ref_sum.total_refunds, 0)
            ) * COALESCE(er.withholding_tax_rate, {wht_rate}) / 100
        )::numeric, 2)), 0) END AS withholding_tax,
    CASE WHEN o.status = 'canceled' OR er.status = 'refunded' THEN (COALESCE(oi_subtotal.line_items_sum, 0) - GREATEST(COALESCE(ref_sum.total_refunds, 0), COALESCE(er.refunded_amount, 0))) ELSE (COALESCE(oi_subtotal.line_items_sum, 0))
        - COALESCE((o.metadata->>'commission_fee')::numeric, 0)
        - COALESCE((o.metadata->'service_fees'->>'total_fees')::numeric, 0)
        - ROUND((COALESCE(pc.amount, 0) * COALESCE(tfr.rate, 5.0) / 100)::numeric, 2)
        - GREATEST(COALESCE((o.metadata->>'withholding_tax')::numeric,
            ROUND((
                (COALESCE(oi_subtotal.line_items_sum, 0)
                 - COALESCE((o.metadata->>'commission_fee')::numeric, 0)
                 - COALESCE((o.metadata->'service_fees'->>'total_fees')::numeric, 0)
                 - ROUND((COALESCE(pc.amount, 0) * COALESCE(tfr.rate, 5.0) / 100)::numeric, 2)
                 - COALESCE(ref_sum.total_refunds, 0)
                ) * COALESCE(er.withholding_tax_rate, {wht_rate}) / 100
            )::numeric, 2)), 0)
        - GREATEST(COALESCE(ref_sum.total_refunds, 0), COALESCE(er.refunded_amount, 0)) END AS net_escrow,
    COALESCE(pc.status, 'N/A') AS payment_status,
    COALESCE(er.status, '—') AS escrow_status,
    COALESCE(er.amount, 0) AS escrow_amount,
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
    COALESCE(pmt.data->>'payment_reference', '—') AS xendit_reference,
    CASE
        WHEN ful.canceled_at IS NOT NULL THEN 'canceled'
        WHEN ful.delivered_at IS NOT NULL THEN 'delivered'
        WHEN ful.shipped_at IS NOT NULL THEN 'shipped'
        WHEN ful.packed_at IS NOT NULL THEN 'packed'
        ELSE 'pending'
    END AS logistics_status
FROM public.order o
LEFT JOIN public.order_extension oe ON oe.order_id = o.id
LEFT JOIN public.seller s ON s.id = (o.metadata->>'seller_id')
LEFT JOIN public.customer c ON c.id = o.customer_id AND c.deleted_at IS NULL
LEFT JOIN LATERAL (
    SELECT oi2.item_id
    FROM public.order_item oi2
    WHERE oi2.order_id = o.id AND oi2.deleted_at IS NULL
    LIMIT 1
) oi ON true
LEFT JOIN public.order_line_item oli ON oli.id = oi.item_id AND oli.deleted_at IS NULL
LEFT JOIN LATERAL (
    SELECT COALESCE(SUM(oi_latest.quantity * COALESCE(oi_latest.unit_price, oli_sum.unit_price, 0)), 0) AS line_items_sum
    FROM (
        SELECT DISTINCT ON (oi_sum.item_id) oi_sum.quantity, oi_sum.unit_price, oi_sum.item_id
        FROM public.order_item oi_sum
        WHERE oi_sum.order_id = o.id AND oi_sum.deleted_at IS NULL
        ORDER BY oi_sum.item_id, oi_sum.version DESC
    ) oi_latest
    LEFT JOIN public.order_line_item oli_sum ON oli_sum.id = oi_latest.item_id AND oli_sum.deleted_at IS NULL
) oi_subtotal ON true
LEFT JOIN LATERAL (
    SELECT osm2.amount
    FROM public.order_shipping os2
    JOIN public.order_shipping_method osm2 ON osm2.id = os2.shipping_method_id AND osm2.deleted_at IS NULL
    WHERE os2.order_id = o.id AND os2.deleted_at IS NULL
    LIMIT 1
) osm ON true
LEFT JOIN LATERAL (
    SELECT pc2.status, pc2.amount, pc2.id AS collection_id
    FROM public.order_payment_collection opc2
    JOIN public.payment_collection pc2 ON pc2.id = opc2.payment_collection_id AND pc2.deleted_at IS NULL
    WHERE opc2.order_id = o.id AND opc2.deleted_at IS NULL
    LIMIT 1
) pc ON true
LEFT JOIN LATERAL (
    SELECT COALESCE(SUM(ref.amount), 0) AS total_refunds
    FROM refund ref
    JOIN payment p ON p.id = ref.payment_id
    WHERE p.payment_session_id IN (
        SELECT ps.id FROM payment_session ps
        WHERE ps.payment_collection_id = pc.collection_id AND ps.deleted_at IS NULL
    ) AND ref.deleted_at IS NULL
) ref_sum ON true
LEFT JOIN LATERAL (
    SELECT ps2.provider_id, ps2.id AS session_id
    FROM public.payment_session ps2
    WHERE ps2.payment_collection_id = pc.collection_id AND ps2.deleted_at IS NULL
    ORDER BY ps2.created_at DESC
    LIMIT 1
) ps ON true
LEFT JOIN LATERAL (
    SELECT gcl2.data
    FROM public.payment_gcash_logs gcl2
    WHERE gcl2.payment_session_id = ps.session_id AND gcl2.deleted_at IS NULL
    LIMIT 1
) gcl ON true
LEFT JOIN LATERAL (
    SELECT p2.data
    FROM public.payment p2
    WHERE p2.payment_session_id = ps.session_id AND p2.deleted_at IS NULL
    LIMIT 1
) pmt ON true
LEFT JOIN LATERAL (
    SELECT COALESCE(
        (SELECT tf.rate_percentage
         FROM public.transaction_fee tf
         JOIN public.payment_channel pch ON pch.id = tf.payment_channel AND pch.deleted_at IS NULL
         WHERE tf.deleted_at IS NULL AND tf.status = 1
           AND tf.created_at <= o.created_at
           AND pch.provider_code = ps.provider_id
           AND tf.start_date <= o.created_at::date::text
           AND tf.end_date >= o.created_at::date::text
         ORDER BY tf.created_at DESC
         LIMIT 1),
        (SELECT tf.rate_percentage
         FROM public.transaction_fee tf
         WHERE tf.deleted_at IS NULL AND tf.status = 1
           AND tf.created_at <= o.created_at
           AND tf.is_global = 1 AND tf.payment_channel IS NULL
           AND tf.start_date <= o.created_at::date::text
           AND tf.end_date >= o.created_at::date::text
         ORDER BY tf.created_at DESC
         LIMIT 1),
        -- Tier 3: channel-specific, existed at order time (now inactive)
        (SELECT tf.rate_percentage
         FROM public.transaction_fee tf
         JOIN public.payment_channel pch ON pch.id = tf.payment_channel AND pch.deleted_at IS NULL
         WHERE tf.deleted_at IS NULL
           AND tf.created_at <= o.created_at
           AND pch.provider_code = ps.provider_id
           AND tf.start_date <= o.created_at::date::text
           AND tf.end_date >= o.created_at::date::text
         ORDER BY tf.created_at DESC
         LIMIT 1),
        -- Tier 4: global, existed at order time (now inactive)
        (SELECT tf.rate_percentage
         FROM public.transaction_fee tf
         WHERE tf.deleted_at IS NULL
           AND tf.created_at <= o.created_at
           AND tf.is_global = 1 AND tf.payment_channel IS NULL
           AND tf.start_date <= o.created_at::date::text
           AND tf.end_date >= o.created_at::date::text
         ORDER BY tf.created_at DESC
         LIMIT 1),
        5.0
    ) AS rate
) tfr ON true
LEFT JOIN LATERAL (
    SELECT er2.status, er2.amount, er2.withholding_tax_rate, er2.refunded_amount
    FROM public.escrow_record er2
    WHERE er2.order_id = o.id AND er2.deleted_at IS NULL
    LIMIT 1
) er ON true
LEFT JOIN LATERAL (
    SELECT ful2.packed_at, ful2.shipped_at, ful2.delivered_at, ful2.canceled_at
    FROM public.order_fulfillment oful2
    JOIN public.fulfillment ful2 ON ful2.id = oful2.fulfillment_id AND ful2.deleted_at IS NULL
    WHERE oful2.order_id = o.id
    LIMIT 1
) ful ON true
WHERE o.deleted_at IS NULL
"""

_STATS_SQL_TEMPLATE = """
SELECT
    COUNT(*) AS total_orders,
    COALESCE(SUM(COALESCE(oi_subtotal.line_items_sum, 0) + COALESCE(osm.amount, 0)), 0) AS total_revenue,
    COALESCE(SUM(GREATEST(COALESCE(ref_sum.total_refunds, 0), COALESCE(er.refunded_amount, 0))), 0) AS total_refunds,
    COALESCE(SUM(er.amount), 0) AS total_escrow,
    COALESCE(SUM(CASE WHEN o.status = 'canceled' OR er.status = 'refunded' THEN 0 ELSE (o.metadata->>'commission_fee')::numeric END), 0) AS total_commission,
    COALESCE(SUM(CASE WHEN o.status = 'canceled' OR er.status = 'refunded' THEN 0 ELSE (o.metadata->'service_fees'->>'total_fees')::numeric END), 0) AS total_service_fee,
    COALESCE(SUM(CASE WHEN o.status = 'canceled' OR er.status = 'refunded' THEN 0 ELSE ROUND((COALESCE(pc.amount, 0) * COALESCE(tfr.rate, 5.0) / 100)::numeric, 2) END), 0) AS total_transaction_fee,
    COALESCE(SUM(CASE WHEN o.status = 'canceled' OR er.status = 'refunded' THEN 0 ELSE GREATEST(COALESCE((o.metadata->>'withholding_tax')::numeric,
        ROUND((
            (COALESCE(oi_subtotal.line_items_sum, 0)
             - COALESCE((o.metadata->>'commission_fee')::numeric, 0)
             - COALESCE((o.metadata->'service_fees'->>'total_fees')::numeric, 0)
             - ROUND((COALESCE(pc.amount, 0) * COALESCE(tfr.rate, 5.0) / 100)::numeric, 2)
             - COALESCE(ref_sum.total_refunds, 0)
            ) * COALESCE(er.withholding_tax_rate, {wht_rate}) / 100
        )::numeric, 2)), 0) END), 0) AS total_withholding_tax,
    COALESCE(SUM(
        CASE WHEN o.status = 'canceled' OR er.status = 'refunded' THEN (COALESCE(oi_subtotal.line_items_sum, 0) - GREATEST(COALESCE(ref_sum.total_refunds, 0), COALESCE(er.refunded_amount, 0))) ELSE (COALESCE(oi_subtotal.line_items_sum, 0))
        - COALESCE((o.metadata->>'commission_fee')::numeric, 0)
        - COALESCE((o.metadata->'service_fees'->>'total_fees')::numeric, 0)
        - ROUND((COALESCE(pc.amount, 0) * COALESCE(tfr.rate, 5.0) / 100)::numeric, 2)
        - GREATEST(COALESCE((o.metadata->>'withholding_tax')::numeric,
            ROUND((
                (COALESCE(oi_subtotal.line_items_sum, 0)
                 - COALESCE((o.metadata->>'commission_fee')::numeric, 0)
                 - COALESCE((o.metadata->'service_fees'->>'total_fees')::numeric, 0)
                 - ROUND((COALESCE(pc.amount, 0) * COALESCE(tfr.rate, 5.0) / 100)::numeric, 2)
                 - COALESCE(ref_sum.total_refunds, 0)
                ) * COALESCE(er.withholding_tax_rate, {wht_rate}) / 100
            )::numeric, 2)), 0)
        - GREATEST(COALESCE(ref_sum.total_refunds, 0), COALESCE(er.refunded_amount, 0)) END
    ), 0) AS net_escrow
FROM public.order o
LEFT JOIN public.order_extension oe ON oe.order_id = o.id
LEFT JOIN public.seller s ON s.id = (o.metadata->>'seller_id')
LEFT JOIN public.customer c ON c.id = o.customer_id AND c.deleted_at IS NULL
LEFT JOIN LATERAL (
    SELECT oi2.item_id
    FROM public.order_item oi2
    WHERE oi2.order_id = o.id AND oi2.deleted_at IS NULL
    LIMIT 1
) oi ON true
LEFT JOIN public.order_line_item oli2 ON oli2.id = oi.item_id AND oli2.deleted_at IS NULL
LEFT JOIN LATERAL (
    SELECT COALESCE(SUM(oi_latest.quantity * COALESCE(oi_latest.unit_price, oli_sum.unit_price, 0)), 0) AS line_items_sum
    FROM (
        SELECT DISTINCT ON (oi_sum.item_id) oi_sum.quantity, oi_sum.unit_price, oi_sum.item_id
        FROM public.order_item oi_sum
        WHERE oi_sum.order_id = o.id AND oi_sum.deleted_at IS NULL
        ORDER BY oi_sum.item_id, oi_sum.version DESC
    ) oi_latest
    LEFT JOIN public.order_line_item oli_sum ON oli_sum.id = oi_latest.item_id AND oli_sum.deleted_at IS NULL
) oi_subtotal ON true
LEFT JOIN LATERAL (
    SELECT osm2.amount
    FROM public.order_shipping os2
    JOIN public.order_shipping_method osm2 ON osm2.id = os2.shipping_method_id AND osm2.deleted_at IS NULL
    WHERE os2.order_id = o.id AND os2.deleted_at IS NULL
    LIMIT 1
) osm ON true
LEFT JOIN LATERAL (
    SELECT pc2.amount, pc2.status, pc2.id AS collection_id
    FROM public.order_payment_collection opc2
    JOIN public.payment_collection pc2 ON pc2.id = opc2.payment_collection_id AND pc2.deleted_at IS NULL
    WHERE opc2.order_id = o.id AND opc2.deleted_at IS NULL
    LIMIT 1
) pc ON true
LEFT JOIN LATERAL (
    SELECT COALESCE(SUM(ref.amount), 0) AS total_refunds
    FROM refund ref
    JOIN payment p ON p.id = ref.payment_id
    WHERE p.payment_session_id IN (
        SELECT ps.id FROM payment_session ps
        WHERE ps.payment_collection_id = pc.collection_id AND ps.deleted_at IS NULL
    ) AND ref.deleted_at IS NULL
) ref_sum ON true
LEFT JOIN LATERAL (
    SELECT ps2.provider_id
    FROM public.payment_session ps2
    WHERE ps2.payment_collection_id = pc.collection_id AND ps2.deleted_at IS NULL
    ORDER BY ps2.created_at DESC
    LIMIT 1
) ps ON true
LEFT JOIN LATERAL (
    SELECT COALESCE(
        (SELECT tf.rate_percentage
         FROM public.transaction_fee tf
         JOIN public.payment_channel pch ON pch.id = tf.payment_channel AND pch.deleted_at IS NULL
         WHERE tf.deleted_at IS NULL AND tf.status = 1
           AND tf.created_at <= o.created_at
           AND pch.provider_code = ps.provider_id
           AND tf.start_date <= o.created_at::date::text
           AND tf.end_date >= o.created_at::date::text
         ORDER BY tf.created_at DESC
         LIMIT 1),
        (SELECT tf.rate_percentage
         FROM public.transaction_fee tf
         WHERE tf.deleted_at IS NULL AND tf.status = 1
           AND tf.created_at <= o.created_at
           AND tf.is_global = 1 AND tf.payment_channel IS NULL
           AND tf.start_date <= o.created_at::date::text
           AND tf.end_date >= o.created_at::date::text
         ORDER BY tf.created_at DESC
         LIMIT 1),
        -- Tier 3: channel-specific, existed at order time (now inactive)
        (SELECT tf.rate_percentage
         FROM public.transaction_fee tf
         JOIN public.payment_channel pch ON pch.id = tf.payment_channel AND pch.deleted_at IS NULL
         WHERE tf.deleted_at IS NULL
           AND tf.created_at <= o.created_at
           AND pch.provider_code = ps.provider_id
           AND tf.start_date <= o.created_at::date::text
           AND tf.end_date >= o.created_at::date::text
         ORDER BY tf.created_at DESC
         LIMIT 1),
        -- Tier 4: global, existed at order time (now inactive)
        (SELECT tf.rate_percentage
         FROM public.transaction_fee tf
         WHERE tf.deleted_at IS NULL
           AND tf.created_at <= o.created_at
           AND tf.is_global = 1 AND tf.payment_channel IS NULL
           AND tf.start_date <= o.created_at::date::text
           AND tf.end_date >= o.created_at::date::text
         ORDER BY tf.created_at DESC
         LIMIT 1),
        5.0
    ) AS rate
) tfr ON true
LEFT JOIN LATERAL (
    SELECT er2.amount, er2.withholding_tax_rate, er2.status, er2.refunded_amount
    FROM public.escrow_record er2
    WHERE er2.order_id = o.id AND er2.deleted_at IS NULL
    LIMIT 1
) er ON true
WHERE o.deleted_at IS NULL
"""

# Format templates with constant rate values (safe: hardcoded, not user input)
BASE_SELECT  = _BASE_SQL_TEMPLATE.format(wht_rate=WHT_DEFAULT_RATE)
STATS_SELECT = _STATS_SQL_TEMPLATE.format(wht_rate=WHT_DEFAULT_RATE)

def handle_recon_api(path, query_params):
    """Handle reconciliation portal API requests. Returns (status, content_type, body_bytes)"""
    try:
        date_from = query_params.get("date_from", [""])[0]
        date_to = query_params.get("date_to", [""])[0]
        order_status = query_params.get("order_status", [""])[0]
        payment_status = query_params.get("payment_status", [""])[0]
        escrow_status = query_params.get("escrow_status", [""])[0]
        search = query_params.get("search", [""])[0].strip()
        page = int(query_params.get("page", ["1"])[0])
        page_size = int(query_params.get("page_size", ["50"])[0])
        export_csv = query_params.get("export", [""])[0] == "csv"

        conditions = []
        params = []

        if date_from:
            conditions.append("o.created_at >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("o.created_at < %s::date + interval '1 day'")
            params.append(date_to)
        if order_status:
            conditions.append("o.status = %s")
            params.append(order_status)
        if payment_status:
            conditions.append("pc.status = %s")
            params.append(payment_status)
        if escrow_status:
            conditions.append("er.status = %s")
            params.append(escrow_status)
        logistics_status = query_params.get("logistics_status", [""])[0]
        if logistics_status:
            conditions.append("CASE WHEN ful.canceled_at IS NOT NULL THEN 'canceled' WHEN ful.delivered_at IS NOT NULL THEN 'delivered' WHEN ful.shipped_at IS NOT NULL THEN 'shipped' WHEN ful.packed_at IS NOT NULL THEN 'packed' ELSE 'pending' END = %s")
            params.append(logistics_status)
        payment_provider = query_params.get("payment_provider", [""])[0]
        if payment_provider:
            if payment_provider == "GCash":
                conditions.append("ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife')")
            elif payment_provider == "Xendit":
                conditions.append("ps.provider_id = 'pp_xendit'")
            elif payment_provider == "Stripe":
                conditions.append("ps.provider_id = 'pp_card_stripe-connect'")
            elif payment_provider == "System":
                conditions.append("ps.provider_id = 'pp_system_default'")
        payment_method = query_params.get("payment_method", [""])[0]
        if payment_method:
            if payment_method in ("Wallet", "GCredit", "GGives"):
                conditions.append("gcl.data->'response'->'paymentViews'->0->'payOptionInfos'->0->>'payMethod' = %s")
                params.append(payment_method.upper() if payment_method == "Wallet" else payment_method)
            elif payment_method in ("GCash (Xendit)", "Maya", "Credit Card"):
                xm = {"GCash (Xendit)": "GCASH", "Maya": "MAYA", "Credit Card": "CARD"}
                conditions.append("pmt.data->>'method' = %s")
                params.append(xm[payment_method])
            elif payment_method == "Card (Stripe)":
                conditions.append("ps.provider_id = 'pp_card_stripe-connect'")
        if search:
            conditions.append("(oe.order_sn ILIKE %s OR o.id ILIKE %s OR s.name ILIKE %s OR c.email ILIKE %s OR c.first_name ILIKE %s OR c.last_name ILIKE %s)")
            params.extend([f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%"])

        extra_where = " AND ".join(conditions) if conditions else "true"

        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if export_csv:
            csv_select = BASE_SELECT.replace("COALESCE(oe.order_sn, o.id) AS order_id", "COALESCE(oe.order_sn, o.id) AS \"Order #\"")
            data_sql = f"{csv_select} AND {extra_where} ORDER BY o.created_at DESC LIMIT 5000"
            cur.execute(data_sql, params)
            rows = cur.fetchall()

            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([d[0] for d in cur.description])
            for r in rows:
                writer.writerow([str(v) if v is not None else '' for v in r.values()])
            return 200, "text/csv", output.getvalue().encode(), True

        # Count
        count_sql = f"SELECT COUNT(*) AS total FROM ({BASE_SELECT} AND {extra_where}) sub"
        cur.execute(count_sql, params)
        total = cur.fetchone()["total"]

        # Data
        data_sql = f"{BASE_SELECT} AND {extra_where} ORDER BY o.created_at DESC LIMIT %s OFFSET %s"
        offset = (page - 1) * page_size
        cur.execute(data_sql, params + [page_size, offset])
        rows = _serialize(cur.fetchall())

        # Stats
        stats_sql = f"{STATS_SELECT} AND {extra_where}"
        cur.execute(stats_sql, params)
        stats = _serialize([cur.fetchone()])[0]

        body = json.dumps({"rows": rows, "total": total, "page": page, "page_size": page_size, "stats": stats})
        return 200, "application/json", body.encode(), True

    except Exception as e:
        return 500, "application/json", json.dumps({"error": str(e)}).encode(), True

def handle_order_reconcile_api(body_json):
    """Handle order reconciliation: match uploaded CSV references against DB.
    Returns (status, content_type, body_bytes, allow_cors)"""
    try:
        references = body_json.get("references", [])
        if not references or not isinstance(references, list):
            return 400, "application/json", json.dumps({"error": "references array required"}).encode(), True
        if len(references) > 20000:
            return 400, "application/json", json.dumps({"error": "max 20,000 references"}).encode(), True

        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Batch query: match payment_session.id against CSV References
        cur.execute("""
            SELECT
                ps.id AS session_id,
                ps.status AS payment_status,
                ps.provider_id,
                COALESCE(pc.amount, 0) AS payment_amount,
                COALESCE(pc.id,'') AS collection_id,
                COALESCE(oe.order_sn, o.id) AS order_id,
                s.name AS merchant,
                p.data->>'payment_reference' AS xendit_reference,
                p.data->>'method' AS xendit_method,
                gcl.data->'response'->'paymentViews'->0->'payOptionInfos'->0->>'payMethod' AS gcash_method,
                COALESCE(ref_sum.total_refunds, 0) AS refund_amount,
                er_data.escrow_status,
                COALESCE(er_data.escrow_refunded, 0) AS escrow_refunded
            FROM payment_session ps
            LEFT JOIN payment_collection pc ON pc.id = ps.payment_collection_id
            LEFT JOIN order_payment_collection opc ON opc.payment_collection_id = pc.id
            LEFT JOIN "order" o ON o.id = opc.order_id
            LEFT JOIN order_extension oe ON oe.order_id = o.id
            LEFT JOIN seller s ON s.id = (o.metadata->>'seller_id')
            LEFT JOIN payment p ON p.payment_session_id = ps.id
            LEFT JOIN payment_gcash_logs gcl ON gcl.payment_session_id = ps.id
            LEFT JOIN (
                SELECT payment_id, SUM(amount) AS total_refunds
                FROM refund
                WHERE deleted_at IS NULL
                GROUP BY payment_id
            ) ref_sum ON ref_sum.payment_id = ps.id
            LEFT JOIN (
                SELECT DISTINCT ON (er.order_id)
                    er.order_id,
                    er.status AS escrow_status,
                    COALESCE(er.refunded_amount, 0) AS escrow_refunded
                FROM escrow_record er
                WHERE er.deleted_at IS NULL
                ORDER BY er.order_id, er.created_at DESC
            ) er_data ON er_data.order_id = o.id
            WHERE ps.id = ANY(%s)
        """, (references,))

        db_rows = cur.fetchall()
        db_map = {}
        for r in db_rows:
            db_map[r['session_id']] = r

        # Also resolve references that have suffixes (e.g. "payses_XYZ_abc-def")
        # Try root matching for refs not found
        remaining_refs = [ref for ref in references if ref not in db_map]
        if remaining_refs:
            # Strip suffixes: everything after the second underscore might be a suffix
            suffix_map = {}
            for ref in remaining_refs:
                # Split on underscore, take first 2 parts as root
                parts = ref.rsplit('_', 2)
                if len(parts) >= 2 and parts[0].startswith('payses_'):
                    root = parts[0] + '_' + parts[1]
                    suffix_map[ref] = root
            if suffix_map:
                roots = list(set(suffix_map.values()))
                cur.execute("""
                    SELECT
                        ps.id AS session_id,
                        ps.status AS payment_status,
                        ps.provider_id,
                        COALESCE(pc.amount, 0) AS payment_amount,
                        COALESCE(pc.id,'') AS collection_id,
                        COALESCE(oe.order_sn, o.id) AS order_id,
                        s.name AS merchant,
                        p.data->>'payment_reference' AS xendit_reference,
                        COALESCE(ref_sum.total_refunds, 0) AS refund_amount,
                        er_data.escrow_status,
                        COALESCE(er_data.escrow_refunded, 0) AS escrow_refunded
                    FROM payment_session ps
                    LEFT JOIN payment_collection pc ON pc.id = ps.payment_collection_id
                    LEFT JOIN order_payment_collection opc ON opc.payment_collection_id = pc.id
                    LEFT JOIN "order" o ON o.id = opc.order_id
                    LEFT JOIN order_extension oe ON oe.order_id = o.id
                    LEFT JOIN seller s ON s.id = (o.metadata->>'seller_id')
                    LEFT JOIN payment p ON p.payment_session_id = ps.id
                    LEFT JOIN (
                        SELECT payment_id, SUM(amount) AS total_refunds
                        FROM refund WHERE deleted_at IS NULL GROUP BY payment_id
                    ) ref_sum ON ref_sum.payment_id = ps.id
                    LEFT JOIN (
                        SELECT DISTINCT ON (er.order_id)
                            er.order_id,
                            er.status AS escrow_status,
                            COALESCE(er.refunded_amount, 0) AS escrow_refunded
                        FROM escrow_record er
                        WHERE er.deleted_at IS NULL
                        ORDER BY er.order_id, er.created_at DESC
                    ) er_data ON er_data.order_id = o.id
                    WHERE ps.id = ANY(%s)
                """, (roots,))
                for r in cur.fetchall():
                    if r['session_id'] not in db_map:
                        db_map[r['session_id']] = r
                # Map suffix refs to root results
                for ref in remaining_refs:
                    if ref in suffix_map and suffix_map[ref] in db_map and ref not in db_map:
                        db_map[ref] = dict(db_map[suffix_map[ref]])
                        db_map[ref]['_suffix_match'] = True

        cur.close()
        conn.close()

        return 200, "application/json", json.dumps({"db_map": {k: _serialize_one(v) for k, v in db_map.items()}}).encode(), True

    except Exception as e:
        return 500, "application/json", json.dumps({"error": str(e)}).encode(), True


_ORDER_ANCHOR_STATUSES = ('COMPLETED', 'CANCELED', 'AUTHORIZED', 'NOT_PAID')


def _normalize_order_statuses(raw):
    """Normalize executionStatus (str | list). '' / 'ALL' -> all; 'COMPLETED_OR_CANCELED'
    (legacy) -> ['COMPLETED', 'CANCELED']; empty/None -> default ['COMPLETED', 'CANCELED']."""
    if isinstance(raw, str):
        raw = raw.strip()
        if raw in ('', 'ALL'):
            statuses = list(_ORDER_ANCHOR_STATUSES)
        elif raw == 'COMPLETED_OR_CANCELED':
            statuses = ['COMPLETED', 'CANCELED']
        else:
            statuses = [raw]
    elif isinstance(raw, (list, tuple)):
        statuses = [str(s).strip() for s in raw if str(s).strip()]
    else:
        statuses = []
    if not statuses:
        statuses = ['COMPLETED', 'CANCELED']
    for s in statuses:
        if s not in _ORDER_ANCHOR_STATUSES:
            return None
    return statuses


def handle_order_reconcile_anchor_api(body_json):
    """Ledger-anchored order recon: anchor = payment sessions of orders in a date range
    (+ status), pulled from OUR DB. Optional CSV rows (reference [+ amount]) are evidence:
    verdicts matched / refunded / amount_mismatch / missing_from_csv; CSV refs with no
    ledger key -> not_in_ledger extras. Amount compare is vs DB net amount
    (gross - refunds), same semantics as the CSV-based flow."""
    try:
        date_from = str(body_json.get('dateFrom', '') or '').strip()
        date_to = str(body_json.get('dateTo', '') or '').strip()
        statuses = _normalize_order_statuses(body_json.get('executionStatus', 'COMPLETED_OR_CANCELED'))
        rows = body_json.get('rows') or []

        if not date_from or not date_to:
            return 400, "application/json", json.dumps({"error": "dateFrom and dateTo required"}).encode(), True
        try:
            datetime.strptime(date_from, '%Y-%m-%d')
            datetime.strptime(date_to, '%Y-%m-%d')
        except ValueError:
            return 400, "application/json", json.dumps({"error": "dates must be YYYY-MM-DD"}).encode(), True
        if statuses is None:
            return 400, "application/json", json.dumps({"error": "invalid executionStatus"}).encode(), True

        clause_specs = []
        if 'COMPLETED' in statuses:
            clause_specs.append("pc.status = 'completed'")
        if 'CANCELED' in statuses:
            clause_specs.append("o.status = 'canceled'")
        if 'AUTHORIZED' in statuses:
            clause_specs.append("pc.status = 'authorized'")
        if 'NOT_PAID' in statuses:
            clause_specs.append("pc.status = 'not_paid'")
        status_clause = ""
        if clause_specs and len(statuses) < len(_ORDER_ANCHOR_STATUSES):
            status_clause = "AND (" + " OR ".join(clause_specs) + ")"

        sql = """
            SELECT
                ps.id AS session_id,
                COALESCE(oe.order_sn, o.id) AS order_id,
                (o.created_at AT TIME ZONE 'Asia/Manila')::timestamp AS order_date,
                COALESCE(s.name, 'Unknown') AS merchant,
                COALESCE(pc.amount, 0) AS payment_amount,
                COALESCE(pc.status, 'N/A') AS payment_status,
                o.status AS order_status,
                CASE WHEN ps.provider_id IN ('pp_gcash_webpay', 'pp_gcashmp_glife') THEN 'GCash'
                     WHEN ps.provider_id = 'pp_xendit' THEN 'Xendit'
                     WHEN ps.provider_id = 'pp_card_stripe-connect' THEN 'Stripe'
                     WHEN ps.provider_id = 'pp_system_default' THEN 'System'
                     ELSE COALESCE(ps.provider_id, 'Unknown') END AS provider,
                COALESCE(p.data->>'payment_reference', '') AS payment_reference,
                COALESCE(ref_sum.total_refunds, 0) AS refund_amount,
                COALESCE(er_data.escrow_refunded, 0) AS escrow_refunded,
                COALESCE(er_data.escrow_status, '') AS escrow_status
            FROM payment_session ps
            JOIN order_payment_collection opc ON opc.payment_collection_id = ps.payment_collection_id
            JOIN public."order" o ON o.id = opc.order_id AND o.deleted_at IS NULL
            LEFT JOIN payment_collection pc ON pc.id = ps.payment_collection_id AND pc.deleted_at IS NULL
            LEFT JOIN order_extension oe ON oe.order_id = o.id
            LEFT JOIN seller s ON s.id = (o.metadata->>'seller_id')
            LEFT JOIN payment p ON p.payment_session_id = ps.id AND p.deleted_at IS NULL
            LEFT JOIN (
                SELECT payment_id, SUM(amount) AS total_refunds
                FROM public.refund WHERE deleted_at IS NULL GROUP BY payment_id
            ) ref_sum ON ref_sum.payment_id = ps.id
            LEFT JOIN (
                SELECT DISTINCT ON (er.order_id) er.order_id,
                    er.status AS escrow_status, COALESCE(er.refunded_amount, 0) AS escrow_refunded
                FROM public.escrow_record er
                WHERE er.deleted_at IS NULL ORDER BY er.order_id, er.created_at DESC
            ) er_data ON er_data.order_id = o.id
            WHERE (o.created_at AT TIME ZONE 'Asia/Manila')::date BETWEEN %s AND %s
              {status_clause}
            ORDER BY order_date
        """.format(status_clause=status_clause)

        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, (date_from, date_to))
        db_rows = cur.fetchall()
        cur.close()
        conn.close()

        # CSV evidence index
        csv_by_ref = {}
        has_amounts = False
        for r in rows:
            ref = str(r.get('reference', '') or '').strip()
            if not ref:
                continue
            amt = r.get('amount')
            if amt is not None and amt != '' and float(amt or 0) != 0:
                has_amounts = True
            entry = csv_by_ref.setdefault(ref, {'total': 0.0, 'n': 0})
            try:
                entry['total'] += float(amt or 0)
            except (TypeError, ValueError):
                pass
            entry['n'] += 1

        all_db_keys = set()
        for row in db_rows:
            for k in (row['session_id'], row['payment_reference'], row['order_id']):
                if k:
                    all_db_keys.add(k)

        out_rows = []
        matched = refunded = missing = mismatch = 0
        matched_amt = refunded_amt = missing_amt = mismatch_amt = 0.0
        for row in db_rows:
            gross = float(row['payment_amount'] or 0)
            db_refund = float(row['refund_amount'] or 0)
            escrow_ref = float(row['escrow_refunded'] or 0)
            total_refund = max(db_refund, escrow_ref)
            net = round(gross - total_refund, 2)
            d = {
                'session_id': row['session_id'],
                'order_id': row['order_id'],
                'order_date': row['order_date'].strftime('%Y-%m-%d %H:%M:%S') if row['order_date'] else '',
                'merchant': row['merchant'],
                'payment_amount': gross,
                'net_amount': net,
                'refund_amount': db_refund,
                'escrow_refunded': escrow_ref,
                'escrow_status': row['escrow_status'],
                'payment_status': row['payment_status'],
                'order_status': row['order_status'],
                'provider': row['provider'],
            }
            csv_hit = None
            for k in (row['session_id'], row['payment_reference'], row['order_id']):
                if k and k in csv_by_ref:
                    csv_hit = csv_by_ref[k]
                    break
            if csv_hit is None:
                d['verdict'] = 'missing'
                d['csv_amount'] = None
                d['diff'] = None
                missing += 1
                missing_amt += net
            else:
                csv_total = round(csv_hit['total'], 2)
                d['csv_amount'] = csv_total
                if not has_amounts:
                    d['verdict'] = 'matched'
                    d['diff'] = None
                    matched += 1
                    matched_amt += net
                elif escrow_ref > 0 and net < 0.01:
                    d['verdict'] = 'refunded'
                    d['diff'] = None
                    refunded += 1
                    refunded_amt += net
                else:
                    diff = round(csv_total - net, 2)
                    d['diff'] = diff
                    if abs(diff) < 0.01:
                        d['verdict'] = 'matched'
                        matched += 1
                        matched_amt += net
                    else:
                        d['verdict'] = 'amount_mismatch'
                        mismatch += 1
                        mismatch_amt += net
            out_rows.append(d)

        extras = []
        for ref, info in csv_by_ref.items():
            if ref not in all_db_keys:
                extras.append({'reference': ref, 'csv_amount': round(info['total'], 2), 'csv_count': info['n']})

        anchor_total = len(out_rows)
        stats = {
            'anchor_total': anchor_total,
            'anchor_amount': round(sum(r['payment_amount'] for r in out_rows), 2),
            'matched': matched,
            'matched_amount': round(matched_amt, 2),
            'refunded': refunded,
            'refunded_amount': round(refunded_amt, 2),
            'missing': missing,
            'missing_amount': round(missing_amt, 2),
            'mismatch': mismatch,
            'mismatch_amount': round(mismatch_amt, 2),
            'extras': len(extras),
            'extras_amount': round(sum(e['csv_amount'] for e in extras), 2),
            'completeness_pct': round((matched + refunded) / anchor_total * 100, 2) if anchor_total else 100.0,
            'csv_evidence': bool(rows),
        }
        return 200, "application/json", json.dumps({"stats": stats, "rows": out_rows, "extras": extras}).encode(), True
    except Exception as e:
        import traceback; traceback.print_exc()
        return 500, "application/json", json.dumps({"error": str(e)}).encode(), True


def _serialize_one(r):
    """Serialize a single dict row"""
    if r is None:
        return None
    result = {}
    for k, v in r.items():
        if v is None:
            result[k] = None
        elif type(v).__name__ == 'Decimal':
            result[k] = float(v)
        elif hasattr(v, 'isoformat'):
            result[k] = v.strftime("%Y-%m-%d %H:%M")
        else:
            result[k] = v
    return result


def serve_recon_portal():
    """Return the reconciliation portal HTML"""
    import os
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recon_portal.html")
    if os.path.exists(html_path):
        with open(html_path, "rb") as f:
            return f.read()
    return _RECON_HTML.encode()

_RECON_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Order Reconciliation — MallPlus</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=Quicksand:wght@500;600;700&display=swap" rel="stylesheet"/>
<link href="https://fonts.cdnfonts.com/css/garet" rel="stylesheet"/>
<style>
  :root { --bg: #E0F7F5; --card: #FFFFFF; --border: rgba(0,175,160,.25); --text: #1A1035; --dim: #6B7280; --accent: #00AFA0; --green: #00AFA0; --red: #EF4444; --amber: #C4880A; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family: 'Space Grotesk',system-ui,sans-serif; background: linear-gradient(135deg,#3724ED 0%,#1A9FD8 45%,#00AFA0 100%); background-attachment: fixed; color: var(--text); font-size: 13px; min-height: 100vh; }
  header { background: rgba(255,255,255,.9); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px); border-bottom: 1px solid var(--border); padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
  header h1 { font-family: 'Garet','Space Grotesk',sans-serif; font-size: 18px; font-weight: 600; }
  header .nav { display: flex; gap: 8px; }
  header .badge { font-size: 11px; color: var(--accent); }
  .container { max-width: 1900px; margin: 0 auto; padding: 16px 24px; }
  .filters { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 16px; display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; }
  .filter-group { display: flex; flex-direction: column; gap: 4px; }
  .filter-group label { font-size: 11px; color: var(--dim); text-transform: uppercase; letter-spacing: .5px; }
  .filter-group input, .filter-group select { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 8px 12px; border-radius: 6px; font-size: 13px; min-width: 150px; }
  .filter-group input:focus, .filter-group select:focus { outline: none; border-color: var(--accent); }
  .btn { padding: 8px 20px; border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer; border: none; transition: all .15s; }
  .btn-primary { background: var(--accent); color: #fff; border-radius: 999px; } .btn-primary:hover { background: #007A73; }
  .btn-secondary { background: rgba(0,175,160,.08); color: var(--text); } .btn-secondary:hover { background: #E0F5F3; }
  .btn-sm { padding: 4px 10px; font-size: 11px; }
  .stats { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 12px 18px; min-width: 140px; }
  .stat-card .value { font-size: 22px; font-weight: 700; } .stat-card .value.red { color: var(--red); } .stat-card .label { font-size: 11px; color: var(--dim); }
  .table-wrap { overflow: auto; max-height: 70vh; background: var(--card); border: 1px solid var(--border); border-radius: 12px; }
  table { width: 100%; border-collapse: collapse; }
  th { background: var(--bg); padding: 10px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: .4px; color: var(--dim); text-align: left; white-space: nowrap; position: sticky; top: 0; z-index: 1; }
  td { padding: 8px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  tr:hover td { background: rgba(0,175,160,.05); }
  .status { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; }
  .status-pending { background: rgba(196,136,10,.12); color: var(--amber); }
  .status-completed { background: rgba(0,175,160,.15); color: var(--green); }
  .status-canceled { background: rgba(239,68,68,.15); color: var(--red); }
  .status-processing { background: rgba(0,175,160,.15); color: var(--accent); }
  .status-held { background: rgba(196,136,10,.12); color: var(--amber); }
  .status-released { background: rgba(0,175,160,.15); color: var(--green); }
  .status-captured { background: rgba(0,175,160,.15); color: var(--green); }
  .status-authorized { background: rgba(0,175,160,.15); color: var(--accent); }
  .status-requires_action { background: rgba(239,68,68,.15); color: var(--red); }
  .status-refunded { background: rgba(239,68,68,.15); color: var(--red); }
  .refund-badge { display: inline-block; padding: 2px 6px; border-radius: 10px; font-size: 10px; font-weight: 600; background: rgba(239,68,68,.2); color: var(--red); margin-right: 4px; }
  .amount { text-align: right; font-variant-numeric: tabular-nums; }
  .loading { text-align: center; padding: 40px; color: var(--dim); }
  .empty { text-align: center; padding: 40px; color: var(--dim); font-size: 14px; }
  .pagination { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-top: 1px solid var(--border); }
  .pagination .info { color: var(--dim); font-size: 12px; }
  .pagination .btns { display: flex; gap: 6px; }
  .error { color: var(--red); padding: 12px; background: rgba(239,68,68,.1); border-radius: 6px; margin-bottom: 12px; }
  code { font-size: 11px; color: var(--accent); }
  .calc-badge { font-size: 9px; color: var(--accent); font-weight: 400; text-transform: none; letter-spacing: 0; }
  .db-badge { font-size: 9px; color: var(--dim); font-weight: 400; text-transform: none; letter-spacing: 0; }
  .copy-btn { cursor: pointer; font-size: 12px; opacity: 0.5; transition: opacity .15s; user-select: none; }
  .copy-btn:hover { opacity: 1; }
  .legend { display: flex; gap: 16px; margin-bottom: 8px; font-size: 11px; color: var(--dim); }
  .tabs { display: flex; gap: 0; margin-bottom: 16px; border-bottom: 2px solid var(--border); }
  .tab-btn { padding: 10px 24px; font-size: 14px; font-weight: 500; cursor: pointer; border: none; background: none; color: var(--dim); border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all .15s; }
  .tab-btn:hover { color: var(--text); }
  .tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .status-SUCCESSFUL { background: rgba(0,175,160,.15); color: var(--green); }
  .status-REFUNDED, .status-PARTIALLY_REFUNDED { background: rgba(239,68,68,.15); color: var(--red); }
  .match-badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600; }
  .match-matched { background: rgba(0,175,160,.2); color: var(--green); }
  .match-mismatch { background: rgba(196,136,10,.15); color: var(--amber); }
  .match-not-found { background: rgba(239,68,68,.2); color: var(--red); }
  .match-refunded { background: rgba(0,175,160,.2); color: var(--accent); }
  .success { color: var(--green); padding: 12px; background: rgba(0,175,160,.1); border-radius: 6px; margin-bottom: 12px; }
  .upload-zone { border: 2px dashed var(--border); border-radius: 12px; padding: 40px; text-align: center; cursor: pointer; transition: all .15s; margin-bottom: 16px; }
  .upload-zone:hover, .upload-zone.dragover { border-color: var(--accent); background: rgba(0,175,160,.05); }
  .upload-zone .upload-icon { font-size: 32px; margin-bottom: 8px; }
  .upload-zone .upload-title { font-size: 14px; font-weight: 500; margin-bottom: 4px; }
  .upload-zone .upload-hint { font-size: 11px; color: var(--dim); }
  .upload-zone input[type=file] { display: none; }
  .preview-box { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 16px; }
  .preview-box h3 { font-size: 13px; font-weight: 600; margin-bottom: 8px; }
  .mappings { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
  .mapping { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 6px 12px; font-size: 12px; }
  .mapping .mfield { color: var(--dim); font-size: 10px; }
  .mapping .col { color: var(--text); font-weight: 500; }
  .mapping .check { color: var(--green); margin-left: 4px; }
  .mapping .warn { color: var(--amber); margin-left: 4px; }
  .preview-table-wrap { font-size: 11px; max-height: 200px; overflow: auto; }
  .preview-table-wrap table { font-size: 11px; }
  .preview-table-wrap th { font-size: 10px; padding: 6px 10px; }
  .preview-table-wrap td { padding: 4px 10px; font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>💰 Order Reconciliation</h1>
  <div class="nav"><a href="/recon/" class="btn btn-secondary btn-sm">← Portal Home</a><span class="badge">Production DB</span></div>
</header>
<div class="container">
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('download')" id="tab-download">📋 Download Board</button>
    <button class="tab-btn" onclick="switchTab('reconcile')" id="tab-reconcile">🔄 Reconcile</button>
  </div>
  <div class="tab-content active" id="download-tab">
  <div class="filters">
    <div class="filter-group"><label>Date From</label><input type="date" id="dateFrom"></div>
    <div class="filter-group"><label>Date To</label><input type="date" id="dateTo"></div>
    <div class="filter-group"><label>Order Status</label><select id="orderStatus"><option value="">All</option><option value="pending">Pending</option><option value="completed">Completed</option><option value="canceled">Canceled</option><option value="draft">Draft</option><option value="archived">Archived</option><option value="requires_action">Requires Action</option></select></div>
    <div class="filter-group"><label>Payment Status</label><select id="paymentStatus"><option value="">All</option><option value="completed">Completed</option><option value="authorized">Authorized</option><option value="canceled">Canceled</option><option value="not_paid">Not Paid</option><option value="requires_action">Requires Action</option></select></div>
    <div class="filter-group"><label>Escrow Status</label><select id="escrowStatus"><option value="">All</option><option value="held">Held</option><option value="released">Released</option><option value="refunded">Refunded</option></select></div>
    <div class="filter-group"><label>Logistics Status</label><select id="logisticsStatus"><option value="">All</option><option value="pending">Pending</option><option value="packed">Packed</option><option value="shipped">Shipped</option><option value="delivered">Delivered</option><option value="canceled">Canceled</option></select></div>
    <div class="filter-group"><label>Payment Provider</label><select id="paymentProvider"><option value="">All</option><option value="GCash">GCash</option><option value="Xendit">Xendit</option><option value="Stripe">Stripe</option><option value="System">System</option></select></div>
    <div class="filter-group"><label>Payment Method</label><select id="paymentMethod"><option value="">All</option><optgroup label="GCash"><option value="Wallet">Wallet (Balance)</option><option value="GCredit">GCredit</option><option value="GGives">GGives</option></optgroup><optgroup label="Xendit"><option value="GCash (Xendit)">GCash</option><option value="Maya">Maya</option><option value="Credit Card">Credit Card</option></optgroup><option value="Card (Stripe)">Card (Stripe)</option></select></div>
    <div class="filter-group"><label>Search (Order / Merchant / Buyer)</label><input type="text" id="search" placeholder="e.g. order ID, merchant, email, name"></div>
    <button class="btn btn-primary" onclick="fetchData()">🔍 Filter</button>
    <button class="btn btn-secondary" onclick="resetFilters()">↺ Reset</button>
    <button class="btn btn-secondary btn-sm" onclick="exportCSV()">📥 Export CSV</button>
  </div>
  <div class="stats" id="stats"></div>
  <div class="legend">
    <span><span class="calc-badge">⚡ calc</span> = computed</span>
    <span><span class="db-badge">📋 DB</span> = from database</span>
  </div>
  <div id="error" style="display:none"></div>
  <div class="table-wrap">
    <div id="loading" class="loading">Loading data...</div>
    <table id="results" style="display:none">
      <thead><tr><th>Order # <span class="db-badge">📋</span></th><th>Date <span class="db-badge">📋</span></th><th>Merchant <span class="db-badge">📋</span></th><th>Buyer <span class="db-badge">📋</span></th><th>Product <span class="db-badge">📋</span></th><th>Order Status <span class="db-badge">📋</span></th><th>Payment <span class="db-badge">📋</span></th><th>Provider <span class="db-badge">📋</span></th><th>Method <span class="db-badge">📋</span></th><th>Xendit Ref <span class="db-badge">📋</span></th><th>Escrow <span class="db-badge">📋</span></th><th>Logistics <span class="db-badge">📋</span></th><th class="amount">Est. Ship <span class="db-badge">📋</span></th><th class="amount">Total <span class="calc-badge">⚡</span></th><th class="amount">Actual Ship <span class="db-badge">📋</span></th><th class="amount">Payment <span class="db-badge">📋</span></th><th class="amount">Refund <span class="db-badge">📋</span></th><th class="amount">Net Payment <span class="calc-badge">⚡</span></th><th class="amount">Commission <span class="db-badge">📋</span></th><th class="amount">Service Fee <span class="db-badge">📋</span></th><th class="amount">Txn Fee <span class="db-badge">📋</span></th><th class="amount">WHT <span class="calc-badge">⚡</span></th><th class="amount">Net Escrow <span class="calc-badge">⚡</span></th></tr></thead>
      <tbody id="tbody"></tbody>
    </table>
    <div class="pagination" id="pagination" style="display:none"></div>
  </div>
  </div><!-- /download-tab -->

  <!-- RECONCILE TAB -->
  <div class="tab-content" id="reconcile-tab">
    <div style="margin-bottom:14px;display:flex;gap:8px;flex-wrap:wrap;">
      <button class="btn btn-primary" id="modeAnchorBtn" onclick="switchReconMode('anchor')">📒 Ledger Anchor Recon</button>
      <button class="btn btn-secondary" id="modeCsvBtn" onclick="switchReconMode('csv')">📄 CSV-Based Recon</button>
      <button class="btn btn-secondary" id="modeGuideBtn" onclick="switchReconMode('guide')">📖 Guide</button>
    </div>
    <div id="anchorPanel" style="display:none;margin-bottom:14px;padding:14px;background:#F8FAFC;border:1px solid rgba(0,175,160,.25);border-radius:10px;">
      <style>.chip{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border:1px solid rgba(0,175,160,.25);border-radius:14px;font-size:12px;cursor:pointer;background:#E0F5F3;color:var(--text);user-select:none}.chip:hover{border-color:var(--accent)}.chip input{accent-color:var(--accent);margin:0}</style>
      <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;">
        <div class="filter-group"><label>Date From</label><input type="date" id="anchorDateFrom"></div>
        <div class="filter-group"><label>Date To</label><input type="date" id="anchorDateTo"></div>
        <div class="filter-group"><label>Anchor Status</label>
          <div id="anchorStatusChips" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
            <label class="chip"><input type="checkbox" value="COMPLETED" checked>✅ Payment Completed</label>
            <label class="chip"><input type="checkbox" value="CANCELED" checked>Order Canceled</label>
            <label class="chip"><input type="checkbox" value="AUTHORIZED">Authorized</label>
            <label class="chip"><input type="checkbox" value="NOT_PAID">Not Paid</label>
            <span onclick="setAnchorStatuses(true)" style="font-size:11px;color:var(--accent);cursor:pointer;text-decoration:underline;">All</span>
            <span onclick="setAnchorStatuses(false)" style="font-size:11px;color:var(--accent);cursor:pointer;text-decoration:underline;margin-left:4px;">None</span>
          </div></div>
        <button class="btn btn-primary" id="runAnchorBtn" onclick="runAnchorRecon()">📒 Run Anchor Recon</button>
      </div>
      <div style="margin-top:10px;font-size:12px;color:var(--dim);line-height:1.6;">
        <b>Anchor</b> = every payment session of orders in <b>our</b> ledger for the date range + status — the completeness basis, not the CSV.<br>
        CSV upload above is <b>optional evidence</b>: sessions missing from the CSV are flagged ❌ (completeness gap), amount differences ⚠️, CSV refs with no session match ➕.<br>
        Default = paid orders (payment completed) + canceled orders (created then canceled) — both should appear in provider settlement files.
      </div>
    </div>

    <div id="guidePanel" style="display:none;margin-bottom:14px;padding:14px;background:#F8FAFC;border:1px solid rgba(0,175,160,.25);border-radius:10px;font-size:13px;line-height:1.7;color:#1A1035;">
      <b>📖 How to use this recon tool</b>
      <div style="margin-top:8px;">
        <b style="color:var(--accent)">📒 Ledger Anchor mode (recommended):</b>
        <ol style="margin:6px 0 10px 18px;padding:0;">
          <li>Set <b>Date From / To</b> (Manila) and tick the <b>statuses</b> to cover — the anchor = every matching record in <b>our</b> ledger.</li>
          <li>(Optional) Upload the 3rd-party file (CSV) as evidence.</li>
          <li>Click <b>📒 Run Anchor Recon</b>.</li>
        </ol>
        <b>Reading the results:</b>
        <ul style="margin:6px 0 10px 18px;padding:0;">
          <li>✅ <b>Matched</b> — in our ledger and the file agrees.</li>
          <li>⚠️ <b>Amount Mismatch</b> — amounts differ (see Diff column).</li>
          <li>❌ <b>Missing from CSV</b> — in our ledger but absent from the 3rd-party file = <b>completeness gap</b>.</li>
          <li>➕ <b>Not in Ledger</b> — in the file but no match in our records.</li>
        </ul>
        <b>Completeness %</b> = matched share of the anchor. Use <b>📥 Export</b> to pull the exceptions for follow-up.<br>
        <b style="color:var(--accent)">📄 CSV-Based mode:</b> upload the file and match it against our ledger (per-row matched / mismatch / not found).<br>
        <span style="color:var(--dim);font-size:12px;">Amounts compare against the <b>net</b> payment (gross − refunds). ↩ Refunded = order fully refunded at escrow level.</span>
        <div style="margin-top:8px;color:var(--dim);font-size:12px;">Tip: anchor on <b>our</b> data first — a 3rd-party file can be silently incomplete.</div>
      </div>
    </div>
    <div class="upload-zone" id="uploadZone" onclick="document.getElementById('csvUpload').click()">
      <div class="upload-icon">📁</div>
      <div class="upload-title">Upload Xendit / GCash Settlement CSV</div>
      <div class="upload-hint">Drag & drop or click to browse. Matches on Reference column against payment sessions.</div>
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

    <div id="reconcile-status" style="display:none"></div>

    <div class="stats" id="reconcileStats" style="display:none"></div>

    <div class="filters" id="reconcileFilters" style="display:none">
      <div class="filter-group"><label>Search</label><input type="text" id="reconSearch" placeholder="Reference, Order #, Merchant" oninput="filterReconcileResults()"></div>
      <div class="filter-group"><label>Match Type</label><select id="reconMatchType" onchange="filterReconcileResults()"><option value="">All</option><option value="matched">✅ Matched</option><option value="refunded">↩ Refunded</option><option value="mismatch">⚠ Amount Mismatch</option><option value="not-found">❌ Not in System</option></select></div>
      <span id="reconFilterCount" style="color:var(--dim);font-size:12px;align-self:flex-end;padding-bottom:8px;"></span>
      <button class="btn btn-secondary btn-sm" onclick="document.getElementById('reconSearch').value='';document.getElementById('reconMatchType').value='';filterReconcileResults();">↺ Clear</button>
    </div>

    <div class="table-wrap" id="reconcileTableWrap" style="display:none">
      <table id="reconcileResults">
        <thead id="reconcileHead"><tr>
          <th>Match</th><th>CSV Reference</th><th class="amount">CSV Amount</th><th class="amount">DB Amount</th><th class="amount">Diff</th>
          <th>CSV Status</th><th>DB Status / Refund</th><th>CSV Channel</th><th>Order #</th><th>Merchant</th>
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
var currentPage=1,PAGE_SIZE=50;
function f(n){return n||'0.00';}
function getFilters(){return{date_from:document.getElementById('dateFrom').value,date_to:document.getElementById('dateTo').value,order_status:document.getElementById('orderStatus').value,payment_status:document.getElementById('paymentStatus').value,escrow_status:document.getElementById('escrowStatus').value,logistics_status:document.getElementById('logisticsStatus').value,payment_provider:document.getElementById('paymentProvider').value,payment_method:document.getElementById('paymentMethod').value,search:document.getElementById('search').value,page:currentPage,page_size:PAGE_SIZE};}
function fetchData(){currentPage=1;loadData();}
function loadData(){
  document.getElementById('loading').style.display='block';
  document.getElementById('results').style.display='none';
  document.getElementById('error').style.display='none';
  var p=new URLSearchParams(getFilters());
  fetch('/recon/api/orders?'+p).then(function(r){return r.json();}).then(function(d){
    if(d.error){document.getElementById('error').innerHTML='<div class="error">'+d.error+'</div>';document.getElementById('error').style.display='block';document.getElementById('loading').style.display='none';return;}
    renderStats(d.stats);renderTable(d.rows);renderPagination(d.total,d.page,d.page_size);
    document.getElementById('loading').style.display='none';document.getElementById('results').style.display='table';document.getElementById('pagination').style.display='flex';
  }).catch(function(e){document.getElementById('error').innerHTML='<div class="error">Error: '+e.message+'</div>';document.getElementById('error').style.display='block';document.getElementById('loading').style.display='none';});
}
function renderStats(s){if(!s)return;document.getElementById('stats').innerHTML='<div class="stat-card"><div class="value">'+(s.total_orders||0)+'</div><div class="label">Orders</div></div><div class="stat-card"><div class="value green">₱'+fmtNum(s.total_revenue||0)+'</div><div class="label">Revenue</div></div><div class="stat-card"><div class="value amber">₱'+fmtNum(s.total_escrow||0)+'</div><div class="label">Escrow</div></div><div class="stat-card"><div class="value red">₱'+fmtNum(s.total_refunds||0)+'</div><div class="label">Refunds</div></div><div class="stat-card"><div class="value">₱'+fmtNum(s.total_commission||0)+'</div><div class="label">Commission</div></div><div class="stat-card"><div class="value">₱'+fmtNum(s.total_service_fee||0)+'</div><div class="label">Service Fee</div></div><div class="stat-card"><div class="value">₱'+fmtNum(s.total_transaction_fee||0)+'</div><div class="label">Txn Fee</div></div><div class="stat-card"><div class="value">₱'+fmtNum(s.total_withholding_tax||0)+'</div><div class="label">WHT</div></div><div class="stat-card"><div class="value green">₱'+fmtNum(s.net_escrow||0)+'</div><div class="label">Net Escrow</div></div>';}
function renderTable(rows){
  var tb=document.getElementById('tbody');
  if(!rows||rows.length===0){tb.innerHTML='<tr><td colspan="23" class="empty">No orders found</td></tr>';return;}
  tb.innerHTML=rows.map(function(r){var buyer=r.buyer_name&&r.buyer_name.trim()?r.buyer_name+' ('+esc(r.buyer_username||'—')+')':esc(r.buyer_username||'—');var refundBadge=r.refund_amount>0?'<span class="refund-badge">Refunded</span>':'';return'<tr><td><code>'+esc(r.order_id)+'</code> <span class="copy-btn" data-copy="'+esc(r.order_id)+'" onclick="copyToClipboard(this)" title="Copy">📋</span></td><td>'+esc(r.order_date)+'</td><td>'+esc(r.merchant)+'</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis" title="'+buyer.replace(/"/g,'&quot;')+'">'+buyer+'</td><td>'+esc(r.product)+'</td><td><span class="status status-'+esc(r.order_status)+'">'+esc(r.order_status)+'</span></td><td><span class="status status-'+(r.payment_status||'na')+'">'+esc(r.payment_status||'N/A')+'</span></td><td>'+esc(r.payment_provider||'—')+'</td><td>'+esc(r.payment_method||'—')+'</td><td><code>'+esc(r.xendit_reference||'—')+'</code></td><td><span class="status status-'+(r.escrow_status||'')+'">'+esc(r.escrow_status||'—')+'</span><br><span class="ref">₱'+fmtNum(r.escrow_amount)+'</span></td><td><span class="status status-'+(r.logistics_status||'pending')+'">'+esc(r.logistics_status||'pending')+'</span></td><td class="amount">₱'+fmtNum(r.estimated_shipping_fee)+'</td><td class="amount"><b>₱'+fmtNum(r.total_price)+'</b></td><td class="amount">₱'+fmtNum(r.actual_shipping_fee)+'</td><td class="amount">₱'+fmtNum(r.payment_amount)+'</td><td class="amount">'+refundBadge+'₱'+fmtNum(r.refund_amount)+'</td><td class="amount"><b>₱'+fmtNum(r.net_payment)+'</b></td><td class="amount">₱'+fmtNum(r.commission_fee)+'</td><td class="amount">₱'+fmtNum(r.service_fee)+'</td><td class="amount">₱'+fmtNum(r.transaction_fee)+'</td><td class="amount">₱'+fmtNum(r.withholding_tax)+'</td><td class="amount"><b>₱'+fmtNum(r.net_escrow)+'</b></td></tr>';}).join('');}
function renderPagination(t,p,ps){var tp=Math.ceil(t/ps);document.getElementById('pagination').innerHTML='<div class="info">Showing '+((p-1)*ps+1)+'–'+Math.min(p*ps,t)+' of '+t+' orders</div><div class="btns"><button class="btn btn-secondary btn-sm" onclick="goPage(1)" '+(p<=1?'disabled':'')+'>««</button><button class="btn btn-secondary btn-sm" onclick="goPage('+(p-1)+')" '+(p<=1?'disabled':'')+'>« Prev</button><span style="padding:4px 12px;color:var(--dim)">Page '+p+' / '+tp+'</span><button class="btn btn-secondary btn-sm" onclick="goPage('+(p+1)+')" '+(p>=tp?'disabled':'')+'>Next »</button><button class="btn btn-secondary btn-sm" onclick="goPage('+tp+')" '+(p>=tp?'disabled':'')+'>»»</button></div>';}
function goPage(p){currentPage=p;loadData();}
function resetFilters(){document.getElementById('dateFrom').value='';document.getElementById('dateTo').value='';document.getElementById('orderStatus').value='';document.getElementById('paymentStatus').value='';document.getElementById('escrowStatus').value='';document.getElementById('logisticsStatus').value='';document.getElementById('paymentProvider').value='';document.getElementById('paymentMethod').value='';document.getElementById('search').value='';currentPage=1;loadData();}
function exportCSV(){var p=new URLSearchParams(getFilters());p.delete('page');p.delete('page_size');p.set('export','csv');window.open('/recon/api/orders?'+p,'_blank');}
function fmtNum(n){if(n===null||n===undefined)return'0.00';return Number(n).toLocaleString('en-PH',{minimumFractionDigits:2,maximumFractionDigits:2});}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function copyToClipboard(el){var text=el.getAttribute('data-copy');navigator.clipboard.writeText(text).then(function(){el.textContent='✅';setTimeout(function(){el.textContent='📋';},1500);}).catch(function(){prompt('Copy:',text);});}
function getTodayDate(){var today=new Date();var y=today.getFullYear();var m=String(today.getMonth()+1).padStart(2,'0');var d=String(today.getDate()).padStart(2,'0');return y+'-'+m+'-'+d;}
setTimeout(function(){switchReconMode('anchor');var today=getTodayDate();document.getElementById('dateFrom').value=today;document.getElementById('dateTo').value=today;loadData();},100);

// ─── Tab Switching ────────────────────────────────────────
function switchTab(tab){
  document.querySelectorAll('.tab-btn').forEach(function(b){b.classList.remove('active');});
  document.querySelectorAll('.tab-content').forEach(function(c){c.classList.remove('active');});
  document.getElementById('tab-'+tab).classList.add('active');
  document.getElementById(tab+'-tab').classList.add('active');
}

// ─── Reconcile ────────────────────────────────────────────
var csvData=[],csvHeaders=[],colMap={},reconcileResults=[],reconMode='anchor',anchorStats=null;

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

function parseCSVLine(l){var r=[],c='',q=false;for(var i=0;i<l.length;i++){var ch=l[i];if(q){if(ch==='"'){if(i+1<l.length&&l[i+1]==='"'){c+='"';i++;}else{q=false;}}else{c+=ch;}}else if(ch==='"'){q=true;}else if(ch===','){r.push(c);c='';}else{c+=ch;}}r.push(c);return r;}

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
  colMap.ref=findCol(/reference/i);colMap.amount=findCol(/amount/i);
  colMap.status=findCol(/^status$/i);colMap.channel=findCol(/payment.*channel/i);
  colMap.method=findCol(/payment.*method/i);colMap.fee=findCol(/total.*fee/i);

  var mapHtml='';
  var flds=[{k:'ref',l:'Reference'},{k:'amount',l:'Amount'},{k:'status',l:'Status'},{k:'channel',l:'Channel'},{k:'fee',l:'Fee'}];
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
  var refCol=colMap.ref;
  if(!refCol){alert('No Reference column found in CSV');return;}
  var btn=document.getElementById('runReconcileBtn');
  btn.disabled=true;btn.textContent='⏳ Matching...';
  document.getElementById('reconcile-status').style.display='none';

  var refs=csvData.map(function(r){return r[refCol];}).filter(function(r){return r&&r.trim()!=='';});
  var uniqueRefs=[];var seen={};
  refs.forEach(function(r){if(!seen[r]){seen[r]=true;uniqueRefs.push(r);}});

  fetch('/recon/order/api/reconcile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({references:uniqueRefs})})
  .then(function(r){return r.json();})
  .then(function(d){
    if(d.error){showReconcileError(d.error);btn.disabled=false;btn.textContent='🔄 Run Reconciliation';return;}
    buildReconcileResults(d.db_map,uniqueRefs);
    btn.disabled=false;btn.textContent='🔄 Run Reconciliation';
  })
  .catch(function(e){showReconcileError(e.message);btn.disabled=false;btn.textContent='🔄 Run Reconciliation';});
}

function showReconcileError(msg){document.getElementById('reconcile-status').innerHTML='<div class="error">'+esc(msg)+'</div>';document.getElementById('reconcile-status').style.display='block';}

function switchReconMode(mode){
  reconMode=mode;
  document.getElementById('modeAnchorBtn').className='btn '+(mode==='anchor'?'btn-primary':'btn-secondary');
  document.getElementById('modeCsvBtn').className='btn '+(mode==='csv'?'btn-primary':'btn-secondary');
  document.getElementById('modeGuideBtn').className='btn '+(mode==='guide'?'btn-primary':'btn-secondary');
  if(mode==='anchor'){
    var t=getTodayDate();
    if(!document.getElementById('anchorDateFrom').value){document.getElementById('anchorDateFrom').value=t;document.getElementById('anchorDateTo').value=t;}
  }
  resetReconcile();
  var isAnchor=mode==='anchor', isGuide=mode==='guide';
  document.getElementById('anchorPanel').style.display=isAnchor?'block':'none';
  document.getElementById('guidePanel').style.display=isGuide?'block':'none';
  document.getElementById('uploadZone').style.display=(isAnchor||mode==='csv')?'block':'none';
  if(!isGuide){document.getElementById('runReconcileBtn').textContent=isAnchor?'📒 Run Anchor Recon':'🔄 Run Reconciliation';}
}
function getAnchorStatuses(def){
  var boxes=document.querySelectorAll('#anchorStatusChips input:checked');
  var vals=[];for(var i=0;i<boxes.length;i++){vals.push(boxes[i].value);}
  return vals.length?vals:def;
}
function setAnchorStatuses(on){
  var boxes=document.querySelectorAll('#anchorStatusChips input');
  for(var i=0;i<boxes.length;i++){boxes[i].checked=on;}
}
function runAnchorRecon(){
  var df=document.getElementById('anchorDateFrom').value,dt=document.getElementById('anchorDateTo').value;
  if(!df||!dt){alert('Set Date From and Date To for the anchor');return;}
  var btn=document.getElementById('runAnchorBtn');
  btn.disabled=true;btn.textContent='⏳ Anchoring...';
  document.getElementById('reconcile-status').style.display='none';
  var payload={dateFrom:df,dateTo:dt,executionStatus:getAnchorStatuses(['COMPLETED','CANCELED']),rows:[]};
  if(csvData.length&&colMap.ref){
    var amtCol=colMap.amount;
    payload.rows=csvData.map(function(r){return {reference:String(r[colMap.ref]||'').trim(),amount:amtCol?(parseFloat(String(r[amtCol]).replace(/[^0-9.-]/g,''))||0):null};}).filter(function(r){return r.reference!=='';});
  }
  fetch('/recon/order/api/reconcile-anchor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
  .then(function(r){return r.json();})
  .then(function(d){
    btn.disabled=false;btn.textContent='📒 Run Anchor Recon';
    if(d.error){showReconcileError(d.error);return;}
    anchorStats=d.stats||null;
    reconcileResults=(d.rows||[]).map(function(x){return {matchType:x.verdict,ref:x.session_id||'',sessionId:x.session_id||'',csvAmt:x.csv_amount==null?null:x.csv_amount,dbGrossAmt:x.payment_amount,dbNetAmt:x.net_amount,dbTotalRefund:Math.max(x.refund_amount||0,x.escrow_refunded||0),escrowStatus:x.escrow_status||'',diff:x.diff==null?null:x.diff,date:x.order_date||'',provider:x.provider||'',orderId:x.order_id||'',dbStatus:x.payment_status||'',orderStatus:x.order_status||'',merchant:x.merchant||''};})
      .concat((d.extras||[]).map(function(x){return {matchType:'not_in_ledger',ref:x.reference||'',sessionId:'',csvAmt:x.csv_amount||0,dbGrossAmt:null,dbNetAmt:null,dbTotalRefund:0,escrowStatus:'',diff:null,date:'',provider:'',orderId:'',dbStatus:'',orderStatus:'',merchant:''};}));
    document.getElementById('reconcileFilters').style.display='flex';
    filterReconcileResults();
  })
  .catch(function(e){btn.disabled=false;btn.textContent='📒 Run Anchor Recon';showReconcileError(e.message);});
}

function buildReconcileResults(dbMap,csvRefs){
  var results=[];
  var refCol=colMap.ref,amtCol=colMap.amount,statusCol=colMap.status,channelCol=colMap.channel;

  csvData.forEach(function(row){
    var ref=row[refCol];
    if(!ref||!ref.trim())return;
    var db=dbMap[ref];
    var csvAmt=parseFloat(row[amtCol])||0;
    var dbGrossAmt=db?parseFloat(db.payment_amount)||0:0;
    var dbRefund=db?parseFloat(db.refund_amount)||0:0;
    var dbEscrowRefund=db?parseFloat(db.escrow_refunded)||0:0;
    var dbTotalRefund=Math.max(dbRefund,dbEscrowRefund);
    var dbNetAmt=dbGrossAmt-dbTotalRefund;
    var diff=csvAmt-dbNetAmt;
    var matchType;
    if(!db){matchType='not-found';}
    else if(dbEscrowRefund>0&&dbNetAmt<0.01){matchType='refunded';}
    else if(Math.abs(diff)<0.01){matchType='matched';}
    else{matchType='mismatch';}

    results.push({
      ref:ref,csvAmt:csvAmt,dbGrossAmt:dbGrossAmt,dbTotalRefund:dbTotalRefund,
      dbNetAmt:dbNetAmt,diff:diff,
      matchType:matchType,
      csvStatus:statusCol?row[statusCol]:'',
      dbStatus:db?db.payment_status:'',
      escrowStatus:db?db.escrow_status||'':'',
      csvChannel:channelCol?row[channelCol]:'',
      dbProvider:db?db.provider_id:'',
      orderId:db?db.order_id:'',
      merchant:db?db.merchant:'',
      xenditRef:db?db.xendit_reference||'':'',
      dbRefund:dbRefund
    });
  });

  reconcileResults=results;
  document.getElementById('reconcileFilters').style.display='flex';
  filterReconcileResults();
}

function renderReconcileStats(r){
  if(reconMode==='anchor'&&anchorStats){
    var s=anchorStats;
    var csvTxt=s.csv_evidence?'':' <span style="font-size:11px;color:var(--dim)">(no CSV uploaded)</span>';
    var pctColor=s.completeness_pct>=100?'green':(s.completeness_pct>=90?'amber':'red');
    document.getElementById('reconcileStats').innerHTML=
      '<div class="stat-card"><div class="value">'+s.anchor_total+'</div><div class="label">Anchor Sessions</div></div>'+
      '<div class="stat-card"><div class="value green">'+s.matched+'</div><div class="label">✅ Matched</div></div>'+
      '<div class="stat-card"><div class="value blue">'+s.refunded+'</div><div class="label">↩ Refunded</div></div>'+
      '<div class="stat-card"><div class="value red">'+s.missing+'</div><div class="label">❌ Missing from CSV (₱'+fmtNum(s.missing_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value amber">'+s.mismatch+'</div><div class="label">⚠ Amount Mismatch (₱'+fmtNum(s.mismatch_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value blue">'+s.extras+'</div><div class="label">➕ Not in Ledger (₱'+fmtNum(s.extras_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value '+pctColor+'">'+s.completeness_pct+'%</div><div class="label">Completeness'+csvTxt+'</div></div>'+
      '<div class="stat-card"><div class="value">₱'+fmtNum(s.anchor_amount)+'</div><div class="label">Anchor Gross</div></div>';
    return;
  }
  var matched=r.filter(function(x){return x.matchType==='matched';}).length;
  var refunded=r.filter(function(x){return x.matchType==='refunded';}).length;
  var mismatch=r.filter(function(x){return x.matchType==='mismatch';}).length;
  var notFound=r.filter(function(x){return x.matchType==='not-found';}).length;
  var tAmt=r.reduce(function(s,x){return s+x.csvAmt;},0);
  var tDiff=r.reduce(function(s,x){return s+Math.abs(x.diff);},0);
  document.getElementById('reconcileStats').innerHTML=
    '<div class="stat-card"><div class="value">'+r.length+'</div><div class="label">CSV Rows</div></div>'+
    '<div class="stat-card"><div class="value green">'+matched+'</div><div class="label">✅ Matched</div></div>'+
    '<div class="stat-card"><div class="value blue">'+refunded+'</div><div class="label">↩ Refunded</div></div>'+
    '<div class="stat-card"><div class="value amber">'+mismatch+'</div><div class="label">⚠ Amount Mismatch</div></div>'+
    '<div class="stat-card"><div class="value red">'+notFound+'</div><div class="label">❌ Not in System</div></div>'+
    '<div class="stat-card"><div class="value blue">₱'+fmtNum(tAmt)+'</div><div class="label">Total CSV Amount</div></div>'+
    '<div class="stat-card"><div class="value red">₱'+fmtNum(tDiff)+'</div><div class="label">Total Variance</div></div>';
}

function renderReconcileTable(results){
  var tb=document.getElementById('reconcileTbody');
  var head=document.getElementById('reconcileHead');
  if(reconMode==='anchor'){
    head.innerHTML='<tr><th>Match</th><th>Session ID</th><th>Order #</th><th>Order Date</th><th class="amount">Net Amt</th><th class="amount">CSV Amt</th><th class="amount">Diff</th><th>Provider</th><th>Pay Status</th><th>Order Status</th></tr>';
    if(results.length===0){tb.innerHTML='<tr><td colspan="10" class="empty">No results</td></tr>';return;}
    tb.innerHTML=results.map(function(r){
      var badge=r.matchType==='matched'?'<span class="match-badge match-matched">✅ Matched</span>'
        :r.matchType==='refunded'?'<span class="match-badge match-refunded">↩ Refunded</span>'
        :r.matchType==='amount_mismatch'?'<span class="match-badge match-mismatch">⚠ Mismatch</span>'
        :r.matchType==='missing'?'<span class="match-badge match-not-found">❌ Missing from CSV</span>'
        :'<span class="match-badge match-escrow">➕ Not in Ledger</span>';
      var diffColor=Math.abs(r.diff||0)<0.01?'var(--green)':'var(--red)';
      return'<tr><td>'+badge+'</td><td><code>'+esc(r.ref||'—')+'</code></td><td><code>'+esc(r.orderId||'—')+'</code></td>'+
        '<td>'+esc(r.date||'—')+'</td><td class="amount">'+(r.dbNetAmt==null?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.dbNetAmt))+'</td>'+
        '<td class="amount">'+(r.csvAmt==null?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.csvAmt))+'</td>'+
        '<td class="amount" style="color:'+diffColor+'">'+(r.diff==null?'—':((r.diff>=0?'+':'')+fmtNum(r.diff)))+'</td>'+
        '<td>'+esc(r.provider||'—')+'</td><td>'+esc(r.dbStatus||'—')+'</td><td>'+esc(r.orderStatus||'—')+'</td></tr>';
    }).join('');
    return;
  }
  head.innerHTML='<tr><th>Match</th><th>CSV Reference</th><th class="amount">CSV Amount</th><th class="amount">DB Amount</th><th class="amount">Diff</th><th>CSV Status</th><th>DB Status / Refund</th><th>CSV Channel</th><th>Order #</th><th>Merchant</th></tr>';
  if(results.length===0){tb.innerHTML='<tr><td colspan="10" class="empty">No results</td></tr>';return;}
  tb.innerHTML=results.map(function(r){
    var badge='';
    if(r.matchType==='matched')badge='<span class="match-badge match-matched">✅ Matched</span>';
    else if(r.matchType==='refunded')badge='<span class="match-badge match-refunded">↩ Refunded</span>';
    else if(r.matchType==='mismatch')badge='<span class="match-badge match-mismatch">⚠ Mismatch</span>';
    else badge='<span class="match-badge match-not-found">❌ Not Found</span>';
    var dbStatus=r.dbStatus||'—';
    var escrowNote=r.escrowStatus==='refunded'?' <span style="font-size:10px;color:var(--red)">(escrow refunded ₱'+fmtNum(r.dbTotalRefund)+')</span>':'';
    var dbAmtCell=r.dbTotalRefund>0?'<span style="text-decoration:line-through;color:var(--dim)">₱'+fmtNum(r.dbGrossAmt)+'</span> <span style="color:var(--red)">₱'+fmtNum(r.dbNetAmt)+'</span>':'₱'+fmtNum(r.dbGrossAmt);
    var csvStatus=r.csvStatus||'—';
    var diffColor=Math.abs(r.diff)<0.01?'var(--green)':'var(--red)';
    return'<tr><td>'+badge+'</td><td><code>'+esc(r.ref)+'</code></td><td class="amount">₱'+fmtNum(r.csvAmt)+'</td><td class="amount">'+dbAmtCell+'</td><td class="amount" style="color:'+diffColor+'">'+(r.diff>=0?'+':'')+fmtNum(r.diff)+'</td><td><span class="status status-'+(csvStatus.replace(/[^a-zA-Z0-9_]/g,'_'))+'">'+esc(csvStatus)+'</span></td><td><span class="status status-'+(dbStatus.replace(/[^a-zA-Z0-9_]/g,'_'))+'">'+esc(dbStatus)+'</span>'+escrowNote+'</td><td>'+esc(r.csvChannel)+'</td><td><code>'+esc(r.orderId||'—')+'</code></td><td>'+esc(r.merchant||'—')+'</td></tr>';
  }).join('');
}

function filterReconcileResults(){
  var opts=reconMode==='anchor'
    ?[['','All'],['matched','✅ Matched'],['refunded','↩ Refunded'],['amount_mismatch','⚠ Amount Mismatch'],['missing','❌ Missing from CSV'],['not_in_ledger','➕ CSV Not in Ledger']]
    :[['','All'],['matched','✅ Matched'],['refunded','↩ Refunded'],['mismatch','⚠ Amount Mismatch'],['not-found','❌ Not in System']];
  var sel=document.getElementById('reconMatchType');
  var cur=sel.value;
  sel.innerHTML=opts.map(function(o){return'<option value="'+o[0]+'">'+o[1]+'</option>';}).join('');
  if(opts.some(function(o){return o[0]===cur;}))sel.value=cur;else sel.value='';
  var search=(document.getElementById('reconSearch').value||'').toLowerCase();
  var matchType=sel.value;
  var filtered=reconcileResults.filter(function(r){
    if(matchType&&r.matchType!==matchType)return false;
    if(search){
      var ref=(r.ref||'').toLowerCase();
      var oid=(r.orderId||'').toLowerCase();
      var mer=(r.merchant||'').toLowerCase();
      if(ref.indexOf(search)===-1&&oid.indexOf(search)===-1&&mer.indexOf(search)===-1)return false;
    }
    return true;
  });
  renderReconcileStats(filtered);
  renderReconcileTable(filtered);
  document.getElementById('reconFilterCount').textContent='Showing '+filtered.length+' of '+reconcileResults.length;
  document.getElementById('reconcileStats').style.display='flex';
  document.getElementById('reconcileTableWrap').style.display='block';
  document.getElementById('reconcileExport').style.display='block';
}

function exportReconcileCSV(){
  if(reconMode==='anchor'){
    var rows=[['Match','Session ID','Order #','Order Date','Net Amount','CSV Amount','Diff','Provider','Pay Status','Order Status']];
    reconcileResults.forEach(function(r){rows.push([r.matchType,r.ref||r.sessionId||'',r.orderId||'',r.date||'',r.dbNetAmt==null?'':r.dbNetAmt,r.csvAmt==null?'':r.csvAmt,r.diff==null?'':r.diff,r.provider||'',r.dbStatus||'',r.orderStatus||'']);});
    var csv=rows.map(function(r){return r.map(function(c){return'"'+String(c==null?'':c).replace(/"/g,'""')+'"';}).join(',');}).join('\n');
    var a=document.createElement('a');a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);a.download='orders-anchor-recon.csv';a.click();
    return;
  }
  var h='Match,Reference,CSV Amount,DB Gross Amount,DB Refund,DB Net Amount,Diff,CSV Status,DB Status,Escrow Status,CSV Channel,Order #,Merchant\n';
  var rows=reconcileResults.map(function(r){
    return [r.matchType,r.ref,r.csvAmt,r.dbGrossAmt,r.dbTotalRefund,r.dbNetAmt,r.diff,r.csvStatus,r.dbStatus,r.escrowStatus,r.csvChannel,r.orderId,r.merchant].map(function(v){return'"'+String(v||'').replace(/"/g,'""')+'"';}).join(',');
  }).join('\n');
  var blob=new Blob(['\uFEFF'+h+rows],{type:'text/csv;charset=utf-8'});
  var url=URL.createObjectURL(blob);
  var a=document.createElement('a');a.href=url;a.download='recon_order_'+new Date().toISOString().slice(0,10)+'.csv';
  a.click();URL.revokeObjectURL(url);
}
</script>
</body>
</html>"""

def _serialize(rows):
    result = []
    for r in rows:
        row = {}
        for k, v in dict(r).items():
            if v is None:
                row[k] = None
            elif type(v).__name__ == 'Decimal':
                row[k] = float(v)
            elif hasattr(v, 'isoformat'):
                row[k] = v.strftime("%Y-%m-%d %H:%M")
            else:
                row[k] = v
        result.append(row)
    return result