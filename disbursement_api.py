#!/usr/bin/env python3
"""MallPlus Disbursement Request Portal — API + HTML Frontend
Data: Google Sheets (new spreadsheet)  |  Attachments: Google Drive  |  Auth: shared with reimbursement portal

COLUMN LAYOUT — Disbursements tab (1-based col → 0-based row index):
  Col  1 (A) row[0]  → ID                  DSB-2026-NNNN
  Col  2 (B) row[1]  → created_at           ISO Manila time
  Col  3 (C) row[2]  → submitter_email
  Col  4 (D) row[3]  → submitter_name
  Col  5 (E) row[4]  → submitter_dept
  Col  6 (F) row[5]  → vendor
  Col  7 (G) row[6]  → description
  Col  8 (H) row[7]  → category             Opex | Capex (auto-derived)
  Col  9 (I) row[8]  → amount               NUMERIC (never string)
  Col 10 (J) row[9]  → vat_class            VATable 12% | VAT-Exempt | Zero-Rated | Non-VAT
  Col 11 (K) row[10] → due_date             YYYY-MM-DD
  Col 12 (L) row[11] → budget_code          e.g. "20002"
  Col 13 (M) row[12] → budget_line_label    e.g. "20002 — Product · Outsourced Services"
  Col 14 (N) row[13] → budget_owner_email   L1 approver source
  Col 15 (O) row[14] → budget_flag          "" | warning message
  Col 16 (P) row[15] → dupe_flag            "" | warning message
  Col 17 (Q) row[16] → status               Pending | Pending L2 | Approved | Paid |
                                             Rejected | Rejected by Finance | Cancelled
  Col 18 (R) row[17] → l1_approver_email    budget owner (or "Skipped")
  Col 19 (S) row[18] → l1_status            Pending | Approved | Rejected | Skipped
  Col 20 (T) row[19] → l1_ts
  Col 21 (U) row[20] → l2_approver_email    shaun@ (<5000) or patt@ (>=5000), or "Skipped"
  Col 22 (V) row[21] → l2_status            Pending | Approved | Rejected | Skipped
  Col 23 (W) row[22] → l2_ts
  Col 24 (X) row[23] → attachment_link      Drive webViewLink or ""
  Col 25 (Y) row[24] → finance_actor        email of who paid/rejected
  Col 26 (Z) row[25] → finance_ts
  Col 27 (AA) row[26] → reject_reason       from any level or cancellation note
  Col 28 (AB) row[27] → updated_at

Vendor Bank Details tab: vendor | bank | account_name | account_number | notes | updated_by | updated_at
Audit Log tab: ts | actor_email | actor_name | request_id | action | field | old_value | new_value | note
"""

import io, json, os, time
from datetime import datetime

import gspread
from googleapiclient.http import MediaIoBaseUpload

# ── Shared auth/session from reimbursement_api (same process, same session store) ──────────
from reimbursement_api import (
    _validate_session, _extract_token, _create_session,
    _find_employee, _load_employees, _lookup_name,
    _get_creds_info, _get_gs, _get_drive,
    _send_email, _html_email, _email_base_style,
    FINANCE_TEAM_EMAILS, _is_finance_user,
    _get_config, _amounts_equal,
    PORTAL_URL,
)

# ── Config ──────────────────────────────────────────────────────────────────────────────────
DSB_SHEET_ID          = os.environ.get('DSB_SHEET_ID',
                            '18O3w5lW1Kuq8cn6EPwkqz10ghZ7gjivUYrghN-9gPYA')
DSB_ATTACH_FOLDER_ID  = os.environ.get('DSB_ATTACH_FOLDER_ID',
                            '10feHDc1MrVaUMf9FWj64VvWEU8BNAi1a')
BUDGET_SHEET_ID       = '1wm59yAn21o6k227bnaxa6glcuCPGVtVyYJ8haXv0wCw'

# L2 approvers by threshold
L2_LOW_EMAIL   = 'shaun@fincom.asia'   # amount < 5000
L2_HIGH_EMAIL  = 'patt@fincom.asia'    # amount >= 5000

# ── Lazy sheet singletons ────────────────────────────────────────────────────────────────────
_dsb_sheet      = None
_dsb_audit      = None
_dsb_vendor_bk  = None

def _get_disbursement_sheet():
    global _dsb_sheet
    if _dsb_sheet is None:
        sh = _get_gs().open_by_key(DSB_SHEET_ID)
        _dsb_sheet = sh.worksheet('Disbursements')
    return _dsb_sheet

def _get_dsb_audit_sheet():
    global _dsb_audit
    if _dsb_audit is None:
        sh = _get_gs().open_by_key(DSB_SHEET_ID)
        _dsb_audit = sh.worksheet('Audit Log')
    return _dsb_audit

def _get_vendor_bank_sheet():
    global _dsb_vendor_bk
    if _dsb_vendor_bk is None:
        sh = _get_gs().open_by_key(DSB_SHEET_ID)
        _dsb_vendor_bk = sh.worksheet('Vendor Bank Details')
    return _dsb_vendor_bk

def _invalidate_dsb_sheet():
    """Force re-open on next access (call after 429 recovery or write errors)."""
    global _dsb_sheet, _dsb_audit, _dsb_vendor_bk, _dsb_rows_cache, _dsb_rows_cache_ts
    _dsb_sheet = _dsb_audit = _dsb_vendor_bk = None
    _dsb_rows_cache = None
    _dsb_rows_cache_ts = 0.0

# Disbursement rows, cached briefly so budget-remaining scans don't re-read the
# sheet once per budget line (38 reads/request was making /budget-lines hang).
_dsb_rows_cache    = None
_dsb_rows_cache_ts = 0.0
_DSB_ROWS_TTL      = 30  # seconds

def _get_dsb_rows():
    """Disbursement tab rows, cached for _DSB_ROWS_TTL seconds."""
    global _dsb_rows_cache, _dsb_rows_cache_ts
    now = time.time()
    if _dsb_rows_cache is not None and (now - _dsb_rows_cache_ts) < _DSB_ROWS_TTL:
        return _dsb_rows_cache
    _dsb_rows_cache    = _get_disbursement_sheet().get_all_values()
    _dsb_rows_cache_ts = now
    return _dsb_rows_cache

# ── Write throttle / 429 backoff ────────────────────────────────────────────────────────────
_last_write_ts = 0.0

def _ws_write(fn, *args, **kwargs):
    """Call a gspread write fn with throttle (≥1.3 s between writes) and 429 retry."""
    global _last_write_ts
    now = time.time()
    gap = now - _last_write_ts
    if gap < 1.3:
        time.sleep(1.3 - gap)
    for attempt in range(3):
        try:
            result = fn(*args, **kwargs)
            _last_write_ts = time.time()
            return result
        except Exception as e:
            err_str = str(e)
            if '429' in err_str or 'quota' in err_str.lower():
                wait = 60 * (attempt + 1)
                print(f'[dsb] 429 quota hit — waiting {wait}s', flush=True)
                time.sleep(wait)
                _invalidate_dsb_sheet()
            else:
                raise
    raise RuntimeError('Failed after 3 attempts due to 429')

# ── ID Generator ─────────────────────────────────────────────────────────────────────────────
def _gen_dsb_id():
    """Generate DSB-2026-NNNN format ID (sequential, based on max existing)."""
    ws = _get_disbursement_sheet()
    rows = ws.get_all_values()
    prefix = 'DSB-2026-'
    max_num = 0
    for row in rows[1:]:
        if row and row[0].startswith(prefix):
            try:
                n = int(row[0][len(prefix):])
                if n > max_num:
                    max_num = n
            except Exception:
                pass
    return f'{prefix}{max_num + 1:04d}'

