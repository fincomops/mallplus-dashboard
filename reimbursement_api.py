#!/usr/bin/env python3
"""MallPlus Reimbursement Portal — API + HTML Frontend
Data: Google Sheets  |  Receipts: Google Drive  |  Auth: email + PIN
"""

import json, io, os, uuid, time, secrets, hashlib, hmac, base64, smtplib
from datetime import datetime, date
from urllib.parse import parse_qs
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ── Config ─────────────────────────────────────────────────────────────
SHEET_ID         = "1Qmqvafw3QCVrxcWTDMlDZXXtaErrNzFTRxVpL5N9Oc8"
EMPLOYEE_SHEET_ID= "1y-W4bAINYcfT-b_ZH3B9A5_LyQ0xl4lhVTRLYQudPcY"
BANK_SHEET_ID    = "1wh_ujbx4kKi3enH0Gj7t3C6DkD1sXrPMNYz5vE5zQbg"
BANK_GID         = 261168599
DRIVE_FOLDER_ID  = "1-09HyHykvrxaSEd5qfntlyXNbQ53TDlr"  # Shared Drive "Finance AI Tools" (SA has no My-Drive quota)

# Lazy-loaded creds: env var GOOGLE_CREDS_JSON → local file → workspace file
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

    # 2. Try local file (dev)
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

# ── Portal / Email constants (restored Aug 13, 2026 — were accidentally deleted in v2 rewrite) ──
PORTAL_URL = 'https://fcos.fincom.asia'
SMTP_HOST  = 'smtp.gmail.com'
SMTP_PORT  = 587
SMTP_USER  = 'reimbursement@fincom.asia'

DEFAULT_FINAL_APPROVER_EMAIL = 'patt@fincom.asia'
DEFAULT_FINAL_APPROVER_NAME  = 'Patt Soyao'

# ── SSO / Signed Sessions ──────────────────────────────────────────────────
# When AUTH_SECRET is set, sessions are stateless signed tokens (cross-machine SSO).
# When AUTH_SECRET is empty, behaves exactly as before (in-memory dict only).
AUTH_SECRET = os.environ.get('AUTH_SECRET', '')

# Finance team — explicit allowlist (Shaun, Aug 19 2026). ONLY these members can
# Pay or Reject Approved reimbursements. Email allowlist (not dept label, not role)
# is the source of truth — Emmaly/Fatima are role=employee but still finance.
FINANCE_TEAM_EMAILS = {
    'shaun@fincom.asia',
    'emmaly@fincom.asia',
    'fatima@fincom.asia',
}

# FS/Board Exec allowlist (Shaun-confirmed 2026-08-19).
# NOTE: Finance department access is roster-driven (see _is_finance_employee) so ALL
# Finance employees (shaun, joan, emmaly, jasmine, rose, fatima) get Recon + FS
# (Shaun-confirmed 2026-08-20). JB is NOT in this list per Shaun's explicit instruction.
FS_VISIBLE_EMAILS = {
    'patt@fincom.asia',
    'justin@fincom.asia',
    'charm@fincom.asia',
}

# Board Presentations — explicit allowlist ONLY (Shaun-confirmed 2026-08-20):
# Shaun, Patt, Justin. NOT roster-driven (Charm/finance roster don't get board).
# NOTE: used as FALLBACK when the PortalAccess tab is missing/empty.
BOARD_VISIBLE_EMAILS = {
    'shaun@fincom.asia',
    'patt@fincom.asia',
    'justin@fincom.asia',
}


# ── Portal access — sheet-driven (PortalAccess tab, Aug 20 2026) ──────────
# Shaun: "Sheet just documents who has access... I can just edit the gsheet."
# The PortalAccess tab in the employee workbook is the SOURCE OF TRUTH for
# Recon / FS / Board visibility AND the Finance Pay/Reject action gate.
# Approvers (ApprovalConfig) stay in the original source — untouched.
# Fallback: if the tab is missing or has no data rows, today's rules apply
# (roster + allowlists) so nothing breaks.
_portal_access_cache = None      # (dict email->{recon,fs,board,pay_reject}, exists)
_portal_access_cache_time = 0
_PORTAL_COLUMNS = ('email', 'recon', 'fs', 'board', 'pay_reject')
_PORTAL_YES = {'yes', 'true', 'y', '1', '\u2713', '\u2714'}


def _load_portal_access():
    """Read PortalAccess tab from the employee workbook. Cached 60s.
    Returns (dict email -> {recon,fs,board,pay_reject}, exists_bool).
    exists=False when the tab is missing OR has no data rows → caller falls back."""
    global _portal_access_cache, _portal_access_cache_time
    now = time.time()
    if _portal_access_cache is not None and now - _portal_access_cache_time < 60:
        return _portal_access_cache
    try:
        sh = _get_gs().open_by_key(EMPLOYEE_SHEET_ID)
        ws = sh.worksheet('PortalAccess')
        rows = ws.get_all_values()
    except Exception as e:
        print(f'[_load_portal_access] tab missing/error: {e}', flush=True)
        _portal_access_cache = ({}, False)
        _portal_access_cache_time = now
        return _portal_access_cache
    if len(rows) < 2:
        _portal_access_cache = ({}, False)
        _portal_access_cache_time = now
        return _portal_access_cache
    headers = [h.strip().lower().replace(' ', '_').replace('/', '_') for h in rows[0]]
    access = {}
    for row in rows[1:]:
        if not any(str(c).strip() for c in row):
            continue
        entry = {}
        email = ''
        for i, h in enumerate(headers):
            if h not in _PORTAL_COLUMNS:
                continue
            val = str(row[i]).strip().lower() if i < len(row) else ''
            if h == 'email':
                email = val
            else:
                entry[h] = val in _PORTAL_YES
        if email:
            access[email] = entry
    exists = bool(access)
    _portal_access_cache = (access, exists)
    _portal_access_cache_time = now
    return _portal_access_cache


def _can_access(email, portal):
    """Sheet-driven portal access. portal: recon | fs | board | pay_reject.
    Fallback when the PortalAccess tab is missing/empty:
      recon → finance roster; fs → roster + exec; board → BOARD_VISIBLE_EMAILS;
      pay_reject → FINANCE_TEAM_EMAILS."""
    email = (email or '').strip().lower()
    access, exists = _load_portal_access()
    if exists:
        return bool((access.get(email) or {}).get(portal, False))
    if portal == 'recon':
        return _is_finance_employee(email)
    if portal == 'fs':
        return _can_view_fs(email)
    if portal == 'board':
        return email in BOARD_VISIBLE_EMAILS
    if portal == 'pay_reject':
        return email in FINANCE_TEAM_EMAILS
    return False


def _is_finance_user(session):
    """Return True if the session user can Pay/Reject Approved reimbursements.
    Sheet-driven (PortalAccess → Pay/Reject) since Aug 20 2026; falls back to
    FINANCE_TEAM_EMAILS when the tab is missing/empty."""
    return _can_access((session or {}).get('email', ''), 'pay_reject')


_finance_emp_cache = {}      # email -> bool (is Finance)
_finance_emp_cache_time = 0


def _is_finance_employee(email):
    """Roster-driven portal access: True if the employee record's department == Finance.
    Source of truth = Employees sheet, so new finance hires get Recon + FS automatically.
    Legacy FINANCE_TEAM_EMAILS members always pass (safety fallback if roster entry is missing).
    Cached 60s to keep /recon API hot path off Google Sheets."""
    global _finance_emp_cache, _finance_emp_cache_time
    email = (email or '').strip().lower()
    if email in FINANCE_TEAM_EMAILS:
        return True
    now = time.time()
    if now - _finance_emp_cache_time > 60:
        _finance_emp_cache = {}
        _finance_emp_cache_time = now
    elif email in _finance_emp_cache:
        return _finance_emp_cache[email]
    emp = None
    try:
        emp = _find_employee(email)
    except Exception as e:
        # Sheets hiccup — deny rather than crash the recon hot path
        print(f'[_is_finance_employee] lookup failed for {email}: {e}', flush=True)
    is_fin = bool(emp and emp.get('department', '').strip().lower() == 'finance')
    _finance_emp_cache[email] = is_fin
    return is_fin


def _can_view_fs(email):
    """Financial Statements + Board access: Finance department (roster) or Exec allowlist."""
    email = (email or '').strip().lower()
    return _is_finance_employee(email) or email in FS_VISIBLE_EMAILS


