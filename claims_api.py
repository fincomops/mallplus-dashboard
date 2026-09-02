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


def handle_claims_reconcile_api(body_json):
    """Match uploaded 3PL claims CSV rows against our claims ledger.
    Match keys: tracking_number, order # (order_sn), claim id.
    Verdicts: matched / amount_mismatch / status_mismatch / not_found."""
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
                SELECT c.id AS claim_id, COALESCE(oe.order_sn, o.id) AS order_id,
                       c.tracking_number, c.status AS claim_status,
                       COALESCE(pc.amount, 0) AS order_payment
                FROM public.three_pl_claim c
                LEFT JOIN public.order_extension oe ON oe.order_id = c.order_id
                LEFT JOIN public."order" o ON o.id = c.order_id
                LEFT JOIN LATERAL (
                    SELECT pc2.amount FROM public.order_payment_collection opc2
                    JOIN public.payment_collection pc2 ON pc2.id = opc2.payment_collection_id AND pc2.deleted_at IS NULL
                    WHERE opc2.order_id = o.id AND opc2.deleted_at IS NULL
                    LIMIT 1
                ) pc ON true
                WHERE c.deleted_at IS NULL
                  AND (c.tracking_number = ANY(%s) OR c.id = ANY(%s)
                       OR COALESCE(oe.order_sn, o.id) = ANY(%s))
            """, (refs, refs, refs))
            for row in cur.fetchall():
                for k in (row.get("tracking_number"), row.get("claim_id"), row.get("order_id")):
                    if k:
                        db_index.setdefault(str(k), row)
            cur.close()
            conn.close()

        results = []
        for r in rows:
            ref = str(r.get("reference", "") or "").strip()
            if not ref:
                continue
            raw_amt = r.get("amount")
            csv_amt = float(raw_amt) if raw_amt not in (None, "") else None
            csv_status = str(r.get("status") or "").strip() or None
            db = db_index.get(ref)
            if not db:
                results.append({"reference": ref, "csv_amount": csv_amt, "db_amount": None,
                                "diff": None, "date": r.get("date", ""),
                                "csv_status": csv_status, "db_status": "",
                                "claim_id": "", "tracking": "", "match_type": "not_found"})
                continue
            db_amt = float(db.get("order_payment") or 0)
            verdict = "matched"
            if csv_amt is not None and abs(csv_amt - db_amt) > 0.009:
                verdict = "amount_mismatch"
            elif csv_status and csv_status.lower() != str(db.get("claim_status") or "").lower():
                verdict = "status_mismatch"
            results.append({"reference": ref, "csv_amount": csv_amt, "db_amount": db_amt,
                            "diff": (round(csv_amt - db_amt, 2) if csv_amt is not None else None),
                            "date": r.get("date", ""), "csv_status": csv_status or "",
                            "db_status": db.get("claim_status") or "",
                            "claim_id": db.get("claim_id") or "",
                            "tracking": db.get("tracking_number") or "",
                            "match_type": verdict})

        total = len(results)
        matched = sum(1 for x in results if x["match_type"] == "matched")
        amt = sum(1 for x in results if x["match_type"] == "amount_mismatch")
        st = sum(1 for x in results if x["match_type"] == "status_mismatch")
        nf = sum(1 for x in results if x["match_type"] == "not_found")
        csv_total = sum(x["csv_amount"] or 0 for x in results)
        db_total = sum(x["db_amount"] or 0 for x in results)
        return 200, "application/json", json.dumps({
            "results": results,
            "stats": {"total": total, "matched": matched, "amount_mismatch": amt,
                       "status_mismatch": st, "not_found": nf,
                       "completeness": round(matched * 100.0 / total, 1) if total else 0,
                       "csv_amount_total": round(csv_total, 2),
                       "db_amount_total": round(db_total, 2)},
        }).encode(), True
    except Exception as e:
        return 500, "application/json", json.dumps({"error": str(e)}).encode(), True


CLAIM_STATUSES = ["Lost", "Damaged", "Breached"]


def handle_claims_reconcile_anchor_api(body_json):
    """Ledger-anchored claims recon: anchor = ALL claims in a date range (+ exception
    status) from OUR ledger. Optional 3PL claims CSV (tracking/order/claim id, amount,
    status) is evidence: verdicts matched / amount_mismatch / status_mismatch /
    missing_from_csv; CSV refs with no claim -> not_in_ledger extras.
    Match keys: tracking_number, order #, claim id."""
    try:
        date_from = str(body_json.get("dateFrom", "") or "").strip()
        date_to = str(body_json.get("dateTo", "") or "").strip()
        statuses = body_json.get("statuses") or CLAIM_STATUSES
        rows = body_json.get("rows") or []

        if not date_from or not date_to:
            return 400, "application/json", json.dumps({"error": "dateFrom and dateTo required"}).encode(), True
        from datetime import datetime as _dt
        try:
            _dt.strptime(date_from, "%Y-%m-%d")
            _dt.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            return 400, "application/json", json.dumps({"error": "dates must be YYYY-MM-DD"}).encode(), True

        status_clause = ""
        params = [date_from, date_to]
        if statuses and len(statuses) < len(CLAIM_STATUSES):
            status_clause = "AND ({status_expr}) = ANY(%s)".format(status_expr=_STATUS_EXPR)
            params.append([str(s).strip() for s in statuses])

        sql = f"""
            SELECT
                c.id AS claim_id,
                COALESCE(oe.order_sn, o.id) AS order_id,
                c.tracking_number,
                {_STATUS_EXPR} AS status,
                COALESCE(c.status, '') AS workflow_status,
                COALESCE(pc.amount, 0) AS order_payment,
                (c.created_at AT TIME ZONE 'Asia/Manila')::timestamp AS claim_date
            FROM public.three_pl_claim c
            LEFT JOIN public.order_extension oe ON oe.order_id = c.order_id
            LEFT JOIN public."order" o ON o.id = c.order_id
            LEFT JOIN LATERAL (
                SELECT pc2.amount FROM public.order_payment_collection opc2
                JOIN public.payment_collection pc2 ON pc2.id = opc2.payment_collection_id AND pc2.deleted_at IS NULL
                WHERE opc2.order_id = o.id AND opc2.deleted_at IS NULL
                LIMIT 1
            ) pc ON true
            WHERE c.deleted_at IS NULL
              AND (c.created_at AT TIME ZONE 'Asia/Manila')::date BETWEEN %s AND %s
              {status_clause}
            ORDER BY claim_date
        """

        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        db_rows = cur.fetchall()
        cur.close()
        conn.close()

        # CSV evidence index (ref -> {total amount, n, status})
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
            entry = csv_by_ref.setdefault(ref, {"total": 0.0, "n": 0, "status": str(r.get("status") or "").strip()})
            entry["total"] += amt
            entry["n"] += 1

        all_db_keys = set()
        for row in db_rows:
            for k in (row.get("tracking_number"), row.get("claim_id"), row.get("order_id")):
                if k:
                    all_db_keys.add(str(k))

        out_rows = []
        matched = missing = amt_mismatch = st_mismatch = 0
        matched_amt = missing_amt = mismatch_amt = 0.0
        for row in db_rows:
            amt = float(row.get("order_payment") or 0)
            d = {
                "claim_id": row.get("claim_id") or "",
                "order_id": row.get("order_id") or "",
                "tracking": row.get("tracking_number") or "",
                "status": row.get("status") or "",
                "workflow_status": row.get("workflow_status") or "",
                "amount": amt,
                "claim_date": row["claim_date"].strftime("%Y-%m-%d %H:%M:%S") if row.get("claim_date") else "",
            }
            csv_hit = None
            for k in (row.get("tracking_number"), row.get("claim_id"), row.get("order_id")):
                if k and str(k) in csv_by_ref:
                    csv_hit = csv_by_ref[str(k)]
                    break
            if csv_hit is None:
                d["verdict"] = "missing"
                d["csv_amount"] = None
                d["diff"] = None
                missing += 1
                missing_amt += amt
            else:
                csv_total = round(csv_hit["total"], 2)
                diff = round(csv_total - amt, 2)
                d["csv_amount"] = csv_total
                d["diff"] = diff
                csv_status = csv_hit.get("status") or ""
                if abs(diff) >= 0.01:
                    d["verdict"] = "amount_mismatch"
                    amt_mismatch += 1
                    mismatch_amt += amt
                elif csv_status and csv_status.lower() != str(row.get("status") or "").lower():
                    d["verdict"] = "status_mismatch"
                    st_mismatch += 1
                else:
                    d["verdict"] = "matched"
                    matched += 1
                    matched_amt += amt
            out_rows.append(d)

        extras = []
        for ref, info in csv_by_ref.items():
            if ref not in all_db_keys:
                extras.append({"reference": ref, "csv_amount": round(info["total"], 2), "csv_count": info["n"], "csv_status": info.get("status") or ""})

        anchor_total = len(out_rows)
        stats = {
            "anchor_total": anchor_total,
            "anchor_amount": round(sum(r["amount"] for r in out_rows), 2),
            "matched": matched,
            "matched_amount": round(matched_amt, 2),
            "missing": missing,
            "missing_amount": round(missing_amt, 2),
            "mismatch": amt_mismatch,
            "mismatch_amount": round(mismatch_amt, 2),
            "status_mismatch": st_mismatch,
            "extras": len(extras),
            "extras_amount": round(sum(e["csv_amount"] for e in extras), 2),
            "completeness_pct": round(matched / anchor_total * 100, 2) if anchor_total else 100.0,
            "csv_evidence": bool(rows),
        }
        return 200, "application/json", json.dumps({"stats": stats, "rows": out_rows, "extras": extras}).encode(), True
    except Exception as e:
        return 500, "application/json", json.dumps({"error": str(e)}).encode(), True


