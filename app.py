#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
نظام استطلاع الرأي الإلكتروني – طوباس 2026
تخزين دائم عبر PostgreSQL (Supabase) مع fallback لـ JSON
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
os.makedirs(DATA_DIR, exist_ok=True)

lock = threading.Lock()
DATABASE_URL = os.environ.get('DATABASE_URL', '')

# ─── قاعدة البيانات ───────────────────────────────────────────────
def get_conn():
    import psycopg2
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    """إنشاء الجداول إذا لم تكن موجودة"""
    if not DATABASE_URL:
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS kv_store (
                        key   TEXT PRIMARY KEY,
                        value TEXT NOT NULL DEFAULT '{}'
                    )
                """)
            conn.commit()
        print("[DB] PostgreSQL connected & tables ready ✅")
    except Exception as e:
        print(f"[DB] Init error: {e}")

def db_get(key, default):
    """قراءة قيمة من قاعدة البيانات"""
    if not DATABASE_URL:
        return _file_get(key, default)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM kv_store WHERE key=%s", (key,))
                row = cur.fetchone()
                return json.loads(row[0]) if row else default
    except Exception as e:
        print(f"[DB] get error ({key}): {e}")
        return default

def db_set(key, value):
    """حفظ قيمة في قاعدة البيانات"""
    if not DATABASE_URL:
        return _file_set(key, value)
    try:
        v = json.dumps(value, ensure_ascii=False)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO kv_store (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """, (key, v))
            conn.commit()
    except Exception as e:
        print(f"[DB] set error ({key}): {e}")

# ─── Fallback: ملفات JSON محلية (للتطوير) ────────────────────────
def _fp(key):
    return os.path.join(DATA_DIR, f"{key}.json")

def _file_get(key, default):
    try:
        p = _fp(key)
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                return json.load(f)
    except:
        pass
    return default

