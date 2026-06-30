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
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/boostpanel")
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-secret-deyisin")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "dev-admin-key-deyisin")  # /admin/* səhifələrinə giriş üçün
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
                customer_name TEXT,
                product_id INTEGER,
                quantity INTEGER NOT NULL,
                profile_link TEXT,
                amount_sent TEXT,
                status TEXT DEFAULT 'Gözləmədə',
                order_date TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                name TEXT,
                balance NUMERIC NOT NULL DEFAULT 0
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS topups (
                topup_id SERIAL PRIMARY KEY,
                user_email TEXT,
                amount NUMERIC NOT NULL,
                status TEXT DEFAULT 'Yoxlanılır',
                created_at TEXT
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


def ensure_user_row(email: str, name: str):
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO users (email, name, balance) VALUES (%s, %s, 0) "
            "ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name",
            (email, name),
        )
        conn.commit()
    finally:
        put_conn(conn)


def get_balance(email: str) -> float:
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT balance FROM users WHERE email = %s", (email,))
        row = c.fetchone()
        return float(row["balance"]) if row else 0.0
    finally:
        put_conn(conn)


# ===================== DESIGN =====================
PAGE_CSS = """
:root { --ink:#10182B; --paper:#FFFFFF; --bg:#EEF2FB; --brand:#3B4CCB; --brand-dark:#2532A6; --accent:#FF6A3D; --muted:#6B7280; --line:#E3E8F5; --radius:10px; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font-family:'Space Grotesk',sans-serif; -webkit-font-smoothing:antialiased; }
.wrap { max-width:1080px; margin:0 auto; padding:0 24px; }
a { color:inherit; text-decoration:none; }
.topbar { background:var(--paper); border-bottom:1px solid var(--line); position:sticky; top:0; z-index:30; }
.topbar-inner { display:flex; justify-content:space-between; align-items:center; height:72px; max-width:1080px; margin:0 auto; padding:0 24px; }
.brand { display:flex; align-items:center; gap:8px; font-weight:700; font-size:20px; }
.brand-mark { color:var(--brand); font-size:24px; }
.brand-name span:first-child { color:var(--ink); }
.brand-name span:last-child { color:var(--brand); }
.burger { width:40px; height:40px; border:none; background:transparent; cursor:pointer; display:flex; flex-direction:column; justify-content:center; gap:5px; }
.burger span { display:block; height:2px; background:var(--ink); border-radius:2px; }
.login-btn { background:transparent; border:1px solid var(--brand); color:var(--brand); font-weight:600; padding:9px 18px; border-radius:var(--radius); font-size:14px; }

/* slide-out menu */
.menu-overlay { position:fixed; inset:0; background:rgba(16,24,43,0.45); display:none; z-index:40; }
.menu-overlay.open { display:block; }
.side-menu { position:fixed; top:0; right:0; height:100%; width:300px; max-width:85vw; background:#fff; z-index:50; transform:translateX(100%); transition:transform .25s ease; padding:24px 0; overflow-y:auto; }
.side-menu.open { transform:translateX(0); }
.side-menu a, .side-menu .menu-balance { display:flex; align-items:center; gap:12px; padding:14px 24px; font-size:15px; color:var(--ink); font-weight:500; }
.side-menu a:hover { background:var(--bg); }
.side-menu .menu-balance { font-weight:700; background:var(--brand); color:#fff; margin-bottom:8px; }
.side-menu hr { border:none; border-top:1px solid var(--line); margin:8px 0; }

.balance-bar { background:var(--brand); color:#fff; text-align:center; padding:14px; font-weight:700; font-size:17px; }

.hero { padding:64px 0 40px; text-align:center; }
.hero h1 { font-size:clamp(28px,5vw,44px); margin:0 0 14px; font-weight:700; }
.hero p { color:var(--muted); max-width:560px; margin:0 auto; font-size:16px; line-height:1.6; }

.login-wrap { padding:48px 24px 64px; display:flex; justify-content:center; }
.login-card { background:#fff; border:1px solid var(--line); border-radius:14px; padding:36px; max-width:400px; width:100%; box-shadow:0 10px 30px rgba(16,24,43,0.06); }
.login-card h2 { margin:0 0 8px; font-size:21px; }
.login-card p.sub { color:var(--muted); font-size:14px; margin:0 0 24px; }
.field { display:flex; flex-direction:column; gap:6px; font-size:13px; color:#3a3f4d; margin-bottom:16px; }
.field input, .field select { background:#fff; border:1px solid var(--line); border-radius:var(--radius); padding:11px 12px; color:var(--ink); font-family:'Space Grotesk',sans-serif; font-size:14px; }
.field input:focus { outline:2px solid var(--brand); outline-offset:1px; }
.btn-primary { width:100%; background:var(--brand); color:#fff; border:none; border-radius:var(--radius); padding:13px 14px; font-weight:600; font-size:15px; cursor:pointer; }
.btn-primary:hover { background:var(--brand-dark); }
.google-btn { display:flex; align-items:center; justify-content:center; gap:10px; background:#fff; border:1px solid var(--line); color:#1F1F1F; font-weight:600; padding:12px 18px; border-radius:var(--radius); font-size:14px; width:100%; margin-top:4px; }
.divider { text-align:center; color:var(--muted); font-size:12px; margin:18px 0; }
.login-card .switch { text-align:center; font-size:13px; color:var(--muted); margin-top:18px; }
.login-card .switch a { color:var(--brand); font-weight:600; }

.perks { display:grid; grid-template-columns:repeat(4,1fr); gap:16px; padding:0 0 56px; max-width:1080px; margin:0 auto; }
.perk { background:#fff; border:1px solid var(--line); border-radius:12px; padding:22px; }
.perk h3 { margin:0 0 6px; font-size:15px; }
.perk p { margin:0; color:var(--muted); font-size:13px; line-height:1.5; }

.catalog { padding:24px 0 56px; }
.cat-block { margin-bottom:40px; }
.cat-title { font-size:19px; margin:0 0 16px; font-weight:700; }
.cat-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:14px; }
.product-card { background:#fff; border:1px solid var(--line); border-radius:12px; padding:18px; display:flex; flex-direction:column; gap:8px; }
.product-top { display:flex; justify-content:space-between; align-items:baseline; gap:12px; }
.product-top h3 { margin:0; font-size:15px; }
.price { color:var(--brand); font-weight:700; font-size:14px; white-space:nowrap; }
.product-desc { margin:0; font-size:13px; color:var(--muted); }
.empty-state { color:var(--muted); text-align:center; padding:48px 0; }

.order-form-section { padding:24px 0 56px; max-width:680px; margin:0 auto; }
.platform-tabs { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:20px; }
.platform-tab { background:#fff; border:1px solid var(--line); border-radius:var(--radius); padding:12px 16px; font-size:14px; font-weight:600; }
.platform-tab.active { background:var(--brand); color:#fff; border-color:var(--brand); }
.form-card { background:#fff; border:1px solid var(--line); border-radius:14px; padding:24px; }
.hint { font-size:12px; color:var(--muted); margin:4px 0 0; }

.tabs-row { display:flex; gap:8px; overflow-x:auto; padding:20px 0 4px; }
.tab-btn { white-space:nowrap; padding:10px 16px; border-radius:var(--radius); font-size:14px; font-weight:600; background:#fff; border:1px solid var(--line); }
.tab-btn.active { background:var(--brand); color:#fff; border-color:var(--brand); }
.search-bar { display:flex; gap:8px; margin:16px 0 20px; }
.search-bar input { flex:1; border:1px solid var(--line); border-radius:var(--radius); padding:11px 14px; font-size:14px; }
.search-bar button { background:var(--brand); color:#fff; border:none; border-radius:var(--radius); padding:0 18px; font-weight:600; }

table.orders-table { width:100%; border-collapse:collapse; background:#fff; border-radius:12px; overflow:hidden; border:1px solid var(--line); }
table.orders-table th { text-align:left; background:var(--bg); padding:12px 14px; font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
table.orders-table td { padding:14px; font-size:13px; border-top:1px solid var(--line); vertical-align:top; }
.status-pill { background:#E6F4EA; color:#1E7E34; padding:3px 10px; border-radius:999px; font-size:12px; font-weight:600; white-space:nowrap; }
.status-pill.pending { background:#FFF4E0; color:#B36B00; }
.status-pill.cancelled { background:#FBE5E3; color:#C0392B; }

.balance-page { padding:32px 0 64px; max-width:520px; margin:0 auto; }
.balance-page h1 { font-size:22px; margin:0 0 4px; }
.balance-page .sub { color:var(--muted); font-size:14px; margin:0 0 24px; }
.bank-tabs { display:flex; gap:10px; margin-bottom:20px; flex-wrap:wrap; }
.bank-tab { padding:10px 16px; border-radius:999px; border:1px solid var(--line); background:#fff; font-size:13px; font-weight:600; }
.bank-tab.active { background:var(--ink); color:#fff; border-color:var(--ink); }
.card-display { background:linear-gradient(135deg,var(--brand),var(--brand-dark)); border-radius:14px; padding:24px; color:#fff; margin-bottom:8px; position:relative; }
.card-bank { font-size:13px; opacity:.8; margin-bottom:22px; }
.card-number { font-size:21px; letter-spacing:0.06em; margin-bottom:14px; font-weight:600; }
.card-holder { font-size:13px; opacity:.85; }
.copy-btn { position:absolute; top:20px; right:20px; background:rgba(255,255,255,0.18); border:none; color:#fff; padding:7px 14px; border-radius:8px; font-size:12px; font-weight:600; cursor:pointer; }
.copy-hint { font-size:12px; color:var(--muted); margin:10px 0 0; }
.notice-box { background:#FFF9E8; border:1px solid #F4E2A4; border-radius:10px; padding:16px 18px; font-size:13px; color:#7A5C00; margin-top:24px; }
.notice-box p { margin:6px 0; }

.footer { padding:32px 0 48px; color:var(--muted); font-size:13px; border-top:1px solid var(--line); text-align:center; }
@media (max-width:640px){ .cat-grid{grid-template-columns:1fr;} .perks{grid-template-columns:1fr 1fr;} }
"""