# ═══════════════════════════════════════════════════════════════════════
# APPROVAL MATRIX — Config-Driven (v2, Aug 12, 2026)
# ═══════════════════════════════════════════════════════════════════════

_approval_matrix_cache = None
_approval_matrix_cache_time = 0

def _load_approval_matrix():
    """Read ApprovalConfig tab from Employee sheet.
    Returns list of tier dicts: {department, min_amount, max_amount, level_1, level_2, level_3}
    Cached for 5 minutes."""
    global _approval_matrix_cache, _approval_matrix_cache_time
    now = time.time()
    if _approval_matrix_cache is not None and (now - _approval_matrix_cache_time) < 300:
        return _approval_matrix_cache
    try:
        sh = _get_gs().open_by_key(EMPLOYEE_SHEET_ID)
        ws = sh.worksheet('ApprovalConfig')
    except:
        print('[matrix] ApprovalConfig tab not found', flush=True)
        return []
    rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    headers = [h.strip().lower().replace(' ', '_') for h in rows[0]]
    configs = []
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        cfg = {}
        for i, h in enumerate(headers):
            cfg[h] = row[i].strip() if i < len(row) else ''
        try:
            cfg['min_amount'] = float(cfg['min_amount']) if cfg.get('min_amount') else 0.0
        except:
            cfg['min_amount'] = 0.0
        try:
            cfg['max_amount'] = float(cfg['max_amount']) if cfg.get('max_amount') else None
        except:
            cfg['max_amount'] = None
        configs.append(cfg)
    _approval_matrix_cache = configs
    _approval_matrix_cache_time = now
    print(f'[matrix] Loaded {len(configs)} approval config rows', flush=True)
    return configs


def _find_approval_chain(department, amount):
    """Find the matching approval chain for department + amount."""
    configs = _load_approval_matrix()
    dept_lower = (department or '').strip().lower()
    if not dept_lower or not configs:
        return None
    candidates = [c for c in configs
                  if c.get('department', '').strip().lower() in dept_lower
                  or dept_lower in c.get('department', '').strip().lower()]
    if not candidates:
        return None
    for cfg in candidates:
        min_amt = cfg.get('min_amount', 0)
        max_amt = cfg.get('max_amount', None)
        if amount >= min_amt and (max_amt is None or amount <= max_amt):
            return cfg
    return candidates[0]  # fallback


def _parse_approver_emails(emails_str):
    """Parse comma-separated email string into list of cleaned emails."""
    return [e.strip().lower() for e in (emails_str or '').split(',') if e.strip()]


def _resolve_chain(chain, submitter_email):
    """Resolve the approval chain for a submitter, skipping self-approval levels.

    If the submitter is a member of a level's approver group, the WHOLE level
    is skipped and the request escalates to the next level. This prevents
    peer-to-peer approval (e.g. Commercial level 1 = mhike + jon — neither
    should approve the other's request).

    Returns dict: {level_1: [...], level_2: [...], level_3: [...], levels: [active 1-based indices]}.
    """
    result = {'levels': []}
    sub = (submitter_email or '').strip().lower()
    for i in range(1, 4):
        key = f'level_{i}'
        raw = _parse_approver_emails(chain.get(key, ''))
        if sub in raw:
            result[key] = []          # submitter is an approver here → skip entire level
        else:
            result[key] = raw
        if result[key]:
            result['levels'].append(i)
    return result


def _first_active_level(resolved):
    """Return (level_index, emails) for the first non-empty level, or (None, [])."""
    for i in range(1, 4):
        emails = resolved.get(f'level_{i}', [])
        if emails:
            return (i, emails)
    return (None, [])


def _level_status(level_index):
    """Convert 1-based level to status string."""
    return {1: 'Pending', 2: 'Pending Second', 3: 'Pending Final'}.get(level_index, 'Pending')


def _status_level(status):
    """Convert status string to 1-based level index. Returns None if not a pending status."""
    s = (status or '').strip()
    return {'Pending': 1, 'Pending Second': 2, 'Pending Final': 3}.get(s)


def _lookup_name(email):
    """Look up display name for an email."""
    email_lower = email.strip().lower()
    for emp in _load_employees():
        if (emp.get('email', '') or '').strip().lower() == email_lower:
            return emp.get('name', email)
    known = {
        'shaun@fincom.asia': 'Shaun Ochia',
        'justin@fincom.asia': 'Justin Francisco',
        'charm@fincom.asia': 'Charm Chua',
        'patt@fincom.asia': 'Patt Soyao',
        'mhike@fincom.asia': 'Mhike Estipular',
        'jon@fincom.asia': 'Jon Banaag',
    }
    return known.get(email_lower, email)


# ═══════════════════════════════════════════════════════════════════════
# SHEET-BASED CONFIG + EMAIL SENDER (restored Aug 13, 2026)
# ═══════════════════════════════════════════════════════════════════════

_config_cache = None
_config_cache_time = 0

def _load_config():
    """Read key-value pairs from the Config tab of the employee sheet."""
    global _config_cache, _config_cache_time
    now = time.time()
    if _config_cache is not None and (now - _config_cache_time) < 300:
        return _config_cache
    try:
        sh = _get_gs().open_by_key(EMPLOYEE_SHEET_ID)
        try:
            ws = sh.worksheet('Config')
        except:
            ws = sh.add_worksheet(title='Config', rows=10, cols=3)
        rows = ws.get_all_values()
        config = {}
        for row in rows[1:]:
            if len(row) >= 2 and row[0].strip():
                config[row[0].strip()] = row[1].strip()
        _config_cache = config
        _config_cache_time = now
        return config
    except Exception as e:
        print(f'[config] load error: {e}', flush=True)
        return _config_cache or {}

def _get_config(key, default=''):
    cfg = _load_config()
    return cfg.get(key, os.environ.get(key, default))

def _send_email(to_email, to_name, subject, html_body):
    """Send HTML email. Prefers MailerSend API, falls back to Gmail SMTP if no API key."""
    ms_key = _get_config('MAILERSEND_API_KEY', '')
    if ms_key:
        return _send_via_mailersend(ms_key, to_email, to_name, subject, html_body)
    return _send_via_smtp(to_email, to_name, subject, html_body)


def _send_via_mailersend(api_key, to_email, to_name, subject, html_body):
    """Send email via MailerSend REST API (works from any cloud server)."""
    import urllib.request as _ur
    payload = json.dumps({
        "from": {"email": "reimbursement@fincom.asia", "name": "MallPlus Reimbursements"},
        "to": [{"email": to_email, "name": to_name}],
        "reply_to": {"email": "finance@fincom.asia", "name": "MallPlus Finance"},
        "subject": subject,
        "html": html_body
    }).encode('utf-8')
    try:
        req = _ur.Request(
            "https://api.mailersend.com/v1/email",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "MallPlus-Reimbursement/1.0",
                "X-Requested-With": "XMLHttpRequest"
            }
        )
        resp = _ur.urlopen(req, timeout=15)
        if resp.status in (200, 201, 202):
            print(f'[email] ✓ MailerSend sent "{subject}" to {to_email}', flush=True)
            return True
        print(f'[email] ✗ MailerSend returned {resp.status} for {to_email}', flush=True)
        return False
    except Exception as e:
        print(f'[email] ✗ MailerSend failed to {to_email}: {e}', flush=True)
        return False


def _send_via_smtp(to_email, to_name, subject, html_body):
    """Send HTML email via Gmail SMTP. Reads app password from sheet Config tab."""
    password = _get_config('SMTP_APP_PASSWORD', '')
    if not password:
        print('[email] SMTP_APP_PASSWORD not set in Config sheet — skipping email to', to_email, flush=True)
        return False

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = 'MallPlus Reimbursements <reimbursement@fincom.asia>'
    msg['To']      = f'{to_name} <{to_email}>'
    msg['Reply-To']= 'finance@fincom.asia'
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, password)
            server.send_message(msg)
        print(f'[email] ✓ Sent "{subject}" to {to_email}', flush=True)
        return True
    except Exception as e:
        print(f'[email] ✗ Failed to send to {to_email}: {e}', flush=True)
        return False


