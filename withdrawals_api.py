"""Reconciliation Portal — Seller Wallet Withdrawal Recon API"""
from datetime import datetime, timedelta, timezone
import json, csv, io
import psycopg2.extras

from recon_db import get_db


# ── SLA tracking: withdrawals should settle within 2 business days ──
SLA_BUSINESS_DAYS = 2


def _business_days_between(start, end):
    """Count weekdays (Mon–Fri) between two datetimes. 0 when invalid."""
    try:
        if not start or not end or end <= start:
            return 0
        days = 0
        d = start.date()
        last = end.date()
        while d < last:
            if d.weekday() < 5:
                days += 1
            d += timedelta(days=1)
        return days
    except Exception:
        return 0


def _sla_for(row, now):
    """Return (sla_flag, business_days) for a raw withdrawal row."""
    status = row.get("status")
    created = row.get("requested_at")
    if not created:
        return ("within", 0)
    if status == "processing":
        bd = _business_days_between(created, now)
        return ("over_sla" if bd > SLA_BUSINESS_DAYS else "processing", bd)
    end = row.get("settled_at") or row.get("processed_at")
    if not end:
        return ("within", 0)
    bd = _business_days_between(created, end)
    return ("over_sla" if bd > SLA_BUSINESS_DAYS else "within", bd)