PAGE_HEAD = """<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{css}</style>""".format(css=PAGE_CSS)

MENU_JS = """
<script>
function toggleMenu(){
  document.getElementById('sideMenu').classList.toggle('open');
  document.getElementById('menuOverlay').classList.toggle('open');
}
function copyCard(){
  var t = document.getElementById('cardNum').innerText.replace(/\\s/g,'');
  navigator.clipboard && navigator.clipboard.writeText(t);
}
</script>
"""


def side_menu_html(user, balance=0):
    if user:
        items = f"""
        <div class="menu-balance">💰 Balans: {balance:.2f} ₼</div>
        <a href="/order">🛒 Yeni Sifariş</a>
        <a href="/orders">📦 Sifarişlərim</a>
        <a href="/balance">💳 Balans artır</a>
        <a href="/#xidmetler">📋 Servislər</a>
        <hr>
        <a href="/logout">🚪 Çıxış</a>
        """
    else:
        items = """
        <a href="/login">🔑 Daxil ol</a>
        <a href="/#xidmetler">📋 Xidmətlər</a>
        """
    return f"""
<div class="menu-overlay" id="menuOverlay" onclick="toggleMenu()"></div>
<div class="side-menu" id="sideMenu">
  {items}
</div>
"""


def topbar(user, balance=0):
    if user:
        right = '<button class="burger" onclick="toggleMenu()"><span></span><span></span><span></span></button>'
    else:
        right = '<a href="/login" class="login-btn">Daxil ol</a>'
    return f"""
<header class="topbar">
  <div class="topbar-inner">
    <a class="brand" href="/"><span class="brand-mark">&#9670;</span>
      <span class="brand-name"><span>Boost</span><span>Panel</span></span>
    </a>
    {right}
  </div>
</header>
{side_menu_html(user, balance)}
"""


