import hashlib
import os
import secrets
from datetime import datetime

import httpx
import psycopg2
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
BOT_TOKEN            = os.getenv("BOT_TOKEN", "")
ADMIN_TELEGRAM_ID    = os.getenv("ADMIN_TELEGRAM_ID", "")
ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_KEY", "")
CARD_NUMBER = os.getenv("CARD_NUMBER", "5522099369926134")
CARD_HOLDER = os.getenv("CARD_HOLDER", "")
CARD_BANK   = os.getenv("CARD_BANK", "ABB")
BRAND       = "BoostPanel"

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

def get_conn():  return db_pool.getconn()
def put_conn(c): db_pool.putconn(c)


# ===== DB INIT =====
def init_db():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS panel_users (
                email       TEXT PRIMARY KEY,
                name        TEXT,
                password    TEXT,          -- NULL for Google-only accounts
                balance     NUMERIC DEFAULT 0,
                created_at  TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS products (
                product_id  SERIAL PRIMARY KEY,
                name        TEXT NOT NULL,
                category    TEXT NOT NULL,
                price       NUMERIC NOT NULL,
                stock       INTEGER NOT NULL,
                description TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id       SERIAL PRIMARY KEY,
                customer_email TEXT,
                product_id     INTEGER,
                quantity       INTEGER NOT NULL,
                profile_link   TEXT,
                price          NUMERIC,
                status         TEXT DEFAULT 'Gözləmədə',
                order_date     TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS topups (
                topup_id       SERIAL PRIMARY KEY,
                customer_email TEXT,
                amount_sent    TEXT,
                status         TEXT DEFAULT 'Yoxlanılır',
                topup_date     TEXT
            )
        """)
        conn.commit()
    finally:
        put_conn(conn)

@app.on_event("startup")
def startup(): init_db()


# ===== HELPERS =====
def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def current_user(request: Request):
    return request.session.get("user")

def get_balance(email: str) -> float:
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT balance FROM panel_users WHERE email=%s", (email,))
        row = c.fetchone()
        return float(row["balance"]) if row else 0.0
    finally:
        put_conn(conn)

def get_user_by_email(email: str):
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM panel_users WHERE email=%s", (email,))
        return c.fetchone()
    finally:
        put_conn(conn)


# ===== DESIGN =====
CSS = """
:root{--pr:#4F46E5;--prd:#3730A3;--bg:#EEF2FF;--sf:#fff;--tx:#1E1B4B;--mu:#6B7280;--ln:#E0E7FF;--ra:10px;--ac:#F59E0B;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--tx);font-family:'Inter',sans-serif;-webkit-font-smoothing:antialiased;}
.wrap{max-width:1060px;margin:0 auto;padding:0 20px;}
a{color:inherit;text-decoration:none;}
/* topbar */
.topbar{background:var(--sf);border-bottom:1px solid var(--ln);position:sticky;top:0;z-index:20;}
.topbar-inner{display:flex;justify-content:space-between;align-items:center;height:64px;}
.brand{display:flex;align-items:center;gap:8px;font-weight:800;font-size:19px;color:var(--prd);}
.brand-mark{color:var(--ac);font-size:22px;}
.hamburger{background:none;border:none;font-size:24px;cursor:pointer;color:var(--prd);padding:8px;}
.bal-chip{font-size:13px;color:var(--mu);margin-right:8px;}
.bal-chip strong{color:var(--prd);}
.topright{display:flex;align-items:center;}
/* sidebar */
.sb-ov{display:none;position:fixed;inset:0;background:rgba(30,27,75,.35);z-index:29;}
.sb-ov.open{display:block;}
.sidebar{position:fixed;top:0;right:-280px;width:260px;height:100%;background:var(--sf);box-shadow:-4px 0 24px rgba(0,0,0,.1);z-index:30;transition:right .25s ease;padding:24px 0;}
.sidebar.open{right:0;}
.sidebar a{display:flex;align-items:center;gap:12px;padding:14px 24px;font-size:15px;color:var(--tx);}
.sidebar a:hover{background:var(--bg);}
.sidebar .div{height:1px;background:var(--ln);margin:10px 0;}
/* auth pages */
.auth-wrap{padding:70px 0;display:flex;justify-content:center;}
.auth-card{background:var(--sf);border:1px solid var(--ln);border-radius:var(--ra);padding:36px;width:100%;max-width:380px;box-shadow:0 10px 30px rgba(79,70,229,.08);}
.auth-card h2{margin:0 0 6px;font-size:21px;color:var(--prd);}
.auth-card p.sub{color:var(--mu);font-size:13px;margin:0 0 22px;}
.auth-tabs{display:flex;gap:0;margin-bottom:24px;border:1px solid var(--ln);border-radius:8px;overflow:hidden;}
.auth-tabs a{flex:1;text-align:center;padding:10px;font-size:14px;font-weight:600;color:var(--mu);}
.auth-tabs a.active{background:var(--pr);color:#fff;}
.field{display:flex;flex-direction:column;gap:5px;font-size:13px;color:var(--mu);margin-bottom:14px;}
.field input,.field select{background:#F8FAFF;border:1px solid var(--ln);border-radius:8px;padding:11px 12px;color:var(--tx);font-family:'Inter',sans-serif;font-size:14px;width:100%;}
.field input:focus,.field select:focus{outline:2px solid var(--pr);outline-offset:1px;}
.btn{width:100%;background:var(--pr);color:#fff;border:none;border-radius:8px;padding:13px;font-weight:700;font-size:15px;cursor:pointer;font-family:'Inter',sans-serif;}
.btn:hover{background:var(--prd);}
.btn.sec{background:var(--sf);border:1px solid var(--ln);color:var(--tx);}
.divider-or{text-align:center;color:var(--mu);font-size:13px;margin:16px 0;position:relative;}
.divider-or::before,.divider-or::after{content:'';position:absolute;top:50%;width:40%;height:1px;background:var(--ln);}
.divider-or::before{left:0;}.divider-or::after{right:0;}
.google-btn{display:flex;align-items:center;justify-content:center;gap:10px;background:#fff;border:1px solid var(--ln);color:#1F1F1F;font-weight:600;padding:12px;border-radius:8px;font-size:14px;width:100%;cursor:pointer;font-family:'Inter',sans-serif;}
.err-box{background:#FEF2F2;color:#B91C1C;border:1px solid #FCA5A5;border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:14px;}
.ok-box{background:#ECFDF5;border:1px solid #6EE7B7;color:#065F46;border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:14px;}
/* hero */
.hero{padding:32px 0 0;}
.hero h1{font-size:24px;margin:0 0 4px;color:var(--prd);}
.hero p{color:var(--mu);font-size:14px;margin:0 0 20px;}
/* catalog */
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px;}
.tab{background:var(--sf);border:1px solid var(--ln);border-radius:999px;padding:8px 18px;font-size:14px;color:var(--tx);}
.tab.active{background:var(--pr);color:#fff;border-color:var(--pr);}
.order-card{background:var(--sf);border:1px solid var(--ln);border-radius:var(--ra);padding:24px;margin-bottom:48px;}
.hint{font-size:12px;color:var(--mu);margin:-8px 0 14px;}
/* balance */
.balance-page{padding:32px 0 64px;max-width:520px;}
.balance-page h1{font-size:22px;margin:0 0 20px;color:var(--prd);}
.card-display{background:linear-gradient(135deg,#4338CA,#1E1B4B);border-radius:14px;padding:24px;color:#fff;margin-bottom:16px;}
.card-bank{font-size:13px;opacity:.8;margin-bottom:20px;}
.card-number{font-family:'IBM Plex Mono',monospace;font-size:21px;letter-spacing:.08em;margin-bottom:14px;}
.card-holder{font-size:13px;opacity:.85;}
.note-box{background:#FFFBEB;border:1px solid #FDE68A;border-radius:8px;padding:14px 16px;font-size:13px;color:#92400E;margin:16px 0;}
.note-box p{margin:4px 0;}
/* orders table */
.o-wrap{padding:24px 0 64px;overflow-x:auto;}
table.ot{width:100%;border-collapse:collapse;background:var(--sf);border-radius:var(--ra);overflow:hidden;}
table.ot th{background:var(--bg);color:var(--prd);text-align:left;font-size:13px;padding:12px 14px;border-bottom:1px solid var(--ln);}
table.ot td{padding:12px 14px;font-size:13px;border-bottom:1px solid var(--ln);}
table.ot tr:last-child td{border-bottom:none;}
.pill{background:var(--pr);color:#fff;padding:3px 10px;border-radius:999px;font-size:12px;}
.pill.done{background:#10B981;}.pill.wait{background:var(--ac);}
.empty{color:var(--mu);padding:48px 0;text-align:center;}
/* footer */
.footer{padding:28px 0 40px;color:var(--mu);font-size:13px;border-top:1px solid var(--ln);margin-top:20px;}
@media(max-width:640px){.tabs{flex-wrap:nowrap;overflow-x:auto;}}
"""

HEAD = """<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400&display=swap" rel="stylesheet">
<style>{css}</style>
<script>
function toggleSidebar(f){{
  var sb=document.getElementById('sb'),ov=document.getElementById('sb-ov');
  var op=f!==undefined?f:!sb.classList.contains('open');
  sb.classList.toggle('open',op);ov.classList.toggle('open',op);
}}
</script>""".format(css=CSS)

GOOGLE_ICON = """<svg width="17" height="17" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.9c1.7-1.57 2.7-3.88 2.7-6.62z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.9-2.26c-.8.54-1.84.86-3.06.86-2.35 0-4.34-1.59-5.05-3.72H.96v2.33A9 9 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.95 10.7A5.4 5.4 0 0 1 3.66 9c0-.59.1-1.17.29-1.7V4.97H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.03l2.99-2.33z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.5.46 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.97l2.99 2.33C4.66 5.17 6.65 3.58 9 3.58z"/></svg>"""


def sidebar():
    return f"""
<div class="sb-ov" id="sb-ov" onclick="toggleSidebar(false)"></div>
<div class="sidebar" id="sb">
  <a href="/">🛒 Yeni Sifariş</a>
  <a href="/orders">📦 Sifarişlərim</a>
  <a href="/balance">💳 Balans artır</a>
  <div class="div"></div>
  <a href="/logout">🚪 Çıxış</a>
</div>"""


def topbar(user=None, balance=None):
    if user:
        bal = f'<span class="bal-chip">Balans: <strong>{balance:.2f} &#8380;</strong></span>' if balance is not None else ""
        right = f"""<div class="topright">{bal}<button class="hamburger" onclick="toggleSidebar()">&#9776;</button></div>"""
        sb = sidebar()
    else:
        right = ""
        sb = ""
    return f"""<header class="topbar"><div class="wrap topbar-inner">
  <a class="brand" href="/"><span class="brand-mark">&#9889;</span><span>{BRAND}</span></a>
  {right}
</div></header>{sb}"""


def page(title, body, user=None, balance=None):
    return f"""<!DOCTYPE html><html lang="az">
<head><title>{title} — {BRAND}</title>{HEAD}</head>
<body>{topbar(user, balance)}{body}
<footer class="footer wrap"><p>&copy; 2026 {BRAND}</p></footer>
</body></html>"""


# ===== AUTH PAGES =====
def render_login(err=""):
    err_html = f'<div class="err-box">{err}</div>' if err else ""
    google = f"""<div class="divider-or">ya da</div>
    <a href="/login/google" class="google-btn">{GOOGLE_ICON} Google ilə daxil ol</a>""" if GOOGLE_CLIENT_ID else ""
    body = f"""<section class="auth-wrap"><div class="auth-card">
  <h2>{BRAND}-a xoş gəlmisiniz</h2>
  <p class="sub">Hesabınıza daxil olun</p>
  <div class="auth-tabs">
    <a href="/login" class="active">Giriş</a>
    <a href="/register">Qeydiyyat</a>
  </div>
  {err_html}
  <form method="post" action="/login">
    <label class="field"><span>E-poçt</span><input type="email" name="email" required placeholder="sizin@gmail.com"></label>
    <label class="field"><span>Şifrə</span><input type="password" name="password" required placeholder="••••••••"></label>
    <button class="btn" type="submit">Daxil ol</button>
  </form>
  {google}
</div></section>"""
    return page("Daxil ol", body)


def render_register(err="", ok=""):
    err_html = f'<div class="err-box">{err}</div>' if err else ""
    ok_html  = f'<div class="ok-box">{ok}</div>' if ok else ""
    body = f"""<section class="auth-wrap"><div class="auth-card">
  <h2>Yeni hesab yarat</h2>
  <p class="sub">Qeydiyyatdan keçin</p>
  <div class="auth-tabs">
    <a href="/login">Giriş</a>
    <a href="/register" class="active">Qeydiyyat</a>
  </div>
  {err_html}{ok_html}
  <form method="post" action="/register">
    <label class="field"><span>Ad Soyad</span><input type="text" name="name" required placeholder="Adınız Soyadınız"></label>
    <label class="field"><span>E-poçt</span><input type="email" name="email" required placeholder="sizin@gmail.com"></label>
    <label class="field"><span>Şifrə</span><input type="password" name="password" required placeholder="Ən azı 6 simvol" minlength="6"></label>
    <label class="field"><span>Şifrəni təsdiqlə</span><input type="password" name="password2" required placeholder="••••••••"></label>
    <button class="btn" type="submit">Qeydiyyatdan keç</button>
  </form>
</div></section>"""
    return page("Qeydiyyat", body)


def render_home(categories, all_products, user, balance):
    cat_names = list(categories.keys())
    tabs = '<a href="/" class="tab active">Hamısı</a>' + "".join(
        f'<a href="/?cat={c}" class="tab">{c}</a>' for c in cat_names
    )
    options = ""
    for c, prods in categories.items():
        for p in prods:
            options += f'<option value="{p["product_id"]}">{c} — {p["name"]} ({p["price"]} &#8380;/ədəd)</option>'

    if not all_products:
        content = '<p class="empty">Hələ aktiv xidmət yoxdur. Tezliklə əlavə olunacaq.</p>'
    else:
        content = f"""
<div class="tabs">{tabs}</div>
<div class="order-card">
  <form method="post" action="/order">
    <label class="field"><span>Xidmət</span><select name="product_id" required>{options}</select></label>
    <label class="field"><span>Profil / Post linki</span><input type="text" name="profile_link" placeholder="https://..." required></label>
    <label class="field"><span>Miqdar</span><input type="number" name="quantity" value="1" min="1" required></label>
    <p class="hint">Balansınız kifayət etmirsə, ödəniş səhifəsinə yönləndiriləcəksiniz.</p>
    <button class="btn" type="submit">Sifariş ver</button>
  </form>
</div>"""

    body = f"""<section class="hero wrap"><h1>Yeni Sifariş</h1><p>Xidməti seçin, linki yapışdırın, miqdarı daxil edin.</p></section>
<section class="wrap">{content}</section>"""
    return page("Yeni Sifariş", body, user, balance)


def render_balance(user, balance, sent=False):
    pretty = " ".join(CARD_NUMBER[i:i+4] for i in range(0,len(CARD_NUMBER),4))
    holder = f'<div class="card-holder">{CARD_HOLDER}</div>' if CARD_HOLDER else ""
    ok = '<div class="ok-box">✅ Sorğunuz göndərildi. Admin qəbzi yoxlayan kimi balansınız artırılacaq.</div>' if sent else ""
    body = f"""<section class="balance-page wrap">
  <h1>Balans artır</h1>
  {ok}
  <div class="card-display">
    <div class="card-bank">{CARD_BANK}</div>
    <div class="card-number">{pretty}</div>
    {holder}
  </div>
  <div class="note-box">
    <p>❗ Yuxarıdakı karta istədiyiniz məbləği köçürün.</p>
    <p>❗ Qəbz şəklini mütləq yükləyin — admin yoxlayan kimi balans artırılır.</p>
    <p>❗ Minimum ödəniş: 1 &#8380;.</p>
  </div>
  <div class="order-card">
    <form method="post" action="/balance" enctype="multipart/form-data">
      <label class="field"><span>Köçürdüyünüz məbləğ (&#8380;)</span>
        <input type="text" name="amount_sent" placeholder="məs. 10.00" required></label>
      <label class="field"><span>Qəbz şəkli (JPG / PNG)</span>
        <input type="file" name="receipt" accept="image/jpeg,image/jpg,image/png" required></label>
      <button class="btn" type="submit">Göndər</button>
    </form>
  </div>
</section>"""
    return page("Balans artır", body, user, balance)


def render_orders(user, balance, orders):
    if not orders:
        body = f"""<section class="hero wrap"><h1>Sifarişlərim</h1></section>
<section class="wrap"><p class="empty">Hələ heç bir sifarişiniz yoxdur.
<a href="/" style="color:var(--pr);">Yeni sifariş ver →</a></p></section>"""
    else:
        rows = ""
        for o in orders:
            sc = "done" if o["status"]=="Tamamlandı" else "wait"
            dt = (o["order_date"] or "")[:10]
            rows += f"""<tr>
<td>#{o['order_id']}</td><td>{dt}</td>
<td style="max-width:200px;word-break:break-all;">{o['profile_link']}</td>
<td>{o['price']} &#8380;</td><td>{o['quantity']}</td>
<td>{o['product_name']}</td>
<td><span class="pill {sc}">{o['status']}</span></td></tr>"""
        body = f"""<section class="hero wrap"><h1>Sifarişlərim</h1></section>
<section class="wrap"><div class="o-wrap"><table class="ot">
<thead><tr><th>ID</th><th>Tarix</th><th>Link</th><th>Qiymət</th><th>Miqdar</th><th>Servis</th><th>Status</th></tr></thead>
<tbody>{rows}</tbody></table></div></section>"""
    return page("Sifarişlərim", body, user, balance)


# ===== AUTH ROUTES =====
@app.get("/login", response_class=HTMLResponse)
def login_get(err: str = ""):
    return render_login(err)

@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, email: str = Form(...), password: str = Form(...)):
    user_row = get_user_by_email(email.lower().strip())
    if not user_row:
        return HTMLResponse(render_login("Bu e-poçt ünvanı ilə hesab tapılmadı. Zəhmət olmasa qeydiyyatdan keçin."))
    if not user_row["password"]:
        return HTMLResponse(render_login("Bu hesab Google ilə yaradılıb. Google düyməsindən daxil olun."))
    if user_row["password"] != hash_pw(password):
        return HTMLResponse(render_login("Şifrə yanlışdır. Yenidən cəhd edin."))
    request.session["user"] = {"email": user_row["email"], "name": user_row["name"]}
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/register", response_class=HTMLResponse)
def register_get():
    return render_register()

@app.post("/register", response_class=HTMLResponse)
def register_post(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    email = email.lower().strip()
    if password != password2:
        return HTMLResponse(render_register(err="Şifrələr uyğun gəlmir."))
    if len(password) < 6:
        return HTMLResponse(render_register(err="Şifrə ən azı 6 simvol olmalıdır."))
    existing = get_user_by_email(email)
    if existing:
        if existing["password"]:
            return HTMLResponse(render_register(err="Bu e-poçtla hesab artıq mövcuddur. Daxil olmağa çalışın."))
        else:
            return HTMLResponse(render_register(err="Bu e-poçt Google ilə qeydiyyatdan keçib. Google düyməsindən daxil olun."))
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO panel_users (email, name, password, balance, created_at) VALUES (%s,%s,%s,0,%s)",
            (email, name.strip(), hash_pw(password), datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        put_conn(conn)
    request.session["user"] = {"email": email, "name": name.strip()}
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/login/google")
async def login_google(request: Request):
    if not GOOGLE_CLIENT_ID:
        return RedirectResponse(url="/login?err=Google+girişi+aktiv+deyil.")
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get("/auth/callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        return RedirectResponse(url="/login?err=Google+girişi+uğursuz+oldu.")
    info = token.get("userinfo") or {}
    email = (info.get("email") or "").lower().strip()
    name  = info.get("name") or email
    if not email:
        return RedirectResponse(url="/login?err=Google+hesabından+e-poçt+alınamadı.")
    existing = get_user_by_email(email)
    conn = get_conn()
    try:
        c = conn.cursor()
        if not existing:
            c.execute(
                "INSERT INTO panel_users (email, name, password, balance, created_at) VALUES (%s,%s,NULL,0,%s)",
                (email, name, datetime.now().isoformat()),
            )
        else:
            c.execute("UPDATE panel_users SET name=%s WHERE email=%s", (name, email))
        conn.commit()
    finally:
        put_conn(conn)
    request.session["user"] = {"email": email, "name": name}
    return RedirectResponse(url="/")

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")


# ===== MAIN ROUTES =====
@app.get("/", response_class=HTMLResponse)
def home(request: Request, cat: str = ""):
    user = current_user(request)
    if not user: return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM products WHERE stock>0 ORDER BY category,price ASC")
        products = c.fetchall()
    finally:
        put_conn(conn)
    by_cat = {}
    for p in products:
        by_cat.setdefault(p["category"], []).append(p)
    if cat and cat in by_cat:
        by_cat = {cat: by_cat[cat]}
    return render_home(by_cat, products, user, get_balance(user["email"]))

@app.post("/order")
def create_order(
    request: Request,
    product_id: int = Form(...),
    quantity: int = Form(...),
    profile_link: str = Form(...),
):
    user = current_user(request)
    if not user: return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM products WHERE product_id=%s", (product_id,))
        product = c.fetchone()
        if not product: raise HTTPException(status_code=404)
        total = float(product["price"]) * quantity
        balance = get_balance(user["email"])
        if balance < total:
            return RedirectResponse(url="/balance", status_code=status.HTTP_303_SEE_OTHER)
        c.execute(
            "INSERT INTO orders (customer_email,product_id,quantity,profile_link,price,status,order_date) "
            "VALUES (%s,%s,%s,%s,%s,'Gözləmədə',%s) RETURNING order_id",
            (user["email"], product_id, quantity, profile_link, total, datetime.now().isoformat()),
        )
        c.execute("UPDATE panel_users SET balance=balance-%s WHERE email=%s", (total, user["email"]))
        conn.commit()
    finally:
        put_conn(conn)
    return RedirectResponse(url="/orders", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/balance", response_class=HTMLResponse)
def balance_page(request: Request, sent: int = 0):
    user = current_user(request)
    if not user: return RedirectResponse(url="/login")
    return render_balance(user, get_balance(user["email"]), sent=bool(sent))

@app.post("/balance")
async def submit_balance(
    request: Request,
    amount_sent: str = Form(...),
    receipt: UploadFile = File(...),
):
    user = current_user(request)
    if not user: return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO topups (customer_email,amount_sent,status,topup_date) VALUES (%s,%s,'Yoxlanılır',%s)",
            (user["email"], amount_sent, datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        put_conn(conn)
    data = await receipt.read()
    if BOT_TOKEN and ADMIN_TELEGRAM_ID:
        caption = (
            f"💳 Balans sorğusu\n"
            f"👤 {user['name']} ({user['email']})\n"
            f"💰 Məbləğ: {amount_sent} ₼"
        )
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                    data={"chat_id": ADMIN_TELEGRAM_ID, "caption": caption},
                    files={"photo": (receipt.filename or "qebz.jpg", data, receipt.content_type)},
                )
        except Exception:
            pass
    return RedirectResponse(url="/balance?sent=1", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request):
    user = current_user(request)
    if not user: return RedirectResponse(url="/login")
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute(
            "SELECT o.*,p.name AS product_name FROM orders o "
            "JOIN products p ON o.product_id=p.product_id "
            "WHERE o.customer_email=%s ORDER BY o.order_id DESC",
            (user["email"],),
        )
        orders = c.fetchall()
    finally:
        put_conn(conn)
    return render_orders(user, get_balance(user["email"]), orders)


# ===== ADMIN =====
@app.get("/admin", response_class=HTMLResponse)
def admin_page(pw: str = ""):
    if not ADMIN_PANEL_PASSWORD or pw != ADMIN_PANEL_PASSWORD:
        return HTMLResponse("<p style='font-family:sans-serif;padding:40px'>403 — ?pw=... əlavə edin</p>", status_code=403)
    return HTMLResponse(f"""<!DOCTYPE html><html><head>{HEAD}</head><body style="padding:40px">
<h2 style="color:#3730A3">{BRAND} — Admin Panel</h2>
<h3>Balans əlavə et</h3>
<form method="post" action="/admin/credit?pw={pw}">
  <label class="field" style="max-width:400px"><span>İstifadəçi e-poçtu</span><input type="email" name="email" required></label>
  <label class="field" style="max-width:400px"><span>Məbləğ (₼)</span><input type="text" name="amount" required></label>
  <button class="btn" style="max-width:400px" type="submit">Əlavə et</button>
</form>
<h3 style="margin-top:32px">Məhsul əlavə et</h3>
<form method="post" action="/admin/product?pw={pw}">
  <label class="field" style="max-width:400px"><span>Ad</span><input type="text" name="name" required></label>
  <label class="field" style="max-width:400px"><span>Kateqoriya (məs. TikTok)</span><input type="text" name="category" required></label>
  <label class="field" style="max-width:400px"><span>Qiymət (₼/ədəd)</span><input type="text" name="price" required></label>
  <label class="field" style="max-width:400px"><span>Stok</span><input type="number" name="stock" value="999" required></label>
  <label class="field" style="max-width:400px"><span>Açıqlama (istəyə bağlı)</span><input type="text" name="description"></label>
  <button class="btn" style="max-width:400px" type="submit">Əlavə et</button>
</form>
</body></html>""")

@app.post("/admin/credit")
def admin_credit(pw: str, email: str = Form(...), amount: str = Form(...)):
    if not ADMIN_PANEL_PASSWORD or pw != ADMIN_PANEL_PASSWORD:
        raise HTTPException(status_code=403)
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO panel_users (email,name,balance) VALUES (%s,%s,%s) "
            "ON CONFLICT (email) DO UPDATE SET balance=panel_users.balance+EXCLUDED.balance",
            (email.lower(), email.lower(), float(amount)),
        )
        conn.commit()
    finally:
        put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/product")
def admin_product(
    pw: str,
    name: str = Form(...),
    category: str = Form(...),
    price: str = Form(...),
    stock: int = Form(...),
    description: str = Form(""),
):
    if not ADMIN_PANEL_PASSWORD or pw != ADMIN_PANEL_PASSWORD:
        raise HTTPException(status_code=403)
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO products (name,category,price,stock,description) VALUES (%s,%s,%s,%s,%s)",
            (name, category, float(price), stock, description or None),
        )
        conn.commit()
    finally:
        put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}", status_code=status.HTTP_303_SEE_OTHER)