# ── Budget Lines ─────────────────────────────────────────────────────────────────────────────
_budget_cache     = None
_budget_cache_ts  = 0.0
_BUDGET_CACHE_TTL = 600  # 10 minutes
_BUDGET_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'budget-codes-2026.json')

def _load_budget_lines():
    """Load budget lines from live sheet (cached 10 min) with fallback to bundled JSON."""
    global _budget_cache, _budget_cache_ts
    now = time.time()
    if _budget_cache and (now - _budget_cache_ts) < _BUDGET_CACHE_TTL:
        return _budget_cache

    try:
        sh   = _get_gs().open_by_key(BUDGET_SHEET_ID)
        ws   = sh.worksheet('Sheet1')
        rows = ws.get_all_values()
        lines = []
        for row in rows[1:]:  # skip header (if any) — budget sheet has no header
            if len(row) < 6 or not any(c.strip() for c in row):
                continue
            code  = row[4].strip() if len(row) > 4 else ''
            owner = row[5].strip() if len(row) > 5 else ''
            dept  = row[2].strip() if len(row) > 2 else ''
            cat   = row[3].strip() if len(row) > 3 else ''
            name  = row[1].strip() if len(row) > 1 else ''
            try:
                total = float(str(row[6]).replace(',', '').strip()) if len(row) > 6 and row[6].strip() else 0.0
            except Exception:
                total = 0.0
            # Sheet now has a title row ("Month to Date") + header row (Phase|ID|...|CODE|...)
            # — skip anything whose code isn't a real numeric code.
            if not code or not code.isdigit():
                continue
            lines.append({
                'code': code, 'name': name, 'dept': dept,
                'category': cat, 'owner': owner, 'total': total,
                'budget_type': 'Capex' if dept.upper() == 'CAPEX' else 'Opex',
                'label': f'{code} — {dept} · {cat}',
            })
        if lines:
            _budget_cache    = lines
            _budget_cache_ts = now
            print(f'[budget] Loaded {len(lines)} live lines', flush=True)
            return _budget_cache
    except Exception as e:
        print(f'[budget] Live fetch failed: {e} — using fallback JSON', flush=True)

    # Fallback to bundled JSON
    try:
        with open(_BUDGET_JSON_PATH) as f:
            data = json.load(f)
        lines = []
        for entry in data.get('lines', []):
            dept = entry.get('dept', '')
            cat  = entry.get('category', '')
            code = entry.get('code', '')
            if not code or not code.isdigit():
                continue
            lines.append({
                'code': code,
                'name': entry.get('name', ''),
                'dept': dept,
                'category': cat,
                'owner': entry.get('owner', ''),
                'total': float(entry.get('total', 0)),
                'budget_type': 'Capex' if dept.upper() == 'CAPEX' else 'Opex',
                'label': f'{code} — {dept} · {cat}',
            })
        _budget_cache    = lines
        _budget_cache_ts = now
        print(f'[budget] Fallback JSON loaded {len(lines)} lines', flush=True)
        return _budget_cache
    except Exception as e2:
        print(f'[budget] Fallback JSON also failed: {e2}', flush=True)
        return []

def _find_budget_line(code):
    """Find a specific budget line by code."""
    for line in _load_budget_lines():
        if line['code'] == code:
            return line
    return None

def _derive_category(dept):
    """Derive Opex/Capex from department string."""
    return 'Capex' if (dept or '').strip().upper() == 'CAPEX' else 'Opex'

# ── Budget Remaining ─────────────────────────────────────────────────────────────────────────
def _get_budget_remaining(budget_code, exclude_id=None):
    """
    remaining = line total − Σ(amount of Approved+Paid disbursements on that line)
    Returns (remaining, total, spent).
    """
    line = _find_budget_line(budget_code)
    total = float(line['total']) if line else 0.0

    rows = _get_dsb_rows()
    spent = 0.0
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        if exclude_id and row[0].strip() == exclude_id:
            continue
        row_code   = row[11].strip() if len(row) > 11 else ''
        row_status = row[16].strip() if len(row) > 16 else ''
        if row_code == budget_code and row_status in ('Approved', 'Paid'):
            try:
                spent += float(str(row[8]).replace(',', '').strip())
            except Exception:
                pass
    return (total - spent, total, spent)

# ── Approval Chain ────────────────────────────────────────────────────────────────────────────
def _resolve_disbursement_chain(budget_owner_email, amount, submitter_email):
    """
    L1 = budget owner of selected budget line (no threshold).
    L2 = shaun@ if amount < 5000, patt@ if amount >= 5000.
    Skip rule: if submitter IS that level's approver, skip the level.
    If all levels skipped → auto-Approved.

    Returns dict:
        l1_email: str or None (None = skipped)
        l2_email: str or None (None = skipped)
        first_active: 'l1' | 'l2' | None (None = auto-approve)
        auto_approve: bool
    """
    sub = (submitter_email or '').strip().lower()
    l1  = (budget_owner_email or '').strip().lower()
    l2  = L2_LOW_EMAIL if float(amount) < 5000 else L2_HIGH_EMAIL

    l1_active = (l1 != sub and bool(l1))
    l2_active = (l2.lower() != sub)

    if l1_active:
        first = 'l1'
    elif l2_active:
        first = 'l2'
    else:
        first = None

    return {
        'l1_email':    l1 if l1_active else None,
        'l2_email':    l2 if l2_active else None,
        'first_active': first,
        'auto_approve': (first is None),
    }

# ── Duplicate Detection ────────────────────────────────────────────────────────────────────────
def _check_dsb_duplicates(vendor, amount, due_date, exclude_id=None):
    """
    Flag if same vendor + same amount (numeric) + same due_date already exists
    in any non-terminal status (not Rejected, Rejected by Finance, Cancelled).
    """
    ws   = _get_disbursement_sheet()
    rows = ws.get_all_values()
    TERMINAL = {'Rejected', 'Rejected by Finance', 'Cancelled'}
    dupes = []
    vendor_lower = vendor.strip().lower()
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        rid = row[0].strip() if row else ''
        if exclude_id and rid == exclude_id:
            continue
        row_status = row[16].strip() if len(row) > 16 else ''
        if row_status in TERMINAL:
            continue
        row_vendor   = row[5].strip().lower()  if len(row) > 5  else ''
        row_amount   = row[8].strip()           if len(row) > 8  else ''
        row_due_date = row[10].strip()          if len(row) > 10 else ''
        if (row_vendor == vendor_lower
                and _amounts_equal(row_amount, float(amount))
                and row_due_date == due_date):
            dupes.append({
                'type': 'matching_fields',
                'request_id': rid,
                'message': f'Same vendor, amount, and due date already submitted as {rid}',
            })
    return dupes

# ── Audit Log ─────────────────────────────────────────────────────────────────────────────────
def _dsb_audit_log(action, request_id, actor_email, actor_name, field='', old_val='', new_val='', note=''):
    """Append one row to the Disbursements Audit Log tab. Never raises."""
    try:
        _ws_write(
            _get_dsb_audit_sheet().append_row,
            [
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                actor_email, actor_name, request_id,
                action, field, str(old_val), str(new_val), note
            ],
            value_input_option='USER_ENTERED'
        )
    except Exception as e:
        print(f'[dsb-audit] log error: {e}', flush=True)

# ── Row Finder ────────────────────────────────────────────────────────────────────────────────
def _dsb_find_row(ws, dsb_id):
    """Return (1-based row_idx, row) or (None, None)."""
    rows = ws.get_all_values()
    for i, row in enumerate(rows):
        if row and row[0].strip() == dsb_id:
            return i + 1, row
    return None, None