# ===================== PAGE RENDERERS =====================
def render_login() -> str:
    return f"""<!DOCTYPE html>
<html lang="az"><head><title>Daxil ol — {BRAND_NAME}</title>{PAGE_HEAD}</head>
<body>
{topbar(None)}
<section class="login-wrap">
  <div class="login-card">
    <h2>Hesabınıza daxil olun</h2>
    <p class="sub">Sifariş vermək və balansınızı izləmək üçün daxil olun.</p>
    <a href="/login/google" class="google-btn">
      <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.9c1.7-1.57 2.7-3.88 2.7-6.62z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.9-2.26c-.8.54-1.84.86-3.06.86-2.35 0-4.34-1.59-5.05-3.72H.96v2.33A9 9 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.95 10.7A5.4 5.4 0 0 1 3.66 9c0-.59.1-1.17.29-1.7V4.97H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.03l2.99-2.33z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.5.46 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.97l2.99 2.33C4.66 5.17 6.65 3.58 9 3.58z"/></svg>
      Google ilə daxil ol
    </a>
  </div>
</section>
{MENU_JS}
</body></html>"""


def render_home(categories: dict, user) -> str:
    bal = get_balance(user["email"]) if user else 0
    if user:
        balance_bar = f'<div class="balance-bar">Balansınız: {bal:.2f} ₼ &nbsp;·&nbsp; <a href="/balance" style="text-decoration:underline;">Artır</a></div>'
        hero = ""
    else:
        balance_bar = ""
        hero = """
        <section class="hero wrap">
          <h1>Sosial media hesabınızı sürətlə böyüdün</h1>
          <p>TikTok, Instagram, Telegram və YouTube üçün takipçi, bəyəni və izlənmə xidmətləri — sürətli başlanğıc, izlənə bilən sifariş statusu.</p>
        </section>
        <div class="login-wrap" style="padding-top:8px;">
          <div class="login-card" style="text-align:center;">
            <h2>Başlamaq üçün daxil olun</h2>
            <p class="sub">Sifariş vermək üçün Google hesabınızla qoşulun.</p>
            <a href="/login/google" class="google-btn">
              <svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.9c1.7-1.57 2.7-3.88 2.7-6.62z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.9-2.26c-.8.54-1.84.86-3.06.86-2.35 0-4.34-1.59-5.05-3.72H.96v2.33A9 9 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.95 10.7A5.4 5.4 0 0 1 3.66 9c0-.59.1-1.17.29-1.7V4.97H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.03l2.99-2.33z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.5.46 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.97l2.99 2.33C4.66 5.17 6.65 3.58 9 3.58z"/></svg>
              Google ilə daxil ol
            </a>
          </div>
        </div>
        <div class="perks wrap">
          <div class="perk"><h3>Sürətli başlanğıc</h3><p>Sifarişlər adətən qısa müddətdə işə düşür.</p></div>
          <div class="perk"><h3>İzlənə bilən status</h3><p>Hər sifarişin gedişatını "Sifarişlərim" bölməsindən izləyin.</p></div>
          <div class="perk"><h3>Sadə balans sistemi</h3><p>Bir dəfə balans artırın, istədiyiniz qədər sifariş verin.</p></div>
          <div class="perk"><h3>Dəstək</h3><p>Suallarınız üçün bizimlə əlaqə saxlayın.</p></div>
        </div>
        """

    if categories:
        blocks = []
        for category, products in categories.items():
            cards = []
            for p in products:
                desc = f'<p class="product-desc">{p["description"]}</p>' if p.get("description") else ""
                action = "/order" if user else "/login"
                cards.append(f"""
                <a href="{action}" class="product-card">
                  <div class="product-top"><h3>{p['name']}</h3><span class="price">{p['price']} &#8380;</span></div>
                  {desc}
                </a>""")
            blocks.append(f"""<div class="cat-block"><h2 class="cat-title">{category}</h2><div class="cat-grid">{''.join(cards)}</div></div>""")
        catalog_html = "".join(blocks)
    else:
        catalog_html = '<p class="empty-state">Hələ aktiv xidmət yoxdur. Tezliklə əlavə olunacaq.</p>'

    return f"""<!DOCTYPE html>
<html lang="az"><head><title>{BRAND_NAME} — Sosial Media Artım Paneli</title>{PAGE_HEAD}</head>
<body>
{topbar(user, bal)}
{balance_bar}
{hero}
<section id="xidmetler" class="catalog wrap">{catalog_html}</section>
<footer class="footer wrap"><p>&copy; 2026 {BRAND_NAME}</p></footer>
{MENU_JS}
</body></html>"""


