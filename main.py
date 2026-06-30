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

# ===== CONFIG (Railway Variables-dən gəlir) =====
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/eren_smm")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-secret-deyisin")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "")
ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD", "")
CARD_NUMBER = os.getenv("CARD_NUMBER", "5522099369926134")
CARD_HOLDER = os.getenv("CARD_HOLDER", "")
CARD_BANK = os.getenv("CARD_BANK", "ABB")
BRAND_NAME = "BoostPanel"

app = FastAPI(title=BRAND_NAME)
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
            """CREATE TABLE IF NOT EXISTS panel_users (
                email TEXT PRIMARY KEY,
                name TEXT,
                balance NUMERIC DEFAULT 0
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS products (
                product_id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                price NUMERIC NOT NULL,
                stock INTEGER NOT NULL,
                description TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                order_id SERIAL PRIMARY KEY,
                customer_email TEXT,
                product_id INTEGER,
                quantity INTEGER NOT NULL,
                profile_link TEXT,
                price NUMERIC,
                status TEXT DEFAULT 'Gözləmədə',
                order_date TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS topups (
                topup_id SERIAL PRIMARY KEY,
                customer_email TEXT,
                amount_sent TEXT,
                status TEXT DEFAULT 'Yoxlanılır',
                topup_date TEXT
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


def get_balance(email: str):
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT balance FROM panel_users WHERE email = %s", (email,))
        row = c.fetchone()
        return float(row["balance"]) if row else 0.0
    finally:
        put_conn(conn)


def ensure_panel_user(email: str, name: str):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO panel_users (email, name, balance) VALUES (%s, %s, 0) ON CONFLICT (email) DO NOTHING",
            (email, name),
        )
        conn.commit()
    finally:
        put_conn(conn)


# ===================== DESIGN (PanelBaku-vari açıq tema, BoostPanel rəngləri) =====================
PAGE_CSS = """
:root {
  --primary:#4F46E5; --primary-dark:#3730A3; --bg:#EEF2FF; --surface:#FFFFFF;
  --text:#1E1B4B; --muted:#6B7280; --line:#E0E7FF; --radius:10px; --accent:#F59E0B;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family:'Inter',sans-serif; -webkit-font-smoothing:antialiased; }
.wrap { max-width:1080px; margin:0 auto; padding:0 20px; }
a { color:inherit; text-decoration:none; }
.topbar { background:var(--surface); border-bottom:1px solid var(--line); position:sticky; top:0; z-index:20; }
.topbar-inner { display:flex; justify-content:space-between; align-items:center; height:64px; }
.brand { display:flex; align-items:center; gap:8px; font-weight:800; font-size:19px; color:var(--primary-dark); }
.brand-mark { color:var(--accent); font-size:22px; }
.hamburger { background:none; border:none; font-size:22px; cursor:pointer; color:var(--primary-dark); padding:8px; }
.balance-chip { font-size:13px; color:var(--muted); margin-right:10px; }
.balance-chip strong { color:var(--primary-dark); }
.topright { display:flex; align-items:center; }

.sidebar-overlay { display:none; position:fixed; inset:0; background:rgba(30,27,75,0.35); z-index:29; }
.sidebar-overlay.open { display:block; }
.sidebar { position:fixed; top:0; right:-280px; width:260px; height:100%; background:var(--surface);
  box-shadow:-4px 0 24px rgba(0,0,0,0.08); z-index:30; transition:right .25s ease; padding:24px 0; }
.sidebar.open { right:0; }
.sidebar a, .sidebar .sidebar-item { display:flex; align-items:center; gap:12px; padding:14px 24px; font-size:15px; color:var(--text); }
.sidebar a:hover { background:var(--bg); }
.sidebar .divider { height:1px; background:var(--line); margin:10px 0; }

.login-wrap { padding:90px 0; display:flex; justify-content:center; }
.login-card { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); padding:40px;
  text-align:center; max-width:380px; box-shadow:0 10px 30px rgba(79,70,229,0.08); }
