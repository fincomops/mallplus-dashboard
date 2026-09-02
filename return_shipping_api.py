"""Return Shipping Fee Reconciliation — MallPlus Recon Portal.

Return-journey shipping fee recon (buyer-initiated returns, NOT failed-delivery
returns which belong to the forward journey / jt_shipment recon).

Ledger = reverse_logistics_shipment (return legs, 100% linked to an
order_return_request_case). Fee policy (Shaun, Sep 2, 2026): return shipping is
ALWAYS borne by the seller — charged via wallet_adjustment reason_code
RETURN_SHIPPING_FEE (negative seller debit) when the return leg completes.
Lost/damaged legs are NOT charged (claims territory). Cancelled legs are
downloadable but not chargeable. Platform-absorbed cases are manual exceptions.

Rate engine: billing_rule "Reverse rule" (flat first-kilo fees per zone pair,
insurance ignored for v1 — debit = recorded fee exactly in observed data).

Board + CSV export + Reconcile tab (Ledger Anchor primary / CSV-Based) mirror
the other recon tools.
"""
import json, csv, io
import psycopg2.extras
from recon_db import get_db

# ═══════════════════════════════════════════════════════════════════════════
# Reverse-rule rate engine (return legs)
# Rule brl_01KVPMVT0K4Z4JEWW18A6TXZNE — flat first-kilo fee per (origin,dest)
# zone pair, zones = ph_province.region_name (GMA / Luzon 1-4 / Visayas 1-3 /
# Mindanao 1-2). One card, unambiguous fees per pair (verified Sep 2, 2026).
# ═══════════════════════════════════════════════════════════════════════════
_REVERSE_RULE_ID = "brl_01KVPMVT0K4Z4JEWW18A6TXZNE"
_REVERSE_CARD_ID = "brc_01KVPND7W1390GRWKE6AT0ENKQ"

_REVERSE_ENGINE = None


def _load_reverse_engine():
    """Load the J&T reverse rate card: {(oreg, dreg): first_kilo_base_fee}."""
    global _REVERSE_ENGINE
    if _REVERSE_ENGINE is not None:
        return _REVERSE_ENGINE
    eng = {"effective_at": None, "pairs": {}}
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT effective_start_at FROM billing_rate_card "
            "WHERE id=%s AND deleted_at IS NULL", (_REVERSE_CARD_ID,))
        card = cur.fetchone()
        if card:
            eng["effective_at"] = card["effective_start_at"]
        cur.execute(
            "SELECT id, origin_region, destination_region FROM billing_route "
            "WHERE billing_rule_id=%s", (_REVERSE_RULE_ID,))
        routes = {r["id"]: (r["origin_region"], r["destination_region"]) for r in cur.fetchall()}
        cur.execute(
            "SELECT route_id, weight_min, base_fee FROM billing_rate_line "
            "WHERE deleted_at IS NULL AND rate_card_id=%s AND fee_mode='flat'",
            (_REVERSE_CARD_ID,))
        for l in cur.fetchall():
            rp = routes.get(l["route_id"])
            if rp and l["weight_min"] == 0 and rp not in eng["pairs"]:
                eng["pairs"][rp] = float(l["base_fee"])
        conn.close()
    except Exception:
        pass
    _REVERSE_ENGINE = eng
    return eng


def _expected_fee(eng, created_at, oreg, dreg):
    """Expected reverse-logistics fee for a return leg. None when no rate applies."""
    try:
        if eng["effective_at"] is not None and created_at:
            ts = created_at
            if ts.tzinfo is None:
                from datetime import timezone
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < eng["effective_at"]:
                return None  # leg booked before reverse rate card effective date
        if not oreg or not dreg:
            return None
        base = eng["pairs"].get((oreg, dreg))
        if base is None:
            return None
        return round(base, 2)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# State labels (logistics_state on reverse_logistics_shipment)
# ═══════════════════════════════════════════════════════════════════════════
_STATE_LABELS = [
    ("", "All States"),
    ("LOGISTICS_NOT_STARTED", "Not Started"),
    ("LOGISTICS_REQUEST_CREATED", "Request Created"),
    ("LOGISTICS_REQUEST_PENDING", "Request Pending"),
    ("LOGISTICS_READY", "Ready"),
    ("LOGISTICS_PICKUP_FAILED", "Pickup Failed"),
    ("LOGISTICS_PICKUP_DONE", "Pickup Done"),
    ("LOGISTICS_DELIVERY_DONE", "Delivered Back"),
    ("LOGISTICS_DELIVERY_FAILED", "Delivery Failed"),
    ("LOGISTICS_LOST", "Lost"),
    ("LOGISTICS_REQUEST_CANCELLED", "Cancelled"),
]
_ALL_STATES = [k for k, _ in _STATE_LABELS if k]
_STATE_LABEL = {k: v for k, v in _STATE_LABELS if k}
# Anchor default = the chargeable/in-flight set (cancelled / not-started /
# lost are opt-in — they are downloadable but not charge events).
_DEFAULT_ANCHOR_STATES = ["LOGISTICS_PICKUP_DONE", "LOGISTICS_DELIVERY_DONE",
                          "LOGISTICS_DELIVERY_FAILED"]

_STATE_EXPR = """
CASE rs.logistics_state
    WHEN 'LOGISTICS_NOT_STARTED' THEN 'Not Started'
    WHEN 'LOGISTICS_REQUEST_CREATED' THEN 'Request Created'
    WHEN 'LOGISTICS_REQUEST_PENDING' THEN 'Request Pending'
    WHEN 'LOGISTICS_READY' THEN 'Ready'
    WHEN 'LOGISTICS_PICKUP_FAILED' THEN 'Pickup Failed'
    WHEN 'LOGISTICS_PICKUP_DONE' THEN 'Pickup Done'
    WHEN 'LOGISTICS_DELIVERY_DONE' THEN 'Delivered Back'
    WHEN 'LOGISTICS_DELIVERY_FAILED' THEN 'Delivery Failed'
    WHEN 'LOGISTICS_LOST' THEN 'Lost'
    WHEN 'LOGISTICS_REQUEST_CANCELLED' THEN 'Cancelled'
    ELSE COALESCE(rs.logistics_state, '—')
END
"""

_FEE_STATUS_EXPR = """
CASE LOWER(COALESCE(rs.fee_payment_status, ''))
    WHEN 'charged' THEN 'charged'
    WHEN 'pending' THEN 'pending'
    ELSE 'n/a'
END
"""

# Return leg = pickup at the BUYER's address; delivery back to the SELLER.
_BASE_SQL = f"""
SELECT
    rs.id AS leg_id,
    COALESCE(oe.order_sn, o.id) AS order_id,
    COALESCE(cc.short_id, cc.id) AS request_ref,
    COALESCE(cc.case_number::text, '—') AS case_number,
    CASE WHEN TRIM(COALESCE(oa.first_name,'') || ' ' || COALESCE(oa.last_name,'')) = ''
         THEN 'Unknown' ELSE TRIM(COALESCE(oa.first_name,'') || ' ' || COALESCE(oa.last_name,'')) END AS buyer,
    COALESCE(s.name, 'Unknown') AS seller,
    COALESCE(rs.tracking_number, '—') AS tracking_number,
    {_STATE_EXPR} AS state,
    {_FEE_STATUS_EXPR} AS fee_status,
    rs.pickup_done_at AT TIME ZONE 'Asia/Manila' AS pickup_at,
    rs.delivery_done_at AT TIME ZONE 'Asia/Manila' AS delivered_at,
    rs.created_at AT TIME ZONE 'Asia/Manila' AS created_at,
    COALESCE(rs.shipping_fee_amount, 0) AS recorded_fee,
    COALESCE(wadj.amount, 0) AS debit_amount,
    COALESCE(wadj.batch_id, '') AS debit_batch,
    COALESCE(cc.solution_type, '—') AS solution_type,
    COALESCE(cc.workflow_state, '—') AS workflow_state,
    COALESCE(cc.final_refund_amount, 0) AS final_refund_amount,
    COALESCE(pp_o.region_name, NULLIF(rs.pickup_address_snapshot->>'region',''), '') AS origin_region,
    COALESCE(NULLIF(rs.return_destination_snapshot->>'region',''), pp_d.region_name, '') AS dest_region,
    rs.order_id AS raw_order_id,
    rs.created_at AS raw_created_at
FROM public.reverse_logistics_shipment rs
JOIN public."order" o ON o.id = rs.order_id AND o.deleted_at IS NULL
LEFT JOIN public.order_extension oe ON oe.order_id = o.id
LEFT JOIN public.order_return_request_case cc ON cc.id = rs.order_return_request_case_id
LEFT JOIN public.seller s ON s.id = rs.seller_id AND s.deleted_at IS NULL
LEFT JOIN public.order_address oa ON oa.id = o.shipping_address_id AND oa.deleted_at IS NULL
-- pickup origin (buyer) region: snapshot city first, else order address
LEFT JOIN public.ph_city_municipality pcm_o ON pcm_o.name = COALESCE(NULLIF(rs.pickup_address_snapshot->>'city',''), oa.city) AND pcm_o.deleted_at IS NULL
LEFT JOIN public.ph_province pp_o ON pp_o.name = COALESCE(pcm_o.province_name, NULLIF(rs.pickup_address_snapshot->>'province',''), oa.province) AND pp_o.deleted_at IS NULL
-- return destination (seller) region: snapshot region first, else seller city
LEFT JOIN public.ph_city_municipality pcm_d ON pcm_d.name = s.city AND pcm_d.deleted_at IS NULL
LEFT JOIN public.ph_province pp_d ON pp_d.name = COALESCE(pcm_d.province_name, s.state) AND pp_d.deleted_at IS NULL
-- fee charge = seller wallet debit (reason RETURN_SHIPPING_FEE), linked by reference
LEFT JOIN public.wallet_adjustment wadj ON wadj.id = rs.fee_wallet_adjustment_id AND wadj.deleted_at IS NULL
WHERE rs.deleted_at IS NULL
"""

