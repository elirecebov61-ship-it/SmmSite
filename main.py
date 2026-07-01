import hashlib, os, secrets
from datetime import datetime
import httpx, psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File, status
from fastapi.responses import RedirectResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware

# ===== CONFIG =====
DATABASE_URL   = os.getenv("DATABASE_URL", "")
SESSION_SECRET = os.getenv("SESSION_SECRET") or os.getenv("ADMIN_KEY") or secrets.token_hex(32)
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "")
ADMIN_KEY         = os.getenv("ADMIN_KEY", "admin123")
CARD_NUMBER = os.getenv("CARD_NUMBER", "5522099369926134")
CARD_HOLDER = os.getenv("CARD_HOLDER", "")
CARD_BANK   = os.getenv("CARD_BANK", "ABB")
BRAND = "BoostPanel"

app = FastAPI(title=BRAND)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

oauth = OAuth()
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)
def get_conn(): return db_pool.getconn()
def put_conn(c): db_pool.putconn(c)

# ===== DB =====
def init_db():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS panel_users (
            email TEXT PRIMARY KEY, name TEXT, password TEXT,
            balance NUMERIC DEFAULT 0, total_spent NUMERIC DEFAULT 0,
            total_orders INTEGER DEFAULT 0, created_at TEXT)""")
        for col in ["password TEXT","balance NUMERIC DEFAULT 0","total_spent NUMERIC DEFAULT 0",
                    "total_orders INTEGER DEFAULT 0","created_at TEXT"]:
            try: c.execute(f"ALTER TABLE panel_users ADD COLUMN IF NOT EXISTS {col}")
            except: conn.rollback()
        c.execute("""CREATE TABLE IF NOT EXISTS products (
            product_id SERIAL PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
            price NUMERIC NOT NULL, min_qty INTEGER DEFAULT 10,
            max_qty INTEGER DEFAULT 100000, stock INTEGER DEFAULT 999, description TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS orders (
            order_id SERIAL PRIMARY KEY, customer_email TEXT, product_id INTEGER,
            quantity INTEGER NOT NULL, profile_link TEXT, price NUMERIC,
            start_count INTEGER DEFAULT 0, remains INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Gözləmədə', order_date TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS topups (
            topup_id SERIAL PRIMARY KEY, customer_email TEXT,
            amount_sent TEXT, status TEXT DEFAULT 'Yoxlanılır', topup_date TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tickets (
            ticket_id SERIAL PRIMARY KEY, customer_email TEXT, subject TEXT,
            message TEXT, status TEXT DEFAULT 'Açıq', created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ticket_replies (
            reply_id SERIAL PRIMARY KEY, ticket_id INTEGER, sender TEXT,
            message TEXT, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS announcements (
            id SERIAL PRIMARY KEY, title TEXT, content TEXT, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS faqs (
            id SERIAL PRIMARY KEY, question TEXT, answer TEXT, sort_order INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS reviews (
            id SERIAL PRIMARY KEY, name TEXT, content TEXT, rating INTEGER DEFAULT 5)""")
        conn.commit()
        # Seed FAQs if empty
        c.execute("SELECT COUNT(*) FROM faqs")
        if c.fetchone()[0] == 0:
            faqs = [
                ("SMM paneli nədir??","SMM paneli sosial media hesablarınızı böyütmək üçün izləyici, bəyəni, baxış kimi xidmətlər təqdim edən onlayn platformadır."),
                ("Panelimizdə hansı növ SMM xidmətlərini tapa bilərəm??","Instagram, TikTok, YouTube, Telegram, Facebook və digər platformalar üçün xidmətlər mövcuddur."),
                ("Bu paneldən SMM xidmətlərini almaq təhlükəsizdirmi??","Bəli, bütün xidmətlər etibarlı provayderlərdən gəlir. Hesabınız heç bir risk altında deyil."),
                ("Kütləvi sifarişlər (Mass orders) nədir??","Eyni anda bir neçə link üçün eyni xidməti sifariş etməyə imkan verir."),
                ("Drip-feed funksiyası necə işləyir?","Drip-feed xidmət miqdarını müəyyən müddət ərzində tədricən çatdırmağa imkan verir."),
            ]
            for q,a in faqs:
                c.execute("INSERT INTO faqs (question,answer) VALUES (%s,%s)",(q,a))
        c.execute("SELECT COUNT(*) FROM reviews")
        if c.fetchone()[0] == 0:
            revs = [
                ("Rüfət Ayxanov","Biznesim üçün ən yaxşı SMM paneli. Sifarişlər anında icra olunur.",5),
                ("Aysel Məmmədova","Qiymətlər çox münasibdir. Bütün funksiyalar mükəmməl işləyir.",5),
                ("Nicat Hüseynov","Çox tez çatdırılır. Hər zaman istifadə edirəm, tövsiyə edirəm!",5),
            ]
            for n,cont,r in revs:
                c.execute("INSERT INTO reviews (name,content,rating) VALUES (%s,%s,%s)",(n,cont,r))
        conn.commit()
    finally: put_conn(conn)

@app.on_event("startup")
def startup(): init_db()

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()
def current_user(request): return request.session.get("user")
def get_balance(email):
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT balance,total_spent,total_orders FROM panel_users WHERE email=%s",(email,))
        return c.fetchone() or {"balance":0,"total_spent":0,"total_orders":0}
    finally: put_conn(conn)
def get_user_db(email):
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM panel_users WHERE email=%s",(email,))
        return c.fetchone()
    finally: put_conn(conn)

# ===== CSS =====
CSS = """
:root{--or:#F97316;--ord:#EA580C;--orlt:#FFF7ED;--orltt:#FFEDD5;--tx:#1F2937;--mu:#6B7280;--sf:#fff;--bg:#FFF7ED;--ln:#FED7AA;--ra:12px;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#f9fafb;color:var(--tx);font-family:'Inter',sans-serif;-webkit-font-smoothing:antialiased;}
a{color:inherit;text-decoration:none;}
.wrap{max-width:1100px;margin:0 auto;padding:0 18px;}

/* TOPBAR */
.topbar{background:var(--sf);border-bottom:1px solid #FED7AA;position:sticky;top:0;z-index:50;box-shadow:0 1px 8px rgba(249,115,22,.08);}
.topbar-inner{display:flex;justify-content:space-between;align-items:center;height:62px;}
.brand{display:flex;align-items:center;gap:8px;font-weight:800;font-size:20px;color:var(--or);}
.brand svg{width:28px;height:28px;}
.hamburger{background:none;border:none;cursor:pointer;padding:8px;color:var(--or);}
.hamburger span{display:block;width:24px;height:2px;background:var(--or);margin:5px 0;border-radius:2px;transition:.3s;}
.topuser{display:flex;align-items:center;gap:12px;font-size:13px;color:var(--mu);}
.bal-badge{background:var(--orltt);color:var(--ord);font-weight:700;padding:5px 12px;border-radius:999px;font-size:13px;}

/* SIDEBAR */
.sb-ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:48;}
.sb-ov.open{display:block;}
.sidebar{position:fixed;top:0;right:-300px;width:280px;height:100%;background:var(--sf);z-index:49;transition:right .28s cubic-bezier(.4,0,.2,1);box-shadow:-4px 0 20px rgba(0,0,0,.12);display:flex;flex-direction:column;}
.sidebar.open{right:0;}
.sb-head{background:linear-gradient(135deg,var(--or),var(--ord));padding:24px 20px 18px;color:#fff;}
.sb-head .sb-name{font-weight:700;font-size:15px;}
.sb-head .sb-email{font-size:12px;opacity:.85;margin-top:2px;}
.sb-head .sb-bal{margin-top:10px;font-size:13px;background:rgba(255,255,255,.2);display:inline-block;padding:4px 12px;border-radius:999px;}
.sb-nav{flex:1;padding:8px 0;}
.sb-nav a{display:flex;align-items:center;gap:12px;padding:13px 20px;font-size:14px;color:var(--tx);transition:background .15s;}
.sb-nav a:hover,.sb-nav a.active{background:var(--orltt);color:var(--or);}
.sb-nav a .icon{font-size:17px;width:22px;text-align:center;}
.sb-div{height:1px;background:#FED7AA;margin:4px 0;}
.sb-footer{padding:16px 20px;font-size:12px;color:var(--mu);}

/* LANDING */
.hero-land{background:linear-gradient(160deg,#fff 0%,var(--orltt) 100%);padding:50px 0 0;overflow:hidden;}
.hero-land h1{font-size:clamp(22px,5vw,36px);font-weight:800;color:var(--tx);line-height:1.2;margin-bottom:10px;}
.hero-land p{color:var(--mu);font-size:15px;max-width:480px;line-height:1.6;}
.hero-login-card{background:var(--sf);border-radius:16px;padding:28px;box-shadow:0 4px 24px rgba(249,115,22,.12);max-width:380px;width:100%;}
.hero-grid{display:grid;grid-template-columns:1fr 1fr;gap:40px;align-items:center;padding-bottom:60px;}
@media(max-width:700px){.hero-grid{grid-template-columns:1fr;}.hero-land h1{font-size:22px;}}
.wave-wrap{margin-top:0;}
.wave-wrap svg{display:block;width:100%;}

/* AUTH CARD */
.auth-tabs{display:flex;border:1px solid var(--ln);border-radius:8px;overflow:hidden;margin-bottom:20px;}
.auth-tabs a{flex:1;text-align:center;padding:10px;font-size:14px;font-weight:600;color:var(--mu);background:var(--sf);}
.auth-tabs a.active{background:var(--or);color:#fff;}
.field{display:flex;flex-direction:column;gap:5px;font-size:13px;color:var(--mu);margin-bottom:12px;}
.field label{font-weight:500;}
.field input,.field select,.field textarea{background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;padding:11px 13px;color:var(--tx);font-family:'Inter',sans-serif;font-size:14px;width:100%;}
.field input:focus,.field select:focus,.field textarea:focus{outline:2px solid var(--or);border-color:var(--or);}
.field textarea{resize:vertical;min-height:100px;}
.btn-or{width:100%;background:linear-gradient(90deg,var(--or),var(--ord));color:#fff;border:none;border-radius:8px;padding:13px;font-weight:700;font-size:15px;cursor:pointer;font-family:'Inter',sans-serif;letter-spacing:.3px;}
.btn-or:hover{filter:brightness(1.05);}
.btn-google{display:flex;align-items:center;justify-content:center;gap:10px;background:var(--sf);border:2px solid var(--or);color:var(--or);font-weight:700;padding:12px;border-radius:8px;font-size:14px;width:100%;cursor:pointer;font-family:'Inter',sans-serif;margin-top:10px;}
.divider-or{text-align:center;color:var(--mu);font-size:12px;margin:12px 0;position:relative;}
.divider-or::before,.divider-or::after{content:'';position:absolute;top:50%;width:44%;height:1px;background:var(--ln);}
.divider-or::before{left:0;}.divider-or::after{right:0;}
.err-box{background:#FEF2F2;color:#B91C1C;border:1px solid #FCA5A5;border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:12px;}
.ok-box{background:#ECFDF5;border:1px solid #6EE7B7;color:#065F46;border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:12px;}
.link-or{color:var(--or);font-weight:600;}
.text-center{text-align:center;}
.mt-10{margin-top:10px;}
.mt-20{margin-top:20px;}

/* FEATURES SECTION */
.features-sec{padding:48px 0;}
.sec-title{text-align:center;font-size:22px;font-weight:800;margin-bottom:6px;}
.sec-sub{text-align:center;color:var(--mu);font-size:14px;margin-bottom:32px;}
.feat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px;}
.feat-card{background:var(--sf);border:1px solid var(--ln);border-radius:var(--ra);padding:20px;text-align:center;}
.feat-icon{width:52px;height:52px;background:var(--orltt);border-radius:10px;display:flex;align-items:center;justify-content:center;margin:0 auto 12px;font-size:22px;}
.feat-card h3{font-size:15px;font-weight:700;margin-bottom:6px;}
.feat-card p{font-size:13px;color:var(--mu);line-height:1.5;}

/* HOW IT WORKS */
.how-sec{background:linear-gradient(135deg,var(--or),var(--ord));padding:52px 0;color:#fff;position:relative;overflow:hidden;}
.how-sec .sec-title,.how-sec .sec-sub{color:#fff;}
.how-sec .sec-sub{opacity:.85;}
.steps-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:20px;position:relative;z-index:1;}
@media(max-width:700px){.steps-grid{grid-template-columns:1fr 1fr;}}
.step-item{text-align:center;}
.step-num{width:52px;height:52px;background:rgba(255,255,255,.25);border-radius:50%;display:flex;align-items:center;justify-content:center;margin:0 auto 12px;font-size:22px;font-weight:800;border:2px solid rgba(255,255,255,.5);}
.step-item h3{font-size:14px;font-weight:700;margin-bottom:6px;}
.step-item p{font-size:12px;opacity:.85;line-height:1.5;}
.how-blob{position:absolute;top:-60px;right:-80px;width:300px;height:300px;background:rgba(255,255,255,.08);border-radius:50%;}

/* REVIEWS */
.reviews-sec{background:linear-gradient(135deg,#FF9A3C,var(--or));padding:48px 0;overflow:hidden;}
.reviews-sec .sec-title,.reviews-sec .sec-sub{color:#fff;}
.reviews-sec .sec-sub{opacity:.85;}
.review-slider{position:relative;overflow:hidden;}
.review-track{display:flex;transition:transform .4s ease;gap:20px;}
.review-card{background:var(--sf);border-radius:var(--ra);padding:22px;min-width:320px;flex-shrink:0;}
.rv-avatar{width:48px;height:48px;background:var(--orltt);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:20px;margin-bottom:10px;}
.rv-name{font-weight:700;font-size:15px;margin-bottom:4px;}
.rv-stars{color:#F59E0B;font-size:14px;margin-bottom:8px;}
.rv-text{font-size:13px;color:var(--mu);line-height:1.5;}
.slider-btns{display:flex;gap:10px;justify-content:center;margin-top:20px;}
.slider-btn{width:40px;height:40px;background:rgba(255,255,255,.25);border:none;border-radius:50%;color:#fff;font-size:16px;cursor:pointer;}

/* FAQ */
.faq-sec{padding:48px 0;}
.faq-item{border:1px solid var(--ln);border-radius:8px;margin-bottom:8px;overflow:hidden;}
.faq-q{display:flex;justify-content:space-between;align-items:center;padding:16px 18px;cursor:pointer;font-weight:600;font-size:14px;background:var(--sf);}
.faq-q:hover{background:var(--orltt);}
.faq-q .arrow{transition:transform .3s;color:var(--or);font-size:18px;}
.faq-a{display:none;padding:14px 18px;font-size:13px;color:var(--mu);line-height:1.6;border-top:1px solid var(--ln);background:#fffbf7;}
.faq-item.open .faq-a{display:block;}
.faq-item.open .arrow{transform:rotate(45deg);}

/* DASHBOARD */
.dash-page{padding:24px 0 60px;}
.page-title{font-size:22px;font-weight:800;margin-bottom:20px;color:var(--tx);}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px;margin-bottom:28px;}
.stat-card{background:var(--sf);border:1px solid var(--ln);border-radius:var(--ra);padding:18px;display:flex;align-items:center;gap:14px;}
.stat-icon{width:48px;height:48px;background:var(--orltt);border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0;}
.stat-label{font-size:12px;color:var(--mu);}
.stat-val{font-size:20px;font-weight:800;color:var(--or);}

/* ORDER FORM */
.order-panel{background:var(--sf);border:1px solid var(--ln);border-radius:var(--ra);padding:24px;margin-bottom:24px;}
.search-bar{display:flex;gap:10px;margin-bottom:16px;}
.search-bar input{flex:1;background:var(--orltt);border:1px solid var(--ln);border-radius:8px;padding:11px 14px;font-size:14px;font-family:'Inter',sans-serif;}
.search-bar input:focus{outline:2px solid var(--or);}
.price-display{background:linear-gradient(90deg,var(--or),var(--ord));color:#fff;border-radius:8px;padding:14px 18px;display:flex;align-items:center;gap:12px;margin-bottom:14px;font-size:18px;font-weight:700;}
.price-display .label{font-size:13px;opacity:.85;font-weight:400;}
.info-box{background:var(--orltt);border:1px solid var(--ln);border-radius:8px;padding:14px 16px;margin-bottom:16px;}
.info-box p{font-size:13px;color:var(--ord);line-height:1.6;display:flex;align-items:flex-start;gap:8px;margin-bottom:6px;}
.info-box p:last-child{margin-bottom:0;}
.hint-text{font-size:12px;color:var(--mu);margin-top:4px;}

/* ORDERS TABLE */
.filter-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px;}
.ftab{background:var(--sf);border:1px solid var(--ln);border-radius:999px;padding:7px 14px;font-size:13px;font-weight:500;cursor:pointer;color:var(--tx);}
.ftab.active{background:var(--or);color:#fff;border-color:var(--or);}
.table-card{background:var(--sf);border:1px solid var(--ln);border-radius:var(--ra);overflow:hidden;}
.search-orders{display:flex;gap:10px;padding:14px;border-bottom:1px solid var(--ln);}
.search-orders input{flex:1;background:var(--orltt);border:1px solid var(--ln);border-radius:8px;padding:9px 13px;font-size:13px;font-family:'Inter',sans-serif;}
.tbl-head{background:linear-gradient(90deg,var(--or),var(--ord));color:#fff;}
table.ot{width:100%;border-collapse:collapse;font-size:13px;}
table.ot th{padding:12px 12px;text-align:left;font-weight:600;white-space:nowrap;}
table.ot td{padding:11px 12px;border-bottom:1px solid #FEE2C8;}
table.ot tr:last-child td{border-bottom:none;}
table.ot tr:hover td{background:#FFF7ED;}
.pill{padding:3px 10px;border-radius:999px;font-size:11px;font-weight:600;}
.pill-wait{background:#FEF3C7;color:#92400E;}
.pill-done{background:#D1FAE5;color:#065F46;}
.pill-run{background:#DBEAFE;color:#1E40AF;}
.pill-cancel{background:#FEE2E2;color:#B91C1C;}
.empty-tbl{padding:48px;text-align:center;color:var(--mu);font-size:14px;}

/* BALANCE PAGE */
.bal-page{max-width:560px;padding:24px 0 64px;}
.card-3d{background:linear-gradient(135deg,#1E3A5F,#0F172A);border-radius:18px;padding:26px;color:#fff;margin-bottom:16px;position:relative;overflow:hidden;}
.card-3d::before{content:'';position:absolute;top:-40px;right:-40px;width:180px;height:180px;background:rgba(255,255,255,.06);border-radius:50%;}
.card-bank-name{font-size:14px;opacity:.8;margin-bottom:28px;display:flex;align-items:center;gap:8px;}
.card-number-disp{font-family:'IBM Plex Mono',monospace;font-size:22px;letter-spacing:.1em;margin-bottom:18px;}
.card-holder-name{font-size:13px;opacity:.8;}
.copy-btn{position:absolute;top:20px;right:20px;background:rgba(255,255,255,.18);border:none;color:#fff;padding:6px 14px;border-radius:6px;font-size:12px;cursor:pointer;font-family:'Inter',sans-serif;}
.copy-btn:hover{background:rgba(255,255,255,.28);}
.note-list{background:#FFFBEB;border:1px solid #FDE68A;border-radius:10px;padding:14px 16px;margin:16px 0;}
.note-list p{font-size:13px;color:#92400E;line-height:1.6;display:flex;align-items:flex-start;gap:8px;margin-bottom:4px;}
.note-list p:last-child{margin-bottom:0;}

/* TICKETS */
.ticket-form{background:var(--sf);border:1px solid var(--ln);border-radius:var(--ra);padding:22px;margin-bottom:24px;}
.ticket-tbl{background:var(--sf);border:1px solid var(--ln);border-radius:var(--ra);overflow:hidden;}
.ticket-tbl-head{background:linear-gradient(90deg,var(--or),var(--ord));color:#fff;display:grid;grid-template-columns:60px 1fr 100px 140px;padding:12px 16px;font-size:13px;font-weight:600;}
.ticket-row{display:grid;grid-template-columns:60px 1fr 100px 140px;padding:12px 16px;border-bottom:1px solid #FEE2C8;font-size:13px;align-items:center;}
.ticket-row:last-child{border-bottom:none;}
.ticket-row:hover{background:var(--orltt);}

/* ANNOUNCEMENT */
.ann-card{background:var(--sf);border:1px solid var(--ln);border-radius:var(--ra);overflow:hidden;margin-bottom:14px;}
.ann-head{background:linear-gradient(90deg,var(--or),var(--ord));color:#fff;padding:10px 16px;font-size:13px;font-weight:600;}
.ann-body{padding:14px 16px;font-size:14px;line-height:1.6;white-space:pre-wrap;}

/* ADMIN */
.admin-wrap{padding:28px 0 64px;max-width:700px;}
.admin-sec{background:var(--sf);border:1px solid var(--ln);border-radius:var(--ra);padding:22px;margin-bottom:20px;}
.admin-sec h3{font-size:16px;font-weight:700;margin-bottom:16px;color:var(--or);}

/* FOOTER */
.footer{background:linear-gradient(90deg,var(--or),var(--ord));color:#fff;padding:18px 0;text-align:center;font-size:13px;}
.footer a{color:rgba(255,255,255,.8);}

@media(max-width:640px){
  .stat-grid{grid-template-columns:1fr 1fr;}
  .filter-tabs{overflow-x:auto;flex-wrap:nowrap;padding-bottom:4px;}
  table.ot{min-width:600px;}
  .table-card{overflow-x:auto;}
  .steps-grid{grid-template-columns:1fr 1fr;}
  .ticket-tbl-head,.ticket-row{grid-template-columns:50px 1fr 80px;}
}
"""

JS = """
function toggleSidebar(f){
  var sb=document.getElementById('sb'),ov=document.getElementById('sb-ov');
  var op=f!==undefined?f:!sb.classList.contains('open');
  sb.classList.toggle('open',op);ov.classList.toggle('open',op);
}
function faqToggle(el){
  var item=el.parentElement;
  document.querySelectorAll('.faq-item.open').forEach(function(i){if(i!==item)i.classList.remove('open');});
  item.classList.toggle('open');
}
var sliderIdx=0;
function slideMove(dir){
  var track=document.getElementById('rv-track');
  if(!track)return;
  var cards=track.querySelectorAll('.review-card');
  var max=cards.length-1;
  sliderIdx=Math.max(0,Math.min(max,sliderIdx+dir));
  var w=cards[0].offsetWidth+20;
  track.style.transform='translateX(-'+(sliderIdx*w)+'px)';
}
function copyCard(){
  var n=document.getElementById('card-num-val');
  if(!n)return;
  navigator.clipboard.writeText(n.innerText.replace(/\\s/g,'')).then(function(){
    var btn=document.getElementById('copy-btn-el');
    if(btn){btn.innerText='Kopyalandı ✓';setTimeout(function(){btn.innerText='Kopyala';},2000);}
  });
}
function filterOrders(st){
  document.querySelectorAll('.ftab').forEach(function(t){t.classList.remove('active');});
  event.target.classList.add('active');
  var rows=document.querySelectorAll('.order-row');
  rows.forEach(function(r){
    if(st==='all'||r.dataset.status===st)r.style.display='';
    else r.style.display='none';
  });
}
function searchOrders(){
  var q=document.getElementById('order-search').value.toLowerCase();
  document.querySelectorAll('.order-row').forEach(function(r){
    r.style.display=r.innerText.toLowerCase().includes(q)?'':'none';
  });
}
document.addEventListener('DOMContentLoaded',function(){
  var si=document.getElementById('svc-select');
  var pi=document.getElementById('price-val');
  var qi=document.getElementById('qty-input');
  var minEl=document.getElementById('min-qty');
  var maxEl=document.getElementById('max-qty');
  function updatePrice(){
    if(!si||!pi)return;
    var opt=si.options[si.selectedIndex];
    var price=parseFloat(opt.dataset.price||0);
    var qty=parseInt(qi?qi.value:1)||1;
    pi.innerText=(price*qty).toFixed(4)+' ₼';
    if(opt.dataset.min&&minEl)minEl.innerText='Min: '+opt.dataset.min;
    if(opt.dataset.max&&maxEl)maxEl.innerText=' - Max: '+opt.dataset.max;
  }
  if(si){si.addEventListener('change',updatePrice);}
  if(qi){qi.addEventListener('input',updatePrice);}
  updatePrice();
});
"""

GOOGLE_SVG = """<svg width="17" height="17" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.9c1.7-1.57 2.7-3.88 2.7-6.62z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.9-2.26c-.8.54-1.84.86-3.06.86-2.35 0-4.34-1.59-5.05-3.72H.96v2.33A9 9 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.95 10.7A5.4 5.4 0 0 1 3.66 9c0-.59.1-1.17.29-1.7V4.97H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.03l2.99-2.33z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.5.46 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.97l2.99 2.33C4.66 5.17 6.65 3.58 9 3.58z"/></svg>"""

def HEAD(title):
    return f"""<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — {BRAND}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
<style>{CSS}</style>"""

def SCRIPTS():
    return f"<script>{JS}</script>"

LOGO_SVG = """<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
<circle cx="16" cy="16" r="16" fill="#F97316"/>
<path d="M8 20L14 12L18 17L22 13" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="22" cy="13" r="2" fill="white"/>
</svg>"""

def topbar_auth(user, bal_info):
    bal = float(bal_info["balance"]) if bal_info else 0
    return f"""<header class="topbar">
<div class="wrap topbar-inner">
  <a class="brand" href="/">{LOGO_SVG}{BRAND}</a>
  <div class="topuser">
    <span class="bal-badge">💰 {bal:.2f} ₼</span>
    <button class="hamburger" onclick="toggleSidebar()">
      <span></span><span></span><span></span>
    </button>
  </div>
</div></header>"""

def sidebar_html(user, bal_info, active=""):
    bal = float(bal_info["balance"]) if bal_info else 0
    name = user.get("name","İstifadəçi")
    email = user.get("email","")
    links = [
        ("🛒","Yeni Sifariş","/","new"),
        ("📦","Sifarişlərim","/orders","orders"),
        ("💳","Balans artır","/balance","balance"),
        ("🎫","Dəstək","/support","support"),
        ("📢","Xəbərlər","/news","news"),
    ]
    nav = ""
    for ic,lbl,href,key in links:
        cls = "active" if active==key else ""
        nav += f'<a href="{href}" class="{cls}"><span class="icon">{ic}</span>{lbl}</a>'
    return f"""
<div class="sb-ov" id="sb-ov" onclick="toggleSidebar(false)"></div>
<div class="sidebar" id="sb">
  <div class="sb-head">
    <div class="sb-name">👤 {name}</div>
    <div class="sb-email">{email}</div>
    <div class="sb-bal">💰 {bal:.2f} ₼</div>
  </div>
  <nav class="sb-nav">
    {nav}
    <div class="sb-div"></div>
    <a href="/logout"><span class="icon">🚪</span>Çıxış</a>
  </nav>
  <div class="sb-footer">© 2026 {BRAND}</div>
</div>"""

def footer():
    return f'<footer class="footer"><p>© 2026 {BRAND} — Bütün hüquqlar qorunur.</p></footer>'

def page_auth(title, body, user, bal_info, active=""):
    return f"""<!DOCTYPE html><html lang="az">
<head>{HEAD(title)}</head>
<body>
{topbar_auth(user, bal_info)}
{sidebar_html(user, bal_info, active)}
{body}
{footer()}
{SCRIPTS()}
</body></html>"""

# ===== LANDING PAGE =====
def render_landing(err="", reg_err="", reg_ok=""):
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM faqs ORDER BY sort_order,id LIMIT 5")
        faqs=c.fetchall()
        c.execute("SELECT * FROM reviews ORDER BY id")
        revs=c.fetchall()
    finally: put_conn(conn)

    err_html=f'<div class="err-box">{err}</div>' if err else ""
    reg_err_html=f'<div class="err-box">{reg_err}</div>' if reg_err else ""
    reg_ok_html=f'<div class="ok-box">{reg_ok}</div>' if reg_ok else ""
    google_btn=f'<a href="/login/google" class="btn-google">{GOOGLE_SVG} Google ilə daxil ol</a>' if GOOGLE_CLIENT_ID else ""

    login_form=f"""<div class="hero-login-card">
  <div class="auth-tabs">
    <a href="/" class="active" id="tab-login">Giriş</a>
    <a href="/register" id="tab-reg">Qeydiyyat</a>
  </div>
  {err_html}
  <form method="post" action="/login">
    <div class="field"><label>👤 İSTİFADƏÇİ ADI / E-POÇT:</label>
      <input type="text" name="email" placeholder="Adınızı daxil edin..." required></div>
    <div class="field"><label>🔒 ŞİFRƏ:</label>
      <input type="password" name="password" placeholder="Şifrənizi daxil edin..." required></div>
    <button class="btn-or" type="submit">🔑 DAXİL OL</button>
  </form>
  <p class="text-center mt-10" style="font-size:13px;color:var(--mu);">Hesabınız yoxdur? <a href="/register" class="link-or">🚀 QEYDİYYATDAN KEÇ!</a></p>
  {f'<div class="divider-or">ya da</div>{google_btn}' if google_btn else ""}
</div>"""

    feat_items=[
        ("⭐","Yüksək keyfiyyət","Möhtəşəm qiymətlərlə ala SMM xidmətlərindən həzz alın!"),
        ("💳","Ödəniş Üsulları","Təqdim etdiyimiz hər hansı bir ödəniş üsulu ilə balansınızı artıra bilərsiniz."),
        ("💰","Ucuz xidmətlər","Xidmətlərimizin hər zaman sərfəli olduğundan əmin oluruq."),
        ("⚡","Super sürətli çatdırılma","Sifarişlərinizin sürətlə çatdırılacağına əmin ola bilərsiniz."),
    ]
    feats="".join(f'<div class="feat-card"><div class="feat-icon">{ic}</div><h3>{t}</h3><p>{d}</p></div>' for ic,t,d in feat_items)

    steps=[
        ("1","Qeydiyyat və Giriş","Qeydiyyatdan keçərək başlayın və sonra hesabınıza daxil olun."),
        ("2","Balans artırın","İstədiyiniz ödəmə üsulunu seçərək hesabınıza vəsait yükləyin."),
        ("3","Sifariş verin","Biznesinizin daha da populyarlaşması üçün lazım olan xidmətləri seçin."),
        ("4","Möhtəşəm nəticələr","Sifarişiniz hazır olduqda sizi xəbərdar edəcəyik."),
    ]
    steps_html="".join(f'<div class="step-item"><div class="step-num">{n}</div><h3>{t}</h3><p>{d}</p></div>' for n,t,d in steps)

    rv_cards="".join(f"""<div class="review-card">
<div class="rv-avatar">👤</div>
<div class="rv-name">{r["name"]}</div>
<div class="rv-stars">{"⭐"*int(r["rating"])}</div>
<div class="rv-text">{r["content"]}</div>
</div>""" for r in revs)

    faq_html="".join(f"""<div class="faq-item">
<div class="faq-q" onclick="faqToggle(this)">{f["question"]}<span class="arrow">+</span></div>
<div class="faq-a">{f["answer"]}</div>
</div>""" for f in faqs)

    return f"""<!DOCTYPE html><html lang="az">
<head>{HEAD("Xoş Gəlmisiniz")}</head>
<body>
<header class="topbar">
<div class="wrap topbar-inner">
  <a class="brand" href="/">{LOGO_SVG}{BRAND}</a>
</div></header>

<section class="hero-land">
<div class="wrap">
<div class="hero-grid">
  <div>
    <h1>Azərbaycanın Ən Yaxşı<br>SMM Panelinə Xoş Gəlmisiniz</h1>
    <p style="margin:14px 0 0;">Panelimizdən necə istifadə edəcəyinizi öyrənmək üçün bu 4 asan addımı izləyin.</p>
  </div>
  {login_form}
</div>
</div>
<div class="wave-wrap">
<svg viewBox="0 0 1440 60" preserveAspectRatio="none" style="height:60px;">
<path d="M0,40 C360,0 1080,80 1440,20 L1440,60 L0,60 Z" fill="#f9fafb"/>
</svg>
</div>
</section>

<section class="features-sec">
<div class="wrap">
  <h2 class="sec-title">Niyə SMM Xidmətlərini Bizdən Sifariş Etməlisiniz?</h2>
  <p class="sec-sub">Panelimizin ən yaxşı SMM həlli olduğunu özünüz görün.</p>
  <div class="feat-grid">{feats}</div>
</div>
</section>

<section class="how-sec">
<div class="how-blob"></div>
<div class="wrap">
  <h2 class="sec-title">Necə işləyir</h2>
  <p class="sec-sub">Panelimizdən necə istifadə edəcəyinizi öyrənmək üçün bu 4 asan addımı izləyin.</p>
  <div class="steps-grid" style="margin-top:32px;">{steps_html}</div>
</div>
</section>

<section class="reviews-sec">
<div class="wrap">
  <h2 class="sec-title">Müştəri Rəyləri</h2>
  <p class="sec-sub">Panelimizin biznesinizə təsirini kəşf edin. Müştəri rəylərimizi mütləq oxuyun!!</p>
  <div class="review-slider" style="margin-top:24px;">
    <div class="review-track" id="rv-track">{rv_cards}</div>
  </div>
  <div class="slider-btns">
    <button class="slider-btn" onclick="slideMove(-1)">‹</button>
    <button class="slider-btn" onclick="slideMove(1)">›</button>
  </div>
</div>
</section>

<section class="faq-sec">
<div class="wrap">
  <h2 class="sec-title">Tez-tez verilən suallar ( FAQ )</h2>
  <p class="sec-sub">SMM panelləri haqqında ən populyar sualları seçdik və onları cavablandırdıq</p>
  <div style="margin-top:24px;">{faq_html}</div>
</div>
</section>

{footer()}
{SCRIPTS()}
</body></html>"""

def render_register(err="",ok=""):
    err_html=f'<div class="err-box">{err}</div>' if err else ""
    ok_html=f'<div class="ok-box">{ok}</div>' if ok else ""
    return f"""<!DOCTYPE html><html lang="az">
<head>{HEAD("Qeydiyyat")}</head>
<body>
<header class="topbar"><div class="wrap topbar-inner">
  <a class="brand" href="/">{LOGO_SVG}{BRAND}</a>
</div></header>
<div style="padding:60px 0;display:flex;justify-content:center;">
<div class="hero-login-card">
  <div class="auth-tabs"><a href="/">Giriş</a><a href="/register" class="active">Qeydiyyat</a></div>
  {err_html}{ok_html}
  <form method="post" action="/register">
    <div class="field"><label>👤 Ad Soyad</label><input type="text" name="name" placeholder="Adınız Soyadınız" required></div>
    <div class="field"><label>📧 E-poçt</label><input type="email" name="email" placeholder="sizin@gmail.com" required></div>
    <div class="field"><label>🔒 Şifrə</label><input type="password" name="password" placeholder="Ən azı 6 simvol" minlength="6" required></div>
    <div class="field"><label>🔒 Şifrəni təsdiqlə</label><input type="password" name="password2" placeholder="••••••••" required></div>
    <button class="btn-or" type="submit">🚀 QEYDİYYATDAN KEÇ</button>
  </form>
  <p class="text-center mt-10" style="font-size:13px;color:var(--mu);">Artıq hesabınız var? <a href="/" class="link-or">Daxil ol</a></p>
</div>
</div>
{footer()}{SCRIPTS()}
</body></html>"""

def _build_prods_js(categories):
    parts = []
    for cat, prods in categories.items():
        cat_esc = cat.replace('"', '\\"')
        items = []
        for p in prods:
            name_esc = str(p["name"]).replace('"', '\\"')
            items.append('{' + f'id:{p["product_id"]},name:"{name_esc}",price:{p["price"]},min:{p["min_qty"]},max:{p["max_qty"]}' + '}')
        parts.append(f'allProds["{cat_esc}"]=[{",".join(items)}];')
    return "\n".join(parts)

def render_dashboard(user, bal_info, categories, all_products, ann_list):
    name=user.get("name","İstifadəçi")
    bal=float(bal_info["balance"])
    spent=float(bal_info.get("total_spent",0))
    total_ord=int(bal_info.get("total_orders",0))

    stats=f"""<div class="stat-grid">
<div class="stat-card"><div class="stat-icon">👤</div><div><div class="stat-label">İstifadəçi</div><div class="stat-val" style="font-size:15px;">{name}</div><div style="font-size:11px;color:var(--mu);">Xoş Gəlmisiniz</div></div></div>
<div class="stat-card"><div class="stat-icon">📦</div><div><div class="stat-label">Ümumi Sifarişlər</div><div class="stat-val">{total_ord}</div></div></div>
<div class="stat-card"><div class="stat-icon">💸</div><div><div class="stat-label">Ümumi Xərclənən</div><div class="stat-val">{spent:.2f} ₼</div></div></div>
<div class="stat-card"><div class="stat-icon">💰</div><div><div class="stat-label">Balans</div><div class="stat-val">{bal:.2f} ₼</div></div></div>
</div>"""

    cat_opts="".join(f'<option value="{c}">{c}</option>' for c in categories.keys())
    first_cat=list(categories.keys())[0] if categories else ""
    first_prods=categories.get(first_cat,[])
    svc_opts="".join(f'<option value="{p["product_id"]}" data-price="{p["price"]}" data-min="{p["min_qty"]}" data-max="{p["max_qty"]}">{p["name"]} — {p["price"]} ₼/ədəd</option>' for p in first_prods)
    all_opts=""
    for cat,prods in categories.items():
        for p in prods:
            all_opts+=f'<option value="{p["product_id"]}" data-price="{p["price"]}" data-min="{p["min_qty"]}" data-max="{p["max_qty"]}">{p["name"]} — {p["price"]} ₼/ədəd</option>'

    ann_html=""
    for a in ann_list:
        dt=(a["created_at"] or "")[:16]
        ann_html+=f"""<div class="ann-card">
<div class="ann-head">📢 {dt}</div>
<div class="ann-body">{a["content"]}</div>
</div>"""

    body=f"""<section class="dash-page wrap">
{stats}
<div class="order-panel">
  <div class="search-bar">
    <input type="text" placeholder="🔍 Xidmət axtar..." oninput="filterSvc(this.value)">
  </div>
  <form method="post" action="/order">
    <div class="field"><label>Kateqoriya</label>
      <select id="cat-select" onchange="changeCat(this.value)">
        <option value="all">🔥 Bütün Xidmətlər</option>
        {cat_opts}
      </select>
    </div>
    <div class="field"><label>Xidmət</label>
      <select id="svc-select" name="product_id" required>
        {all_opts}
      </select>
    </div>
    <div class="field" id="completion-field"><label>Təxmini Tamamlanma Vaxtı</label>
      <input type="text" value="Xidmət seçin" readonly style="background:#f3f4f6;color:var(--mu);">
    </div>
    <div class="field"><label>Link</label>
      <input type="text" name="profile_link" placeholder="https://..." required></div>
    <div class="field"><label>Miqdar</label>
      <input type="number" id="qty-input" name="quantity" value="100" min="1" required>
      <span class="hint-text" id="min-qty">Min: 10</span>
    </div>
    <div class="price-display">
      <span class="label">Toplam Məbləğ</span>
      <span id="price-val">0.0000 ₼</span>
    </div>
    <div class="info-box">
      <p>⚠️ Profil Gizli Olmamalıdır! Sifariş verərkən profiliniz mütləq "Public" (Açıq) olmalıdır.</p>
      <p>⚠️ İkinci Sifarişi Vurmayın. Eyni linkə edilən bir sifariş bitmədən, ikinci sifarişi verməyin.</p>
      <p>⚠️ Link Formatı Düzgün Olsun. Linki tam şəkildə kopyalayın.</p>
      <p>⚠️ Dəstək Tələbi. Hər hansı bir xəta baş verərsə "Dəstək" bölməsindən bizə yazın.</p>
    </div>
    <button class="btn-or" type="submit">🚀 SİFARİŞİ TƏSDİQLƏ</button>
  </form>
</div>
{ann_html if ann_html else ""}
</section>
<script>
var allProds={{}};
{_build_prods_js(categories)}
function changeCat(cat){{
  var sel=document.getElementById('svc-select');
  sel.innerHTML='';
  var prods=cat==='all'?Object.values(allProds).flat():allProds[cat]||[];
  prods.forEach(function(p){{
    var o=document.createElement('option');
    o.value=p.id;o.dataset.price=p.price;o.dataset.min=p.min;o.dataset.max=p.max;
    o.innerText=p.name+' — '+p.price+' ₼/ədəd';
    sel.appendChild(o);
  }});
  sel.dispatchEvent(new Event('change'));
}}
function filterSvc(q){{
  var sel=document.getElementById('svc-select');
  var all=Object.values(allProds).flat();
  sel.innerHTML='';
  all.filter(p=>p.name.toLowerCase().includes(q.toLowerCase())).forEach(function(p){{
    var o=document.createElement('option');
    o.value=p.id;o.dataset.price=p.price;o.dataset.min=p.min;o.dataset.max=p.max;
    o.innerText=p.name+' — '+p.price+' ₼/ədəd';
    sel.appendChild(o);
  }});
}}
</script>"""
    return page_auth("Yeni Sifariş", body, user, bal_info, "new")

def render_orders(user, bal_info, orders):
    STATUS_MAP={"Gözləmədə":"pill-wait","Tamamlandı":"pill-done","Davam Edir":"pill-run","Ləğv Edildi":"pill-cancel","Yüklənir":"pill-run"}
    if not orders:
        tbl='<div class="empty-tbl">📭 Hələ heç bir sifarişiniz yoxdur. <a href="/" class="link-or">Yeni sifariş ver →</a></div>'
    else:
        rows=""
        for o in orders:
            sc=STATUS_MAP.get(o["status"],"pill-wait")
            dt=(o["order_date"] or "")[:16]
            rows+=f"""<tr class="order-row" data-status="{o["status"]}">
<td>#{o["order_id"]}</td>
<td>{dt}</td>
<td style="max-width:180px;word-break:break-all;font-size:12px;">{o["profile_link"]}</td>
<td>{o["price"]} ₼</td>
<td>{o.get("start_count",0)}</td>
<td>{o["quantity"]}</td>
<td style="max-width:140px;">{o["product_name"]}</td>
<td><span class="pill {sc}">{o["status"]}</span></td>
<td>{o.get("remains",0)}</td>
</tr>"""
        tbl=f'<div style="overflow-x:auto;"><table class="ot"><tbody>{rows}</tbody></table></div>'

    body=f"""<section class="dash-page wrap">
<h1 class="page-title">📦 Sifarişlərim</h1>
<div class="filter-tabs">
  <button class="ftab active" onclick="filterOrders('all')">Hamısı</button>
  <button class="ftab" onclick="filterOrders('Gözləmədə')">Gözləmədə</button>
  <button class="ftab" onclick="filterOrders('Yüklənir')">Yüklənir</button>
  <button class="ftab" onclick="filterOrders('Tamamlandı')">Tamamlandı</button>
  <button class="ftab" onclick="filterOrders('Davam Edir')">Davam Edir</button>
  <button class="ftab" onclick="filterOrders('Ləğv Edildi')">Ləğv Edildi</button>
</div>
<div class="table-card">
  <div class="search-orders">
    <input type="text" id="order-search" placeholder="🔍 Search orders" oninput="searchOrders()">
  </div>
  <div class="tbl-head" style="display:grid;grid-template-columns:50px 110px 1fr 70px 80px 60px 120px 90px 60px;padding:10px 12px;font-size:12px;font-weight:600;">
    <span>ID</span><span>Tarix</span><span>Link</span><span>Məbləğ</span><span>Başl.sayı</span><span>Miqdar</span><span>Xidmət Adı</span><span>Status</span><span>Qalıq</span>
  </div>
  {tbl}
</div>
</section>"""
    return page_auth("Sifarişlərim", body, user, bal_info, "orders")

def render_balance(user, bal_info, sent=False):
    bal=float(bal_info["balance"])
    pretty=" ".join(CARD_NUMBER[i:i+4] for i in range(0,len(CARD_NUMBER),4))
    holder=f'<div class="card-holder-name">{CARD_HOLDER}</div>' if CARD_HOLDER else ""
    ok=f'<div class="ok-box">✅ Sorğunuz göndərildi! Admin qəbzi yoxlayan kimi balansınız artırılacaq. Minimum 1-5 dəqiqə.</div>' if sent else ""
    body=f"""<section class="dash-page wrap">
<h1 class="page-title">💳 Balans artır</h1>
<div class="bal-page">
{ok}
<p style="font-size:14px;color:var(--mu);margin-bottom:16px;text-align:center;">Balans artırmaq üçün kartı seçin</p>
<div style="display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;">
  <button class="ftab active" onclick="void(0)">{CARD_BANK}</button>
</div>
<div class="card-3d">
  <div class="card-bank-name">💳 {CARD_BANK} Bank</div>
  <button class="copy-btn" id="copy-btn-el" onclick="copyCard()">Kopyala</button>
  <div>KART NÖMRƏSİ</div>
  <div class="card-number-disp" id="card-num-val">{pretty}</div>
  {holder}
</div>
<p style="font-size:13px;color:var(--mu);text-align:center;margin-bottom:16px;">
  20 AZN+ yükləmələrdə +3% Bonus.<br>Terminaldan və ya kartdan-karta ödəniş edə bilərsiniz.
</p>
<div class="order-panel">
  <h3 style="font-size:15px;font-weight:700;margin-bottom:16px;text-align:center;">Ödənişi Təsdiqle</h3>
  <form method="post" action="/balance" enctype="multipart/form-data">
    <div class="field"><label>MƏBLƏĞ (AZN)</label>
      <input type="text" name="amount_sent" placeholder="Məs: 10.00" required></div>
    <div class="field"><label>ÇEK / ŞƏKİL</label>
      <input type="file" name="receipt" accept="image/jpeg,image/jpg,image/png" required></div>
    <button class="btn-or" type="submit">GÖNDƏR</button>
  </form>
</div>
<div class="note-list">
  <p>❗ Ödəniş çekini mütləq yükləyin.</p>
  <p>❗ Bonuslar avtomatik əlavə olunur.</p>
  <p>❗ Min. ödəniş 1 AZN.</p>
</div>
</div>
</section>"""
    return page_auth("Balans artır", body, user, bal_info, "balance")

def render_support(user, bal_info, tickets, ok=""):
    ok_html=f'<div class="ok-box">{ok}</div>' if ok else ""
    rows=""
    if tickets:
        for t in tickets:
            dt=(t["created_at"] or "")[:10]
            sc="pill-done" if t["status"]=="Bağlı" else "pill-wait"
            rows+=f"""<div class="ticket-row">
<span>#{t["ticket_id"]}</span>
<span>{t["subject"]}</span>
<span><span class="pill {sc}">{t["status"]}</span></span>
<span style="font-size:12px;">{dt}</span>
</div>"""
    else:
        rows='<div class="empty-tbl">📭 Hələ heç bir ticket yoxdur.</div>'

    body=f"""<section class="dash-page wrap">
<h1 class="page-title">🎫 Dəstək</h1>
{ok_html}
<div class="ticket-form">
  <h3 style="font-size:15px;font-weight:700;margin-bottom:16px;">Yeni Ticket Yarat</h3>
  <form method="post" action="/support">
    <div class="field"><label>Mövzu</label>
      <select name="subject">
        <option>Sürətləndirmə</option>
        <option>Ödəniş problemi</option>
        <option>Sifariş problemi</option>
        <option>Digər</option>
      </select>
    </div>
    <div class="field"><label>Mesaj</label>
      <textarea name="message" placeholder="Probleminizi ətraflı təsvir edin..." required></textarea>
    </div>
    <button class="btn-or" type="submit">Yarat</button>
  </form>
</div>
<div class="ticket-tbl">
  <div class="ticket-tbl-head"><span>ID</span><span>Mövzu</span><span>Status</span><span>Son Yeniləmə</span></div>
  {rows}
</div>
</section>"""
    return page_auth("Dəstək", body, user, bal_info, "support")

def render_news(user, bal_info, anns):
    if not anns:
        body_inner='<p style="color:var(--mu);text-align:center;padding:40px 0;">Hələ xəbər yoxdur.</p>'
    else:
        body_inner=""
        for a in anns:
            dt=(a["created_at"] or "")[:16]
            body_inner+=f"""<div class="ann-card">
<div class="ann-head">📢 {dt} — {a["title"]}</div>
<div class="ann-body">{a["content"]}</div>
</div>"""
    body=f"""<section class="dash-page wrap">
<h1 class="page-title">📢 Son Xəbərlər</h1>
{body_inner}
</section>"""
    return page_auth("Xəbərlər", body, user, bal_info, "news")

# ===== AUTH ROUTES =====
@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    user=current_user(request)
    if not user: return HTMLResponse(render_landing())
    # logged in → dashboard
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM products WHERE stock>0 ORDER BY category,price")
        prods=c.fetchall()
        c.execute("SELECT * FROM announcements ORDER BY id DESC LIMIT 5")
        anns=c.fetchall()
    finally: put_conn(conn)
    by_cat={}
    for p in prods: by_cat.setdefault(p["category"],[]).append(p)
    bal_info=get_balance(user["email"])
    return HTMLResponse(render_dashboard(user,bal_info,by_cat,prods,anns))

@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, email: str=Form(...), password: str=Form(...)):
    email=email.lower().strip()
    user_row=get_user_db(email)
    if not user_row:
        return HTMLResponse(render_landing(err="Bu e-poçt ünvanı ilə hesab tapılmadı."))
    if not user_row.get("password"):
        return HTMLResponse(render_landing(err="Bu hesab Google ilə yaradılıb. Google ilə daxil olun."))
    if user_row["password"]!=hash_pw(password):
        return HTMLResponse(render_landing(err="Şifrə yanlışdır. Yenidən cəhd edin."))
    request.session["user"]={"email":user_row["email"],"name":user_row["name"]}
    return RedirectResponse(url="/",status_code=status.HTTP_303_SEE_OTHER)

@app.get("/register", response_class=HTMLResponse)
def register_get(): return HTMLResponse(render_register())

@app.post("/register", response_class=HTMLResponse)
def register_post(request: Request, name:str=Form(...), email:str=Form(...), password:str=Form(...), password2:str=Form(...)):
    email=email.lower().strip()
    if password!=password2: return HTMLResponse(render_register(err="Şifrələr uyğun gəlmir."))
    if len(password)<6: return HTMLResponse(render_register(err="Şifrə ən azı 6 simvol olmalıdır."))
    ex=get_user_db(email)
    if ex:
        if ex.get("password"): return HTMLResponse(render_register(err="Bu e-poçtla hesab artıq mövcuddur."))
        else: return HTMLResponse(render_register(err="Bu e-poçt Google ilə qeydiyyatdan keçib."))
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("INSERT INTO panel_users (email,name,password,balance,total_spent,total_orders,created_at) VALUES (%s,%s,%s,0,0,0,%s)",
                  (email,name.strip(),hash_pw(password),datetime.now().isoformat()))
        conn.commit()
    finally: put_conn(conn)
    request.session["user"]={"email":email,"name":name.strip()}
    return RedirectResponse(url="/",status_code=status.HTTP_303_SEE_OTHER)

@app.get("/login/google")
async def login_google(request: Request):
    if not GOOGLE_CLIENT_ID: return RedirectResponse(url="/")
    return await oauth.google.authorize_redirect(request, request.url_for("auth_callback"))

@app.get("/auth/callback")
async def auth_callback(request: Request):
    try: token=await oauth.google.authorize_access_token(request)
    except: return RedirectResponse(url="/")
    info=token.get("userinfo") or {}
    email=(info.get("email") or "").lower().strip()
    name=info.get("name") or email
    if not email: return RedirectResponse(url="/")
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("INSERT INTO panel_users (email,name,password,balance,total_spent,total_orders,created_at) VALUES (%s,%s,NULL,0,0,0,%s) ON CONFLICT (email) DO UPDATE SET name=EXCLUDED.name",
                  (email,name,datetime.now().isoformat()))
        conn.commit()
    finally: put_conn(conn)
    request.session["user"]={"email":email,"name":name}
    return RedirectResponse(url="/")

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")

# ===== MAIN ROUTES =====
@app.post("/order")
def create_order(request: Request, product_id:int=Form(...), quantity:int=Form(...), profile_link:str=Form(...)):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM products WHERE product_id=%s",(product_id,))
        prod=c.fetchone()
        if not prod: raise HTTPException(status_code=404)
        total=float(prod["price"])*quantity
        bal=float(get_balance(user["email"])["balance"])
        if bal<total: return RedirectResponse(url="/balance",status_code=status.HTTP_303_SEE_OTHER)
        c.execute("INSERT INTO orders (customer_email,product_id,quantity,profile_link,price,start_count,remains,status,order_date) VALUES (%s,%s,%s,%s,%s,0,%s,'Gözləmədə',%s) RETURNING order_id",
                  (user["email"],product_id,quantity,profile_link,total,quantity,datetime.now().isoformat()))
        c.execute("UPDATE panel_users SET balance=balance-%s,total_spent=total_spent+%s,total_orders=total_orders+1 WHERE email=%s",
                  (total,total,user["email"]))
        conn.commit()
    finally: put_conn(conn)
    return RedirectResponse(url="/orders",status_code=status.HTTP_303_SEE_OTHER)

@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT o.*,p.name AS product_name FROM orders o JOIN products p ON o.product_id=p.product_id WHERE o.customer_email=%s ORDER BY o.order_id DESC",(user["email"],))
        orders=c.fetchall()
    finally: put_conn(conn)
    return HTMLResponse(render_orders(user,get_balance(user["email"]),orders))

@app.get("/balance", response_class=HTMLResponse)
def balance_page(request: Request, sent:int=0):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    return HTMLResponse(render_balance(user,get_balance(user["email"]),sent=bool(sent)))

@app.post("/balance")
async def submit_balance(request: Request, amount_sent:str=Form(...), receipt:UploadFile=File(...)):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("INSERT INTO topups (customer_email,amount_sent,status,topup_date) VALUES (%s,%s,'Yoxlanılır',%s)",
                  (user["email"],amount_sent,datetime.now().isoformat()))
        conn.commit()
    finally: put_conn(conn)
    data=await receipt.read()
    if BOT_TOKEN and ADMIN_TELEGRAM_ID:
        cap=f"💳 Balans sorğusu\n👤 {user['name']} ({user['email']})\n💰 Məbləğ: {amount_sent} ₼"
        try:
            async with httpx.AsyncClient() as cl:
                await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                    data={"chat_id":ADMIN_TELEGRAM_ID,"caption":cap},
                    files={"photo":(receipt.filename or "qebz.jpg",data,receipt.content_type)})
        except: pass
    return RedirectResponse(url="/balance?sent=1",status_code=status.HTTP_303_SEE_OTHER)

@app.get("/support", response_class=HTMLResponse)
def support_page(request: Request, ok:str=""):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM tickets WHERE customer_email=%s ORDER BY ticket_id DESC",(user["email"],))
        tickets=c.fetchall()
    finally: put_conn(conn)
    return HTMLResponse(render_support(user,get_balance(user["email"]),tickets,ok))

@app.post("/support")
def submit_ticket(request: Request, subject:str=Form(...), message:str=Form(...)):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("INSERT INTO tickets (customer_email,subject,message,status,created_at) VALUES (%s,%s,%s,'Açıq',%s)",
                  (user["email"],subject,message,datetime.now().isoformat()))
        conn.commit()
    finally: put_conn(conn)
    return RedirectResponse(url="/support?ok=Ticketiniz+uğurla+yaradıldı.",status_code=status.HTTP_303_SEE_OTHER)

@app.get("/news", response_class=HTMLResponse)
def news_page(request: Request):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM announcements ORDER BY id DESC")
        anns=c.fetchall()
    finally: put_conn(conn)
    return HTMLResponse(render_news(user,get_balance(user["email"]),anns))

# ===== ADMIN =====
@app.get("/admin", response_class=HTMLResponse)
def admin_page(pw:str=""):
    if not ADMIN_KEY or pw!=ADMIN_KEY:
        return HTMLResponse(f"""<!DOCTYPE html><html><head>{HEAD("Admin")}</head><body style="display:flex;justify-content:center;padding:80px;">
<div class="hero-login-card" style="max-width:340px;">
<h2 style="color:var(--or);margin-bottom:16px;">🔐 Admin Girişi</h2>
<form method="get">
<div class="field"><label>Admin Şifrəsi</label><input type="password" name="pw" required></div>
<button class="btn-or" type="submit">Daxil ol</button>
</form></div></body></html>""",status_code=403)

    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM topups WHERE status='Yoxlanılır' ORDER BY topup_id DESC")
        pending=c.fetchall()
        c.execute("SELECT * FROM orders ORDER BY order_id DESC LIMIT 20")
        orders=c.fetchall()
        c.execute("SELECT * FROM panel_users ORDER BY created_at DESC LIMIT 20")
        users=c.fetchall()
    finally: put_conn(conn)

    pending_html=""
    for t in pending:
        pending_html+=f"""<tr>
<td>#{t["topup_id"]}</td><td>{t["customer_email"]}</td><td>{t["amount_sent"]} ₼</td>
<td><a href="/admin/approve?topup_id={t["topup_id"]}&pw={pw}&amount={t["amount_sent"]}&email={t["customer_email"]}" style="color:green;font-weight:700;">✅ Təsdiqlə</a></td>
</tr>"""

    user_rows=""
    for u in users:
        user_rows+=f"<tr><td>{u['email']}</td><td>{u['name']}</td><td>{u['balance']} ₼</td><td>{u['total_orders']}</td></tr>"

    return HTMLResponse(f"""<!DOCTYPE html><html><head>{HEAD("Admin Panel")}<style>
.adm{{padding:28px 0 60px;max-width:900px;margin:0 auto;}}
.adm-card{{background:#fff;border:1px solid #FED7AA;border-radius:12px;padding:20px;margin-bottom:20px;}}
.adm-card h3{{color:var(--or);margin-bottom:14px;font-size:16px;}}
table.at{{width:100%;border-collapse:collapse;font-size:13px;}}
table.at th{{background:var(--or);color:#fff;padding:10px 12px;text-align:left;}}
table.at td{{padding:10px 12px;border-bottom:1px solid #FEE2C8;}}
</style></head><body style="background:#f9fafb;">
<header style="background:#fff;border-bottom:1px solid #FED7AA;padding:0 20px;height:60px;display:flex;align-items:center;">
<span style="font-weight:800;color:var(--or);font-size:18px;">⚡ {BRAND} — Admin Panel</span>
</header>
<div class="adm wrap">

<div class="adm-card">
<h3>⏳ Gözləyən Balans Sorğuları</h3>
{"<table class='at'><thead><tr><th>ID</th><th>Email</th><th>Məbləğ</th><th>Əməliyyat</th></tr></thead><tbody>"+pending_html+"</tbody></table>" if pending else "<p style='color:#6B7280;'>Gözləyən sorğu yoxdur.</p>"}
</div>

<div class="adm-card">
<h3>💰 Balans əlavə et</h3>
<form method="post" action="/admin/credit?pw={pw}" style="display:flex;gap:10px;flex-wrap:wrap;">
<input style="flex:1;min-width:200px;background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;padding:10px 12px;font-size:14px;" type="email" name="email" placeholder="Email" required>
<input style="width:120px;background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;padding:10px 12px;font-size:14px;" type="text" name="amount" placeholder="Məbləğ" required>
<button class="btn-or" style="width:auto;padding:10px 20px;" type="submit">Əlavə et</button>
</form>
</div>

<div class="adm-card">
<h3>📦 Məhsul əlavə et</h3>
<form method="post" action="/admin/product?pw={pw}" style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
<input style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;padding:10px 12px;font-size:13px;" type="text" name="name" placeholder="Xidmət adı" required>
<input style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;padding:10px 12px;font-size:13px;" type="text" name="category" placeholder="Kateqoriya (məs. TikTok)" required>
<input style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;padding:10px 12px;font-size:13px;" type="text" name="price" placeholder="Qiymət (₼)" required>
<input style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;padding:10px 12px;font-size:13px;" type="number" name="min_qty" placeholder="Min miqdar" value="10">
<input style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;padding:10px 12px;font-size:13px;" type="number" name="max_qty" placeholder="Max miqdar" value="100000">
<input style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;padding:10px 12px;font-size:13px;" type="text" name="description" placeholder="Açıqlama (istəyə bağlı)">
<button class="btn-or" style="grid-column:1/-1;" type="submit">Məhsul əlavə et</button>
</form>
</div>

<div class="adm-card">
<h3>📢 Xəbər əlavə et</h3>
<form method="post" action="/admin/announce?pw={pw}" style="display:flex;flex-direction:column;gap:10px;">
<input style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;padding:10px 12px;font-size:13px;" type="text" name="title" placeholder="Başlıq" required>
<textarea style="background:#FFF7ED;border:1px solid #FED7AA;border-radius:8px;padding:10px 12px;font-size:13px;min-height:80px;" name="content" placeholder="Məzmun..." required></textarea>
<button class="btn-or" type="submit">Yayımla</button>
</form>
</div>

<div class="adm-card">
<h3>👥 İstifadəçilər</h3>
<table class="at"><thead><tr><th>Email</th><th>Ad</th><th>Balans</th><th>Sifariş</th></tr></thead>
<tbody>{user_rows}</tbody></table>
</div>

</div>
{SCRIPTS()}
</body></html>""")

@app.get("/admin/approve")
def admin_approve(pw:str,topup_id:int,amount:str,email:str):
    if pw!=ADMIN_KEY: raise HTTPException(403)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("UPDATE topups SET status='Təsdiqləndi' WHERE topup_id=%s",(topup_id,))
        try: amt=float(amount.replace(",","."))
        except: amt=0
        c.execute("INSERT INTO panel_users (email,name,balance) VALUES (%s,%s,%s) ON CONFLICT (email) DO UPDATE SET balance=panel_users.balance+EXCLUDED.balance",
                  (email,email,amt))
        conn.commit()
    finally: put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/credit")
def admin_credit(pw:str,email:str=Form(...),amount:str=Form(...)):
    if pw!=ADMIN_KEY: raise HTTPException(403)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("INSERT INTO panel_users (email,name,balance) VALUES (%s,%s,%s) ON CONFLICT (email) DO UPDATE SET balance=panel_users.balance+EXCLUDED.balance",
                  (email.lower(),email.lower(),float(amount)))
        conn.commit()
    finally: put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/product")
def admin_product(pw:str,name:str=Form(...),category:str=Form(...),price:str=Form(...),
                  min_qty:int=Form(10),max_qty:int=Form(100000),description:str=Form("")):
    if pw!=ADMIN_KEY: raise HTTPException(403)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("INSERT INTO products (name,category,price,min_qty,max_qty,stock,description) VALUES (%s,%s,%s,%s,%s,999,%s)",
                  (name,category,float(price),min_qty,max_qty,description or None))
        conn.commit()
    finally: put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/announce")
def admin_announce(pw:str,title:str=Form(...),content:str=Form(...)):
    if pw!=ADMIN_KEY: raise HTTPException(403)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("INSERT INTO announcements (title,content,created_at) VALUES (%s,%s,%s)",
                  (title,content,datetime.now().isoformat()))
        conn.commit()
    finally: put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