.login-card h2 { margin:0 0 10px; font-size:21px; color:var(--primary-dark); }
.login-card p { color:var(--muted); font-size:14px; margin:0 0 26px; }
.google-btn { display:inline-flex; align-items:center; gap:10px; background:#fff; border:1px solid var(--line);
  color:#1F1F1F; font-weight:600; padding:12px 22px; border-radius:var(--radius); font-size:14px; }
.login-err { background:#FEF2F2; color:#B91C1C; border:1px solid #FCA5A5; border-radius:8px; padding:10px 14px; font-size:13px; margin-bottom:18px; }

.hero { padding:36px 0 0; }
.hero h1 { font-size:26px; margin:0 0 6px; color:var(--primary-dark); }
.hero p { color:var(--muted); font-size:14px; margin:0; }

.tabs { display:flex; gap:10px; flex-wrap:wrap; padding:24px 0 4px; }
.tab { background:var(--surface); border:1px solid var(--line); border-radius:999px; padding:8px 18px; font-size:14px; color:var(--text); }
.tab.active { background:var(--primary); color:#fff; border-color:var(--primary); }

.order-card { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); padding:24px; margin:18px 0 56px; }
.field { display:flex; flex-direction:column; gap:6px; font-size:13px; color:var(--muted); margin-bottom:16px; }
.field select, .field input { background:#F8FAFF; border:1px solid var(--line); border-radius:8px; padding:11px 12px;
  color:var(--text); font-family:'Inter',sans-serif; font-size:14px; }
.field select:focus, .field input:focus { outline:2px solid var(--primary); outline-offset:1px; }
.hint { font-size:12px; color:var(--muted); margin:-8px 0 16px; }
.submit-btn { width:100%; background:var(--primary); color:#fff; border:none; border-radius:8px; padding:13px 14px;
  font-weight:700; font-size:15px; cursor:pointer; }
.submit-btn:hover { background:var(--primary-dark); }

.balance-page { padding:32px 0 64px; max-width:520px; }
.balance-page h1 { font-size:22px; margin:0 0 22px; color:var(--primary-dark); }
.card-display { background:linear-gradient(135deg,#4338CA,#1E1B4B); border-radius:14px; padding:24px; color:#fff; margin-bottom:16px; }
.card-bank { font-size:13px; opacity:.8; margin-bottom:22px; }
.card-number { font-family:'IBM Plex Mono',monospace; font-size:21px; letter-spacing:0.08em; margin-bottom:14px; }
.card-holder { font-size:13px; opacity:.85; }
.note-box { background:#FFFBEB; border:1px solid #FDE68A; border-radius:8px; padding:14px 16px; font-size:13px; color:#92400E; margin:18px 0; }
.note-box p { margin:4px 0; }
.success-box { background:#ECFDF5; border:1px solid #6EE7B7; color:#065F46; border-radius:8px; padding:14px 16px; font-size:14px; margin-bottom:20px; }

.orders-table-wrap { padding:24px 0 64px; overflow-x:auto; }
table.orders { width:100%; border-collapse:collapse; background:var(--surface); border-radius:var(--radius); overflow:hidden; }
table.orders th { background:var(--bg); color:var(--primary-dark); text-align:left; font-size:13px; padding:12px 14px; border-bottom:1px solid var(--line); }
table.orders td { padding:12px 14px; font-size:13px; border-bottom:1px solid var(--line); color:var(--text); }
table.orders tr:last-child td { border-bottom:none; }
.status-pill { background:var(--primary); color:#fff; padding:3px 10px; border-radius:999px; font-size:12px; }
.status-pill.done { background:#10B981; }
.status-pill.pending { background:var(--accent); }
.empty-state { color:var(--muted); padding:48px 0; text-align:center; }

@media (max-width:640px){ .tabs{overflow-x:auto; flex-wrap:nowrap;} }
"""

PAGE_HEAD = """<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{css}</style>
<script>
function toggleSidebar(force) {{
  var sb = document.getElementById('sidebar');
  var ov = document.getElementById('sidebar-overlay');
  var open = force !== undefined ? force : !sb.classList.contains('open');
  sb.classList.toggle('open', open);
  ov.classList.toggle('open', open);
}}
</script>""".format(css=PAGE_CSS)


def sidebar_html():
    return """
<div class="sidebar-overlay" id="sidebar-overlay" onclick="toggleSidebar(false)"></div>
<div class="sidebar" id="sidebar">
  <a href="/" class="sidebar-item">🛒 Yeni Sifariş</a>
  <a href="/orders" class="sidebar-item">📦 Sifarişlərim</a>
  <a href="/balance" class="sidebar-item">💳 Balans artır</a>
  <div class="divider"></div>
  <a href="/logout" class="sidebar-item">🚪 Çıxış</a>
</div>
"""


def topbar(user, balance=None):
    if user:
        bal = f'<span class="balance-chip">Balans: <strong>{balance:.2f} &#8380;</strong></span>' if balance is not None else ""
        right = f"""<div class="topright">{bal}<button class="hamburger" onclick="toggleSidebar()">&#9776;</button></div>"""
    else:
        right = ""
    return f"""
<header class="topbar">
  <div class="wrap topbar-inner">
    <a class="brand" href="/"><span class="brand-mark">&#9889;</span><span>{BRAND_NAME}</span></a>
    {right}
  </div>
</header>
{sidebar_html() if user else ""}
"""


# ===================== PAGE RENDERERS =====================
def render_login(error: str = "") -> str:
    err_html = f'<div class="login-err">{error}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="az"><head><title>Daxil ol — {BRAND_NAME}</title>{PAGE_HEAD}</head>
<body>
{topbar(None)}
<section class="login-wrap">
  <div class="login-card">
    <h2>{BRAND_NAME}-a xoş gəlmisiniz</h2>
    <p>Sifariş vermək üçün Google hesabınızla daxil olun.</p>
    {err_html}
    <a href="/login/google" class="google-btn">
      <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.9c1.7-1.57 2.7-3.88 2.7-6.62z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.9-2.26c-.8.54-1.84.86-3.06.86-2.35 0-4.34-1.59-5.05-3.72H.96v2.33A9 9 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.95 10.7A5.4 5.4 0 0 1 3.66 9c0-.59.1-1.17.29-1.7V4.97H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.03l2.99-2.33z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.5.46 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.97l2.99 2.33C4.66 5.17 6.65 3.58 9 3.58z"/></svg>
      Google ilə daxil ol
    </a>
  </div>
</section>
</body></html>"""


def render_home(categories: dict, all_products: list, user, balance) -> str:
    cat_names = list(categories.keys())
    tabs_html = '<a href="/" class="tab active">Hamısı</a>' + "".join(
        f'<a href="/?cat={c}" class="tab">{c}</a>' for c in cat_names
    )

    options = ""
    for c, products in categories.items():
        for p in products:
            options += f'<option value="{p["product_id"]}" data-price="{p["price"]}">{c} — {p["name"]} ({p["price"]} &#8380;/ədəd)</option>'

    if not all_products:
        body = '<p class="empty-state">Hələ aktiv xidmət yoxdur. Tezliklə əlavə olunacaq.</p>'
    else:
        body = f"""
        <div class="tabs">{tabs_html}</div>
        <form class="order-card" method="post" action="/order">
          <label class="field"><span>Xidmət</span>
            <select name="product_id" required>{options}</select>
          </label>
          <label class="field"><span>Link</span>
            <input type="text" name="profile_link" placeholder="https://..." required>
          </label>
          <label class="field"><span>Miqdar</span>
            <input type="number" name="quantity" value="1" min="1" required>
          </label>
          <p class="hint">Balansınızdan kifayət qədər vəsait yoxdursa, sifariş vermədən əvvəl "Balans artır"a yönləndiriləcəksiniz.</p>
          <button type="submit" class="submit-btn">Sifariş ver</button>
        </form>
        """

    return f"""<!DOCTYPE html>
<html lang="az"><head><title>{BRAND_NAME} — Sosial Media Artım Paneli</title>{PAGE_HEAD}</head>
<body>
{topbar(user, balance)}
<section class="hero wrap">
  <h1>Yeni sifariş</h1>
  <p>Xidməti seçin, linki yapışdırın, miqdarı yazın.</p>
</section>
<section class="wrap">{body}</section>
</body></html>"""


def render_balance(user, balance, sent=False) -> str:
    holder_html = f'<div class="card-holder">{CARD_HOLDER}</div>' if CARD_HOLDER else ""
    pretty_card = " ".join([CARD_NUMBER[i:i + 4] for i in range(0, len(CARD_NUMBER), 4)])
    success = '<div class="success-box">✅ Sorğunuz göndərildi, admin qəbzi yoxlayan kimi balansınız artırılacaq.</div>' if sent else ""
    return f"""<!DOCTYPE html>
<html lang="az"><head><title>Balans artır — {BRAND_NAME}</title>{PAGE_HEAD}</head>
<body>
{topbar(user, balance)}
<section class="balance-page wrap">
  <h1>Balans artır</h1>
  {success}
  <div class="card-display">
    <div class="card-bank">{CARD_BANK} kart</div>
    <div class="card-number">{pretty_card}</div>
    {holder_html}
  </div>
  <div class="note-box">
    <p>❗ Yuxarıdakı karta istədiyiniz məbləği köçürün.</p>
    <p>❗ Qəbz şəklini mütləq yükləyin, admin yoxlayandan sonra balansınız artırılır.</p>
    <p>❗ Minimum ödəniş: 1 ₼.</p>
  </div>
  <form method="post" action="/balance" enctype="multipart/form-data">
    <label class="field"><span>Köçürdüyünüz məbləğ (₼)</span>
      <input type="text" name="amount_sent" placeholder="məs. 10.00" required>
    </label>
    <label class="field"><span>Qəbz şəkli</span>
      <input type="file" name="receipt" accept="image/jpeg,image/jpg,image/png" required>
    </label>
    <button type="submit" class="submit-btn">Göndər</button>
  </form>
</section>
</body></html>"""


def render_orders(user, balance, orders: list) -> str:
    if not orders:
        body = '<p class="empty-state">Hələ heç bir sifarişiniz yoxdur. <a href="/" style="color:var(--primary);">Yeni sifariş ver →</a></p>'
    else:
        rows = ""
        for o in orders:
            status_class = "done" if o["status"] == "Tamamlandı" else "pending"
            rows += f"""<tr>
              <td>#{o['order_id']}</td>
              <td>{o['order_date'][:10] if o['order_date'] else '-'}</td>
              <td style="max-width:220px;word-break:break-all;">{o['profile_link']}</td>
              <td>{o['price']} &#8380;</td>
              <td>{o['quantity']}</td>
              <td>{o['product_name']}</td>
              <td><span class="status-pill {status_class}">{o['status']}</span></td>
            </tr>"""
        body = f"""
        <div class="orders-table-wrap">
        <table class="orders">
          <thead><tr><th>ID</th><th>Tarix</th><th>Link</th><th>Qiymət</th><th>Miqdar</th><th>Servis</th><th>Status</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="az"><head><title>Sifarişlərim — {BRAND_NAME}</title>{PAGE_HEAD}</head>
<body>
{topbar(user, balance)}
<section class="hero wrap"><h1>Sifarişlərim</h1></section>
<section class="wrap">{body}</section>
</body></html>"""


# ===================== AUTH ROUTES =====================
@app.get("/login")
def login_page(error: str = ""):
    return HTMLResponse(render_login(error))


@app.get("/login/google")
async def login_google(request: Request):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return RedirectResponse(
            url="/login?error=" + "Google girişi hələ qurulmayıb (GOOGLE_CLIENT_ID/SECRET Railway-də əlavə edilməyib)."
        )
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo") or {}
    email = userinfo.get("email", "")
    name = userinfo.get("name", email or "İstifadəçi")
    request.session["user"] = {"email": email, "name": name}
    ensure_panel_user(email, name)
    return RedirectResponse(url="/")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")


# ===================== STORE ROUTES =====================
@app.get("/", response_class=HTMLResponse)
def home(request: Request, cat: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/login")

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

    if cat and cat in by_category:
        by_category = {cat: by_category[cat]}

    return render_home(by_category, products, user, get_balance(user["email"]))


@app.post("/order")
def create_order(
    request: Request,
    product_id: int = Form(...),
    quantity: int = Form(...),
    profile_link: str = Form(...),
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

        total_price = float(product["price"]) * quantity
        balance = get_balance(user["email"])
        if balance < total_price:
            return RedirectResponse(url="/balance", status_code=status.HTTP_303_SEE_OTHER)

        c.execute(
            """INSERT INTO orders (customer_email, product_id, quantity, profile_link, price, status, order_date)
               VALUES (%s, %s, %s, %s, %s, 'Gözləmədə', %s) RETURNING order_id""",
            (user["email"], product_id, quantity, profile_link, total_price, datetime.now().isoformat()),
        )
        order_id = c.fetchone()["order_id"]
        c.execute("UPDATE panel_users SET balance = balance - %s WHERE email = %s", (total_price, user["email"]))
        conn.commit()
    finally:
        put_conn(conn)

    return RedirectResponse(url="/orders", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/balance", response_class=HTMLResponse)
def balance_page(request: Request, sent: int = 0):
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    return render_balance(user, get_balance(user["email"]), sent=bool(sent))


@app.post("/balance")
async def submit_balance(
    request: Request,
    amount_sent: str = Form(...),
    receipt: UploadFile = File(...),
):
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/login")

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO topups (customer_email, amount_sent, status, topup_date) VALUES (%s, %s, 'Yoxlanılır', %s)",
            (user["email"], amount_sent, datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        put_conn(conn)

    receipt_bytes = await receipt.read()
    if BOT_TOKEN and ADMIN_TELEGRAM_ID:
        caption = (
            f"💳 Yeni balans artırma sorğusu\n"
            f"👤 {user['name']} ({user['email']})\n"
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
            pass

    return RedirectResponse(url="/balance?sent=1", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/login")

    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute(
            """SELECT o.*, p.name AS product_name
               FROM orders o JOIN products p ON o.product_id = p.product_id
               WHERE o.customer_email = %s ORDER BY o.order_id DESC""",
            (user["email"],),
        )
        orders = c.fetchall()
    finally:
        put_conn(conn)

    return render_orders(user, get_balance(user["email"]), orders)


# ===================== ADMIN (sadə, parol qorumalı) =====================
@app.get("/admin/credit", response_class=HTMLResponse)
def admin_credit_form(password: str = ""):
    if not ADMIN_PANEL_PASSWORD or password != ADMIN_PANEL_PASSWORD:
        return HTMLResponse("<p style='font-family:sans-serif;padding:40px;'>403 — yanlış parol. URL-ə ?password=... əlavə edin.</p>", status_code=403)
    return HTMLResponse(f"""<!DOCTYPE html><html><head>{PAGE_HEAD}</head><body style="padding:40px;">
    <h2>Balans əlavə et (admin)</h2>
    <form method="post" action="/admin/credit?password={password}">
      <label class="field"><span>İstifadəçi email</span><input type="email" name="email" required></label>
      <label class="field"><span>Əlavə ediləcək məbləğ (₼)</span><input type="text" name="amount" required></label>
      <button class="submit-btn" type="submit">Əlavə et</button>
    </form>
    </body></html>""")


@app.post("/admin/credit")
def admin_credit_submit(password: str, email: str = Form(...), amount: str = Form(...)):
    if not ADMIN_PANEL_PASSWORD or password != ADMIN_PANEL_PASSWORD:
        raise HTTPException(status_code=403, detail="Yanlış parol")

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO panel_users (email, name, balance) VALUES (%s, %s, %s) "
            "ON CONFLICT (email) DO UPDATE SET balance = panel_users.balance + EXCLUDED.balance",
            (email, email, float(amount)),
        )
        conn.commit()
    finally:
        put_conn(conn)

    return RedirectResponse(url=f"/admin/credit?password={password}", status_code=status.HTTP_303_SEE_OTHER)