def _file_set(key, value):
    try:
        with open(_fp(key), 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[FILE] set error ({key}): {e}")

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
def h(s):
    return hashlib.sha256(str(s).encode()).hexdigest()[:20]

def find_voter(reg):
    reg = str(reg).strip()
    return (VOTERS_DB.get(reg)
            or VOTERS_DB.get(reg.lstrip('0'))
            or (VOTERS_DB.get(str(int(reg))) if reg.isdigit() else None))

def get_client_ip():
    xff = request.headers.get('X-Forwarded-For', '')
    return xff.split(',')[0].strip() if xff else (request.remote_addr or 'unknown')

def is_open():
    return db_get('settings', {}).get('open', True)

def get_security_cfg():
    cfg = db_get('settings', {})
    return {
        'max_votes_per_device': cfg.get('max_votes_per_device', 1),
        'max_votes_per_ip':     cfg.get('max_votes_per_ip',     3),
        'block_device':         cfg.get('block_device',         True),
        'block_ip':             cfg.get('block_ip',             True),
    }

def check_device(fp_hash, ip_hash, cfg):
    devices = db_get('devices', {'fingerprints': {}, 'ips': {}})
    if cfg['block_device'] and fp_hash:
        cnt = devices['fingerprints'].get(fp_hash, {}).get('count', 0)
        if cnt >= cfg['max_votes_per_device']:
            return False, '⛔ هذا الجهاز شارك مسبقاً. لا يُسمح بأكثر من مشاركة واحدة لكل جهاز.'
    if cfg['block_ip'] and ip_hash:
        cnt = devices['ips'].get(ip_hash, {}).get('count', 0)
        if cnt >= cfg['max_votes_per_ip']:
            return False, f'⛔ تجاوزت هذه الشبكة الحد المسموح به.'
    return True, ''

def record_device(fp_hash, ip_hash, reg_hash):
    devices = db_get('devices', {'fingerprints': {}, 'ips': {}})
    now = datetime.now().strftime('%H:%M:%S')
    if fp_hash:
        if fp_hash not in devices['fingerprints']:
            devices['fingerprints'][fp_hash] = {'count': 0, 'first': now}
        devices['fingerprints'][fp_hash]['count'] += 1
        devices['fingerprints'][fp_hash]['last'] = now
    if ip_hash:
        if ip_hash not in devices['ips']:
            devices['ips'][ip_hash] = {'count': 0, 'first': now}
        devices['ips'][ip_hash]['count'] += 1
        devices['ips'][ip_hash]['last'] = now
    db_set('devices', devices)

def log_security(event_type, detail):
    log = db_get('security_log', [])
    log.append({'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'type': event_type, 'detail': detail})
    db_set('security_log', log[-500:])

# ─── API: تحقق من مشارك ───────────────────────────────────────────
@app.route('/api/voter/<reg>')
def api_voter(reg):
    if not is_open():
        return jsonify({'ok': False, 'error': 'الاستطلاع مغلق حالياً'}), 403
    fp      = request.args.get('fp', '')
    fp_hash = h(fp) if fp else ''
    ip_hash = h(get_client_ip())
    cfg     = get_security_cfg()
    allowed, err = check_device(fp_hash, ip_hash, cfg)
    if not allowed:
        log_security('DEVICE_BLOCKED_AT_LOGIN', {'fp': fp_hash[:8], 'ip': ip_hash[:8]})
        return jsonify({'ok': False, 'error': err}), 403
    voter = find_voter(reg)
    if not voter:
        return jsonify({'ok': False, 'error': 'الرقم غير موجود في سجل المشاركين'}), 404
    voted = db_get('voted', {})
    if voted.get(h(reg.strip())):
        return jsonify({'ok': False, 'error': 'هذا الرقم شارك في الاستطلاع مسبقاً'}), 403
    return jsonify({'ok': True, 'name': voter['name'], 'center': voter['center']})

# ─── API: القوائم ──────────────────────────────────────────────────
@app.route('/api/candidates')
def api_candidates():
    return jsonify({'ok': True, 'lists': CANDIDATES})

# ─── API: تسجيل المشاركة ──────────────────────────────────────────
@app.route('/api/vote', methods=['POST'])
def api_vote():
    if not is_open():
        return jsonify({'ok': False, 'error': 'الاستطلاع مغلق'}), 403
    data    = request.get_json(silent=True) or {}
    reg     = str(data.get('reg_num', '')).strip()
    list_id = data.get('list_id')
    chosen  = data.get('candidates', [])
    fp      = data.get('fingerprint', '')
    if not reg or not list_id or not chosen:
        return jsonify({'ok': False, 'error': 'بيانات ناقصة'}), 400
    if not 1 <= len(chosen) <= 5:
        return jsonify({'ok': False, 'error': 'اختر من 1 إلى 5 مرشحين'}), 400
    voter = find_voter(reg)
    if not voter:
        return jsonify({'ok': False, 'error': 'رقم غير صالح'}), 404
    lst = next((l for l in CANDIDATES if l['id'] == list_id), None)
    if not lst:
        return jsonify({'ok': False, 'error': 'قائمة غير موجودة'}), 400
    fp_hash = h(fp) if fp else ''
    ip_hash = h(get_client_ip())
    cfg     = get_security_cfg()
    with lock:
        voted = db_get('voted', {})
        reg_h = h(reg)
        if voted.get(reg_h):
            return jsonify({'ok': False, 'error': 'تمت المشاركة بهذا الرقم مسبقاً'}), 403
        allowed, err = check_device(fp_hash, ip_hash, cfg)
        if not allowed:
            log_security('DEVICE_BLOCKED_AT_VOTE', {'fp': fp_hash[:8], 'ip': ip_hash[:8]})
            return jsonify({'ok': False, 'error': err}), 403
        # حفظ الأصوات
        votes = db_get('votes', {'lists': {}, 'candidates': {}, 'total': 0})
        votes['lists'][lst['name']]  = votes['lists'].get(lst['name'], 0) + 1
        votes['total']               = votes.get('total', 0) + 1
        for c in chosen:
            votes['candidates'][c]   = votes['candidates'].get(c, 0) + 1
        db_set('votes', votes)
        # تسجيل المشارك (hash فقط)
        voted[reg_h] = True
        db_set('voted', voted)
        # تسجيل الجهاز
        record_device(fp_hash, ip_hash, reg_h)
    return jsonify({'ok': True, 'msg': 'تم تسجيل رأيك بنجاح'})

# ─── API: الإدارة ──────────────────────────────────────────────────
@app.route('/api/admin/results')
def api_results():
    if request.headers.get('X-Admin-Pass') != os.environ.get('ADMIN_PASSWORD', 'Tubas@0598652625'):
        return jsonify({'ok': False, 'error': 'غير مصرح'}), 401
    votes   = db_get('votes',       {'lists': {}, 'candidates': {}, 'total': 0})
    voted   = db_get('voted',       {})
    devices = db_get('devices',     {'fingerprints': {}, 'ips': {}})
    sec_log = db_get('security_log',[])
    cfg     = get_security_cfg()
    total   = len(VOTERS_DB)
    tv      = sum(votes['lists'].values()) or 1
    lists_r = sorted([
        {'id': l['id'], 'name': l['name'],
         'votes': votes['lists'].get(l['name'], 0),
         'pct':   round(votes['lists'].get(l['name'], 0) / tv * 100, 1),
         'candidates_count': len(l['candidates'])}
        for l in CANDIDATES], key=lambda x: -x['votes'])
    cands_r = sorted([
        {'name': c['name'], 'list': l['name'], 'gender': c['gender'],
         'votes': votes['candidates'].get(c['name'], 0)}
        for l in CANDIDATES for c in l['candidates']], key=lambda x: -x['votes'])
    fp_counts = sorted(
        [{'fp': k[:8]+'...', 'votes': v['count'], 'last': v.get('last','')}
         for k, v in devices['fingerprints'].items() if v['count'] > 1],
        key=lambda x: -x['votes'])
    ip_counts = sorted(
        [{'ip': k[:8]+'...', 'votes': v['count'], 'last': v.get('last','')}
         for k, v in devices['ips'].items()],
        key=lambda x: -x['votes'])
    return jsonify({
        'ok': True,
        'total_voters':        total,
        'total_voted':         len(voted),
        'participation_pct':   round(len(voted) / max(total, 1) * 100, 1),
        'election_open':       is_open(),
        'total_devices':       len(devices['fingerprints']),
        'blocked_attempts':    len([e for e in sec_log if 'BLOCKED' in e.get('type','')]),
        'security_settings':   cfg,
        'suspicious_devices':  fp_counts[:10],
        'suspicious_ips':      ip_counts[:10],
        'recent_security_log': sec_log[-20:][::-1],
        'lists':               lists_r,
        'candidates':          cands_r,
    })

@app.route('/api/admin/toggle', methods=['POST'])
def api_toggle():
    if request.headers.get('X-Admin-Pass') != os.environ.get('ADMIN_PASSWORD', 'Tubas@0598652625'):
        return jsonify({'ok': False, 'error': 'غير مصرح'}), 401
    cfg = db_get('settings', {})
    cfg['open'] = not cfg.get('open', True)
    db_set('settings', cfg)
    return jsonify({'ok': True, 'open': cfg['open']})

@app.route('/api/admin/security', methods=['POST'])
def api_security():
    if request.headers.get('X-Admin-Pass') != os.environ.get('ADMIN_PASSWORD', 'Tubas@0598652625'):
        return jsonify({'ok': False, 'error': 'غير مصرح'}), 401
    data = request.get_json(silent=True) or {}
    cfg  = db_get('settings', {})
    for k in ['max_votes_per_device','max_votes_per_ip']:
        if k in data: cfg[k] = int(data[k])
    for k in ['block_device','block_ip']:
        if k in data: cfg[k] = bool(data[k])
    db_set('settings', cfg)
    return jsonify({'ok': True, 'settings': get_security_cfg()})

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

# ─── تشغيل ────────────────────────────────────────────────────────
load_data()
init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    mode = "PostgreSQL ☁️" if DATABASE_URL else "JSON Files 📁 (local)"
    print(f'\n{"="*52}')
    print(f'  استطلاع رأي مجلس بلدي طوباس 2026')
    print(f'  الرابط:    http://localhost:{port}')
    print(f'  ناخبون:   {len(VOTERS_DB):,}')
    print(f'  التخزين:  {mode}')
    print(f'{"="*52}\n')
    app.run(host='0.0.0.0', port=port)