def render_order_form(categories: dict, user, selected_product=None) -> str:
    bal = get_balance(user["email"])
    options = []
    for category, products in categories.items():
        opts = "".join(
            f'<option value="{p["product_id"]}" {"selected" if selected_product and selected_product["product_id"]==p["product_id"] else ""}>{p["name"]} — {p["price"]} ₼</option>'
            for p in products
        )
        options.append(f'<optgroup label="{category}">{opts}</optgroup>')
    select_html = "".join(options) if options else '<option disabled>Xidmət yoxdur</option>'

    return f"""<!DOCTYPE html>
<html lang="az"><head><title>Yeni Sifariş — {BRAND_NAME}</title>{PAGE_HEAD}</head>
<body>
{topbar(user, bal)}
<div class="balance-bar">Balansınız: {bal:.2f} ₼ &nbsp;·&nbsp; <a href="/balance" style="text-decoration:underline;">Artır</a></div>
<section class="order-form-section wrap">
  <h1 style="font-size:22px;margin:24px 0 18px;">Yeni Sifariş</h1>
  <div class="form-card">
    <form method="post" action="/order">
      <label class="field"><span>Xidmət</span>
        <select name="product_id" required>{select_html}</select>
      </label>
      <label class="field"><span>Link</span>
        <input type="text" name="profile_link" placeholder="https://..." required>
      </label>
      <label class="field"><span>Miqdar</span>
        <input type="number" name="quantity" value="1" min="1" required>
      </label>
      <button type="submit" class="btn-primary">Sifariş ver</button>
      <p class="hint">Sifariş vermədən əvvəl balansınızın kifayət qədər olduğuna əmin olun.</p>
    </form>
  </div>
</section>
<footer class="footer wrap"><p>&copy; 2026 {BRAND_NAME}</p></footer>
{MENU_JS}
</body></html>"""


