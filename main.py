import os
from datetime import datetime

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Form, HTTPException, status
from fastapi.responses import RedirectResponse, HTMLResponse

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/eren_smm")

app = FastAPI(title="SMM Panel")

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
            """CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0,
                vip_status BOOLEAN DEFAULT FALSE,
                registration_date TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS products (
                product_id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                price INTEGER NOT NULL,
                stock INTEGER NOT NULL,
                vip_only BOOLEAN DEFAULT FALSE,
                description TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                order_id SERIAL PRIMARY KEY,
                user_id BIGINT,
                product_id INTEGER,
                quantity INTEGER NOT NULL,
                status TEXT DEFAULT 'Beklemede',
                order_date TEXT,
                profile_link TEXT
            )"""
        )
        conn.commit()
    finally:
        put_conn(conn)


@app.on_event("startup")
def on_startup():
    init_db()


# ===================== DESIGN: CSS (inline, no static folder needed) =====================
PAGE_CSS = """
:root {
  --ink: #0E1116; --paper: #F6F4EE; --acid: #C8FF4D; --coral: #FF5C39;
  --muted: #6B7280; --card: #161A22; --radius: 4px;
}
* { box-sizing: border-box; }
body { margin:0; background:var(--ink); color:var(--paper); font-family:'Space Grotesk',sans-serif; -webkit-font-smoothing:antialiased; }
.wrap { max-width:1080px; margin:0 auto; padding:0 24px; }
a { color:inherit; text-decoration:none; }
.topbar { border-bottom:1px solid #262B36; position:sticky; top:0; background:rgba(14,17,22,0.92); backdrop-filter:blur(6px); z-index:10; }
.topbar-inner { display:flex; justify-content:space-between; align-items:center; height:64px; }
.brand { display:flex; align-items:center; gap:8px; font-weight:700; letter-spacing:0.04em; }
.brand-mark { color:var(--acid); font-size:22px; }
.topnav { display:flex; gap:28px; font-size:14px; color:#B7BCC6; }
.topnav a:hover { color:var(--acid); }
.hero { position:relative; padding:88px 0 0; overflow:hidden; }
.eyebrow { font-family:'IBM Plex Mono',monospace; font-size:12px; letter-spacing:0.18em; color:var(--acid); margin:0 0 16px; }
.hero h1 { font-size:clamp(36px,6vw,64px); line-height:1.04; margin:0 0 20px; font-weight:700; letter-spacing:-0.01em; }
.hero-sub { max-width:520px; color:#B7BCC6; font-size:17px; line-height:1.6; margin:0 0 32px; }
.cta { display:inline-block; background:var(--acid); color:var(--ink); font-weight:600; padding:14px 24px; border-radius:var(--radius); font-size:15px; }
.hero-tape { margin-top:72px; border-top:1px solid #262B36; border-bottom:1px solid #262B36; overflow:hidden; background:var(--coral); }
.tape-track { display:flex; white-space:nowrap; font-family:'IBM Plex Mono',monospace; font-weight:500; font-size:14px; color:var(--ink); padding:10px 0; animation:scroll 22s linear infinite; width:max-content; }
.tape-track span { padding:0 10px; }
@keyframes scroll { from{transform:translateX(0);} to{transform:translateX(-50%);} }
@media (prefers-reduced-motion: reduce) { .tape-track { animation:none; } }
.catalog { padding:72px 0; }
.cat-block { margin-bottom:56px; }
.cat-title { font-size:22px; margin:0 0 20px; padding-bottom:12px; border-bottom:1px solid #262B36; }
.cat-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:16px; }
.product-card { background:var(--card); border:1px solid #262B36; border-radius:var(--radius); padding:20px; display:flex; flex-direction:column; gap:12px; }
.product-top { display:flex; justify-content:space-between; align-items:baseline; gap:12px; }
.product-top h3 { margin:0; font-size:16px; }
.price { font-family:'IBM Plex Mono',monospace; color:var(--acid); font-size:14px; white-space:nowrap; }
.product-desc { margin:0; font-size:13px; color:var(--muted); }
.field { display:flex; flex-direction:column; gap:4px; font-size:12px; color:#B7BCC6; }
.field input { background:var(--ink); border:1px solid #2D3340; border-radius:var(--radius); padding:9px 10px; color:var(--paper); font-family:'Space Grotesk',sans-serif; font-size:14px; }
.field input:focus { outline:2px solid var(--acid); outline-offset:1px; }
.order-btn { margin-top:4px; background:var(--coral); color:var(--ink); border:none; border-radius:var(--radius); padding:10px 14px; font-weight:600; font-size:14px; cursor:pointer; font-family:'Space Grotesk',sans-serif; }
.empty-state { color:var(--muted); }
.howto { padding:0 0 88px; }
.howto h2 { font-size:28px; margin:0 0 32px; }
.howto-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:24px; }
.step-no { font-family:'IBM Plex Mono',monospace; color:var(--coral); font-size:13px; }
.howto-step h3 { margin:8px 0 6px; font-size:17px; }
.howto-step p { margin:0; color:var(--muted); font-size:14px; line-height:1.55; }
@media (max-width:640px) { .howto-grid { grid-template-columns:1fr; } .topnav { display:none; } }
.status-page { padding:72px 0 96px; max-width:600px; }
.status-page h1 { font-size:30px; margin:8px 0 28px; }
.status-card { background:var(--card); border:1px solid #262B36; border-radius:var(--radius); padding:8px 20px; margin-bottom:28px; }
.status-row { display:flex; justify-content:space-between; gap:12px; padding:14px 0; border-bottom:1px solid #21262F; font-size:14px; }
.status-row:last-child { border-bottom:none; }
.status-row span { color:var(--muted); }
.status-row strong { font-weight:600; text-align:right; }
.mono { font-family:'IBM Plex Mono',monospace; font-size:13px; word-break:break-all; }
.badge { background:var(--acid); color:var(--ink); padding:2px 10px; border-radius:999px; font-size:12px; }
.footer { padding:32px 0 48px; color:var(--muted); font-size:13px; border-top:1px solid #262B36; }
"""

