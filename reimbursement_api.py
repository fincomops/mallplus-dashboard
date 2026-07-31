#!/usr/bin/env python3
"""MallPlus Reimbursement Portal — API + HTML Frontend
Data: Google Sheets  |  Receipts: Google Drive  |  Auth: email + PIN
"""

import json, io, os, uuid, time, secrets, hashlib
from datetime import datetime, date
from urllib.parse import parse_qs

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ── Config ─────────────────────────────────────────────────────────────
SHEET_ID         = "1Qmqvafw3QCVrxcWTDMlDZXXtaErrNzFTRxVpL5N9Oc8"
EMPLOYEE_SHEET_ID= "1y-W4bAINYcfT-b_ZH3B9A5_LyQ0xl4lhVTRLYQudPcY"
BANK_SHEET_ID    = "1wh_ujbx4kKi3enH0Gj7t3C6DkD1sXrPMNYz5vE5zQbg"
BANK_GID         = 261168599
DRIVE_FOLDER_ID  = "1FwOC0JglaJKRlI6Ai2fED4TASSxgrXOG"
# Creds: lazy-loaded from env var or file
_CACHED_CREDS_INFO = None

def _get_creds_info():
    """Return creds dict. Evaluated lazily so env vars are picked up at request time."""
    global _CACHED_CREDS_INFO
    if _CACHED_CREDS_INFO is not None:
        return _CACHED_CREDS_INFO

    # 1. Try env var (Railway / production)
    raw = os.environ.get("GOOGLE_CREDS_JSON", "").strip()
    if raw:
        try:
            info = json.loads(raw)
            if "private_key" in info:
                info["private_key"] = info["private_key"].replace("\\n", "\n")
            _CACHED_CREDS_INFO = info
            return _CACHED_CREDS_INFO
        except Exception as e:
            print(f"[WARN] GOOGLE_CREDS_JSON parse failed: {e}", flush=True)

    # 2. Try local files (dev)
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(here, "service_account.json"),
        os.path.join(here, "..", "workspace-finance", "service_account.json"),
        os.path.expanduser("~/.openclaw/workspace-finance/service_account.json"),
    ):
        if os.path.exists(candidate):
            with open(candidate) as f:
                _CACHED_CREDS_INFO = json.load(f)
            return _CACHED_CREDS_INFO

    raise RuntimeError(
        "No credentials found. Set GOOGLE_CREDS_JSON env var "
        "or place service_account.json alongside the server."
    )

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

# ── Department → Approver mapping ──────────────────────────────────────
DEPT_APPROVERS = {
    'Operations':             'Shaun Ochia',
    'Exco':                   'Shaun Ochia',
    'Marketing':              'Shaun Ochia',
    'Commercial':             'Justin Francisco',
    'Product':                'Charm Chua',
    'Finance':                'Shaun Ochia',
    'Admin':                  'Shaun Ochia',
}

APPROVER_EMAILS = {
    'Patt Soyao':        'patt@fincom.asia',
    'Justin Francisco':  'justin@fincom.asia',
    'Charm Chua':        'charm@fincom.asia',
    'Shaun Ochia':       'shaun@fincom.asia',
}

FINAL_APPROVER_EMAIL = 'patt@fincom.asia'
FINAL_APPROVER_NAME  = 'Patt Soyao'

# Departments that escalate to Patt after first approval
_TWO_LEVEL_DEPTS = {'Commercial', 'Product', 'Finance and Admin', 'Finance', 'Admin'}

def _needs_escalation(department):
    """Check if this department needs Patt as final approver."""
    for key in _TWO_LEVEL_DEPTS:
        if key.lower() in department.lower():
            return True
    return False

CATEGORIES = [
    'Travel & Transportation',
    'Meals & Entertainment',
    'Office Supplies & Equipment',
    'Software & Subscriptions',
    'Professional Services',
    'Marketing & Advertising',
    'Utilities & Rent',
    'Training & Development',
    'Other',
]

# ── Lazy connections ───────────────────────────────────────────────────
_gs_client = None
_drive_service = None
_reimb_sheet = None
_emp_sheet = None
_bank_sheet = None

def _get_gs():
    global _gs_client
    if _gs_client is None:
        creds = Credentials.from_service_account_info(_get_creds_info(), scopes=SCOPES)
        _gs_client = gspread.authorize(creds)
    return _gs_client

def _get_drive():
    global _drive_service
    if _drive_service is None:
        creds = Credentials.from_service_account_info(_get_creds_info(), scopes=SCOPES)
        _drive_service = build('drive', 'v3', credentials=creds)
    return _drive_service