def render_balance_page(user, history) -> str:
    bal = get_balance(user["email"])
    pretty_card = " ".join([CARD_NUMBER[i:i + 4] for i in range(0, len(CARD_NUMBER), 4)])
    holder_html = f'<div class="card-holder">{CARD_HOLDER}</div>' if CARD_HOLDER else ""

    rows = "".join(
        f"""<tr><td>#{t['topup_id']}</td><td>{t['created_at'][:16]}</td><td>{t['amount']} ₼</td>
        <td><span class="status-pill {'pending' if t['status']=='Yoxlanılır' else ('cancelled' if t['status']=='Rədd edildi' else '')}">{t['status']}</span></td></tr>"""
        for t in history
    ) or '<tr><td colspan="4" class="empty-state">Hələ balans artırma sorğunuz yoxdur.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="az"><head><title>Balans artır — {BRAND_NAME}</title>{PAGE_HEAD}</head>
<body>
{topbar(user, bal)}
<section class="balance-page wrap">
  <h1>Balans artır</h1>
  <p class="sub">Cari balansınız: <strong>{bal:.2f} ₼</strong></p>

  <div class="bank-tabs">
    <span class="bank-tab active">{CARD_BANK}</span>
  </div>

  <div class="card-display">
    <button class="copy-btn" onclick="copyCard()">Kopyala</button>
    <div class="card-bank">{CARD_BANK} kart</div>
    <div class="card-number" id="cardNum">{pretty_card}</div>
    {holder_html}
  </div>
  <p class="copy-hint">Yuxarıdakı karta məbləği köçürün, sonra aşağıdakı formu doldurub qəbz şəklini yükləyin. Admin təsdiqlədikdən sonra balansınıza əlavə olunacaq.</p>

  <div class="form-card" style="margin-top:24px;">
    <form method="post" action="/balance/topup" enctype="multipart/form-data">
      <label class="field"><span>Məbləğ (₼)</span>
        <input type="number" step="0.01" min="1" name="amount" placeholder="Məs: 10.00" required>
      </label>
      <label class="field"><span>Qəbz şəkli</span>
        <input type="file" name="receipt" accept="image/jpeg,image/jpg,image/png" required>
      </label>
      <button type="submit" class="btn-primary">Göndər</button>
    </form>
  </div>

  <div class="notice-box">
    <p>⚠️ Ödəniş çekini mütləq yükləyin.</p>
    <p>⚠️ Balans, admin sorğunu təsdiqlədikdən sonra hesabınıza əlavə olunur.</p>
    <p>⚠️ Minimum ödəniş: 1 ₼.</p>
  </div>

  <h2 style="font-size:17px;margin:32px 0 12px;">Ödəniş Tarixçəsi</h2>
  <table class="orders-table">
    <thead><tr><th>ID</th><th>Tarix</th><th>Məbləğ</th><th>Status</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</section>