PAGE_HEAD = """<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{css}</style>""".format(css=PAGE_CSS)

TOPBAR = """
<header class="topbar">
  <div class="wrap topbar-inner">
    <a class="brand" href="/">
      <span class="brand-mark">&#10209;</span>
      <span class="brand-name">BOOST</span>
    </a>
    <nav class="topnav">
      <a href="/#xidmetler">Xidmətlər</a>
      <a href="/#nece-isleyir">Necə işləyir</a>
    </nav>
  </div>
</header>
"""


def render_home(categories: dict) -> str:
    if categories:
        blocks = []
        for category, products in categories.items():
            cards = []
            for p in products:
                desc = f'<p class="product-desc">{p["description"]}</p>' if p.get("description") else ""
                cards.append(f"""
                <form class="product-card" method="post" action="/order">
                  <input type="hidden" name="product_id" value="{p['product_id']}">
                  <div class="product-top">
                    <h3>{p['name']}</h3>
                    <span class="price">{p['price']} &#8380;</span>
                  </div>
                  {desc}
                  <label class="field"><span>Link</span>
                    <input type="text" name="profile_link" placeholder="https://..." required>
                  </label>
                  <label class="field"><span>Miqdar</span>
                    <input type="number" name="quantity" value="1" min="1" required>
                  </label>
                  <button type="submit" class="order-btn">Sifariş et &#8594;</button>
                </form>
                """)
            blocks.append(f"""
            <div class="cat-block">
              <h2 class="cat-title">{category}</h2>
              <div class="cat-grid">{''.join(cards)}</div>
            </div>
            """)
        catalog_html = "".join(blocks)
    else:
        catalog_html = '<p class="empty-state">Hələ aktiv xidmət yoxdur. Tezliklə əlavə olunacaq.</p>'

    return f"""<!DOCTYPE html>
<html lang="az">
<head><title>Boost — Sosial Media Artım Paneli</title>{PAGE_HEAD}</head>
<body>
{TOPBAR}
<section class="hero">
  <div class="wrap hero-inner">
    <p class="eyebrow">İZLƏNMƏ &middot; BƏYƏNİ &middot; TAKİPÇİ</p>
    <h1>Hesabınız böyüsün,<br>siz işinizlə məşğul olun.</h1>
    <p class="hero-sub">TikTok, Instagram, Telegram və YouTube üçün sürətli, dayanıqlı artım xidmətləri. Sifariş ver, linki yapışdır, geri qalanı bizdə.</p>
    <a href="#xidmetler" class="cta">Xidmətlərə bax &#8595;</a>
  </div>
  <div class="hero-tape" aria-hidden="true">
    <div class="tape-track">
      <span>TIKTOK</span><span>&middot;</span><span>INSTAGRAM</span><span>&middot;</span><span>TELEGRAM</span><span>&middot;</span><span>YOUTUBE</span><span>&middot;</span>
      <span>TIKTOK</span><span>&middot;</span><span>INSTAGRAM</span><span>&middot;</span><span>TELEGRAM</span><span>&middot;</span><span>YOUTUBE</span><span>&middot;</span>
    </div>
  </div>
</section>
<section id="xidmetler" class="catalog wrap">{catalog_html}</section>
<section id="nece-isleyir" class="howto wrap">
  <h2>Necə işləyir</h2>
  <div class="howto-grid">
    <div class="howto-step"><span class="step-no">01</span><h3>Xidməti seç</h3><p>Platformanı və miqdarı seç, linkini yapışdır.</p></div>
    <div class="howto-step"><span class="step-no">02</span><h3>Ödə</h3><p>Sifariş yarananda ödəniş üçün admin sizinlə əlaqə saxlayır.</p></div>
    <div class="howto-step"><span class="step-no">03</span><h3>İzlə</h3><p>Sifariş nömrəsi ilə statusu canlı izlə.</p></div>
  </div>
</section>
<footer class="footer wrap"><p>&copy; 2026 Boost Panel &middot; Dəstək üçün Telegram botumuza yazın.</p></footer>
</body>
</html>"""