_CSV_COL_MAP = {
    "leg_id": "Leg ID", "request_ref": "Return Request", "case_number": "Case #",
    "order_id": "Order #", "buyer": "Buyer", "seller": "Seller",
    "tracking_number": "Tracking #", "state": "Leg State", "fee_status": "Fee Status",
    "pickup_at": "Pickup At", "delivered_at": "Delivered Back At", "created_at": "Created At",
    "recorded_fee": "Recorded Fee", "expected_fee": "Est Fee (rated)",
    "debit_amount": "Seller Debit", "debit_batch": "Debit Batch",
    "solution_type": "Solution Type", "workflow_state": "Case Workflow",
    "final_refund_amount": "Final Refund Amt",
    "origin_region": "Origin Zone", "dest_region": "Dest Zone",
}


def _build_where(filters):
    conditions = ["rs.deleted_at IS NULL"]
    params = []

    date_from = filters.get("dateFrom", "")
    date_to = filters.get("dateTo", "")
    state = filters.get("state", "")
    fee_status = filters.get("feeStatus", "")
    search = filters.get("search", "")

    if date_from:
        conditions.append("(rs.created_at AT TIME ZONE 'Asia/Manila')::date >= %s")
        params.append(date_from)
    if date_to:
        conditions.append("(rs.created_at AT TIME ZONE 'Asia/Manila')::date <= %s")
        params.append(date_to)
    if state:
        conditions.append("rs.logistics_state = %s")
        params.append(state)
    if fee_status:
        conditions.append(f"({_FEE_STATUS_EXPR}) = %s")
        params.append(fee_status)
    if search:
        conditions.append("""
            (LOWER(COALESCE(oe.order_sn, o.id)) LIKE LOWER(%s)
             OR LOWER(COALESCE(s.name, '')) LIKE LOWER(%s)
             OR LOWER(COALESCE(rs.tracking_number, '')) LIKE LOWER(%s)
             OR LOWER(COALESCE(cc.short_id, '')) LIKE LOWER(%s)
             OR LOWER(COALESCE(oa.first_name, '') || ' ' || COALESCE(oa.last_name, '')) LIKE LOWER(%s))
        """)
        st = f"%{search}%"
        params.extend([st, st, st, st, st])

    return " AND ".join(conditions), params


def _enrich_expected(rows):
    """Attach expected_fee + variance to rows via the reverse rate engine."""
    eng = _load_reverse_engine()
    for r in rows:
        exp = _expected_fee(eng, r.get("raw_created_at"), r.get("origin_region"),
                            r.get("dest_region"))
        r["expected_fee"] = exp if exp is not None else None
    return rows


def _fetch_rows(query_dict):
    page = int(query_dict.get("page", ["1"])[0])
    page_size = int(query_dict.get("page_size", ["50"])[0])
    export_csv = query_dict.get("export", [""])[0] == "csv"

    filters = {
        "dateFrom": query_dict.get("dateFrom", [""])[0],
        "dateTo": query_dict.get("dateTo", [""])[0],
        "state": query_dict.get("state", [""])[0],
        "feeStatus": query_dict.get("feeStatus", [""])[0],
        "search": query_dict.get("search", [""])[0],
    }
    where_clause, params = _build_where(filters)

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if export_csv:
        sql = f"{_BASE_SQL} AND {where_clause} ORDER BY rs.created_at DESC LIMIT 5000"
        cur.execute(sql, params)
        rows = _enrich_expected(cur.fetchall())
        conn.close()
        return rows, None, None

    count_sql = f"SELECT COUNT(*) AS total FROM ({_BASE_SQL} AND {where_clause}) sub"
    cur.execute(count_sql, params)
    total = cur.fetchone()["total"]

    data_sql = f"{_BASE_SQL} AND {where_clause} ORDER BY rs.created_at DESC LIMIT %s OFFSET %s"
    cur.execute(data_sql, params + [page_size, (page - 1) * page_size])
    rows = _enrich_expected(cur.fetchall())

    stats_sql = f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE state = 'Pickup Done') AS pickup_done,
               COUNT(*) FILTER (WHERE state = 'Delivered Back') AS delivered,
               COUNT(*) FILTER (WHERE state = 'Lost') AS lost,
               COUNT(*) FILTER (WHERE fee_status = 'charged') AS charged,
               COUNT(*) FILTER (WHERE fee_status = 'pending') AS pending,
               COALESCE(SUM(COALESCE(recorded_fee, 0)), 0) AS recorded_total,
               COALESCE(SUM(CASE WHEN fee_status = 'charged' THEN COALESCE(ABS(debit_amount), 0) ELSE 0 END), 0) AS debited_total,
               COUNT(*) FILTER (WHERE fee_status = 'charged') AS charged_count
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
    """Convert datetime/Decimal to JSON-safe values (mirrors sibling modules)."""
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


def handle_return_shipping_api(path, query_dict):
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


def _fee_baseline(row):
    """Our money-side fee for a leg: recorded fee, else abs(seller debit)."""
    rec = row.get("recorded_fee")
    if rec:
        return float(rec)
    deb = row.get("debit_amount")
    if deb:
        return float(abs(deb))
    return None


def _match_db_index(cur, refs):
    """Build tracking -> leg index (recorded fee + debit) for CSV reconcile."""
    cur.execute("""
        SELECT rs.id AS leg_id, rs.tracking_number,
               COALESCE(rs.shipping_fee_amount, 0) AS recorded_fee,
               COALESCE(wadj.amount, 0) AS debit_amount,
               {fee_expr} AS fee_status
        FROM public.reverse_logistics_shipment rs
        LEFT JOIN public.wallet_adjustment wadj ON wadj.id = rs.fee_wallet_adjustment_id AND wadj.deleted_at IS NULL
        WHERE rs.deleted_at IS NULL AND rs.tracking_number = ANY(%s)
    """.format(fee_expr=_FEE_STATUS_EXPR), (refs,))
    idx = {}
    for row in cur.fetchall():
        trk = row.get("tracking_number")
        if trk:
            idx.setdefault(str(trk), row)
    return idx


def handle_return_shipping_reconcile_api(body_json):
    """CSV-based recon: match an uploaded J&T return-leg bill CSV (tracking #,
    shipping fee) against our return-leg ledger. Verdicts: matched /
    amount_mismatch / not_found."""
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
            db_index = _match_db_index(cur, refs)
            cur.close()
            conn.close()

        results = []
        for r in rows:
            ref = str(r.get("reference", "") or "").strip()
            if not ref:
                continue
            raw_amt = r.get("amount")
            csv_amt = float(raw_amt) if raw_amt not in (None, "") else None
            db = db_index.get(ref)
            if not db:
                results.append({"reference": ref, "csv_amount": csv_amt, "db_amount": None,
                                "diff": None, "date": r.get("date", ""),
                                "csv_status": r.get("status", ""), "db_status": "",
                                "leg_id": "", "fee_status": "", "tracking": ref,
                                "match_type": "not_found"})
                continue
            db_amt = _fee_baseline(db)
            if db_amt is not None and csv_amt is not None and abs(csv_amt - db_amt) > 0.009:
                verdict = "amount_mismatch"
            else:
                verdict = "matched"
            results.append({"reference": ref, "csv_amount": csv_amt, "db_amount": db_amt,
                            "diff": (round(csv_amt - db_amt, 2)
                                     if csv_amt is not None and db_amt is not None else None),
                            "date": r.get("date", ""), "csv_status": r.get("status", ""),
                            "db_status": db.get("fee_status") or "",
                            "leg_id": db.get("leg_id") or "",
                            "fee_status": db.get("fee_status") or "",
                            "tracking": ref, "match_type": verdict})

        total = len(results)
        matched = sum(1 for x in results if x["match_type"] == "matched")
        amt = sum(1 for x in results if x["match_type"] == "amount_mismatch")
        nf = sum(1 for x in results if x["match_type"] == "not_found")
        csv_total = sum(x["csv_amount"] or 0 for x in results)
        db_total = sum(x["db_amount"] or 0 for x in results)
        return 200, "application/json", json.dumps({
            "results": results,
            "stats": {"total": total, "matched": matched, "amount_mismatch": amt,
                       "not_found": nf,
                       "completeness": round(matched * 100.0 / total, 1) if total else 0,
                       "csv_amount_total": round(csv_total, 2),
                       "db_amount_total": round(db_total, 2)},
        }).encode(), True
    except Exception as e:
        return 500, "application/json", json.dumps({"error": str(e)}).encode(), True


