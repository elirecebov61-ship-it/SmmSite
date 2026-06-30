import os
from datetime import datetime

import httpx
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File, status
from fastapi.responses import RedirectResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware

# ===== CONFIG (hamısı Railway Variables-dən gəlir, koda heç bir gizli məlumat yazılmır) =====
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/eren_smm")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-secret-deyisin")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")          # Telegram botunuzun tokeni - qəbz şəkli admin-ə bununla göndərilir
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "")  # Sizin Telegram istifadəçi ID-niz
CARD_NUMBER = os.getenv("CARD_NUMBER", "5522099369926134")
CARD_HOLDER = os.getenv("CARD_HOLDER", "")
CARD_BANK = os.getenv("CARD_BANK", "ABB")

app = FastAPI(title="SMM Panel")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)


def get_conn():
    return db_pool.getconn()


def put_conn(conn):
    db_pool.putconn(conn)


def init_db():
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS products (
                product_id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                price INTEGER NOT NULL,
                stock INTEGER NOT NULL,
                description TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                order_id SERIAL PRIMARY KEY,
                customer_email TEXT,
                customer_name TEXT,
                product_id INTEGER,
                quantity INTEGER NOT NULL,
                profile_link TEXT,
                amount_sent TEXT,
                status TEXT DEFAULT 'Ödəniş yoxlanılır',
                order_date TEXT
            )"""
        )
        conn.commit()
    finally:
        put_conn(conn)


@app.on_event("startup")
def on_startup():
    init_db()


def current_user(request: Request):
    return request.session.get("user")


# ===================== DESIGN =====================
PAGE_CSS = """
:root { --ink:#0E1116; --paper:#F6F4EE; --acid:#C8FF4D; --coral:#FF5C39; --muted:#6B7280; --card:#161A22; --radius:4px; }
* { box-sizing:border-box; }
body { margin:0; background:var(--ink); color:var(--paper); font-family:'Space Grotesk',sans-serif; -webkit-font-smoothing:antialiased; }
.wrap { max-width:1080px; margin:0 auto; padding:0 24px; }
a { color:inherit; text-decoration:none; }
.topbar { border-bottom:1px solid #262B36; position:sticky; top:0; background:rgba(14,17,22,0.92); backdrop-filter:blur(6px); z-index:10; }
.topbar-inner { display:flex; justify-content:space-between; align-items:center; height:64px; }
.brand { display:flex; align-items:center; gap:8px; font-weight:700; letter-spacing:0.04em; }
.brand-mark { color:var(--acid); font-size:22px; }
.userchip { font-size:13px; color:#B7BCC6; display:flex; align-items:center; gap:10px; }
.userchip a { color:var(--coral); }
.hero { padding:88px 0 0; }
.eyebrow { font-family:'IBM Plex Mono',monospace; font-size:12px; letter-spacing:0.18em; color:var(--acid); margin:0 0 16px; }
.hero h1 { font-size:clamp(34px,6vw,58px); line-height:1.05; margin:0 0 20px; font-weight:700; letter-spacing:-0.01em; }
.hero-sub { max-width:520px; color:#B7BCC6; font-size:17px; line-height:1.6; margin:0 0 32px; }
.cta { display:inline-flex; align-items:center; gap:10px; background:var(--acid); color:var(--ink); font-weight:600; padding:14px 24px; border-radius:var(--radius); font-size:15px; border:none; cursor:pointer; }
.cta.secondary { background:transparent; border:1px solid #2D3340; color:var(--paper); }
.login-wrap { padding:100px 0; display:flex; justify-content:center; }
.login-card { background:var(--card); border:1px solid #262B36; border-radius:var(--radius); padding:40px; text-align:center; max-width:380px; }
.login-card h2 { margin:0 0 10px; font-size:22px; }
.login-card p { color:var(--muted); font-size:14px; margin:0 0 28px; }
.google-btn { display:inline-flex; align-items:center; gap:10px; background:#fff; color:#1F1F1F; font-weight:600; padding:12px 22px; border-radius:var(--radius); font-size:14px; }
.catalog { padding:56px 0; }
.cat-block { margin-bottom:48px; }
.cat-title { font-size:22px; margin:0 0 20px; padding-bottom:12px; border-bottom:1px solid #262B36; }
.cat-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:16px; }
.product-card { background:var(--card); border:1px solid #262B36; border-radius:var(--radius); padding:20px; display:flex; flex-direction:column; gap:10px; }
.product-top { display:flex; justify-content:space-between; align-items:baseline; gap:12px; }
.product-top h3 { margin:0; font-size:16px; }
.price { font-family:'IBM Plex Mono',monospace; color:var(--acid); font-size:14px; white-space:nowrap; }
.product-desc { margin:0; font-size:13px; color:var(--muted); }
.empty-state { color:var(--muted); }
.checkout { padding:56px 0 96px; max-width:560px; }
.checkout h1 { font-size:26px; margin:0 0 8px; }
.checkout .sub { color:var(--muted); font-size:14px; margin:0 0 32px; }
.step-card { background:var(--card); border:1px solid #262B36; border-radius:var(--radius); padding:24px; margin-bottom:24px; }
.step-label { font-family:'IBM Plex Mono',monospace; color:var(--coral); font-size:12px; letter-spacing:0.1em; margin:0 0 14px; }
.card-display { background:linear-gradient(135deg,#1B2030,#0E1116); border:1px solid #2D3340; border-radius:8px; padding:22px; margin-bottom:6px; }
.card-bank { font-size:13px; color:var(--muted); margin-bottom:18px; }
.card-number { font-family:'IBM Plex Mono',monospace; font-size:20px; letter-spacing:0.08em; margin-bottom:14px; }
.card-holder { font-size:13px; color:#B7BCC6; }
.copy-hint { font-size:12px; color:var(--muted); margin:10px 0 0; }
.field { display:flex; flex-direction:column; gap:6px; font-size:13px; color:#B7BCC6; margin-bottom:16px; }
.field input { background:var(--ink); border:1px solid #2D3340; border-radius:var(--radius); padding:10px 12px; color:var(--paper); font-family:'Space Grotesk',sans-serif; font-size:14px; }
.field input:focus { outline:2px solid var(--acid); outline-offset:1px; }
.field input[type=file] { padding:10px; font-size:13px; }
.order-btn { width:100%; background:var(--coral); color:var(--ink); border:none; border-radius:var(--radius); padding:13px 14px; font-weight:600; font-size:15px; cursor:pointer; font-family:'Space Grotesk',sans-serif; }
.status-page { padding:72px 0 96px; max-width:560px; }
.status-card { background:var(--card); border:1px solid #262B36; border-radius:var(--radius); padding:8px 20px; margin:24px 0; }
.status-row { display:flex; justify-content:space-between; gap:12px; padding:14px 0; border-bottom:1px solid #21262F; font-size:14px; }
.status-row:last-child { border-bottom:none; }
.status-row span { color:var(--muted); }
.status-row strong { font-weight:600; text-align:right; }
.mono { font-family:'IBM Plex Mono',monospace; font-size:13px; word-break:break-all; }
.badge { background:var(--acid); color:var(--ink); padding:2px 10px; border-radius:999px; font-size:12px; }
.footer { padding:32px 0 48px; color:var(--muted); font-size:13px; border-top:1px solid #262B36; }
@media (max-width:640px){ .cat-grid{grid-template-columns:1fr;} }
"""

PAGE_HEAD = """<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{css}</style>""".format(css=PAGE_CSS)


def topbar(user):
    if user:
        right = f"""<div class="userchip">{user['name']} ({user['email']}) &middot; <a href="/logout">Çıxış</a></div>"""
    else:
        right = '<a href="/login" class="cta secondary" style="padding:8px 16px;font-size:13px;">Daxil ol</a>'
    return f"""
<header class="topbar">
  <div class="wrap topbar-inner">
    <a class="brand" href="/"><span class="brand-mark">&#10209;</span><span class="brand-name">BOOST</span></a>
    {right}
  </div>
</header>
"""


# ===================== PAGE RENDERERS =====================
def render_login() -> str:
    return f"""<!DOCTYPE html>
<html lang="az"><head><title>Daxil ol — Boost</title>{PAGE_HEAD}</head>
<body>
{topbar(None)}
<section class="login-wrap">
  <div class="login-card">
    <h2>Davam etmək üçün daxil olun</h2>
    <p>Sifariş vermək üçün Google hesabınızla daxil olun.</p>
    <a href="/login/google" class="google-btn">
      <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.9c1.7-1.57 2.7-3.88 2.7-6.62z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.9-2.26c-.8.54-1.84.86-3.06.86-2.35 0-4.34-1.59-5.05-3.72H.96v2.33A9 9 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.95 10.7A5.4 5.4 0 0 1 3.66 9c0-.59.1-1.17.29-1.7V4.97H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.03l2.99-2.33z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.5.46 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.97l2.99 2.33C4.66 5.17 6.65 3.58 9 3.58z"/></svg>
      Google ilə daxil ol
    </a>
  </div>
</section>
</body></html>"""


def render_home(categories: dict, user) -> str:
    if categories:
        blocks = []
        for category, products in categories.items():
            cards = []
            for p in products:
                desc = f'<p class="product-desc">{p["description"]}</p>' if p.get("description") else ""
                action = f"/checkout/{p['product_id']}" if user else "/login"
                cards.append(f"""
                <a href="{action}" class="product-card" style="text-decoration:none;">
                  <div class="product-top"><h3>{p['name']}</h3><span class="price">{p['price']} &#8380;</span></div>
                  {desc}
                </a>""")
            blocks.append(f"""<div class="cat-block"><h2 class="cat-title">{category}</h2><div class="cat-grid">{''.join(cards)}</div></div>""")
        catalog_html = "".join(blocks)
    else:
        catalog_html = '<p class="empty-state">Hələ aktiv xidmət yoxdur. Tezliklə əlavə olunacaq.</p>'

    return f"""<!DOCTYPE html>
<html lang="az"><head><title>Boost — Sosial Media Artım Paneli</title>{PAGE_HEAD}</head>
<body>
{topbar(user)}
<section class="hero wrap">
  <p class="eyebrow">İZLƏNMƏ &middot; BƏYƏNİ &middot; TAKİPÇİ</p>
  <h1>Hesabınız böyüsün,<br>siz işinizlə məşğul olun.</h1>
  <p class="hero-sub">TikTok, Instagram, Telegram və YouTube üçün sürətli, dayanıqlı artım xidmətləri.</p>
</section>
<section class="catalog wrap">{catalog_html}</section>
<footer class="footer wrap"><p>&copy; 2026 Boost Panel</p></footer>
</body></html>"""


def render_checkout(product: dict) -> str:
    holder_html = f'<div class="card-holder">{CARD_HOLDER}</div>' if CARD_HOLDER else ""
    pretty_card = " ".join([CARD_NUMBER[i:i + 4] for i in range(0, len(CARD_NUMBER), 4)])
    return f"""<!DOCTYPE html>
<html lang="az"><head><title>Sifariş — {product['name']}</title>{PAGE_HEAD}</head>
<body>
{topbar(current_user_placeholder())}
<section class="checkout wrap">
  <h1>{product['name']}</h1>
  <p class="sub">{product['price']} &#8380; &middot; {product['category']}</p>

  <div class="step-card">
    <p class="step-label">ADDIM 1 &middot; ÖDƏNİŞ</p>
    <div class="card-display">
      <div class="card-bank">{CARD_BANK} kart</div>
      <div class="card-number">{pretty_card}</div>
      {holder_html}
    </div>
    <p class="copy-hint">Yuxarıdakı karta sifariş məbləğini köçürün, sonra aşağıdakı formu doldurub qəbz şəklini yükləyin.</p>
  </div>

  <div class="step-card">
    <p class="step-label">ADDIM 2 &middot; SİFARİŞ MƏLUMATI</p>
    <form method="post" action="/checkout/{product['product_id']}" enctype="multipart/form-data">
      <label class="field"><span>Hesab/Profil linki</span>
        <input type="text" name="profile_link" placeholder="https://..." required>
      </label>
      <label class="field"><span>Miqdar</span>
        <input type="number" name="quantity" value="1" min="1" required>
      </label>
      <label class="field"><span>Köçürdüyünüz məbləğ (₼)</span>
        <input type="text" name="amount_sent" placeholder="məs. 5.00" required>
      </label>
      <label class="field"><span>Qəbz şəkli (JPG)</span>
        <input type="file" name="receipt" accept="image/jpeg,image/jpg,image/png" required>
      </label>
      <button type="submit" class="order-btn">Sifarişi göndər &#8594;</button>
    </form>
  </div>
</section>
</body></html>"""


def current_user_placeholder():
    # topbar() yalnız adı/email göstərmək üçündür, checkout səhifəsində request session-a bu funksiyadan çatmırıq;
    # sadəlik üçün checkout marşrutunda topbar ayrıca çağırılır (aşağıya bax).
    return None


def render_thanks(order_id: int) -> str:
    return f"""<!DOCTYPE html>
<html lang="az"><head><title>Sifariş göndərildi — Boost</title>{PAGE_HEAD}</head>
<body>
{topbar(None)}
<section class="status-page wrap">
  <p class="eyebrow">SİFARİŞ #{order_id}</p>
  <h1>Qəbul edildi, yoxlanılır</h1>
  <p class="sub">Ödənişiniz admin tərəfindən təsdiqlənəndən sonra sifarişiniz başlayacaq. Bu adətən qısa müddətdə baş verir.</p>
  <a href="/order/{order_id}" class="cta">Statusu izlə</a>
</section>
</body></html>"""


def render_order_status(order: dict) -> str:
    return f"""<!DOCTYPE html>
<html lang="az"><head><title>Sifariş #{order['order_id']} — Boost</title>{PAGE_HEAD}</head>
<body>
{topbar(None)}
<section class="status-page wrap">
  <p class="eyebrow">SİFARİŞ #{order['order_id']}</p>
  <h1>{order['product_name']}</h1>
  <div class="status-card">
    <div class="status-row"><span>Platforma</span><strong>{order['category']}</strong></div>
    <div class="status-row"><span>Miqdar</span><strong>{order['quantity']}</strong></div>
    <div class="status-row"><span>Link</span><strong class="mono">{order['profile_link']}</strong></div>
    <div class="status-row"><span>Köçürülən məbləğ</span><strong>{order['amount_sent']} &#8380;</strong></div>
    <div class="status-row"><span>Status</span><strong class="badge">{order['status']}</strong></div>
    <div class="status-row"><span>Tarix</span><strong class="mono">{order['order_date']}</strong></div>
  </div>
  <a href="/" class="cta">&#8592; Ana səhifə</a>
</section>
</body></html>"""


# ===================== AUTH ROUTES =====================
@app.get("/login")
def login_page():
    return HTMLResponse(render_login())


@app.get("/login/google")
async def login_google(request: Request):
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo") or {}
    request.session["user"] = {
        "email": userinfo.get("email", ""),
        "name": userinfo.get("name", userinfo.get("email", "İstifadəçi")),
    }
    return RedirectResponse(url="/")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")


# ===================== STORE ROUTES =====================
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM products WHERE stock > 0 ORDER BY category, price ASC")
        products = c.fetchall()
    finally:
        put_conn(conn)

    by_category = {}
    for p in products:
        by_category.setdefault(p["category"], []).append(p)

    return render_home(by_category, current_user(request))


@app.get("/checkout/{product_id}", response_class=HTMLResponse)
def checkout_page(request: Request, product_id: int):
    if not current_user(request):
        return RedirectResponse(url="/login")

    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM products WHERE product_id = %s", (product_id,))
        product = c.fetchone()
    finally:
        put_conn(conn)

    if not product:
        raise HTTPException(status_code=404, detail="Məhsul tapılmadı")

    html = render_checkout(product).replace(
        topbar(None), topbar(current_user(request)), 1
    )
    return HTMLResponse(html)


@app.post("/checkout/{product_id}")
async def submit_checkout(
    request: Request,
    product_id: int,
    profile_link: str = Form(...),
    quantity: int = Form(...),
    amount_sent: str = Form(...),
    receipt: UploadFile = File(...),
):
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/login")

    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM products WHERE product_id = %s", (product_id,))
        product = c.fetchone()
        if not product:
            raise HTTPException(status_code=404, detail="Məhsul tapılmadı")

        c.execute(
            """INSERT INTO orders (customer_email, customer_name, product_id, quantity, profile_link, amount_sent, status, order_date)
               VALUES (%s, %s, %s, %s, %s, %s, 'Ödəniş yoxlanılır', %s) RETURNING order_id""",
            (user["email"], user["name"], product_id, quantity, profile_link, amount_sent, datetime.now().isoformat()),
        )
        order_id = c.fetchone()["order_id"]
        conn.commit()
    finally:
        put_conn(conn)

    # Qəbz şəklini admin-ə Telegram bot vasitəsilə göndəririk (diskdə saxlamırıq).
    receipt_bytes = await receipt.read()
    if BOT_TOKEN and ADMIN_TELEGRAM_ID:
        caption = (
            f"🧾 Yeni sifariş #{order_id}\n"
            f"👤 {user['name']} ({user['email']})\n"
            f"📦 {product['name']} x{quantity}\n"
            f"🔗 {profile_link}\n"
            f"💰 Göndərilən məbləğ: {amount_sent} ₼"
        )
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                    data={"chat_id": ADMIN_TELEGRAM_ID, "caption": caption},
                    files={"photo": (receipt.filename or "receipt.jpg", receipt_bytes, receipt.content_type)},
                )
        except Exception:
            pass  # admin bildirişi uğursuz olsa belə sifariş bazada qalır, admin əl ilə yoxlaya bilər

    return RedirectResponse(url=f"/thanks/{order_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/thanks/{order_id}", response_class=HTMLResponse)
def thanks_page(order_id: int):
    return render_thanks(order_id)


@app.get("/order/{order_id}", response_class=HTMLResponse)
def order_status_page(order_id: int):
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute(
            """SELECT o.*, p.name AS product_name, p.category
               FROM orders o JOIN products p ON o.product_id = p.product_id
               WHERE o.order_id = %s""",
            (order_id,),
        )
        order = c.fetchone()
    finally:
        put_conn(conn)

    if not order:
        raise HTTPException(status_code=404, detail="Sifariş tapılmadı")

    return render_order_status(order)