# ── Drive Upload (Attachments) ────────────────────────────────────────────────────────────────
def _upload_attachment(file_data, filename, mime_type):
    """Upload attachment to Disbursement Attachments folder in shared drive."""
    drive_svc = _get_drive()
    media     = MediaIoBaseUpload(io.BytesIO(file_data), mimetype=mime_type, resumable=True)
    f = drive_svc.files().create(
        body={'name': filename, 'parents': [DSB_ATTACH_FOLDER_ID]},
        media_body=media, fields='id,webViewLink',
        supportsAllDrives=True
    ).execute()
    # Make readable by anyone with link
    drive_svc.permissions().create(
        fileId=f['id'],
        body={'type': 'anyone', 'role': 'reader'},
        supportsAllDrives=True
    ).execute()
    return f.get('webViewLink', f'https://drive.google.com/file/d/{f["id"]}/view')

# ═══════════════════════════════════════════════════════════════════════════════════════════════
# EMAIL NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════════════════════════════════════

_DSB_PORTAL = f'{PORTAL_URL}/disbursements'

def _dsb_email_banner(icon, title):
    return f'<div class="banner"><div class="icon">{icon}</div><h1>{title}</h1></div>'

def _dsb_info_table(rows_html):
    return f'<table class="info-table">{rows_html}</table>'

def _dsb_info_row(label, value):
    return f'<tr><td class="label">{label}</td><td class="value">{value}</td></tr>'

def _dsb_btn():
    return (f'<div class="button-row">'
            f'<a class="button" href="{_DSB_PORTAL}">Review in Portal →</a></div>')

def _dsb_footer():
    return ('<div class="footer">This is an automated notification from the MallPlus Disbursement Portal.'
            '<br>Questions? Reply to finance@fincom.asia</div>')

def _notify_dsb_submit(dsb_id, sub_name, sub_email, amount, vendor, description, budget_label,
                       budget_flag, l1_email):
    """Notify L1 approver on new submission."""
    amt_str = f'₱{float(amount):,.2f}'
    flag_note = (f'<div style="background:#FFF8E1;border:1px solid #C4880A;border-radius:8px;'
                 f'padding:10px 14px;margin:12px 0;font-size:13px;color:#C4880A;">'
                 f'⚠️ {budget_flag}</div>') if budget_flag else ''
    rows_html = (
        _dsb_info_row('Request ID', dsb_id)
        + _dsb_info_row('Employee', sub_name)
        + _dsb_info_row('Vendor', vendor)
        + _dsb_info_row('Description', description[:80])
        + _dsb_info_row('Budget Line', budget_label)
    )
    name = _lookup_name(l1_email)
    chunks = [
        _dsb_email_banner('📋', 'New Disbursement Request'),
        f'<div class="body"><p>Hello <strong>{name}</strong>,</p>',
        f'<p><strong>{sub_name}</strong> submitted a disbursement request for your approval.</p>',
        f'<div class="amount">{amt_str}</div>',
        flag_note,
        _dsb_info_table(rows_html),
        _dsb_btn(),
        '<div class="divider"></div>',
        _dsb_footer(),
        '</div>',
    ]
    _send_email(
        l1_email, name,
        f'Action Needed: {sub_name} disbursement {amt_str} ({dsb_id})',
        _html_email(chunks)
    )

def _notify_dsb_auto_approve(dsb_id, sub_name, sub_email, amount, vendor):
    """Notify submitter that request was auto-approved (all levels skipped)."""
    amt_str = f'₱{float(amount):,.2f}'
    chunks = [
        _dsb_email_banner('✅', 'Disbursement Request Auto-Approved'),
        f'<div class="body"><p>Hello <strong>{sub_name}</strong>,</p>',
        f'<p>Your disbursement request was <strong>auto-approved</strong> because all '
        f'approval levels were skipped (you are the budget owner and final approver for this request).</p>',
        f'<div class="amount">{amt_str}</div>',
        _dsb_info_table(_dsb_info_row('Request ID', dsb_id) + _dsb_info_row('Vendor', vendor)),
        '<p>Finance will process payment in the next disbursement cycle.</p>',
        _dsb_btn(),
        '<div class="divider"></div>',
        _dsb_footer(),
        '</div>',
    ]
    _send_email(sub_email, sub_name, f'Auto-Approved: Your ₱{float(amount):,.0f} disbursement ({dsb_id})', _html_email(chunks))

def _notify_dsb_advance_to_l2(dsb_id, sub_name, amount, vendor, l1_name, l2_email):
    """Notify L2 approver that L1 approved and request escalated to them."""
    amt_str = f'₱{float(amount):,.2f}'
    l2_name = _lookup_name(l2_email)
    chunks = [
        _dsb_email_banner('⬆️', 'Disbursement — Final Approval Needed'),
        f'<div class="body"><p>Hello <strong>{l2_name}</strong>,</p>',
        f'<p><strong>{l1_name}</strong> approved this disbursement request. Your final approval is required.</p>',
        f'<div class="amount">{amt_str}</div>',
        _dsb_info_table(
            _dsb_info_row('Request ID', dsb_id)
            + _dsb_info_row('Employee', sub_name)
            + _dsb_info_row('Vendor', vendor)
            + _dsb_info_row('L1 Approved by', l1_name)
        ),
        _dsb_btn(),
        '<div class="divider"></div>',
        _dsb_footer(),
        '</div>',
    ]
    _send_email(l2_email, l2_name,
                f'Final Approval Needed: {sub_name} ₱{float(amount):,.0f} disbursement ({dsb_id})',
                _html_email(chunks))

def _notify_dsb_fully_approved(dsb_id, sub_name, sub_email, amount, vendor, approver_name):
    """Notify finance + submitter when request is fully approved."""
    amt_str = f'₱{float(amount):,.2f}'
    # Notify submitter
    sub_chunks = [
        _dsb_email_banner('✅', 'Disbursement Request Approved'),
        f'<div class="body"><p>Hello <strong>{sub_name}</strong>,</p>',
        f'<p><strong>{approver_name}</strong> approved your disbursement request.</p>',
        f'<div class="amount">{amt_str}</div>',
        _dsb_info_table(
            _dsb_info_row('Request ID', dsb_id)
            + _dsb_info_row('Vendor', vendor)
            + _dsb_info_row('Approved by', approver_name)
        ),
        '<p>Finance will process your payment in the next disbursement cycle.</p>',
        _dsb_btn(),
        '<div class="divider"></div>',
        _dsb_footer(),
        '</div>',
    ]
    _send_email(sub_email, sub_name,
                f'✓ Approved: Your ₱{float(amount):,.0f} disbursement ({dsb_id})',
                _html_email(sub_chunks))
    # Notify finance team
    for fin_email in sorted(FINANCE_TEAM_EMAILS):
        fin_name = _lookup_name(fin_email)
        fin_chunks = [
            _dsb_email_banner('💰', 'Disbursement Ready for Payment'),
            f'<div class="body"><p>Hello <strong>{fin_name}</strong>,</p>',
            f'<p>A disbursement request has been fully approved and is ready for payment.</p>',
            f'<div class="amount">{amt_str}</div>',
            _dsb_info_table(
                _dsb_info_row('Request ID', dsb_id)
                + _dsb_info_row('Employee', sub_name)
                + _dsb_info_row('Vendor', vendor)
                + _dsb_info_row('Approved by', approver_name)
            ),
            _dsb_btn(),
            '<div class="divider"></div>',
            _dsb_footer(),
            '</div>',
        ]
        _send_email(fin_email, fin_name,
                    f'Payment Ready: {sub_name} ₱{float(amount):,.0f} disbursement ({dsb_id})',
                    _html_email(fin_chunks))