def _email_base_style():
    return '''
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
           color: #1A1035; background: #f0f9f8; margin: 0; padding: 0; }
    .wrap { max-width: 540px; margin: 32px auto; background: #fff;
            border-radius: 16px; overflow: hidden;
            box-shadow: 0 2px 24px rgba(0,0,0,.06); }
    .banner { background: linear-gradient(135deg, #3724ED, #1A9FD8, #00AFA0);
              padding: 32px 28px; text-align: center; }
    .banner .icon { font-size: 36px; margin-bottom: 4px; }
    .banner h1 { color: #fff; font-size: 18px; margin: 0; font-weight: 700; }
    .body { padding: 28px 28px 20px; }
    .amount { font-size: 26px; font-weight: 800; color: #00AFA0; margin: 4px 0 16px; }
    .info-table { width: 100%; border-collapse: collapse; margin: 16px 0 20px; }
    .info-table td { padding: 10px 14px; border-bottom: 1px solid #f0f0f0; font-size: 14px; }
    .info-table td.label { color: #6B7280; width: 120px; font-size: 13px; }
    .info-table td.value { font-weight: 600; }
    .divider { border-top: 1px solid #f0f0f0; margin: 8px 0; }
    .button-row { text-align: center; padding: 8px 0 20px; }
    .button { display: inline-block; padding: 13px 36px; background: #00AFA0;
              color: #fff !important; text-decoration: none; border-radius: 999px;
              font-weight: 700; font-size: 14px; }
    .button:hover { background: #007A73; }
    .footer { padding: 0 28px 24px; color: #A0AEC0; font-size: 12px; line-height: 1.6; }
    .status-badge { display: inline-block; padding: 4px 14px; border-radius: 999px;
                    font-weight: 700; font-size: 12px; }
    .status-approved { background: #E0F5F3; color: #007A73; }
    .status-rejected { background: #FEE2E2; color: #B91C1C; }
    .status-pending  { background: #FFF8E1; color: #C4880A; }
'''


