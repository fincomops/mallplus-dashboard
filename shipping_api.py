"""Reconciliation Portal — Shipping Fee Recon API"""
import json, csv, io
import psycopg2.extras
from datetime import datetime

from recon_db import get_db

_BASE_SQL = """
SELECT
    js.id AS shipment_id,
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

# ═══════════════════════════════════════════════════════════════════════════
# EXPECTED FEE ENGINE — J&T Express Forward rate card (Phase 1)
# Canonical card: brc_01KRX4CX8A95NQWS1H5SJ3PWHJ (100 routes, flat fees,
# insurance 0.25% of whole order above ₱1,200 = J&T "valuation fee").
# All cards share identical pair-level fees; this card has one route per pair
# (no sub-route ambiguity) and is the original upload for the J&T channel.
# Rate lookup: origin_region → destination_region → first-kilo base fee.
# Weight is NULL across jt_shipment, so the 0-weight bucket (first-kilo) applies.
# ═══════════════════════════════════════════════════════════════════════════
_JT_RULE_ID = "brl_01KRX4CX8573TRRTVR950FFTWG"
_JT_CARD_ID = "brc_01KRX4CX8A95NQWS1H5SJ3PWHJ"
_NCR_CITIES = ("MANILA", "TAGUIG", "CALOOCAN", "MANDALUYONG", "QUEZON", "PASIG", "MAKATI",
               "PARANAQUE", "LAS PINAS", "MUNTINLUPA", "MARIKINA", "NAVOTAS", "PATEROS",
               "SAN JUAN", "VALENZUELA", "MALABON")

_RATE_ENGINE = None


def _load_rate_engine():
    """Load the J&T forward rate card into memory: {(oreg, dreg): first_kilo_base_fee}"""
    global _RATE_ENGINE
    if _RATE_ENGINE is not None:
        return _RATE_ENGINE
    eng = {"effective_at": None, "insurance_enabled": False,
           "insurance_threshold": 0.0, "insurance_fee_value": 0.0, "pairs": {}}
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT effective_start_at, insurance_enabled, insurance_threshold, "
            "insurance_charge_mode, insurance_fee_value FROM billing_rate_card "
            "WHERE id=%s AND deleted_at IS NULL", (_JT_CARD_ID,))
        card = cur.fetchone()
        if card:
            eng["effective_at"] = card["effective_start_at"]
            eng["insurance_enabled"] = bool(card["insurance_enabled"])
            eng["insurance_threshold"] = float(card["insurance_threshold"] or 0)
            eng["insurance_fee_value"] = float(card["insurance_fee_value"] or 0)
        cur.execute("SELECT id, origin_region, destination_region FROM billing_route WHERE billing_rule_id=%s",
                    (_JT_RULE_ID,))
        routes = {r["id"]: (r["origin_region"], r["destination_region"]) for r in cur.fetchall()}
        # Rate lines: flat fees, weight in grams. First-kilo bucket = weight_min 0.
        cur.execute("SELECT route_id, weight_min, base_fee FROM billing_rate_line "
                    "WHERE deleted_at IS NULL AND rate_card_id=%s AND fee_mode='flat'",
                    (_JT_CARD_ID,))
        for l in cur.fetchall():
            rp = routes.get(l["route_id"])
            if rp and l["weight_min"] == 0 and rp not in eng["pairs"]:
                eng["pairs"][rp] = float(l["base_fee"])
        conn.close()
    except Exception:
        pass
    _RATE_ENGINE = eng
    return eng


def _expected_fee(eng, created_at, oreg, dreg, order_value):
    """Expected J&T fee for a shipment. Returns None when no rate applies."""
    try:
        if eng["effective_at"] is not None and created_at and created_at < eng["effective_at"]:
            return None  # shipped before rate card effective date
        if not oreg or not dreg:
            return None
        base = eng["pairs"].get((oreg, dreg))
        if base is None:
            return None
        fee = base
        if eng["insurance_enabled"] and order_value and order_value > eng["insurance_threshold"]:
            fee += float(order_value) * eng["insurance_fee_value"]  # valuation fee
        return round(fee, 2)
    except Exception:
        return None


def _variance_flag(expected, charged):
    if expected is None:
        return "no_rate"
    diff = float(charged or 0) - expected
    if abs(diff) < 0.01:
        return "matched"
    return "over" if diff > 0 else "under"


# ── Reconcile tab: match uploaded J&T bill CSV rows against shipments ──
_SHIP_RECON_LOOKUP_SQL = """
SELECT DISTINCT ON (fl.tracking_number)
    fl.tracking_number AS tracking_number,
    js.id AS shipment_id,
    COALESCE(oe.order_sn, o.id) AS order_id,
    js.status AS shipment_status,
    js.logistics_status,
    js.created_at,
    COALESCE(osm2.amount, 0) AS charged_fee,
    COALESCE(pp.region_name, pp2.region_name,
        CASE WHEN UPPER(REPLACE(sla.city, ' City', '')) IN %(ncr)s THEN 'GMA' END) AS origin_region,
    pp_d.region_name AS dest_region,
    COALESCE(oi_val.items_total, 0) + COALESCE(osm2.amount, 0) AS order_value