def _notify_dsb_rejected(dsb_id, sub_name, sub_email, amount, vendor, rejector_name, reason):
    """Notify submitter that request was rejected."""
    amt_str = f'₱{float(amount):,.2f}'
    chunks = [
        _dsb_email_banner('❌', 'Disbursement Request Rejected'),
        f'<div class="body"><p>Hello <strong>{sub_name}</strong>,</p>',
        f'<p><strong>{rejector_name}</strong> rejected your disbursement request.</p>',
        f'<div class="amount">{amt_str}</div>',
        _dsb_info_table(
            _dsb_info_row('Request ID', dsb_id)
            + _dsb_info_row('Vendor', vendor)
            + _dsb_info_row('Rejected by', rejector_name)
            + (_dsb_info_row('Reason', reason) if reason else '')
        ),
        '<p>You can edit and resubmit this request from the portal.</p>',
        _dsb_btn(),
        '<div class="divider"></div>',
        _dsb_footer(),
        '</div>',
    ]
    _send_email(sub_email, sub_name,
                f'✗ Rejected: Your ₱{float(amount):,.0f} disbursement ({dsb_id})',
                _html_email(chunks))

def _notify_dsb_paid(dsb_id, sub_name, sub_email, amount, vendor, finance_name):
    """Notify submitter that disbursement was paid."""
    amt_str = f'₱{float(amount):,.2f}'
    chunks = [
        _dsb_email_banner('💵', 'Disbursement Paid'),
        f'<div class="body"><p>Hello <strong>{sub_name}</strong>,</p>',
        f'<p>Your disbursement has been processed and payment released.</p>',
        f'<div class="amount">{amt_str}</div>',
        _dsb_info_table(
            _dsb_info_row('Request ID', dsb_id)
            + _dsb_info_row('Vendor', vendor)
            + _dsb_info_row('Processed by', finance_name)
        ),
        _dsb_btn(),
        '<div class="divider"></div>',
        _dsb_footer(),
        '</div>',
    ]
    _send_email(sub_email, sub_name,
                f'💵 Paid: ₱{float(amount):,.0f} disbursement to {vendor} ({dsb_id})',
                _html_email(chunks))

def _notify_dsb_finance_reject(dsb_id, sub_name, sub_email, amount, vendor, finance_name, reason, approver_emails):
    """Notify submitter + approvers of record that Finance rejected an approved request."""
    amt_str = f'₱{float(amount):,.2f}'
    # Submitter
    sub_chunks = [
        _dsb_email_banner('❌', 'Disbursement Rejected by Finance'),
        f'<div class="body"><p>Hello <strong>{sub_name}</strong>,</p>',
        f'<p><strong>{finance_name}</strong> rejected your disbursement during the finance review.</p>',
        f'<div class="amount">{amt_str}</div>',
        _dsb_info_table(
            _dsb_info_row('Request ID', dsb_id)
            + _dsb_info_row('Vendor', vendor)
            + _dsb_info_row('Reason', reason)
        ),
        '<p>Please fix the issue and resubmit from the portal.</p>',
        _dsb_btn(),
        '<div class="divider"></div>',
        _dsb_footer(),
        '</div>',
    ]
    _send_email(sub_email, sub_name,
                f'⚠️ Finance Rejected: ₱{float(amount):,.0f} disbursement ({dsb_id})',
                _html_email(sub_chunks))
    # Approvers of record
    for app_email in set(e.strip() for e in approver_emails if e.strip()):
        if app_email.lower() in ('skipped', ''):
            continue
        app_name = _lookup_name(app_email)
        app_chunks = [
            _dsb_email_banner('⚠️', 'Disbursement Rejected by Finance'),
            f'<div class="body"><p>Hello <strong>{app_name}</strong>,</p>',
            f'<p><strong>{finance_name}</strong> rejected this approved disbursement during finance review.</p>',
            f'<div class="amount">{amt_str}</div>',
            _dsb_info_table(
                _dsb_info_row('Request ID', dsb_id)
                + _dsb_info_row('Employee', sub_name)
                + _dsb_info_row('Vendor', vendor)
                + _dsb_info_row('Reason', reason)
            ),
            '<p>The requester has been notified and may resubmit (fresh approval cycle).</p>',
            _dsb_btn(),
            '<div class="divider"></div>',
            _dsb_footer(),
            '</div>',
        ]
        _send_email(app_email, app_name,
                    f'⚠️ Finance Rejected {dsb_id}: {sub_name} {amt_str}',
                    _html_email(app_chunks))

# ═══════════════════════════════════════════════════════════════════════════════════════════════
# API HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════════════════════

def _json_ok(data):
    import json as _json
    return 200, 'application/json', _json.dumps(data).encode(), True

def _json_err(code, msg):
    import json as _json
    return code, 'application/json', _json.dumps({'error': msg}).encode(), True

def _manila_now():
    """Return current Manila time as ISO string."""
    try:
        import zoneinfo
        from datetime import timezone
        tz = zoneinfo.ZoneInfo('Asia/Manila')
        return datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')  # fallback

def _row_to_dict(row, headers=None):
    """Convert a sheet row to a dict using column names."""
    COLS = [
        'id','created_at','submitter_email','submitter_name','submitter_dept',
        'vendor','description','category','amount','vat_class',
        'due_date','budget_code','budget_line_label','budget_owner_email',
        'budget_flag','dupe_flag','status',
        'l1_approver_email','l1_status','l1_ts',
        'l2_approver_email','l2_status','l2_ts',
        'attachment_link','finance_actor','finance_ts',
        'reject_reason','updated_at',
    ]
    d = {}
    for i, col in enumerate(COLS):
        d[col] = row[i].strip() if i < len(row) else ''
    return d

# ── Login (delegates to reimbursement auth) ─────────────────────────────────────────────────
def _api_dsb_login(body_raw):
    import json as _json
    data  = _json.loads(body_raw or '{}')
    email = data.get('email', '').strip()
    pin   = data.get('pin', '').strip()
    if not email or not pin:
        return _json_err(400, 'Email and PIN required')
    emp = _find_employee(email)
    if not emp:
        return _json_err(401, 'Employee not found. Contact admin to register.')
    if emp.get('pin', '') != pin:
        return _json_err(401, 'Invalid PIN')
    if emp.get('status', 'Active').strip().lower() != 'active':
        return _json_err(403, 'Account is inactive')
    token = _create_session(email, emp.get('name', ''), emp.get('department', ''), emp.get('role', 'employee'))
    return _json_ok({'token': token, 'name': emp.get('name', ''), 'email': email,
                     'department': emp.get('department', ''), 'role': emp.get('role', 'employee')})

# ── Session ──────────────────────────────────────────────────────────────────────────────────
def _api_dsb_session(headers):
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Invalid session')
    return _json_ok(session)

# ── Budget Lines ─────────────────────────────────────────────────────────────────────────────
def _api_budget_lines(qs, headers):
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Please log in')

    lines  = _load_budget_lines()
    result = []
    for line in lines:
        remaining, total, spent = _get_budget_remaining(line['code'])
        result.append({**line, 'remaining': remaining, 'spent': spent})
    # Sort: OPEX first, then CAPEX; within each group by code
    opex   = [l for l in result if l['budget_type'] == 'Opex']
    capex  = [l for l in result if l['budget_type'] == 'Capex']
    opex.sort(key=lambda x: x['code'])
    capex.sort(key=lambda x: x['code'])
    source = 'live' if _budget_cache else 'fallback'
    return _json_ok({'lines': opex + capex, 'source': source})

# ── Vendor Bank Details (read-only for portal) ───────────────────────────────────────────────
def _api_vendor_bank(qs, headers):
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Please log in')
    if not _is_finance_user(session):
        return _json_err(403, 'Finance access only')
    try:
        ws   = _get_vendor_bank_sheet()
        rows = ws.get_all_values()
        if not rows:
            return _json_ok([])
        hdrs = [h.strip().lower().replace(' ', '_') for h in rows[0]]
        result = []
        for row in rows[1:]:
            if not any(c.strip() for c in row):
                continue
            d = {}
            for i, h in enumerate(hdrs):
                d[h] = row[i].strip() if i < len(row) else ''
            result.append(d)
        return _json_ok(result)
    except Exception as e:
        return _json_err(500, str(e))