def _get_reimb_sheet():
    global _reimb_sheet
    if _reimb_sheet is None:
        sh = _get_gs().open_by_key(SHEET_ID)
        try:
            _reimb_sheet = sh.worksheet('Reimbursements')
        except:
            _reimb_sheet = sh.sheet1
    return _reimb_sheet

def _get_emp_sheet():
    global _emp_sheet
    if _emp_sheet is None:
        sh = _get_gs().open_by_key(EMPLOYEE_SHEET_ID)
        try:
            _emp_sheet = sh.worksheet('Employees')
        except:
            _emp_sheet = sh.sheet1
    return _emp_sheet

def _get_bank_sheet():
    global _bank_sheet
    if _bank_sheet is None:
        sh = _get_gs().open_by_key(BANK_SHEET_ID)
        try:
            _bank_sheet = sh.get_worksheet_by_id(BANK_GID)
        except:
            _bank_sheet = sh.sheet1
    return _bank_sheet

def _load_bank_accounts():
    """Return dict of email → bank_account_no"""
    try:
        ws = _get_bank_sheet()
        rows = ws.get_all_values()
        if not rows:
            return {}
        accounts = {}
        for row in rows[1:]:
            if len(row) >= 4:
                email = (row[2] or '').strip().lower()
                acct = (row[3] or '').strip()
                if email and acct:
                    accounts[email] = acct
        return accounts
    except Exception as e:
        print(f"[bank-accounts] load error: {e}")
        return {}

def _load_employees():
    """Return list of employee dicts from Employees tab."""
    ws = _get_emp_sheet()
    rows = ws.get_all_values()
    if not rows:
        return []
    headers = rows[0]
    employees = []
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        emp = {}
        for i, h in enumerate(headers):
            emp[h.lower().replace(' ', '_')] = row[i] if i < len(row) else ''
        employees.append(emp)
    return employees

def _find_employee(email):
    """Find employee by email (case-insensitive)."""
    for emp in _load_employees():
        if emp.get('email', '').strip().lower() == email.strip().lower():
            return emp
    return None

def _get_approver_for_dept(dept):
    """Return approver name for a department."""
    for key, approver in DEPT_APPROVERS.items():
        if key.lower() in dept.lower():
            return approver
    return 'Patt Soyao'  # default

def _get_approver_email(name):
    return APPROVER_EMAILS.get(name, '')

def _gen_reimb_id():
    """Generate RMB-YYYYMMDD-XXXX format ID."""
    today = datetime.now().strftime('%Y%m%d')
    # Count today's submissions to generate sequence
    ws = _get_reimb_sheet()
    rows = ws.get_all_values()
    count = 0
    today_prefix = f'RMB-{today}'
    for row in rows[1:]:
        if row and row[0].startswith(today_prefix):
            count += 1
    return f'{today_prefix}-{count+1:04d}'

# ── Session management (in-memory, simple) ─────────────────────────────
_sessions = {}  # {token: {email, name, dept, role, expires}}

def _clean_sessions():
    now = time.time()
    expired = [t for t, s in _sessions.items() if s.get('expires', 0) < now]
    for t in expired:
        del _sessions[t]

def _create_session(email, name, dept, role):
    _clean_sessions()
    # Remove existing session for this email
    for t, s in list(_sessions.items()):
        if s.get('email') == email:
            del _sessions[t]
    token = secrets.token_hex(32)
    _sessions[token] = {
        'email': email,
        'name': name,
        'department': dept,
        'role': role,
        'expires': time.time() + 86400  # 24 hours
    }
    return token

def _validate_session(token):
    _clean_sessions()
    return _sessions.get(token)

# ── Drive upload helper ─────────────────────────────────────────────────
def _upload_to_drive(file_data, filename, mime_type):
    """Upload file to Drive folder, return public URL."""
    drive_service = _get_drive()
    media = MediaIoBaseUpload(io.BytesIO(file_data), mimetype=mime_type, resumable=True)
    file_meta = {
        'name': filename,
        'parents': [DRIVE_FOLDER_ID]
    }
    f = drive_service.files().create(body=file_meta, media_body=media, fields='id').execute()
    file_id = f['id']
    # Make publicly readable
    drive_service.permissions().create(
        fileId=file_id,
        body={'type': 'anyone', 'role': 'reader'}
    ).execute()
    return f"https://drive.google.com/file/d/{file_id}/view"