<footer class="footer wrap"><p>&copy; 2026 {BRAND_NAME}</p></footer>
{MENU_JS}
</body></html>"""


def render_orders_page(user, orders, active_tab="all") -> str:
    bal = get_balance(user["email"])
    tabs = [
        ("all", "Bütün Sifarişlər"),
        ("Gözləmədə", "Gözləmədə"),
        ("Yüklənir", "Yüklənir"),
        ("Tamamlandı", "Tamamlandı"),
        ("Ləğv edildi", "Ləğv Edildi"),
    ]
    tabs_html = "".join(
        f'<a href="/orders?status={key}" class="tab-btn {"active" if key==active_tab else ""}">{label}</a>'
        for key, label in tabs
    )

    if not orders:
        body = '<p class="empty-state">Hələ heç bir sifarişiniz yoxdur. <a href="/order" style="color:var(--brand);font-weight:600;">İlk sifarişinizi verin →</a></p>'
    else:
        rows = "".join(
            f"""<tr>
              <td>#{o['order_id']}</td>
              <td>{o['order_date'][:16] if o['order_date'] else ''}</td>
              <td style="max-width:160px;word-break:break-all;">{o['profile_link']}</td>
              <td>{o['product_name']}</td>
              <td>{o['quantity']}</td>
              <td><span class="status-pill {'pending' if o['status'] not in ('Tamamlandı',) else ''}">{o['status']}</span></td>
            </tr>"""
            for o in orders
        )
        body = f"""
        <table class="orders-table">
          <thead><tr><th>ID</th><th>Tarix</th><th>Link</th><th>Servis</th><th>Miqdar</th><th>Status</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
        """

    return f"""<!DOCTYPE html>
<html lang="az"><head><title>Sifarişlərim — {BRAND_NAME}</title>{PAGE_HEAD}</head>
<body>
{topbar(user, bal)}
<div class="balance-bar">Balansınız: {bal:.2f} ₼</div>
<section class="wrap">
  <h1 style="font-size:22px;margin:24px 0 4px;">Sifarişlərim</h1>
  <div class="tabs-row">{tabs_html}</div>
  <div class="search-bar">
    <form method="get" action="/orders" style="display:flex;gap:8px;width:100%;">
      <input type="text" name="q" placeholder="Axtarış...">
      <button type="submit">Axtar</button>
    </form>
  </div>
  {body}
</section>
<footer class="footer wrap"><p>&copy; 2026 {BRAND_NAME}</p></footer>
{MENU_JS}
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
    email = userinfo.get("email", "")
    name = userinfo.get("name", email or "İstifadəçi")
    request.session["user"] = {"email": email, "name": name}
    ensure_user_row(email, name)
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


@app.get("/order", response_class=HTMLResponse)
def order_form_page(request: Request):
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

    return render_order_form(by_category, user)