FROM public.fulfillment_label fl
JOIN public.fulfillment f ON f.id = fl.fulfillment_id AND f.deleted_at IS NULL
JOIN public.jt_shipment js ON js.fulfillment_id = f.id AND js.deleted_at IS NULL
JOIN public."order" o ON o.id = js.order_id AND o.deleted_at IS NULL
LEFT JOIN public.order_extension oe ON oe.order_id = o.id
LEFT JOIN public.stock_location sl ON sl.id = f.location_id AND sl.deleted_at IS NULL
LEFT JOIN public.stock_location_address sla ON sla.id = sl.address_id AND sla.deleted_at IS NULL
LEFT JOIN public.ph_province pp ON pp.name = sla.province AND pp.deleted_at IS NULL
LEFT JOIN public.ph_city_municipality pcm2 ON (
    pcm2.name = sla.city OR pcm2.name ILIKE 'City of ' || sla.city OR pcm2.name ILIKE sla.city || ' City'
) AND pcm2.deleted_at IS NULL
LEFT JOIN public.ph_province pp2 ON pp2.name = pcm2.province_name AND pp2.deleted_at IS NULL
LEFT JOIN public.fulfillment_address fa ON fa.id = f.delivery_address_id AND fa.deleted_at IS NULL
LEFT JOIN public.ph_province pp_d ON pp_d.name = fa.province AND pp_d.deleted_at IS NULL
LEFT JOIN LATERAL (
    SELECT osm2.amount
    FROM public.order_shipping os2
    JOIN public.order_shipping_method osm2 ON osm2.id = os2.shipping_method_id AND osm2.deleted_at IS NULL
    WHERE os2.order_id = o.id AND os2.deleted_at IS NULL
    LIMIT 1
) osm2 ON true
LEFT JOIN LATERAL (
    SELECT COALESCE(SUM(oi_latest.quantity * COALESCE(oi_latest.unit_price, oli_sum.unit_price, 0)), 0) AS items_total
    FROM (
        SELECT DISTINCT ON (oi_sum.item_id) oi_sum.quantity, oi_sum.unit_price, oi_sum.item_id
        FROM public.order_item oi_sum
        WHERE oi_sum.order_id = o.id AND oi_sum.deleted_at IS NULL
        ORDER BY oi_sum.item_id, oi_sum.version DESC
    ) oi_latest
    LEFT JOIN public.order_line_item oli_sum ON oli_sum.id = oi_latest.item_id AND oli_sum.deleted_at IS NULL
) oi_val ON true
WHERE fl.deleted_at IS NULL AND fl.tracking_number = ANY(%(trackings)s)
"""


def handle_shipping_reconcile_api(body_json):
    """Match uploaded J&T bill CSV rows against shipments by tracking number.
    CSV columns expected: tracking, shipping_fee, valuation_fee, date (delivered/returned).
    Returns per-row: matched / mismatch (billed vs expected rate) / not_found."""
    try:
        rows = body_json.get("rows", [])
        if not rows or not isinstance(rows, list):
            return 400, "application/json", json.dumps({"error": "rows array required"}).encode(), True
        if len(rows) > 20000:
            return 400, "application/json", json.dumps({"error": "max 20,000 rows"}).encode(), True
        trackings = []
        for r in rows:
            t = str(r.get("tracking", "") or "").strip()
            if t and t not in trackings:
                trackings.append(t)
        db_map = {}
        if trackings:
            conn = get_db()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            sql = _SHIP_RECON_LOOKUP_SQL.replace("%(ncr)s", "(" + ",".join("'%s'" % c for c in _NCR_CITIES) + ")")
            cur.execute(sql, {"trackings": trackings})
            for row in cur.fetchall():
                db_map[row["tracking_number"]] = row
            cur.close()
            conn.close()
        eng = _load_rate_engine()
        results = []
        for r in rows:
            tracking = str(r.get("tracking", "") or "").strip()
            if not tracking:
                continue
            billed = float(r.get("shipping_fee") or 0)
            valuation = float(r.get("valuation_fee") or 0)
            billed_total = round(billed + valuation, 2)
            res = {"tracking": tracking, "billed_fee": billed, "valuation_fee": valuation,
                   "billed_total": billed_total, "date": r.get("date", ""), "match_type": "not_found",
                   "expected_fee": None, "charged_fee": None, "diff": None,
                   "order_id": "", "logistics_status": "", "shipment_status": ""}
            db = db_map.get(tracking)
            if db:
                exp = _expected_fee(eng, db["created_at"], db["origin_region"],
                                    db["dest_region"], db["order_value"])
                charged = float(db["charged_fee"] or 0)
                res.update({"order_id": db["order_id"],
                            "logistics_status": db["logistics_status"] or "",
                            "shipment_status": db["shipment_status"] or "",
                            "expected_fee": exp, "charged_fee": charged})
                if exp is not None:
                    res["diff"] = round(billed_total - exp, 2)
                    res["match_type"] = "matched" if abs(billed_total - exp) < 0.01 else "mismatch"
                else:
                    res["diff"] = round(billed_total - charged, 2)
                    res["match_type"] = "matched" if abs(billed_total - charged) < 0.01 else "mismatch"
            results.append(res)
        return 200, "application/json", json.dumps({"results": results}).encode(), True
    except Exception as e:
        return 500, "application/json", json.dumps({"error": str(e)}).encode(), True


_SHIP_ANCHOR_STATUSES = ('delivered', 'returned', 'failed', 'cancelled', 'in_transit', 'picked_up', 'booked', 'pending', 'lost', 'damaged')


def _normalize_ship_statuses(raw):
    """Normalize executionStatus (str | list). '' / 'ALL' -> all; 'TERMINAL' (legacy) ->
    ['delivered', 'returned', 'lost', 'damaged']; empty/None -> same default."""
    if isinstance(raw, str):
        raw = raw.strip()
        if raw in ('', 'ALL'):
            statuses = list(_SHIP_ANCHOR_STATUSES)
        elif raw == 'TERMINAL':
            statuses = ['delivered', 'returned', 'lost', 'damaged']
        else:
            statuses = [raw]
    elif isinstance(raw, (list, tuple)):
        statuses = [str(s).strip() for s in raw if str(s).strip()]
    else:
        statuses = []
    if not statuses:
        statuses = ['delivered', 'returned', 'lost', 'damaged']
    for s in statuses:
        if s not in _SHIP_ANCHOR_STATUSES:
            return None
    return statuses


def handle_shipping_reconcile_anchor_api(body_json):
    """Ledger-anchored shipping recon: anchor = ALL jt_shipment rows in a date range
    (+ status), from OUR DB. Optional J&T bill CSV (tracking, shipping_fee, valuation_fee)
    is evidence: verdicts matched / amount_mismatch / missing_from_csv; bill trackings with
    no shipment -> not_in_ledger extras. Diff = billed total vs expected fee (rate engine),
    falling back to charged fee when no rate applies."""
    try:
        date_from = str(body_json.get('dateFrom', '') or '').strip()
        date_to = str(body_json.get('dateTo', '') or '').strip()
        statuses = _normalize_ship_statuses(body_json.get('executionStatus', 'TERMINAL'))
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

        params = [date_from, date_to]
        status_clause = ""
        if len(statuses) < len(_SHIP_ANCHOR_STATUSES):
            status_clause = "AND js.status = ANY(%s)"
            params.append(statuses)

        sql = """
            SELECT DISTINCT ON (js.id)
                js.id AS shipment_id,
                COALESCE(fl.tracking_number, '') AS tracking_number,
                COALESCE(oe.order_sn, o.id) AS order_id,
                js.status AS shipment_status,
                js.logistics_status,
                js.created_at,
                (js.created_at AT TIME ZONE 'Asia/Manila')::timestamp AS ship_date,
                COALESCE(osm2.amount, 0) AS charged_fee,
                COALESCE(pp.region_name, pp2.region_name,
                    CASE WHEN UPPER(REPLACE(sla.city, ' City', '')) IN %(ncr)s THEN 'GMA' END) AS origin_region,
                pp_d.region_name AS dest_region,
                COALESCE(oi_val.items_total, 0) + COALESCE(osm2.amount, 0) AS order_value
            FROM public.jt_shipment js
            JOIN public."order" o ON o.id = js.order_id AND o.deleted_at IS NULL
            LEFT JOIN public.order_extension oe ON oe.order_id = o.id
            LEFT JOIN public.fulfillment f ON f.id = js.fulfillment_id AND f.deleted_at IS NULL
            LEFT JOIN public.fulfillment_label fl ON fl.fulfillment_id = f.id AND fl.deleted_at IS NULL
            LEFT JOIN public.stock_location sl ON sl.id = f.location_id AND sl.deleted_at IS NULL
            LEFT JOIN public.stock_location_address sla ON sla.id = sl.address_id AND sla.deleted_at IS NULL
            LEFT JOIN public.ph_province pp ON pp.name = sla.province AND pp.deleted_at IS NULL
            LEFT JOIN public.ph_city_municipality pcm2 ON (
                pcm2.name = sla.city OR pcm2.name ILIKE 'City of ' || sla.city OR pcm2.name ILIKE sla.city || ' City'
            ) AND pcm2.deleted_at IS NULL
            LEFT JOIN public.ph_province pp2 ON pp2.name = pcm2.province_name AND pp2.deleted_at IS NULL
            LEFT JOIN public.fulfillment_address fa ON fa.id = f.delivery_address_id AND fa.deleted_at IS NULL
            LEFT JOIN public.ph_province pp_d ON pp_d.name = fa.province AND pp_d.deleted_at IS NULL
            LEFT JOIN LATERAL (
                SELECT osm2.amount
                FROM public.order_shipping os2
                JOIN public.order_shipping_method osm2 ON osm2.id = os2.shipping_method_id AND osm2.deleted_at IS NULL
                WHERE os2.order_id = o.id AND os2.deleted_at IS NULL
                LIMIT 1
            ) osm2 ON true
            LEFT JOIN LATERAL (
                SELECT COALESCE(SUM(oi_latest.quantity * COALESCE(oi_latest.unit_price, oli_sum.unit_price, 0)), 0) AS items_total
                FROM (
                    SELECT DISTINCT ON (oi_sum.item_id) oi_sum.quantity, oi_sum.unit_price, oi_sum.item_id
                    FROM public.order_item oi_sum
                    WHERE oi_sum.order_id = o.id AND oi_sum.deleted_at IS NULL
                    ORDER BY oi_sum.item_id, oi_sum.version DESC
                ) oi_latest
                LEFT JOIN public.order_line_item oli_sum ON oli_sum.id = oi_latest.item_id AND oli_sum.deleted_at IS NULL
            ) oi_val ON true
            WHERE js.deleted_at IS NULL
              AND (js.created_at AT TIME ZONE 'Asia/Manila')::date BETWEEN %s AND %s
              {status_clause}
            ORDER BY js.id, js.created_at DESC
        """.format(status_clause=status_clause)
        sql = sql.replace("%(ncr)s", "(" + ",".join("'%s'" % c for c in _NCR_CITIES) + ")")

        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        db_rows = cur.fetchall()
        cur.close()
        conn.close()

        # CSV bill evidence: tracking -> billed total
        csv_by_ref = {}
        for r in rows:
            trk = str(r.get('tracking', '') or '').strip()
            if not trk:
                continue
            billed = 0.0
            valuation = 0.0
            try:
                billed = float(r.get('shipping_fee') or 0)
            except (TypeError, ValueError):
                pass
            try:
                valuation = float(r.get('valuation_fee') or 0)
            except (TypeError, ValueError):
                pass
            entry = csv_by_ref.setdefault(trk, {'total': 0.0, 'n': 0})
            entry['total'] += round(billed + valuation, 2)
            entry['n'] += 1

        eng = _load_rate_engine()
        all_db_trackings = set(r['tracking_number'] for r in db_rows if r['tracking_number'])

        out_rows = []
        matched = missing = mismatch = 0
        matched_amt = missing_amt = mismatch_amt = 0.0
        for row in db_rows:
            charged = float(row['charged_fee'] or 0)
            exp = _expected_fee(eng, row['created_at'], row['origin_region'], row['dest_region'], row['order_value'])
            d = {
                'shipment_id': row['shipment_id'],
                'tracking_number': row['tracking_number'] or '',
                'order_id': row['order_id'],
                'ship_date': row['ship_date'].strftime('%Y-%m-%d %H:%M:%S') if row['ship_date'] else '',
                'shipment_status': row['shipment_status'] or '',
                'logistics_status': row['logistics_status'] or '',
                'charged_fee': charged,
                'expected_fee': exp,
                'order_value': round(float(row['order_value'] or 0), 2),
            }
            csv_hit = csv_by_ref.get(row['tracking_number']) if row['tracking_number'] else None
            if csv_hit is None:
                d['verdict'] = 'missing'
                d['csv_amount'] = None
                d['diff'] = None
                missing += 1
                missing_amt += charged
            else:
                csv_total = round(csv_hit['total'], 2)
                d['csv_amount'] = csv_total
                if exp is not None:
                    d['diff'] = round(csv_total - exp, 2)
                else:
                    d['diff'] = round(csv_total - charged, 2)
                if abs(d['diff']) < 0.01:
                    d['verdict'] = 'matched'
                    matched += 1
                    matched_amt += charged
                else:
                    d['verdict'] = 'amount_mismatch'
                    mismatch += 1
                    mismatch_amt += charged
            out_rows.append(d)

        extras = []
        for trk, info in csv_by_ref.items():
            if trk not in all_db_trackings:
                extras.append({'reference': trk, 'csv_amount': round(info['total'], 2), 'csv_count': info['n']})

        anchor_total = len(out_rows)
        stats = {
            'anchor_total': anchor_total,
            'anchor_amount': round(sum(r['charged_fee'] for r in out_rows), 2),
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


_FEE_ENRICH_SQL = """
SELECT
    js.id AS shipment_id,
    js.created_at,
    COALESCE(pp.region_name, pp2.region_name,
        CASE WHEN UPPER(REPLACE(sla.city, ' City', '')) IN %(ncr)s THEN 'GMA' END) AS origin_region,
    pp_d.region_name AS dest_region,
    COALESCE(osm2.amount, 0) AS charged_fee,
    COALESCE(oi_val.items_total, 0) + COALESCE(osm2.amount, 0) AS order_value
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
LEFT JOIN public.ph_province pp ON pp.name = sla.province AND pp.deleted_at IS NULL
LEFT JOIN public.ph_city_municipality pcm2 ON (
    pcm2.name = sla.city OR pcm2.name ILIKE 'City of ' || sla.city OR pcm2.name ILIKE sla.city || ' City'
) AND pcm2.deleted_at IS NULL
LEFT JOIN public.ph_province pp2 ON pp2.name = pcm2.province_name AND pp2.deleted_at IS NULL
LEFT JOIN public.fulfillment_address fa ON fa.id = f.delivery_address_id AND fa.deleted_at IS NULL
LEFT JOIN public.ph_province pp_d ON pp_d.name = fa.province AND pp_d.deleted_at IS NULL
LEFT JOIN LATERAL (
    SELECT fl2.tracking_number
    FROM public.fulfillment_label fl2
    WHERE fl2.fulfillment_id = f.id AND fl2.deleted_at IS NULL
    LIMIT 1
) fl ON true
LEFT JOIN LATERAL (
    SELECT osm2.amount
    FROM public.order_shipping os2
    JOIN public.order_shipping_method osm2 ON osm2.id = os2.shipping_method_id AND osm2.deleted_at IS NULL
    WHERE os2.order_id = o.id AND os2.deleted_at IS NULL
    LIMIT 1
) osm2 ON true
LEFT JOIN LATERAL (
    SELECT COALESCE(SUM(oi_latest.quantity * COALESCE(oi_latest.unit_price, oli_sum.unit_price, 0)), 0) AS items_total
    FROM (
        SELECT DISTINCT ON (oi_sum.item_id) oi_sum.quantity, oi_sum.unit_price, oi_sum.item_id
        FROM public.order_item oi_sum
        WHERE oi_sum.order_id = o.id AND oi_sum.deleted_at IS NULL
        ORDER BY oi_sum.item_id, oi_sum.version DESC
    ) oi_latest
    LEFT JOIN public.order_line_item oli_sum ON oli_sum.id = oi_latest.item_id AND oli_sum.deleted_at IS NULL
) oi_val ON true
WHERE js.deleted_at IS NULL
"""


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
        variance = query_params.get("variance", [""])[0]

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

        # ── Fee enrichment: compute expected J&T fee per shipment (rate engine) ──
        eng = _load_rate_engine()
        fee_map = {}
        enrich_sql = _FEE_ENRICH_SQL.replace("%(ncr)s", "(" + ",".join("'%s'" % c for c in _NCR_CITIES) + ")")
        cur.execute(f"{enrich_sql} AND {extra_where}", params)
        for row in cur.fetchall():
            exp = _expected_fee(eng, row["created_at"], row["origin_region"],
                                row["dest_region"], row["order_value"])
            charged = float(row["charged_fee"] or 0)
            fee_map[row["shipment_id"]] = {
                "expected": exp,
                "charged": charged,
                "variance": None if exp is None else round(charged - exp, 2),
                "flag": _variance_flag(exp, charged),
            }

        # Variance filter (computed in Python after enrichment)
        variance_ids = None
        if variance:
            variance_ids = {sid for sid, m in fee_map.items() if m["flag"] == variance}

        def _fee_stats():
            fs = {"total_expected_fee": 0.0, "total_charged_fee": 0.0, "total_fee_variance": 0.0,
                  "fee_over_count": 0, "fee_under_count": 0, "fee_matched_count": 0, "fee_no_rate_count": 0,
                  "fee_over_pct": 0.0}
            rated = 0
            for m in fee_map.values():
                fs["total_charged_fee"] += m["charged"]
                if m["flag"] == "no_rate":
                    fs["fee_no_rate_count"] += 1
                    continue
                rated += 1
                fs["total_expected_fee"] += m["expected"]
                fs["total_fee_variance"] += m["variance"]
                if m["flag"] == "over":
                    fs["fee_over_count"] += 1
                elif m["flag"] == "under":
                    fs["fee_under_count"] += 1
                else:
                    fs["fee_matched_count"] += 1
            if rated:
                fs["fee_over_pct"] = round(fs["fee_over_count"] / rated * 100, 1)
            fs["total_expected_fee"] = round(fs["total_expected_fee"], 2)
            fs["total_charged_fee"] = round(fs["total_charged_fee"], 2)
            fs["total_fee_variance"] = round(fs["total_fee_variance"], 2)
            return fs

        def _merge_fee(r):
            m = fee_map.get(r.get("shipment_id"), {})
            r["expected_fee"] = m.get("expected")
            r["charged_fee"] = m.get("charged", 0)
            r["fee_variance"] = m.get("variance")
            r["variance_flag"] = m.get("flag", "no_rate")
            return r

        if export_csv:
            data_sql = f"{_BASE_SQL} AND {extra_where} ORDER BY js.created_at DESC LIMIT 5000"
            cur.execute(data_sql, params)
            rows = cur.fetchall()
            if variance_ids:
                rows = [r for r in rows if r["shipment_id"] in variance_ids]
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
            headers = [col_map.get(d[0], d[0]) for d in cur.description] + \
                ["Expected Fee", "Charged Fee", "Variance", "Variance Flag"]
            writer.writerow(headers)
            for r in rows:
                _merge_fee(r)
                vals = []
                for k, v in r.items():
                    if k in ("expected_fee", "charged_fee", "fee_variance", "variance_flag", "shipment_id"):
                        continue
                    if v is None:
                        vals.append("")
                    elif isinstance(v, dict):
                        vals.append(json.dumps(v))
                    else:
                        vals.append(str(v))
                vals += [r.get("expected_fee") if r.get("expected_fee") is not None else "",
                         r.get("charged_fee", ""),
                         r.get("fee_variance") if r.get("fee_variance") is not None else "",
                         r.get("variance_flag", "")]
                writer.writerow(vals)
            return 200, "text/csv", output.getvalue().encode(), True

        # Count
        count_sql = f"SELECT COUNT(*) AS total FROM ({_BASE_SQL} AND {extra_where}) sub"
        cur.execute(count_sql, params)
        total = cur.fetchone()["total"]
        if variance_ids:
            total = len(variance_ids)

        # Data
        data_sql = f"{_BASE_SQL} AND {extra_where} ORDER BY js.created_at DESC LIMIT %s OFFSET %s"
        offset = (page - 1) * page_size
        cur.execute(data_sql, params + [page_size, offset])
        rows = cur.fetchall()
        if variance_ids:
            rows = [r for r in rows if r["shipment_id"] in variance_ids]
            # Re-paginate after variance filter
            rows = rows[(page - 1) * page_size: page * page_size]
        rows = [_merge_fee(r) for r in rows]
        rows = _serialize(rows)

        # Stats
        stats_sql = f"{_STATS_SQL} AND {extra_where}"
        cur.execute(stats_sql, params)
        stats = _serialize([cur.fetchone()])[0]
        stats.update(_fee_stats())

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
  header .nav { display: flex; gap: 8px; align-items: center; }
  header .badge { font-size: 11px; color: var(--accent); }
  .container { max-width: 1900px; margin: 0 auto; padding: 16px 24px; }
  .filters { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 16px; display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; }
  .filter-group { display: flex; flex-direction: column; gap: 4px; }
  .filter-group label { font-size: 11px; color: var(--dim); text-transform: uppercase; letter-spacing: .5px; }
  .filter-group input, .filter-group select { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 8px 12px; border-radius: 6px; font-size: 13px; min-width: 180px; }
  .filter-group input:focus, .filter-group select:focus { outline: none; border-color: var(--accent); }
  .btn { padding: 8px 20px; border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer; border: none; transition: all .15s; }
  .btn-primary { background: var(--accent); color: #fff; border-radius: 999px; } .btn-primary:hover { background: #007A73; }
  .btn-secondary { background: rgba(0,175,160,.08); color: var(--text); } .btn-secondary:hover { background: #E0F5F3; }
  .btn-sm { padding: 4px 10px; font-size: 11px; }
  .stats { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 12px 18px; min-width: 150px; }
  .stat-card .value { font-size: 22px; font-weight: 700; } .stat-card .label { font-size: 11px; color: var(--dim); }
  .green { color: var(--green); } .amber { color: var(--amber); } .red { color: var(--red); }
  .table-wrap { overflow: auto; max-height: 70vh; background: var(--card); border: 1px solid var(--border); border-radius: 12px; }
  table { width: 100%; border-collapse: collapse; }
  th { background: var(--bg); padding: 10px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: .4px; color: var(--dim); text-align: left; white-space: nowrap; position: sticky; top: 0; z-index: 1; }
  td { padding: 8px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  tr:hover td { background: rgba(0,175,160,.05); }
  .status { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; }
  .status-done { background: rgba(0,175,160,.15); color: var(--green); }
  .status-failed { background: rgba(239,68,68,.15); color: var(--red); }
  .status-returned { background: rgba(239,68,68,.15); color: var(--red); }
  .status-lost { background: rgba(239,68,68,.15); color: var(--red); }
  .status-cancelled { background: rgba(239,68,68,.15); color: var(--red); }
  .status-transit { background: rgba(0,175,160,.15); color: var(--accent); }
  .amount { text-align: right; font-variant-numeric: tabular-nums; }
  .loading { text-align: center; padding: 40px; color: var(--dim); }
  .empty { text-align: center; padding: 40px; color: var(--dim); font-size: 14px; }
  .pagination { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-top: 1px solid var(--border); }
  .pagination .info { color: var(--dim); font-size: 12px; }
  .pagination .btns { display: flex; gap: 6px; }
  .error { color: var(--red); padding: 12px; background: rgba(239,68,68,.1); border-radius: 6px; margin-bottom: 12px; }
  code { font-size: 11px; color: var(--accent); }
  .copy-btn { cursor: pointer; font-size: 12px; opacity: 0.5; transition: opacity .15s; user-select: none; }
  .copy-btn:hover { opacity: 1; }
  .address { max-width: 200px; overflow: hidden; text-overflow: ellipsis; }
  .hint { font-size: 10px; color: var(--dim); font-style: italic; }
  .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
  .tab-btn { padding: 8px 18px; border-radius: 8px; font-size: 13px; font-weight: 500; cursor: pointer; border: 1px solid var(--border); background: var(--card); color: var(--dim); transition: all .15s; }
  .tab-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .tab-content { display: none; } .tab-content.active { display: block; }
  .upload-zone { border: 2px dashed var(--border); border-radius: 12px; padding: 40px; text-align: center; cursor: pointer; transition: all .15s; margin-bottom: 16px; background: var(--card); }
  .upload-zone:hover, .upload-zone.dragover { border-color: var(--accent); background: rgba(0,175,160,.05); }
  .upload-zone .upload-icon { font-size: 32px; margin-bottom: 8px; }
  .upload-zone .upload-title { font-size: 14px; font-weight: 500; margin-bottom: 4px; }
  .upload-zone .upload-hint { font-size: 11px; color: var(--dim); }
  .upload-zone input[type=file] { display: none; }
  .preview-box { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 16px; }
  .preview-box h3 { font-size: 14px; margin-bottom: 12px; }
  .mappings { display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 12px; }
  .mapping { font-size: 12px; } .mapping .mfield { color: var(--dim); text-transform: uppercase; font-size: 10px; letter-spacing: .4px; } .mapping .col { font-weight: 600; } .mapping .warn { color: var(--amber); } .mapping .check { color: var(--green); }
  .preview-table-wrap { overflow: auto; max-height: 220px; }
  .match-badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; }
  .match-matched { background: rgba(0,175,160,.15); color: var(--green); }
  .match-mismatch { background: rgba(196,136,10,.12); color: var(--amber); }
  .match-not-found { background: rgba(239,68,68,.15); color: var(--red); }
  .blue { color: var(--accent); }
</style>
</head>
<body>
<header>
  <h1>📦 Shipping Fee Reconciliation</h1>
  <div class="nav"><a href="/recon/" class="btn btn-secondary btn-sm">← Portal Home</a><span class="badge">Production DB</span></div>
</header>
<div class="container">
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('download')" id="tab-download">📋 Download Board</button>
    <button class="tab-btn" onclick="switchTab('reconcile')" id="tab-reconcile">🔄 Reconcile (J&T Bill)</button>
  </div>
  <div class="tab-content active" id="download-tab">
  <div class="filters">
    <div class="filter-group"><label>Carrier</label><select id="carrier"><option value="">All Carriers</option><option value="J&T">J&T Express</option></select></div>
    <div class="filter-group"><label>Logistics Status</label><select id="logisticsStatus"></select></div>
    <div class="filter-group"><label>Fee Variance</label><select id="variance"><option value="">All</option><option value="over">⚠ Overcharged</option><option value="under">✅ Undercharged</option><option value="matched">✓ Matched</option><option value="no_rate">— No Rate</option></select></div>
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
        <th>Shipping Method</th><th class="amount">Est. Ship Fee</th><th class="amount">Actual Ship Fee</th><th class="amount">Expected Fee</th><th class="amount">Variance</th>
        <th>Carrier</th><th>Tracking #</th>
        <th>Logistics Status</th><th>JT Code</th><th>JT Desc</th>
        <th>Bill Code</th><th>TX Logistic ID</th>
        <th>Booked</th><th>Picked Up</th><th>Delivered</th><th>Failed</th><th>Cancelled</th><th>Returned</th>
      </tr></thead>
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
            <label class="chip"><input type="checkbox" value="delivered" checked>Delivered</label>
            <label class="chip"><input type="checkbox" value="returned" checked>Returned</label>
            <label class="chip"><input type="checkbox" value="lost" checked>Lost</label>
            <label class="chip"><input type="checkbox" value="damaged" checked>Damaged</label>
            <label class="chip"><input type="checkbox" value="failed">Failed</label>
            <label class="chip"><input type="checkbox" value="cancelled">Cancelled</label>
            <label class="chip"><input type="checkbox" value="in_transit">In Transit</label>
            <label class="chip"><input type="checkbox" value="picked_up">Picked Up</label>
            <label class="chip"><input type="checkbox" value="booked">Booked</label>
            <label class="chip"><input type="checkbox" value="pending">Pending</label>
            <span onclick="setAnchorStatuses(true)" style="font-size:11px;color:var(--accent);cursor:pointer;text-decoration:underline;">All</span>
            <span onclick="setAnchorStatuses(false)" style="font-size:11px;color:var(--accent);cursor:pointer;text-decoration:underline;margin-left:4px;">None</span>
          </div></div>
        <button class="btn btn-primary" id="runAnchorBtn" onclick="runAnchorRecon()">📒 Run Anchor Recon</button>
      </div>
      <div style="margin-top:10px;font-size:12px;color:var(--dim);line-height:1.6;">
        <b>Anchor</b> = every shipment in <b>our</b> ledger for the date range + status — the completeness basis, not the bill.<br>
        J&T bill upload above is <b>optional evidence</b>: shipments missing from the bill are flagged ❌ (completeness gap), fee differences ⚠️ (billed vs expected rate), bill trackings with no shipment ➕.<br>
        Default = terminal states (delivered/returned/lost/damaged) that should appear on the J&T bill.
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
        <span style="color:var(--dim);font-size:12px;">Fees compare against the J&amp;T rate-engine expected fee (falls back to charged fee). Shipments without a tracking label can't be matched by the bill — they'll show as Missing.</span>
        <div style="margin-top:8px;color:var(--dim);font-size:12px;">Tip: anchor on <b>our</b> data first — a 3rd-party file can be silently incomplete.</div>
      </div>
    </div>
    <div class="upload-zone" id="uploadZone" onclick="document.getElementById('csvUpload').click()">
      <div class="upload-icon">📁</div>
      <div class="upload-title">Upload J&T Monthly Bill CSV</div>
      <div class="upload-hint">Drag & drop or click. Needs: Tracking #, Shipping Fee, Valuation Fee (Date optional). Matches against J&T rate card.</div>
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
      <div class="filter-group"><label>Search</label><input type="text" id="reconSearch" placeholder="Tracking, Order #" oninput="filterReconcileResults()"></div>
      <div class="filter-group"><label>Match Type</label><select id="reconMatchType" onchange="filterReconcileResults()"><option value="">All</option><option value="matched">✅ Matched</option><option value="mismatch">⚠ Fee Mismatch</option><option value="not-found">❌ Not in System</option></select></div>
      <span id="reconFilterCount" style="color:var(--dim);font-size:12px;align-self:flex-end;padding-bottom:8px;"></span>
      <button class="btn btn-secondary btn-sm" onclick="document.getElementById('reconSearch').value='';document.getElementById('reconMatchType').value='';filterReconcileResults();">↺ Clear</button>
    </div>
    <div class="table-wrap" id="reconcileTableWrap" style="display:none">
      <table id="reconcileResults">
        <thead id="reconcileHead"><tr>
          <th>Match</th><th>Tracking #</th><th class="amount">Billed Fee</th><th class="amount">Valuation</th><th class="amount">Billed Total</th><th class="amount">Expected</th><th class="amount">Charged</th><th class="amount">Diff vs Expected</th><th>Bill Date</th><th>Order #</th><th>Logistics</th>
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
var STATUS_LABELS={};
function f(n){return n||'0.00';}
function getFilters(){return{
  date_from:document.getElementById('dateFrom').value,
  date_to:document.getElementById('dateTo').value,
  carrier:document.getElementById('carrier').value,
  logistics_status:document.getElementById('logisticsStatus').value,
  variance:document.getElementById('variance').value,
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
// ─── Reconcile ────────────────────────────────────────────
var csvData=[],csvHeaders=[],colMap={},reconcileResults=[],reconMode='anchor',anchorStats=null;
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
  colMap.tracking=findCol(/tracking|waybill|awb|bill.?no|consignment/i);
  colMap.shipping=findCol(/shipping.*fee|fee.*shipping|shipping.*cost|freight/i);
  colMap.valuation=findCol(/valuation|insurance|declared/i);
  colMap.date=findCol(/date|delivered|returned/i);
  var mapHtml='';
  var flds=[{k:'tracking',l:'Tracking #'},{k:'shipping',l:'Shipping Fee'},{k:'valuation',l:'Valuation Fee'},{k:'date',l:'Date'}];
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
  var trkCol=colMap.tracking;
  if(!trkCol){alert('No Tracking column found in CSV');return;}
  var btn=document.getElementById('runReconcileBtn');
  btn.disabled=true;btn.textContent='⏳ Matching...';
  document.getElementById('reconcile-status').style.display='none';
  var shpCol=colMap.shipping,valCol=colMap.valuation,dtCol=colMap.date;
  var rows=csvData.map(function(r){
    return {tracking:String(r[trkCol]||'').trim(),
            shipping_fee:shpCol?parseFloat(String(r[shpCol]).replace(/[^0-9.\-]/g,''))||0:0,
            valuation_fee:valCol?parseFloat(String(r[valCol]).replace(/[^0-9.\-]/g,''))||0:0,
            date:dtCol?(r[dtCol]||''):''};
  }).filter(function(r){return r.tracking!=='';});
  fetch('/recon/shipping/api/reconcile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rows:rows})})
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
  var payload={dateFrom:df,dateTo:dt,executionStatus:getAnchorStatuses(['delivered','returned','lost','damaged']),rows:[]};
  if(csvData.length&&colMap.tracking){
    var shpCol=colMap.shipping,valCol=colMap.valuation;
    payload.rows=csvData.map(function(r){return {tracking:String(r[colMap.tracking]||'').trim(),shipping_fee:shpCol?(parseFloat(String(r[shpCol]).replace(/[^0-9.\-]/g,''))||0):0,valuation_fee:valCol?(parseFloat(String(r[valCol]).replace(/[^0-9.\-]/g,''))||0):0};}).filter(function(r){return r.tracking!=='';});
  }
  fetch('/recon/shipping/api/reconcile-anchor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
  .then(function(r){return r.json();})
  .then(function(d){
    btn.disabled=false;btn.textContent='📒 Run Anchor Recon';
    if(d.error){document.getElementById('reconcile-status').innerHTML='<div class="error">'+esc(d.error)+'</div>';document.getElementById('reconcile-status').style.display='block';return;}
    anchorStats=d.stats||null;
    reconcileResults=(d.rows||[]).map(function(x){return {match_type:x.verdict,tracking:x.tracking_number||x.shipment_id||'',order_id:x.order_id||'',date:x.ship_date||'',expected_fee:x.expected_fee,charged_fee:x.charged_fee||0,csv_amount:x.csv_amount==null?null:x.csv_amount,diff:x.diff==null?null:x.diff,shipment_status:x.shipment_status||'',logistics_status:x.logistics_status||'',billed_total:x.csv_amount==null?0:x.csv_amount};})
      .concat((d.extras||[]).map(function(x){return {match_type:'not_in_ledger',tracking:x.reference||'',order_id:'',date:'',expected_fee:null,charged_fee:0,csv_amount:x.csv_amount||0,diff:null,shipment_status:'',logistics_status:'',billed_total:x.csv_amount||0};}));
    document.getElementById('reconcileFilters').style.display='flex';
    filterReconcileResults();
  })
  .catch(function(e){btn.disabled=false;btn.textContent='📒 Run Anchor Recon';document.getElementById('reconcile-status').innerHTML='<div class="error">Error: '+esc(e.message)+'</div>';document.getElementById('reconcile-status').style.display='block';});
}
function filterReconcileResults(){
  var opts=reconMode==='anchor'
    ?[['','All'],['matched','✅ Matched'],['amount_mismatch','⚠ Fee Mismatch'],['missing','❌ Missing from Bill'],['not_in_ledger','➕ Bill Not in Ledger']]
    :[['','All'],['matched','✅ Matched'],['mismatch','⚠ Fee Mismatch'],['not_found','❌ Not in System']];
  var sel=document.getElementById('reconMatchType');
  var cur=sel.value;
  sel.innerHTML=opts.map(function(o){return'<option value="'+o[0]+'">'+o[1]+'</option>';}).join('');
  if(opts.some(function(o){return o[0]===cur;}))sel.value=cur;else sel.value='';
  var q=document.getElementById('reconSearch').value.toLowerCase();
  var mt=sel.value;
  var shown=reconcileResults.filter(function(r){
    if(mt&&r.match_type!==mt)return false;
    if(q&&!(String(r.tracking).toLowerCase().indexOf(q)>=0||String(r.order_id||'').toLowerCase().indexOf(q)>=0))return false;
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
    var csvTxt=s.csv_evidence?'':' <span style="font-size:11px;color:var(--dim)">(no bill uploaded)</span>';
    var pctColor=s.completeness_pct>=100?'green':(s.completeness_pct>=90?'amber':'red');
    document.getElementById('reconcileStats').innerHTML=
      '<div class="stat-card"><div class="value">'+s.anchor_total+'</div><div class="label">Anchor Shipments</div></div>'+
      '<div class="stat-card"><div class="value green">'+s.matched+'</div><div class="label">✅ Matched</div></div>'+
      '<div class="stat-card"><div class="value red">'+s.missing+'</div><div class="label">❌ Missing from Bill (₱'+fmtNum(s.missing_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value amber">'+s.mismatch+'</div><div class="label">⚠ Fee Mismatch (₱'+fmtNum(s.mismatch_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value blue">'+s.extras+'</div><div class="label">➕ Bill Not in Ledger (₱'+fmtNum(s.extras_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value '+pctColor+'">'+s.completeness_pct+'%</div><div class="label">Completeness'+csvTxt+'</div></div>'+
      '<div class="stat-card"><div class="value">₱'+fmtNum(s.anchor_amount)+'</div><div class="label">Anchor Charged Fees</div></div>';
    return;
  }
  var matched=r.filter(function(x){return x.match_type==='matched';}).length;
  var mismatch=r.filter(function(x){return x.match_type==='mismatch';}).length;
  var notFound=r.filter(function(x){return x.match_type==='not_found';}).length;
  var tBilled=r.reduce(function(s,x){return s+x.billed_total;},0);
  var tExpected=r.reduce(function(s,x){return s+(x.expected_fee||0);},0);
  var tDiff=r.reduce(function(s,x){return s+(x.diff||0);},0);
  document.getElementById('reconcileStats').innerHTML=
    '<div class="stat-card"><div class="value">'+r.length+'</div><div class="label">CSV Rows</div></div>'+
    '<div class="stat-card"><div class="value green">'+matched+'</div><div class="label">✅ Matched</div></div>'+
    '<div class="stat-card"><div class="value amber">'+mismatch+'</div><div class="label">⚠ Fee Mismatch</div></div>'+
    '<div class="stat-card"><div class="value red">'+notFound+'</div><div class="label">❌ Not in System</div></div>'+
    '<div class="stat-card"><div class="value blue">₱'+fmtNum(tBilled)+'</div><div class="label">Total Billed</div></div>'+
    '<div class="stat-card"><div class="value">₱'+fmtNum(tExpected)+'</div><div class="label">Total Expected</div></div>'+
    '<div class="stat-card"><div class="value '+(tDiff>=0?'red':'green')+'">'+(tDiff>=0?'+':'')+'₱'+fmtNum(tDiff)+'</div><div class="label">Billed vs Expected</div></div>';
}
function renderReconcileTable(results){
  var tb=document.getElementById('reconcileTbody');
  var head=document.getElementById('reconcileHead');
  if(reconMode==='anchor'){
    head.innerHTML='<tr><th>Match</th><th>Tracking #</th><th>Order #</th><th>Ship Date</th><th class="amount">Expected</th><th class="amount">Charged</th><th class="amount">Bill Amt</th><th class="amount">Diff</th><th>Logistics</th><th>Status</th></tr>';
    if(results.length===0){tb.innerHTML='<tr><td colspan="10" class="empty">No results</td></tr>';return;}
    tb.innerHTML=results.map(function(r){
      var badge=r.match_type==='matched'?'<span class="match-badge match-matched">✅ Matched</span>'
        :r.match_type==='amount_mismatch'?'<span class="match-badge match-mismatch">⚠ Mismatch</span>'
        :r.match_type==='missing'?'<span class="match-badge match-not-found">❌ Missing from Bill</span>'
        :'<span class="match-badge match-escrow">➕ Not in Ledger</span>';
      var diffColor=Math.abs(r.diff||0)<0.01?'var(--green)':'var(--red)';
      return'<tr><td>'+badge+'</td><td><code>'+esc(r.tracking||'—')+'</code></td><td><code>'+esc(r.order_id||'—')+'</code></td>'+
        '<td>'+esc(r.date||'—')+'</td><td class="amount">'+(r.expected_fee==null?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.expected_fee))+'</td>'+
        '<td class="amount">₱'+fmtNum(r.charged_fee||0)+'</td>'+
        '<td class="amount">'+(r.csv_amount==null?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.csv_amount))+'</td>'+
        '<td class="amount" style="color:'+diffColor+'">'+(r.diff==null?'—':((r.diff>=0?'+':'')+fmtNum(r.diff)))+'</td>'+
        '<td>'+esc(r.logistics_status||'—')+'</td><td>'+esc(r.shipment_status||'—')+'</td></tr>';
    }).join('');
    return;
  }
  head.innerHTML='<tr><th>Match</th><th>Tracking #</th><th class="amount">Billed Fee</th><th class="amount">Valuation</th><th class="amount">Billed Total</th><th class="amount">Expected</th><th class="amount">Charged</th><th class="amount">Diff vs Expected</th><th>Bill Date</th><th>Order #</th><th>Logistics</th></tr>';
  if(results.length===0){tb.innerHTML='<tr><td colspan="11" class="empty">No results</td></tr>';return;}
  tb.innerHTML=results.map(function(r){
    var badge='';
    if(r.match_type==='matched')badge='<span class="match-badge match-matched">✅ Matched</span>';
    else if(r.match_type==='mismatch')badge='<span class="match-badge match-mismatch">⚠ Mismatch</span>';
    else badge='<span class="match-badge match-not-found">❌ Not Found</span>';
    var diffColor=Math.abs(r.diff||0)<0.01?'var(--green)':'var(--red)';
    return'<tr><td>'+badge+'</td><td><code>'+esc(r.tracking)+'</code></td>'+
      '<td class="amount">₱'+fmtNum(r.billed_fee)+'</td><td class="amount">₱'+fmtNum(r.valuation_fee)+'</td><td class="amount"><b>₱'+fmtNum(r.billed_total)+'</b></td>'+
      '<td class="amount">'+(r.expected_fee==null?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.expected_fee))+'</td>'+
      '<td class="amount">₱'+fmtNum(r.charged_fee||0)+'</td>'+
      '<td class="amount" style="color:'+diffColor+'">'+(r.diff==null?'—':((r.diff>=0?'+':'')+fmtNum(r.diff)))+'</td>'+
      '<td>'+esc(r.date||'—')+'</td><td><code>'+esc(r.order_id||'—')+'</code></td>'+
      '<td>'+esc(r.logistics_status||'—')+'</td></tr>';
  }).join('');
}
function exportReconcileCSV(){
  if(reconMode==='anchor'){
    var rows=[['Match','Tracking #','Order #','Ship Date','Expected','Charged','Bill Amount','Diff','Logistics','Status']];
    reconcileResults.forEach(function(r){rows.push([r.match_type,r.tracking||'',r.order_id||'',r.date||'',r.expected_fee==null?'':r.expected_fee,r.charged_fee||0,r.csv_amount==null?'':r.csv_amount,r.diff==null?'':r.diff,r.logistics_status||'',r.shipment_status||'']);});
    var csv=rows.map(function(r){return r.map(function(c){return'"'+String(c==null?'':c).replace(/"/g,'""')+'"';}).join(',');}).join('\n');
    var a=document.createElement('a');a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);a.download='shipping-anchor-recon.csv';a.click();
    return;
  }
  var rows=[['Match','Tracking #','Billed Fee','Valuation','Billed Total','Expected','Charged','Diff','Date','Order #','Logistics']];
  reconcileResults.forEach(function(r){rows.push([r.match_type,r.tracking,r.billed_fee,r.valuation_fee,r.billed_total,r.expected_fee==null?'':r.expected_fee,r.charged_fee,r.diff==null?'':r.diff,r.date||'',r.order_id||'',r.logistics_status||'']);});
  var csv=rows.map(function(r){return r.map(function(c){return'"'+String(c==null?'':c).replace(/"/g,'""')+'"';}).join(',');}).join('\n');
  var a=document.createElement('a');a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);a.download='shipping-reconcile-report.csv';a.click();
}
function loadStatuses(){
  fetch('/recon/shipping/api/orders?page=1').then(function(r){return r.json();}).then(function(d){
    if(d.statuses){var sel=document.getElementById('logisticsStatus');d.statuses.forEach(function(s){STATUS_LABELS[s.value]=s.label;var o=document.createElement('option');o.value=s.value;o.textContent=s.label;sel.appendChild(o);});}
  }).catch(function(){});
  setTimeout(function(){switchReconMode('anchor');var today=getTodayDate();document.getElementById('dateFrom').value=today;document.getElementById('dateTo').value=today;loadData();},150);
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
  '<div class="stat-card"><div class="value">₱'+fmtNum(s.total_expected_fee||0)+'</div><div class="label">Expected Fees (rated)</div></div>'+
  '<div class="stat-card"><div class="value '+( (s.total_fee_variance||0)>=0?'red':'green')+'">'+((s.total_fee_variance||0)>0?'+':'')+'₱'+fmtNum(s.total_fee_variance||0)+'</div><div class="label">Charged vs Expected</div></div>'+
  '<div class="stat-card"><div class="value red">'+(s.fee_over_count||0)+'</div><div class="label">Overcharged ('+(s.fee_over_pct||0)+'%)</div></div>'+
  '<div class="stat-card"><div class="value green">'+(s.fee_under_count||0)+'</div><div class="label">Undercharged</div></div>'+
  '<div class="stat-card"><div class="value">'+(s.fee_matched_count||0)+'</div><div class="label">Matched</div></div>'+
  '<div class="stat-card"><div class="value" style="color:var(--dim)">'+(s.fee_no_rate_count||0)+'</div><div class="label">No Rate</div></div>'+
  '<div class="stat-card"><div class="value green">'+(s.delivered_count||0)+'</div><div class="label">Delivered</div></div>'+
  '<div class="stat-card"><div class="value amber">'+(s.in_transit_count||0)+'</div><div class="label">In Transit</div></div>'+
  '<div class="stat-card"><div class="value red">'+(s.failed_return_count||0)+'</div><div class="label">Failed / Returned</div></div>';
}
function varianceBadge(r){
  var f=r.variance_flag;
  if(f==='no_rate')return'<span class="status" style="background:rgba(139,143,163,.15);color:var(--dim)">No rate</span>';
  if(f==='over')return'<span class="status" style="background:rgba(255,71,87,.15);color:var(--red)">+₱'+fmtNum(r.fee_variance)+'</span>';
  if(f==='under')return'<span class="status" style="background:rgba(60,205,92,.15);color:var(--green)">−₱'+fmtNum(Math.abs(r.fee_variance))+'</span>';
  return'<span class="status" style="background:rgba(108,140,255,.15);color:var(--accent)">✓</span>';
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
      '<td class="amount">'+(r.variance_flag==='no_rate'?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.expected_fee))+'</td><td class="amount">'+varianceBadge(r)+'</td>'+
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
function resetFilters(){document.getElementById('dateFrom').value='';document.getElementById('dateTo').value='';document.getElementById('carrier').value='';document.getElementById('logisticsStatus').value='';document.getElementById('variance').value='';document.getElementById('search').value='';document.getElementById('dateHint').textContent='(by created date)';currentPage=1;loadData();}
function exportCSV(){var p=new URLSearchParams(getFilters());p.delete('page');p.delete('page_size');p.set('export','csv');window.open('/recon/shipping/api/orders?'+p,'_blank');}
function fmtNum(n){if(n===null||n===undefined)return'0.00';return Number(n).toLocaleString('en-PH',{minimumFractionDigits:2,maximumFractionDigits:2});}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function copyToClipboard(el){var text=el.getAttribute('data-copy');navigator.clipboard.writeText(text).then(function(){el.textContent='✅';setTimeout(function(){el.textContent='📋';},1500);}).catch(function(){prompt('Copy:',text);});}
loadStatuses();
</script>
</body>
</html>"""