# ── API Handlers ────────────────────────────────────────────────────────
def handle_reimbursement_api(path, qs, body_raw=None, headers=None):
    """Route API requests. Returns (status, content_type, body_bytes, cors)."""
    try:
        if path == '/reimbursements/api/login':
            return _api_login(body_raw)
        elif path == '/reimbursements/api/session':
            return _api_session(headers)
        elif path == '/reimbursements/api/register':
            return _api_register(body_raw)
        elif path == '/reimbursements/api/submit':
            return _api_submit(body_raw, headers)
        elif path == '/reimbursements/api/requests':
            return _api_my_requests(qs, headers)
        elif path == '/reimbursements/api/pending-approvals':
            return _api_pending_approvals(qs, headers)
        elif path == '/reimbursements/api/approve':
            return _api_approve(body_raw, headers)
        elif path == '/reimbursements/api/reject':
            return _api_reject(body_raw, headers)
        elif path == '/reimbursements/api/upload-receipt':
            return _api_upload_receipt(body_raw, headers)
        elif path == '/reimbursements/api/employees':
            return _api_list_employees(headers)
        elif path == '/reimbursements/api/change-pin':
            return _api_change_pin(body_raw, headers)
        elif path == '/reimbursements/api/batch-payment':
            return _api_batch_payment(body_raw, headers)
        elif path == '/reimbursements/api/approved-for-payment':
            return _api_approved_for_payment(headers)
        elif path == '/reimbursements/api/mark-paid':
            return _api_mark_single_paid(body_raw, headers)
        elif path == '/reimbursements/api/stats':
            return _api_stats(qs, headers)
        else:
            return 404, 'application/json', json.dumps({'error': 'Not found'}).encode(), False
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, 'application/json', json.dumps({'error': str(e)}).encode(), True

def _api_login(body_raw):
    data = json.loads(body_raw or '{}')
    email = data.get('email', '').strip()
    pin = data.get('pin', '').strip()
    if not email or not pin:
        return 400, 'application/json', json.dumps({'error': 'Email and PIN required'}).encode(), True

    emp = _find_employee(email)
    if not emp:
        return 401, 'application/json', json.dumps({'error': 'Employee not found. Contact admin to register.'}).encode(), True
    if emp.get('pin', '') != pin:
        return 401, 'application/json', json.dumps({'error': 'Invalid PIN'}).encode(), True
    if emp.get('status', 'Active').strip().lower() != 'active':
        return 403, 'application/json', json.dumps({'error': 'Account is inactive'}).encode(), True

    token = _create_session(email, emp.get('name', ''), emp.get('department', ''), emp.get('role', 'employee'))

    return 200, 'application/json', json.dumps({
        'token': token,
        'name': emp.get('name', ''),
        'email': email,
        'department': emp.get('department', ''),
        'role': emp.get('role', 'employee'),
    }).encode(), True

def _api_session(headers):
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Invalid session'}).encode(), True
    return 200, 'application/json', json.dumps(session).encode(), True

def _api_register(body_raw):
    data = json.loads(body_raw or '{}')
    name = data.get('name', '').strip()
    email = data.get('email', '').strip()
    department = data.get('department', '').strip()
    pin = data.get('pin', '').strip()

    if not all([name, email, department, pin]):
        return 400, 'application/json', json.dumps({'error': 'All fields required'}).encode(), True
    if len(pin) < 4:
        return 400, 'application/json', json.dumps({'error': 'PIN must be at least 4 characters'}).encode(), True

    # Check if already exists
    existing = _find_employee(email)
    if existing:
        return 409, 'application/json', json.dumps({'error': 'Email already registered'}).encode(), True

    # Add to Employees sheet
    ws = _get_emp_sheet()
    ws.append_row([
        name, email, department, pin, 'employee', 'Active',
        datetime.now().strftime('%Y-%m-%d')
    ])
    # Clear cache
    global _emp_sheet
    _emp_sheet = None

    return 200, 'application/json', json.dumps({'success': True, 'message': 'Registration successful'}).encode(), True