# ── Submit ────────────────────────────────────────────────────────────────────────────────────
def _api_dsb_submit(body_raw, headers):
    import json as _json
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Please log in')

    data         = _json.loads(body_raw or '{}')
    vendor       = data.get('vendor', '').strip()
    description  = data.get('description', '').strip()
    budget_code  = data.get('budget_code', '').strip()
    amount_str   = data.get('amount', '').strip()
    vat_class    = data.get('vat_class', '').strip()
    due_date     = data.get('due_date', '').strip()
    attachment   = data.get('attachment_link', '').strip()

    if not all([vendor, description, budget_code, amount_str, vat_class, due_date]):
        return _json_err(400, 'vendor, description, budget_code, amount, vat_class, due_date are required')

    try:
        amount = float(amount_str.replace(',', '').replace('₱', '').strip())
        if amount <= 0:
            raise ValueError
    except Exception:
        return _json_err(400, 'Invalid amount')

    # Budget line lookup
    budget_line = _find_budget_line(budget_code)
    if not budget_line:
        return _json_err(400, f'Budget code {budget_code!r} not found')

    budget_owner  = budget_line['owner']
    budget_label  = budget_line['label']
    category      = _derive_category(budget_line['dept'])

    # Budget flag check
    remaining, total, spent = _get_budget_remaining(budget_code)
    budget_flag = ''
    if amount > remaining:
        budget_flag = (f'Over remaining budget: requested ₱{amount:,.2f}, '
                       f'remaining ₱{remaining:,.2f} (total ₱{total:,.2f} − approved/paid ₱{spent:,.2f})')

    # Duplicate check
    dupes      = _check_dsb_duplicates(vendor, amount, due_date)
    dupe_flag  = '; '.join(d['message'] for d in dupes) if dupes else ''

    # Resolve approval chain
    sub_email  = session['email']
    chain      = _resolve_disbursement_chain(budget_owner, amount, sub_email)

    # Determine initial status and approver states
    l1_email  = chain['l1_email'] or 'Skipped'
    l1_status = 'Skipped' if chain['l1_email'] is None else 'Pending'
    l2_email  = chain['l2_email'] or 'Skipped'
    l2_status = 'Skipped' if chain['l2_email'] is None else 'Pending'

    if chain['auto_approve']:
        status = 'Approved'
    elif chain['first_active'] == 'l1':
        status = 'Pending'
    else:  # first_active == 'l2' (L1 skipped, L2 not)
        status = 'Pending L2'
        l1_status = 'Skipped'
        l1_email  = 'Skipped'

    # Generate ID
    dsb_id    = _gen_dsb_id()
    created   = _manila_now()
    row_data  = [
        dsb_id, created,
        sub_email, session['name'], session.get('department', ''),
        vendor, description, category,
        amount,       # NUMERIC
        vat_class, due_date, budget_code, budget_label, budget_owner,
        budget_flag, dupe_flag, status,
        l1_email, l1_status, '',     # l1_ts empty on submit
        l2_email, l2_status, '',     # l2_ts empty on submit
        attachment, '', '',          # finance_actor, finance_ts empty
        '',                          # reject_reason
        created,                     # updated_at = created_at on submit
    ]
    _ws_write(_get_disbursement_sheet().append_row, row_data, value_input_option='USER_ENTERED')

    # Audit log
    _dsb_audit_log('SUBMIT', dsb_id, sub_email, session['name'], 'status', '', status,
                   f'chain: L1={l1_email} L2={l2_email}')

    # Notifications
    try:
        if chain['auto_approve']:
            _notify_dsb_auto_approve(dsb_id, session['name'], sub_email, amount, vendor)
            # Also audit the auto-approval
            _dsb_audit_log('AUTO_APPROVE', dsb_id, 'system', 'System', 'status', 'Pending', 'Approved',
                           'All approval levels skipped — submitter is own approver at every level')
        elif chain['first_active'] == 'l1':
            _notify_dsb_submit(dsb_id, session['name'], sub_email, amount, vendor,
                               description, budget_label, budget_flag, chain['l1_email'])
        else:
            # L1 skipped, notify L2 directly
            _notify_dsb_advance_to_l2(dsb_id, session['name'], amount, vendor,
                                       'System (L1 auto-skipped)', chain['l2_email'])
    except Exception as e:
        print(f'[dsb-notify] submit error: {e}', flush=True)

    result = {'success': True, 'request_id': dsb_id, 'status': status}
    if dupes:
        result['duplicate_warning'] = True
        result['duplicates'] = dupes
    if budget_flag:
        result['budget_warning'] = budget_flag
    return _json_ok(result)

# ── My Requests ───────────────────────────────────────────────────────────────────────────────
def _api_dsb_my_requests(qs, headers):
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Please log in')

    ws     = _get_disbursement_sheet()
    rows   = ws.get_all_values()
    result = []
    email  = session['email'].strip().lower()
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        d = _row_to_dict(row)
        if d.get('submitter_email', '').strip().lower() == email:
            result.append(d)
    result.reverse()
    return _json_ok(result)

# ── Pending Approvals (for approvers) ────────────────────────────────────────────────────────
def _api_dsb_pending_approvals(qs, headers):
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Please log in')

    approver = session['email'].strip().lower()
    ws       = _get_disbursement_sheet()
    rows     = ws.get_all_values()
    result   = []
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        d = _row_to_dict(row)
        status = d.get('status', '')
        # Determine if this user is the current active approver
        if status == 'Pending':
            if d.get('l1_approver_email', '').strip().lower() == approver and d.get('l1_status') == 'Pending':
                result.append(d)
        elif status == 'Pending L2':
            if d.get('l2_approver_email', '').strip().lower() == approver and d.get('l2_status') == 'Pending':
                result.append(d)
    return _json_ok(result)