def handle_withdrawals_reconcile_api(body_json):
    """Match uploaded bank/Xendit payout CSV rows against withdrawal requests.
    CSV columns expected: reference, amount, date.
    Matches on external_reference / xendit_reference_id / xendit_disbursement_id /
    internal_transfer_id / idempotency_key. Returns matched / amount_mismatch / not_found."""
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
                SELECT wr.id, wr.short_id, wr.amount, wr.status, wr.external_reference,
                       wr.idempotency_key,
                       wr.metadata->>'xendit_reference_id' AS xendit_ref,
                       wr.metadata->>'xendit_disbursement_id' AS disb_id,
                       wr.metadata->>'internal_transfer_id' AS int_ref,
                       s.name AS seller_name,
                       wr.created_at AT TIME ZONE 'Asia/Manila' AS requested_at
                FROM public.withdrawal_request wr
                LEFT JOIN public.seller s ON s.id = wr.seller_id AND s.deleted_at IS NULL
                WHERE wr.deleted_at IS NULL
                  AND (wr.external_reference = ANY(%s) OR wr.idempotency_key = ANY(%s)
                       OR wr.metadata->>'xendit_reference_id' = ANY(%s)
                       OR wr.metadata->>'xendit_disbursement_id' = ANY(%s)
                       OR wr.metadata->>'internal_transfer_id' = ANY(%s))
            """, (refs, refs, refs, refs, refs))
            for row in cur.fetchall():
                keys = [row.get("external_reference"), row.get("xendit_ref"), row.get("disb_id"),
                        row.get("int_ref"), row.get("idempotency_key")]
                for k in keys:
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
                                "short_id": "", "status": "", "seller_name": "", "requested_at": ""})
                continue
            db_amt = float(db["amount"] or 0)
            diff = round(csv_amt - db_amt, 2)
            match_type = "matched" if abs(diff) < 0.01 else "amount_mismatch"
            results.append({"reference": ref, "csv_amount": csv_amt, "db_amount": db_amt,
                            "diff": diff, "match_type": match_type, "date": r.get("date", ""),
                            "short_id": db["short_id"], "status": db["status"],
                            "seller_name": db["seller_name"] or "",
                            "requested_at": db["requested_at"].strftime("%Y-%m-%d %H:%M") if db["requested_at"] else ""})
        return 200, "application/json", json.dumps({"results": results}).encode(), True
    except Exception as e:
        return 500, "application/json", json.dumps({"error": str(e)}).encode(), True

_WD_ANCHOR_STATUSES = ('COMPLETED', 'FAILED', 'PROCESSING')


def _normalize_wd_statuses(raw):
    """Normalize executionStatus (str | list). '' / 'ALL' -> all; 'COMPLETED_FAILED'
    (legacy) -> ['COMPLETED', 'FAILED']; empty/None -> same default."""
    if isinstance(raw, str):
        raw = raw.strip()
        if raw in ('', 'ALL'):
            statuses = list(_WD_ANCHOR_STATUSES)
        elif raw == 'COMPLETED_FAILED':
            statuses = ['COMPLETED', 'FAILED']
        else:
            statuses = [raw]
    elif isinstance(raw, (list, tuple)):
        statuses = [str(s).strip() for s in raw if str(s).strip()]
    else:
        statuses = []
    if not statuses:
        statuses = ['COMPLETED', 'FAILED']
    for s in statuses:
        if s not in _WD_ANCHOR_STATUSES:
            return None
    return statuses


def handle_withdrawals_reconcile_anchor_api(body_json):
    """Ledger-anchored seller wallet recon: anchor = ALL withdrawal requests in a date
    range (+ status) from OUR DB. Optional payout/bank CSV (reference, amount) is evidence:
    verdicts matched / amount_mismatch / missing_from_csv; CSV refs with no request ->
    not_in_ledger extras. Match keys: external_reference / xendit_reference_id /
    xendit_disbursement_id / internal_transfer_id / idempotency_key."""
    try:
        date_from = str(body_json.get('dateFrom', '') or '').strip()
        date_to = str(body_json.get('dateTo', '') or '').strip()
        statuses = _normalize_wd_statuses(body_json.get('executionStatus', 'COMPLETED_FAILED'))
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

        status_clause = ""
        if len(statuses) < len(_WD_ANCHOR_STATUSES):
            status_clause = "AND wr.status = ANY(%s)"
            params = [date_from, date_to, [s.lower() for s in statuses]]
        else:
            params = [date_from, date_to]

        sql = """
            SELECT
                wr.id AS withdrawal_id,
                wr.short_id,
                wr.amount,
                wr.status,
                COALESCE(wr.external_reference, '') AS external_reference,
                COALESCE(wr.idempotency_key, '') AS idempotency_key,
                COALESCE(wr.metadata->>'xendit_reference_id', '') AS xendit_ref,
                COALESCE(wr.metadata->>'xendit_disbursement_id', '') AS disb_id,
                COALESCE(wr.metadata->>'internal_transfer_id', '') AS int_ref,
                COALESCE(s.name, 'Unknown') AS seller_name,
                (wr.created_at AT TIME ZONE 'Asia/Manila')::timestamp AS requested_at
            FROM public.withdrawal_request wr
            LEFT JOIN public.seller s ON s.id = wr.seller_id AND s.deleted_at IS NULL
            WHERE wr.deleted_at IS NULL
              AND (wr.created_at AT TIME ZONE 'Asia/Manila')::date BETWEEN %s AND %s
              {status_clause}
            ORDER BY requested_at
        """.format(status_clause=status_clause)

        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        db_rows = cur.fetchall()
        cur.close()
        conn.close()

        # CSV evidence index
        csv_by_ref = {}
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
            for k in (row['external_reference'], row['idempotency_key'], row['xendit_ref'], row['disb_id'], row['int_ref']):
                if k:
                    all_db_keys.add(k)

        out_rows = []
        matched = missing = mismatch = 0
        matched_amt = missing_amt = mismatch_amt = 0.0
        for row in db_rows:
            amt = float(row['amount'] or 0)
            d = {
                'withdrawal_id': row['withdrawal_id'],
                'short_id': row['short_id'],
                'requested_at': row['requested_at'].strftime('%Y-%m-%d %H:%M:%S') if row['requested_at'] else '',
                'amount': amt,
                'status': row['status'] or '',
                'seller_name': row['seller_name'] or '',
            }
            csv_hit = None
            for k in (row['external_reference'], row['xendit_ref'], row['disb_id'], row['int_ref'], row['idempotency_key']):
                if k and k in csv_by_ref:
                    csv_hit = csv_by_ref[k]
                    break
            if csv_hit is None:
                d['verdict'] = 'missing'
                d['csv_amount'] = None
                d['diff'] = None
                missing += 1
                missing_amt += amt
            else:
                csv_total = round(csv_hit['total'], 2)
                diff = round(csv_total - amt, 2)
                d['csv_amount'] = csv_total
                d['diff'] = diff
                if abs(diff) < 0.01:
                    d['verdict'] = 'matched'
                    matched += 1
                    matched_amt += amt
                else:
                    d['verdict'] = 'amount_mismatch'
                    mismatch += 1
                    mismatch_amt += amt
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
    CASE WHEN wr.metadata->>'settled_at' IS NOT NULL AND wr.metadata->>'settled_at' <> '' THEN (wr.metadata->>'settled_at')::timestamptz AT TIME ZONE 'Asia/Manila' END AS settled_at,
    COALESCE((wr.metadata->>'net_amount')::numeric, wr.amount) AS net_amount,
    COALESCE((wr.metadata->>'transaction_fee')::numeric, 0) AS transaction_fee,
    COALESCE(wr.metadata->>'xendit_disbursement_id', '—') AS xendit_disbursement_id,
    COALESCE(wr.metadata->>'internal_transfer_id', '—') AS internal_transfer_id,
    COALESCE(wr.metadata->>'reconciliation_needed', '') AS reconciliation_needed,
    CASE WHEN wr.metadata->>'flagged_at' IS NOT NULL AND wr.metadata->>'flagged_at' <> '' THEN (wr.metadata->>'flagged_at')::timestamptz AT TIME ZONE 'Asia/Manila' END AS flagged_at,
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
    COALESCE(SUM(CASE WHEN wr.status = 'failed' THEN 1 ELSE 0 END), 0) AS failed_count,
    COALESCE(SUM(CASE WHEN wr.status = 'completed' THEN wr.amount ELSE 0 END), 0) AS completed_amount,
    COALESCE(SUM(CASE WHEN wr.status = 'failed' THEN wr.amount ELSE 0 END), 0) AS failed_amount,
    COALESCE(SUM(CASE WHEN wr.status = 'processing' THEN wr.amount ELSE 0 END), 0) AS processing_amount,
    COALESCE(SUM(COALESCE((wr.metadata->>'transaction_fee')::numeric, 0)), 0) AS total_fees,
    COALESCE(SUM(CASE WHEN wr.metadata->>'reconciliation_needed' = 'true' THEN 1 ELSE 0 END), 0) AS recon_needed_count,
    COALESCE(SUM(CASE WHEN wr.metadata->>'flagged_at' IS NOT NULL AND wr.metadata->>'flagged_at' <> '' THEN 1 ELSE 0 END), 0) AS flagged_count,
    AVG(CASE WHEN wr.processed_at IS NOT NULL THEN EXTRACT(EPOCH FROM (wr.processed_at - wr.created_at))/3600 END) AS avg_process_hours,
    AVG(CASE WHEN wr.metadata->>'settled_at' IS NOT NULL AND wr.metadata->>'settled_at' <> '' THEN EXTRACT(EPOCH FROM ((wr.metadata->>'settled_at')::timestamptz - wr.created_at))/3600 END) AS avg_settle_hours
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
        sla_filter = query_params.get("sla", [""])[0]
        exception = query_params.get("exception", [""])[0]
        now = datetime.now(timezone.utc)

        conditions = []
        params = []

        # Filter on the MANILA date of created_at — the board displays requested_at
        # in Asia/Manila (same leak as the order Download Board, fixed Sep 3, 2026).
        if date_from:
            conditions.append("(wr.created_at AT TIME ZONE 'Asia/Manila')::date >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("(wr.created_at AT TIME ZONE 'Asia/Manila')::date <= %s")
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

        # ── SLA lean pass: full filtered set → flags + exception filters ──
        sla_sql = f"""