def render_order_status(order: dict) -> str:
    return f"""<!DOCTYPE html>
<html lang="az">
<head><title>Sifariş #{order['order_id']} — Boost</title>{PAGE_HEAD}</head>
<body>
{TOPBAR}
<section class="status-page wrap">
  <p class="eyebrow">SİFARİŞ #{order['order_id']}</p>
  <h1>{order['product_name']}</h1>
  <div class="status-card">
    <div class="status-row"><span>Platforma</span><strong>{order['category']}</strong></div>
    <div class="status-row"><span>Miqdar</span><strong>{order['quantity']}</strong></div>
    <div class="status-row"><span>Link</span><strong class="mono">{order['profile_link']}</strong></div>
    <div class="status-row"><span>Status</span><strong class="badge">{order['status']}</strong></div>
    <div class="status-row"><span>Tarix</span><strong class="mono">{order['order_date']}</strong></div>
  </div>
  <a href="/" class="cta">&#8592; Yeni sifariş</a>
</section>
</body>
</html>"""


# ===================== ROUTES =====================
@app.get("/", response_class=HTMLResponse)
def home():
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

    return render_home(by_category)


@app.post("/order")
def create_order(
    product_id: int = Form(...),
    quantity: int = Form(...),
    profile_link: str = Form(...),
):
    conn = get_conn()
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM products WHERE product_id = %s", (product_id,))
        product = c.fetchone()
        if not product:
            raise HTTPException(status_code=404, detail="Məhsul tapılmadı")

        c.execute(
            """INSERT INTO orders (user_id, product_id, quantity, status, order_date, profile_link)
               VALUES (NULL, %s, %s, 'Beklemede', %s, %s) RETURNING order_id""",
            (product_id, quantity, datetime.now().isoformat(), profile_link),
        )
        order_id = c.fetchone()["order_id"]
        conn.commit()
    finally:
        put_conn(conn)

    return RedirectResponse(url=f"/order/{order_id}", status_code=status.HTTP_303_SEE_OTHER)


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