# ── Approve ────────────────────────────────────────────────────────────────────────────────────
def _api_dsb_approve(body_raw, headers):
    import json as _json
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Please log in')

    data   = _json.loads(body_raw or '{}')
    dsb_id = data.get('request_id', '').strip()
    if not dsb_id:
        return _json_err(400, 'request_id required')

    ws          = _get_disbursement_sheet()
    row_idx, row = _dsb_find_row(ws, dsb_id)
    if row_idx is None:
        return _json_err(404, 'Request not found')

    d       = _row_to_dict(row)
    status  = d['status']
    actor   = session['email'].strip().lower()

    if status not in ('Pending', 'Pending L2'):
        return _json_err(409, f'Request is {status!r} — only Pending/Pending L2 can be approved')

    now_ts = _manila_now()

    if status == 'Pending':
        # L1 approval
        if d['l1_approver_email'].strip().lower() != actor:
            return _json_err(403, 'You are not the L1 approver for this request')
        # Check if L2 exists (not skipped)
        l2_email = d['l2_approver_email'].strip()
        if l2_email == 'Skipped' or not l2_email:
            # L2 skipped → fully Approved
            _ws_write(ws.update_cell, row_idx, 19, 'Approved')   # l1_status
            _ws_write(ws.update_cell, row_idx, 20, now_ts)        # l1_ts
            _ws_write(ws.update_cell, row_idx, 22, 'Skipped')    # l2_status
            _ws_write(ws.update_cell, row_idx, 17, 'Approved')   # status
            _ws_write(ws.update_cell, row_idx, 28, now_ts)        # updated_at
            new_status = 'Approved'
            _dsb_audit_log('APPROVE', dsb_id, actor, session['name'], 'status', 'Pending', 'Approved',
                           'L1 approved; L2 was skipped → auto-Approved')
            try:
                _notify_dsb_fully_approved(dsb_id, d['submitter_name'], d['submitter_email'],
                                           d['amount'], d['vendor'], session['name'])
            except Exception as e:
                print(f'[dsb-notify] approve L1→Approved error: {e}', flush=True)
        else:
            # Advance to L2
            _ws_write(ws.update_cell, row_idx, 19, 'Approved')   # l1_status
            _ws_write(ws.update_cell, row_idx, 20, now_ts)        # l1_ts
            _ws_write(ws.update_cell, row_idx, 17, 'Pending L2') # status
            _ws_write(ws.update_cell, row_idx, 28, now_ts)        # updated_at
            new_status = 'Pending L2'
            _dsb_audit_log('APPROVE', dsb_id, actor, session['name'], 'status', 'Pending', 'Pending L2',
                           f'L1 approved; advancing to L2={l2_email}')
            try:
                _notify_dsb_advance_to_l2(dsb_id, d['submitter_name'], d['amount'], d['vendor'],
                                           session['name'], l2_email)
            except Exception as e:
                print(f'[dsb-notify] advance-L2 error: {e}', flush=True)

    else:  # status == 'Pending L2'
        if d['l2_approver_email'].strip().lower() != actor:
            return _json_err(403, 'You are not the L2 approver for this request')
        _ws_write(ws.update_cell, row_idx, 22, 'Approved')   # l2_status
        _ws_write(ws.update_cell, row_idx, 23, now_ts)        # l2_ts
        _ws_write(ws.update_cell, row_idx, 17, 'Approved')   # status
        _ws_write(ws.update_cell, row_idx, 28, now_ts)        # updated_at
        new_status = 'Approved'
        _dsb_audit_log('APPROVE', dsb_id, actor, session['name'], 'status', 'Pending L2', 'Approved',
                       'L2 final approval')
        try:
            _notify_dsb_fully_approved(dsb_id, d['submitter_name'], d['submitter_email'],
                                       d['amount'], d['vendor'], session['name'])
        except Exception as e:
            print(f'[dsb-notify] fully-approved error: {e}', flush=True)

    return _json_ok({'success': True, 'request_id': dsb_id, 'status': new_status})

# ── Reject ─────────────────────────────────────────────────────────────────────────────────────
def _api_dsb_reject(body_raw, headers):
    import json as _json
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Please log in')

    data   = _json.loads(body_raw or '{}')
    dsb_id = data.get('request_id', '').strip()
    reason = data.get('reason', '').strip()
    if not dsb_id:
        return _json_err(400, 'request_id required')
    if not reason:
        return _json_err(400, 'Rejection reason is required')

    ws           = _get_disbursement_sheet()
    row_idx, row = _dsb_find_row(ws, dsb_id)
    if row_idx is None:
        return _json_err(404, 'Request not found')

    d       = _row_to_dict(row)
    status  = d['status']
    actor   = session['email'].strip().lower()

    if status not in ('Pending', 'Pending L2'):
        return _json_err(409, f'Request is {status!r} — only Pending/Pending L2 can be rejected')

    # Verify approver authority
    if status == 'Pending' and d['l1_approver_email'].strip().lower() != actor:
        return _json_err(403, 'You are not the L1 approver for this request')
    if status == 'Pending L2' and d['l2_approver_email'].strip().lower() != actor:
        return _json_err(403, 'You are not the L2 approver for this request')

    now_ts = _manila_now()
    if status == 'Pending':
        _ws_write(ws.update_cell, row_idx, 19, 'Rejected')   # l1_status
        _ws_write(ws.update_cell, row_idx, 20, now_ts)
    else:
        _ws_write(ws.update_cell, row_idx, 22, 'Rejected')   # l2_status
        _ws_write(ws.update_cell, row_idx, 23, now_ts)

    _ws_write(ws.update_cell, row_idx, 17, 'Rejected')   # status
    _ws_write(ws.update_cell, row_idx, 27, reason)        # reject_reason
    _ws_write(ws.update_cell, row_idx, 28, now_ts)        # updated_at

    _dsb_audit_log('REJECT', dsb_id, actor, session['name'], 'status', status, 'Rejected', reason)

    try:
        _notify_dsb_rejected(dsb_id, d['submitter_name'], d['submitter_email'],
                              d['amount'], d['vendor'], session['name'], reason)
    except Exception as e:
        print(f'[dsb-notify] reject error: {e}', flush=True)

    return _json_ok({'success': True, 'request_id': dsb_id, 'status': 'Rejected'})

# ── Finance Reject ─────────────────────────────────────────────────────────────────────────────
def _api_dsb_finance_reject(body_raw, headers):
    import json as _json
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Please log in')
    if not _is_finance_user(session):
        return _json_err(403, 'Finance team access only')

    data   = _json.loads(body_raw or '{}')
    dsb_id = data.get('request_id', '').strip()
    reason = data.get('reason', '').strip()
    if not dsb_id:
        return _json_err(400, 'request_id required')
    if not reason:
        return _json_err(400, 'Rejection reason is required')

    ws           = _get_disbursement_sheet()
    row_idx, row = _dsb_find_row(ws, dsb_id)
    if row_idx is None:
        return _json_err(404, 'Request not found')

    d = _row_to_dict(row)
    if d['status'] == 'Paid':
        return _json_err(409, 'Cannot reject a Paid request')
    if d['status'] != 'Approved':
        return _json_err(400, f'Request is {d["status"]!r} — finance reject only applies to Approved requests')

    now_ts = _manila_now()
    _ws_write(ws.update_cell, row_idx, 17, 'Rejected by Finance')  # status
    _ws_write(ws.update_cell, row_idx, 25, session['email'])        # finance_actor
    _ws_write(ws.update_cell, row_idx, 26, now_ts)                  # finance_ts
    _ws_write(ws.update_cell, row_idx, 27, reason)                  # reject_reason
    _ws_write(ws.update_cell, row_idx, 28, now_ts)                  # updated_at

    _dsb_audit_log('FINANCE REJECT', dsb_id, session['email'], session['name'],
                   'status', 'Approved', 'Rejected by Finance', reason)

    # Collect approvers of record
    approver_emails = [d['l1_approver_email'], d['l2_approver_email']]
    try:
        _notify_dsb_finance_reject(dsb_id, d['submitter_name'], d['submitter_email'],
                                   d['amount'], d['vendor'], session['name'], reason,
                                   approver_emails)
    except Exception as e:
        print(f'[dsb-notify] finance-reject error: {e}', flush=True)

    return _json_ok({'success': True, 'request_id': dsb_id, 'status': 'Rejected by Finance'})

# ── Pay ──────────────────────────────────────────────────────────────────────────────────────
def _api_dsb_pay(body_raw, headers):
    import json as _json
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Please log in')
    if not _is_finance_user(session):
        return _json_err(403, 'Finance team access only')

    data   = _json.loads(body_raw or '{}')
    dsb_id = data.get('request_id', '').strip()
    if not dsb_id:
        return _json_err(400, 'request_id required')

    ws           = _get_disbursement_sheet()
    row_idx, row = _dsb_find_row(ws, dsb_id)
    if row_idx is None:
        return _json_err(404, 'Request not found')

    d = _row_to_dict(row)
    if d['status'] == 'Paid':
        return _json_err(409, 'Already marked as Paid')
    if d['status'] != 'Approved':
        return _json_err(400, f'Request must be Approved before paying (current: {d["status"]!r})')

    now_ts = _manila_now()
    _ws_write(ws.update_cell, row_idx, 17, 'Paid')          # status
    _ws_write(ws.update_cell, row_idx, 25, session['email'])# finance_actor
    _ws_write(ws.update_cell, row_idx, 26, now_ts)          # finance_ts
    _ws_write(ws.update_cell, row_idx, 28, now_ts)          # updated_at

    _dsb_audit_log('PAY', dsb_id, session['email'], session['name'],
                   'status', 'Approved', 'Paid', '')

    try:
        _notify_dsb_paid(dsb_id, d['submitter_name'], d['submitter_email'],
                         d['amount'], d['vendor'], session['name'])
    except Exception as e:
        print(f'[dsb-notify] pay error: {e}', flush=True)

    return _json_ok({'success': True, 'request_id': dsb_id, 'status': 'Paid'})

