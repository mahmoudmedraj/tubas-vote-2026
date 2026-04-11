#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
استطلاع رأي إلكتروني – طوباس 2026
تخزين ثلاثي الطبقات: ذاكرة + PostgreSQL + JSON
"""

import os, json, hashlib, threading
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import pandas as pd

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE_DIR, 'data')
EXCEL_FILE = os.path.join(DATA_DIR, 'voters.xlsx')
os.makedirs(DATA_DIR, exist_ok=True)

lock         = threading.Lock()
DATABASE_URL = os.environ.get('DATABASE_URL', '')

# ══ ذاكرة عشان: في حالة ما عندنا DB لا يصوت نفس الشخص مرتين ═══════
# تُحمَّل من DB عند بدء التشغيل وتبقى في الذاكرة طوال الجلسة
VOTED_CACHE   = set()   # أرقام الناخبين (hash) الذين صوّتوا
DEVICES_CACHE = {}      # بصمات الأجهزة
IP_CACHE      = {}      # عدد الأصوات لكل IP

# ══ PostgreSQL ══════════════════════════════════════════════════════
def get_conn():
    import psycopg2
    url = DATABASE_URL
    # Supabase يحتاج ?sslmode=require
    if 'supabase' in url and 'sslmode' not in url:
        url += '?sslmode=require'
    return psycopg2.connect(url, connect_timeout=8)

def init_db():
    global VOTED_CACHE, DEVICES_CACHE, IP_CACHE
    if not DATABASE_URL:
        print("[DB] No DATABASE_URL — using memory+JSON fallback")
        # تحميل من JSON إذا موجود
        v = _file_get('voted', {})
        VOTED_CACHE = set(v.keys())
        d = _file_get('devices', {'fingerprints':{},'ips':{}})
        DEVICES_CACHE = d.get('fingerprints',{})
        IP_CACHE      = d.get('ips',{})
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS kv_store (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL DEFAULT '{}'
                    )
                """)
            conn.commit()
        # تحميل البيانات الموجودة في الذاكرة
        voted   = db_get('voted',   {})
        devices = db_get('devices', {'fingerprints':{},'ips':{}})
        VOTED_CACHE   = set(voted.keys())
        DEVICES_CACHE = devices.get('fingerprints',{})
        IP_CACHE      = devices.get('ips',{})
        print(f"[DB] PostgreSQL ✅ — {len(VOTED_CACHE)} votes loaded from DB")
    except Exception as e:
        print(f"[DB] Warning: {e} — falling back to memory+JSON")
        v = _file_get('voted', {})
        VOTED_CACHE = set(v.keys())

def db_get(key, default):
    if not DATABASE_URL:
        return _file_get(key, default)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM kv_store WHERE key=%s", (key,))
                row = cur.fetchone()
                return json.loads(row[0]) if row else default
    except Exception as e:
        print(f"[DB] get({key}): {e}")
        return _file_get(key, default)