@app.post("/order")
def submit_order(
    request: Request,
    product_id: int = Form(...),
    profile_link: str = Form(...),
    quantity: int = Form(...),
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
            raise HTTPException(status_code=400, detail="Balansınız kifayət deyil. Balansı artırın.")

        c.execute(
            """INSERT INTO orders (customer_email, customer_name, product_id, quantity, profile_link, amount_sent, status, order_date)
               VALUES (%s, %s, %s, %s, %s, %s, 'Gözləmədə', %s) RETURNING order_id""",
            (user["email"], user["name"], product_id, quantity, profile_link, str(total_price), datetime.now().isoformat()),
        )
        order_id = c.fetchone()["order_id"]
        c.execute("UPDATE users SET balance = balance - %s WHERE email = %s", (total_price, user["email"]))
        conn.commit()
    finally:
        put_conn(conn)

    return RedirectResponse(url=f"/thanks/{order_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, status: str = "all", q: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/login")

    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        query = """SELECT o.*, p.name AS product_name FROM orders o
                   JOIN products p ON o.product_id = p.product_id
                   WHERE o.customer_email = %s"""
        params = [user["email"]]
        if status != "all":
            query += " AND o.status = %s"
            params.append(status)
        if q:
            query += " AND o.profile_link ILIKE %s"
            params.append(f"%{q}%")
        query += " ORDER BY o.order_id DESC"
        c.execute(query, tuple(params))
        orders = c.fetchall()
    finally:
        put_conn(conn)

    return render_orders_page(user, orders, active_tab=status)


@app.get("/balance", response_class=HTMLResponse)
def balance_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/login")

    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM topups WHERE user_email = %s ORDER BY topup_id DESC", (user["email"],))
        history = c.fetchall()
    finally:
        put_conn(conn)

    return render_balance_page(user, history)


@app.post("/balance/topup")
async def submit_topup(
    request: Request,
    amount: float = Form(...),
    receipt: UploadFile = File(...),
):
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/login")

    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute(
            "INSERT INTO topups (user_email, amount, status, created_at) VALUES (%s, %s, 'Yoxlanılır', %s) RETURNING topup_id",
            (user["email"], amount, datetime.now().isoformat()),
        )
        topup_id = c.fetchone()["topup_id"]
        conn.commit()
    finally:
        put_conn(conn)

    receipt_bytes = await receipt.read()
    if BOT_TOKEN and ADMIN_TELEGRAM_ID:
        caption = (
            f"💳 Balans artırma sorğusu #{topup_id}\n"
            f"👤 {user['name']} ({user['email']})\n"
            f"💰 Məbləğ: {amount} ₼\n"
            f"Təsdiq üçün: /admin/topups?key=...&id={topup_id}&action=approve"
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

    return RedirectResponse(url="/balance", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/thanks/{order_id}", response_class=HTMLResponse)
def thanks_page(request: Request, order_id: int):
    user = current_user(request)
    bal = get_balance(user["email"]) if user else 0
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="az"><head><title>Sifariş göndərildi — {BRAND_NAME}</title>{PAGE_HEAD}</head>
<body>
{topbar(user, bal)}
<section class="balance-page wrap">
  <p class="sub">SİFARİŞ #{order_id}</p>
  <h1>Qəbul edildi</h1>
  <p class="sub">Sifarişiniz qısa zamanda işə düşəcək. Statusunu "Sifarişlərim" bölməsindən izləyə bilərsiniz.</p>
  <a href="/orders" class="btn-primary" style="display:inline-block;width:auto;padding:12px 24px;">Sifarişlərimə bax</a>
</section>
{MENU_JS}
</body></html>""")


# ===================== ADMIN (sadə təsdiq paneli) =====================
@app.get("/admin/topups", response_class=HTMLResponse)
def admin_topups(key: str = "", action: str = "", id: int = 0):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Yetkiniz yoxdur")

    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        if action == "approve" and id:
            c.execute("SELECT * FROM topups WHERE topup_id = %s", (id,))
            t = c.fetchone()
            if t and t["status"] == "Yoxlanılır":
                c.execute("UPDATE topups SET status = 'Təsdiqləndi' WHERE topup_id = %s", (id,))
                c.execute("UPDATE users SET balance = balance + %s WHERE email = %s", (t["amount"], t["user_email"]))
                conn.commit()
        elif action == "reject" and id:
            c.execute("UPDATE topups SET status = 'Rədd edildi' WHERE topup_id = %s", (id,))
            conn.commit()

        c.execute("SELECT * FROM topups ORDER BY topup_id DESC LIMIT 50")
        rows = c.fetchall()
    finally:
        put_conn(conn)

    rows_html = "".join(
        f"""<tr><td>#{t['topup_id']}</td><td>{t['user_email']}</td><td>{t['amount']} ₼</td><td>{t['status']}</td>
        <td><a href="/admin/topups?key={key}&action=approve&id={t['topup_id']}">Təsdiqlə</a> |
            <a href="/admin/topups?key={key}&action=reject&id={t['topup_id']}">Rədd et</a></td></tr>"""
        for t in rows
    )
    return HTMLResponse(f"""<!DOCTYPE html><html><head>{PAGE_HEAD}</head><body class="wrap">
    <h1>Balans sorğuları</h1>
    <table class="orders-table"><thead><tr><th>ID</th><th>Email</th><th>Məbləğ</th><th>Status</th><th>Əməliyyat</th></tr></thead>
    <tbody>{rows_html}</tbody></table></body></html>""")