def handle_return_shipping_reconcile_anchor_api(body_json):
    """Ledger-anchored return recon: anchor = EVERY return leg in our ledger for
    the date range + leg-state set (default: pickup/delivery done + delivery
    failed). Optional J&T return bill CSV (tracking + fee) is evidence:
    verdicts matched / amount_mismatch / missing_from_csv; CSV refs with no leg
    -> not_in_ledger extras. Money-side: delivered-but-not-debited legs are
    counted as a charge gap (seller recovery still owed)."""
    try:
        date_from = str(body_json.get("dateFrom", "") or "").strip()
        date_to = str(body_json.get("dateTo", "") or "").strip()
        states = body_json.get("statuses") or list(_DEFAULT_ANCHOR_STATES)
        rows = body_json.get("rows") or []

        if not date_from or not date_to:
            return 400, "application/json", json.dumps({"error": "dateFrom and dateTo required"}).encode(), True
        from datetime import datetime as _dt
        try:
            _dt.strptime(date_from, "%Y-%m-%d")
            _dt.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            return 400, "application/json", json.dumps({"error": "dates must be YYYY-MM-DD"}).encode(), True

        state_clause = ""
        params = [date_from, date_to]
        if states and len(states) < len(_ALL_STATES):
            state_clause = "AND rs.logistics_state = ANY(%s)"
            params.append([str(s).strip() for s in states])

        sql = f"""
            SELECT
                rs.id AS leg_id,
                COALESCE(oe.order_sn, o.id) AS order_id,
                COALESCE(cc.short_id, cc.id) AS request_ref,
                COALESCE(cc.case_number::text, '—') AS case_number,
                CASE WHEN TRIM(COALESCE(oa.first_name,'') || ' ' || COALESCE(oa.last_name,'')) = ''
                     THEN 'Unknown' ELSE TRIM(COALESCE(oa.first_name,'') || ' ' || COALESCE(oa.last_name,'')) END AS buyer,
                COALESCE(s.name, 'Unknown') AS seller,
                COALESCE(rs.tracking_number, '—') AS tracking_number,
                {_STATE_EXPR} AS state,
                {_FEE_STATUS_EXPR} AS fee_status,
                rs.pickup_done_at AT TIME ZONE 'Asia/Manila' AS pickup_at,
                rs.delivery_done_at AT TIME ZONE 'Asia/Manila' AS delivered_at,
                rs.created_at AT TIME ZONE 'Asia/Manila' AS created_at,
                COALESCE(rs.shipping_fee_amount, 0) AS recorded_fee,
                COALESCE(wadj.amount, 0) AS debit_amount,
                COALESCE(wadj.batch_id, '') AS debit_batch,
                COALESCE(pp_o.region_name, NULLIF(rs.pickup_address_snapshot->>'region',''), '') AS origin_region,
                COALESCE(NULLIF(rs.return_destination_snapshot->>'region',''), pp_d.region_name, '') AS dest_region,
                rs.created_at AS raw_created_at
            FROM public.reverse_logistics_shipment rs
            JOIN public."order" o ON o.id = rs.order_id AND o.deleted_at IS NULL
            LEFT JOIN public.order_extension oe ON oe.order_id = o.id
            LEFT JOIN public.order_return_request_case cc ON cc.id = rs.order_return_request_case_id
            LEFT JOIN public.seller s ON s.id = rs.seller_id AND s.deleted_at IS NULL
            LEFT JOIN public.order_address oa ON oa.id = o.shipping_address_id AND oa.deleted_at IS NULL
            LEFT JOIN public.ph_city_municipality pcm_o ON pcm_o.name = COALESCE(NULLIF(rs.pickup_address_snapshot->>'city',''), oa.city) AND pcm_o.deleted_at IS NULL
            LEFT JOIN public.ph_province pp_o ON pp_o.name = COALESCE(pcm_o.province_name, NULLIF(rs.pickup_address_snapshot->>'province',''), oa.province) AND pp_o.deleted_at IS NULL
            LEFT JOIN public.ph_city_municipality pcm_d ON pcm_d.name = s.city AND pcm_d.deleted_at IS NULL
            LEFT JOIN public.ph_province pp_d ON pp_d.name = COALESCE(pcm_d.province_name, s.state) AND pp_d.deleted_at IS NULL
            LEFT JOIN public.wallet_adjustment wadj ON wadj.id = rs.fee_wallet_adjustment_id AND wadj.deleted_at IS NULL
            WHERE rs.deleted_at IS NULL
              AND (rs.created_at AT TIME ZONE 'Asia/Manila')::date BETWEEN %s AND %s
              {state_clause}
            ORDER BY rs.created_at
        """

        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        db_rows = _enrich_expected(cur.fetchall())
        cur.close()
        conn.close()

        # CSV evidence index (tracking -> {total fee, n})
        csv_by_ref = {}
        for r in rows:
            ref = str(r.get("reference", "") or "").strip()
            if not ref:
                continue
            amt = 0.0
            try:
                amt = float(r.get("amount") or 0)
            except (TypeError, ValueError):
                amt = 0.0
            entry = csv_by_ref.setdefault(ref, {"total": 0.0, "n": 0})
            entry["total"] += amt
            entry["n"] += 1

        all_db_keys = set()
        for row in db_rows:
            trk = row.get("tracking_number")
            if trk and str(trk) != "—":
                all_db_keys.add(str(trk))

        out_rows = []
        matched = missing = amt_mismatch = 0
        matched_amt = missing_amt = mismatch_amt = 0.0
        charge_gap = 0
        charge_gap_amt = 0.0
        for row in db_rows:
            baseline = _fee_baseline(row)
            d = {
                "leg_id": row.get("leg_id") or "",
                "request_ref": row.get("request_ref") or "",
                "order_id": row.get("order_id") or "",
                "tracking": row.get("tracking_number") or "",
                "state": row.get("state") or "",
                "fee_status": row.get("fee_status") or "",
                "amount": baseline if baseline is not None else 0.0,
                "recorded_fee": float(row.get("recorded_fee") or 0),
                "debit_amount": float(row.get("debit_amount") or 0),
                "expected_fee": row.get("expected_fee"),
                "pickup_at": row["pickup_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("pickup_at") else "",
                "delivered_at": row["delivered_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("delivered_at") else "",
                "created_at": row["created_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("created_at") else "",
                "buyer": row.get("buyer") or "",
                "seller": row.get("seller") or "",
            }
            # Charge gap: delivered back but never debited to the seller
            if row.get("state") == "Delivered Back" and row.get("fee_status") != "charged":
                charge_gap += 1
                charge_gap_amt += baseline if baseline is not None else 0.0

            trk = row.get("tracking_number")
            csv_hit = csv_by_ref.get(str(trk)) if trk and str(trk) != "—" else None
            if csv_hit is None:
                d["verdict"] = "missing"
                d["csv_amount"] = None
                d["diff"] = None
                missing += 1
                missing_amt += d["amount"]
            else:
                csv_total = round(csv_hit["total"], 2)
                diff = round(csv_total - d["amount"], 2)
                d["csv_amount"] = csv_total
                d["diff"] = diff
                if baseline is not None and abs(diff) >= 0.01:
                    d["verdict"] = "amount_mismatch"
                    amt_mismatch += 1
                    mismatch_amt += d["amount"]
                else:
                    d["verdict"] = "matched"
                    matched += 1
                    matched_amt += d["amount"]
            out_rows.append(d)

        extras = []
        for ref, info in csv_by_ref.items():
            if ref not in all_db_keys:
                extras.append({"reference": ref, "csv_amount": round(info["total"], 2),
                               "csv_count": info["n"]})

        anchor_total = len(out_rows)
        anchor_amt = sum(r["amount"] for r in out_rows)
        stats = {
            "anchor_total": anchor_total,
            "anchor_amount": round(anchor_amt, 2),
            "matched": matched,
            "matched_amount": round(matched_amt, 2),
            "missing": missing,
            "missing_amount": round(missing_amt, 2),
            "mismatch": amt_mismatch,
            "mismatch_amount": round(mismatch_amt, 2),
            "extras": len(extras),
            "extras_amount": round(sum(e["csv_amount"] for e in extras), 2),
            "charge_gap": charge_gap,
            "charge_gap_amount": round(charge_gap_amt, 2),
            "completeness_pct": round(matched / anchor_total * 100, 2) if anchor_total else 100.0,
            "csv_evidence": bool(rows),
        }
        return 200, "application/json", json.dumps({"stats": stats, "rows": out_rows, "extras": extras}).encode(), True
    except Exception as e:
        return 500, "application/json", json.dumps({"error": str(e)}).encode(), True


def serve_return_shipping_portal():
    """Serve the Return Shipping Fee Reconciliation page."""
    return _HTML_TEMPLATE


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Return Shipping Fee Reconciliation — MallPlus</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Quicksand:wght@500;600;700&display=swap" rel="stylesheet"/>
<link href="https://fonts.cdnfonts.com/css/garet" rel="stylesheet"/>
<style>
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:'Space Grotesk',system-ui,sans-serif;background:linear-gradient(135deg,#3724ED 0%,#1A9FD8 45%,#00AFA0 100%);background-attachment:fixed;color:var(--dark);font-size:14px;min-height:100vh;}
  :root{--dark:#1A1035;--dim:#5B6B7C;--dimlt:#A0AEC0;--card:#FFFFFF;--border:rgba(0,175,160,.13);--accent:#00AFA0;--teal-dk:#007A73;--red:#EF4444;--amber:#F59E0B;--purple:#7C3AED;--green:#22C55E;--blue:#2563EB;--shadow-sm:0 2px 12px rgba(0,175,160,.10);--shadow-md:0 8px 32px rgba(0,175,160,.16);--r-lg:16px;--r-sm:10px;}
  header{background:rgba(255,255,255,.92);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;justify-content:space-between;gap:12px;position:sticky;top:0;z-index:10;}
  header h1{font-family:'Garet','Space Grotesk',sans-serif;font-size:1.15rem;font-weight:700;color:var(--dark);}
  .nav{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
  .badge{background:var(--purple);color:#fff;font-size:.65rem;font-weight:700;letter-spacing:1px;text-transform:uppercase;border-radius:999px;padding:4px 10px;}
  .badge-amber{background:#B45309;color:#fff;}
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
  .stat-card .value.red{color:var(--red);} .stat-card .value.amber{color:var(--amber);} .stat-card .value.purple{color:var(--purple);} .stat-card .value.green{color:var(--green);} .stat-card .value.blue{color:var(--blue);}
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
  .status-delivered{background:rgba(34,197,94,.14);color:var(--green);}
  .status-pickup{background:rgba(37,99,235,.14);color:var(--blue);}
  .status-lost{background:rgba(239,68,68,.12);color:var(--red);}
  .status-cancelled{background:rgba(100,116,139,.12);color:var(--dim);}
  .status-failed{background:rgba(245,158,11,.14);color:var(--amber);}
  .status-pending{background:rgba(245,158,11,.14);color:var(--amber);}
  .status-charged{background:rgba(34,197,94,.14);color:var(--green);}
  .status-na{background:rgba(100,116,139,.12);color:var(--dim);}
  .status-unknown{background:rgba(100,116,139,.12);color:var(--dim);}
  .empty{text-align:center;color:var(--dim);padding:40px;font-family:'Quicksand',sans-serif;}
  .loading{padding:40px;text-align:center;color:var(--dim);font-family:'Quicksand',sans-serif;}
  .pagination{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;font-size:.82rem;color:var(--dim);flex-wrap:wrap;gap:10px;}
  .pagination .btns{display:flex;gap:6px;align-items:center;}
  #error{display:none;background:#FDECEA;color:#C0392B;border:1px solid #F5C6C0;border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:.85rem;}
  .tabs{display:flex;gap:8px;margin-bottom:16px;}
  .tab-btn{padding:8px 18px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;border:1.5px solid var(--border);background:var(--card);color:var(--dim);transition:all .15s;font-family:'Quicksand',sans-serif;}
  .tab-btn.active{background:var(--accent);color:#fff;border-color:var(--accent);}
  .tab-content{display:none;} .tab-content.active{display:block;}
  .upload-zone{border:2px dashed var(--border);border-radius:12px;padding:40px;text-align:center;cursor:pointer;transition:all .15s;margin-bottom:16px;background:var(--card);}
  .upload-zone:hover,.upload-zone.dragover{border-color:var(--accent);background:rgba(0,175,160,.05);}
  .upload-zone .upload-icon{font-size:32px;margin-bottom:8px;}
  .upload-zone .upload-title{font-size:14px;font-weight:600;margin-bottom:4px;}
  .upload-zone .upload-hint{font-size:11px;color:var(--dim);}
  .upload-zone input[type=file]{display:none;}
  .preview-box{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:16px;}
  .preview-box h3{font-size:14px;margin-bottom:12px;}
  .mappings{display:flex;flex-wrap:wrap;gap:16px;margin-bottom:12px;}
  .mapping-item{font-size:12px;color:var(--dim);}
  .mapping-item b{color:var(--dark);}
  .preview-table-wrap{max-height:260px;overflow:auto;border:1px solid var(--border);border-radius:8px;}
  .preview-table-wrap table{min-width:0;width:100%;}
  .preview-table-wrap th,.preview-table-wrap td{font-size:.72rem;padding:6px 8px;white-space:nowrap;}
  .guide-panel{margin-bottom:14px;padding:14px;background:#F8FAFC;border:1px solid rgba(0,175,160,.25);border-radius:10px;font-size:13px;line-height:1.7;color:#1A1035;}
  .recon-status{margin-bottom:12px;padding:12px 16px;border-radius:10px;font-size:13px;}
  .recon-status.ok{background:#E7F8EE;color:#166534;border:1px solid #BBF7D0;}
  .recon-status.warn{background:#FEF3C7;color:#92400E;border:1px solid #FDE68A;}
  .match-badge{display:inline-block;border-radius:999px;padding:3px 10px;font-size:.7rem;font-weight:700;letter-spacing:.4px;text-transform:uppercase;}
  .match-matched{background:rgba(34,197,94,.14);color:var(--green);}
  .match-amount_mismatch{background:rgba(245,158,11,.14);color:var(--amber);}
  .match-not_found{background:rgba(239,68,68,.12);color:var(--red);}
  .match-missing{background:rgba(239,68,68,.12);color:var(--red);}
  .match-not_in_ledger{background:rgba(37,99,235,.12);color:var(--blue);}
</style>
</head>
<body>
<header>
  <div class="nav"><a id="backLink" href="/recon/logistics/shipping/" class="btn btn-secondary btn-sm">← Shipping Fee Reconciliation</a><span class="badge">Return Journey</span></div>
  <h1>↩️ Return Shipping Fee Reconciliation</h1>
  <div class="nav"><span class="badge badge-amber">Seller-Charge Basis</span></div>
</header>
<div class="container">
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('download')" id="tab-download">📋 Download Board</button>
    <button class="tab-btn" onclick="switchTab('reconcile')" id="tab-reconcile">🔄 Reconcile (J&amp;T Return Bill)</button>
  </div>
  <div class="tab-content active" id="download-tab">
  <div class="filters">
    <div class="filter-group"><label>Leg State</label>
      <select id="state"><option value="">All States</option>
        <option value="LOGISTICS_NOT_STARTED">Not Started</option>
        <option value="LOGISTICS_REQUEST_CREATED">Request Created</option>
        <option value="LOGISTICS_REQUEST_PENDING">Request Pending</option>
        <option value="LOGISTICS_READY">Ready</option>
        <option value="LOGISTICS_PICKUP_FAILED">Pickup Failed</option>
        <option value="LOGISTICS_PICKUP_DONE">Pickup Done</option>
        <option value="LOGISTICS_DELIVERY_DONE">Delivered Back</option>
        <option value="LOGISTICS_DELIVERY_FAILED">Delivery Failed</option>
        <option value="LOGISTICS_LOST">Lost</option>
        <option value="LOGISTICS_REQUEST_CANCELLED">Cancelled</option>
      </select>
    </div>
    <div class="filter-group"><label>Fee Status</label>
      <select id="feeStatus"><option value="">All</option><option value="charged">Charged</option><option value="pending">Pending</option><option value="n/a">N/A</option></select>
    </div>
    <div class="filter-group"><label>Date From</label><input type="date" id="dateFrom"></div>
    <div class="filter-group"><label>Date To</label><input type="date" id="dateTo"></div>
    <div class="filter-group" style="flex:1;min-width:220px"><label>Search (Order / Buyer / Seller / Tracking / Return Req)</label><input type="text" id="search" placeholder="e.g. order #, tracking, seller"></div>
    <button class="btn btn-primary" onclick="fetchData()">🔍 Filter</button>
    <button class="btn btn-secondary" onclick="resetFilters()">↺ Reset</button>
    <button class="btn btn-secondary" onclick="exportCSV()">📥 Export CSV</button>
  </div>
  <div id="error"></div>
  <div class="stats" id="stats"></div>
  <div class="table-wrap">
    <div id="loading" class="loading">Loading return legs…</div>
    <table id="results" style="display:none">
      <thead><tr>
        <th>Return Req</th><th>Order #</th><th>Buyer</th><th>Seller</th><th>Tracking #</th>
        <th>Leg State</th><th>Fee Status</th><th>Pickup At</th><th>Delivered Back</th>
        <th class="amount">Recorded Fee</th><th class="amount">Est Fee</th><th class="amount">Seller Debit</th><th>Debit Batch</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
  <div class="pagination" id="pagination" style="display:none"></div>
  </div><!-- /download-tab -->

  <!-- RECONCILE TAB -->
  <div class="tab-content" id="reconcile-tab">
    <div style="margin-bottom:14px;display:flex;gap:8px;flex-wrap:wrap;">
      <button class="btn btn-primary" id="modeAnchorBtn" onclick="switchReconMode('anchor')">📒 Ledger Anchor Recon</button>
      <button class="btn btn-secondary" id="modeCsvBtn" onclick="switchReconMode('csv')">📄 CSV-Based Recon</button>
      <button class="btn btn-secondary" id="modeGuideBtn" onclick="switchReconMode('guide')">📖 Guide</button>
    </div>
    <div id="anchorPanel" style="display:none;margin-bottom:14px;padding:14px;background:#F8FAFC;border:1px solid rgba(0,175,160,.25);border-radius:10px;">
      <style>.chip{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border:1px solid rgba(0,175,160,.25);border-radius:14px;font-size:12px;cursor:pointer;background:#E0F5F3;color:var(--dark);user-select:none}.chip:hover{border-color:var(--accent)}.chip input{accent-color:var(--accent);margin:0}</style>
      <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;">
        <div class="filter-group"><label>Date From</label><input type="date" id="anchorDateFrom"></div>
        <div class="filter-group"><label>Date To</label><input type="date" id="anchorDateTo"></div>
        <div class="filter-group"><label>Leg States in Anchor</label>
          <div id="anchorStateChips" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
            <label class="chip"><input type="checkbox" value="LOGISTICS_PICKUP_DONE" checked>🔵 Pickup Done</label>
            <label class="chip"><input type="checkbox" value="LOGISTICS_DELIVERY_DONE" checked>🟢 Delivered Back</label>
            <label class="chip"><input type="checkbox" value="LOGISTICS_DELIVERY_FAILED" checked>🟠 Delivery Failed</label>
            <label class="chip"><input type="checkbox" value="LOGISTICS_LOST">🔴 Lost</label>
            <label class="chip"><input type="checkbox" value="LOGISTICS_REQUEST_CANCELLED">⚪ Cancelled</label>
            <label class="chip"><input type="checkbox" value="LOGISTICS_NOT_STARTED">⚪ Not Started</label>
            <label class="chip"><input type="checkbox" value="LOGISTICS_READY">⚪ Ready</label>
            <label class="chip"><input type="checkbox" value="LOGISTICS_REQUEST_CREATED">⚪ Req Created</label>
            <label class="chip"><input type="checkbox" value="LOGISTICS_REQUEST_PENDING">⚪ Req Pending</label>
            <label class="chip"><input type="checkbox" value="LOGISTICS_PICKUP_FAILED">🟠 Pickup Failed</label>
            <span onclick="setAnchorStates(true)" style="font-size:11px;color:var(--accent);cursor:pointer;text-decoration:underline;">All</span>
            <span onclick="setAnchorStates(false)" style="font-size:11px;color:var(--accent);cursor:pointer;text-decoration:underline;margin-left:4px;">None</span>
          </div></div>
        <button class="btn btn-primary" id="runAnchorBtn" onclick="runAnchorRecon()">📒 Run Anchor Recon</button>
      </div>
      <div style="margin-top:10px;font-size:12px;color:var(--dim);line-height:1.6;">
        <b>Anchor</b> = every return leg in <b>our</b> ledger (reverse_logistics_shipment) for the date range + states — the completeness basis, not the J&amp;T file.<br>
        Upload the J&amp;T return-leg bill CSV above as <b>optional evidence</b>: legs missing from the bill are flagged ❌ (completeness gap), fee differences ⚠️, bill refs with no leg ➕.<br>
        Default states = Pickup Done + Delivered Back + Delivery Failed (chargeable / in-flight). Tick <b>Lost</b> or <b>Cancelled</b> to include them — they are downloadable but NOT charge events.<br>
        <b>Charge gap</b> = Delivered Back legs not yet debited to the seller (money still owed / manual absorb).
      </div>
    </div>
    <div class="guide-panel" id="guidePanel" style="display:none">
      <b>📖 How to use this recon tool</b>
      <div style="margin-top:8px;">
        <b style="color:var(--accent)">📄 CSV-Based Recon:</b> upload the J&amp;T return-leg bill (own tracking numbers, billed separately from forward shipping) and match it against our return-leg ledger.
        <ul style="margin:6px 0 10px 18px;padding:0;">
          <li><b>Tracking #</b> column is required — matched against our return legs.</li>
          <li><b>Amount</b> column optional — compared against our recorded fee (seller debit when no recorded fee).</li>
        </ul>
        <b>Reading the results:</b>
        <ul style="margin:6px 0 10px 18px;padding:0;">
          <li>✅ <b>Matched</b> — leg found and the fee agrees.</li>
          <li>⚠️ <b>Amount Mismatch</b> — leg found but the billed fee differs (see Diff column).</li>
          <li>❌ <b>Not Found</b> — in the bill but no matching return leg in our ledger.</li>
        </ul>
        <b style="color:var(--accent)">📒 Ledger Anchor mode (recommended):</b>
        <ol style="margin:6px 0 10px 18px;padding:0;">
          <li>Set <b>Date From / To</b> (Manila) and tick the <b>leg states</b> to cover — the anchor = every matching leg in <b>our</b> ledger.</li>
          <li>(Optional) Upload the J&amp;T return bill (CSV) as evidence.</li>
          <li>Click <b>📒 Run Anchor Recon</b>.</li>
        </ol>
        <b>Reading anchor results:</b>
        <ul style="margin:6px 0 10px 18px;padding:0;">
          <li>✅ <b>Matched</b> — in our ledger and the bill agrees.</li>
          <li>⚠️ <b>Amount Mismatch</b> — found but the billed fee differs.</li>
          <li>❌ <b>Missing from CSV</b> — in our ledger but absent from the bill = <b>completeness gap</b>.</li>
          <li>➕ <b>Not in Ledger</b> — in the bill but no return leg in our records.</li>
        </ul>
        <b>Charge gap</b> = Delivered Back legs with no seller debit yet — the seller still owes the return fee (or it is a manual platform absorb).<br>
        <b>Est Fee</b> = reverse rate card (J&amp;T Reverse rule, zone-pair flat fee). No rate → <span style="color:var(--dim)">—</span>.<br>
        <b>Completeness %</b> = matched share of the anchor. Use <b>📥 Export</b> to pull exceptions.<br>
        <span style="color:var(--dim);font-size:12px;">Match key: tracking number. Tip: anchor on <b>our</b> data first — a 3rd-party bill can be silently incomplete. Lost-in-return legs belong to Claims Reconciliation (not chargeable to the seller).</span>
      </div>
    </div>
    <div class="upload-zone" id="uploadZone" onclick="document.getElementById('csvUpload').click()">
      <div class="upload-icon">📁</div>
      <div class="upload-title">Upload J&amp;T Return Bill CSV</div>
      <div class="upload-hint">Drag &amp; drop or click. Needs: Tracking #. Optional: Amount (fee), Date.</div>
      <input type="file" id="csvUpload" accept=".csv" onchange="handleCSVUpload(event)">
    </div>
    <div class="preview-box" id="previewBox" style="display:none">
      <h3>📊 CSV Preview &amp; Column Mapping</h3>
      <div class="mappings" id="mappings"></div>
      <div class="preview-table-wrap" id="previewTable"></div>
      <div style="margin-top:12px;display:flex;gap:8px;">
        <button class="btn btn-primary" onclick="runReconcile()">🔄 Run Reconciliation</button>
        <button class="btn btn-secondary" onclick="resetReconcile()">↺ Clear</button>
      </div>
    </div>
    <div id="reconcileStatus" class="recon-status" style="display:none"></div>
    <div class="stats" id="reconcileStats" style="display:none"></div>
    <div class="filters" id="reconcileFilters" style="display:none">
      <div class="filter-group"><label>Search</label><input type="text" id="reconSearch" placeholder="Tracking, Order #, Return Req" oninput="filterReconcileResults()"></div>
      <div class="filter-group"><label>Match Type</label><select id="reconMatchType" onchange="filterReconcileResults()"><option value="">All</option><option value="matched">✅ Matched</option><option value="amount_mismatch">⚠️ Amount Mismatch</option><option value="not_found">❌ Not Found</option></select></div>
      <span id="reconFilterCount" style="color:var(--dim);font-size:12px;align-self:flex-end;padding-bottom:8px;"></span>
      <button class="btn btn-secondary btn-sm" onclick="document.getElementById('reconSearch').value='';document.getElementById('reconMatchType').value='';filterReconcileResults();">↺ Clear</button>
    </div>
    <div class="table-wrap" id="reconcileTableWrap" style="display:none">
      <table id="reconcileResults">
        <thead id="reconcileHead"><tr>
          <th>Match</th><th>Tracking #</th><th>Return Req</th><th>Leg State</th><th class="amount">Recorded Fee</th><th class="amount">Seller Debit</th><th class="amount">CSV Bill Amt</th><th class="amount">Diff</th><th>Pickup At</th><th>Delivered Back</th>
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
var RECON_BASE='/recon';
if(location.pathname.indexOf('/recon-staging')===0){RECON_BASE='/recon-staging';}
document.getElementById('backLink').href=RECON_BASE+'/logistics/shipping/';
var currentPage=1;
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function fmtNum(n){var v=Number(n||0);return v.toLocaleString('en-PH',{minimumFractionDigits:2,maximumFractionDigits:2});}
function getFilters(){return {state:document.getElementById('state').value,feeStatus:document.getElementById('feeStatus').value,dateFrom:document.getElementById('dateFrom').value,dateTo:document.getElementById('dateTo').value,search:document.getElementById('search').value.trim()};}
function resetFilters(){document.getElementById('state').value='';document.getElementById('feeStatus').value='';document.getElementById('dateFrom').value='';document.getElementById('dateTo').value='';document.getElementById('search').value='';fetchData();}
function fetchData(){currentPage=1;loadData();}
function loadData(){var f=getFilters();var p=new URLSearchParams(Object.entries(f).filter(function(kv){return kv[1]!=='';}));p.set('page',currentPage);p.set('page_size',50);
  document.getElementById('error').style.display='none';
  fetch(RECON_BASE+'/return-shipping/api/orders?'+p).then(function(r){return r.json();}).then(function(d){
    if(d.error){document.getElementById('error').textContent='Error: '+d.error;document.getElementById('error').style.display='block';return;}
    document.getElementById('loading').style.display='none';
    document.getElementById('results').style.display='table';
    renderStats(d.stats||{});renderTable(d.rows||[]);renderPagination(d.total||0,currentPage,50);
  }).catch(function(e){document.getElementById('error').textContent='Network error: '+e;document.getElementById('error').style.display='block';});}
function renderStats(s){
  var cards=[
    {v:s.total||0,l:'Return Legs',c:''},
    {v:s.pickup_done||0,l:'Pickup Done',c:'blue'},
    {v:s.delivered||0,l:'Delivered Back',c:'green'},
    {v:s.lost||0,l:'Lost',c:'red'},
    {v:s.charged_count||0,l:'🔴 Charged to Seller',c:''},
    {v:'₱'+fmtNum(s.debited_total||0),l:'Total Seller Debits',c:''},
    {v:s.pending||0,l:'Fee Pending',c:'amber'},
    {v:'₱'+fmtNum(s.recorded_total||0),l:'Recorded Fees',c:''}
  ];
  document.getElementById('stats').innerHTML=cards.map(function(c){return '<div class="stat-card"><div class="value '+c.c+'">'+c.v+'</div><div class="label">'+c.l+'</div></div>';}).join('');
}
function stateBadge(s){
  var map={'Delivered Back':'status-delivered','Pickup Done':'status-pickup','Lost':'status-lost','Cancelled':'status-cancelled','Delivery Failed':'status-failed','Pickup Failed':'status-failed','Not Started':'status-pending','Ready':'status-pending','Request Created':'status-pending','Request Pending':'status-pending'};
  return '<span class="status '+(map[s]||'status-unknown')+'">'+esc(s||'Unknown')+'</span>';
}
function feeBadge(s){
  var map={'charged':'status-charged','pending':'status-pending'};
  var label={'charged':'Charged','pending':'Pending'};
  return '<span class="status '+(map[s]||'status-na')+'">'+(label[s]||esc(s==='n/a'?'N/A':(s||'—')))+'</span>';
}
function renderTable(rows){
  var tb=document.getElementById('tbody');
  if(!rows||rows.length===0){tb.innerHTML='<tr><td colspan="13" class="empty">No return legs found</td></tr>';return;}
  tb.innerHTML=rows.map(function(r){
    var debit=Math.abs(r.debit_amount||0);
    return '<tr>'+
      '<td><code>'+esc(r.request_ref||'—')+'</code> <span class="copy-btn" data-copy="'+esc(r.request_ref||'')+'" onclick="copyToClipboard(this)" title="Copy">📋</span></td>'+
      '<td><code>'+esc(r.order_id||'—')+'</code> <span class="copy-btn" data-copy="'+esc(r.order_id||'')+'" onclick="copyToClipboard(this)" title="Copy">📋</span></td>'+
      '<td>'+esc(r.buyer||'—')+'</td><td>'+esc(r.seller||'—')+'</td>'+
      '<td>'+esc(r.tracking_number||'—')+'</td>'+
      '<td>'+stateBadge(r.state)+'</td>'+
      '<td>'+feeBadge(r.fee_status)+'</td>'+
      '<td>'+esc(r.pickup_at||'—')+'</td><td>'+esc(r.delivered_at||'—')+'</td>'+
      '<td class="amount">'+(Number(r.recorded_fee||0)>0?'₱'+fmtNum(r.recorded_fee):'<span style="color:var(--dim)">—</span>')+'</td>'+
      '<td class="amount">'+(r.expected_fee==null||r.expected_fee===0?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.expected_fee))+'</td>'+
      '<td class="amount">'+(debit>0?'<span style="color:var(--red)">−₱'+fmtNum(debit)+'</span>':'<span style="color:var(--dim)">—</span>')+'</td>'+
      '<td>'+esc(r.debit_batch||'—')+'</td>'+
    '</tr>';
  }).join('');
}
function renderPagination(t,p,ps){
  if(t<=ps&&p===1){document.getElementById('pagination').style.display='none';return;}
  document.getElementById('pagination').style.display='flex';
  var tp=Math.max(1,Math.ceil(t/ps));
  document.getElementById('pagination').innerHTML='<div class="info">Showing '+((p-1)*ps+1)+'–'+Math.min(p*ps,t)+' of '+t+' return legs</div><div class="btns">'+
    '<button class="btn btn-secondary btn-sm" onclick="goPage(1)" '+(p<=1?'disabled':'')+'>««</button>'+
    '<button class="btn btn-secondary btn-sm" onclick="goPage('+(p-1)+')" '+(p<=1?'disabled':'')+'>« Prev</button>'+
    '<span style="padding:4px 12px">Page '+p+' / '+tp+'</span>'+
    '<button class="btn btn-secondary btn-sm" onclick="goPage('+(p+1)+')" '+(p>=tp?'disabled':'')+'>Next »</button>'+
    '<button class="btn btn-secondary btn-sm" onclick="goPage('+tp+')" '+(p>=tp?'disabled':'')+'>»»</button></div>';
}
function goPage(p){currentPage=p;loadData();}
function copyToClipboard(el){var text=el.getAttribute('data-copy');navigator.clipboard.writeText(text).then(function(){el.textContent='✅';setTimeout(function(){el.textContent='📋';},1500);}).catch(function(){prompt('Copy:',text);});}
function exportCSV(){var p=new URLSearchParams(getFilters());p.delete('page');p.delete('page_size');p.set('export','csv');window.open(RECON_BASE+'/return-shipping/api/orders?'+p,'_blank');}
function getTodayDate(){var today=new Date();var y=today.getFullYear();var m=String(today.getMonth()+1).padStart(2,'0');var d=String(today.getDate()).padStart(2,'0');return y+'-'+m+'-'+d;}

/* ── Reconcile tab ─────────────────────────────────────────────── */
var csvData=[],csvHeaders=[],colMap={},reconcileResults=[],reconMode='anchor',anchorStats=null;
function switchTab(t){
  document.querySelectorAll('.tab-btn').forEach(function(b){b.classList.remove('active');});
  document.querySelectorAll('.tab-content').forEach(function(c){c.classList.remove('active');});
  document.getElementById('tab-'+t).classList.add('active');
  document.getElementById(t+'-tab').classList.add('active');
}
function switchReconMode(m){
  reconMode=m;
  document.getElementById('modeAnchorBtn').className=m==='anchor'?'btn btn-primary':'btn btn-secondary';
  document.getElementById('modeCsvBtn').className=m==='csv'?'btn btn-primary':'btn btn-secondary';
  document.getElementById('modeGuideBtn').className=m==='guide'?'btn btn-primary':'btn btn-secondary';
  document.getElementById('anchorPanel').style.display=m==='anchor'?'block':'none';
  document.getElementById('guidePanel').style.display=m==='guide'?'block':'none';
  document.getElementById('uploadZone').style.display=(m==='csv'||m==='anchor')?'block':'none';
}
function getAnchorStates(def){
  var boxes=document.querySelectorAll('#anchorStateChips input:checked');
  var vals=[];for(var i=0;i<boxes.length;i++){vals.push(boxes[i].value);}
  return vals.length?vals:def;
}
function setAnchorStates(on){
  var boxes=document.querySelectorAll('#anchorStateChips input');
  for(var i=0;i<boxes.length;i++){boxes[i].checked=on;}
}
function runAnchorRecon(){
  var df=document.getElementById('anchorDateFrom').value,dt=document.getElementById('anchorDateTo').value;
  if(!df||!dt){alert('Set Date From and Date To for the anchor');return;}
  var btn=document.getElementById('runAnchorBtn');
  btn.disabled=true;btn.textContent='⏳ Anchoring...';
  document.getElementById('reconcileStatus').style.display='none';
  var payload={dateFrom:df,dateTo:dt,statuses:getAnchorStates(['LOGISTICS_PICKUP_DONE','LOGISTICS_DELIVERY_DONE','LOGISTICS_DELIVERY_FAILED']),rows:[]};
  if(csvData.length&&colMap.reference){
    var amtCol=colMap.amount;
    payload.rows=csvData.map(function(r){
      return {reference:String(r[colMap.reference]||'').trim(),
              amount:amtCol?(parseFloat(String(r[amtCol]).replace(/[^0-9.\-]/g,''))||0):0,
              status:''};
    }).filter(function(r){return r.reference!=='';});
  }
  fetch(RECON_BASE+'/return-shipping/api/reconcile-anchor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
  .then(function(r){return r.json();})
  .then(function(d){
    btn.disabled=false;btn.textContent='📒 Run Anchor Recon';
    if(d.error){document.getElementById('reconcileStatus').className='recon-status warn';document.getElementById('reconcileStatus').textContent='Error: '+d.error;document.getElementById('reconcileStatus').style.display='block';return;}
    anchorStats=d.stats||null;
    reconcileResults=(d.rows||[]).map(function(x){
      return {match_type:x.verdict,reference:x.tracking||'',csv_amount:x.csv_amount==null?null:x.csv_amount,db_amount:x.amount,diff:x.diff==null?null:x.diff,date:x.created_at||'',csv_status:'',db_status:x.fee_status||'',leg_id:x.leg_id||'',tracking:x.tracking||'',request_ref:x.request_ref||'',state:x.state||'',recorded_fee:x.recorded_fee||0,debit_amount:x.debit_amount||0,pickup_at:x.pickup_at||'',delivered_at:x.delivered_at||''};
    }).concat((d.extras||[]).map(function(x){
      return {match_type:'not_in_ledger',reference:x.reference||'',csv_amount:x.csv_amount||0,db_amount:null,diff:null,date:'',csv_status:'',db_status:'',leg_id:'',tracking:x.reference||'',request_ref:'',state:'',recorded_fee:0,debit_amount:0,pickup_at:'',delivered_at:''};
    }));
    document.getElementById('reconcileStats').style.display='flex';
    document.getElementById('reconcileFilters').style.display='flex';
    document.getElementById('reconcileTableWrap').style.display='block';
    document.getElementById('reconcileExport').style.display='block';
    filterReconcileResults();
  })
  .catch(function(e){btn.disabled=false;btn.textContent='📒 Run Anchor Recon';document.getElementById('reconcileStatus').className='recon-status warn';document.getElementById('reconcileStatus').textContent='Network error: '+e;document.getElementById('reconcileStatus').style.display='block';});
}
function parseCSVLine(l){var r=[],c='',q=false;for(var i=0;i<l.length;i++){var ch=l[i];if(q){if(ch=='"'){if(i+1<l.length&&l[i+1]=='"'){c+='"';i++;}else{q=false;}}else{c+=ch;}}else if(ch=='"'){q=true;}else if(ch===','){r.push(c);c='';}else{c+=ch;}}r.push(c);return r;}
function findCol(rx){for(var i=0;i<csvHeaders.length;i++){if(rx.test(csvHeaders[i]))return csvHeaders[i];}return null;}
function handleCSVUpload(e){
  var file=e.target.files[0];if(!file)return;
  var reader=new FileReader();
  reader.onload=function(ev){
    var lines=ev.target.result.split(/\r?\n/).filter(function(l){return l.trim()!=='';});
    if(lines.length<2){alert('CSV needs a header row and at least one data row.');return;}
    csvHeaders=parseCSVLine(lines[0]);csvData=[];
    for(var i=1;i<lines.length;i++){
      var vals=parseCSVLine(lines[i]);
      if(vals.length===csvHeaders.length){
        var row={};csvHeaders.forEach(function(h,j){row[h]=vals[j];});csvData.push(row);
      }
    }
    colMap={};
    colMap.reference=findCol(/tracking|track.?no|awb|parcel|waybill|bill.?no/i);
    colMap.amount=findCol(/amount|fee|value|shipping.?fee|charge/i);
    colMap.date=findCol(/date|created|billed|return/i);
    if(!colMap.reference){alert('Could not detect a Tracking / AWB column.');return;}
    var mapHtml='<div class="mapping-item">Reference (Tracking): <b>'+esc(colMap.reference)+'</b></div>'+
      (colMap.amount?'<div class="mapping-item">Amount: <b>'+esc(colMap.amount)+'</b></div>':'')+
      (colMap.date?'<div class="mapping-item">Date: <b>'+esc(colMap.date)+'</b></div>':'');
    document.getElementById('mappings').innerHTML=mapHtml;
    var previewRows=csvData.slice(0,5);
    var th='<tr>'+csvHeaders.map(function(h){return'<th>'+esc(h)+'</th>';}).join('')+'</tr>';
    var tr=previewRows.map(function(r){return'<tr>'+csvHeaders.map(function(h){return'<td>'+esc(String(r[h]||''))+'</td>';}).join('')+'</tr>';}).join('');
    document.getElementById('previewTable').innerHTML='<table>'+th+tr+'</table>';
    document.getElementById('previewBox').style.display='block';
  };
  reader.readAsText(file);
}
function runReconcile(){
  if(!csvData.length){alert('Upload a CSV first.');return;}
  reconMode='csv';
  var rows=csvData.map(function(r){
    var o={reference:(r[colMap.reference]||'').trim()};
    if(colMap.amount)o.amount=parseFloat(r[colMap.amount])||0;
    if(colMap.date)o.date=(r[colMap.date]||'').trim();
    return o;
  });
  document.getElementById('reconcileStatus').style.display='none';
  fetch(RECON_BASE+'/return-shipping/api/reconcile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rows:rows})})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.error){document.getElementById('reconcileStatus').className='recon-status warn';document.getElementById('reconcileStatus').textContent='Error: '+d.error;document.getElementById('reconcileStatus').style.display='block';return;}
      reconcileResults=d.results||[];
      document.getElementById('reconcileStats').style.display='flex';
      document.getElementById('reconcileFilters').style.display='flex';
      document.getElementById('reconcileTableWrap').style.display='block';
      document.getElementById('reconcileExport').style.display='block';
      filterReconcileResults();
    })
    .catch(function(e){document.getElementById('reconcileStatus').className='recon-status warn';document.getElementById('reconcileStatus').textContent='Network error: '+e;document.getElementById('reconcileStatus').style.display='block';});
}
function matchBadge(m){
  var map={'matched':'match-matched','amount_mismatch':'match-amount_mismatch','not_found':'match-not_found','missing':'match-missing','not_in_ledger':'match-not_in_ledger'};
  var label={'matched':'✅ Matched','amount_mismatch':'⚠️ Amount','not_found':'❌ Not Found','missing':'❌ Missing from Bill','not_in_ledger':'➕ Not in Ledger'};
  return '<span class="match-badge '+(map[m]||'')+'">'+(label[m]||m)+'</span>';
}
function filterReconcileResults(){
  var opts=reconMode==='anchor'
    ?[['','All'],['matched','✅ Matched'],['amount_mismatch','⚠️ Amount Mismatch'],['missing','❌ Missing from Bill'],['not_in_ledger','➕ Not in Ledger']]
    :[['','All'],['matched','✅ Matched'],['amount_mismatch','⚠️ Amount Mismatch'],['not_found','❌ Not Found']];
  var sel=document.getElementById('reconMatchType');
  var cur=sel.value;
  sel.innerHTML=opts.map(function(o){return'<option value="'+o[0]+'">'+o[1]+'</option>';}).join('');
  if(opts.some(function(o){return o[0]===cur;}))sel.value=cur;else sel.value='';
  var q=document.getElementById('reconSearch').value.toLowerCase();
  var mt=sel.value;
  var shown=reconcileResults.filter(function(r){
    if(mt&&r.match_type!==mt)return false;
    if(q){var hay=(r.reference+' '+r.request_ref+' '+(r.state||'')).toLowerCase();if(hay.indexOf(q)===-1)return false;}
    return true;
  });
  renderReconcileStats(shown);
  renderReconcileTable(shown);
  document.getElementById('reconFilterCount').textContent=shown.length+' / '+reconcileResults.length+' rows';
}
function renderReconcileStats(shown){
  if(reconMode==='anchor'&&anchorStats){
    var s=anchorStats;
    var csvTxt=s.csv_evidence?'':' <span style="font-size:11px;color:var(--dim)">(no CSV uploaded)</span>';
    var pctColor=s.completeness_pct>=100?'green':(s.completeness_pct>=50?'amber':'red');
    var gapColor=s.charge_gap>0?'red':'green';
    document.getElementById('reconcileStats').innerHTML=
      '<div class="stat-card"><div class="value">'+s.anchor_total+'</div><div class="label">Anchor Return Legs</div></div>'+
      '<div class="stat-card"><div class="value green">'+s.matched+'</div><div class="label">✅ Matched (₱'+fmtNum(s.matched_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value red">'+s.missing+'</div><div class="label">❌ Missing from Bill (₱'+fmtNum(s.missing_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value amber">'+s.mismatch+'</div><div class="label">⚠️ Amount Mismatch</div></div>'+
      '<div class="stat-card"><div class="value purple">'+s.extras+'</div><div class="label">➕ Not in Ledger (₱'+fmtNum(s.extras_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value '+gapColor+'">'+s.charge_gap+'</div><div class="label">⚠️ Delivered, Not Debited (₱'+fmtNum(s.charge_gap_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value '+pctColor+'">'+s.completeness_pct+'%</div><div class="label">Completeness'+csvTxt+'</div></div>'+
      '<div class="stat-card"><div class="value">₱'+fmtNum(s.anchor_amount)+'</div><div class="label">Anchor Fees</div></div>';
    return;
  }
  var matched=shown.filter(function(x){return x.match_type==='matched';}).length;
  var mismatch=shown.filter(function(x){return x.match_type==='amount_mismatch';}).length;
  var notFound=shown.filter(function(x){return x.match_type==='not_found';}).length;
  var tAmt=shown.reduce(function(s,x){return s+(x.csv_amount||0);},0);
  document.getElementById('reconcileStats').innerHTML=
    '<div class="stat-card"><div class="value">'+shown.length+'</div><div class="label">Rows</div></div>'+
    '<div class="stat-card"><div class="value green">'+matched+'</div><div class="label">✅ Matched</div></div>'+
    '<div class="stat-card"><div class="value amber">'+mismatch+'</div><div class="label">⚠️ Amount</div></div>'+
    '<div class="stat-card"><div class="value red">'+notFound+'</div><div class="label">❌ Not Found</div></div>'+
    '<div class="stat-card"><div class="value">₱'+fmtNum(tAmt)+'</div><div class="label">Total Bill Amount</div></div>';
}
function renderReconcileTable(rows){
  var tb=document.getElementById('reconcileTbody');
  var head=document.getElementById('reconcileHead');
  if(reconMode==='anchor'){
    head.innerHTML='<tr><th>Match</th><th>Tracking #</th><th>Return Req</th><th>Leg State</th><th class="amount">Recorded Fee</th><th class="amount">Seller Debit</th><th class="amount">CSV Bill Amt</th><th class="amount">Diff</th><th>Pickup At</th><th>Delivered Back</th></tr>';
    if(!rows.length){tb.innerHTML='<tr><td colspan="10" class="empty">No results</td></tr>';return;}
    tb.innerHTML=rows.map(function(r){
      var diffColor=Math.abs(r.diff||0)<0.01?'var(--green)':'var(--red)';
      return '<tr><td>'+matchBadge(r.match_type)+'</td><td><code>'+esc(r.tracking||r.reference||'—')+'</code></td>'+
        '<td><code>'+esc(r.request_ref||'—')+'</code></td>'+
        '<td>'+stateBadge(r.state)+'</td>'+
        '<td class="amount">'+(Number(r.recorded_fee||0)>0?'₱'+fmtNum(r.recorded_fee):'<span style="color:var(--dim)">—</span>')+'</td>'+
        '<td class="amount">'+(Math.abs(r.debit_amount||0)>0?'<span style="color:var(--red)">−₱'+fmtNum(Math.abs(r.debit_amount))+'</span>':'<span style="color:var(--dim)">—</span>')+'</td>'+
        '<td class="amount">'+(r.csv_amount==null?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.csv_amount))+'</td>'+
        '<td class="amount" style="color:'+diffColor+'">'+(r.diff==null?'—':((r.diff>=0?'+':'')+fmtNum(r.diff)))+'</td>'+
        '<td>'+esc(r.pickup_at||'—')+'</td><td>'+esc(r.delivered_at||'—')+'</td></tr>';
    }).join('');
    return;
  }
  head.innerHTML='<tr><th>Match</th><th>Tracking #</th><th class="amount">CSV Bill Amt</th><th class="amount">Our Fee</th><th class="amount">Diff</th><th>File Date</th><th>Fee Status</th><th>Leg ID</th></tr>';
  if(!rows.length){tb.innerHTML='<tr><td colspan="8" class="empty">No matching rows</td></tr>';return;}
  tb.innerHTML=rows.map(function(r){
    var diff=r.diff===null||r.diff===undefined?'—':(r.diff>0?'+'+fmtNum(r.diff):fmtNum(r.diff));
    return '<tr><td>'+matchBadge(r.match_type)+'</td><td><code>'+esc(r.tracking||r.reference)+'</code></td>'+
      '<td class="amount">'+(r.csv_amount===null||r.csv_amount===undefined?'—':'₱'+fmtNum(r.csv_amount))+'</td>'+
      '<td class="amount">'+(r.db_amount===null||r.db_amount===undefined?'—':'₱'+fmtNum(r.db_amount))+'</td>'+
      '<td class="amount">'+diff+'</td><td>'+esc(r.date||'—')+'</td>'+
      '<td>'+feeBadge(r.db_status)+'</td>'+
      '<td><code>'+esc(r.leg_id||'—')+'</code></td></tr>';
  }).join('');
}
function exportReconcileCSV(){
  if(!reconcileResults.length)return;
  var cols=['match_type','tracking','request_ref','state','recorded_fee','debit_amount','csv_amount','diff','pickup_at','delivered_at'];
  var head=['Match Type','Tracking #','Return Req','Leg State','Recorded Fee','Seller Debit','CSV Bill Amt','Diff','Pickup At','Delivered Back'];
  var lines=[head.join(',')];
  reconcileResults.forEach(function(r){
    lines.push(cols.map(function(c){
      var v=r[c]===null||r[c]===undefined?'':r[c];
      if(typeof v==='string'&&(v.indexOf(',')>-1||v.indexOf('"')>-1))v='"'+v.replace(/"/g,'""')+'"';
      return v;
    }).join(','));
  });
  var a=document.createElement('a');
  a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(lines.join('\n'));
  a.download='return_shipping_recon_'+new Date().toISOString().slice(0,10)+'.csv';
  document.body.appendChild(a);a.click();a.remove();
}
function resetReconcile(){
  csvData=[];csvHeaders=[];colMap={};reconcileResults=[];anchorStats=null;
  document.getElementById('csvUpload').value='';
  document.getElementById('anchorDateFrom').value='';
  document.getElementById('anchorDateTo').value='';
  document.getElementById('previewBox').style.display='none';
  document.getElementById('reconcileStatus').style.display='none';
  document.getElementById('reconcileStats').style.display='none';
  document.getElementById('reconcileFilters').style.display='none';
  document.getElementById('reconcileTableWrap').style.display='none';
  document.getElementById('reconcileExport').style.display='none';
}
setTimeout(function(){var today=getTodayDate();document.getElementById('dateFrom').value=today;document.getElementById('dateTo').value=today;loadData();},100);
</script>
</body>
</html>"""