# ── Cancel ────────────────────────────────────────────────────────────────────────────────────
def _api_dsb_cancel(body_raw, headers):
    import json as _json
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Please log in')

    data   = _json.loads(body_raw or '{}')
    dsb_id = data.get('request_id', '').strip()
    reason = data.get('reason', '').strip()
    if not dsb_id:
        return _json_err(400, 'request_id required')
    if not reason:
        return _json_err(400, 'Cancellation reason is required')

    ws           = _get_disbursement_sheet()
    row_idx, row = _dsb_find_row(ws, dsb_id)
    if row_idx is None:
        return _json_err(404, 'Request not found')

    d = _row_to_dict(row)
    if d['submitter_email'].strip().lower() != session['email'].strip().lower():
        return _json_err(403, 'You can only cancel your own requests')
    if d['status'] != 'Pending':
        return _json_err(409, f'Request is {d["status"]!r} — it can only be cancelled before the first approval')

    now_ts = _manila_now()
    _ws_write(ws.update_cell, row_idx, 17, 'Cancelled')     # status
    _ws_write(ws.update_cell, row_idx, 27, f'Cancelled: {reason}')  # reject_reason
    _ws_write(ws.update_cell, row_idx, 28, now_ts)           # updated_at

    _dsb_audit_log('CANCEL', dsb_id, session['email'], session['name'],
                   'status', 'Pending', 'Cancelled', reason)

    return _json_ok({'success': True, 'request_id': dsb_id, 'status': 'Cancelled'})

# ── Edit ──────────────────────────────────────────────────────────────────────────────────────
# Fields editable while Pending:
# vendor(6), description(7), amount(9→numeric), vat_class(10), due_date(11), budget_code(12),
# attachment_link(24)
def _api_dsb_edit(body_raw, headers):
    import json as _json
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Please log in')

    data   = _json.loads(body_raw or '{}')
    dsb_id = data.get('request_id', '').strip()
    if not dsb_id:
        return _json_err(400, 'request_id required')

    ws           = _get_disbursement_sheet()
    row_idx, row = _dsb_find_row(ws, dsb_id)
    if row_idx is None:
        return _json_err(404, 'Request not found')

    d = _row_to_dict(row)
    if d['submitter_email'].strip().lower() != session['email'].strip().lower():
        return _json_err(403, 'You can only edit your own requests')
    if d['status'] != 'Pending':
        return _json_err(409, f'Request is {d["status"]!r} — it can only be edited while Pending (no approval yet)')

    # Parse new values (fall back to existing if not provided)
    new_vendor      = data.get('vendor', d['vendor']).strip()
    new_description = data.get('description', d['description']).strip()
    new_vat_class   = data.get('vat_class', d['vat_class']).strip()
    new_due_date    = data.get('due_date', d['due_date']).strip()
    new_attachment  = data.get('attachment_link', d['attachment_link']).strip()
    new_budget_code = data.get('budget_code', d['budget_code']).strip()
    new_amount_str  = data.get('amount', str(d['amount'])).strip()

    try:
        new_amount = float(new_amount_str.replace(',', '').replace('₱', '').strip())
        if new_amount <= 0:
            raise ValueError
    except Exception:
        return _json_err(400, 'Invalid amount')

    # Validate budget code if changed
    new_budget_line = _find_budget_line(new_budget_code)
    if not new_budget_line:
        return _json_err(400, f'Budget code {new_budget_code!r} not found')

    new_budget_owner = new_budget_line['owner']
    new_budget_label = new_budget_line['label']
    new_category     = _derive_category(new_budget_line['dept'])

    # Re-check budget flag and dupes with new values
    remaining, total, spent = _get_budget_remaining(new_budget_code, exclude_id=dsb_id)
    new_budget_flag = ''
    if new_amount > remaining:
        new_budget_flag = (f'Over remaining budget: requested ₱{new_amount:,.2f}, '
                           f'remaining ₱{remaining:,.2f} (total ₱{total:,.2f} − approved/paid ₱{spent:,.2f})')

    dupes     = _check_dsb_duplicates(new_vendor, new_amount, new_due_date, exclude_id=dsb_id)
    dupe_flag = '; '.join(d2['message'] for d2 in dupes) if dupes else ''

    # Re-resolve chain if amount or budget line changed
    chain_changed = (new_amount != float(d['amount'] or 0)) or (new_budget_code != d['budget_code'])
    if chain_changed:
        chain     = _resolve_disbursement_chain(new_budget_owner, new_amount, session['email'])
        l1_email  = chain['l1_email'] or 'Skipped'
        l2_email  = chain['l2_email'] or 'Skipped'
        l1_status = 'Skipped' if chain['l1_email'] is None else 'Pending'
        l2_status = 'Skipped' if chain['l2_email'] is None else 'Pending'
        new_status = 'Approved' if chain['auto_approve'] else 'Pending'
    else:
        l1_email  = d['l1_approver_email']
        l2_email  = d['l2_approver_email']
        l1_status = d['l1_status']
        l2_status = d['l2_status']
        new_status = d['status']

    # Detect changes for audit
    EDITABLE = [
        ('vendor', 6, d['vendor'], new_vendor),
        ('description', 7, d['description'], new_description),
        ('amount', 9, d['amount'], str(new_amount)),
        ('vat_class', 10, d['vat_class'], new_vat_class),
        ('due_date', 11, d['due_date'], new_due_date),
        ('budget_code', 12, d['budget_code'], new_budget_code),
        ('attachment_link', 24, d['attachment_link'], new_attachment),
    ]
    changes = [(label, col, old, new) for (label, col, old, new) in EDITABLE if str(old).strip() != str(new).strip()]

    now_ts = _manila_now()
    # Write changed fields
    for label, col, old, new_val in changes:
        _ws_write(ws.update_cell, row_idx, col, new_val if col != 9 else new_amount)

    # Always update derived/flagging fields
    _ws_write(ws.update_cell, row_idx, 8, new_category)
    _ws_write(ws.update_cell, row_idx, 13, new_budget_label)
    _ws_write(ws.update_cell, row_idx, 14, new_budget_owner)
    _ws_write(ws.update_cell, row_idx, 15, new_budget_flag)
    _ws_write(ws.update_cell, row_idx, 16, dupe_flag)
    if chain_changed:
        _ws_write(ws.update_cell, row_idx, 17, new_status)
        _ws_write(ws.update_cell, row_idx, 18, l1_email)
        _ws_write(ws.update_cell, row_idx, 19, l1_status)
        _ws_write(ws.update_cell, row_idx, 21, l2_email)
        _ws_write(ws.update_cell, row_idx, 22, l2_status)
    _ws_write(ws.update_cell, row_idx, 28, now_ts)

    for label, col, old, new_val in changes:
        _dsb_audit_log('EDIT', dsb_id, session['email'], session['name'], label, old, new_val)

    if chain_changed and chain['auto_approve']:
        _dsb_audit_log('AUTO_APPROVE', dsb_id, 'system', 'System', 'status', 'Pending', 'Approved',
                       'Edit triggered chain re-resolve → all levels now skipped')

    result = {'success': True, 'request_id': dsb_id, 'status': new_status, 'changed': [c[0] for c in changes]}
    if dupes:
        result['duplicate_warning'] = True
        result['duplicates'] = dupes
    return _json_ok(result)