def _api_submit(body_raw, headers):
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    data = json.loads(body_raw or '{}')
    employee_name   = session['name']
    employee_email  = session['email']
    department      = session['department']
    purchase_date   = data.get('purchase_date', '').strip()
    amount          = data.get('amount', '').strip()
    category        = data.get('category', '').strip()
    purpose         = data.get('purpose', '').strip()
    receipt_url     = data.get('receipt_url', '').strip()
    receipt_hash    = data.get('receipt_hash', '').strip()
    vendor          = data.get('vendor', '').strip()
    invoice_number  = data.get('invoice_number', '').strip()
    vat_status      = data.get('vat_status', '').strip()
    notes           = data.get('notes', '').strip()

    if not all([purchase_date, amount, category, purpose, vendor, vat_status]):
        return 400, 'application/json', json.dumps({'error': 'Purchase date, amount, category, purpose, vendor, and VAT Status are required'}).encode(), True

    try:
        amt = float(amount.replace(',', '').replace('₱', '').strip())
        if amt <= 0:
            raise ValueError
    except:
        return 400, 'application/json', json.dumps({'error': 'Invalid amount'}).encode(), True

    # ── Duplicate checks ──
    dupes = []
    ws = _get_reimb_sheet()
    rows = ws.get_all_values()

    # A: File hash check
    if receipt_hash:
        for row in rows[1:]:
            existing_hash = row[19].strip() if len(row) > 19 else ''
            if existing_hash and existing_hash == receipt_hash:
                dupes.append({
                    'type': 'identical_receipt',
                    'reimbursement_id': row[0],
                    'message': f"This exact receipt was already submitted as {row[0]} on {row[5]} by {row[2]}"
                })
                break

    # B: Vendor + Date + Amount match
    for row in rows[1:]:
        existing_vendor = row[17].strip().lower() if len(row) > 17 else ''
        existing_date = row[5].strip() if len(row) > 5 else ''
        existing_amt = row[6].strip() if len(row) > 6 else ''
        if existing_vendor and existing_vendor == vendor.lower():
            if existing_date == purchase_date and existing_amt == f'{amt:.2f}':
                dupes.append({
                    'type': 'matching_fields',
                    'reimbursement_id': row[0],
                    'message': f"Same vendor, date, and amount already submitted as {row[0]} by {row[2]}"
                })

    # Determine approver — Shaun's own requests go to Patt (no self-approval)
    if employee_email.strip().lower() == 'shaun@fincom.asia':
        approver_name = FINAL_APPROVER_NAME
        approver_email = FINAL_APPROVER_EMAIL
    else:
        approver_name = _get_approver_for_dept(department)
        approver_email = _get_approver_email(approver_name)

    # Generate ID
    reimb_id = _gen_reimb_id()
    timestamp = datetime.now().isoformat()

    # Append to sheet
    ws.append_row([
        reimb_id, timestamp, employee_name, employee_email,
        department, purchase_date, f'{amt:.2f}', category, purpose,
        receipt_url, 'Pending', approver_email, approver_name,
        '', '', '', notes, vendor, invoice_number, receipt_hash, vat_status
    ])

    result = {
        'success': True,
        'reimbursement_id': reimb_id,
        'approver': approver_name,
    }
    if dupes:
        result['duplicate_warning'] = True
        result['duplicates'] = dupes

    return 200, 'application/json', json.dumps(result).encode(), True

def _api_my_requests(qs, headers):
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    email = session['email']
    ws = _get_reimb_sheet()
    rows = ws.get_all_values()
    if not rows:
        return 200, 'application/json', json.dumps([]).encode(), True

    headers_row = [h.strip().lower().replace(' ', '_') for h in rows[0]]
    results = []
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        item = {}
        for i, h in enumerate(headers_row):
            item[h] = row[i] if i < len(row) else ''
        if item.get('employee_email', '').strip().lower() == email.strip().lower():
            results.append(item)

    # Reverse chronological
    results.reverse()
    return 200, 'application/json', json.dumps(results).encode(), True

def _api_pending_approvals(qs, headers):
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    if session.get('role') != 'approver':
        return 403, 'application/json', json.dumps({'error': 'Approver access only'}).encode(), True

    approver_email = session['email'].strip().lower()
    is_patt = approver_email == FINAL_APPROVER_EMAIL
    ws = _get_reimb_sheet()
    rows = ws.get_all_values()
    if not rows:
        return 200, 'application/json', json.dumps([]).encode(), True

    headers_row = [h.strip().lower().replace(' ', '_') for h in rows[0]]
    results = []
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        item = {}
        for i, h in enumerate(headers_row):
            item[h] = row[i] if i < len(row) else ''
        row_status = item.get('status', '').strip()

        if is_patt:
            # Patt sees: Pending (his direct depts) + Pending Final (escalated from any dept)
            row_approver = item.get('approver_email', '').strip().lower()
            if row_status == 'Pending' and row_approver == FINAL_APPROVER_EMAIL:
                results.append(item)
            elif row_status == 'Pending Final':
                results.append(item)
        else:
            # Dept approver sees only their Pending items
            row_approver = item.get('approver_email', '').strip().lower()
            if row_approver == approver_email and row_status == 'Pending':
                results.append(item)

    results.reverse()
    return 200, 'application/json', json.dumps(results).encode(), True