def _html_email(chunks):
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<style>{_email_base_style()}</style></head>
<body><div class="wrap">{chr(10).join(chunks)}</div></body></html>'''


# ═══════════════════════════════════════════════════════════════════════
# EMAIL NOTIFICATIONS — Config-Driven
# ═══════════════════════════════════════════════════════════════════════

def _notify_on_submit(reimb_id, emp_name, emp_email, amt, category, purpose,
                       resolved_chain, first_level):
    """Notify approvers on new submission.
    Level 1 approvers get Action Needed; higher levels get FYI.
    All FYI recipients get notified; non-current level is CC only."""
    amt_str = f'\u20b1{float(amt):,.2f}'
    portal_link = f'{PORTAL_URL}/reimbursements'
    first_emails = resolved_chain.get(f'level_{first_level}', [])

    # ── Action Needed email to level 1 approvers ──
    approver_names = ', '.join(_lookup_name(e) for e in first_emails)
    chunks = [
        '<div class="banner"><div class="icon">\U0001f4cb</div>'
        f'<h1>New Reimbursement Request</h1></div>',
        f'<div class="body"><p>Hello <strong>{approver_names}</strong>,</p>',
        f'<p><strong>{emp_name}</strong> submitted a reimbursement for your approval.</p>',
        f'<div class="amount">{amt_str}</div>',
        '<table class="info-table">'
        f'<tr><td class="label">Reimbursement ID</td><td class="value">{reimb_id}</td></tr>'
        f'<tr><td class="label">Employee</td><td class="value">{emp_name}</td></tr>'
        f'<tr><td class="label">Category</td><td class="value">{category}</td></tr>'
        f'<tr><td class="label">Purpose</td><td class="value">{purpose}</td></tr>'
        '</table>',
        '<div class="button-row">'
        f'<a class="button" href="{portal_link}">Review in Portal \u2192</a></div>',
        '<div class="divider"></div>',
        '<div class="footer">This is an automated notification from the MallPlus Reimbursement Portal.'
        f'<br>Questions? Reply to finance@fincom.asia</div>',
    ]
    for email in first_emails:
        _send_email(email, _lookup_name(email),
                    f'Action Needed: {emp_name} submitted \u20b1{float(amt):,.0f} reimbursement',
                    _html_email(chunks))

    # ── FYI email to higher-level approvers ──
    fyi_emails = set()
    for lvl in range(first_level + 1, 4):
        for email in resolved_chain.get(f'level_{lvl}', []):
            fyi_emails.add(email)
    for email in fyi_emails:
        fyi_chunks = [
            '<div class="banner"><div class="icon">ℹ️</div>'
            f'<h1>New Reimbursement — FYI</h1></div>',
            f'<div class="body"><p>Hello <strong>{_lookup_name(email)}</strong>,</p>',
            f'<p><strong>{emp_name}</strong> submitted a reimbursement. This is for your awareness — '
            f'it is currently with the level {first_level} approver(s).</p>',
            f'<div class="amount">{amt_str}</div>',
            '<table class="info-table">'
            f'<tr><td class="label">Reimbursement ID</td><td class="value">{reimb_id}</td></tr>'
            f'<tr><td class="label">Employee</td><td class="value">{emp_name}</td></tr>'
            f'<tr><td class="label">Category</td><td class="value">{category}</td></tr>'
            f'<tr><td class="label">Purpose</td><td class="value">{purpose}</td></tr>'
            f'<tr><td class="label">Status</td><td class="value"><span class="status-badge status-pending">Pending</span></td></tr>'
            '</table>',
            '<div class="divider"></div>',
            '<div class="footer">This is an automated notification from the MallPlus Reimbursement Portal.'
            f'<br>Questions? Reply to finance@fincom.asia</div>',
        ]
        _send_email(email, _lookup_name(email),
                    f'FYI: {emp_name} submitted \u20b1{float(amt):,.0f} reimbursement ({reimb_id})',
                    _html_email(fyi_chunks))


def _notify_level_advance(reimb_id, emp_name, emp_email, amt, category,
                           approver_name, next_level, next_emails, total_levels):
    """Notify next-level approvers that a request has advanced to them."""
    amt_str = f'\u20b1{float(amt):,.2f}'
    portal_link = f'{PORTAL_URL}/reimbursements'
    next_names = ', '.join(_lookup_name(e) for e in next_emails)
    is_final = (next_level == total_levels)

    if is_final:
        heading = 'Escalated for Final Approval'
        icon = '\u2b06\ufe0f'
        subject = f'Escalated: {emp_name} \u20b1{float(amt):,.0f} — final approval needed'
        extra = f'<p><strong>{approver_name}</strong> approved this and escalated it for final sign-off.</p>'
    else:
        heading = 'Approval Needed — Next Level'
        icon = '\u27a1\ufe0f'
        subject = f'Action Needed: {emp_name} \u20b1{float(amt):,.0f} — next approval ({reimb_id})'
        extra = f'<p><strong>{approver_name}</strong> approved and forwarded to you for next-level review.</p>'

    chunks = [
        f'<div class="banner"><div class="icon">{icon}</div>'
        f'<h1>{heading}</h1></div>',
        f'<div class="body"><p>Hello <strong>{next_names}</strong>,</p>',
        extra,
        f'<div class="amount">{amt_str}</div>',
        '<table class="info-table">'
        f'<tr><td class="label">Reimbursement ID</td><td class="value">{reimb_id}</td></tr>'
        f'<tr><td class="label">Employee</td><td class="value">{emp_name}</td></tr>'
        f'<tr><td class="label">Category</td><td class="value">{category}</td></tr>'
        f'<tr><td class="label">Approved by</td><td class="value">{approver_name}</td></tr>'
        '</table>',
        '<div class="button-row">'
        f'<a class="button" href="{portal_link}">Review in Portal \u2192</a></div>',
        '<div class="divider"></div>',
        '<div class="footer">This is an automated notification from the MallPlus Reimbursement Portal.'
        f'<br>Questions? Reply to finance@fincom.asia</div>',
    ]
    for email in next_emails:
        _send_email(email, _lookup_name(email), subject, _html_email(chunks))


def _notify_decision(reimb_id, emp_name, amt, category, new_status, approver_name, reason, submitter_email):
    """Notify submitter of approval or rejection."""
    amt_str = f'\u20b1{float(amt):,.2f}'
    portal_link = f'{PORTAL_URL}/reimbursements'

    if 'Approved' in new_status or new_status == 'Approved':
        badge_html = '<span class="status-badge status-approved">\u2713 Approved</span>'
        emoji = '\u2705'
        heading = 'Reimbursement Approved'
        action_text = f'<strong>{approver_name}</strong> approved your reimbursement.'
        next_steps = '<p>Finance will process your payment in the next reimbursement cycle.</p>'
        subject = f'\u2713 Approved: Your \u20b1{float(amt):,.0f} reimbursement ({reimb_id})'
    else:
        badge_html = '<span class="status-badge status-rejected">\u2717 Rejected</span>'
        emoji = '\u274c'
        heading = 'Reimbursement Rejected'
        action_text = f'<strong>{approver_name}</strong> rejected your reimbursement.'
        reason_note = f'<p><strong>Reason:</strong> {reason}</p>' if reason else ''
        next_steps = f'{reason_note}<p>Please review and resubmit if needed.</p>'
        subject = f'\u2717 Rejected: Your \u20b1{float(amt):,.0f} reimbursement ({reimb_id})'

    chunks = [
        f'<div class="banner"><div class="icon">{emoji}</div>'
        f'<h1>{heading}</h1></div>',
        f'<div class="body"><p>Hello <strong>{emp_name}</strong>,</p>',
        f'<p>{action_text}</p>',
        f'<div style="margin:8px 0;">{badge_html}</div>',
        f'<div class="amount">{amt_str}</div>',
        '<table class="info-table">'
        f'<tr><td class="label">Reimbursement ID</td><td class="value">{reimb_id}</td></tr>'
        f'<tr><td class="label">Category</td><td class="value">{category}</td></tr>'
        f'<tr><td class="label">Reviewed by</td><td class="value">{approver_name}</td></tr>'
        '</table>',
        next_steps,
        '<div class="button-row">'
        f'<a class="button" href="{portal_link}">View in Portal \u2192</a></div>',
        '<div class="divider"></div>',
        '<div class="footer">This is an automated notification from the MallPlus Reimbursement Portal.'
        f'<br>Questions? Reply to finance@fincom.asia</div>',
    ]
    _send_email(submitter_email, emp_name, subject, _html_email(chunks))


def _notify_finance_reject_approvers(reimb_id, emp_name, amt, category, reason, finance_name, approver_emails):
    """Notify the approvers of record that Finance rejected an already-approved request.

    Informational — the requester was already notified and may resubmit, which starts a
    fresh approval cycle (approvers will be re-engaged then)."""
    amt_str = f'\u20b1{float(amt):,.2f}'
    portal_link = f'{PORTAL_URL}/reimbursements'
    names = ', '.join(_lookup_name(e) for e in approver_emails)
    subject = f'\u26a0\ufe0f Finance rejected {reimb_id}: {emp_name} {amt_str}'
    chunks = [
        '<div class="banner"><div class="icon">\u274c</div>'
        '<h1>Reimbursement Rejected by Finance</h1></div>',
        f'<div class="body"><p>Hello <strong>{names}</strong>,</p>',
        f'<p><strong>{finance_name}</strong> rejected this request during the finance review — '
        'it passed approval but failed finance checks (e.g. unacceptable receipt).</p>',
        f'<div class="amount">{amt_str}</div>',
        '<table class="info-table">'
        f'<tr><td class="label">Reimbursement ID</td><td class="value">{reimb_id}</td></tr>'
        f'<tr><td class="label">Employee</td><td class="value">{emp_name}</td></tr>'
        f'<tr><td class="label">Category</td><td class="value">{category}</td></tr>'
        f'<tr><td class="label">Reason</td><td class="value">{reason}</td></tr>'
        '</table>',
        '<p>The requester has been notified and can fix the issue and resubmit, '
        'which will start a fresh approval cycle.</p>',
        '<div class="button-row">'
        f'<a class="button" href="{portal_link}">Review in Portal \u2192</a></div>',
        '<div class="divider"></div>',
        '<div class="footer">This is an automated notification from the MallPlus Reimbursement Portal.'
        '<br>Questions? Reply to finance@fincom.asia</div>',
    ]
    for email in approver_emails:
        _send_email(email, _lookup_name(email), subject, _html_email(chunks))

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

# ── Audit Log (edit / cancel / resubmit history) ───────────────────────
_AUDIT_SHEET = None

def _get_audit_sheet():
    """Return the 'Audit Log' worksheet in the reimbursement spreadsheet, creating it if missing."""
    global _AUDIT_SHEET
    if _AUDIT_SHEET is None:
        sh = _get_gs().open_by_key(SHEET_ID)
        try:
            _AUDIT_SHEET = sh.worksheet('Audit Log')
        except:
            _AUDIT_SHEET = sh.add_worksheet(title='Audit Log', rows=200, cols=9)
            _AUDIT_SHEET.append_row(['Timestamp', 'Action', 'Reimbursement ID', 'Actor Name', 'Actor Email',
                                     'Field', 'Old Value', 'New Value', 'Note'])
    return _AUDIT_SHEET

def _audit_log(action, reimb_id, actor_name, actor_email, field, old_val, new_val, note=''):
    """Append one row to the Audit Log tab. Never raises — auditing must not break the request flow."""
    try:
        _get_audit_sheet().append_row([
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            action, reimb_id, actor_name, actor_email,
            field, str(old_val), str(new_val), note
        ], value_input_option='USER_ENTERED')
    except Exception as e:
        print(f'[audit] log error: {e}', flush=True)

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

# ── Session management ──────────────────────────────────────────────────
# When AUTH_SECRET is set: signed stateless tokens (HMAC-SHA256 over base64url JSON).
# Token format: base64url(payload_json) + "." + hmac_sha256_hex(AUTH_SECRET, payload_b64)
# When AUTH_SECRET is empty: legacy in-memory dict only (no behaviour change).
_sessions = {}  # {token: {email, name, department, role, expires}}
_reset_tokens = {}  # {token: {email, expires}}


def _clean_sessions():
    now = time.time()
    expired = [t for t, s in _sessions.items() if s.get('expires', 0) < now]
    for t in expired:
        del _sessions[t]


def _sign_session_payload(payload_b64):
    """HMAC-SHA256 hex of payload_b64 using AUTH_SECRET."""
    return hmac.new(AUTH_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()


def _create_session(email, name, dept, role):
    """Create a session token. When AUTH_SECRET is set, returns a signed stateless
    token AND stores in _sessions dict for backward compat. Otherwise dict-only."""
    _clean_sessions()
    for t, s in list(_sessions.items()):
        if s.get('email') == email:
            del _sessions[t]

    if AUTH_SECRET:
        payload = {
            'email': email, 'name': name,
            'department': dept, 'role': role,
            'exp': int(time.time()) + 86400,
        }
        payload_b64 = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(',', ':')).encode()
        ).decode().rstrip('=')
        sig = _sign_session_payload(payload_b64)
        token = payload_b64 + '.' + sig
        # Also store in dict (backward compat during rollout)
        _sessions[token] = {
            'email': email, 'name': name,
            'department': dept, 'role': role,
            'expires': payload['exp'],
        }
        return token
    else:
        token = secrets.token_hex(32)
        _sessions[token] = {
            'email': email, 'name': name,
            'department': dept, 'role': role,
            'expires': time.time() + 86400,
        }
        return token


def _validate_session(token):
    """Validate a session token. Tries signed verification first (if AUTH_SECRET set),
    then falls back to in-memory dict. Returns session dict or None."""
    if not token:
        return None

    if AUTH_SECRET and '.' in token:
        try:
            payload_b64, sig = token.rsplit('.', 1)
            expected = _sign_session_payload(payload_b64)
            if hmac.compare_digest(sig, expected):
                raw = base64.urlsafe_b64decode(
                    payload_b64 + '=' * (-len(payload_b64) % 4)
                )
                payload = json.loads(raw)
                if payload.get('exp', 0) > time.time():
                    return payload   # valid signed session
                return None          # expired
            # Sig mismatch -> could be legacy random hex with a dot
        except Exception:
            pass  # malformed -> fall through to dict

    # Legacy / no AUTH_SECRET: in-memory dict
    _clean_sessions()
    return _sessions.get(token)

# ── Drive upload helper ─────────────────────────────────────────────────
def _upload_to_drive(file_data, filename, mime_type):
    """Upload file to Drive folder (shared drive), return public URL."""
    drive_service = _get_drive()
    media = MediaIoBaseUpload(io.BytesIO(file_data), mimetype=mime_type, resumable=True)
    file_meta = {
        'name': filename,
        'parents': [DRIVE_FOLDER_ID]
    }
    f = drive_service.files().create(body=file_meta, media_body=media, fields='id', supportsAllDrives=True).execute()
    file_id = f['id']
    # Make publicly readable
    drive_service.permissions().create(
        fileId=file_id,
        body={'type': 'anyone', 'role': 'reader'},
        supportsAllDrives=True
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
        elif path == '/reimbursements/api/requests/update':
            return _api_update_request(body_raw, headers)
        elif path == '/reimbursements/api/requests/cancel':
            return _api_cancel_request(body_raw, headers)
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
        elif path == '/reimbursements/api/finance-reject':
            return _api_finance_reject(body_raw, headers)
        elif path == '/reimbursements/api/stats':
            return _api_stats(qs, headers)
        elif path == '/reimbursements/api/forgot-pin':
            return _api_forgot_pin(body_raw)
        elif path == '/reimbursements/api/reset-pin':
            return _api_reset_pin(body_raw)
        elif path == '/reimbursements/api/debug':
            return _api_debug()
        elif path in ('/api/portal-tools', '/reimbursements/api/portal-tools'):
            return _api_portal_tools(headers)
        else:
            return 404, 'application/json', json.dumps({'error': 'Not found'}).encode(), False
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 500, 'application/json', json.dumps({'error': str(e)}).encode(), True

def _api_debug():
    raw = os.environ.get("GOOGLE_CREDS_JSON", "")
    raw_len = len(raw)
    raw_head = raw[:80] if raw else ""
    info = {
        "env_var_set": bool(raw),
        "env_var_len": raw_len,
        "env_var_head": raw_head,
        "has_service_account": "service_account" in raw.lower(),
        "starts_with_brace": raw.startswith("{") if raw else False,
    }
    if raw:
        try:
            d = json.loads(raw)
            info["json_valid"] = True
            info["project_id"] = d.get("project_id", "?")
            info["has_private_key"] = "private_key" in d
        except Exception as e:
            info["json_valid"] = False
            info["json_error"] = str(e)[:200]
    return 200, 'application/json', json.dumps(info).encode(), True


def _api_forgot_pin(body_raw):
    """Send a PIN reset email to the employee. Token expires in 30 min."""
    data = json.loads(body_raw or '{}')
    email = data.get('email', '').strip().lower()
    if not email:
        return 400, 'application/json', json.dumps({'error': 'Email is required'}).encode(), True

    emp = _find_employee(email)
    if not emp:
        # Don't reveal whether email exists — just say "if found, email sent"
        return 200, 'application/json', json.dumps({'success': True, 'message': 'If your email is registered, a reset link has been sent.'}).encode(), True

    # Generate one-time reset token
    _clean_reset_tokens()
    reset_token = secrets.token_hex(32)
    _reset_tokens[reset_token] = {
        'email': email,
        'expires': time.time() + 1800  # 30 min
    }

    # Send reset email
    reset_link = f"{PORTAL_URL}/reimbursements?reset={reset_token}"
    emp_name = emp.get('name', email)

    chunks = [
        '<div class="banner"><div class="icon">🔑</div>'
        f'<h1>Reset Your PIN</h1></div>',
        f'<div class="body"><p>Hello <strong>{emp_name}</strong>,</p>',
        '<p>We received a request to reset your PIN for the MallPlus Reimbursement Portal.</p>',
        '<div class="button-row">'
        f'<a class="button" href="{reset_link}">Set New PIN →</a></div>',
        '<p style="color:#6B7280;font-size:13px;margin-top:16px;">This link expires in 30 minutes.'
        ' If you didn\'t request this, you can ignore this email.</p>',
        '<div class="divider"></div>',
        '<div class="footer">This is an automated notification from the MallPlus Reimbursement Portal.'
        f'<br>Questions? Reply to finance@fincom.asia</div>',
    ]
    _send_email(email, emp_name, 'Reset your MallPlus Reimbursement PIN', _html_email(chunks))

    return 200, 'application/json', json.dumps({'success': True, 'message': 'If your email is registered, a reset link has been sent.'}).encode(), True


def _clean_reset_tokens():
    now = time.time()
    expired = [t for t, s in _reset_tokens.items() if s.get('expires', 0) < now]
    for t in expired:
        del _reset_tokens[t]


def _api_reset_pin(body_raw):
    """Set a new PIN using a valid reset token."""
    data = json.loads(body_raw or '{}')
    reset_token = data.get('token', '').strip()
    new_pin = data.get('pin', '').strip()

    if not reset_token or not new_pin:
        return 400, 'application/json', json.dumps({'error': 'Token and new PIN are required'}).encode(), True
    if len(new_pin) < 4:
        return 400, 'application/json', json.dumps({'error': 'PIN must be at least 4 characters'}).encode(), True

    _clean_reset_tokens()
    entry = _reset_tokens.get(reset_token)
    if not entry:
        return 400, 'application/json', json.dumps({'error': 'Reset link has expired or is invalid. Please request a new one.'}).encode(), True

    email = entry['email']

    # Update PIN in Employees sheet
    ws = _get_emp_sheet()
    rows = ws.get_all_values()
    for i, row in enumerate(rows):
        if len(row) > 1 and row[1].strip().lower() == email.strip().lower():
            ws.update_cell(i + 1, 4, new_pin)
            # Consume the token
            del _reset_tokens[reset_token]
            return 200, 'application/json', json.dumps({'success': True, 'message': 'PIN has been reset. You can now log in with your new PIN.'}).encode(), True

    return 404, 'application/json', json.dumps({'error': 'Employee record not found'}).encode(), True


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

    for row in rows[1:]:
        existing_vendor = row[17].strip().lower() if len(row) > 17 else ''
        existing_date = row[5].strip() if len(row) > 5 else ''
        existing_amt = row[6].strip() if len(row) > 6 else ''
        if existing_vendor and existing_vendor == vendor.lower():
            if existing_date == purchase_date and _amounts_equal(existing_amt, amt):
                dupes.append({
                    'type': 'matching_fields',
                    'reimbursement_id': row[0],
                    'message': f"Same vendor, date, and amount already submitted as {row[0]} by {row[2]}"
                })

    # ── Config-driven approval chain ──
    chain = _find_approval_chain(department, amt)
    if chain is None:
        # No config match — fall back to Patt as sole approver
        approver_email = DEFAULT_FINAL_APPROVER_EMAIL
        approver_name = DEFAULT_FINAL_APPROVER_NAME
        status = 'Pending'
    else:
        resolved = _resolve_chain(chain, employee_email)
        first_level, first_emails = _first_active_level(resolved)
        if first_level is None:
            # All levels were self (unlikely but handle gracefully)
            approver_email = DEFAULT_FINAL_APPROVER_EMAIL
            approver_name = DEFAULT_FINAL_APPROVER_NAME
            status = 'Pending'
        else:
            approver_email = ', '.join(first_emails)
            approver_name = ', '.join(_lookup_name(e) for e in first_emails)
            status = _level_status(first_level)

    # Generate ID and timestamp
    reimb_id = _gen_reimb_id()
    timestamp = datetime.now().isoformat()

    # Send notifications if we have a resolved chain
    if chain is not None:
        resolved = _resolve_chain(chain, employee_email)
        first_level, first_emails = _first_active_level(resolved)
        if first_level is not None:
            try:
                _notify_on_submit(reimb_id, employee_name, employee_email, f'{amt:.2f}',
                                  category, purpose, resolved, first_level)
            except Exception as e:
                print(f'[notify] submit notification error: {e}', flush=True)

    dupe_flag = '; '.join(d['message'] for d in dupes) if dupes else ''
    _ensure_dupe_header(ws)
    ws.append_row([
        reimb_id, timestamp, employee_name, employee_email,
        department, purchase_date, f'{amt:.2f}', category, purpose,
        receipt_url, status, approver_email, approver_name,
        '', '', '', notes, vendor, invoice_number, receipt_hash, vat_status,
        dupe_flag
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

# ── Requester Edit / Resubmit / Cancel ─────────────────────────────────
# Editable fields: (0-based column index, JSON field name, human label)
EDITABLE_FIELDS = [
    (5,  'purchase_date',  'Purchase Date'),
    (6,  'amount',         'Amount'),
    (7,  'category',       'Category'),
    (8,  'purpose',        'Purpose'),
    (9,  'receipt_url',    'Receipt'),
    (16, 'notes',          'Notes'),
    (17, 'vendor',         'Vendor'),
    (18, 'invoice_number', 'Invoice / OR Number'),
    (20, 'vat_status',     'VAT Status'),
]

def _validate_and_normalize(data):
    """Shared validation for submit/edit. Returns (amount, error_response_or_None)."""
    purchase_date = data.get('purchase_date', '').strip()
    amount = data.get('amount', '').strip()
    category = data.get('category', '').strip()
    purpose = data.get('purpose', '').strip()
    vendor = data.get('vendor', '').strip()
    vat_status = data.get('vat_status', '').strip()
    if not all([purchase_date, amount, category, purpose, vendor, vat_status]):
        err = {'error': 'Purchase date, amount, category, purpose, vendor, and VAT Status are required'}
        return None, (400, 'application/json', json.dumps(err).encode(), True)
    try:
        amt = float(amount.replace(',', '').replace('₱', '').strip())
        if amt <= 0:
            raise ValueError
    except:
        return None, (400, 'application/json', json.dumps({'error': 'Invalid amount'}).encode(), True)
    return amt, None

def _find_row_by_id(ws, reimb_id):
    """Return (row_idx, row) for the given reimbursement id, or (None, None)."""
    rows = ws.get_all_values()
    for i, row in enumerate(rows):
        if row and row[0].strip() == reimb_id:
            return i + 1, row
    return None, None

def _amounts_equal(sheet_amt, amt):
    """Compare a sheet-stored amount string ('6000' or '6,000.00') to a float amount numerically.
    Sheets stores amounts as numbers, so '6000' must equal 6000.00 — string compare never matches."""
    try:
        return abs(float(str(sheet_amt).replace(',', '').replace('₱', '').strip()) - amt) < 0.001
    except:
        return False


def _ensure_dupe_header(ws):
    """Ensure the Reimbursements sheet has a 'Duplicate Flag' header in column 22 (idempotent)."""
    try:
        rows = ws.get_all_values()
        if not rows:
            ws.update_cell(1, 22, 'Duplicate Flag')
            return
        hdr = rows[0]
        if len(hdr) < 22 or not (hdr[21] or '').strip():
            ws.update_cell(1, 22, 'Duplicate Flag')
    except Exception as e:
        print(f'[dupe] header ensure error: {e}', flush=True)


def _check_duplicates(ws, vendor, purchase_date, amt, receipt_hash, exclude_id=None):
    """Mirror submit's duplicate checks, optionally excluding one request id (its own row on edit)."""
    dupes = []
    rows = ws.get_all_values()
    for row in rows[1:]:
        if exclude_id and row and row[0].strip() == exclude_id:
            continue
        existing_hash = row[19].strip() if len(row) > 19 else ''
        if receipt_hash and existing_hash and existing_hash == receipt_hash:
            dupes.append({
                'type': 'identical_receipt',
                'reimbursement_id': row[0],
                'message': f"This exact receipt was already submitted as {row[0]} on {row[5]} by {row[2]}"
            })
            break
    for row in rows[1:]:
        if exclude_id and row and row[0].strip() == exclude_id:
            continue
        existing_vendor = row[17].strip().lower() if len(row) > 17 else ''
        existing_date = row[5].strip() if len(row) > 5 else ''
        existing_amt = row[6].strip() if len(row) > 6 else ''
        if existing_vendor and existing_vendor == vendor.lower():
            if existing_date == purchase_date and _amounts_equal(existing_amt, amt):
                dupes.append({
                    'type': 'matching_fields',
                    'reimbursement_id': row[0],
                    'message': f"Same vendor, date, and amount already submitted as {row[0]} by {row[2]}"
                })
    return dupes

def _api_update_request(body_raw, headers):
    """Requester edits their own submission.

    Allowed statuses:
      - 'Pending'                    → in-place edit, stays Pending; approval chain re-resolved if amount changed
      - 'Rejected' / 'Rejected Final' → edit + RESUBMIT: fresh approval cycle (status → Pending,
                                        chain re-resolved, decision columns cleared, approvers notified)
    Every change is recorded in the Audit Log tab.
    """
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    data = json.loads(body_raw or '{}')
    reimb_id = data.get('reimbursement_id', '').strip()
    if not reimb_id:
        return 400, 'application/json', json.dumps({'error': 'Reimbursement ID required'}).encode(), True

    amt, err = _validate_and_normalize(data)
    if err:
        return err

    ws = _get_reimb_sheet()
    row_idx, row = _find_row_by_id(ws, reimb_id)
    if row_idx is None:
        return 404, 'application/json', json.dumps({'error': 'Reimbursement not found'}).encode(), True

    # Owner check
    owner_email = row[3].strip().lower() if len(row) > 3 else ''
    if owner_email != session['email'].strip().lower():
        return 403, 'application/json', json.dumps({'error': 'You can only edit your own requests'}).encode(), True

    # Status gate (checked at write time — blocks edits on requests approved between page load and save)
    row_status = row[10].strip() if len(row) > 10 else ''
    is_pending = row_status == 'Pending'
    is_rejected = row_status in ('Rejected', 'Rejected Final', 'Rejected by Finance')
    if not is_pending and not is_rejected:
        return 409, 'application/json', json.dumps({
            'error': f'Request is in "{row_status}" status — it can only be edited while Pending, or resubmitted after Rejection'
        }).encode(), True

    receipt_url = data.get('receipt_url', '').strip()
    receipt_hash = data.get('receipt_hash', '').strip()

    # Duplicate checks (excluding this request's own row)
    dupes = _check_duplicates(ws, data.get('vendor', '').strip(),
                              data.get('purchase_date', '').strip(), amt,
                              receipt_hash, exclude_id=reimb_id)

    # Build change list (1-based column, label, old value, new value)
    changes = []
    for col0, key, label in EDITABLE_FIELDS:
        old_val = row[col0].strip() if len(row) > col0 else ''
        if key == 'amount':
            new_val = f'{amt:.2f}'
            # Sheet stores amounts as numbers (e.g. "6000") — compare numerically
            try:
                old_num = float(old_val.replace(',', '').replace('₱', '').strip())
                is_change = abs(old_num - amt) > 0.001
            except:
                is_change = old_val != new_val
        elif key == 'receipt_url':
            new_val = receipt_url
            is_change = old_val != new_val
        else:
            new_val = data.get(key, '').strip()
            is_change = old_val != new_val
        if is_change:
            changes.append((col0 + 1, label, old_val, new_val))

    if not changes and is_pending:
        return 200, 'application/json', json.dumps({
            'success': True, 'message': 'No changes were made', 'changed': []
        }).encode(), True

    # Re-resolve approval chain with the (possibly new) amount
    row_department = row[4].strip() if len(row) > 4 else ''
    submitter_email = row[3].strip() if len(row) > 3 else ''
    chain = _find_approval_chain(row_department, amt)
    resolved = _resolve_chain(chain, submitter_email) if chain else {}
    first_level, first_emails = _first_active_level(resolved)
    if chain is None or first_level is None:
        new_status = 'Pending'
        approver_email = DEFAULT_FINAL_APPROVER_EMAIL
        approver_name = DEFAULT_FINAL_APPROVER_NAME
    else:
        new_status = _level_status(first_level)
        approver_email = ', '.join(first_emails)
        approver_name = ', '.join(_lookup_name(e) for e in first_emails)

    # Apply field changes
    for col, label, old_val, new_val in changes:
        ws.update_cell(row_idx, col, new_val)
    if receipt_hash and receipt_url and (row[19].strip() if len(row) > 19 else '') != receipt_hash:
        ws.update_cell(row_idx, 20, receipt_hash)
        changes.append((20, 'Receipt Hash', row[19].strip() if len(row) > 19 else '', receipt_hash))

    if is_rejected:
        # Resubmit: fresh approval cycle, same ID
        old_reject_reason = row[15].strip() if len(row) > 15 else ''
        ws.update_cell(row_idx, 11, new_status)
        ws.update_cell(row_idx, 12, approver_email)
        ws.update_cell(row_idx, 13, approver_name)
        ws.update_cell(row_idx, 14, '')   # clear Approval Date
        ws.update_cell(row_idx, 15, '')   # clear Rejection Reason
        _audit_log('RESUBMIT', reimb_id, session['name'], session['email'],
                   'status', row_status, new_status,
                   f'Resubmitted after rejection. Prior reason: {old_reject_reason or "—"}')
        if first_level is not None:
            try:
                _notify_on_submit(reimb_id, session['name'], session['email'], f'{amt:.2f}',
                                  data.get('category', '').strip(), data.get('purpose', '').strip(),
                                  resolved, first_level)
            except Exception as e:
                print(f'[notify] resubmit error: {e}', flush=True)
    else:
        # Pending edit — refresh chain columns only if the amount changed
        if any(c[0] == 7 for c in changes):
            ws.update_cell(row_idx, 11, new_status)
            ws.update_cell(row_idx, 12, approver_email)
            ws.update_cell(row_idx, 13, approver_name)

    # Audit each changed field
    for col, label, old_val, new_val in changes:
        _audit_log('EDIT', reimb_id, session['name'], session['email'], label, old_val, new_val)

    # Refresh duplicate flag (col 22) — set when dupes found, clear when resolved by the edit
    dupe_flag = '; '.join(d['message'] for d in dupes) if dupes else ''
    _ensure_dupe_header(ws)
    ws.update_cell(row_idx, 22, dupe_flag)

    result = {
        'success': True,
        'new_status': new_status if is_rejected else row_status,
        'changed': [c[1] for c in changes],
    }
    if dupes:
        result['duplicate_warning'] = True
        result['duplicates'] = dupes
    return 200, 'application/json', json.dumps(result).encode(), True


def _api_cancel_request(body_raw, headers):
    """Requester cancels their own request. Only allowed while status = Pending (no approval yet).

    Cancellation is terminal — the row keeps its ID with status 'Cancelled', the reason is stored
    in the Rejection Reason column (prefixed) for the finance team, and the event is audit-logged.
    Approvers are intentionally NOT notified (per Shaun, Aug 17 2026).
    """
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    data = json.loads(body_raw or '{}')
    reimb_id = data.get('reimbursement_id', '').strip()
    reason = data.get('reason', '').strip()
    if not reimb_id:
        return 400, 'application/json', json.dumps({'error': 'Reimbursement ID required'}).encode(), True
    if not reason:
        return 400, 'application/json', json.dumps({'error': 'Cancellation reason is required'}).encode(), True

    ws = _get_reimb_sheet()
    row_idx, row = _find_row_by_id(ws, reimb_id)
    if row_idx is None:
        return 404, 'application/json', json.dumps({'error': 'Reimbursement not found'}).encode(), True

    owner_email = row[3].strip().lower() if len(row) > 3 else ''
    if owner_email != session['email'].strip().lower():
        return 403, 'application/json', json.dumps({'error': 'You can only cancel your own requests'}).encode(), True

    row_status = row[10].strip() if len(row) > 10 else ''
    if row_status != 'Pending':
        return 409, 'application/json', json.dumps({
            'error': f'Request is in "{row_status}" status — it can only be cancelled before the first approval'
        }).encode(), True

    ws.update_cell(row_idx, 11, 'Cancelled')
    ws.update_cell(row_idx, 15, f'Cancelled: {reason}')
    _audit_log('CANCEL', reimb_id, session['name'], session['email'],
               'status', 'Pending', 'Cancelled', reason)

    return 200, 'application/json', json.dumps({'success': True, 'status': 'Cancelled'}).encode(), True


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
        current_level = _status_level(row_status)
        if current_level is None:
            continue  # not a pending status

        # Get the approval chain for this row
        row_dept = item.get('department', '')
        try:
            row_amt = float(row[6].replace(',', '').strip()) if len(row) > 6 and row[6].strip() else 0
        except:
            row_amt = 0

        chain = _find_approval_chain(row_dept, row_amt)
        if chain is None:
            # No config — only Patt (default final) sees it
            if approver_email == DEFAULT_FINAL_APPROVER_EMAIL and current_level <= 3:
                results.append(item)
        else:
            submitter_email = item.get('employee_email', '')
            resolved = _resolve_chain(chain, submitter_email)
            level_emails = resolved.get(f'level_{current_level}', [])

            if approver_email in level_emails:
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

    ws = _get_reimb_sheet()
    rows = ws.get_all_values()
    approver_email_session = session['email'].strip().lower()

    for i, row in enumerate(rows):
        if not row or row[0].strip() != reimb_id:
            continue
        row_idx = i + 1
        row_status = row[10].strip() if len(row) > 10 else ''
        current_level = _status_level(row_status)

        if current_level is None:
            return 403, 'application/json', json.dumps({
                'error': f'Request is in "{row_status}" status — only pending statuses can be approved'
            }).encode(), True

        # Get the approver email(s) for this row
        row_department = row[4].strip() if len(row) > 4 else ''
        try:
            row_amount = float(row[6].replace(',', '').strip()) if len(row) > 6 and row[6].strip() else 0
        except:
            row_amount = 0

        chain = _find_approval_chain(row_department, row_amount)
        if chain is None:
            # No config — only Patt can approve
            if approver_email_session != DEFAULT_FINAL_APPROVER_EMAIL:
                return 403, 'application/json', json.dumps({
                    'error': 'No approval config found. Only the default final approver can act.'
                }).encode(), True
            new_status = 'Approved'
        else:
            resolved = _resolve_chain(chain, row[3].strip() if len(row) > 3 else '')
            level_emails = resolved.get(f'level_{current_level}', [])

            if approver_email_session not in level_emails:
                return 403, 'application/json', json.dumps({
                    'error': 'You are not an approver at the current level for this request',
                    'your_email': approver_email_session,
                    'current_level': current_level,
                    'level_approvers': level_emails,
                }).encode(), True

            # Determine what happens next
            total_active = max(resolved['levels']) if resolved['levels'] else current_level

            if current_level >= total_active:
                # Final level — approved!
                new_status = 'Approved'
            else:
                # Advance to next active level
                next_level = None
                for lvl in resolved['levels']:
                    if lvl > current_level:
                        next_level = lvl
                        break

                if next_level is None:
                    new_status = 'Approved'
                else:
                    new_status = _level_status(next_level)
                    next_emails = resolved.get(f'level_{next_level}', [])

                    # Update approver_email and approver_name to next level
                    ws.update_cell(row_idx, 12, ', '.join(next_emails))
                    ws.update_cell(row_idx, 13, ', '.join(_lookup_name(e) for e in next_emails))

                    # Notify next-level approvers
                    try:
                        emp_name = row[2].strip() if len(row) > 2 else ''
                        emp_email = row[3].strip() if len(row) > 3 else ''
                        cat = row[7].strip() if len(row) > 7 else ''
                        _notify_level_advance(
                            reimb_id, emp_name, emp_email,
                            row[6].strip() if len(row) > 6 else '0',
                            cat, session['name'], next_level, next_emails,
                            total_active
                        )
                    except Exception as e:
                        print(f'[notify] level advance error: {e}', flush=True)

        # Write status, actual approver, date
        ws.update_cell(row_idx, 11, new_status)
        ws.update_cell(row_idx, 13, session['name'])
        ws.update_cell(row_idx, 14, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

        # Notify submitter on final approval
        if new_status == 'Approved':
            try:
                emp_name = row[2].strip() if len(row) > 2 else ''
                emp_email = row[3].strip() if len(row) > 3 else ''
                cat = row[7].strip() if len(row) > 7 else ''
                _notify_decision(reimb_id, emp_name,
                                 row[6].strip() if len(row) > 6 else '0',
                                 cat, 'Approved', session['name'], '', emp_email)
            except Exception as e:
                print(f'[notify] approval decision error: {e}', flush=True)

        return 200, 'application/json', json.dumps({
            'success': True,
            'new_status': new_status,
            'message': 'Approved' if new_status == 'Approved' else f'Advanced to {new_status}'
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

    for i, row in enumerate(rows):
        if not row or row[0].strip() != reimb_id:
            continue
        row_idx = i + 1
        row_status = row[10].strip() if len(row) > 10 else ''
        current_level = _status_level(row_status)

        if current_level is None:
            return 403, 'application/json', json.dumps({
                'error': f'Request is in "{row_status}" status — cannot reject'
            }).encode(), True

        # Validate approver
        row_department = row[4].strip() if len(row) > 4 else ''
        try:
            row_amount = float(row[6].replace(',', '').strip()) if len(row) > 6 and row[6].strip() else 0
        except:
            row_amount = 0

        chain = _find_approval_chain(row_department, row_amount)
        if chain is None:
            if approver_email_session != DEFAULT_FINAL_APPROVER_EMAIL:
                return 403, 'application/json', json.dumps({
                    'error': 'No approval config found. Only the default final approver can act.'
                }).encode(), True
        else:
            resolved = _resolve_chain(chain, row[3].strip() if len(row) > 3 else '')
            level_emails = resolved.get(f'level_{current_level}', [])

            if approver_email_session not in level_emails:
                return 403, 'application/json', json.dumps({
                    'error': 'You are not an approver at the current level',
                    'current_level': current_level,
                    'level_approvers': level_emails,
                }).encode(), True

        # Determine rejection status code based on level
        if current_level == 1:
            new_status = 'Rejected'
        elif current_level == 2:
            new_status = 'Rejected'
        else:
            new_status = 'Rejected Final'

        ws.update_cell(row_idx, 11, new_status)
        ws.update_cell(row_idx, 13, session['name'])
        ws.update_cell(row_idx, 14, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        ws.update_cell(row_idx, 15, reason)

        # Notify submitter
        try:
            emp_name = row[2].strip() if len(row) > 2 else ''
            emp_email = row[3].strip() if len(row) > 3 else ''
            cat = row[7].strip() if len(row) > 7 else ''
            _notify_decision(reimb_id, emp_name,
                             row[6].strip() if len(row) > 6 else '0',
                             cat, new_status, session['name'], reason, emp_email)
        except Exception as e:
            print(f'[notify] reject error: {e}', flush=True)

        return 200, 'application/json', json.dumps({'success': True}).encode(), True

    return 404, 'application/json', json.dumps({'error': 'Reimbursement not found'}).encode(), True

RECEIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'receipts')

if not os.path.exists(RECEIPTS_DIR):
    os.makedirs(RECEIPTS_DIR, exist_ok=True)

def _api_upload_receipt(body_raw, headers):
    """Handle file upload. Uploads to Google Drive (persistent), returns public URL + SHA-256 hash."""
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
        # Upload to Google Drive (persistent storage — survives Railway redeploys)
        url = _upload_to_drive(body_raw, filename, content_type)
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

    # Only Finance team (explicit allowlist)
    if not _is_finance_user(session):
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

        _audit_log('PAY', rid, session['name'], session['email'],
                   'status', 'Approved', 'Paid',
                   f'Payment date {payment_date}' + (f', ref {payment_ref}' if payment_ref else ''))

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

    if not _is_finance_user(session):
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

    if not _is_finance_user(session):
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

            _audit_log('PAY', reimb_id, session['name'], session['email'],
                       'status', 'Approved', 'Paid',
                       f'Payment date {payment_date}' + (f', ref {payment_ref}' if payment_ref else ''))

            return 200, 'application/json', json.dumps({
                'success': True,
                'reimbursement_id': reimb_id,
                'payment_date': payment_date,
                'payment_ref': payment_ref,
            }).encode(), True

    return 404, 'application/json', json.dumps({'error': 'Reimbursement not found'}).encode(), True


def _api_finance_reject(body_raw, headers):
    """Finance team rejects an Approved reimbursement (receipt not acceptable, error
    slipped past approvers, etc.). Status → 'Rejected by Finance'; requester can
    Edit & Resubmit (fresh approval cycle). Reason is required and audit-logged."""
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Please log in'}).encode(), True

    if not _is_finance_user(session):
        return 403, 'application/json', json.dumps({'error': 'Only Finance team can reject for payment'}).encode(), True

    data = json.loads(body_raw or '{}')
    reimb_id = data.get('reimbursement_id', '').strip()
    reason = data.get('reason', '').strip()
    if not reimb_id:
        return 400, 'application/json', json.dumps({'error': 'Reimbursement ID required'}).encode(), True
    if not reason:
        return 400, 'application/json', json.dumps({'error': 'Rejection reason required'}).encode(), True

    ws = _get_reimb_sheet()
    rows = ws.get_all_values()
    for i, row in enumerate(rows):
        if not row or row[0].strip() != reimb_id:
            continue
        row_idx = i + 1
        status = row[10].strip() if len(row) > 10 else ''
        if status == 'Paid':
            return 409, 'application/json', json.dumps({'error': 'Already marked as Paid'}).encode(), True
        if status != 'Approved':
            return 400, 'application/json', json.dumps({
                'error': f'Status is "{status}" — only Approved requests can be rejected by Finance'
            }).encode(), True

        ws.update_cell(row_idx, 11, 'Rejected by Finance')
        ws.update_cell(row_idx, 13, session['name'])
        ws.update_cell(row_idx, 14, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        ws.update_cell(row_idx, 15, reason)

        _audit_log('FINANCE REJECT', reimb_id, session['name'], session['email'],
                   'status', 'Approved', 'Rejected by Finance', reason)

        try:
            emp_name = row[2].strip() if len(row) > 2 else ''
            emp_email = row[3].strip() if len(row) > 3 else ''
            cat = row[7].strip() if len(row) > 7 else ''
            _notify_decision(reimb_id, emp_name,
                             row[6].strip() if len(row) > 6 else '0',
                             cat, 'Rejected by Finance', session['name'], reason, emp_email)

            # Also loop in the approvers of record (col 12 = approver_email)
            approver_emails = [e.strip().lower() for e in (row[11].strip() if len(row) > 11 else '').split(',') if e.strip()]
            if approver_emails:
                _notify_finance_reject_approvers(reimb_id, emp_name,
                                                 row[6].strip() if len(row) > 6 else '0',
                                                 cat, reason, session['name'], approver_emails)
        except Exception as e:
            print(f'[notify] finance reject error: {e}', flush=True)

        return 200, 'application/json', json.dumps({
            'success': True,
            'reimbursement_id': reimb_id,
            'status': 'Rejected by Finance',
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
        return 200, 'application/json', json.dumps({'total': 0, 'pending': 0, 'approved': 0, 'rejected': 0, 'paid': 0, 'cancelled': 0}).encode(), True

    user_email = session['email']
    total = pending = approved = rejected = paid = cancelled = 0

    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        row_email = row[3].strip().lower() if len(row) > 3 else ''
        row_status = row[10].strip() if len(row) > 10 else ''

        # Stats always count the user's own submissions (employee_email), even if they're also an approver
        if row_email != user_email.strip().lower():
            continue

        total += 1
        if row_status in ('Pending', 'Pending Second', 'Pending Final'):
            pending += 1
        elif row_status == 'Approved':
            approved += 1
        elif row_status in ('Rejected', 'Rejected Final', 'Rejected by Finance'):
            rejected += 1
        elif row_status == 'Paid':
            paid += 1
        elif row_status == 'Cancelled':
            cancelled += 1

    return 200, 'application/json', json.dumps({
        'total': total, 'pending': pending, 'approved': approved,
        'rejected': rejected, 'paid': paid, 'cancelled': cancelled
    }).encode(), True


def _api_portal_tools(headers):
    """Return the list of portal tools the authenticated user can see.
    Access matrix (Shaun-confirmed 2026-08-20):
      Reimbursement + Disbursement → all employees
      Recon/FS/Board → PortalAccess tab in the employee workbook (sheet-driven)
    """
    token = _extract_token(headers)
    session = _validate_session(token)
    if not session:
        return 401, 'application/json', json.dumps({'error': 'Unauthorized'}).encode(), True

    email = session.get('email', '').strip().lower()
    tools = [
        {
            'id': 'reimbursement',
            'name': 'Reimbursement Portal',
            'icon': '\U0001f4b8',
            'desc': 'Expense reimbursement requests, approvals, and payments.',
            'url': '/reimbursements',
            'category': 'Finance & Ops',
        },
        {
            'id': 'disbursement',
            'name': 'Disbursement Portal',
            'icon': '\U0001f9fe',
            'desc': 'Disbursement requests, approvals, and payments.',
            'url': '/disbursements',
            'category': 'Finance & Ops',
        },
    ]
    if _can_access(email, 'recon'):
        tools.append({
            'id': 'recon',
            'name': 'Recon Portal',
            'icon': '\U0001f504',
            'desc': 'Reconciliation for orders, refunds, shipping, and seller withdrawals.',
            'url': '/recon',
            'category': 'Finance & Ops',
        })
    if _can_access(email, 'fs'):
        tools.append({
            'id': 'fs',
            'name': 'Financial Statements',
            'icon': '\U0001f4ca',
            'desc': 'Board financial statement decks \u2014 restricted access.',
            'url': '/fs',
            'category': 'Reports',
        })
    if _can_access(email, 'board'):
        tools.append({
            'id': 'board',
            'name': 'Board Presentations',
            'icon': '\U0001f3a4',
            'desc': 'Board decks and presentations \u2014 restricted.',
            'url': '/board',
            'category': 'Reports',
        })
    return 200, 'application/json', json.dumps({
        'tools': tools,
        'user': {
            'email': email,
            'name': session.get('name', ''),
            'department': session.get('department', ''),
            'role': session.get('role', ''),
        },
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