def serve_claims_portal():
    """Serve the Claims Reconciliation page."""
    return _HTML_TEMPLATE


_HTML_TEMPLATE = r"""<!DOCTYPE html>
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
  .match-status_mismatch{background:rgba(124,58,237,.12);color:var(--purple);}
  .match-not_found{background:rgba(239,68,68,.12);color:var(--red);}
  .match-escrow{background:rgba(37,99,235,.12);color:#2563EB;}
</style>
</head>
<body>
<header>
  <div class="nav"><a href="/recon/" class="btn btn-secondary btn-sm">← Portal Home</a><span class="badge">Claims</span></div>
  <h1>💼 Claims Reconciliation</h1>
  <div class="nav"><span class="badge">Production DB</span></div>
</header>
<div class="container">
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('download')" id="tab-download">📋 Download Board</button>
    <button class="tab-btn" onclick="switchTab('reconcile')" id="tab-reconcile">🔄 Reconcile (3PL File)</button>
  </div>
  <div class="tab-content active" id="download-tab">
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
        <div class="filter-group"><label>Anchor Status</label>
          <div id="anchorStatusChips" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
            <label class="chip"><input type="checkbox" value="Lost" checked>🔴 Lost</label>
            <label class="chip"><input type="checkbox" value="Damaged" checked>🟠 Damaged</label>
            <label class="chip"><input type="checkbox" value="Breached" checked>🟣 Breached</label>
            <span onclick="setAnchorStatuses(true)" style="font-size:11px;color:var(--accent);cursor:pointer;text-decoration:underline;">All</span>
            <span onclick="setAnchorStatuses(false)" style="font-size:11px;color:var(--accent);cursor:pointer;text-decoration:underline;margin-left:4px;">None</span>
          </div></div>
        <button class="btn btn-primary" id="runAnchorBtn" onclick="runAnchorRecon()">📒 Run Anchor Recon</button>
      </div>
      <div style="margin-top:10px;font-size:12px;color:var(--dim);line-height:1.6;">
        <b>Anchor</b> = every claim in <b>our</b> ledger for the date range + status — the completeness basis, not the 3PL file.<br>
        Upload the 3PL claims CSV above as <b>optional evidence</b>: claims missing from the CSV are flagged ❌ (completeness gap), amount/status differences ⚠️🔶, CSV refs with no claim ➕.<br>
        Default = all statuses (lost / damaged / breached).
      </div>
    </div>
    <div class="guide-panel" id="guidePanel" style="display:none">
      <b>📖 How to use this recon tool</b>
      <div style="margin-top:8px;">
        <b style="color:var(--accent)">📄 CSV-Based Recon:</b> upload the 3PL claim file (e.g. J&amp;T claim status export) and match it against our claims ledger.
        <ul style="margin:6px 0 10px 18px;padding:0;">
          <li><b>Tracking #</b> (or Order # / Claim ID) column is required — matched against our claim records.</li>
          <li><b>Amount</b> column optional — compared against the order payment (value at risk).</li>
          <li><b>Status</b> column optional — compared against our claim workflow status.</li>
        </ul>
        <b>Reading the results:</b>
        <ul style="margin:6px 0 10px 18px;padding:0;">
          <li>✅ <b>Matched</b> — claim found and amounts/status agree.</li>
          <li>⚠️ <b>Amount Mismatch</b> — claim found but the amount differs (see Diff column).</li>
          <li>🔶 <b>Status Mismatch</b> — claim found but statuses differ.</li>
          <li>❌ <b>Not Found</b> — in the file but no matching claim in our ledger.</li>
        </ul>
        <b style="color:var(--accent)">📒 Ledger Anchor mode (recommended):</b>
        <ol style="margin:6px 0 10px 18px;padding:0;">
          <li>Set <b>Date From / To</b> (Manila) and tick the <b>statuses</b> to cover — the anchor = every matching claim in <b>our</b> ledger.</li>
          <li>(Optional) Upload the 3PL claims file (CSV) as evidence.</li>
          <li>Click <b>📒 Run Anchor Recon</b>.</li>
        </ol>
        <b>Reading anchor results:</b>
        <ul style="margin:6px 0 10px 18px;padding:0;">
          <li>✅ <b>Matched</b> — in our ledger and the file agrees.</li>
          <li>⚠️/🔶 <b>Amount/Status Mismatch</b> — found but differs.</li>
          <li>❌ <b>Missing from CSV</b> — in our ledger but absent from the file = <b>completeness gap</b>.</li>
          <li>➕ <b>Not in Ledger</b> — in the file but no claim in our records.</li>
        </ul>
        <b>Completeness %</b> = matched share of the anchor. Use <b>📥 Export</b> to pull exceptions.<br>
        <span style="color:var(--dim);font-size:12px;">Match keys: tracking number, order #, claim ID. Tip: anchor on <b>our</b> data first — a 3rd-party file can be silently incomplete.</span>
      </div>
    </div>
    <div class="upload-zone" id="uploadZone" onclick="document.getElementById('csvUpload').click()">
      <div class="upload-icon">📁</div>
      <div class="upload-title">Upload 3PL Claims CSV</div>
      <div class="upload-hint">Drag &amp; drop or click. Needs: Tracking # (or Order # / Claim ID). Optional: Amount, Status, Date.</div>
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
      <div class="filter-group"><label>Search</label><input type="text" id="reconSearch" placeholder="Tracking, Order #, Claim ID" oninput="filterReconcileResults()"></div>
      <div class="filter-group"><label>Match Type</label><select id="reconMatchType" onchange="filterReconcileResults()"><option value="">All</option><option value="matched">✅ Matched</option><option value="amount_mismatch">⚠️ Amount Mismatch</option><option value="status_mismatch">🔶 Status Mismatch</option><option value="not_found">❌ Not Found</option></select></div>
      <span id="reconFilterCount" style="color:var(--dim);font-size:12px;align-self:flex-end;padding-bottom:8px;"></span>
      <button class="btn btn-secondary btn-sm" onclick="document.getElementById('reconSearch').value='';document.getElementById('reconMatchType').value='';filterReconcileResults();">↺ Clear</button>
    </div>
    <div class="table-wrap" id="reconcileTableWrap" style="display:none">
      <table id="reconcileResults">
        <thead id="reconcileHead"><tr>
          <th>Match</th><th>Reference</th><th class="amount">CSV Amount</th><th class="amount">DB Order Payment</th><th class="amount">Diff</th><th>File Date</th><th>CSV Status</th><th>DB Status</th><th>Claim ID</th><th>Tracking #</th>
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
  document.getElementById('reconcileStatus').style.display='none';
  var payload={dateFrom:df,dateTo:dt,statuses:getAnchorStatuses(['Lost','Damaged','Breached']),rows:[]};
  if(csvData.length&&colMap.reference){
    var amtCol=colMap.amount,stCol=colMap.status;
    payload.rows=csvData.map(function(r){
      return {reference:String(r[colMap.reference]||'').trim(),
              amount:amtCol?(parseFloat(String(r[amtCol]).replace(/[^0-9.\-]/g,''))||0):0,
              status:stCol?String(r[stCol]||'').trim():''};
    }).filter(function(r){return r.reference!=='';});
  }
  fetch('/recon/claims/api/reconcile-anchor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
  .then(function(r){return r.json();})
  .then(function(d){
    btn.disabled=false;btn.textContent='📒 Run Anchor Recon';
    if(d.error){document.getElementById('reconcileStatus').className='recon-status warn';document.getElementById('reconcileStatus').textContent='Error: '+d.error;document.getElementById('reconcileStatus').style.display='block';return;}
    anchorStats=d.stats||null;
    reconcileResults=(d.rows||[]).map(function(x){
      return {match_type:x.verdict,reference:x.claim_id||'',csv_amount:x.csv_amount==null?null:x.csv_amount,db_amount:x.amount,diff:x.diff==null?null:x.diff,date:x.claim_date||'',csv_status:'',db_status:x.workflow_status||'',claim_id:x.claim_id||'',tracking:x.tracking||'',status:x.status||''};
    }).concat((d.extras||[]).map(function(x){
      return {match_type:'not_in_ledger',reference:x.reference||'',csv_amount:x.csv_amount||0,db_amount:null,diff:null,date:'',csv_status:x.csv_status||'',db_status:'',claim_id:'',tracking:'',status:''};
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
    colMap.reference=findCol(/tracking|track.?no|awb|parcel|order.?no|order|claim.?id|reference/i);
    colMap.amount=findCol(/amount|value|claim.?amt|fee|payment/i);
    colMap.status=findCol(/status|state|stage/i);
    colMap.date=findCol(/date|created|filed|submitted/i);
    if(!colMap.reference){alert('Could not detect a Tracking / Order # / Reference column.');return;}
    var mapHtml='<div class="mapping-item">Reference: <b>'+esc(colMap.reference)+'</b></div>'+
      (colMap.amount?'<div class="mapping-item">Amount: <b>'+esc(colMap.amount)+'</b></div>':'')+
      (colMap.status?'<div class="mapping-item">Status: <b>'+esc(colMap.status)+'</b></div>':'')+
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
    if(colMap.status)o.status=(r[colMap.status]||'').trim();
    if(colMap.date)o.date=(r[colMap.date]||'').trim();
    return o;
  });
  document.getElementById('reconcileStatus').style.display='none';
  fetch('/recon/claims/api/reconcile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rows:rows})})
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
  var map={'matched':'match-matched','amount_mismatch':'match-amount_mismatch','status_mismatch':'match-status_mismatch','not_found':'match-not_found','missing':'match-not_found','not_in_ledger':'match-escrow'};
  var label={'matched':'✅ Matched','amount_mismatch':'⚠️ Amount','status_mismatch':'🔶 Status','not_found':'❌ Not Found','missing':'❌ Missing from CSV','not_in_ledger':'➕ Not in Ledger'};
  return '<span class="match-badge '+(map[m]||'')+'">'+(label[m]||m)+'</span>';
}
function filterReconcileResults(){
  var opts=reconMode==='anchor'
    ?[['','All'],['matched','✅ Matched'],['amount_mismatch','⚠️ Amount Mismatch'],['status_mismatch','🔶 Status Mismatch'],['missing','❌ Missing from CSV'],['not_in_ledger','➕ Not in Ledger']]
    :[['','All'],['matched','✅ Matched'],['amount_mismatch','⚠️ Amount Mismatch'],['status_mismatch','🔶 Status Mismatch'],['not_found','❌ Not Found']];
  var sel=document.getElementById('reconMatchType');
  var cur=sel.value;
  sel.innerHTML=opts.map(function(o){return'<option value="'+o[0]+'">'+o[1]+'</option>';}).join('');
  if(opts.some(function(o){return o[0]===cur;}))sel.value=cur;else sel.value='';
  var q=document.getElementById('reconSearch').value.toLowerCase();
  var mt=sel.value;
  var shown=reconcileResults.filter(function(r){
    if(mt&&r.match_type!==mt)return false;
    if(q){var hay=(r.reference+' '+r.claim_id+' '+r.tracking+' '+(r.db_status||'')).toLowerCase();if(hay.indexOf(q)===-1)return false;}
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
    document.getElementById('reconcileStats').innerHTML=
      '<div class="stat-card"><div class="value">'+s.anchor_total+'</div><div class="label">Anchor Claims</div></div>'+
      '<div class="stat-card"><div class="value green">'+s.matched+'</div><div class="label">✅ Matched (₱'+fmtNum(s.matched_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value red">'+s.missing+'</div><div class="label">❌ Missing from CSV (₱'+fmtNum(s.missing_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value amber">'+(s.mismatch+s.status_mismatch)+'</div><div class="label">⚠️ Amount / 🔶 Status Mismatch</div></div>'+
      '<div class="stat-card"><div class="value purple">'+s.extras+'</div><div class="label">➕ Not in Ledger (₱'+fmtNum(s.extras_amount)+')</div></div>'+
      '<div class="stat-card"><div class="value '+pctColor+'">'+s.completeness_pct+'%</div><div class="label">Completeness'+csvTxt+'</div></div>'+
      '<div class="stat-card"><div class="value">₱'+fmtNum(s.anchor_amount)+'</div><div class="label">Anchor Order Value</div></div>';
    return;
  }
  var matched=shown.filter(function(x){return x.match_type==='matched';}).length;
  var mismatch=shown.filter(function(x){return x.match_type==='amount_mismatch';}).length;
  var stmis=shown.filter(function(x){return x.match_type==='status_mismatch';}).length;
  var notFound=shown.filter(function(x){return x.match_type==='not_found';}).length;
  var tAmt=shown.reduce(function(s,x){return s+x.csv_amount;},0);
  document.getElementById('reconcileStats').innerHTML=
    '<div class="stat-card"><div class="value">'+shown.length+'</div><div class="label">Rows</div></div>'+
    '<div class="stat-card"><div class="value green">'+matched+'</div><div class="label">✅ Matched</div></div>'+
    '<div class="stat-card"><div class="value amber">'+mismatch+'</div><div class="label">⚠️ Amount</div></div>'+
    '<div class="stat-card"><div class="value purple">'+stmis+'</div><div class="label">🔶 Status</div></div>'+
    '<div class="stat-card"><div class="value red">'+notFound+'</div><div class="label">❌ Not Found</div></div>'+
    '<div class="stat-card"><div class="value">₱'+fmtNum(tAmt)+'</div><div class="label">Total CSV Amount</div></div>';
}
function renderReconcileTable(rows){
  var tb=document.getElementById('reconcileTbody');
  var head=document.getElementById('reconcileHead');
  if(reconMode==='anchor'){
    head.innerHTML='<tr><th>Match</th><th>Claim ID</th><th>Claim Date</th><th class="amount">Order Payment</th><th class="amount">CSV Amt</th><th class="amount">Diff</th><th>Status</th><th>Workflow</th><th>Order #</th><th>Tracking #</th></tr>';
    if(!rows.length){tb.innerHTML='<tr><td colspan="10" class="empty">No results</td></tr>';return;}
    tb.innerHTML=rows.map(function(r){
      var diffColor=Math.abs(r.diff||0)<0.01?'var(--green)':'var(--red)';
      return '<tr><td>'+matchBadge(r.match_type)+'</td><td><code>'+esc(r.claim_id||r.reference||'—')+'</code></td>'+
        '<td>'+esc(r.date||'—')+'</td>'+
        '<td class="amount">'+(r.db_amount==null?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.db_amount))+'</td>'+
        '<td class="amount">'+(r.csv_amount==null?'<span style="color:var(--dim)">—</span>':'₱'+fmtNum(r.csv_amount))+'</td>'+
        '<td class="amount" style="color:'+diffColor+'">'+(r.diff==null?'—':((r.diff>=0?'+':'')+fmtNum(r.diff)))+'</td>'+
        '<td>'+esc(r.status||'—')+'</td><td>'+esc(r.db_status||'—')+'</td>'+
        '<td><code>'+esc((r.reference&&r.claim_id===r.reference?'':r.reference)||'—')+'</code></td>'+
        '<td>'+esc(r.tracking||'—')+'</td></tr>';
    }).join('');
    return;
  }
  head.innerHTML='<tr><th>Match</th><th>Reference</th><th class="amount">CSV Amount</th><th class="amount">DB Order Payment</th><th class="amount">Diff</th><th>File Date</th><th>CSV Status</th><th>DB Status</th><th>Claim ID</th><th>Tracking #</th></tr>';
  if(!rows.length){tb.innerHTML='<tr><td colspan="10" class="empty">No matching rows</td></tr>';return;}
  tb.innerHTML=rows.map(function(r){
    var diff=r.diff===null||r.diff===undefined?'—':(r.diff>0?'+'+fmtNum(r.diff):fmtNum(r.diff));
    return '<tr><td>'+matchBadge(r.match_type)+'</td><td><code>'+esc(r.reference)+'</code></td>'+
      '<td class="amount">'+(r.csv_amount===null||r.csv_amount===undefined?'—':'₱'+fmtNum(r.csv_amount))+'</td>'+
      '<td class="amount">'+(r.db_amount===null||r.db_amount===undefined?'—':'₱'+fmtNum(r.db_amount))+'</td>'+
      '<td class="amount">'+diff+'</td><td>'+esc(r.date||'—')+'</td>'+
      '<td>'+esc(r.csv_status||'—')+'</td><td>'+esc(r.db_status||'—')+'</td>'+
      '<td><code>'+esc(r.claim_id||'—')+'</code></td><td>'+esc(r.tracking||'—')+'</td></tr>';
  }).join('');
}
function exportReconcileCSV(){
  if(!reconcileResults.length)return;
  var cols=['match_type','reference','csv_amount','db_amount','diff','date','csv_status','db_status','claim_id','tracking'];
  var head=['Match Type','Reference','CSV Amount','DB Amount','Diff','File Date','CSV Status','DB Status','Claim ID','Tracking #'];
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
  a.download='claims_recon_report_'+new Date().toISOString().slice(0,10)+'.csv';
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