# ── Resubmit ──────────────────────────────────────────────────────────────────────────────────
def _api_dsb_resubmit(body_raw, headers):
    import json as _json
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Please log in')

    data   = _json.loads(body_raw or '{}')
    dsb_id = data.get('request_id', '').strip()
    if not dsb_id:
        return _json_err(400, 'request_id required')

    ws           = _get_disbursement_sheet()
    row_idx, row = _dsb_find_row(ws, dsb_id)
    if row_idx is None:
        return _json_err(404, 'Request not found')

    d = _row_to_dict(row)
    if d['submitter_email'].strip().lower() != session['email'].strip().lower():
        return _json_err(403, 'You can only resubmit your own requests')
    if d['status'] not in ('Rejected', 'Rejected by Finance'):
        return _json_err(409, f'Request is {d["status"]!r} — only Rejected or Rejected by Finance can be resubmitted')

    # Re-resolve chain
    try:
        amount = float(str(d['amount']).replace(',', '').strip())
    except Exception:
        amount = 0.0
    budget_owner = d['budget_owner_email']
    chain        = _resolve_disbursement_chain(budget_owner, amount, session['email'])
    l1_email     = chain['l1_email'] or 'Skipped'
    l2_email     = chain['l2_email'] or 'Skipped'
    l1_status    = 'Skipped' if chain['l1_email'] is None else 'Pending'
    l2_status    = 'Skipped' if chain['l2_email'] is None else 'Pending'
    new_status   = 'Approved' if chain['auto_approve'] else ('Pending' if chain['first_active'] == 'l1' else 'Pending L2')

    old_reason = d['reject_reason']
    now_ts = _manila_now()

    # Reset all approval/decision fields
    _ws_write(ws.update_cell, row_idx, 17, new_status)    # status
    _ws_write(ws.update_cell, row_idx, 18, l1_email)      # l1_approver_email
    _ws_write(ws.update_cell, row_idx, 19, l1_status)     # l1_status
    _ws_write(ws.update_cell, row_idx, 20, '')             # l1_ts
    _ws_write(ws.update_cell, row_idx, 21, l2_email)      # l2_approver_email
    _ws_write(ws.update_cell, row_idx, 22, l2_status)     # l2_status
    _ws_write(ws.update_cell, row_idx, 23, '')             # l2_ts
    _ws_write(ws.update_cell, row_idx, 25, '')             # finance_actor
    _ws_write(ws.update_cell, row_idx, 26, '')             # finance_ts
    _ws_write(ws.update_cell, row_idx, 27, '')             # reject_reason cleared
    _ws_write(ws.update_cell, row_idx, 28, now_ts)         # updated_at

    _dsb_audit_log('RESUBMIT', dsb_id, session['email'], session['name'],
                   'status', d['status'], new_status,
                   f'Resubmitted. Prior reason: {old_reason or "—"}')

    # Notify
    try:
        if chain['auto_approve']:
            _notify_dsb_auto_approve(dsb_id, session['name'], session['email'],
                                     amount, d['vendor'])
        elif chain['first_active'] == 'l1':
            _notify_dsb_submit(dsb_id, session['name'], session['email'], amount,
                               d['vendor'], d['description'], d['budget_line_label'],
                               d['budget_flag'], chain['l1_email'])
        else:
            _notify_dsb_advance_to_l2(dsb_id, session['name'], amount, d['vendor'],
                                       'System (L1 skipped)', chain['l2_email'])
    except Exception as e:
        print(f'[dsb-notify] resubmit error: {e}', flush=True)

    return _json_ok({'success': True, 'request_id': dsb_id, 'status': new_status})

# ── Approved for Payment (finance tab) ────────────────────────────────────────────────────────
def _api_dsb_approved_for_payment(qs, headers):
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Please log in')
    if not _is_finance_user(session):
        return _json_err(403, 'Finance team access only')

    ws     = _get_disbursement_sheet()
    rows   = ws.get_all_values()
    result = []
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        d = _row_to_dict(row)
        if d.get('status') == 'Approved':
            result.append(d)
    return _json_ok(result)

# ── Upload Attachment ─────────────────────────────────────────────────────────────────────────
def _api_dsb_upload_attachment(body_raw, headers):
    """Multipart-form style upload embedded in JSON body (base64 encoded)."""
    import json as _json, base64
    token   = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return _json_err(401, 'Please log in')

    data         = _json.loads(body_raw or '{}')
    file_b64     = data.get('file', '')
    filename     = data.get('filename', 'attachment')
    mime_type    = data.get('mime_type', 'application/octet-stream')
    dsb_id       = data.get('request_id', '').strip()

    if not file_b64:
        return _json_err(400, 'file (base64) required')

    try:
        file_data = base64.b64decode(file_b64)
    except Exception:
        return _json_err(400, 'Invalid base64 file data')

    if len(file_data) > 20 * 1024 * 1024:
        return _json_err(400, 'File too large (max 20 MB)')

    try:
        safe_name = f'{dsb_id}_{filename}' if dsb_id else filename
        url       = _upload_attachment(file_data, safe_name, mime_type)
    except Exception as e:
        return _json_err(500, f'Upload failed: {e}')

    return _json_ok({'success': True, 'url': url})

# ── HTML Frontend ─────────────────────────────────────────────────────────────────────────────
_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'disbursement.html')

def serve_disbursement_portal():
    with open(_HTML_PATH, 'r', encoding='utf-8') as f:
        return f.read().encode('utf-8')

# ═══════════════════════════════════════════════════════════════════════════════════════════════
# ROUTER
# ═══════════════════════════════════════════════════════════════════════════════════════════════

def handle_disbursement_api(path, qs, body_raw=None, headers=None):
    """Route /disbursements/api/* requests. Returns (status, content_type, body_bytes, cors)."""
    try:
        if path == '/disbursements/api/login':
            return _api_dsb_login(body_raw)
        elif path == '/disbursements/api/session':
            return _api_dsb_session(headers)
        elif path == '/disbursements/api/submit':
            return _api_dsb_submit(body_raw, headers)
        elif path == '/disbursements/api/requests':
            return _api_dsb_my_requests(qs, headers)
        elif path == '/disbursements/api/pending-approvals':
            return _api_dsb_pending_approvals(qs, headers)
        elif path == '/disbursements/api/approve':
            return _api_dsb_approve(body_raw, headers)
        elif path == '/disbursements/api/reject':
            return _api_dsb_reject(body_raw, headers)
        elif path == '/disbursements/api/finance-reject':
            return _api_dsb_finance_reject(body_raw, headers)
        elif path == '/disbursements/api/pay':
            return _api_dsb_pay(body_raw, headers)
        elif path == '/disbursements/api/cancel':
            return _api_dsb_cancel(body_raw, headers)
        elif path == '/disbursements/api/edit':
            return _api_dsb_edit(body_raw, headers)
        elif path == '/disbursements/api/resubmit':
            return _api_dsb_resubmit(body_raw, headers)
        elif path == '/disbursements/api/approved-for-payment':
            return _api_dsb_approved_for_payment(qs, headers)
        elif path == '/disbursements/api/upload-attachment':
            return _api_dsb_upload_attachment(body_raw, headers)
        elif path == '/disbursements/api/budget-lines':
            return _api_budget_lines(qs, headers)
        elif path == '/disbursements/api/vendor-bank':
            return _api_vendor_bank(qs, headers)
        else:
            import json as _json
            return 404, 'application/json', _json.dumps({'error': 'Not found'}).encode(), False
    except Exception as e:
        import traceback, json as _json
        traceback.print_exc()
        return 500, 'application/json', _json.dumps({'error': str(e)}).encode(), True