SELECT wr.id, wr.created_at, wr.processed_at, wr.status,
       wr.metadata->>'settled_at' AS settled_at_str,
       wr.metadata->>'reconciliation_needed' AS recon_needed,
       wr.metadata->>'flagged_at' AS flagged_at_str
FROM public.withdrawal_request wr
LEFT JOIN public.seller s ON s.id = wr.seller_id AND s.deleted_at IS NULL
WHERE wr.deleted_at IS NULL AND {extra_where}
"""
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sla_sql, params)
        sla_rows = cur.fetchall()
        sla_map = {}
        for r in sla_rows:
            settled = None
            if r.get("settled_at_str"):
                try:
                    settled = datetime.fromisoformat(r["settled_at_str"].replace("Z", "+00:00"))
                except Exception:
                    settled = None
            proxy = {"status": r["status"], "requested_at": r["created_at"],
                     "processed_at": r["processed_at"], "settled_at": settled}
            flag, days = _sla_for(proxy, now)
            is_recon = (r.get("recon_needed") == "true")
            is_flagged = bool(r.get("flagged_at_str"))
            sla_map[r["id"]] = {"sla_flag": flag, "sla_days": days,
                                 "is_recon": is_recon, "is_flagged": is_flagged}

        def _row_passes(row_id):
            m = sla_map.get(row_id, {})
            if sla_filter and m.get("sla_flag") != sla_filter:
                return False
            if exception == "recon" and not m.get("is_recon"):
                return False
            if exception == "flagged" and not m.get("is_flagged"):
                return False
            return True

        sla_counts = {"sla_over_count": 0, "sla_processing_count": 0, "sla_within_count": 0}
        for m in sla_map.values():
            f = m["sla_flag"]
            if f == "over_sla":
                sla_counts["sla_over_count"] += 1
            elif f == "processing":
                sla_counts["sla_processing_count"] += 1
            else:
                sla_counts["sla_within_count"] += 1

        if export_csv:
            data_sql = f"{_BASE_SQL} AND {extra_where} ORDER BY wr.created_at DESC LIMIT 5000"
            cur.execute(data_sql, params)
            rows = cur.fetchall()
            rows = [r for r in rows if _row_passes(r["withdrawal_id"])]
            if not rows:
                return 200, "text/csv", b"", True
            for r in rows:
                m = sla_map.get(r["withdrawal_id"], {})
                r["sla_flag"] = m.get("sla_flag", "within")
                r["sla_days"] = m.get("sla_days", 0)
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
                "settled_at": "Settled At",
                "net_amount": "Net Amount",
                "transaction_fee": "Transaction Fee",
                "xendit_disbursement_id": "Xendit Disbursement ID",
                "internal_transfer_id": "Internal Transfer ID",
                "reconciliation_needed": "Recon Needed",
                "flagged_at": "Flagged At",
                "sla_flag": "SLA",
                "sla_days": "SLA Business Days",
            }
            headers = [col_map.get(d[0], d[0]) for d in cur.description] + ["SLA", "SLA Business Days"]
            writer.writerow(headers)
            for r in rows:
                writer.writerow([str(v) if v is not None else "" for v in r.values()])
            return 200, "text/csv", output.getvalue().encode(), True

        # Count
        count_sql = f"SELECT COUNT(*) AS total FROM ({_BASE_SQL} AND {extra_where}) sub"
        cur.execute(count_sql, params)
        total = cur.fetchone()["total"]
        if sla_filter or exception:
            total = sum(1 for r in sla_rows if _row_passes(r["id"]))

        # Data
        data_sql = f"{_BASE_SQL} AND {extra_where} ORDER BY wr.created_at DESC LIMIT %s OFFSET %s"
        offset = (page - 1) * page_size
        cur.execute(data_sql, params + [page_size, offset])
        rows = cur.fetchall()
        if sla_filter or exception:
            rows = [r for r in rows if _row_passes(r["withdrawal_id"])]
            rows = rows[(page - 1) * page_size: page * page_size]
        for r in rows:
            m = sla_map.get(r["withdrawal_id"], {})
            r["sla_flag"] = m.get("sla_flag", "within")
            r["sla_days"] = m.get("sla_days", 0)
        rows = _serialize(rows)

        # Stats
        stats_sql = f"{_STATS_SQL} AND {extra_where}"
        cur.execute(stats_sql, params)
        stats = _serialize([cur.fetchone()])[0]
        stats.update(sla_counts)
        stats["failure_rate"] = round(stats["failed_count"] / stats["total_withdrawals"] * 100, 1) if stats["total_withdrawals"] else 0
        # Top failure reason (of failed requests in the filtered set)
        top_sql = ("SELECT COALESCE(NULLIF(wr.rejection_reason, ''), '(no reason given)') AS reason, COUNT(*) n "
                   "FROM public.withdrawal_request wr "
                   "LEFT JOIN public.seller s ON s.id = wr.seller_id AND s.deleted_at IS NULL "
                   f"WHERE wr.deleted_at IS NULL AND wr.status = 'failed' AND {extra_where} "
                   "GROUP BY 1 ORDER BY n DESC LIMIT 1")
        try:
            cur.execute(top_sql, params)
            top = cur.fetchone()
            stats["top_failure_reason"] = top["reason"] if top else "—"
            stats["top_failure_count"] = top["n"] if top else 0
        except Exception:
            stats["top_failure_reason"] = "—"
            stats["top_failure_count"] = 0

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
  .filter-group input, .filter-group select { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 8px 12px; border-radius: 6px; font-size: 13px; min-width: 160px; }
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
  .status-completed { background: rgba(0,175,160,.15); color: var(--green); }
  .status-processing { background: rgba(0,175,160,.15); color: var(--accent); }
  .status-failed { background: rgba(239,68,68,.15); color: var(--red); }
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
  <h1>🏦 Wallet Withdrawal Reconciliation</h1>
  <div class="nav"><a href="/recon/" class="btn btn-secondary btn-sm">← Portal Home</a><span class="badge">Production DB</span></div>
</header>
<div class="container">
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('download')" id="tab-download">📋 Download Board</button>
    <button class="tab-btn" onclick="switchTab('reconcile')" id="tab-reconcile">🔄 Reconcile (Bank File)</button>
  </div>
  <div class="tab-content active" id="download-tab">
  <div class="filters">
    <div class="filter-group"><label>Date From</label><input type="date" id="dateFrom"></div>
    <div class="filter-group"><label>Date To</label><input type="date" id="dateTo"></div>
    <div class="filter-group"><label>Status</label><select id="status"><option value="">All</option><option value="completed">Completed</option><option value="processing">Processing</option><option value="failed">Failed</option></select></div>
    <div class="filter-group"><label>SLA (2 business days)</label><select id="sla"><option value="">All</option><option value="over_sla">❗ Over SLA</option><option value="processing">⏳ Processing</option><option value="within">✓ Within SLA</option></select></div>
    <div class="filter-group"><label>Exception</label><select id="exception"><option value="">All</option><option value="recon">⚠ Recon Needed</option><option value="flagged">🚩 Flagged</option></select></div>
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
        <th class="amount">Amount</th><th class="amount">Net Amount</th><th class="amount">Fee</th><th>Currency</th><th>Status</th><th>SLA</th><th>Xendit Reference</th><th>Xendit Disb ID</th>
        <th>Bank</th><th>Account #</th><th>Account Holder</th>
        <th>External Ref</th><th>Int Transfer ID</th><th>Recon</th><th>Rejection Reason</th>
        <th>Requested At</th><th>Processed At</th><th>Settled At</th><th class="amount">Wallet Balance</th>
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
            <label class="chip"><input type="checkbox" value="COMPLETED" checked>✅ Completed</label>
            <label class="chip"><input type="checkbox" value="FAILED" checked>Failed</label>
            <label class="chip"><input type="checkbox" value="PROCESSING">Processing</label>
            <span onclick="setAnchorStatuses(true)" style="font-size:11px;color:var(--accent);cursor:pointer;text-decoration:underline;">All</span>
            <span onclick="setAnchorStatuses(false)" style="font-size:11px;color:var(--accent);cursor:pointer;text-decoration:underline;margin-left:4px;">None</span>
          </div></div>
        <button class="btn btn-primary" id="runAnchorBtn" onclick="runAnchorRecon()">📒 Run Anchor Recon</button>
      </div>
      <div style="margin-top:10px;font-size:12px;color:var(--dim);line-height:1.6;">
        <b>Anchor</b> = every withdrawal request in <b>our</b> ledger for the date range + status — the completeness basis, not the payout file.<br>
        Bank/Xendit payout CSV upload above is <b>optional evidence</b>: requests missing from the CSV are flagged ❌ (completeness gap), amount differences ⚠️, CSV refs with no request ➕.<br>
        Default = terminal states (completed/failed) that should appear in the payout/bank file.
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
        <span style="color:var(--dim);font-size:12px;">Match keys: external reference, Xendit reference/disbursement IDs, internal transfer ID, idempotency key.</span>
        <div style="margin-top:8px;color:var(--dim);font-size:12px;">Tip: anchor on <b>our</b> data first — a 3rd-party file can be silently incomplete.</div>
      </div>
    </div>
    <div class="upload-zone" id="uploadZone" onclick="document.getElementById('csvUpload').click()">
      <div class="upload-icon">📁</div>
      <div class="upload-title">Upload Bank / Xendit Payout CSV</div>
      <div class="upload-hint">Drag & drop or click. Needs: Reference (Xendit ref / disbursement ID / external ref), Amount. Matches against withdrawal requests.</div>
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
      <div class="filter-group"><label>Search</label><input type="text" id="reconSearch" placeholder="Reference, Short ID, Seller" oninput="filterReconcileResults()"></div>
      <div class="filter-group"><label>Match Type</label><select id="reconMatchType" onchange="filterReconcileResults()"><option value="">All</option><option value="matched">✅ Matched</option><option value="amount_mismatch">⚠ Amount Mismatch</option><option value="not_found">❌ Not Found</option></select></div>
      <span id="reconFilterCount" style="color:var(--dim);font-size:12px;align-self:flex-end;padding-bottom:8px;"></span>
      <button class="btn btn-secondary btn-sm" onclick="document.getElementById('reconSearch').value='';document.getElementById('reconMatchType').value='';filterReconcileResults();">↺ Clear</button>
    </div>
    <div class="table-wrap" id="reconcileTableWrap" style="display:none">
      <table id="reconcileResults">
        <thead id="reconcileHead"><tr>
          <th>Match</th><th>Reference</th><th class="amount">CSV Amount</th><th class="amount">DB Amount</th><th class="amount">Diff</th><th>File Date</th><th>Short ID</th><th>DB Status</th><th>Seller</th><th>Requested At</th>
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
function getFilters(){return{
  date_from:document.getElementById('dateFrom').value,
  date_to:document.getElementById('dateTo').value,
  status:document.getElementById('status').value,
  sla:document.getElementById('sla').value,
  exception:document.getElementById('exception').value,
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
  '<div class="stat-card"><div class="value red">'+(s.failed_count||0)+'</div><div class="label">Failed ('+(s.failure_rate||0)+'%)</div></div>'+
  '<div class="stat-card"><div class="value red">₱'+fmtNum(s.failed_amount||0)+'</div><div class="label">Failed Amount</div></div>'+
  '<div class="stat-card"><div class="value" style="color:var(--red)">'+(s.sla_over_count||0)+'</div><div class="label">❗ Over SLA</div></div>'+
  '<div class="stat-card"><div class="value amber">'+(s.sla_processing_count||0)+'</div><div class="label">Still Processing</div></div>'+
  '<div class="stat-card"><div class="value">'+(s.avg_process_hours==null?'—':fmtNum(Math.round(s.avg_process_hours*10)/10)+'h')+'</div><div class="label">Avg Process Time</div></div>'+
  '<div class="stat-card"><div class="value">'+(s.avg_settle_hours==null?'—':fmtNum(Math.round(s.avg_settle_hours*10)/10)+'h')+'</div><div class="label">Avg Settle Time</div></div>'+
  '<div class="stat-card"><div class="value amber">'+(s.recon_needed_count||0)+'</div><div class="label">Recon Needed</div></div>'+
  '<div class="stat-card"><div class="value red">'+(s.flagged_count||0)+'</div><div class="label">Flagged</div></div>'+
  '<div class="stat-card"><div class="value">₱'+fmtNum(s.total_fees||0)+'</div><div class="label">Total Fees</div></div>'+
  '<div class="stat-card" style="min-width:260px"><div class="value" style="font-size:13px">'+esc(s.top_failure_reason||'—')+'</div><div class="label">Top Failure Reason ('+(s.top_failure_count||0)+')</div></div>';
}
function slaBadge(r){
  var f=r.sla_flag;
  if(f==='over_sla')return'<span class="status" style="background:rgba(255,71,87,.15);color:var(--red)">❗ '+r.sla_days+'bd</span>';
  if(f==='processing')return'<span class="status" style="background:rgba(108,140,255,.15);color:var(--accent)">⏳</span>';
  if(r.status==='completed')return'<span class="status" style="background:rgba(60,205,92,.15);color:var(--green)">✓ '+(r.sla_days||0)+'bd</span>';
  if(r.status==='failed')return'<span style="color:var(--dim)">'+r.sla_days+'bd</span>';
  return'<span style="color:var(--dim)">—</span>';
}
function reconBadge(r){
  if(r.reconciliation_needed==='true')return'<span class="status" style="background:rgba(255,165,2,.15);color:var(--amber)">⚠ Recon</span>';
  if(r.flagged_at)return'<span class="status" style="background:rgba(255,71,87,.15);color:var(--red)">🚩 Flagged</span>';
  return'<span style="color:var(--dim)">—</span>';
}
function renderTable(rows){
  var tb=document.getElementById('tbody');
  if(!rows||rows.length===0){tb.innerHTML='<tr><td colspan="24" class="empty">No withdrawals found</td></tr>';return;}
  tb.innerHTML=rows.map(function(r){return'<tr>'+
    '<td><code>'+esc(r.withdrawal_id)+'</code> <span class="copy-btn" data-copy="'+esc(r.withdrawal_id)+'" onclick="copyToClipboard(this)" title="Copy">📋</span></td><td><code>'+esc(r.short_id||'—')+'</code></td>'+
    '<td>'+esc(r.seller_name)+'</td><td>'+esc(r.seller_email)+'</td><td>'+esc(r.seller_phone)+'</td>'+
    '<td class="amount"><b>₱'+fmtNum(r.amount)+'</b></td><td class="amount">₱'+fmtNum(r.net_amount)+'</td><td class="amount">₱'+fmtNum(r.transaction_fee)+'</td><td>'+esc(r.currency)+'</td>'+
    '<td><span class="status status-'+esc(r.status)+'">'+esc(r.status)+'</span></td><td>'+slaBadge(r)+'</td>'+
    '<td><code>'+esc(r.xendit_reference||'—')+'</code></td><td><code>'+esc(r.xendit_disbursement_id||'—')+'</code></td>'+
    '<td>'+esc(r.bank_name)+'</td><td><code>'+esc(r.account_number)+'</code></td><td>'+esc(r.account_holder)+'</td>'+
    '<td>'+esc(r.external_reference)+'</td><td><code>'+esc(r.internal_transfer_id||'—')+'</code></td><td>'+reconBadge(r)+'</td>'+
    '<td style="max-width:180px;overflow:hidden;text-overflow:ellipsis" title="'+esc(r.rejection_reason||'').replace(/"/g,'&quot;')+'">'+esc(r.rejection_reason)+'</td>'+
    '<td>'+esc(r.requested_at)+'</td><td>'+esc(r.processed_at||'—')+'</td><td>'+esc(r.settled_at||'—')+'</td>'+
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
function resetFilters(){document.getElementById('dateFrom').value='';document.getElementById('dateTo').value='';document.getElementById('status').value='';document.getElementById('sla').value='';document.getElementById('exception').value='';document.getElementById('seller').value='';document.getElementById('search').value='';currentPage=1;loadData();}
function exportCSV(){var p=new URLSearchParams(getFilters());p.delete('page');p.delete('page_size');p.set('export','csv');window.open('/recon/withdrawals/api/orders?'+p,'_blank');}
function fmtNum(n){if(n===null||n===undefined)return'0.00';return Number(n).toLocaleString('en-PH',{minimumFractionDigits:2,maximumFractionDigits:2});}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function copyToClipboard(el){var text=el.getAttribute('data-copy');navigator.clipboard.writeText(text).then(function(){el.textContent='✅';setTimeout(function(){el.textContent='📋';},1500);}).catch(function(){prompt('Copy:',text);});}
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
  colMap.reference=findCol(/reference|ref.?id|disbursement|external|transaction.?id|transfer.?id|idempotency/i);
  colMap.amount=findCol(/amount|fee|value|net/i);
  colMap.date=findCol(/date|created|cleared|settled/i);
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
            amount:amtCol?parseFloat(String(r[amtCol]).replace(/[^0-9.\-]/g,''))||0:0,
            date:dtCol?(r[dtCol]||''):''};
  }).filter(function(r){return r.reference!=='';});
  fetch('/recon/withdrawals/api/reconcile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rows:rows})})
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
  var payload={dateFrom:df,dateTo:dt,executionStatus:getAnchorStatuses(['COMPLETED','FAILED']),rows:[]};
  if(csvData.length&&colMap.reference){
    var amtCol=colMap.amount;
    payload.rows=csvData.map(function(r){return {reference:String(r[colMap.reference]||'').trim(),amount:amtCol?(parseFloat(String(r[amtCol]).replace(/[^0-9.\-]/g,''))||0):0};}).filter(function(r){return r.reference!=='';});
  }
  fetch('/recon/withdrawals/api/reconcile-anchor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
  .then(function(r){return r.json();})
  .then(function(d){
    btn.disabled=false;btn.textContent='📒 Run Anchor Recon';
    if(d.error){document.getElementById('reconcile-status').innerHTML='<div class="error">'+esc(d.error)+'</div>';document.getElementById('reconcile-status').style.display='block';return;}
    anchorStats=d.stats||null;
    reconcileResults=(d.rows||[]).map(function(x){return {match_type:x.verdict,reference:x.short_id||x.withdrawal_id||'',short_id:x.short_id||'',csv_amount:x.csv_amount==null?null:x.csv_amount,db_amount:x.amount,diff:x.diff==null?null:x.diff,date:x.requested_at||'',status:x.status||'',seller_name:x.seller_name||'',ref_key:x.withdrawal_id||''};})
      .concat((d.extras||[]).map(function(x){return {match_type:'not_in_ledger',reference:x.reference||'',short_id:'',csv_amount:x.csv_amount||0,db_amount:null,diff:null,date:'',status:'',seller_name:'',ref_key:x.reference||''};}));
    document.getElementById('reconcileFilters').style.display='flex';
    filterReconcileResults();
  })
  .catch(function(e){btn.disabled=false;btn.textContent='📒 Run Anchor Recon';document.getElementById('reconcile-status').innerHTML='<div class="error">Error: '+esc(e.message)+'</div>';document.getElementById('reconcile-status').style.display='block';});
}
function filterReconcileResults(){
  var opts=reconMode==='anchor'
    ?[['','All'],['matched','✅ Matched'],['amount_mismatch','⚠ Amount Mismatch'],['missing','❌ Missing from CSV'],['not_in_ledger','➕ CSV Not in Ledger']]
    :[['','All'],['matched','✅ Matched'],['amount_mismatch','⚠ Amount Mismatch'],['not_found','❌ Not Found']];
  var sel=document.getElementById('reconMatchType');
  var cur=sel.value;
  sel.innerHTML=opts.map(function(o){return'<option value="'+o[0]+'">'+o[1]+'</option>';}).join('');
  if(opts.some(function(o){return o[0]===cur;}))sel.value=cur;else sel.value='';
  var q=document.getElementById('reconSearch').value.toLowerCase();
  var mt=sel.value;
  var shown=reconcileResults.filter(function(r){
    if(mt&&r.match_type!==mt)return false;
    if(q&&!(String(r.reference).toLowerCase().indexOf(q)>=0||String(r.short_id||'').toLowerCase().indexOf(q)>=0||String(r.seller_name||'').toLowerCase().indexOf(q)>=0))return false;
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
      '<div class="stat-card"><div class="value">'+s.anchor_total+'</div><div class="label">Anchor Requests</div></div>'+
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
  var tAmt=r.reduce(function(s,x){return s+x.csv_amount;},0);
  var tDiff=r.reduce(function(s,x){return s+Math.abs(x.diff||0);},0);
  document.getElementById('reconcileStats').innerHTML=
    '<div class="stat-card"><div class="value">'+r.length+'</div><div class="label">CSV Rows</div></div>'+
    '<div class="stat-card"><div class="value green">'+matched+'</div><div class="label">✅ Matched</div></div>'+
    '<div class="stat-card"><div class="value amber">'+mismatch+'</div><div class="label">⚠ Amount Mismatch</div></div>'+
    '<div class="stat-card"><div class="value red">'+notFound+'</div><div class="label">❌ Not Found</div></div>'+
    '<div class="stat-card"><div class="value blue">₱'+fmtNum(tAmt)+'</div><div class="label">Total CSV Amount</div></div>'+
    '<div class="stat-card"><div class="value red">₱'+fmtNum(tDiff)+'</div><div class="label">Total Variance</div></div>';
}
function renderReconcileTable(results){
  var tb=document.getElementById('reconcileTbody');
  var head=document.getElementById('reconcileHead');
  if(reconMode==='anchor'){
    head.innerHTML='<tr><th>Match</th><th>Short ID</th><th>Requested At</th><th class="amount">Amount</th><th class="amount">CSV Amt</th><th class="amount">Diff</th><th>Seller</th><th>Status</th><th>Reference</th><th>CSV Rows</th></tr>';
    if(results.length===0){tb.innerHTML='<tr><td colspan="10" class="empty">No results</td></tr>';return;}
    tb.innerHTML=results.map(function(r){
      var badge=r.match_type==='matched'?'<span class="match-badge match-matched">✅ Matched</span>'
        :r.match_type==='amount_mismatch'?'<span class="match-badge match-mismatch">⚠ Mismatch</span>'
        :r.match_type==='missing'?'<span class="match-badge match-not-found">❌ Missing from CSV</span>'
        :'<span class="match-badge match-escrow">➕ Not in Ledger</span>';
      var diffColor=Math.abs(r.diff||0)<0.01?'var(--green)':'var(--red)';
      return'<tr><td>'+badge+'</td><td><code>'+esc(r.short_id||r.reference||'—')+'</code></td><td>'+esc(r.date||'—')+'</td>'+
        '<td class="amount">'+(r.db_amount==null?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.db_amount))+'</td>'+
        '<td class="amount">'+(r.csv_amount==null?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.csv_amount))+'</td>'+
        '<td class="amount" style="color:'+diffColor+'">'+(r.diff==null?'—':((r.diff>=0?'+':'')+fmtNum(r.diff)))+'</td>'+
        '<td>'+esc(r.seller_name||'—')+'</td><td>'+esc(r.status||'—')+'</td><td><code>'+esc(r.ref_key||'—')+'</code></td><td>'+(r.csv_count||0)+'</td></tr>';
    }).join('');
    return;
  }
  head.innerHTML='<tr><th>Match</th><th>Reference</th><th class="amount">CSV Amount</th><th class="amount">DB Amount</th><th class="amount">Diff</th><th>File Date</th><th>Short ID</th><th>DB Status</th><th>Seller</th><th>Requested At</th></tr>';
  if(results.length===0){tb.innerHTML='<tr><td colspan="10" class="empty">No results</td></tr>';return;}
  tb.innerHTML=results.map(function(r){
    var badge='';
    if(r.match_type==='matched')badge='<span class="match-badge match-matched">✅ Matched</span>';
    else if(r.match_type==='amount_mismatch')badge='<span class="match-badge match-mismatch">⚠ Mismatch</span>';
    else badge='<span class="match-badge match-not-found">❌ Not Found</span>';
    var diffColor=Math.abs(r.diff||0)<0.01?'var(--green)':'var(--red)';
    return'<tr><td>'+badge+'</td><td><code>'+esc(r.reference)+'</code></td>'+
      '<td class="amount">₱'+fmtNum(r.csv_amount)+'</td>'+
      '<td class="amount">'+(r.db_amount==null?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.db_amount))+'</td>'+
      '<td class="amount" style="color:'+diffColor+'">'+(r.diff==null?'—':((r.diff>=0?'+':'')+fmtNum(r.diff)))+'</td>'+
      '<td>'+esc(r.date||'—')+'</td><td><code>'+esc(r.short_id||'—')+'</code></td>'+
      '<td>'+esc(r.status||'—')+'</td><td>'+esc(r.seller_name||'—')+'</td><td>'+esc(r.requested_at||'—')+'</td></tr>';
  }).join('');
}
function exportReconcileCSV(){
  if(reconMode==='anchor'){
    var rows=[['Match','Short ID','Requested At','Amount','CSV Amount','Diff','Seller','Status','Reference','CSV Rows']];
    reconcileResults.forEach(function(r){rows.push([r.match_type,r.short_id||r.reference||'',r.date||'',r.db_amount==null?'':r.db_amount,r.csv_amount==null?'':r.csv_amount,r.diff==null?'':r.diff,r.seller_name||'',r.status||'',r.ref_key||'',r.csv_count||0]);});
    var csv=rows.map(function(r){return r.map(function(c){return'"'+String(c==null?'':c).replace(/"/g,'""')+'"';}).join(',');}).join('\n');
    var a=document.createElement('a');a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);a.download='withdrawals-anchor-recon.csv';a.click();
    return;
  }
  var rows=[['Match','Reference','CSV Amount','DB Amount','Diff','Date','Short ID','DB Status','Seller','Requested At']];
  reconcileResults.forEach(function(r){rows.push([r.match_type,r.reference,r.csv_amount,r.db_amount==null?'':r.db_amount,r.diff==null?'':r.diff,r.date||'',r.short_id||'',r.status||'',r.seller_name||'',r.requested_at||'']);});
  var csv=rows.map(function(r){return r.map(function(c){return'"'+String(c==null?'':c).replace(/"/g,'""')+'"';}).join(',');}).join('\n');
  var a=document.createElement('a');a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);a.download='withdrawals-reconcile-report.csv';a.click();
}
function getTodayDate(){var today=new Date();var y=today.getFullYear();var m=String(today.getMonth()+1).padStart(2,'0');var d=String(today.getDate()).padStart(2,'0');return y+'-'+m+'-'+d;}
// Load sellers on page load
fetch('/recon/withdrawals/api/orders?page=1').then(function(r){return r.json();}).then(function(d){
  if(d.sellers){var sel=document.getElementById('seller');d.sellers.forEach(function(s){var o=document.createElement('option');o.value=s;o.textContent=s;sel.appendChild(o);});}
}).catch(function(){});
setTimeout(function(){switchReconMode('anchor');var today=getTodayDate();document.getElementById('dateFrom').value=today;document.getElementById('dateTo').value=today;loadData();},100);
</script>
</body>
</html>"""