def db_set(key, value):
    if not DATABASE_URL:
        return _file_set(key, value)
    try:
        v = json.dumps(value, ensure_ascii=False)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO kv_store (key,value) VALUES (%s,%s)
                    ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
                """, (key, v))
            conn.commit()
    except Exception as e:
        print(f"[DB] set({key}): {e}")
        _file_set(key, value)  # fallback

def _fp(k): return os.path.join(DATA_DIR, f"{k}.json")
def _file_get(k, d):
    try:
        p = _fp(k)
        if os.path.exists(p):
            with open(p,'r',encoding='utf-8') as f: return json.load(f)
    except: pass
    return d
def _file_set(k, v):
    try:
        with open(_fp(k),'w',encoding='utf-8') as f:
            json.dump(v, f, ensure_ascii=False, indent=2)
    except Exception as e: print(f"[FILE] {k}: {e}")

# ══ بيانات الناخبين ══════════════════════════════════════════════
VOTERS_DB  = {}
CANDIDATES = []

def load_data():
    global VOTERS_DB, CANDIDATES
    if not os.path.exists(EXCEL_FILE): return
    df = pd.read_excel(EXCEL_FILE, sheet_name='سجل الناخبين', dtype=str)
    df.columns = df.columns.str.strip()
    for _, row in df.iterrows():
        reg  = str(row.get('رقم التسجيل الانتخابي','')).strip().replace('.0','')
        name = str(row.get('الاسم الكامل','')).strip()
        ctr  = str(row.get('مركز الاقتراع','')).strip()
        if reg and reg != 'nan':
            VOTERS_DB[reg] = {'name':name,'center':ctr}
    df2 = pd.read_excel(EXCEL_FILE, sheet_name='قوائم المرشحين')
    df2.columns = df2.columns.str.strip()
    tmp = {}
    for _, row in df2.iterrows():
        lid = int(row['رقم القائمة'])
        if lid not in tmp:
            tmp[lid] = {'id':lid,'name':str(row['اسم القائمة']).strip(),'candidates':[]}
        tmp[lid]['candidates'].append({
            'order':int(row['الترتيب']),
            'name':str(row['اسم المرشح']).strip(),
            'gender':str(row['الجنس']).strip()
        })
    for l in tmp.values(): l['candidates'].sort(key=lambda x:x['order'])
    CANDIDATES = [tmp[k] for k in sorted(tmp.keys())]
    print(f"[OK] Voters:{len(VOTERS_DB):,}  Lists:{len(CANDIDATES)}")

# ══ مساعدات ══════════════════════════════════════════════════════
def h(s): return hashlib.sha256(str(s).encode()).hexdigest()[:20]
def find_voter(reg):
    reg = str(reg).strip()
    return (VOTERS_DB.get(reg)
            or VOTERS_DB.get(reg.lstrip('0'))
            or (VOTERS_DB.get(str(int(reg))) if reg.isdigit() else None))
def get_ip():
    xff = request.headers.get('X-Forwarded-For','')
    return xff.split(',')[0].strip() if xff else (request.remote_addr or 'unknown')
def is_open():
    return db_get('settings',{}).get('open',True)
def get_cfg():
    c = db_get('settings',{})
    return {
        'max_device': c.get('max_votes_per_device',1),
        'max_ip':     c.get('max_votes_per_ip',3),
        'block_dev':  c.get('block_device',True),
        'block_ip':   c.get('block_ip',True),
    }

def check_device(fp_h, ip_h, cfg):
    # ── فحص من الذاكرة أولاً (أسرع وأموثوق) ──
    if cfg['block_dev'] and fp_h:
        cnt = DEVICES_CACHE.get(fp_h,{}).get('count',0)
        if cnt >= cfg['max_device']:
            return False, '⛔ هذا الجهاز شارك مسبقاً. لا يُسمح بأكثر من مشاركة واحدة لكل جهاز.'
    if cfg['block_ip'] and ip_h:
        cnt = IP_CACHE.get(ip_h,{}).get('count',0)
        if cnt >= cfg['max_ip']:
            return False, '⛔ تجاوزت هذه الشبكة الحد المسموح به.'
    return True, ''

def record_device_vote(fp_h, ip_h, reg_h):
    now = datetime.now().strftime('%H:%M:%S')
    # تحديث الذاكرة أولاً
    if fp_h:
        if fp_h not in DEVICES_CACHE:
            DEVICES_CACHE[fp_h] = {'count':0,'first':now}
        DEVICES_CACHE[fp_h]['count'] += 1
        DEVICES_CACHE[fp_h]['last']   = now
    if ip_h:
        if ip_h not in IP_CACHE:
            IP_CACHE[ip_h] = {'count':0,'first':now}
        IP_CACHE[ip_h]['count'] += 1
        IP_CACHE[ip_h]['last']   = now
    # حفظ في DB
    db_set('devices', {'fingerprints':DEVICES_CACHE,'ips':IP_CACHE})

# ══ API ══════════════════════════════════════════════════════════
@app.route('/api/voter/<reg>')
def api_voter(reg):
    if not is_open():
        return jsonify({'ok':False,'error':'الاستطلاع مغلق حالياً'}),403
    fp_h = h(request.args.get('fp',''))
    ip_h = h(get_ip())
    cfg  = get_cfg()
    # ── فحص الجهاز ──
    ok2, err = check_device(fp_h, ip_h, cfg)
    if not ok2:
        return jsonify({'ok':False,'error':err}),403
    voter = find_voter(reg)
    if not voter:
        return jsonify({'ok':False,'error':'الرقم غير موجود في سجل المشاركين'}),404
    # ── فحص التصويت المسبق من الذاكرة أولاً ──
    reg_h = h(reg.strip())
    if reg_h in VOTED_CACHE:
        return jsonify({'ok':False,'error':'هذا الرقم شارك في الاستطلاع مسبقاً'}),403
    return jsonify({'ok':True,'name':voter['name'],'center':voter['center']})

@app.route('/api/candidates')
def api_candidates():
    return jsonify({'ok':True,'lists':CANDIDATES})

@app.route('/api/vote', methods=['POST'])
def api_vote():
    if not is_open():
        return jsonify({'ok':False,'error':'الاستطلاع مغلق'}),403
    data    = request.get_json(silent=True) or {}
    reg     = str(data.get('reg_num','')).strip()
    list_id = data.get('list_id')
    chosen  = data.get('candidates',[])
    fp      = data.get('fingerprint','')
    if not reg or not list_id or not chosen:
        return jsonify({'ok':False,'error':'بيانات ناقصة'}),400
    if not 1<=len(chosen)<=5:
        return jsonify({'ok':False,'error':'اختر من 1 إلى 5'}),400
    voter = find_voter(reg)
    if not voter:
        return jsonify({'ok':False,'error':'رقم غير صالح'}),404
    lst = next((l for l in CANDIDATES if l['id']==list_id),None)
    if not lst:
        return jsonify({'ok':False,'error':'قائمة غير موجودة'}),400
    fp_h  = h(fp)
    ip_h  = h(get_ip())
    reg_h = h(reg)
    cfg   = get_cfg()

    with lock:
        # ── التحقق من الذاكرة أولاً (الأسرع والأموثوق) ──
        if reg_h in VOTED_CACHE:
            return jsonify({'ok':False,'error':'تمت المشاركة بهذا الرقم مسبقاً'}),403
        ok2, err = check_device(fp_h, ip_h, cfg)
        if not ok2:
            return jsonify({'ok':False,'error':err}),403

        # ── تسجيل في الذاكرة فوراً (قبل DB حتى لا يفوت شيء) ──
        VOTED_CACHE.add(reg_h)

        # ── حفظ الأصوات ──
        votes = db_get('votes',{'lists':{},'candidates':{},'total':0})
        votes['lists'][lst['name']]  = votes['lists'].get(lst['name'],0)+1
        votes['total']               = votes.get('total',0)+1
        for c in chosen:
            votes['candidates'][c]   = votes['candidates'].get(c,0)+1
        db_set('votes', votes)

        # ── تسجيل الناخب (hash فقط) ──
        voted = db_get('voted',{})
        voted[reg_h] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        db_set('voted', voted)

        # ── تسجيل الجهاز ──
        record_device_vote(fp_h, ip_h, reg_h)

    return jsonify({'ok':True,'msg':'تم تسجيل رأيك بنجاح'})

ADMIN_PW = lambda: os.environ.get('ADMIN_PASSWORD', 'Tubas@0598652625')

@app.route('/api/admin/results')
def api_results():
    if request.headers.get('X-Admin-Pass') != ADMIN_PW():
        return jsonify({'ok':False,'error':'غير مصرح'}),401
    votes = db_get('votes',{'lists':{},'candidates':{},'total':0})
    tv    = sum(votes['lists'].values()) or 1
    lists_r = sorted([{
        'id':l['id'],'name':l['name'],
        'votes':votes['lists'].get(l['name'],0),
        'pct':round(votes['lists'].get(l['name'],0)/tv*100,1),
        'candidates_count':len(l['candidates'])} for l in CANDIDATES],
        key=lambda x:-x['votes'])
    cands_r = sorted([{
        'name':c['name'],'list':l['name'],'gender':c['gender'],
        'votes':votes['candidates'].get(c['name'],0)}
        for l in CANDIDATES for c in l['candidates']],key=lambda x:-x['votes'])
    fp_s = sorted([{'fp':k[:8]+'...','votes':v['count'],'last':v.get('last','')}
        for k,v in DEVICES_CACHE.items() if v['count']>1],key=lambda x:-x['votes'])
    ip_s = sorted([{'ip':k[:8]+'...','votes':v['count'],'last':v.get('last','')}
        for k,v in IP_CACHE.items()],key=lambda x:-x['votes'])
    cfg  = get_cfg()
    return jsonify({
        'ok':True,
        'total_voters':len(VOTERS_DB),
        'total_voted':len(VOTED_CACHE),
        'participation_pct':round(len(VOTED_CACHE)/max(len(VOTERS_DB),1)*100,1),
        'election_open':is_open(),
        'total_devices':len(DEVICES_CACHE),
        'blocked_attempts':0,
        'security_settings':cfg,
        'suspicious_devices':fp_s[:10],
        'suspicious_ips':ip_s[:10],
        'recent_security_log':[],
        'lists':lists_r,'candidates':cands_r
    })

@app.route('/api/admin/toggle', methods=['POST'])
def api_toggle():
    if request.headers.get('X-Admin-Pass') != ADMIN_PW():
        return jsonify({'ok':False,'error':'غير مصرح'}),401
    cfg = db_get('settings',{})
    cfg['open'] = not cfg.get('open',True)
    db_set('settings', cfg)
    return jsonify({'ok':True,'open':cfg['open']})

@app.route('/api/admin/security', methods=['POST'])
def api_security():
    if request.headers.get('X-Admin-Pass') != ADMIN_PW():
        return jsonify({'ok':False,'error':'غير مصرح'}),401
    data = request.get_json(silent=True) or {}
    cfg  = db_get('settings',{})
    for k in ['max_votes_per_device','max_votes_per_ip']:
        if k in data: cfg[k] = int(data[k])
    for k in ['block_device','block_ip']:
        if k in data: cfg[k] = bool(data[k])
    db_set('settings', cfg)
    return jsonify({'ok':True})

@app.route('/')
def index():
    return send_from_directory('static','index.html')


@app.route('/api/lookup', methods=['POST'])
def api_lookup():
    """البحث عن رقم التسجيل من موقع لجنة الانتخابات"""
    import re
    from bs4 import BeautifulSoup as BS
    data  = request.get_json(silent=True) or {}
    palid = str(data.get('id','')).strip()
    year  = str(data.get('year','')).strip()
    if not palid or not year:
        return jsonify({'ok':False,'error':'أدخل رقم الهوية وسنة الميلاد'}),400
    try:
        import urllib.request as ur, urllib.parse as up
        CEC = 'https://www.elections.ps/tabid/596/language/ar-PS/Default.aspx'
        req1 = ur.Request(CEC, headers={'User-Agent':'Mozilla/5.0'})
        with ur.urlopen(req1, timeout=10) as res:
            page = res.read().decode('utf-8','ignore')
        def gv(name):
            m = re.search(rf'name="{re.escape(name)}"[^>]*value="([^"]*)"', page)
            return m.group(1) if m else ''
        payload = up.urlencode({
            '__VIEWSTATE': gv('__VIEWSTATE'),
            '__VIEWSTATEGENERATOR': gv('__VIEWSTATEGENERATOR'),
            '__EVENTVALIDATION': gv('__EVENTVALIDATION'),
            'dnn$ctr4525$View$PalID': palid,
            'dnn$ctr4525$View$YearOfBirth': year,
            'dnn$ctr4525$View$btnSearch': 'بحث',
            'g-recaptcha-response': '',
        }).encode('utf-8')
        req2 = ur.Request(CEC, data=payload, headers={
            'User-Agent':'Mozilla/5.0','Content-Type':'application/x-www-form-urlencoded',
            'Referer': CEC})
        with ur.urlopen(req2, timeout=12) as res2:
            page2 = res2.read().decode('utf-8','ignore')
        m = re.search(r'lblRegNum[^>]*>([\d]+)<', page2)
        if m:
            reg = m.group(1)
            # استخراج الاسم أيضاً
            nm = re.search(r'lblName[^>]*>([^<]+)<', page2)
            name = nm.group(1).strip() if nm else ''
            return jsonify({'ok':True,'reg':reg,'name':name})
        if 'لا يوجد' in page2 or 'غير مسجل' in page2:
            return jsonify({'ok':False,'error':'لم يتم العثور على بيانات لهذا الرقم'})
        return jsonify({'ok':False,'error':'تعذر استرجاع البيانات، حاول مرة أخرى'})
    except Exception as e:
        return jsonify({'ok':False,'error':f'خطأ في الاتصال: {str(e)[:60]}'}),500


load_data()
init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT',5050))
    mode = "PostgreSQL ☁️" if DATABASE_URL else "Memory+JSON 📁"
    print(f'Voters:{len(VOTERS_DB):,} | Cache:{len(VOTED_CACHE)} | DB:{mode}')
    app.run(host='0.0.0.0', port=port)
