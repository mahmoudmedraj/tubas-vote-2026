#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
نظام التصويت الإلكتروني – طوباس 2026
نسخة محسّنة بحماية ثلاثية: بصمة الجهاز + IP + منع التصويت المتكرر
"""

import os, json, hashlib, threading, time
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import pandas as pd

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(BASE_DIR, 'data')
EXCEL_FILE  = os.path.join(DATA_DIR, 'voters.xlsx')
VOTES_FILE  = os.path.join(DATA_DIR, 'votes.json')
VOTED_FILE  = os.path.join(DATA_DIR, 'voted.json')
STATUS_FILE = os.path.join(DATA_DIR, 'status.json')
DEVICES_FILE= os.path.join(DATA_DIR, 'devices.json')   # بصمات الأجهزة
SECURITY_FILE=os.path.join(DATA_DIR, 'security.json')  # سجل الأمان

os.makedirs(DATA_DIR, exist_ok=True)
lock = threading.Lock()

# ─── قاعدة البيانات في الذاكرة ───────────────────────────────────
VOTERS_DB  = {}
CANDIDATES = []

def load_data():
    global VOTERS_DB, CANDIDATES
    if not os.path.exists(EXCEL_FILE):
        print(f"[ERROR] Excel not found: {EXCEL_FILE}")
        return

    df = pd.read_excel(EXCEL_FILE, sheet_name='سجل الناخبين', dtype=str)
    df.columns = df.columns.str.strip()
    for _, row in df.iterrows():
        reg    = str(row.get('رقم التسجيل الانتخابي', '')).strip().replace('.0','')
        name   = str(row.get('الاسم الكامل',   '')).strip()
        center = str(row.get('مركز الاقتراع',  '')).strip()
        if reg and reg != 'nan':
            VOTERS_DB[reg] = {'name': name, 'center': center}

    df2 = pd.read_excel(EXCEL_FILE, sheet_name='قوائم المرشحين')
    df2.columns = df2.columns.str.strip()
    tmp = {}
    for _, row in df2.iterrows():
        lid   = int(row['رقم القائمة'])
        lname = str(row['اسم القائمة']).strip()
        cname = str(row['اسم المرشح']).strip()
        gen   = str(row['الجنس']).strip()
        order = int(row['الترتيب'])
        if lid not in tmp:
            tmp[lid] = {'id': lid, 'name': lname, 'candidates': []}
        tmp[lid]['candidates'].append({'order': order, 'name': cname, 'gender': gen})
    for lst in tmp.values():
        lst['candidates'].sort(key=lambda x: x['order'])
    CANDIDATES = [tmp[k] for k in sorted(tmp.keys())]

    print(f"[OK] Voters: {len(VOTERS_DB):,}  |  Lists: {len(CANDIDATES)}")


# ─── مساعدات ─────────────────────────────────────────────────────
def rj(path, default):
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return default

def wj(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_open():
    return rj(STATUS_FILE, {}).get('open', True)

def h(s):
    """تشفير نص بـ SHA-256 (16 حرف فقط للاختصار)"""
    return hashlib.sha256(str(s).encode('utf-8')).hexdigest()[:20]

def find_voter(reg):
    reg = str(reg).strip()
    return (VOTERS_DB.get(reg)
            or VOTERS_DB.get(reg.lstrip('0'))
            or (VOTERS_DB.get(str(int(reg))) if reg.isdigit() else None))

def get_client_ip():
    """الحصول على IP الحقيقي حتى خلف البروكسي (Railway/Render)"""
    # X-Forwarded-For: ip1, ip2, ip3 — نأخذ الأول (الأصلي)
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or 'unknown'

def get_security_settings():
    cfg = rj(STATUS_FILE, {})
    return {
        'max_votes_per_device': cfg.get('max_votes_per_device', 1),   # حد الجهاز
        'max_votes_per_ip':     cfg.get('max_votes_per_ip',     3),    # حد الشبكة (للعائلات)
        'block_device':         cfg.get('block_device',         True), # تفعيل حماية الجهاز
        'block_ip':             cfg.get('block_ip',             True), # تفعيل حماية IP
    }

def log_security_event(event_type, detail):
    """تسجيل أحداث الأمان"""
    log = rj(SECURITY_FILE, [])
    log.append({
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'type': event_type,
        'detail': detail
    })
    # احتفظ بآخر 500 حدث فقط
    wj(SECURITY_FILE, log[-500:])


# ─── فحص بصمة الجهاز ─────────────────────────────────────────────
def check_device(fp_hash, ip_hash, cfg):
    """
    يُرجع: (مسموح: bool, رسالة خطأ: str)
    """
    devices = rj(DEVICES_FILE, {'fingerprints': {}, 'ips': {}})

    # ── فحص بصمة الجهاز ──
    if cfg['block_device'] and fp_hash:
        fp_data = devices['fingerprints'].get(fp_hash, {})
        count   = fp_data.get('count', 0)
        if count >= cfg['max_votes_per_device']:
            return False, f"⛔ هذا الجهاز قام بالتصويت مسبقاً. لا يُسمح بأكثر من تصويت واحد لكل جهاز."

    # ── فحص IP ──
    if cfg['block_ip'] and ip_hash:
        ip_data = devices['ips'].get(ip_hash, {})
        count   = ip_data.get('count', 0)
        if count >= cfg['max_votes_per_ip']:
            return False, f"⛔ تجاوزت هذه الشبكة الحد المسموح به ({cfg['max_votes_per_ip']}) من هذا الجهاز/الشبكة."

    return True, ''

def record_device(fp_hash, ip_hash, reg_hash):
    """تسجيل الجهاز بعد التصويت الناجح"""
    devices = rj(DEVICES_FILE, {'fingerprints': {}, 'ips': {}})

    if fp_hash:
        if fp_hash not in devices['fingerprints']:
            devices['fingerprints'][fp_hash] = {'count': 0, 'voters': [], 'first': datetime.now().strftime('%H:%M:%S')}
        devices['fingerprints'][fp_hash]['count'] += 1
        devices['fingerprints'][fp_hash]['voters'].append(reg_hash)
        devices['fingerprints'][fp_hash]['last'] = datetime.now().strftime('%H:%M:%S')

    if ip_hash:
        if ip_hash not in devices['ips']:
            devices['ips'][ip_hash] = {'count': 0, 'voters': [], 'first': datetime.now().strftime('%H:%M:%S')}
        devices['ips'][ip_hash]['count'] += 1
        devices['ips'][ip_hash]['voters'].append(reg_hash)
        devices['ips'][ip_hash]['last'] = datetime.now().strftime('%H:%M:%S')

    wj(DEVICES_FILE, devices)


# ─── API: تحقق من ناخب ────────────────────────────────────────────
@app.route('/api/voter/<reg>')
def api_voter(reg):
    if not is_open():
        return jsonify({'ok': False, 'error': 'التصويت مغلق حالياً'}), 403

    # بصمة الجهاز من query param
    fp      = request.args.get('fp', '')
    fp_hash = h(fp)   if fp else ''
    ip      = get_client_ip()
    ip_hash = h(ip)

    # ── فحص الجهاز قبل البحث عن الرقم ──
    cfg = get_security_settings()
    allowed, err = check_device(fp_hash, ip_hash, cfg)
    if not allowed:
        log_security_event('DEVICE_BLOCKED_AT_LOGIN', {'fp': fp_hash[:8], 'ip': ip_hash[:8], 'reg': h(reg)[:8]})
        return jsonify({'ok': False, 'error': err}), 403

    voter = find_voter(reg)
    if not voter:
        return jsonify({'ok': False, 'error': 'الرقم غير موجود في سجل الناخبين'}), 404

    voted = rj(VOTED_FILE, {})
    if voted.get(h(reg.strip())):
        return jsonify({'ok': False, 'error': 'لقد قام هذا الرقم الانتخابي بالتصويت مسبقاً'}), 403

    return jsonify({'ok': True, 'name': voter['name'], 'center': voter['center']})


# ─── API: القوائم والمرشحون ───────────────────────────────────────
@app.route('/api/candidates')
def api_candidates():
    return jsonify({'ok': True, 'lists': CANDIDATES})


# ─── API: تسجيل الصوت ────────────────────────────────────────────
@app.route('/api/vote', methods=['POST'])
def api_vote():
    if not is_open():
        return jsonify({'ok': False, 'error': 'التصويت مغلق'}), 403

    data    = request.get_json(silent=True) or {}
    reg     = str(data.get('reg_num', '')).strip()
    list_id = data.get('list_id')
    chosen  = data.get('candidates', [])
    fp      = data.get('fingerprint', '')

    if not reg or not list_id or not chosen:
        return jsonify({'ok': False, 'error': 'بيانات ناقصة'}), 400
    if not 1 <= len(chosen) <= 5:
        return jsonify({'ok': False, 'error': 'اختر من 1 إلى 5 مرشحين'}), 400

    fp_hash = h(fp) if fp else ''
    ip      = get_client_ip()
    ip_hash = h(ip)
    cfg     = get_security_settings()

    voter = find_voter(reg)
    if not voter:
        return jsonify({'ok': False, 'error': 'رقم غير صالح'}), 404

    lst = next((l for l in CANDIDATES if l['id'] == list_id), None)
    if not lst:
        return jsonify({'ok': False, 'error': 'قائمة غير موجودة'}), 400

    with lock:
        # ── التحقق من التصويت السابق بالرقم الانتخابي ──
        voted = rj(VOTED_FILE, {})
        reg_h = h(reg)
        if voted.get(reg_h):
            return jsonify({'ok': False, 'error': 'تم التصويت بهذا الرقم مسبقاً'}), 403

        # ── التحقق من الجهاز / IP ──
        allowed, err = check_device(fp_hash, ip_hash, cfg)
        if not allowed:
            log_security_event('DEVICE_BLOCKED_AT_VOTE', {
                'fp': fp_hash[:8], 'ip': ip_hash[:8], 'reg': reg_h[:8],
                'list': lst['name']
            })
            return jsonify({'ok': False, 'error': err}), 403

        # ── حفظ الأصوات (منفصلة تماماً عن هوية الناخب) ──
        votes = rj(VOTES_FILE, {'lists': {}, 'candidates': {}, 'total': 0})
        votes['lists'][lst['name']]  = votes['lists'].get(lst['name'], 0) + 1
        votes['total']               = votes.get('total', 0) + 1
        for c in chosen:
            votes['candidates'][c]   = votes['candidates'].get(c, 0) + 1
        wj(VOTES_FILE, votes)

        # ── تسجيل الرقم الانتخابي كمصوَّت (hash فقط) ──
        voted[reg_h] = True
        wj(VOTED_FILE, voted)

        # ── تسجيل الجهاز ──
        record_device(fp_hash, ip_hash, reg_h)

    return jsonify({'ok': True, 'msg': 'تم تسجيل صوتك بنجاح'})


# ─── API: إدارة - نتائج ────────────────────────────────────────────
@app.route('/api/admin/results')
def api_results():
    if request.headers.get('X-Admin-Pass') != 'admin@2026':
        return jsonify({'ok': False, 'error': 'غير مصرح'}), 401

    votes   = rj(VOTES_FILE,  {'lists': {}, 'candidates': {}, 'total': 0})
    voted   = rj(VOTED_FILE,  {})
    devices = rj(DEVICES_FILE, {'fingerprints': {}, 'ips': {}})
    sec_log = rj(SECURITY_FILE, [])
    cfg     = get_security_settings()
    total   = len(VOTERS_DB)
    tv      = sum(votes['lists'].values()) or 1

    # ── إحصائيات الأمان ──
    fp_counts = sorted(
        [{'fp': k[:8]+'...', 'votes': v['count'], 'last': v.get('last','')}
         for k, v in devices['fingerprints'].items() if v['count'] > 1],
        key=lambda x: -x['votes']
    )
    ip_counts = sorted(
        [{'ip': k[:8]+'...', 'votes': v['count'], 'last': v.get('last','')}
         for k, v in devices['ips'].items() if v['count'] > 0],
        key=lambda x: -x['votes']
    )
    total_devices   = len(devices['fingerprints'])
    blocked_attempts= len([e for e in sec_log if 'BLOCKED' in e['type']])

    lists_r = sorted([
        {'id': l['id'], 'name': l['name'],
         'votes': votes['lists'].get(l['name'], 0),
         'pct':   round(votes['lists'].get(l['name'], 0) / tv * 100, 1),
         'candidates_count': len(l['candidates'])}
        for l in CANDIDATES
    ], key=lambda x: -x['votes'])

    cands_r = sorted([
        {'name': c['name'], 'list': l['name'],
         'gender': c['gender'],
         'votes': votes['candidates'].get(c['name'], 0)}
        for l in CANDIDATES for c in l['candidates']
    ], key=lambda x: -x['votes'])

    return jsonify({
        'ok': True,
        'total_voters':        total,
        'total_voted':         len(voted),
        'participation_pct':   round(len(voted) / max(total, 1) * 100, 1),
        'election_open':       is_open(),
        'total_devices':       total_devices,
        'blocked_attempts':    blocked_attempts,
        'security_settings':   cfg,
        'suspicious_devices':  fp_counts[:10],
        'suspicious_ips':      ip_counts[:10],
        'recent_security_log': sec_log[-20:][::-1],
        'lists':               lists_r,
        'candidates':          cands_r,
    })


# ─── API: تحديث إعدادات الأمان ────────────────────────────────────
@app.route('/api/admin/security', methods=['POST'])
def api_security():
    if request.headers.get('X-Admin-Pass') != 'admin@2026':
        return jsonify({'ok': False, 'error': 'غير مصرح'}), 401
    data = request.get_json(silent=True) or {}
    cfg  = rj(STATUS_FILE, {})
    if 'max_votes_per_device' in data: cfg['max_votes_per_device'] = int(data['max_votes_per_device'])
    if 'max_votes_per_ip'     in data: cfg['max_votes_per_ip']     = int(data['max_votes_per_ip'])
    if 'block_device'         in data: cfg['block_device']         = bool(data['block_device'])
    if 'block_ip'             in data: cfg['block_ip']             = bool(data['block_ip'])
    wj(STATUS_FILE, cfg)
    return jsonify({'ok': True, 'settings': get_security_settings()})


# ─── API: إغلاق/فتح التصويت ──────────────────────────────────────
@app.route('/api/admin/toggle', methods=['POST'])
def api_toggle():
    if request.headers.get('X-Admin-Pass') != 'admin@2026':
        return jsonify({'ok': False, 'error': 'غير مصرح'}), 401
    cfg = rj(STATUS_FILE, {})
    cfg['open'] = not cfg.get('open', True)
    wj(STATUS_FILE, cfg)
    return jsonify({'ok': True, 'open': cfg['open']})


# ─── الصفحة الرئيسية ─────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


# ─── تشغيل ───────────────────────────────────────────────────────
load_data()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    print(f'\n{"="*52}')
    print(f'  نظام التصويت الإلكتروني – طوباس 2026')
    print(f'  الرابط:  http://localhost:{port}')
    print(f'  ناخبون: {len(VOTERS_DB):,}')
    print(f'  الحماية: بصمة الجهاز + IP + منع التكرار')
    print(f'{"="*52}\n')
    app.run(host='0.0.0.0', port=port)