def _api_approve(body_raw, headers):
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    if session.get('role') != 'approver':
        return 403, 'application/json', json.dumps({'error': 'Approver access only'}).encode(), True

    data = json.loads(body_raw or '{}')
    reimb_id = data.get('reimbursement_id', '').strip()
    if not reimb_id:
        return 400, 'application/json', json.dumps({'error': 'Reimbursement ID required'}).encode(), True

    # Find and update the row
    ws = _get_reimb_sheet()
    rows = ws.get_all_values()
    approver_email_session = session['email'].strip().lower()
    is_patt = approver_email_session == FINAL_APPROVER_EMAIL

    for i, row in enumerate(rows):
        if row and row[0].strip() == reimb_id:
            row_idx = i + 1  # 1-based
            row_status = row[10].strip() if len(row) > 10 else ''
            row_approver = row[11].strip().lower() if len(row) > 11 else ''

            if is_patt:
                # Patt can approve: "Pending" (his direct depts) or "Pending Final" (escalated)
                if row_status == 'Pending' and row_approver == FINAL_APPROVER_EMAIL:
                    pass  # Patt's direct department — final approval
                elif row_status == 'Pending Final':
                    pass  # Escalated from dept approver — final approval
                else:
                    return 403, 'application/json', json.dumps({'error': 'Not awaiting your approval (status: ' + row_status + ')'}).encode(), True

                ws.update_cell(row_idx, 11, 'Approved')
                ws.update_cell(row_idx, 13, session['name'])
                ws.update_cell(row_idx, 14, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                return 200, 'application/json', json.dumps({'success': True, 'message': 'Final approval granted'}).encode(), True

            else:
                # Dept approver (Shaun/Justin/Charm)
                if row_approver != approver_email_session:
                    return 403, 'application/json', json.dumps({'error': 'Not your department to approve'}).encode(), True
                if row_status != 'Pending':
                    return 403, 'application/json', json.dumps({'error': 'Request is not in Pending status'}).encode(), True

                # Check amount — only escalate to Patt if > ₱5,000
                try:
                    amt = float(row[6].replace(',', '').strip()) if row[6].strip() else 0
                except:
                    amt = 0

                if amt > 5000:
                    # Escalate to Patt for final approval
                    ws.update_cell(row_idx, 11, 'Pending Final')
                    ws.update_cell(row_idx, 13, session['name'])
                    ws.update_cell(row_idx, 14, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                    return 200, 'application/json', json.dumps({
                        'success': True,
                        'message': 'Approved — escalated to Patt Soyao for final sign-off (amount > ₱5,000)'
                    }).encode(), True
                else:
                    # ≤ ₱5,000 — dept approver can give final approval directly
                    ws.update_cell(row_idx, 11, 'Approved')
                    ws.update_cell(row_idx, 13, session['name'])
                    ws.update_cell(row_idx, 14, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                    return 200, 'application/json', json.dumps({
                        'success': True,
                        'message': 'Final approval granted (within dept threshold ₱5,000)'
                    }).encode(), True

    return 404, 'application/json', json.dumps({'error': 'Reimbursement not found'}).encode(), True

def _api_reject(body_raw, headers):
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    if session.get('role') != 'approver':
        return 403, 'application/json', json.dumps({'error': 'Approver access only'}).encode(), True

    data = json.loads(body_raw or '{}')
    reimb_id = data.get('reimbursement_id', '').strip()
    reason = data.get('reason', '').strip()
    if not reimb_id:
        return 400, 'application/json', json.dumps({'error': 'Reimbursement ID required'}).encode(), True
    if not reason:
        return 400, 'application/json', json.dumps({'error': 'Rejection reason required'}).encode(), True

    ws = _get_reimb_sheet()
    rows = ws.get_all_values()
    approver_email_session = session['email'].strip().lower()
    is_patt = approver_email_session == FINAL_APPROVER_EMAIL

    for i, row in enumerate(rows):
        if row and row[0].strip() == reimb_id:
            row_idx = i + 1
            row_status = row[10].strip() if len(row) > 10 else ''
            row_approver = row[11].strip().lower() if len(row) > 11 else ''

            if is_patt:
                # Patt can reject: "Pending" (his direct depts) or "Pending Final" (escalated)
                if row_status == 'Pending' and row_approver == FINAL_APPROVER_EMAIL:
                    new_status = 'Rejected'
                elif row_status == 'Pending Final':
                    new_status = 'Rejected Final'
                else:
                    return 403, 'application/json', json.dumps({'error': 'Not awaiting your approval (status: ' + row_status + ')'}).encode(), True
            else:
                if row_approver != approver_email_session:
                    return 403, 'application/json', json.dumps({'error': 'Not your department to reject'}).encode(), True
                if row_status != 'Pending':
                    return 403, 'application/json', json.dumps({'error': 'Request is not in Pending status'}).encode(), True
                new_status = 'Rejected'

            ws.update_cell(row_idx, 11, new_status)
            ws.update_cell(row_idx, 13, session['name'])
            ws.update_cell(row_idx, 14, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            ws.update_cell(row_idx, 15, reason)
            return 200, 'application/json', json.dumps({'success': True}).encode(), True

    return 404, 'application/json', json.dumps({'error': 'Reimbursement not found'}).encode(), True

RECEIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'receipts')

if not os.path.exists(RECEIPTS_DIR):
    os.makedirs(RECEIPTS_DIR, exist_ok=True)

def _api_upload_receipt(body_raw, headers):
    """Handle file upload. Saves locally, returns URL + SHA-256 hash."""
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    if not body_raw:
        return 400, 'application/json', json.dumps({'error': 'No file uploaded'}).encode(), True

    # Compute hash immediately
    file_hash = hashlib.sha256(body_raw).hexdigest()

    # Generate unique filename
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"receipt_{session['name'].replace(' ','_')}_{ts}.pdf"

    # Determine mime type and extension
    ext = '.pdf'
    content_type = 'application/pdf'
    if body_raw[:4] == b'\x89PNG':
        content_type = 'image/png'
        ext = '.png'
        filename = filename.replace('.pdf', '.png')
    elif body_raw[:2] == b'\xff\xd8':
        content_type = 'image/jpeg'
        ext = '.jpg'
        filename = filename.replace('.pdf', '.jpg')
    elif body_raw[:4] == b'RIFF':
        content_type = 'image/webp'
        ext = '.webp'
        filename = filename.replace('.pdf', '.webp')

    try:
        # Save locally
        filepath = os.path.join(RECEIPTS_DIR, filename)
        with open(filepath, 'wb') as f:
            f.write(body_raw)
        
        # URL: /reimbursements/receipts/<filename>
        url = f'/reimbursements/receipts/{filename}'
        return 200, 'application/json', json.dumps({
            'url': url, 'filename': filename, 'hash': file_hash
        }).encode(), True
    except Exception as e:
        return 500, 'application/json', json.dumps({'error': f'Upload failed: {str(e)}'}).encode(), True

def _api_list_employees(headers):
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    employees = _load_employees()
    # Don't expose PINs
    safe = []
    for e in employees:
        safe.append({
            'name': e.get('name', ''),
            'email': e.get('email', ''),
            'department': e.get('department', ''),
            'role': e.get('role', 'employee'),
        })
    return 200, 'application/json', json.dumps(safe).encode(), True

def _api_change_pin(body_raw, headers):
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    data = json.loads(body_raw or '{}')
    current_pin = data.get('current_pin', '').strip()
    new_pin = data.get('new_pin', '').strip()

    if not current_pin or not new_pin:
        return 400, 'application/json', json.dumps({'error': 'Current PIN and new PIN are required'}).encode(), True
    if len(new_pin) < 4:
        return 400, 'application/json', json.dumps({'error': 'New PIN must be at least 4 characters'}).encode(), True
    if current_pin == new_pin:
        return 400, 'application/json', json.dumps({'error': 'New PIN must be different from current PIN'}).encode(), True

    # Verify current PIN
    email = session['email']
    emp = _find_employee(email)
    if not emp:
        return 404, 'application/json', json.dumps({'error': 'Employee record not found'}).encode(), True
    if emp.get('pin', '') != current_pin:
        return 401, 'application/json', json.dumps({'error': 'Current PIN is incorrect'}).encode(), True

    # Update PIN in sheet
    ws = _get_emp_sheet()
    rows = ws.get_all_values()
    for i, row in enumerate(rows):
        if len(row) > 1 and row[1].strip().lower() == email.strip().lower():
            ws.update_cell(i + 1, 4, new_pin)
            return 200, 'application/json', json.dumps({'success': True, 'message': 'PIN updated successfully'}).encode(), True

    return 404, 'application/json', json.dumps({'error': 'Employee record not found'}).encode(), True


def _api_batch_payment(body_raw, headers):
    """Process batch payment CSV upload. Returns report of matched/unmatched/already-paid."""
    import csv, io as _io

    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    # Only Finance & Admin department
    dept = session.get('department', '')
    email = session.get('email', '').strip().lower()
    is_finance = any(k.lower() in dept.lower() for k in ('finance',))
    if not is_finance:
        return 403, 'application/json', json.dumps({'error': 'Only Finance team can upload batch payments'}).encode(), True

    if not body_raw:
        return 400, 'application/json', json.dumps({'error': 'No CSV data provided'}).encode(), True

    # Parse CSV from body
    try:
        reader = csv.reader(_io.StringIO(body_raw.decode('utf-8', errors='replace')))
        rows = list(reader)
    except Exception as e:
        return 400, 'application/json', json.dumps({'error': f'Invalid CSV format: {str(e)}'}).encode(), True

    if not rows:
        return 400, 'application/json', json.dumps({'error': 'CSV is empty'}).encode(), True

    # Detect header — first row may or may not have headers
    # Try: if first cell looks like an ID, treat as data; otherwise skip as header
    first_cell = (rows[0][0] or '').strip().upper()
    data_rows = rows
    header_skipped = False
    if not first_cell.startswith('RMB-'):
        data_rows = rows[1:]
        header_skipped = True

    ws = _get_reimb_sheet()
    sheet_rows = ws.get_all_values()
    # Build lookup: {reimb_id: {row_idx, status, current_payment_date}}
    lookup = {}
    for i, row in enumerate(sheet_rows[1:], start=2):  # 2-based with header
        if row and row[0].strip():
            rid = row[0].strip()
            lookup[rid] = {
                'row_idx': i,
                'status': row[10].strip() if len(row) > 10 else '',
                'current_payment_date': row[15].strip() if len(row) > 15 else '',
            }

    matched = []
    not_found = []
    already_paid = []
    skipped_status = []

    for row in data_rows:
        if not row or not any(c.strip() for c in row):
            continue
        rid = row[0].strip() if len(row) > 0 else ''
        payment_date = row[1].strip() if len(row) > 1 else ''
        payment_ref = row[2].strip() if len(row) > 2 else ''

        if not rid:
            continue

        entry = lookup.get(rid)
        if not entry:
            not_found.append({'reimbursement_id': rid, 'reason': 'Not found'})
            continue

        current_status = entry['status']
        if current_status == 'Paid':
            already_paid.append({'reimbursement_id': rid, 'reason': 'Already marked as Paid'})
            continue
        if current_status != 'Approved':
            skipped_status.append({
                'reimbursement_id': rid,
                'reason': f'Status is "{current_status}" — only Approved can be marked Paid'
            })
            continue

        # Update: col 11 = status, col 16 = payment_date, col 17 = notes (append ref)
        row_idx = entry['row_idx']
        if not payment_date:
            payment_date = datetime.now().strftime('%Y-%m-%d')
        ws.update_cell(row_idx, 11, 'Paid')
        ws.update_cell(row_idx, 16, payment_date)

        if payment_ref:
            # Append payment ref to existing notes
            existing_notes = sheet_rows[row_idx - 1][16].strip() if len(sheet_rows[row_idx - 1]) > 16 else ''
            new_note = f'[Payment Ref: {payment_ref}]'
            updated_notes = f'{existing_notes} {new_note}'.strip()
            ws.update_cell(row_idx, 17, updated_notes)

        matched.append({
            'reimbursement_id': rid,
            'payment_date': payment_date,
            'payment_ref': payment_ref,
        })

    # Build report
    report = {
        'total_processed': len(matched) + len(not_found) + len(already_paid) + len(skipped_status),
        'matched': len(matched),
        'not_found': len(not_found),
        'already_paid': len(already_paid),
        'skipped_wrong_status': len(skipped_status),
        'details': {
            'updated': matched,
            'not_found': not_found,
            'already_paid': already_paid,
            'skipped_wrong_status': skipped_status,
        },
        'processed_by': session['name'],
        'timestamp': datetime.now().isoformat(),
    }

    return 200, 'application/json', json.dumps(report).encode(), True


def _api_approved_for_payment(headers):
    """Return all Approved reimbursements for Finance manual payment review."""
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    dept = session.get('department', '')
    is_finance = any(k.lower() in dept.lower() for k in ('finance',))
    if not is_finance:
        return 403, 'application/json', json.dumps({'error': 'Only Finance team can process payments'}).encode(), True

    ws = _get_reimb_sheet()
    rows = ws.get_all_values()
    if not rows:
        return 200, 'application/json', json.dumps([]).encode(), True

    headers_row = [h.strip().lower().replace(' ', '_') for h in rows[0]]
    results = []
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        item = {}
        for i, h in enumerate(headers_row):
            item[h] = row[i] if i < len(row) else ''
        status = item.get('status', '').strip()
        if status == 'Approved':
            results.append(item)

    results.reverse()

    # Enrich with bank account numbers
    bank_accounts = _load_bank_accounts()
    for item in results:
        email = (item.get('employee_email', '') or '').strip().lower()
        item['bank_account'] = bank_accounts.get(email, '')

    return 200, 'application/json', json.dumps(results).encode(), True


def _api_mark_single_paid(body_raw, headers):
    """Mark a single reimbursement as Paid with date and reference."""
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    dept = session.get('department', '')
    is_finance = any(k.lower() in dept.lower() for k in ('finance',))
    if not is_finance:
        return 403, 'application/json', json.dumps({'error': 'Only Finance team can process payments'}).encode(), True

    data = json.loads(body_raw or '{}')
    reimb_id = data.get('reimbursement_id', '').strip()
    payment_date = data.get('payment_date', '').strip()
    payment_ref = data.get('payment_ref', '').strip()

    if not reimb_id:
        return 400, 'application/json', json.dumps({'error': 'Reimbursement ID required'}).encode(), True

    ws = _get_reimb_sheet()
    rows = ws.get_all_values()
    for i, row in enumerate(rows):
        if row and row[0].strip() == reimb_id:
            row_idx = i + 1
            status = row[10].strip() if len(row) > 10 else ''
            if status == 'Paid':
                return 409, 'application/json', json.dumps({'error': 'Already marked as Paid'}).encode(), True
            if status != 'Approved':
                return 400, 'application/json', json.dumps({'error': f'Status is "{status}" — only Approved can be marked Paid'}).encode(), True

            if not payment_date:
                payment_date = datetime.now().strftime('%Y-%m-%d')

            ws.update_cell(row_idx, 11, 'Paid')
            ws.update_cell(row_idx, 16, payment_date)

            if payment_ref:
                existing_notes = row[16].strip() if len(row) > 16 else ''
                updated_notes = f'{existing_notes} [Payment Ref: {payment_ref}]'.strip()
                ws.update_cell(row_idx, 17, updated_notes)

            return 200, 'application/json', json.dumps({
                'success': True,
                'reimbursement_id': reimb_id,
                'payment_date': payment_date,
                'payment_ref': payment_ref,
            }).encode(), True

    return 404, 'application/json', json.dumps({'error': 'Reimbursement not found'}).encode(), True


def _api_stats(qs, headers):
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    ws = _get_reimb_sheet()
    rows = ws.get_all_values()
    if not rows:
        return 200, 'application/json', json.dumps({'total': 0, 'pending': 0, 'approved': 0, 'rejected': 0, 'paid': 0}).encode(), True

    user_email = session['email']
    total = pending = approved = rejected = paid = 0

    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        row_email = row[3].strip().lower() if len(row) > 3 else ''
        row_status = row[10].strip() if len(row) > 10 else ''

        # Stats always count the user's own submissions (employee_email), even if they're also an approver
        if row_email != user_email.strip().lower():
            continue

        total += 1
        if row_status in ('Pending', 'Pending Final'):
            pending += 1
        elif row_status == 'Approved':
            approved += 1
        elif row_status in ('Rejected', 'Rejected Final'):
            rejected += 1
        elif row_status == 'Paid':
            paid += 1

    return 200, 'application/json', json.dumps({
        'total': total, 'pending': pending, 'approved': approved,
        'rejected': rejected, 'paid': paid
    }).encode(), True


def _extract_token(headers):
    """Extract Bearer token from Authorization header."""
    if not headers:
        return None
    auth = headers.get('Authorization', '') or headers.get('authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return None


# ═══════════════════════════════════════════════════════════════════════
# HTML FRONTEND — Serve from external file
# ═══════════════════════════════════════════════════════════════════════

_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reimbursement.html')

def serve_reimbursement_portal():
    """Return the full HTML portal page as bytes."""
    with open(_HTML_PATH, 'r', encoding='utf-8') as f:
        return f.read().encode('utf-8')