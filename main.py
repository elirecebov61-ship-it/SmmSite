import hashlib, os, secrets, logging, smtplib, ssl, sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse, quote
import httpx, psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File, status
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

# ===== LOGGING =====
# Railway "Deployments -> Logs" bolmesinde butun bu loglari gore bilersiniz.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("boostpanel")

# ===== CONFIG =====
DATABASE_URL   = os.getenv("DATABASE_URL", "")

# SESSION_SECRET MUTLEQ env-de sabit olmalidir. Set edilmeyibse, hasil sabit
# (ama gucsuz) acar duzeldirik ki, Railway serveri her yeniden basladiginda
# (redeploy/restart/sleep) butun istifadecilerin sessiyasi/girisi silinmesin.
# Bunun evezine Railway panelinde Variables bolmesine ozunuz uzun,
# tesadufi SESSION_SECRET elave etmenizi meslehet gorurem (mes. 64 simvollu random string).
_env_secret = os.getenv("SESSION_SECRET")
if _env_secret:
    SESSION_SECRET = _env_secret
else:
    logger.warning("SESSION_SECRET env deyiskeni tapilmadi! Sabit fallback acar istifade olunur — "
                    "Railway-de mutleq SESSION_SECRET elave edin, eks halda deploy zamani problem yarana biler.")
    _fallback_seed = (os.getenv("ADMIN_KEY","") + "|" + DATABASE_URL + "|boostpanel-fallback")
    SESSION_SECRET = hashlib.sha256(_fallback_seed.encode()).hexdigest()

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
BOT_USERNAME      = os.getenv("BOT_USERNAME", "")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID", "")
ADMIN_KEY         = os.getenv("ADMIN_KEY", "admin123")
OWNER_EMAIL       = os.getenv("OWNER_EMAIL", "elirecebov61@gmail.com").strip().lower()
CARD_NUMBER = os.getenv("CARD_NUMBER", "5522099369926134")
CARD_HOLDER = os.getenv("CARD_HOLDER", "")
CARD_BANK   = os.getenv("CARD_BANK", "ABB")
BRAND = "PanelimAz"
SITE_URL = os.getenv("SITE_URL", "https://panelbaku.com")

# ===== PANELBAKU PROVIDER API (avtomatik sifariş ötürülməsi) =====
# Bu API vasitəsilə istifadəçi sifariş verən kimi sifariş avtomatik olaraq
# provayderə (PanelBaku) ötürülür və status/başlanğıc say/qalıq PanelBaku-dan
# çəkilib bizim panelimizdə göstərilir. API açarını Railway Variables-da
# PANELBAKU_API_KEY kimi saxlamaq daha təhlükəsizdir, amma default olaraq
# sizin verdiyiniz açar aşağıda təyin olunub.
PANELBAKU_API_KEY = os.getenv("PANELBAKU_API_KEY", "af5111d02469901041d92f3463ad21ed")
PANELBAKU_API_URL = os.getenv("PANELBAKU_API_URL", "https://panelbaku.com/api/v2")

# ===== EMAIL (SMTP) — Railway Variables-a bu deyiskenleri elave edin =====
# Gmail ucun: SMTP_HOST=smtp.gmail.com, SMTP_PORT=587, SMTP_USER=sizin@gmail.com,
# SMTP_PASS = Google "App Password" (adi Gmail sifreniz deyil!)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or 587)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "") or SMTP_USER

app = FastAPI(title=BRAND)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    max_age=60*60*24*60,   # 60 gun - istifadeci brauzeri baglasa da girisde qalsin
    same_site="lax",
)

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

# ===== ERROR/ACTIVITY LOGGING HELPERS =====
def log_event(kind, email="", detail=""):
    """Butun vacib hereketleri Railway loglarina yazir (login, sifaris, odenis ve s.)"""
    logger.info(f"[{kind}] user={email or '-'} | {detail}")

def log_error(kind, err, email=""):
    logger.error(f"[{kind}] user={email or '-'} | XETA: {err}")

# ===== PANELBAKU PROVIDER API (sifarişlərin avtomatik ötürülməsi) =====
def panelbaku_call(params: dict):
    """PanelBaku API-yə (POST https://panelbaku.com/api/v2) sorğu göndərir.
    params-a 'key' avtomatik əlavə olunur. Xəta halında None qaytarır."""
    if not PANELBAKU_API_KEY:
        return None
    data = {"key": PANELBAKU_API_KEY, **params}
    try:
        with httpx.Client(timeout=15) as cl:
            r = cl.post(PANELBAKU_API_URL, data=data)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log_error("PANELBAKU_API_FAIL", e, "")
        return None

def panelbaku_send_order(service_id, link, quantity):
    """Sifarişi PanelBaku-ya ötürür. {'order': 123} kimi cavab qaytarır ya da None."""
    return panelbaku_call({"action": "add", "service": service_id, "link": link, "quantity": quantity})

def panelbaku_order_status(provider_order_id):
    """Tək sifarişin statusunu PanelBaku-dan çəkir."""
    return panelbaku_call({"action": "status", "order": provider_order_id})

def panelbaku_multi_status(provider_order_ids):
    """Bir neçə sifarişin (max 100) statusunu bir sorğu ilə çəkir. Dict qaytarır: {order_id: {...}}"""
    if not provider_order_ids: return {}
    ids = ",".join(str(i) for i in provider_order_ids[:100])
    res = panelbaku_call({"action": "status", "orders": ids})
    return res or {}

def panelbaku_balance():
    return panelbaku_call({"action": "balance"})

def panelbaku_services():
    return panelbaku_call({"action": "services"})

# PanelBaku status -> bizim panelimizdəki status adları arasında uyğunluq
PANELBAKU_STATUS_MAP = {
    "Pending": "Gözləmədə",
    "In progress": "Davam Edir",
    "Processing": "Davam Edir",
    "Completed": "Tamamlandı",
    "Partial": "Davam Edir",
    "Canceled": "Ləğv Edildi",
    "Cancelled": "Ləğv Edildi",
}

def sync_orders_from_provider(order_ids=None):
    """Verilmiş (ya da bütün açıq) sifarişləri PanelBaku-dan sinxronlaşdırır:
    status, start_count, remains sahələrini yeniləyir. Admin panelindən
    və ya periodik olaraq çağırıla bilər."""
    if not PANELBAKU_API_KEY:
        return 0
    conn = get_conn()
    updated = 0
    try:
        c = conn.cursor(cursor_factory=RealDictCursor)
        if order_ids:
            c.execute("SELECT order_id,provider_order_id FROM orders WHERE order_id=ANY(%s) AND provider_order_id IS NOT NULL", (order_ids,))
        else:
            c.execute("""SELECT order_id,provider_order_id FROM orders
                         WHERE provider_order_id IS NOT NULL
                         AND status NOT IN ('Tamamlandı','Ləğv Edildi') LIMIT 100""")
        rows = c.fetchall()
        if not rows:
            return 0
        pid_map = {str(r["provider_order_id"]): r["order_id"] for r in rows if r["provider_order_id"]}
        results = panelbaku_multi_status(list(pid_map.keys()))
        for pid_str, info in results.items():
            local_id = pid_map.get(pid_str)
            if not local_id or not isinstance(info, dict) or "error" in info:
                continue
            new_status = PANELBAKU_STATUS_MAP.get(info.get("status"), None)
            start_count = info.get("start_count")
            remains = info.get("remains")
            sets, vals = [], []
            if new_status:
                sets.append("status=%s"); vals.append(new_status)
            if start_count is not None:
                sets.append("start_count=%s"); vals.append(start_count)
            if remains is not None:
                sets.append("remains=%s"); vals.append(remains)
            if sets:
                vals.append(local_id)
                c.execute(f"UPDATE orders SET {','.join(sets)} WHERE order_id=%s", vals)
                updated += 1
        conn.commit()
        if updated:
            log_event("PANELBAKU_SYNC", "", f"updated={updated}")
    except Exception as e:
        log_error("PANELBAKU_SYNC_FAIL", e, "")
        conn.rollback()
    finally:
        put_conn(conn)
    return updated


# ===== EMAIL GONDERME =====
def gen_token():
    return secrets.token_urlsafe(32)

def send_email(to_email, subject, html_body):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        logger.warning(f"SMTP konfiqurasiya edilmeyib — email gonderilmedi: '{subject}' -> {to_email}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{BRAND} <{SMTP_FROM}>"
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html"))
        ctx = ssl.create_default_context()
        # ===== IPv4-Ə MƏCBUR ETMƏ =====
        # Railway kimi bəzi host mühitlərində konteynerin IPv6 çıxışı olmur,
        # amma Gmail SMTP ünvanı həm IPv4, həm IPv6 qaytarır. Python defolt
        # olaraq bəzən əvvəlcə IPv6-nı sınayır və "Network is unreachable"
        # xətası alınır. Bunun qarşısını almaq üçün qoşulma zamanı yalnız
        # IPv4 nəticələrini qaytaracaq şəkildə DNS axtarışını müvəqqəti
        # məhdudlaşdırırıq (host adı olduğu kimi qalır ki, TLS sertifikat
        # yoxlaması pozulmasın).
        import socket
        _orig_getaddrinfo = socket.getaddrinfo
        def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
        socket.getaddrinfo = _ipv4_only_getaddrinfo
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.ehlo()
                server.starttls(context=ctx)
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_FROM, to_email, msg.as_string())
        finally:
            socket.getaddrinfo = _orig_getaddrinfo
        log_event("EMAIL_SENT", to_email, subject)
        return True
    except Exception as e:
        log_error("EMAIL_FAIL", e, to_email)
        return False

def send_verification_email(request, email, token):
    link = f"{str(request.base_url).rstrip('/')}/verify-email?token={token}"
    html = f"""<div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:24px;background:#181622;color:#F1F0F5;border-radius:14px;">
<h2 style="color:#3B82F6;">{BRAND}</h2>
<p>Salam! Qeydiyyatınızı tamamlamaq üçün aşağıdakı düyməyə klikləyin:</p>
<p style="text-align:center;margin:26px 0;">
<a href="{link}" style="background:#3B82F6;color:#1a1220;padding:12px 26px;border-radius:10px;text-decoration:none;font-weight:700;">E-poçtu təsdiqlə</a>
</p>
<p style="font-size:12px;color:#9B98AE;">Link işləmirsə, bu ünvanı brauzerə yapışdırın:<br>{link}</p>
</div>"""
    return send_email(email, f"{BRAND} — E-poçtunuzu təsdiqləyin", html)

def send_reset_email(request, email, token):
    link = f"{str(request.base_url).rstrip('/')}/reset-password?token={token}"
    html = f"""<div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:24px;background:#181622;color:#F1F0F5;border-radius:14px;">
<h2 style="color:#3B82F6;">{BRAND}</h2>
<p>Şifrənizi bərpa etmək üçün klikləyin (link 1 saat etibarlıdır):</p>
<p style="text-align:center;margin:26px 0;">
<a href="{link}" style="background:#3B82F6;color:#1a1220;padding:12px 26px;border-radius:10px;text-decoration:none;font-weight:700;">Şifrəni bərpa et</a>
</p>
<p style="font-size:12px;color:#9B98AE;">Bu tələbi siz etməmisinizsə, bu emaili nəzərə almayın.<br>Link işləmirsə: {link}</p>
</div>"""
    return send_email(email, f"{BRAND} — Şifrə bərpası", html)

LOGO_B64 = "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"

@app.get("/logo.png")
def serve_logo():
    from fastapi.responses import Response
    import base64
    return Response(content=base64.b64decode(LOGO_B64), media_type="image/png")


FACEBOOK_ICON_B64 = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAFkklEQVR42u2az2scZRjHP887M012U02qplUq1SpazUHUm94UvXmolvXgrQehiCIoepDCZkV78GAPFtF/oIKB2oJeCqIHvfkDC1qlYIuFNiSUJm2T7WRm3sfDO5PdxMTsZLOb3XS+YRh28+47z/N9n1/vOw8UKFCgQIECBQoUuDUhGzqbqjCezvnHBs89hgIwjiKivUOhqlBVv+vPraqPqmyuBVTUY0KSxc8f6D1YRoAyysaR4qFExGxjnoAZ3pXJVWXoGgHZg9/T3ZR5DcsLKA+gbMfrhKUBFkW4geFv4GtiPqUml9ohQdpSvqov4fMZA4wSAXEqKCjakWglCOADARAyTcwhanJivSRIG8rvZ4CvSICYCMFrmk86FXEW70qCT4AHhLxITU6uhwTJHfAEqLILw1kMw8TYVPlNCMAk+Bgss4SMcYRJqgg1sa1OYXI9cBwPRBHeoMQIEcmmKe+WzyMiocQI23gdRPPqlNNUVagiCGcIGCMi9wNXXAUBkaXCqKZxb+1YYgkQIs7yO48xgU2J2GALqKpJJ94J7CVG2vV1I+6yESR1iOfSax6Sm+77lhbRyXI/D7ETRJ2srSF/rvbYgaXUbpQ3Bmzowtp9ozA2Crtvh3IAMzfh4iycvwr/zIJdiwAnS4mAHcDlPHLkJyChhI+QoOu1AGPA1uHJPfD+s/DMXqf4ckzPwcPHYGYexHNusWo49BCUUl5ZWicgq+0NwWJCkvUr//wjcOoVKKUSWG34uyp4BgLPxYaWk2OSypZjH2LoIkRAY9g1Al9UnPKRdfIbAd80FLYKC3nLGpPfMbtKgCegIbz5NNxZcsoHpmFImo4JPEfGzqEWLaANdHUXl1jwS3Dg0dTMZWnaE4HvL8CfUxBauBZCPepgXdlNArJ0d+9dsHdk6cpadf9/6zQc/Ta1y8yYBx0BqlvAAlAYGXQmnsXQTPlzV+DoD2DKLgBm+sZ2C7kAy8y+efXPTIHEIIGLDd1CxwnITF1WKHebMbfQ5CpNUVH7nQBNU1kiQLK6SWs6NkpYWvqZPiZABG4rubsRsB4MD648dtCH4TL45QZJVuF62IcEiLjVHC7DT6+6wJelOc8s3Yb66ef9++C5d1JrSWPFdxegchzMtpZ2hb1pAbuGYPvA2mMHfHc141rojtnMQJ8SAK6cVW1YAP8TCLNcH1tnGX9d2QJZIFF3qTrFRRtusDwdZqucqBt3fqazVWBXXGC03JoS2eFIc1y4OOuygGqfEaBpmTe3AIe+cRFeAJvAnjvg7af+Wwn+fBmO/9IIeFbh7LST0Go/WoBAGMHnPzadHYSw70FHQPZdRsCvk/DxaWCoqQ4YTC2gb2OAuLyebYUTH3ascmZT8sHfvrQOSOwWqAQzZVScQskqlaDVdKzt/AZo0w5EehGtE5C9nxfiJaVcLyCTJU5lG2vdc9ZzKlxPD6J7hQInS4ISMN95F4i4CtRT9bWHLKBOwtXOEVATCyqcYwq4gO92sD1hAU6W88AUaAdfjlbJXj+fJECw2E1X36bvBuEUNbFU872sNbkfhwoLHKPODAEeSrKJa58Q4FFnBuUTUGE8nzz5CKiJpYLhiFzGchAPwU87eFz9tqZbmHZVbjTLRPh4eAiWg9RkkgombwdZfnkmJKGiHjU5ScgBYJohAgJM2sAiuA6O1f9AdaX7Gr/BtcgIAYYhAmCakAPr7Q5Z/4I0SDhBzOOEfIjlN+A6oBgEf+VLnL+KMe4eeOl9YPXf4CN4i2n3BglnWOAIdZ5opz+ovVK4QcIl4DBwmI/0biJGWKBMgE/SqBXEpVB0YVlWjSAIIA4h7TdaVkunUgoxhnkMMxyWxivwNtvkNmLvews3Sq5ExhqtspUKfFlZwaAm4OWJFkrxnmuVLVCgQIECBQoUKNCf+BfGr0L/gIyLSwAAAABJRU5ErkJggg=="
VERIFIED_BADGE_B64 = "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"
TELEGRAM_ICON_B64 = "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"
TIKTOK_ICON_B64 = "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"
INSTAGRAM_ICON_B64 = "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"
TWITTER_ICON_B64 = "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"
WHATSAPP_ICON_B64 = "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"
DISCORD_ICON_B64 = "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"
YOUTUBE_ICON_B64 = "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"

@app.get("/icons/{name}.png")
def serve_platform_icon(name: str):
    from fastapi.responses import Response
    import base64
    mapping = {
        "facebook": FACEBOOK_ICON_B64,
        "verified": VERIFIED_BADGE_B64,
        "telegram": TELEGRAM_ICON_B64,
        "tiktok": TIKTOK_ICON_B64,
        "instagram": INSTAGRAM_ICON_B64,
        "twitter": TWITTER_ICON_B64,
        "whatsapp": WHATSAPP_ICON_B64,
        "discord": DISCORD_ICON_B64,
        "youtube": YOUTUBE_ICON_B64,
    }
    data = mapping.get(name)
    if not data:
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    return Response(content=base64.b64decode(data), media_type="image/png")

@app.get("/robots.txt")
def robots_txt():
    from fastapi.responses import PlainTextResponse
    content = f"""User-agent: *
Allow: /
Disallow: /admin
Disallow: /profile
Disallow: /orders
Disallow: /balance
Disallow: /support

Sitemap: {SITE_URL}/sitemap.xml
"""
    return PlainTextResponse(content)

@app.get("/sitemap.xml")
def sitemap_xml():
    from fastapi.responses import Response
    pages = ["/", "/news"]
    urls = "".join(f"<url><loc>{SITE_URL}{p}</loc><changefreq>daily</changefreq></url>" for p in pages)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(content=xml, media_type="application/xml")





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
                    "total_orders INTEGER DEFAULT 0","created_at TEXT","avatar_url TEXT",
                    "telegram_chat_id TEXT","telegram_username TEXT","api_key TEXT","telegram_link_code TEXT",
                    "email_verified BOOLEAN DEFAULT FALSE","verify_token TEXT","verify_sent_at TEXT",
                    "reset_token TEXT","reset_expires TEXT"]:
            try: c.execute(f"ALTER TABLE panel_users ADD COLUMN IF NOT EXISTS {col}")
            except: conn.rollback()
        for col2 in ["min_qty INTEGER DEFAULT 10","max_qty INTEGER DEFAULT 100000","description TEXT",
                     "provider_service_id TEXT"]:
            try: c.execute(f"ALTER TABLE products ADD COLUMN IF NOT EXISTS {col2}")
            except: conn.rollback()
        # Köhnə verilənlər bazasında 'price' sütunu tam ədəd (scale=0) kimi
        # yaradılmış ola bilər — bu, 0.20 kimi qəpik qiymətlərinin avtomatik
        # 0-a yuvarlaqlaşmasına səbəb olur. Sütun tipini məcburi olaraq
        # onluq kəsr saxlaya bilən NUMERIC(14,4)-ə çeviririk.
        try: c.execute("ALTER TABLE products ALTER COLUMN price TYPE NUMERIC(14,4)")
        except: conn.rollback()
        try: c.execute("ALTER TABLE orders ALTER COLUMN price TYPE NUMERIC(14,4)")
        except: conn.rollback()
        c.execute("""CREATE TABLE IF NOT EXISTS products (
            product_id SERIAL PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
            price NUMERIC NOT NULL, min_qty INTEGER DEFAULT 10,
            max_qty INTEGER DEFAULT 100000, stock INTEGER DEFAULT 999, description TEXT)""")
        for col3 in ["start_count INTEGER DEFAULT 0","remains INTEGER DEFAULT 0","customer_email TEXT",
                     "product_id INTEGER","quantity INTEGER","profile_link TEXT","price NUMERIC",
                     "status TEXT DEFAULT 'Gözləmədə'","order_date TEXT",
                     "provider_order_id TEXT","provider_error TEXT"]:
            try: c.execute(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {col3}")
            except: conn.rollback()
        c.execute("""CREATE TABLE IF NOT EXISTS orders (
            order_id SERIAL PRIMARY KEY, customer_email TEXT, product_id INTEGER,
            quantity INTEGER NOT NULL, profile_link TEXT, price NUMERIC,
            start_count INTEGER DEFAULT 0, remains INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Gözləmədə', order_date TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS topups (
            topup_id SERIAL PRIMARY KEY, customer_email TEXT,
            amount_sent TEXT, status TEXT DEFAULT 'Yoxlanılır', topup_date TEXT)""")
        for col4 in ["customer_email TEXT","amount_sent TEXT","status TEXT DEFAULT 'Yoxlanılır'","topup_date TEXT"]:
            try: c.execute(f"ALTER TABLE topups ADD COLUMN IF NOT EXISTS {col4}")
            except: conn.rollback()
        c.execute("""CREATE TABLE IF NOT EXISTS tickets (
            ticket_id SERIAL PRIMARY KEY, customer_email TEXT, subject TEXT,
            message TEXT, status TEXT DEFAULT 'Açıq', created_at TEXT)""")
        for col5 in ["customer_email TEXT","subject TEXT","message TEXT","status TEXT DEFAULT 'Açıq'","created_at TEXT","user_unread BOOLEAN DEFAULT FALSE"]:
            try: c.execute(f"ALTER TABLE tickets ADD COLUMN IF NOT EXISTS {col5}")
            except: conn.rollback()
        c.execute("""CREATE TABLE IF NOT EXISTS ticket_replies (
            reply_id SERIAL PRIMARY KEY, ticket_id INTEGER, sender TEXT,
            message TEXT, created_at TEXT)""")
        for col6 in ["ticket_id INTEGER","sender TEXT","message TEXT","created_at TEXT",
                     "attachment_url TEXT","is_read BOOLEAN DEFAULT FALSE"]:
            try: c.execute(f"ALTER TABLE ticket_replies ADD COLUMN IF NOT EXISTS {col6}")
            except: conn.rollback()
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
        c.execute("SELECT balance,total_spent,total_orders,email_verified,password FROM panel_users WHERE email=%s",(email,))
        return c.fetchone() or {"balance":0,"total_spent":0,"total_orders":0,"email_verified":True,"password":None}
    finally: put_conn(conn)
def get_user_db(email):
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM panel_users WHERE email=%s",(email,))
        return c.fetchone()
    finally: put_conn(conn)
def get_unread_ticket_count(email):
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("SELECT COUNT(*) FROM tickets WHERE customer_email=%s AND user_unread=TRUE",(email,))
        return c.fetchone()[0]
    finally: put_conn(conn)

def get_notifications(email):
    """Builds a light-weight notification list: unread ticket replies + recent order status."""
    notifs=[]
    if not email: return notifs
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT ticket_id,subject FROM tickets WHERE customer_email=%s AND user_unread=TRUE ORDER BY ticket_id DESC LIMIT 8",(email,))
        for t in c.fetchall():
            notifs.append({"icon":"🎫","title":f"Ticket #{t['ticket_id']} — {t['subject']}","meta":"Yeni cavab var","href":f"/support/{t['ticket_id']}"})
        c.execute("""SELECT o.order_id,o.status,o.order_date,p.name FROM orders o
                     LEFT JOIN products p ON p.product_id=o.product_id
                     WHERE o.customer_email=%s ORDER BY o.order_id DESC LIMIT 5""",(email,))
        for o in c.fetchall():
            notifs.append({"icon":"📦","title":f"Sifariş #{o['order_id']} — {o.get('name') or 'Xidmət'}","meta":str(o.get('status') or ''),"href":"/orders"})
    except Exception:
        pass
    finally: put_conn(conn)
    return notifs

# ===== CSS =====
CSS = """
:root{
  --or:#3B82F6;--ord:#2563EB;--orlt:#1E1B2E;--orltt:rgba(59,130,246,.14);
  --tx:#1A1625;--mu:#6B6680;--sf:rgba(255,255,255,.82);--sf-solid:#FFFFFF;
  --bg:#F3F1F8;--ln:rgba(59,130,246,.28);--ra:14px;
  --glass-brd:rgba(20,10,40,.08);--glow:0 8px 32px rgba(120,90,40,.10);
}
[data-theme="dark"]{
  --tx:#F1F0F5;--mu:#9B98AE;--sf:rgba(30,27,46,.72);--sf-solid:#181622;
  --bg:#0B0A12;--ln:rgba(59,130,246,.18);
  --glass-brd:rgba(255,255,255,.08);--glow:0 8px 32px rgba(0,0,0,.45);
}
[data-theme="dark"] .topbar{background:rgba(11,10,18,.72);}
[data-theme="dark"] .sidebar{background:rgba(255,255,255,.98);}
[data-theme="dark"] ::-webkit-scrollbar-thumb{background:rgba(59,130,246,.35);}
*{box-sizing:border-box;margin:0;padding:0;}
html{scroll-behavior:smooth;}
body{
  background:
    radial-gradient(1100px 600px at 12% -10%,rgba(59,130,246,.16),transparent 60%),
    radial-gradient(900px 500px at 110% 10%,rgba(168,85,247,.12),transparent 55%),
    var(--bg);
  background-attachment:fixed;
  color:var(--tx);font-family:'Inter',sans-serif;-webkit-font-smoothing:antialiased;
  animation:pageFadeIn .45s cubic-bezier(.4,0,.2,1);
}
@keyframes pageFadeIn{from{opacity:0;transform:translateY(10px);}to{opacity:1;transform:translateY(0);}}
@keyframes fadeUp{from{opacity:0;transform:translateY(16px);}to{opacity:1;transform:translateY(0);}}
@keyframes floatGlow{0%,100%{transform:translateY(0);}50%{transform:translateY(-10px);}}
@keyframes shimmer{0%{background-position:-200% 0;}100%{background-position:200% 0;}}
a{color:inherit;text-decoration:none;}
.wrap{max-width:1100px;margin:0 auto;padding:0 18px;}
::selection{background:rgba(59,130,246,.35);color:#fff;}
::-webkit-scrollbar{width:10px;height:10px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:rgba(59,130,246,.35);border-radius:10px;}

.glass{background:var(--sf);backdrop-filter:blur(18px) saturate(140%);-webkit-backdrop-filter:blur(18px) saturate(140%);border:1px solid var(--glass-brd);box-shadow:var(--glow);}

/* TOPBAR */
.topbar{background:rgba(11,10,18,.72);backdrop-filter:blur(16px) saturate(140%);-webkit-backdrop-filter:blur(16px) saturate(140%);border-bottom:1px solid var(--glass-brd);position:sticky;top:0;z-index:50;box-shadow:0 4px 24px rgba(0,0,0,.35);}
.topbar-inner{display:flex;justify-content:space-between;align-items:center;height:74px;}
.brand{display:flex;align-items:center;gap:8px;font-weight:800;font-size:20px;color:var(--or);transition:filter .2s;}
.brand:hover{filter:brightness(1.15);}
.brand svg{width:28px;height:28px;}
.brand img{filter:drop-shadow(0 0 14px rgba(59,130,246,.45));}
.brand-text{display:inline-flex;font-weight:800;font-size:21px;letter-spacing:.2px;}
.brand-white{color:var(--tx);}
.brand-navy{color:#1E3A8A;}
.brand-orange{color:var(--or);}
.hamburger{background:none;border:none;cursor:pointer;padding:8px;color:var(--or);}
.hamburger span{display:block;width:24px;height:2px;background:var(--or);margin:5px 0;border-radius:2px;transition:.3s;}
.topuser{display:flex;align-items:center;gap:12px;font-size:13px;color:var(--mu);}
.bal-badge{background:linear-gradient(135deg,rgba(59,130,246,.22),rgba(37,99,235,.1));border:1px solid var(--ln);color:#93C5FD;font-weight:700;padding:6px 14px;border-radius:999px;font-size:13px;box-shadow:0 0 18px rgba(59,130,246,.12) inset;}

/* SIDEBAR */
.sb-ov{display:none;position:fixed;top:var(--topbar-h,64px);left:0;right:0;bottom:0;background:rgba(0,0,0,.6);backdrop-filter:blur(2px);z-index:48;}
.sb-ov.open{display:block;animation:pageFadeIn .2s ease;}
.sidebar{position:fixed;top:var(--topbar-h,64px);right:-300px;width:280px;height:calc(100% - var(--topbar-h,64px));background:rgba(255,255,255,.98);backdrop-filter:blur(20px) saturate(140%);-webkit-backdrop-filter:blur(20px) saturate(140%);border-left:1px solid rgba(0,0,0,.08);z-index:49;transition:right .32s cubic-bezier(.4,0,.2,1);box-shadow:-8px 0 32px rgba(0,0,0,.25);display:flex;flex-direction:column;overflow-y:auto;}
.sidebar.open{right:0;}
.sb-head{background:linear-gradient(135deg,var(--or),var(--ord));padding:24px 20px 22px;color:#fff;position:relative;overflow:hidden;}
.sb-head::after{content:'';position:absolute;top:-30px;right:-30px;width:120px;height:120px;background:rgba(255,255,255,.12);border-radius:50%;}
.sb-head .sb-name{font-weight:700;font-size:15px;display:flex;align-items:center;flex-wrap:wrap;}
.sb-head .sb-email{font-size:12px;opacity:.85;margin-top:2px;}
.sb-head .sb-bal{margin-top:10px;font-size:13px;background:rgba(255,255,255,.2);display:inline-block;padding:4px 12px;border-radius:999px;}
.sb-nav{flex:1;padding:8px 0;margin-top:10px;}
.sb-nav a{display:flex;align-items:center;gap:12px;padding:13px 20px;font-size:14px;color:var(--tx);transition:all .18s;border-left:3px solid transparent;}
.sb-nav a:hover{background:rgba(59,130,246,.08);color:var(--or);}
.sb-nav a.active{background:linear-gradient(90deg,rgba(59,130,246,.16),transparent);color:var(--or);border-left:3px solid var(--or);}
.sb-nav a .icon{font-size:17px;width:22px;text-align:center;}
.nav-badge{margin-left:auto;background:linear-gradient(135deg,#F87171,#DC2626);color:#fff;font-size:10px;font-weight:800;padding:2px 7px;border-radius:999px;box-shadow:0 0 10px rgba(248,113,113,.5);animation:floatGlow 2s ease-in-out infinite;}
.sb-nav a{position:relative;}
.sb-div{height:1px;background:var(--glass-brd);margin:4px 0;}
.sb-footer{padding:16px 20px;font-size:12px;color:var(--mu);}

/* LANDING */
.hero-land{background:radial-gradient(900px 420px at 20% 0%,rgba(59,130,246,.16),transparent 65%),var(--bg);padding:56px 0 0;overflow:hidden;}
.hero-land h1{font-size:clamp(24px,5vw,38px);font-weight:800;color:var(--tx);line-height:1.22;margin-bottom:10px;background:linear-gradient(90deg,#fff,#93C5FD);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;}
.hero-land p{color:var(--mu);font-size:15px;max-width:480px;line-height:1.6;}
.hero-login-card{background:var(--sf);backdrop-filter:blur(18px) saturate(140%);-webkit-backdrop-filter:blur(18px) saturate(140%);border:1px solid var(--glass-brd);border-radius:18px;padding:28px;box-shadow:var(--glow);max-width:380px;width:100%;animation:fadeUp .5s ease .1s both;}
.hero-grid{display:grid;grid-template-columns:1fr 1fr;gap:40px;align-items:center;padding-bottom:60px;}
@media(max-width:700px){.hero-grid{grid-template-columns:1fr;}.hero-land h1{font-size:24px;}}
.wave-wrap{margin-top:0;}
.wave-wrap svg{display:block;width:100%;}

/* AUTH CARD */
.auth-tabs{display:flex;border:1px solid var(--ln);border-radius:10px;overflow:hidden;margin-bottom:20px;background:rgba(255,255,255,.03);}
.auth-tabs a{flex:1;text-align:center;padding:10px;font-size:14px;font-weight:600;color:var(--mu);transition:all .2s;}
.auth-tabs a.active{background:linear-gradient(90deg,var(--or),var(--ord));color:#fff;}
.field{display:flex;flex-direction:column;gap:6px;font-size:13px;color:var(--tx);margin-bottom:14px;font-weight:500;}
.field label{font-weight:600;font-size:13px;color:var(--mu);}
.field input,.field select,.field textarea{background:rgba(255,255,255,.04);border:1.5px solid var(--ln);border-radius:10px;padding:12px 14px;color:var(--tx);font-family:'Inter',sans-serif;font-size:14px;width:100%;transition:border-color .2s,box-shadow .2s;}
.field input::placeholder,.field textarea::placeholder{color:#6b6880;}
.field input:focus,.field select:focus,.field textarea:focus{outline:none;border-color:var(--or);box-shadow:0 0 0 3px rgba(59,130,246,.15);}
.field textarea{resize:vertical;min-height:100px;}
.field select{appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%233B82F6' d='M6 8L1 3h10z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center;padding-right:36px;}
.field select option{background:#181622;color:var(--tx);}
.cs-wrap{position:relative;}
.cs-trigger{display:flex;align-items:center;justify-content:space-between;width:100%;background:rgba(255,255,255,.04);border:1.5px solid var(--ln);border-radius:10px;padding:12px 14px;color:var(--tx);font-family:'Inter',sans-serif;font-size:14px;cursor:pointer;transition:border-color .2s,box-shadow .2s;}
.cs-trigger.open,.cs-trigger:focus{outline:none;border-color:var(--or);box-shadow:0 0 0 3px rgba(59,130,246,.15);}
.cs-trigger-inner{display:flex;align-items:center;gap:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.cs-ic{width:20px;height:20px;object-fit:contain;flex-shrink:0;border-radius:4px;}
.cs-ic-emoji{font-size:16px;flex-shrink:0;width:20px;text-align:center;}
.cs-arrow{color:var(--or);font-size:11px;transition:transform .2s;flex-shrink:0;margin-left:8px;}
.cs-trigger.open .cs-arrow{transform:rotate(180deg);}
.cs-list{display:none;position:absolute;top:calc(100% + 6px);left:0;right:0;max-height:280px;overflow-y:auto;background:var(--sf-solid);border:1px solid var(--glass-brd);border-radius:12px;box-shadow:0 12px 32px rgba(0,0,0,.22);z-index:40;padding:6px;}
.cs-list.open{display:block;}
.cs-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;cursor:pointer;font-size:14px;color:var(--tx);}
.cs-item:hover{background:rgba(59,130,246,.1);}
.cs-item.active{background:rgba(59,130,246,.14);color:var(--or);font-weight:700;}
.btn-or{width:100%;background:linear-gradient(90deg,#93C5FD,var(--or),var(--ord));color:#1a1220;border:none;border-radius:10px;padding:14px;font-weight:800;font-size:15px;cursor:pointer;font-family:'Inter',sans-serif;letter-spacing:.3px;box-shadow:0 4px 20px rgba(59,130,246,.35);transition:all .2s;}
.btn-or:hover{transform:translateY(-2px);box-shadow:0 8px 26px rgba(59,130,246,.5);filter:brightness(1.06);}
.btn-or:active{transform:translateY(0);}
.btn-google{display:flex;align-items:center;justify-content:center;gap:10px;background:rgba(255,255,255,.04);border:2px solid var(--or);color:var(--or);font-weight:700;padding:12px;border-radius:10px;font-size:14px;width:100%;cursor:pointer;font-family:'Inter',sans-serif;margin-top:10px;transition:background .2s;}
.btn-google:hover{background:rgba(59,130,246,.1);}
.divider-or{text-align:center;color:var(--mu);font-size:12px;margin:12px 0;position:relative;}
.divider-or::before,.divider-or::after{content:'';position:absolute;top:50%;width:44%;height:1px;background:var(--ln);}
.divider-or::before{left:0;}.divider-or::after{right:0;}
.err-box{background:rgba(239,68,68,.1);color:#FCA5A5;border:1px solid rgba(239,68,68,.35);border-radius:10px;padding:10px 14px;font-size:13px;margin-bottom:12px;animation:errIn .4s cubic-bezier(.34,1.56,.64,1) both;}
@keyframes errIn{0%{opacity:0;transform:translateY(-14px) scale(.97);}60%{opacity:1;transform:translateY(2px) scale(1.01);}100%{opacity:1;transform:translateY(0) scale(1);}}
.ok-box{background:rgba(52,211,153,.1);border:1px solid rgba(52,211,153,.35);color:#6EE7B7;border-radius:10px;padding:10px 14px;font-size:13px;margin-bottom:12px;}
.link-or{color:var(--or);font-weight:600;}
.text-center{text-align:center;}
.mt-10{margin-top:10px;}
.mt-20{margin-top:20px;}

/* FEATURES SECTION */
.features-sec{padding:52px 0;}
.sec-title{text-align:center;font-size:23px;font-weight:800;margin-bottom:6px;}
.sec-sub{text-align:center;color:var(--mu);font-size:14px;margin-bottom:34px;}
.feat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px;}
.feat-card{background:var(--sf);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--glass-brd);border-radius:var(--ra);padding:22px;text-align:center;transition:transform .25s,box-shadow .25s,border-color .25s;}
.feat-card:hover{transform:translateY(-4px);box-shadow:0 12px 30px rgba(59,130,246,.15);border-color:rgba(59,130,246,.35);}
.feat-icon{width:52px;height:52px;background:var(--orltt);border-radius:12px;display:flex;align-items:center;justify-content:center;margin:0 auto 12px;font-size:22px;}
.feat-card h3{font-size:15px;font-weight:700;margin-bottom:6px;}
.feat-card p{font-size:13px;color:var(--mu);line-height:1.5;}

/* HOW IT WORKS */
.how-sec{background:linear-gradient(135deg,#3b1f0f,#1a0f22 60%,#0B0A12);padding:56px 0;color:#fff;position:relative;overflow:hidden;border-top:1px solid var(--glass-brd);border-bottom:1px solid var(--glass-brd);}
.how-sec .sec-title,.how-sec .sec-sub{color:#fff;}
.how-sec .sec-sub{opacity:.75;}
.steps-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:20px;position:relative;z-index:1;}
@media(max-width:700px){.steps-grid{grid-template-columns:1fr 1fr;}}
.step-item{text-align:center;}
.step-num{width:52px;height:52px;background:rgba(59,130,246,.16);border-radius:50%;display:flex;align-items:center;justify-content:center;margin:0 auto 12px;font-size:22px;font-weight:800;border:2px solid rgba(59,130,246,.4);color:var(--or);}
.step-item h3{font-size:14px;font-weight:700;margin-bottom:6px;}
.step-item p{font-size:12px;opacity:.75;line-height:1.5;}
.how-blob{position:absolute;top:-60px;right:-80px;width:300px;height:300px;background:rgba(59,130,246,.1);border-radius:50%;animation:floatGlow 7s ease-in-out infinite;}

/* REVIEWS */
.reviews-sec{background:linear-gradient(135deg,#1a0f22,#0B0A12);padding:52px 0;overflow:hidden;}
.reviews-sec .sec-title,.reviews-sec .sec-sub{color:#fff;}
.reviews-sec .sec-sub{opacity:.75;}
.review-slider{position:relative;overflow:hidden;}
.review-track{display:flex;transition:transform .4s ease;gap:20px;}
.review-card{background:var(--sf);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--glass-brd);border-radius:var(--ra);padding:22px;min-width:320px;flex-shrink:0;}
.rv-avatar{width:48px;height:48px;background:var(--orltt);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:20px;margin-bottom:10px;}
.rv-name{font-weight:700;font-size:15px;margin-bottom:4px;}
.rv-stars{color:#FBBF24;font-size:14px;margin-bottom:8px;}
.rv-text{font-size:13px;color:var(--mu);line-height:1.5;}
.slider-btns{display:flex;gap:10px;justify-content:center;margin-top:20px;}
.slider-btn{width:40px;height:40px;background:rgba(59,130,246,.14);border:1px solid var(--ln);border-radius:50%;color:#fff;font-size:16px;cursor:pointer;transition:background .2s;}
.slider-btn:hover{background:rgba(59,130,246,.28);}

/* FAQ */
.faq-sec{padding:52px 0;}
.faq-item{border:1px solid var(--ln);border-radius:10px;margin-bottom:8px;overflow:hidden;background:var(--sf);backdrop-filter:blur(10px);}
.faq-q{display:flex;justify-content:space-between;align-items:center;padding:16px 18px;cursor:pointer;font-weight:600;font-size:14px;}
.faq-q:hover{color:var(--or);}
.faq-q .arrow{transition:transform .3s;color:var(--or);font-size:18px;}
.faq-a{display:none;padding:14px 18px;font-size:13px;color:var(--mu);line-height:1.6;border-top:1px solid var(--ln);}
.faq-item.open .faq-a{display:block;animation:fadeUp .25s ease;}
.faq-item.open .arrow{transform:rotate(45deg);}

/* DASHBOARD */
.dash-page{padding:24px 0 60px;}
.page-title{font-size:22px;font-weight:800;margin-bottom:20px;color:var(--tx);}

.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:0;}
.stat-card{background:var(--sf);backdrop-filter:blur(16px) saturate(140%);-webkit-backdrop-filter:blur(16px) saturate(140%);border:1.5px solid var(--glass-brd);border-radius:16px;padding:18px 16px;display:flex;align-items:center;gap:14px;box-shadow:var(--glow);animation:fadeUp .45s ease both;transition:transform .2s,border-color .2s;}
.stat-card:hover{transform:translateY(-3px);border-color:rgba(59,130,246,.4);}
.stat-icon-wrap{width:54px;height:54px;background:var(--orltt);border-radius:12px;display:flex;align-items:center;justify-content:center;flex-shrink:0;border:1.5px solid rgba(59,130,246,.3);}
.stat-icon{font-size:26px;}
.stat-info .stat-name{font-size:16px;font-weight:700;color:var(--tx);line-height:1.2;}
.stat-info .stat-label{font-size:12px;color:var(--mu);margin-top:3px;}
.stat-label{font-size:12px;color:var(--mu);}
.stat-val{font-size:20px;font-weight:800;color:var(--or);}

/* MINI CHART + RECENT ORDERS */
.dash-grid2{display:grid;grid-template-columns:1.1fr 1fr;gap:16px;margin:20px 0;}
@media(max-width:800px){.dash-grid2{grid-template-columns:1fr;}}
.panel-card{background:var(--sf);backdrop-filter:blur(16px) saturate(140%);-webkit-backdrop-filter:blur(16px) saturate(140%);border:1px solid var(--glass-brd);border-radius:16px;padding:20px;box-shadow:var(--glow);animation:fadeUp .5s ease both;}
.panel-card h3{font-size:14px;font-weight:700;margin-bottom:14px;display:flex;align-items:center;gap:8px;color:var(--tx);}
.chart-wrap{display:flex;align-items:flex-end;gap:8px;height:120px;padding-top:8px;}
.chart-bar-col{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px;height:100%;justify-content:flex-end;}
.chart-bar{width:100%;max-width:28px;border-radius:6px 6px 2px 2px;background:linear-gradient(180deg,var(--or),var(--ord));box-shadow:0 0 14px rgba(59,130,246,.35);animation:barGrow .6s cubic-bezier(.34,1.56,.64,1) both;transform-origin:bottom;}
@keyframes barGrow{from{transform:scaleY(0);}to{transform:scaleY(1);}}
.chart-lbl{font-size:10px;color:var(--mu);white-space:nowrap;}
.chart-empty{color:var(--mu);font-size:13px;text-align:center;padding:30px 0;}
.recent-list{display:flex;flex-direction:column;gap:10px;}
.recent-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border-radius:10px;background:rgba(255,255,255,.03);border:1px solid var(--glass-brd);transition:background .2s;}
.recent-row:hover{background:rgba(59,130,246,.06);}
.recent-row .rr-name{font-size:12.5px;font-weight:600;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.recent-row .rr-meta{font-size:11px;color:var(--mu);}
.recent-row .rr-price{font-size:12.5px;font-weight:700;color:var(--or);white-space:nowrap;}

/* ORDER FORM */
.orange-wave{width:100%;overflow:hidden;line-height:0;margin:0 -18px;width:calc(100% + 36px);}
.orange-wave svg{display:block;width:100%;}
.order-panel{background:var(--sf);backdrop-filter:blur(18px) saturate(140%);-webkit-backdrop-filter:blur(18px) saturate(140%);border:1px solid var(--glass-brd);border-radius:18px;padding:24px;margin-bottom:24px;box-shadow:var(--glow);}
.search-bar{display:flex;gap:10px;margin-bottom:16px;}
.search-bar input{flex:1;background:rgba(255,255,255,.04);border:1px solid var(--ln);border-radius:10px;padding:11px 14px;font-size:14px;font-family:'Inter',sans-serif;color:var(--tx);}
.search-bar input:focus{outline:2px solid var(--or);}
.price-display{background:linear-gradient(90deg,#93C5FD,#3B82F6,#2563EB);color:#1a1220;border-radius:12px;padding:16px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px;box-shadow:0 4px 20px rgba(59,130,246,.35);}
.price-display .p-label{font-size:13px;opacity:.85;font-weight:600;}
.price-display .p-val{font-size:22px;font-weight:800;display:flex;align-items:center;gap:8px;}
.price-display .label{font-size:13px;opacity:.85;font-weight:400;}
.plat-tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;}
.plat-tab{display:flex;align-items:center;gap:7px;background:var(--sf);border:1px solid var(--ln);border-radius:10px;padding:9px 14px;font-size:13px;font-weight:600;cursor:pointer;color:var(--tx);transition:all .15s;font-family:'Inter',sans-serif;}
.plat-tab:hover{border-color:var(--or);color:var(--or);}
.plat-tab.active{background:linear-gradient(90deg,var(--or),var(--ord));color:#fff;border-color:var(--or);box-shadow:0 3px 14px rgba(59,130,246,.35);}
.plat-tab .plat-ic{width:16px;height:16px;font-size:14px;line-height:1;display:inline-block;}
.info-box{background:rgba(59,130,246,.06);border:1.5px solid var(--ln);border-radius:12px;padding:16px;margin-bottom:16px;position:relative;}
.info-box::before{content:'⚠️ Sifariş Qaydaları';display:block;font-weight:700;font-size:13px;color:var(--or);margin-bottom:10px;}
.info-box p{font-size:12.5px;color:var(--mu);line-height:1.6;display:flex;align-items:flex-start;gap:8px;margin-bottom:5px;padding:6px 10px;background:rgba(255,255,255,.03);border-radius:6px;}
.info-box p:last-child{margin-bottom:0;}
.hint-text{font-size:12px;color:var(--mu);margin-top:4px;}

/* ORDERS TABLE */
.filter-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px;}
.ftab{background:var(--sf);border:1px solid var(--ln);border-radius:999px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer;color:var(--tx);transition:all .15s;white-space:nowrap;}
.ftab:hover{border-color:var(--or);color:var(--or);}
.ftab.active{background:linear-gradient(90deg,var(--or),var(--ord));color:#1a1220;border-color:var(--or);box-shadow:0 3px 14px rgba(59,130,246,.35);}
.table-card{background:var(--sf);backdrop-filter:blur(16px) saturate(140%);-webkit-backdrop-filter:blur(16px) saturate(140%);border:1px solid var(--glass-brd);border-radius:var(--ra);overflow:hidden;box-shadow:var(--glow);}
.search-orders{display:flex;gap:10px;padding:14px;border-bottom:1px solid var(--ln);}
.search-orders input{flex:1;background:rgba(255,255,255,.04);border:1px solid var(--ln);border-radius:10px;padding:9px 13px;font-size:13px;font-family:'Inter',sans-serif;color:var(--tx);}
.tbl-head{background:linear-gradient(90deg,var(--or),var(--ord));color:#1a1220;}
table.ot{width:100%;border-collapse:collapse;font-size:13px;}
table.ot th{padding:12px 12px;text-align:left;font-weight:700;white-space:nowrap;}
table.ot td{padding:11px 12px;border-bottom:1px solid var(--glass-brd);}
table.ot tr:last-child td{border-bottom:none;}
table.ot tr:hover td{background:rgba(59,130,246,.06);}
.pill{padding:3px 10px;border-radius:999px;font-size:11px;font-weight:700;}
.pill-wait{background:rgba(251,191,36,.15);color:#FBBF24;}
.pill-done{background:rgba(52,211,153,.15);color:#34D399;}
.pill-run{background:rgba(96,165,250,.15);color:#60A5FA;}
.pill-cancel{background:rgba(248,113,113,.15);color:#F87171;}
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
.bonus-badge{position:absolute;top:18px;left:22px;background:linear-gradient(135deg,var(--or),var(--ord));color:#1a1220;font-size:11px;font-weight:800;padding:4px 10px;border-radius:999px;box-shadow:0 4px 12px rgba(59,130,246,.4);z-index:2;animation:floatGlow 3s ease-in-out infinite;}
.copy-btn-anim{background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.3);color:#fff;padding:6px 14px;border-radius:8px;font-size:12px;cursor:pointer;font-family:'Inter',sans-serif;font-weight:600;transition:all .25s cubic-bezier(.34,1.56,.64,1);}
.copy-btn-anim:hover{background:rgba(255,255,255,.3);transform:translateY(-1px);}
.copy-btn-anim:active{transform:scale(.94);}
.copy-btn-anim.copied{background:linear-gradient(135deg,#34D399,#10B981);border-color:transparent;transform:scale(1.08);}
@keyframes copyPop{0%{transform:scale(1);}40%{transform:scale(1.18);}100%{transform:scale(1);}}
.copy-btn-anim.copied span{display:inline-block;animation:copyPop .4s ease;}
.note-list{background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.3);border-radius:10px;padding:14px 16px;margin:16px 0;}
.note-list p{font-size:13px;color:#FBBF24;line-height:1.6;display:flex;align-items:flex-start;gap:8px;margin-bottom:4px;}
.note-list p:last-child{margin-bottom:0;}

/* TICKETS */
.ticket-form{background:var(--sf);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--glass-brd);border-radius:var(--ra);padding:22px;margin-bottom:24px;box-shadow:var(--glow);}
.ticket-tbl{background:var(--sf);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--glass-brd);border-radius:var(--ra);overflow:hidden;box-shadow:var(--glow);}
.ticket-tbl-head{background:linear-gradient(90deg,var(--or),var(--ord));color:#1a1220;display:grid;grid-template-columns:60px 1fr 100px 140px;padding:12px 16px;font-size:13px;font-weight:700;}
.ticket-row{display:grid;grid-template-columns:60px 1fr 100px 140px;padding:12px 16px;border-bottom:1px solid var(--glass-brd);font-size:13px;align-items:center;}
.ticket-row:last-child{border-bottom:none;}
.ticket-row:hover{background:rgba(59,130,246,.06);}
.ticket-row{cursor:pointer;}
.ticket-row .unread-dot{width:8px;height:8px;border-radius:50%;background:#F87171;display:inline-block;margin-right:6px;box-shadow:0 0 8px rgba(248,113,113,.6);}

/* TICKET CHAT */
.chat-back{display:inline-flex;align-items:center;gap:6px;color:var(--mu);font-size:13px;margin-bottom:14px;}
.chat-back:hover{color:var(--or);}
.chat-wrap{display:flex;flex-direction:column;gap:14px;padding:20px;background:var(--sf);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--glass-brd);border-radius:var(--ra);box-shadow:var(--glow);margin-bottom:18px;max-height:520px;overflow-y:auto;}
.chat-msg{max-width:78%;animation:fadeUp .3s ease;}
.chat-msg.user{align-self:flex-end;}
.chat-msg.admin{align-self:flex-start;}
.chat-bubble{padding:12px 14px;border-radius:14px;font-size:14px;line-height:1.55;white-space:pre-wrap;word-break:break-word;}
.chat-msg.user .chat-bubble{background:linear-gradient(135deg,var(--or),var(--ord));color:#1a1220;border-bottom-right-radius:4px;}
.chat-msg.admin .chat-bubble{background:rgba(255,255,255,.06);color:var(--tx);border:1px solid var(--glass-brd);border-bottom-left-radius:4px;}
.chat-meta{font-size:11px;color:var(--mu);margin-top:4px;padding:0 4px;}
.chat-msg.user .chat-meta{text-align:right;}
.chat-form{background:var(--sf);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--glass-brd);border-radius:var(--ra);padding:18px;box-shadow:var(--glow);}
.chat-form textarea{width:100%;min-height:70px;background:rgba(255,255,255,.03);border:1px solid var(--glass-brd);border-radius:10px;padding:12px;color:var(--tx);font-family:'Inter',sans-serif;font-size:14px;margin-bottom:10px;resize:vertical;}
.chat-form-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
.chat-form-row input[type=file]{flex:1;min-width:160px;font-size:12px;color:var(--mu);}
.ticket-status-pill{display:inline-block;margin-left:10px;}

/* PROFILE */
.profile-page{max-width:640px;padding:24px 0 64px;}
.profile-hero{display:flex;flex-direction:column;align-items:center;gap:14px;margin-bottom:26px;}
.avatar-wrap{position:relative;width:104px;height:104px;}
.avatar-img{width:104px;height:104px;border-radius:50%;object-fit:cover;border:3px solid var(--or);box-shadow:0 8px 26px rgba(59,130,246,.35);}
.avatar-placeholder{width:104px;height:104px;border-radius:50%;background:linear-gradient(135deg,var(--or),var(--ord));display:flex;align-items:center;justify-content:center;font-size:38px;color:#1a1220;font-weight:800;border:3px solid var(--or);box-shadow:0 8px 26px rgba(59,130,246,.35);}
.avatar-edit{position:absolute;bottom:0;right:0;width:32px;height:32px;border-radius:50%;background:var(--sf-solid);border:2px solid var(--bg);display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:14px;}
.profile-card{background:var(--sf);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--glass-brd);border-radius:var(--ra);padding:20px 22px;margin-bottom:18px;box-shadow:var(--glow);}
.profile-card h3{font-size:15px;font-weight:700;margin-bottom:14px;color:var(--or);display:flex;align-items:center;gap:8px;}
.profile-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--glass-brd);}
.profile-row:last-child{border-bottom:none;}
.profile-row .pr-label{font-size:13px;color:var(--mu);}
.profile-row .pr-val{font-size:13px;font-weight:600;color:var(--tx);}
.api-key-box{font-family:'IBM Plex Mono',monospace;font-size:12px;background:rgba(255,255,255,.04);border:1px solid var(--glass-brd);border-radius:8px;padding:10px 12px;word-break:break-all;color:var(--or);margin-bottom:10px;}
.btn-sm{width:auto;padding:8px 16px;font-size:13px;display:inline-flex;align-items:center;gap:6px;}
.btn-outline{background:transparent;border:1px solid var(--glass-brd);color:var(--tx);}
.btn-outline:hover{border-color:var(--or);color:var(--or);}
.btn-danger{background:linear-gradient(135deg,#F87171,#DC2626);color:#fff;border:none;}

/* ANNOUNCEMENT */
.ann-card{background:var(--sf);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--glass-brd);border-radius:var(--ra);overflow:hidden;margin-bottom:14px;box-shadow:var(--glow);}
.ann-head{background:linear-gradient(90deg,var(--or),var(--ord));color:#1a1220;padding:10px 16px;font-size:13px;font-weight:700;}
.ann-body{padding:14px 16px;font-size:14px;line-height:1.6;white-space:pre-wrap;color:var(--tx);}

/* ADMIN */
.admin-wrap{padding:28px 0 64px;max-width:700px;}
.admin-sec{background:var(--sf);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--glass-brd);border-radius:var(--ra);padding:22px;margin-bottom:20px;box-shadow:var(--glow);}
.admin-sec h3{font-size:16px;font-weight:700;margin-bottom:16px;color:var(--or);}

/* FOOTER */
.footer{background:linear-gradient(90deg,#1a0f22,#0B0A12);border-top:1px solid var(--glass-brd);color:var(--mu);padding:20px 0;text-align:center;font-size:13px;}
.footer a{color:var(--or);}

/* ORDER VALIDATION */
.err-hint{display:block;font-size:12px;margin-top:4px;font-weight:600;}
.field input{transition:border-color .2s ease;}
.btn-or:disabled{opacity:.5;cursor:not-allowed;filter:grayscale(.3);}

/* TOASTS */
.toast-container{position:fixed;top:20px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:10px;max-width:320px;}
.toast{background:rgba(255,255,255,.97);backdrop-filter:blur(16px) saturate(140%);-webkit-backdrop-filter:blur(16px) saturate(140%);border:1px solid rgba(20,10,40,.08);border-left:4px solid var(--or);border-radius:12px;padding:13px 16px;font-size:13px;color:#1A1625;box-shadow:0 10px 30px rgba(0,0,0,.18);animation:toastIn .35s cubic-bezier(.34,1.56,.64,1) both;display:flex;align-items:flex-start;gap:10px;}
.toast.ok{border-left-color:#34D399;}
.toast.err{border-left-color:#F87171;}
.toast.out{animation:toastOut .3s ease forwards;}
@keyframes toastIn{from{opacity:0;transform:translateX(40px);}to{opacity:1;transform:translateX(0);}}
@keyframes toastOut{from{opacity:1;transform:translateX(0);}to{opacity:0;transform:translateX(40px);}}

@media(max-width:640px){
  .stat-grid{grid-template-columns:1fr 1fr;}
  .filter-tabs{overflow-x:auto;flex-wrap:nowrap;padding-bottom:4px;}
  table.ot{min-width:600px;}
  .table-card{overflow-x:auto;}
  .steps-grid{grid-template-columns:1fr 1fr;}
  .ticket-tbl-head,.ticket-row{grid-template-columns:50px 1fr 80px;}
  .toast-container{left:12px;right:12px;max-width:none;}
}

/* ===== LIGHT THEME ===== */
.field select option{background:#fff;color:#1A1625;}
[data-theme="dark"] .field select option{background:#181622;color:#F1F0F5;}
/* Qeyd: acig (light) rengler indi yuxarida :root-da defolt olaraq teyin edilib. */

/* ===== PERFORMANCE / RESPONSIVE FEEL (target ~120fps feel) ===== */
*{-webkit-tap-highlight-color:transparent;}
a,button,.btn-or,.ftab,.hamburger,.slider-btn,input[type="submit"],.icon-btn,.chip{touch-action:manipulation;}
.btn-or,.btn-google,.ftab,.chip,.icon-btn,.nav-bell,.theme-toggle{transition:transform .1s cubic-bezier(.2,.8,.2,1),box-shadow .12s ease,filter .12s ease,background .12s ease,border-color .12s ease;}
.btn-or:active,.btn-google:active,.ftab:active,.chip:active,.icon-btn:active,.nav-bell:active,.theme-toggle:active{transform:scale(.96);filter:brightness(.97);}
.sb-nav a{transition:background .12s ease,color .12s ease,border-color .12s ease;}
.feat-card,.review-card{transition:transform .18s cubic-bezier(.2,.8,.2,1),box-shadow .18s ease,border-color .18s ease;}
button,input,select,textarea{will-change:transform;}
.btn-or[disabled]{opacity:.65;cursor:wait;filter:grayscale(.15);}
.btn-or .spin{display:inline-block;width:14px;height:14px;border:2px solid rgba(0,0,0,.25);border-top-color:#1a1220;border-radius:50%;animation:spin .6s linear infinite;margin-right:8px;vertical-align:-2px;}
@keyframes spin{to{transform:rotate(360deg);}}

/* ===== THEME TOGGLE ===== */
.theme-toggle{background:rgba(59,130,246,.12);border:1px solid var(--ln);color:var(--or);width:38px;height:38px;border-radius:50%;cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;}
.theme-toggle:hover{background:rgba(59,130,246,.22);}

/* ===== NOTIFICATION BELL ===== */
.nav-bell{position:relative;background:rgba(59,130,246,.12);border:1px solid var(--ln);color:var(--or);width:38px;height:38px;border-radius:50%;cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;}
.nav-bell:hover{background:rgba(59,130,246,.22);}
.bell-badge{position:absolute;top:-4px;right:-4px;background:linear-gradient(135deg,#F87171,#DC2626);color:#fff;font-size:10px;font-weight:800;min-width:16px;height:16px;padding:0 4px;border-radius:999px;display:flex;align-items:center;justify-content:center;box-shadow:0 0 10px rgba(248,113,113,.5);}
.notif-panel{position:absolute;top:52px;right:0;width:320px;max-height:400px;overflow-y:auto;background:var(--sf-solid);border:1px solid var(--glass-brd);border-radius:14px;box-shadow:var(--glow);z-index:60;display:none;animation:fadeUp .18s ease both;}
.notif-panel.open{display:block;}
.notif-head{padding:14px 16px;font-weight:700;font-size:14px;border-bottom:1px solid var(--glass-brd);color:var(--or);}
.notif-item{padding:12px 16px;border-bottom:1px solid var(--glass-brd);font-size:13px;}
.notif-item:last-child{border-bottom:none;}
.notif-item .ni-title{font-weight:600;margin-bottom:3px;}
.notif-item .ni-meta{color:var(--mu);font-size:11px;}
.notif-empty{padding:24px 16px;text-align:center;color:var(--mu);font-size:13px;}
.topuser-wrap{display:flex;align-items:center;gap:10px;position:relative;}

/* ===== AI ASSISTANT ===== */
.ai-box{background:linear-gradient(135deg,rgba(59,130,246,.12),rgba(168,85,247,.08));border:1px solid var(--ln);border-radius:14px;padding:16px;margin-bottom:16px;}
.ai-box h4{font-size:14px;margin-bottom:10px;color:var(--or);display:flex;align-items:center;gap:6px;}
.ai-row{display:flex;gap:8px;}
.ai-row input{flex:1;background:rgba(255,255,255,.05);border:1.5px solid var(--ln);border-radius:10px;padding:11px 14px;color:var(--tx);font-size:14px;font-family:'Inter',sans-serif;}
.ai-row button{background:linear-gradient(90deg,#93C5FD,var(--or),var(--ord));color:#1a1220;border:none;border-radius:10px;padding:0 16px;font-weight:700;font-size:13px;cursor:pointer;white-space:nowrap;}
.ai-result{margin-top:10px;font-size:12px;color:var(--mu);}

/* ===== FAVORITES / RECENT CHIPS ===== */
.chip-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;}
.chip{background:rgba(255,255,255,.05);border:1px solid var(--ln);border-radius:999px;padding:6px 12px;font-size:12px;color:var(--tx);cursor:pointer;display:inline-flex;align-items:center;gap:6px;}
.chip:hover{border-color:var(--or);color:var(--or);}
.chip .chip-x{opacity:.6;font-size:11px;}
.chip .chip-x:hover{opacity:1;color:#F87171;}
.fav-heart{cursor:pointer;font-size:18px;user-select:none;transition:transform .15s ease;}
.fav-heart:active{transform:scale(1.3);}
.fav-heart.on{filter:drop-shadow(0 0 6px rgba(59,130,246,.6));}
.svc-row-flex{display:flex;gap:10px;align-items:center;}
.svc-row-flex select{flex:1;}
.chip-empty{font-size:12px;color:var(--mu);}

/* ===== ORDER STATS CHART ===== */
.stats-chart-card{background:var(--sf);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--glass-brd);border-radius:var(--ra);padding:18px;margin-bottom:18px;}
.stats-chart-card h3{font-size:15px;margin-bottom:12px;}
.stats-legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;font-size:12px;color:var(--mu);}
.stats-legend span{display:inline-flex;align-items:center;gap:6px;}
.stats-legend i{width:10px;height:10px;border-radius:3px;display:inline-block;}

/* ===== ADMIN GRADIENT ===== */
.adm-head-gradient{background:linear-gradient(90deg,var(--or),var(--ord),#a855f7);}
.adm-search{width:100%;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:10px 12px;font-size:13px;margin-bottom:12px;}
.adm-stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px;}
.adm-stat{background:linear-gradient(135deg,var(--or),var(--ord));color:#fff;border-radius:12px;padding:16px;text-align:center;box-shadow:0 6px 20px rgba(59,130,246,.3);}
.adm-stat .n{font-size:22px;font-weight:800;}
.adm-stat .l{font-size:11px;opacity:.9;margin-top:4px;}
.adm-btn-gradient{background:linear-gradient(90deg,#93C5FD,var(--or),var(--ord));color:#1a1220 !important;border:none;font-weight:800;}

/* ===== VERIFY EMAIL BANNER ===== */
.verify-banner{background:linear-gradient(90deg,rgba(251,191,36,.18),rgba(59,130,246,.1));border-bottom:1px solid rgba(251,191,36,.35);color:#FDE68A;font-size:13px;padding:10px 18px;text-align:center;display:flex;align-items:center;justify-content:center;gap:10px;flex-wrap:wrap;}
.verify-banner-btn{background:rgba(253,230,138,.16);border:1px solid rgba(253,230,138,.4);color:#FDE68A;font-size:12px;font-weight:700;padding:5px 12px;border-radius:999px;cursor:pointer;}
.verify-banner-btn:hover{background:rgba(253,230,138,.28);}

/* ===== FORGOT / RESET PASSWORD LINK ===== */
.link-muted{color:var(--mu);font-size:12px;text-decoration:underline;}
.link-muted:hover{color:var(--or);}

/* ===== EXTRA MOBILE / PROFESSIONAL POLISH ===== */
@media(max-width:480px){
  .hero-login-card{padding:20px;border-radius:14px;}
  .topbar-inner{height:64px;}
  .brand-text{font-size:18px;}
  .bal-badge{padding:5px 10px;font-size:12px;}
  .sec-title{font-size:20px;}
  .verify-banner{font-size:12px;padding:8px 12px;}
  .page-title{font-size:20px;}
  .field input,.field select,.field textarea{font-size:16px;} /* iOS zoom-un qarsisini alir */

  /* ===== KOMPAKT MOBİL DIZAYN (dashboard + sifariş paneli PC ölçüsündə görünməsin) ===== */
  .wrap{padding:0 12px;}
  .dash-page{padding:16px 0 32px;}
  .stat-grid{gap:8px;}
  .stat-card{padding:12px 10px;gap:10px;border-radius:12px;}
  .stat-icon-wrap{width:38px;height:38px;border-radius:9px;}
  .stat-icon{font-size:18px;}
  .stat-info .stat-name{font-size:13px;}
  .stat-info .stat-label{font-size:10.5px;}
  .order-panel{padding:14px;border-radius:14px;margin-bottom:16px;}
  .search-bar{margin-bottom:12px;}
  .search-bar input{padding:9px 12px;font-size:14px;}
  .plat-tabs{gap:6px;margin-bottom:12px;}
  .plat-tab{padding:7px 10px;font-size:12px;gap:5px;}
  .plat-tab .plat-ic{width:14px;height:14px;font-size:12px;}
  .cs-trigger{padding:10px 12px;font-size:13px;}
  .field{margin-bottom:11px;gap:5px;}
  .field label{font-size:12px;}
  .field input,.field select,.field textarea{padding:10px 12px;}
  .price-display{padding:12px 14px;margin-bottom:12px;}
  .price-display .p-val{font-size:18px;}
  .price-display .p-label{font-size:11.5px;}
  .btn-or{padding:12px;font-size:14px;}
  .info-box{padding:12px;}
  .info-box::before{font-size:12px;margin-bottom:8px;}
  .info-box p{font-size:11.5px;padding:5px 8px;}
}
@media(max-width:640px){
  .stats-chart-card{padding:14px;}
}

/* ===== SIDEBAR BAŞLIĞI HƏMİŞƏ TAM GÖRÜNSÜN (kəsilmə problemi düzəlişi) ===== */
.sb-head{position:sticky;top:0;z-index:2;}
.sb-head .sb-name{gap:6px;}
.sb-verified-tag{display:inline-flex;align-items:center;gap:3px;background:rgba(255,255,255,.22);font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:999px;margin-left:6px;letter-spacing:.2px;}

/* ===== BOŞ VƏZİYYƏT (EMPTY STATE) İLLÜSTRASİYASI ===== */
.empty-state{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:38px 16px;gap:6px;}
.es-illus{width:74px;height:74px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:radial-gradient(circle at 35% 30%,rgba(59,130,246,.28),rgba(59,130,246,.06) 70%);border:1px solid rgba(59,130,246,.22);margin-bottom:6px;animation:esFloat 3.2s ease-in-out infinite;}
.es-illus-icon{font-size:32px;filter:drop-shadow(0 4px 10px rgba(59,130,246,.25));}
.es-title{font-weight:700;font-size:14px;color:var(--tx);}
.es-sub{font-size:12.5px;color:var(--mu);max-width:280px;line-height:1.5;}
.es-cta{margin-top:6px;}
@keyframes esFloat{0%,100%{transform:translateY(0);}50%{transform:translateY(-6px);}}

/* ===== YÜKLƏNMƏ SKELETONU (səhifə keçidləri üçün) ===== */
#page-skeleton{position:fixed;inset:0;z-index:9997;background:var(--bg);display:none;opacity:0;transition:opacity .18s ease;padding-top:74px;overflow:hidden;}
#page-skeleton.active{display:block;opacity:1;}
#page-loading-bar{position:fixed;top:0;left:0;height:3px;width:0%;background:linear-gradient(90deg,var(--or),var(--ord));z-index:9999;box-shadow:0 0 10px rgba(59,130,246,.7);transition:width .35s ease;}
#page-loading-bar.active{width:78%;}
.skel-wrap{max-width:1100px;margin:0 auto;padding:24px 20px;}
.skel-row{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:20px;}
.skel-card{height:78px;border-radius:14px;}
.skel-block{height:220px;border-radius:16px;margin-bottom:16px;}
.skel-line{height:14px;border-radius:7px;margin-bottom:10px;}
.skel-shine{background:linear-gradient(90deg,rgba(140,130,160,.10) 25%,rgba(140,130,160,.22) 37%,rgba(140,130,160,.10) 63%);background-size:400% 100%;animation:skelShine 1.4s ease infinite;}
@keyframes skelShine{0%{background-position:100% 0;}100%{background-position:0 0;}}
@media(max-width:700px){.skel-row{grid-template-columns:repeat(2,1fr);}}

/* ===== STATİSTİKA LENTİ (ana səhifə) ===== */
.stats-strip{background:linear-gradient(90deg,rgba(59,130,246,.14),rgba(168,85,247,.10));border-top:1px solid var(--glass-brd);border-bottom:1px solid var(--glass-brd);padding:26px 0;}
.stats-strip-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:18px;max-width:1100px;margin:0 auto;padding:0 20px;text-align:center;}
.stats-strip-num{font-size:26px;font-weight:800;background:linear-gradient(90deg,var(--or),#EC4899);-webkit-background-clip:text;background-clip:text;color:transparent;}
.stats-strip-lbl{font-size:12.5px;color:var(--mu);margin-top:4px;font-weight:600;}
@media(max-width:700px){.stats-strip-grid{grid-template-columns:repeat(2,1fr);gap:20px;}}

/* ===== TRUST / SSL NİŞANLARI (footer) ===== */
.trust-strip{display:flex;flex-wrap:wrap;justify-content:center;gap:10px;padding:14px 16px 4px;}
.trust-badge{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:var(--mu);background:rgba(255,255,255,.04);border:1px solid var(--glass-brd);padding:6px 12px;border-radius:999px;}
.footer-links{display:flex;justify-content:center;gap:16px;flex-wrap:wrap;font-size:12px;padding:4px 16px 0;}
.footer-links a{color:var(--mu);text-decoration:none;}
.footer-links a:hover{color:var(--or);}

/* ===== HÜQUQİ SƏHİFƏLƏR (Şərtlər / Məxfilik) ===== */
.legal-wrap{max-width:760px;margin:0 auto;padding:48px 20px 70px;}
.legal-wrap h1{font-size:26px;margin-bottom:6px;}
.legal-wrap .legal-updated{color:var(--mu);font-size:12.5px;margin-bottom:28px;}
.legal-wrap h2{font-size:17px;margin:26px 0 10px;color:var(--or);}
.legal-wrap p,.legal-wrap li{font-size:14.5px;line-height:1.8;color:var(--tx);}
.legal-wrap ul{padding-left:20px;margin:8px 0;}
.legal-wrap li{margin-bottom:4px;}

/* ===== QEYDİYYAT — ŞƏRTLƏR CHECKBOX ===== */
.terms-check{display:flex;align-items:flex-start;gap:8px;font-size:12.5px;color:var(--mu);margin:4px 0 14px;line-height:1.5;}
.terms-check input{margin-top:3px;flex-shrink:0;width:16px;height:16px;accent-color:var(--or);}
.terms-check a{color:var(--or);text-decoration:underline;}
"""

JS = """
/* ===== THEME TOGGLE ===== */
function applyThemeIcon(){
  var t=document.documentElement.getAttribute('data-theme')||'light';
  var btn=document.getElementById('theme-toggle-btn');
  if(btn)btn.innerText = t==='light' ? '☀️' : '🌙';
}
function toggleTheme(){
  var cur=document.documentElement.getAttribute('data-theme')||'light';
  var next=cur==='light'?'dark':'light';
  document.documentElement.setAttribute('data-theme',next);
  try{localStorage.setItem('bp_theme',next);}catch(e){}
  applyThemeIcon();
}
document.addEventListener('DOMContentLoaded',applyThemeIcon);

/* ===== SIDEBAR-IN TOPBAR ALTINDA BAŞLAMASI ÜÇÜN HÜNDÜRLÜK SİNXRONU ===== */
function syncTopbarHeight(){
  var tb=document.getElementById('site-topbar');
  if(tb){document.documentElement.style.setProperty('--topbar-h', tb.offsetHeight+'px');}
}
document.addEventListener('DOMContentLoaded',syncTopbarHeight);
window.addEventListener('resize',syncTopbarHeight);
window.addEventListener('load',syncTopbarHeight);

/* ===== DİL SEÇİMİ (AZ/EN/RU) ===== */
document.addEventListener('DOMContentLoaded',function(){
  var sel=document.getElementById('lang-select');
  if(!sel)return;
  var m=document.cookie.match(/googtrans=\\/az\\/([a-z]{2})/);
  var lang=m?m[1]:(function(){try{return localStorage.getItem('bp_lang')||'az';}catch(e){return 'az';}})();
  sel.value=lang;
});

/* ===== NOTIFICATION PANEL ===== */
function toggleNotifPanel(){
  var p=document.getElementById('notif-panel');
  if(!p)return;
  p.classList.toggle('open');
}
document.addEventListener('click',function(e){
  var p=document.getElementById('notif-panel');
  if(!p||!p.classList.contains('open'))return;
  if(p.contains(e.target)||e.target.closest('.nav-bell'))return;
  p.classList.remove('open');
});

/* ===== FAVORITES (localStorage) ===== */
function getFavs(){
  try{return JSON.parse(localStorage.getItem('bp_favs')||'[]');}catch(e){return [];}
}
function setFavs(f){try{localStorage.setItem('bp_favs',JSON.stringify(f));}catch(e){}}
function toggleFav(id,name){
  var favs=getFavs();
  var idx=favs.findIndex(function(f){return String(f.id)===String(id);});
  if(idx>-1)favs.splice(idx,1);
  else{favs.unshift({id:id,name:name});favs=favs.slice(0,10);}
  setFavs(favs);
  renderFavChips();
}
function renderFavChips(){
  var box=document.getElementById('fav-chips');
  if(!box)return;
  var favs=getFavs();
  if(!favs.length){box.innerHTML='<span class="chip-empty">❤️ Hələ seçilmiş xidmət yoxdur — ürək ikonuna basaraq əlavə edin.</span>';return;}
  box.innerHTML=favs.map(function(f){
    return '<span class="chip" onclick="selectProductById(\\''+f.id+'\\')">❤️ '+f.name+'</span>';
  }).join('');
}
function selectProductById(id){
  var sel=document.getElementById('svc-select');
  if(!sel)return;
  for(var i=0;i<sel.options.length;i++){
    if(String(sel.options[i].value)===String(id)){sel.selectedIndex=i;sel.dispatchEvent(new Event('change'));break;}
  }
  updateFavHeart();
}
function updateFavHeart(){
  var sel=document.getElementById('svc-select');
  var heart=document.getElementById('fav-heart-btn');
  if(!sel||!heart)return;
  var opt=sel.options[sel.selectedIndex];
  if(!opt)return;
  var favs=getFavs();
  var on=favs.some(function(f){return String(f.id)===String(opt.value);});
  heart.classList.toggle('on',on);
  heart.innerText=on?'❤️':'🤍';
}
function currentSvcFavToggle(){
  var sel=document.getElementById('svc-select');
  if(!sel)return;
  var opt=sel.options[sel.selectedIndex];
  if(!opt)return;
  toggleFav(opt.value, opt.innerText.split(' — ')[0]);
  updateFavHeart();
}

/* ===== AI ASSISTANT (keyword match, client-side) ===== */
function aiFindService(){
  var q=(document.getElementById('ai-input')||{}).value||'';
  var out=document.getElementById('ai-result');
  if(!q.trim()){if(out)out.innerText='Nə axtardığınızı yazın, məsələn: "instagram izləyici".';return;}
  var all=(typeof allProds!=='undefined')?Object.values(allProds).flat():[];
  var words=q.toLowerCase().split(/\\s+/).filter(Boolean);
  var scored=all.map(function(p){
    var name=p.name.toLowerCase();
    var score=0;
    words.forEach(function(w){if(name.indexOf(w)>-1)score++;});
    return {p:p,score:score};
  }).filter(function(s){return s.score>0;}).sort(function(a,b){return b.score-a.score;});
  if(!scored.length){if(out)out.innerText='🤖 Uyğun xidmət tapılmadı. Başqa açar sözlə yenidən yoxlayın.';return;}
  var best=scored[0].p;
  selectProductById(best.id);
  if(out)out.innerText='🤖 Tapıldı: '+best.name+' seçildi.';
}

function toggleSidebar(f){
  var sb=document.getElementById('sb'),ov=document.getElementById('sb-ov');
  var op=f!==undefined?f:!sb.classList.contains('open');
  sb.classList.toggle('open',op);ov.classList.toggle('open',op);
  if(op&&sb)sb.scrollTop=0; /* menyu her acilanda basliq (ad/verified) hemise tam gorunsun */
}

/* ===== SEHIFE KECIDI ZAMANI YUKLENME SKELETONU / PROGRESS BAR ===== */
function showPageLoading(){
  var bar=document.getElementById('page-loading-bar');
  var skel=document.getElementById('page-skeleton');
  if(bar){bar.classList.add('active');}
  if(skel){skel.classList.add('active');}
}
function hidePageLoading(){
  var bar=document.getElementById('page-loading-bar');
  var skel=document.getElementById('page-skeleton');
  if(bar)bar.classList.remove('active');
  if(skel)skel.classList.remove('active');
}
document.addEventListener('DOMContentLoaded',function(){
  document.addEventListener('click',function(e){
    var a=e.target.closest('a');
    if(!a)return;
    var href=a.getAttribute('href')||'';
    if(!href||href.charAt(0)==='#'||href.indexOf('javascript:')===0)return;
    if(a.target==='_blank'||a.hasAttribute('download'))return;
    if(e.metaKey||e.ctrlKey||e.shiftKey||e.button===1)return;
    try{
      var url=new URL(href,location.href);
      if(url.origin!==location.origin)return;
    }catch(err){return;}
    var sbEl=document.getElementById('sb'), ovEl=document.getElementById('sb-ov');
    if(sbEl&&sbEl.classList.contains('open')){sbEl.classList.remove('open');if(ovEl)ovEl.classList.remove('open');}
    showPageLoading();
  });
  document.querySelectorAll('form').forEach(function(f){
    f.addEventListener('submit',function(){ if(f.method&&f.method.toLowerCase()!=='get')showPageLoading(); });
  });
});
window.addEventListener('pageshow',function(){
  var bar=document.getElementById('page-loading-bar');
  var skel=document.getElementById('page-skeleton');
  if(bar)bar.classList.remove('active');
  if(skel)skel.classList.remove('active');
});
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
    var txt=document.getElementById('copy-btn-txt');
    if(btn&&txt){
      btn.classList.add('copied');
      txt.innerText='✅ Kopyalandı';
      setTimeout(function(){
        btn.classList.remove('copied');
        txt.innerText='📋 Kopyala';
      },2000);
    }
    if(typeof showToast==='function')showToast('Kart nömrəsi kopyalandı','ok');
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
  var etaEl=document.getElementById('eta-val');
  var qtyWarn=document.getElementById('qty-warn');
  var linkInput=document.getElementById('link-input');
  var linkWarn=document.getElementById('link-warn');
  var submitBtn=document.getElementById('order-submit');

  function isValidLink(v){
    if(!v)return false;
    try{
      var u=new URL(v);
      return (u.protocol==='http:'||u.protocol==='https:') && u.hostname.indexOf('.')>-1;
    }catch(e){return false;}
  }
  function checkAll(){
    var qtyOk=true, linkOk=true;
    if(qi&&si){
      var opt=si.options[si.selectedIndex];
      var min=parseInt(opt.dataset.min||1), max=parseInt(opt.dataset.max||1e9);
      var qty=parseInt(qi.value)||0;
      qtyOk = qty>=min && qty<=max;
      if(qtyWarn)qtyWarn.style.display=qtyOk?'none':'inline-block';
      qi.style.borderColor=qtyOk?'':'#F87171';
    }
    if(linkInput){
      var v=linkInput.value.trim();
      linkOk = v.length===0 || isValidLink(v);
      if(linkWarn)linkWarn.style.display=(v.length>0&&!isValidLink(v))?'block':'none';
      linkInput.style.borderColor=(v.length>0&&!isValidLink(v))?'#F87171':'';
    }
    if(submitBtn)submitBtn.disabled=!(qtyOk&&linkOk);
    return qtyOk&&linkOk;
  }
  function updatePrice(){
    if(!si||!pi)return;
    var opt=si.options[si.selectedIndex];
    if(!opt)return;
    var price=parseFloat(opt.dataset.price||0);
    var qty=parseInt(qi?qi.value:1)||1;
    pi.innerText=(price*qty/1000).toFixed(4)+' ₼';
    if(opt.dataset.min&&minEl)minEl.innerText='Min: '+opt.dataset.min;
    if(opt.dataset.max&&maxEl)maxEl.innerText=' - Max: '+opt.dataset.max;
    if(etaEl){
      etaEl.value = '~30 dəqiqə - 1 saat';
    }
    checkAll();
  }
  if(si){si.addEventListener('change',updatePrice);}
  if(qi){qi.addEventListener('input',updatePrice);qi.addEventListener('keyup',updatePrice);qi.addEventListener('change',updatePrice);}
  if(linkInput){linkInput.addEventListener('input',checkAll);}
  updatePrice();
});

/* ===== TOASTS ===== */
function getToastBox(){
  var box=document.getElementById('toast-box');
  if(!box){
    box=document.createElement('div');
    box.id='toast-box';box.className='toast-container';
    document.body.appendChild(box);
  }
  return box;
}
function showToast(msg,type){
  var box=getToastBox();
  var t=document.createElement('div');
  t.className='toast '+(type||'ok');
  var ic=type==='err'?'⛔':'✅';
  t.innerHTML='<span>'+ic+'</span><span>'+msg+'</span>';
  box.appendChild(t);
  setTimeout(function(){
    t.classList.add('out');
    setTimeout(function(){t.remove();},300);
  },4200);
}
document.addEventListener('DOMContentLoaded',function(){
  document.querySelectorAll('.ok-box[data-toast],.err-box[data-toast]').forEach(function(el){
    showToast(el.textContent.trim(),el.classList.contains('err-box')?'err':'ok');
  });
});

/* ===== PAGE TRANSITIONS (fast, non-blocking) ===== */
document.addEventListener('DOMContentLoaded',function(){
  document.querySelectorAll('a[href^="/"]').forEach(function(a){
    a.addEventListener('click',function(e){
      if(a.target==='_blank'||e.metaKey||e.ctrlKey)return;
      var href=a.getAttribute('href');
      if(!href||href.startsWith('#'))return;
      // Instant visual feedback without delaying navigation
      a.style.opacity='.7';
      document.body.style.transition='opacity .12s ease';
      document.body.style.opacity='.85';
      // Let the browser navigate immediately — no artificial setTimeout delay
    });
  });
  document.querySelectorAll('form').forEach(function(f){
    f.addEventListener('submit',function(){
      var btn=f.querySelector('button[type=submit],.btn-or');
      if(btn && !btn.disabled){
        btn.dataset.origText=btn.innerHTML;
        btn.innerHTML='<span class="spin"></span>Göndərilir...';
        btn.disabled=true;
      }
    });
  });
});

/* ===== REAL-TIME SIFARIS STATUSU ===== */
/* ===== BROWSER PUSH BİLDİRİŞLƏRİ ===== */
function askPushPermission(){
  if(!('Notification' in window)){ alert('Brauzeriniz bildirişləri dəstəkləmir.'); return; }
  if(Notification.permission==='granted'){ sendPushNotification('🔔 Bildirişlər aktivdir', BRAND_NAME+' bildirişləri artıq aktivdir.'); return; }
  if(Notification.permission!=='denied'){
    Notification.requestPermission().then(function(perm){
      if(perm==='granted'){ sendPushNotification('🔔 Bildirişlər aktivləşdi', 'Sifariş statusu dəyişəndə sizə bildiriş göndəriləcək.'); }
    });
  }
}
function sendPushNotification(title, body){
  try{
    if('Notification' in window && Notification.permission==='granted'){
      var n=new Notification(title,{body:body,icon:'/logo.png'});
      setTimeout(function(){ try{n.close();}catch(e){} },8000);
    }
  }catch(e){}
}
var BRAND_NAME=document.title.split(' — ').pop()||'PanelimAz';
var STATUS_CLASS_MAP={"Gözləmədə":"pill-wait","Tamamlandı":"pill-done","Davam Edir":"pill-run","Ləğv Edildi":"pill-cancel","Yüklənir":"pill-run"};
function pollOrderStatus(){
  var rows=document.querySelectorAll('tr.order-row[data-order-id]');
  if(!rows.length)return;
  fetch('/api/orders/status',{credentials:'same-origin'})
    .then(function(r){ if(!r.ok) throw new Error('bad status'); return r.json(); })
    .then(function(data){
      var map={};
      data.forEach(function(o){ map[String(o.order_id)]=o; });
      rows.forEach(function(tr){
        var id=tr.getAttribute('data-order-id');
        var o=map[id];
        if(!o) return;
        var newStatus=o.status||'Gözləmədə';
        if(tr.getAttribute('data-status')!==newStatus){
          var oldStatus=tr.getAttribute('data-status');
          tr.setAttribute('data-status',newStatus);
          var pill=tr.querySelector('.pill');
          if(pill){
            pill.textContent=newStatus;
            pill.className='pill '+(STATUS_CLASS_MAP[newStatus]||'pill-wait');
            pill.style.transition='background .3s ease';
          }
          if(oldStatus && oldStatus!==newStatus){
            sendPushNotification('Sifariş #'+id+' — '+newStatus, 'Sifarişinizin statusu yeniləndi: '+oldStatus+' → '+newStatus);
          }
        }
        var remCell=tr.querySelector('.cell-remains');
        if(remCell && o.remains!==undefined) remCell.textContent=o.remains;
        var startCell=tr.querySelector('.cell-start');
        if(startCell && o.start_count!==undefined) startCell.textContent=o.start_count;
      });
    })
    .catch(function(){ /* sessizce buraxiriq, novbeti pollda yeniden cehd olunacaq */ });
}
document.addEventListener('DOMContentLoaded',function(){
  if(document.querySelector('tr.order-row[data-order-id]')){
    pollOrderStatus();
    setInterval(pollOrderStatus, 8000);
  }
});
"""

GOOGLE_SVG = """<svg width="17" height="17" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.9c1.7-1.57 2.7-3.88 2.7-6.62z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.9-2.26c-.8.54-1.84.86-3.06.86-2.35 0-4.34-1.59-5.05-3.72H.96v2.33A9 9 0 0 0 9 18z"/><path fill="#FBBC05" d="M3.95 10.7A5.4 5.4 0 0 1 3.66 9c0-.59.1-1.17.29-1.7V4.97H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.03l2.99-2.33z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.5.46 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.97l2.99 2.33C4.66 5.17 6.65 3.58 9 3.58z"/></svg>"""

def HEAD(title):
    return f"""<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{title} — {BRAND}</title>
<meta name="description" content="{BRAND} — Instagram, TikTok, YouTube, Telegram və digər sosial media platformaları üçün sürətli, etibarlı SMM xidmətləri.">
<meta name="theme-color" content="#F3F1F8">
<link rel="icon" type="image/png" href="/logo.png">
<link rel="apple-touch-icon" href="/logo.png">
<meta property="og:title" content="{title} — {BRAND}">
<meta property="og:description" content="Azərbaycanın SMM paneli — sürətli çatdırılma, sərfəli qiymətlər.">
<meta property="og:image" content="/logo.png">
<meta property="og:type" content="website">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
<script>(function(){{try{{document.documentElement.setAttribute('data-theme','light');localStorage.setItem('bp_theme','light');}}catch(e){{}}}})();</script>
<style>{CSS}</style>
<!-- ===== ÇOXDİLLİ DƏSTƏK (AZ/EN/RU) — Google Website Translator ===== -->
<style>#google_translate_element{{display:none!important;}} .goog-te-banner-frame{{display:none!important;}} body{{top:0!important;}} .goog-tooltip,.goog-tooltip:hover{{display:none!important;}} .goog-text-highlight{{background:none!important;box-shadow:none!important;}}</style>
<div id="google_translate_element"></div>
<script>
function googleTranslateElementInit(){{
  new google.translate.TranslateElement({{pageLanguage:'az',includedLanguages:'az,en,ru',autoDisplay:false}},'google_translate_element');
}}
function setSiteLang(lang){{
  var host=location.hostname;
  function setCookie(v){{
    document.cookie='googtrans='+v+';path=/';
    document.cookie='googtrans='+v+';path=/;domain='+host;
    document.cookie='googtrans='+v+';path=/;domain=.'+host;
  }}
  if(lang==='az'){{ setCookie('/az/az'); }} else {{ setCookie('/az/'+lang); }}
  try{{localStorage.setItem('bp_lang',lang);}}catch(e){{}}
  location.reload();
}}
</script>
<script src="https://translate.google.com/translate_a/element.js?cb=googleTranslateElementInit"></script>"""

def SCRIPTS():
    return f"<script>{JS}</script>"

# Səhifə keçidi zamanı göstərilən yüklənmə zolağı + skelet overlay (bütün səhifələrdə ortaq)
LOADER_HTML = """<div id="page-loading-bar"></div>
<div id="page-skeleton">
  <div class="skel-wrap">
    <div class="skel-row">
      <div class="skel-card skel-shine"></div><div class="skel-card skel-shine"></div>
      <div class="skel-card skel-shine"></div><div class="skel-card skel-shine"></div>
    </div>
    <div class="skel-block skel-shine"></div>
    <div class="skel-line skel-shine" style="width:60%;"></div>
    <div class="skel-line skel-shine" style="width:85%;"></div>
    <div class="skel-line skel-shine" style="width:40%;"></div>
  </div>
</div>"""

LOGO_SVG = '<img src="/logo.png" alt="PanelimAz" style="height:46px;width:auto;">'
MENU_ICON_B64 = "iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAYAAADDPmHLAAADIElEQVR4nO3dMU8UQRiA4XeXu2iCFQ0JdsbWRhsboaGg1M7EVktCAb+CgBWdCZ2JFbYUNGBibDQGKxJjpJCEXiKG485ido7l7ogVbG7mfZLJLbtbTPLNzs7OZD5AkiRJkiRJkiRJkpSgYuDviepcr4G66PrF2J6PujDYGJSufrzjQXziZ4F5YIbQG/SwYYy7GMNz4AjYAfaqa/3YTgFb1c2W9MtWFXMK4DawDcwRWonv/7QVhN59F1hoAUuE4J8B7QYrpptzRoj5UgEcAPerC2VjVdJN6la/3wvgFLjVYGXUnL8l0Gm6FmpMpwT2CQO/7n9uVjq6hJjvl8AqYWTYxUaQgxjnAliNEwFrwHLtBj8F01RwMdBfB1aonVgEftL8JIXlesvPKtYAZewB4nTwJPAImAZaKCUd4Bj4DJwwYtFvooFKqRn9WA8u9NTfES4CpSU+7Y7xJEmSJEmSJEmSJEmSJEmSJEmSUjC4+6fENDGpG5kGIGaOUh5iRtihRJH3gCfAXewJUtMFfgEfgB/VuX4jaAOvCduGm96/brneclLFug0UcTfwW+A5gcki01V/1b8DXhTAS+ANIXlgC7eFp65HSBbRBl4VwFfgQXXR934e4lfAtwL4Q8gXrPyc+sRnriTkCjZHYF5ivA9KYIPQEBz956FHiHUJbPgZmJehz8B40omgfMrQRJBTwXm4cio4HrgYlI9Li0F1Lgenzy8+SZIkSZIkSZIkSZIkSZIkSZKkFAzuDIq7hUdd03iL+z+7teNL3BuYj36si9pvD7gDPASmCRnDlI4OcAx8AX5T2xUeu/xF4JDm969brrccVrEGKGMPsAYsV8dXviM09upjvHVgpQCeAu8JXYTbw9MXt4e3gGcF8BF4THjqDX4euoTe4FNBGBBMNlsfNeSkxNF+zlolYVRo2pC8xHgflsAmF4kilYeYKHKzICSK3gbmMElkDmJGuF1gIZ6cArZofpLCcjNlq4r50P8MmgXmgRlCK+nhotC4izE8B46AHWCvunYpUaSBzkc/3oNBjxkkHQekKcbWAb8kSZIkSZIkSZIkpe0fxIFn8wkIB2AAAAAASUVORK5CYII="

VERIFIED_BADGE_SVG = '<img src="/icons/verified.png" alt="Təsdiqlənmiş" style="width:16px;height:16px;vertical-align:-3px;margin-left:5px;display:inline-block;flex-shrink:0;">'

OWNER_NAME_HTML = '<span class="notranslate" style="background:linear-gradient(90deg,#FBBF24,#2563EB,#EC4899,#8B5CF6);-webkit-background-clip:text;background-clip:text;color:transparent;font-weight:800;">👑 Sahib</span>'

# ===== KATEQORİYA İKONLARI =====
# Xidmət kateqoriyası adına görə (Instagram, TikTok, YouTube və s.) avtomatik
# müvafiq ikon seçilir. Admin panelindən yeni kateqoriya ilə məhsul əlavə
# edildikdə də ikon avtomatik təyin olunur — ayrıca ayar lazım deyil.
CATEGORY_ICONS = [
    ("instagram", "📸"), ("insta", "📸"),
    ("tiktok", "🎵"), ("tik tok", "🎵"),
    ("youtube", "▶️"), ("you tube", "▶️"),
    ("telegram", "✈️"),
    ("facebook", "📘"), ("fb", "📘"),
    ("twitter", "🐦"), ("x.com", "🐦"),
    ("whatsapp", "💬"),
    ("spotify", "🎧"),
    ("discord", "🎮"),
    ("twitch", "🟣"),
    ("linkedin", "💼"),
    ("snapchat", "👻"),
    ("pinterest", "📌"),
    ("threads", "🧵"),
    ("kick", "🥊"),
    ("website", "🌐"), ("sayt", "🌐"), ("web", "🌐"),
    ("email", "📧"), ("e-poçt", "📧"),
    ("sms", "📩"),
    ("apple", "🍎"),
    ("soundcloud", "🎶"),
]
def get_cat_icon(category_name):
    n = (category_name or "").strip().lower()
    for key, icon in CATEGORY_ICONS:
        if key in n:
            return icon
    return "🔥"

def cat_label(category_name):
    """Kateqoriya adının önünə müvafiq ikonu əlavə edir."""
    return f"{get_cat_icon(category_name)} {category_name}"

# Real platform loqoları — yalnız adi HTML kontekstlərində istifadə olunur
# (məs. admin panel cədvəli). <select><option> daxilində şəkil göstərmək
# mümkün olmadığı üçün orada emoji (cat_label) istifadə olunmağa davam edir.
PLATFORM_LOGO_ICONS = [
    ("tiktok", "tiktok"), ("tik tok", "tiktok"),
    ("instagram", "instagram"), ("insta", "instagram"),
    ("facebook", "facebook"), ("fb", "facebook"),
    ("telegram", "telegram"),
    ("youtube", "youtube"), ("you tube", "youtube"),
    ("twitter", "twitter"), ("x.com", "twitter"),
    ("whatsapp", "whatsapp"),
    ("discord", "discord"),
]
def cat_label_html(category_name):
    """Kateqoriya adının önünə real platforma loqosu (mövcud olduqda) və ya
    emoji əlavə edir. Adi HTML üçündür, <select> daxilində istifadə olunmaz."""
    n = (category_name or "").strip().lower()
    for key, icon_name in PLATFORM_LOGO_ICONS:
        if key in n:
            return f'<img src="/icons/{icon_name}.png" alt="" style="width:16px;height:16px;vertical-align:-3px;margin-right:5px;display:inline-block;">{category_name}'
    return cat_label(category_name)

# ===== BOŞ VƏZİYYƏT (EMPTY STATE) İLLÜSTRASİYASI =====
# Sadə emoji yazısı əvəzinə, dairəvi gradient arxa fonlu, animasiyalı,
# daha "peşəkar" görünüşlü boş-vəziyyət kartı.
def empty_state(icon, title, sub="", cta_html=""):
    sub_html = f'<div class="es-sub">{sub}</div>' if sub else ""
    cta = f'<div class="es-cta">{cta_html}</div>' if cta_html else ""
    return f"""<div class="empty-state">
  <div class="es-illus"><span class="es-illus-icon">{icon}</span></div>
  <div class="es-title">{title}</div>
  {sub_html}
  {cta}
</div>"""

def display_name_html(email, name, verified=False):
    """Sahibin (OWNER_EMAIL) adı heç yerdə göstərilmir — onun əvəzinə rəngli
    '👑 Sahib' yazısı çıxır (adi mavi təsdiq nişanı əvəzinə). Digər istifadəçilər
    üçün adi ad + (varsa) mavi təsdiq nişanı göstərilir."""
    if (email or "").strip().lower() == OWNER_EMAIL:
        return OWNER_NAME_HTML, ""
    badge_svg = VERIFIED_BADGE_SVG if verified else ""
    return name, badge_svg

def topbar_auth(user, bal_info):
    bal = float(bal_info["balance"]) if bal_info else 0
    return f"""<header class="topbar" id="site-topbar">
<div class="wrap topbar-inner">
  <a class="brand" href="/">{LOGO_SVG}</a>
  <div class="topuser">
    <span class="bal-badge">💰 {bal:.2f} ₼</span>
    <button class="hamburger" onclick="toggleSidebar()">
      <img src="data:image/png;base64,{MENU_ICON_B64}" alt="Menyu" style="width:22px;height:22px;">
    </button>
  </div>
</div></header>"""

def sidebar_html(user, bal_info, active=""):
    bal = float(bal_info["balance"]) if bal_info else 0
    name = user.get("name","İstifadəçi")
    email = user.get("email","")
    verified = True  # Bütün istifadəçilər üçün təsdiq nişanı göstərilir
    name, badge_svg = display_name_html(email, name, verified)
    unread = get_unread_ticket_count(email) if email else 0
    badge = f'<span class="nav-badge">{unread}</span>' if unread else ""
    links = [
        ("🛒","Yeni Sifariş","/","new",""),
        ("📦","Sifarişlərim","/orders","orders",""),
        ("💳","Balans artır","/balance","balance",""),
        ("🎫","Dəstək","/support","support",badge),
        ("📢","Xəbərlər","/news","news",""),
        ("👤","Profil","/profile","profile",""),
    ]
    nav = ""
    for ic,lbl,href,key,bd in links:
        cls = "active" if active==key else ""
        nav += f'<a href="{href}" class="{cls}"><span class="icon">{ic}</span>{lbl}{bd}</a>'
    verified_tag = '<span class="sb-verified-tag">✓ Təsdiqlənmiş</span>' if (verified and (email or "").strip().lower()!=OWNER_EMAIL) else ""
    return f"""
<div class="sb-ov" id="sb-ov" onclick="toggleSidebar(false)"></div>
<div class="sidebar" id="sb">
  <div class="sb-head">
    <div class="sb-name">{name}{badge_svg}{verified_tag}</div>
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
    return f"""<footer class="footer">
  <div class="trust-strip">
    <span class="trust-badge">🔒 SSL ilə şifrələnib</span>
    <span class="trust-badge">🛡️ Təhlükəsiz ödəniş</span>
    <span class="trust-badge">✅ Etibarlı platforma</span>
  </div>
  <div class="footer-links">
    <a href="/terms">İstifadə Şərtləri</a>
    <a href="/privacy">Məxfilik Siyasəti</a>
    <a href="/support">Əlaqə</a>
  </div>
  <p>© 2026 {BRAND} — Bütün hüquqlar qorunur.</p>
</footer>"""

def verify_banner_html(bal_info):
    # E-poçt təsdiqi hesaba daxil olmaq/istifadə üçün tələb olunmur — buna görə
    # bu bildiriş artıq göstərilmir. E-poçt təsdiqi yalnız "Şifrəni unutdum"
    # funksiyası üçün (bərpa linkini göndərmək məqsədilə) istifadə olunur.
    return ""

def page_auth(title, body, user, bal_info, active=""):
    return f"""<!DOCTYPE html><html lang="az">
<head>{HEAD(title)}</head>
<body>
{LOADER_HTML}
{topbar_auth(user, bal_info)}
{sidebar_html(user, bal_info, active)}
{verify_banner_html(bal_info)}
{body}
{footer()}
{SCRIPTS()}
</body></html>"""

def render_error_page(code, title, message):
    return f"""<!DOCTYPE html><html lang="az">
<head>{HEAD(title)}</head>
<body>
{LOADER_HTML}
<header class="topbar"><div class="wrap topbar-inner"><a class="brand" href="/">{LOGO_SVG}</a></div></header>
<div style="padding:80px 0;display:flex;justify-content:center;">
<div class="hero-login-card" style="text-align:center;">
  <div style="font-size:52px;font-weight:800;color:var(--or);">{code}</div>
  <h2 style="margin:10px 0;">{title}</h2>
  <p style="color:var(--mu);margin-bottom:20px;">{message}</p>
  <a href="/" class="btn-or" style="display:inline-block;text-decoration:none;">🏠 Ana səhifəyə qayıt</a>
</div>
</div>
{footer()}{SCRIPTS()}
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

    err_html=f'<div class="err-box" data-toast>{err}</div>' if err else ""
    reg_err_html=f'<div class="err-box" data-toast>{reg_err}</div>' if reg_err else ""
    reg_ok_html=f'<div class="ok-box" data-toast>{reg_ok}</div>' if reg_ok else ""
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
    <div style="text-align:right;margin:-8px 0 4px;"><a href="/forgot-password" class="link-muted">Şifrəni unutmusunuz?</a></div>
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
{LOADER_HTML}
<header class="topbar">
<div class="wrap topbar-inner">
  <a class="brand" href="/">{LOGO_SVG}</a>
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
<path d="M0,40 C360,0 1080,80 1440,20 L1440,60 L0,60 Z" fill="#0B0A12"/>
</svg>
</div>
</section>

<div class="stats-strip">
<div class="stats-strip-grid">
  <div><div class="stats-strip-num">10,000+</div><div class="stats-strip-lbl">Tamamlanmış sifariş</div></div>
  <div><div class="stats-strip-num">500+</div><div class="stats-strip-lbl">Məmnun müştəri</div></div>
  <div><div class="stats-strip-num">24/7</div><div class="stats-strip-lbl">Dəstək xidməti</div></div>
  <div><div class="stats-strip-num">99.9%</div><div class="stats-strip-lbl">Uğurla çatdırılma</div></div>
</div>
</div>

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
    err_html=f'<div class="err-box" data-toast>{err}</div>' if err else ""
    ok_html=f'<div class="ok-box" data-toast>{ok}</div>' if ok else ""
    return f"""<!DOCTYPE html><html lang="az">
<head>{HEAD("Qeydiyyat")}</head>
<body>
{LOADER_HTML}
<header class="topbar"><div class="wrap topbar-inner">
  <a class="brand" href="/">{LOGO_SVG}</a>
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
    <label class="terms-check">
      <input type="checkbox" name="agree_terms" id="agree-terms" required onchange="document.getElementById('reg-submit-btn').disabled=!this.checked;">
      <span>Qeydiyyatdan keçməklə <a href="/terms" target="_blank">İstifadə şərtləri</a> və <a href="/privacy" target="_blank">Məxfilik siyasəti</a> ilə razılaşırsınız.</span>
    </label>
    <button class="btn-or" id="reg-submit-btn" type="submit" disabled>🚀 QEYDİYYATDAN KEÇ</button>
  </form>
  <p class="text-center mt-10" style="font-size:13px;color:var(--mu);">Artıq hesabınız var? <a href="/" class="link-or">Daxil ol</a></p>
</div>
</div>
{footer()}{SCRIPTS()}
</body></html>"""

def render_forgot(err="", ok=""):
    err_html=f'<div class="err-box" data-toast>{err}</div>' if err else ""
    ok_html=f'<div class="ok-box" data-toast>{ok}</div>' if ok else ""
    return f"""<!DOCTYPE html><html lang="az">
<head>{HEAD("Şifrəni bərpa et")}</head>
<body>
{LOADER_HTML}
<header class="topbar"><div class="wrap topbar-inner"><a class="brand" href="/">{LOGO_SVG}</a></div></header>
<div style="padding:60px 0;display:flex;justify-content:center;">
<div class="hero-login-card">
  <h2 style="margin-bottom:14px;font-size:18px;">🔑 Şifrəni bərpa et</h2>
  <p style="color:var(--mu);font-size:13px;margin-bottom:16px;">E-poçt ünvanınızı daxil edin, sizə bərpa linki göndərəcəyik.</p>
  {err_html}{ok_html}
  <form method="post" action="/forgot-password">
    <div class="field"><label>📧 E-poçt</label><input type="email" name="email" placeholder="sizin@gmail.com" required></div>
    <button class="btn-or" type="submit">Bərpa linki göndər</button>
  </form>
  <p class="text-center mt-10" style="font-size:13px;color:var(--mu);"><a href="/" class="link-or">← Girişə qayıt</a></p>
</div>
</div>
{footer()}{SCRIPTS()}
</body></html>"""

def render_reset(token, err=""):
    err_html=f'<div class="err-box" data-toast>{err}</div>' if err else ""
    return f"""<!DOCTYPE html><html lang="az">
<head>{HEAD("Yeni şifrə")}</head>
<body>
{LOADER_HTML}
<header class="topbar"><div class="wrap topbar-inner"><a class="brand" href="/">{LOGO_SVG}</a></div></header>
<div style="padding:60px 0;display:flex;justify-content:center;">
<div class="hero-login-card">
  <h2 style="margin-bottom:14px;font-size:18px;">🔒 Yeni şifrə təyin et</h2>
  {err_html}
  <form method="post" action="/reset-password">
    <input type="hidden" name="token" value="{token}">
    <div class="field"><label>🔒 Yeni şifrə</label><input type="password" name="password" placeholder="Ən azı 6 simvol" minlength="6" required></div>
    <div class="field"><label>🔒 Yeni şifrəni təsdiqlə</label><input type="password" name="password2" placeholder="••••••••" required></div>
    <button class="btn-or" type="submit">Şifrəni yenilə</button>
  </form>
</div>
</div>
{footer()}{SCRIPTS()}
</body></html>"""

def render_legal_page(title, body_html):
    return f"""<!DOCTYPE html><html lang="az">
<head>{HEAD(title)}</head>
<body>
{LOADER_HTML}
<header class="topbar"><div class="wrap topbar-inner"><a class="brand" href="/">{LOGO_SVG}</a></div></header>
<div class="legal-wrap">
  <h1>{title}</h1>
  <div class="legal-updated">Son yenilənmə: 2026</div>
  {body_html}
  <p style="margin-top:32px;"><a href="/" class="link-or">← Ana səhifəyə qayıt</a></p>
</div>
{footer()}{SCRIPTS()}
</body></html>"""

def render_terms():
    body=f"""
<p>Bu İstifadə Şərtləri {BRAND} platformasından ("Panel") istifadə edərkən tərəflərin hüquq və vəzifələrini müəyyən edir. Panelə qeydiyyatdan keçməklə və ya istifadə etməklə bu şərtləri qəbul etmiş sayılırsınız.</p>
<h2>1. Xidmətlərin təsviri</h2>
<p>{BRAND} sosial media hesabları üçün izləyici, bəyəni, baxış və digər SMM xidmətlərinin sifariş edilməsinə imkan verən onlayn platformadır.</p>
<h2>2. İstifadəçi öhdəlikləri</h2>
<ul>
<li>Qeydiyyat zamanı dəqiq və düzgün məlumat verməlisiniz.</li>
<li>Hesabınızın təhlükəsizliyinə görə (şifrə daxil olmaqla) özünüz məsuliyyət daşıyırsınız.</li>
<li>Sifariş verdiyiniz profil/hesab ictimai (public) rejimdə olmalıdır, əks halda sifariş yerinə yetirilməyə bilər.</li>
<li>Platformadan qanuna zidd, aldadıcı və ya üçüncü şəxslərin hüquqlarını pozan məqsədlərlə istifadə etmək qadağandır.</li>
</ul>
<h2>3. Ödəniş və balans</h2>
<p>Balansa əlavə edilən vəsait yalnız Panel daxilindəki xidmətlərin alınması üçün istifadə oluna bilər. Artıq yerinə yetirilmiş sifarişlər üçün ödənişlər geri qaytarılmır, istisna hallar Dəstək bölməsi vasitəsilə araşdırılır.</p>
<h2>4. Sifarişlərin icrası</h2>
<p>Sifarişlər adətən qısa müddətdə başlayır, lakin provayder tərəfli gecikmələr mümkündür. Yanlış və ya natamam məlumat (səhv link, bağlı profil və s.) səbəbindən yaranan problemlərə görə Panel məsuliyyət daşımır.</p>
<h2>5. Hesabın dayandırılması</h2>
<p>Bu şərtlərin pozulması halında {BRAND} istənilən hesabı xəbərdarlıq etmədən müvəqqəti və ya daimi olaraq bloklaya bilər.</p>
<h2>6. Dəyişikliklər</h2>
<p>Bu şərtlər zaman-zaman yenilənə bilər. Yenilənmiş şərtlər saytda dərc olunduğu andan qüvvəyə minir.</p>
<h2>7. Əlaqə</h2>
<p>Suallarınız üçün "Dəstək" bölməsi vasitəsilə bizimlə əlaqə saxlaya bilərsiniz.</p>
"""
    return render_legal_page("İstifadə Şərtləri", body)

def render_privacy():
    body=f"""
<p>{BRAND} olaraq istifadəçilərimizin məxfiliyinə önəm veririk. Bu sənəd hansı məlumatları topladığımızı və onlardan necə istifadə etdiyimizi izah edir.</p>
<h2>1. Topladığımız məlumatlar</h2>
<ul>
<li>Ad, e-poçt ünvanı və hesabınızı idarə etmək üçün lazım olan digər qeydiyyat məlumatları.</li>
<li>Sifariş tarixçəsi, balans hərəkətləri və dəstək müraciətləri.</li>
<li>Texniki məlumatlar (IP ünvanı, brauzer növü) — təhlükəsizlik və xidmət keyfiyyəti məqsədilə.</li>
</ul>
<h2>2. Məlumatlardan istifadə</h2>
<p>Toplanan məlumatlar yalnız hesabınızın idarə olunması, sifarişlərin icrası, dəstək xidməti göstərilməsi və platformanın təhlükəsizliyinin təmin edilməsi məqsədilə istifadə olunur.</p>
<h2>3. Məlumatların paylaşılması</h2>
<p>Şəxsi məlumatlarınız üçüncü tərəflərə satılmır. Sifarişin icrası üçün zəruri olan minimal məlumat (məsələn, profil linki) yalnız xidməti təmin edən provayderlə paylaşılır.</p>
<h2>4. Məlumatların saxlanması</h2>
<p>Məlumatlarınız hesabınız aktiv olduğu müddətdə, habelə qanuni öhdəliklərin icrası üçün lazım olan müddət ərzində saxlanılır.</p>
<h2>5. Sizin hüquqlarınız</h2>
<p>İstənilən vaxt Profil bölməsindən şəxsi məlumatlarınıza baxa, onları yeniləyə və ya hesabınızın silinməsini Dəstək bölməsi vasitəsilə tələb edə bilərsiniz.</p>
<h2>6. Kukilər (Cookies)</h2>
<p>Sayt seansınızı idarə etmək və dil seçimini yadda saxlamaq üçün zəruri kukilərdən istifadə olunur.</p>
<h2>7. Əlaqə</h2>
<p>Məxfilik siyasəti ilə bağlı suallarınız üçün "Dəstək" bölməsi vasitəsilə bizimlə əlaqə saxlaya bilərsiniz.</p>
"""
    return render_legal_page("Məxfilik Siyasəti", body)

def _build_prods_js(categories):
    parts = []
    for cat, prods in categories.items():
        cat_esc = cat.replace('"', '\\"')
        items = []
        for p in prods:
            name_esc = str(p["name"]).replace('"', '\\"')
            items.append('{' + f'id:{p["product_id"]},name:"{name_esc}",price:{p["price"]},min:{p["min_qty"]},max:{p["max_qty"]},cat:"{cat_esc}"' + '}')
        parts.append(f'allProds["{cat_esc}"]=[{",".join(items)}];')
    return "\n".join(parts)

def render_dashboard(user, bal_info, categories, all_products, ann_list, recent_orders=None, order_err=""):
    recent_orders = recent_orders or []
    order_err_html = f"""<div class="err-box" style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:16px;">
  <span>⚠️ {order_err}</span>
  <span onclick="this.closest('.err-box').style.display='none'" style="cursor:pointer;font-size:16px;line-height:1;opacity:.75;">×</span>
</div>""" if order_err else ""
    name=user.get("name","İstifadəçi")
    name,_bs=display_name_html(user.get("email",""), name)
    bal=float(bal_info["balance"])
    spent=float(bal_info.get("total_spent",0))
    total_ord=int(bal_info.get("total_orders",0))

    stats=f"""<div class="stat-grid">
<div class="stat-card">
  <div class="stat-icon-wrap"><div class="stat-icon">👤</div></div>
  <div class="stat-info">
    <div class="stat-name" style="color:var(--or);">{name}</div>
    <div class="stat-label">Xoş Gəlmisiniz</div>
  </div>
</div>
<div class="stat-card">
  <div class="stat-icon-wrap"><div class="stat-icon">💸</div></div>
  <div class="stat-info">
    <div class="stat-name">&#8380; {spent:.2f}</div>
    <div class="stat-label">Ümumi Xərclənən</div>
  </div>
</div>
<div class="stat-card">
  <div class="stat-icon-wrap"><div class="stat-icon">📦</div></div>
  <div class="stat-info">
    <div class="stat-name" style="color:var(--or);">{total_ord}</div>
    <div class="stat-label">Ümumi Sifarişlər</div>
  </div>
</div>
<div class="stat-card">
  <div class="stat-icon-wrap"><div class="stat-icon">💰</div></div>
  <div class="stat-info">
    <div class="stat-name">&#8380; {bal:.2f}</div>
    <div class="stat-label">Balans</div>
  </div>
</div>
</div>"""

    cat_opts="".join(f'<option value="{c}">{cat_label(c)}</option>' for c in categories.keys())
    first_cat=list(categories.keys())[0] if categories else ""
    first_prods=categories.get(first_cat,[])
    svc_opts="".join(f'<option value="{p["product_id"]}" data-price="{p["price"]}" data-min="{p["min_qty"]}" data-max="{p["max_qty"]}">{p["name"]} — {p["price"]} ₼/1000</option>' for p in first_prods)
    all_opts=""
    for cat,prods in categories.items():
        for p in prods:
            all_opts+=f'<option value="{p["product_id"]}" data-price="{p["price"]}" data-min="{p["min_qty"]}" data-max="{p["max_qty"]}">{p["name"]} — {p["price"]} ₼/1000</option>'

    ann_html=""
    for a in ann_list:
        dt=(a["created_at"] or "")[:16]
        ann_html+=f"""<div class="ann-card">
<div class="ann-head">📢 {dt}</div>
<div class="ann-body">{a["content"]}</div>
</div>"""

    # Mini spending chart (oldest -> newest of last 6 orders)
    chart_orders=list(reversed(recent_orders))
    if chart_orders:
        max_price=max(float(o.get("price") or 0) for o in chart_orders) or 1
        bars=""
        for i,o in enumerate(chart_orders):
            p=float(o.get("price") or 0)
            h=max(6,round((p/max_price)*100))
            lbl=str(o.get("order_date") or "")[5:10] or f"#{o.get('order_id')}"
            bars+=f"""<div class="chart-bar-col">
  <div class="chart-bar" style="height:{h}%;animation-delay:{i*0.06:.2f}s;" title="{p:.2f} ₼"></div>
  <div class="chart-lbl">{lbl}</div>
</div>"""
        chart_html=f'<div class="chart-wrap">{bars}</div>'
    else:
        chart_html=empty_state("📊","Hələ məlumat yoxdur","İlk sifarişinizi verdikdən sonra xərc qrafikiniz burada görünəcək.")

    STATUS_PILL={"Gözləmədə":"pill-wait","Tamamlandı":"pill-done","Davam Edir":"pill-run","Ləğv Edildi":"pill-cancel","Yüklənir":"pill-run"}
    if recent_orders:
        rr=""
        for o in recent_orders:
            st=o.get("status") or "Gözləmədə"
            sc=STATUS_PILL.get(st,"pill-wait")
            dt=str(o.get("order_date") or "")[:10]
            rr+=f"""<div class="recent-row">
  <div>
    <div class="rr-name">{o.get("product_name") or "Xidmət"}</div>
    <div class="rr-meta">{dt} · <span class="pill {sc}">{st}</span></div>
  </div>
  <div class="rr-price">{float(o.get("price") or 0):.2f} ₼</div>
</div>"""
        recent_html=f'<div class="recent-list">{rr}</div>'
    else:
        recent_html=empty_state("📭","Hələ sifariş yoxdur","Yuxarıdan ilk sifarişinizi verin, burada görünəcək.")

    body=f"""<section class="dash-page wrap">
{stats}
</section>
<div class="orange-wave" style="margin:0;width:100%;background:var(--bg);">
<svg viewBox="0 0 1440 60" preserveAspectRatio="none" style="height:50px;display:block;">
<path d="M0,0 C240,60 480,60 720,30 C960,0 1200,0 1440,40 L1440,60 L0,60 Z" fill="#2563EB" opacity="0.15"/>
<path d="M0,20 C360,60 720,0 1080,40 C1260,60 1380,20 1440,10 L1440,60 L0,60 Z" fill="#2563EB" opacity="0.1"/>
</svg>
</div>
<section class="wrap" style="padding-bottom:60px;">
<div class="order-panel">
  {order_err_html}
  <div class="search-bar">
    <input type="text" placeholder="🔍 Xidmət axtar..." oninput="filterSvc(this.value)">
  </div>
  <div class="plat-tabs" id="plat-tabs">
    <button type="button" class="plat-tab active" data-plat="all" onclick="selectPlatform('all',this)"><img class="plat-ic" src="/icons/all.png" alt="">Hamısı</button>
    <button type="button" class="plat-tab" data-plat="tiktok" onclick="selectPlatform('tiktok',this)"><img class="plat-ic" src="/icons/tiktok.png" alt="">TikTok</button>
    <button type="button" class="plat-tab" data-plat="instagram" onclick="selectPlatform('instagram',this)"><img class="plat-ic" src="/icons/instagram.png" alt="">Instagram</button>
    <button type="button" class="plat-tab" data-plat="facebook" onclick="selectPlatform('facebook',this)"><img class="plat-ic" src="/icons/facebook.png" alt="">Facebook</button>
    <button type="button" class="plat-tab" data-plat="telegram" onclick="selectPlatform('telegram',this)"><img class="plat-ic" src="/icons/telegram.png" alt="">Telegram</button>
    <button type="button" class="plat-tab" data-plat="youtube" onclick="selectPlatform('youtube',this)"><img class="plat-ic" src="/icons/youtube.png" alt="">YouTube</button>
    <button type="button" class="plat-tab" data-plat="twitter" onclick="selectPlatform('twitter',this)"><img class="plat-ic" src="/icons/twitter.png" alt="">Twitter</button>
    <button type="button" class="plat-tab" data-plat="whatsapp" onclick="selectPlatform('whatsapp',this)"><img class="plat-ic" src="/icons/whatsapp.png" alt="">WhatsApp</button>
    <button type="button" class="plat-tab" data-plat="discord" onclick="selectPlatform('discord',this)"><img class="plat-ic" src="/icons/discord.png" alt="">Discord</button>
    <button type="button" class="plat-tab" data-plat="other" onclick="selectPlatform('other',this)"><span class="plat-ic">➕</span>Digər</button>
  </div>
  <form method="post" action="/order" id="order-form">
    <div class="field"><label>Kateqoriya</label>
      <div class="cs-wrap" id="cat-cs-wrap">
        <button type="button" class="cs-trigger" id="cat-trigger" onclick="toggleCatList(event)">
          <span class="cs-trigger-inner" id="cat-trigger-inner"><img class="cs-ic" src="/icons/all.png" alt="">Bütün Xidmətlər</span>
          <span class="cs-arrow">▾</span>
        </button>
        <div class="cs-list" id="cat-list"></div>
      </div>
      <select id="cat-select" style="display:none">
        <option value="all">Bütün Xidmətlər</option>
        {cat_opts}
      </select>
    </div>
    <div class="field"><label>Xidmət</label>
      <div class="svc-row-flex">
        <div class="cs-wrap" id="svc-cs-wrap" style="flex:1;">
          <button type="button" class="cs-trigger" id="svc-trigger" onclick="toggleSvcList(event)">
            <span class="cs-trigger-inner" id="svc-trigger-inner">Xidmət seçin</span>
            <span class="cs-arrow">▾</span>
          </button>
          <div class="cs-list" id="svc-list"></div>
        </div>
      </div>
      <select id="svc-select" name="product_id" required style="display:none">
        {all_opts}
      </select>
    </div>
    <div class="field" id="completion-field"><label>Təxmini Tamamlanma Vaxtı</label>
      <input type="text" id="eta-val" value="Xidmət seçin" readonly style="background:rgba(255,255,255,.02);color:var(--mu);">
    </div>
    <div class="field"><label>Link</label>
      <input type="text" id="link-input" name="profile_link" placeholder="https://..." required autocomplete="off">
      <span class="hint-text err-hint" id="link-warn" style="display:none;color:#F87171;">⚠️ Link formatı düzgün deyil. Tam URL daxil edin (https:// ilə başlamalı).</span>
    </div>
    <div class="field"><label>Miqdar</label>
      <input type="number" id="qty-input" name="quantity" value="100" min="1" required>
      <span class="hint-text" id="min-qty">Min: 10</span><span class="hint-text" id="max-qty"> - Max: 100000</span>
      <span class="hint-text err-hint" id="qty-warn" style="display:none;color:#F87171;">⚠️ Miqdar icazə verilən aralıqda deyil.</span>
    </div>
    <div class="price-display">
      <div>
        <div class="p-label">Toplam Məbləğ</div>
        <div class="p-val" style="font-size:17px;"><span id="price-val">0.0000 ₼</span></div>
      </div>
    </div>
    <button class="btn-or" id="order-submit" type="submit">🚀 SİFARİŞİ TƏSDİQLƏ</button>
    <div class="info-box">
      <p>⚠️ Profil Gizli Olmamalıdır! Sifariş verərkən profiliniz mütləq "Public" (Açıq) olmalıdır.</p>
      <p>⚠️ İkinci Sifarişi Vurmayın. Eyni linkə edilən bir sifariş bitmədən, ikinci sifarişi verməyin.</p>
      <p>⚠️ Link Formatı Düzgün Olsun. Linki tam şəkildə kopyalayın.</p>
      <p>⚠️ Dəstək Tələbi. Hər hansı bir xəta baş verərsə "Dəstək" bölməsindən bizə yazın.</p>
    </div>
  </form>
</div>
{ann_html if ann_html else ""}
</section>
<script>
var allProds={{}};
{_build_prods_js(categories)}
function changeCat(cat){{
  var prods=cat==='all'?Object.values(allProds).flat():allProds[cat]||[];
  populateSvcSelect(prods);
}}
function svcOptLabel(p){{return p.name+' — '+p.price+' ₼/1000';}}
function populateSvcSelect(prods){{
  var sel=document.getElementById('svc-select');
  var list=document.getElementById('svc-list');
  sel.innerHTML='';list.innerHTML='';
  prods.forEach(function(p){{
    var o=document.createElement('option');
    o.value=p.id;o.dataset.price=p.price;o.dataset.min=p.min;o.dataset.max=p.max;
    o.innerText=svcOptLabel(p);
    sel.appendChild(o);
    var ic=catIconHtml(p.cat);
    var lbl=svcOptLabel(p);
    var div=document.createElement('div');
    div.className='cs-item';
    div.dataset.value=p.id;
    div.innerHTML=ic+'<span>'+lbl+'</span>';
    div.onclick=function(pid,l,i){{return function(){{selectSvcItem(pid,l,i);}};}}(p.id,lbl,ic);
    list.appendChild(div);
  }});
  if(prods.length){{
    var first=prods[0];
    document.getElementById('svc-trigger-inner').innerHTML=catIconHtml(first.cat)+'<span>'+svcOptLabel(first)+'</span>';
    sel.value=first.id;
    document.querySelectorAll('#svc-list .cs-item').forEach(function(el){{el.classList.toggle('active',String(el.dataset.value)===String(first.id));}});
  }}else{{
    document.getElementById('svc-trigger-inner').innerHTML='<span>Xidmət tapılmadı</span>';
  }}
  sel.dispatchEvent(new Event('change'));
}}
function selectSvcItem(id,label,iconHtml){{
  var sel=document.getElementById('svc-select');
  sel.value=id;
  document.getElementById('svc-trigger-inner').innerHTML=iconHtml+'<span>'+label+'</span>';
  document.querySelectorAll('#svc-list .cs-item').forEach(function(el){{el.classList.toggle('active',String(el.dataset.value)===String(id));}});
  closeSvcList();
  sel.dispatchEvent(new Event('change'));
}}
function toggleSvcList(e){{
  if(e)e.stopPropagation();
  var list=document.getElementById('svc-list');
  var trig=document.getElementById('svc-trigger');
  var isOpen=list.classList.contains('open');
  if(isOpen){{closeSvcList();}}else{{list.classList.add('open');trig.classList.add('open');}}
}}
function closeSvcList(){{
  document.getElementById('svc-list').classList.remove('open');
  document.getElementById('svc-trigger').classList.remove('open');
}}
document.addEventListener('click',function(e){{
  var wrap=document.getElementById('svc-cs-wrap');
  if(wrap && !wrap.contains(e.target)) closeSvcList();
}});
var PLAT_KEYWORDS={{
  tiktok:['tiktok','tik tok'],
  instagram:['instagram','insta'],
  facebook:['facebook','fb'],
  telegram:['telegram'],
  youtube:['youtube','you tube'],
  twitter:['twitter','x.com'],
  whatsapp:['whatsapp'],
  discord:['discord']
}};
function catMatchesPlatform(cat,plat){{
  var n=cat.toLowerCase();
  if(plat==='all')return true;
  if(plat==='other'){{
    return !Object.values(PLAT_KEYWORDS).some(function(kws){{return kws.some(function(k){{return n.indexOf(k)>-1;}});}});
  }}
  var kws=PLAT_KEYWORDS[plat]||[plat];
  return kws.some(function(k){{return n.indexOf(k)>-1;}});
}}
var PLAT_ICON_FILES={{tiktok:'tiktok.png',instagram:'instagram.png',facebook:'facebook.png',telegram:'telegram.png',youtube:'youtube.png',twitter:'twitter.png',whatsapp:'whatsapp.png',discord:'discord.png'}};
function catIconHtml(cat){{
  var n=(cat||'').toLowerCase();
  var plats=Object.keys(PLAT_KEYWORDS);
  for(var i=0;i<plats.length;i++){{
    var p=plats[i];
    if(PLAT_KEYWORDS[p].some(function(k){{return n.indexOf(k)>-1;}})){{
      return '<img class="cs-ic" src="/icons/'+PLAT_ICON_FILES[p]+'" alt="">';
    }}
  }}
  return '<span class="cs-ic-emoji">🔥</span>';
}}
function renderCatList(items){{
  var list=document.getElementById('cat-list');
  var sel=document.getElementById('cat-select');
  list.innerHTML='';sel.innerHTML='';
  items.forEach(function(it){{
    var opt=document.createElement('option');
    opt.value=it.value;opt.innerText=it.label;
    sel.appendChild(opt);
    var div=document.createElement('div');
    div.className='cs-item';
    div.dataset.value=it.value;
    div.innerHTML=it.icon+'<span>'+it.label+'</span>';
    div.onclick=function(v,l,ic){{return function(){{selectCatItem(v,l,ic);}};}}(it.value,it.label,it.icon);
    list.appendChild(div);
  }});
}}
function selectCatItem(value,label,iconHtml){{
  document.getElementById('cat-select').value=value;
  document.getElementById('cat-trigger-inner').innerHTML=iconHtml+'<span>'+label+'</span>';
  document.querySelectorAll('.cs-item').forEach(function(el){{el.classList.toggle('active',el.dataset.value===value);}});
  closeCatList();
  changeCat(value);
}}
function toggleCatList(e){{
  if(e)e.stopPropagation();
  var list=document.getElementById('cat-list');
  var trig=document.getElementById('cat-trigger');
  var isOpen=list.classList.contains('open');
  if(isOpen){{closeCatList();}}else{{list.classList.add('open');trig.classList.add('open');}}
}}
function closeCatList(){{
  document.getElementById('cat-list').classList.remove('open');
  document.getElementById('cat-trigger').classList.remove('open');
}}
document.addEventListener('click',function(e){{
  var wrap=document.getElementById('cat-cs-wrap');
  if(wrap && !wrap.contains(e.target)) closeCatList();
}});
function selectPlatform(plat,btn){{
  document.querySelectorAll('.plat-tab').forEach(function(b){{b.classList.remove('active');}});
  if(btn)btn.classList.add('active');
  var matchedCats=Object.keys(allProds).filter(function(c){{return catMatchesPlatform(c,plat);}});
  var items=[];
  if(plat==='all'){{items.push({{value:'all',label:'Bütün Xidmətlər',icon:'<img class="cs-ic" src="/icons/all.png" alt="">'}});}}
  matchedCats.forEach(function(c){{items.push({{value:c,label:c,icon:catIconHtml(c)}});}});
  renderCatList(items);
  if(plat==='all'){{
    selectCatItem('all','Bütün Xidmətlər','<img class="cs-ic" src="/icons/all.png" alt="">');
  }}else if(matchedCats.length){{
    selectCatItem(matchedCats[0],matchedCats[0],catIconHtml(matchedCats[0]));
  }}else{{
    var sel=document.getElementById('svc-select');
    var list=document.getElementById('svc-list');
    sel.innerHTML='<option value="">Bu platforma üzrə xidmət tapılmadı</option>';
    if(list)list.innerHTML='';
    var trig=document.getElementById('svc-trigger-inner');
    if(trig)trig.innerHTML='<span>Xidmət tapılmadı</span>';
    document.getElementById('cat-trigger-inner').innerHTML='<span class="cs-ic-emoji">🚫</span><span>Xidmət tapılmadı</span>';
  }}
}}
function filterSvc(q){{
  var all=Object.values(allProds).flat();
  var filtered=all.filter(function(p){{return p.name.toLowerCase().includes(q.toLowerCase());}});
  populateSvcSelect(filtered);
}}
document.addEventListener('DOMContentLoaded',function(){{
  var allBtn=document.querySelector('.plat-tab[data-plat="all"]');
  selectPlatform('all',allBtn);
  var form=document.getElementById('order-form');
  var linkInput=document.getElementById('link-input');
  if(form&&linkInput){{
    form.addEventListener('submit',function(e){{
      e.preventDefault();
      var btn=document.getElementById('order-submit');
      var fd=new FormData(form);
      fetch('/order',{{method:'POST',body:fd,headers:{{'X-Requested-With':'fetch'}}}})
        .then(function(r){{return r.json();}})
        .then(function(data){{
          hidePageLoading();
          if(data.ok){{
            if(btn){{btn.innerHTML='✅ Uğurla göndərildi!';}}
            setTimeout(function(){{window.location.href=data.redirect||'/orders?new=1';}},550);
          }}else{{
            if(btn){{btn.disabled=false;btn.innerHTML=btn.dataset.origText||'🚀 SİFARİŞİ TƏSDİQLƏ';}}
            showOrderInlineError(data.error||'Xəta baş verdi.');
          }}
        }})
        .catch(function(){{
          hidePageLoading();
          if(btn){{btn.disabled=false;btn.innerHTML=btn.dataset.origText||'🚀 SİFARİŞİ TƏSDİQLƏ';}}
          showOrderInlineError('Şəbəkə xətası. Yenidən cəhd edin.');
        }});
    }});
  }}
}});
function showOrderInlineError(msg){{
  var panel=document.querySelector('.order-panel');
  if(!panel)return;
  var old=panel.querySelector('.err-box');
  if(old)old.remove();
  var div=document.createElement('div');
  div.className='err-box';
  div.style.cssText='display:flex;align-items:center;justify-content:space-between;gap:10px;';
  div.innerHTML='<span>⚠️ '+msg+'</span><span onclick="this.closest(\\'.err-box\\').style.display=\\'none\\'" style="cursor:pointer;font-size:16px;line-height:1;opacity:.75;">×</span>';
  panel.insertBefore(div,panel.firstChild);
  div.scrollIntoView({{behavior:'smooth',block:'start'}});
}}
</script>"""
    return page_auth("Yeni Sifariş", body, user, bal_info, "new")

def render_orders(user, bal_info, orders, new=False):
    STATUS_MAP={"Gözləmədə":"pill-wait","Tamamlandı":"pill-done","Davam Edir":"pill-run","Ləğv Edildi":"pill-cancel","Yüklənir":"pill-run"}
    new_toast='<div class="ok-box" data-toast>✅ Sifariş uğurla yaradıldı!</div>' if new else ""
    if not orders:
        tbl=empty_state("📭","Hələ heç bir sifarişiniz yoxdur","İlk sifarişinizi verərək başlayın.",'<a href="/" class="link-or">Yeni sifariş ver →</a>')
    else:
        rows=""
        for o in orders:
            status = o.get("status") or "Gözləmədə"
            sc=STATUS_MAP.get(status,"pill-wait")
            dt=str(o.get("order_date") or "")[:16]
            price = o.get("price") or 0
            qty = o.get("quantity") or 0
            link = o.get("profile_link") or "-"
            oid = o.get("order_id") or "-"
            pname = o.get("product_name") or "Xidmət"
            sc0 = o.get("start_count") or 0
            rem = o.get("remains") or 0
            rows+=f"""<tr class="order-row" data-status="{status}" data-order-id="{oid}">
<td>#{oid}</td>
<td>{dt}</td>
<td style="max-width:180px;word-break:break-all;font-size:12px;">{link}</td>
<td>{price} ₼</td>
<td class="cell-start">{sc0}</td>
<td>{qty}</td>
<td style="max-width:140px;">{pname}</td>
<td><span class="pill {sc}">{status}</span></td>
<td class="cell-remains">{rem}</td>
</tr>"""
        tbl=f'<div style="overflow-x:auto;"><table class="ot"><tbody>{rows}</tbody></table></div>'

    # Order statistics (by status)
    status_order=["Gözləmədə","Yüklənir","Davam Edir","Tamamlandı","Ləğv Edildi"]
    status_colors={"Gözləmədə":"#FBBF24","Yüklənir":"#38BDF8","Davam Edir":"#38BDF8","Tamamlandı":"#34D399","Ləğv Edildi":"#F87171"}
    counts={s:0 for s in status_order}
    for o in orders:
        st=o.get("status") or "Gözləmədə"
        counts[st]=counts.get(st,0)+1
    max_c=max(counts.values()) or 1
    stats_bars="".join(
        f'<div class="chart-bar-col"><div class="chart-bar" style="height:{max(6,round((v/max_c)*100))}%;background:{status_colors.get(s,"#3B82F6")};" title="{s}: {v}"></div><div class="chart-lbl">{v}</div></div>'
        for s,v in counts.items()
    )
    stats_legend="".join(f'<span><i style="background:{status_colors.get(s,"#3B82F6")};"></i>{s}</span>' for s in status_order)
    body=f"""<section class="dash-page wrap">
{new_toast}
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

def render_balance(user, bal_info, sent=False, history=None):
    history = history or []
    bal=float(bal_info["balance"])
    pretty=" ".join(CARD_NUMBER[i:i+4] for i in range(0,len(CARD_NUMBER),4))
    holder=f'<div class="card-holder-name">{CARD_HOLDER}</div>' if CARD_HOLDER else ""
    ok=f'<div class="ok-box" data-toast>✅ Sorğunuz göndərildi! Admin qəbzi yoxlayan kimi balansınız artırılacaq. Minimum 1-5 dəqiqə.</div>' if sent else ""
    body=f"""<section class="dash-page wrap">
<h1 class="page-title">💳 Balans artır</h1>
<div class="bal-page">
{ok}
<p style="font-size:14px;color:var(--mu);margin-bottom:16px;text-align:center;">Balans artırmaq üçün kartı seçin</p>
<div style="display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;">
  <button class="ftab active" onclick="void(0)">{CARD_BANK}</button>
</div>
<div style="position:relative;height:200px;margin-bottom:24px;">
  <div style="position:absolute;left:50%;transform:translateX(-50%);width:320px;max-width:100%;">
    <!-- back cards -->
    <div style="position:absolute;top:10px;left:-12px;width:310px;height:185px;background:linear-gradient(135deg,#6B7280,#374151);border-radius:16px;transform:rotate(-4deg);opacity:.5;"></div>
    <div style="position:absolute;top:5px;left:-6px;width:310px;height:185px;background:linear-gradient(135deg,#9CA3AF,#4B5563);border-radius:16px;transform:rotate(-2deg);opacity:.7;"></div>
    <!-- main card -->
    <div style="position:relative;background:linear-gradient(135deg,#1D4ED8,#1E3A8A,#0F172A);border-radius:16px;padding:22px 22px 18px;color:#fff;box-shadow:0 20px 40px rgba(29,78,216,.35),0 8px 16px rgba(0,0,0,.2);width:310px;max-width:100%;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;">
        <span style="font-size:15px;font-weight:600;opacity:.9;">{CARD_BANK} Bank</span>
        <div style="width:36px;height:24px;background:linear-gradient(135deg,#FCD34D,#F59E0B);border-radius:4px;"></div>
      </div>
      <div style="font-size:11px;opacity:.7;letter-spacing:.1em;margin-bottom:6px;">KART NÖMRƏSİ</div>
      <div id="card-num-val" style="font-family:'IBM Plex Mono',monospace;font-size:20px;letter-spacing:.12em;margin-bottom:18px;">{pretty}</div>
      {holder}
      <div style="position:absolute;top:18px;right:18px;">
        <button id="copy-btn-el" onclick="copyCard()" class="copy-btn-anim"><span id="copy-btn-txt">📋 Kopyala</span></button>
      </div>
      <div style="position:absolute;bottom:0;right:0;width:120px;height:120px;background:rgba(255,255,255,.04);border-radius:50%;transform:translate(30%,30%);"></div>
    </div>
  </div>
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
<h3 style="font-size:15px;font-weight:700;margin:28px 0 14px;">📜 Ödəniş Tarixçəsi</h3>
{_render_topup_history(history)}
</div>
</section>"""
    return page_auth("Balans artır", body, user, bal_info, "balance")

def _render_topup_history(history):
    if not history:
        return f'<div class="table-card">{empty_state("💳","Hələ ödəniş tarixçəniz yoxdur")}</div>'
    status_pill = {
        "Yoxlanılır": ("pill-wait","⏳ Yoxlanılır"),
        "Beklemede": ("pill-wait","⏳ Yoxlanılır"),
        "Təsdiqləndi": ("pill-done","✅ Təsdiqləndi"),
        "Rədd edildi": ("pill-cancel","❌ Rədd edildi"),
    }
    rows=""
    for t in history:
        st=t.get("status") or "Yoxlanılır"
        cls,label = status_pill.get(st,("pill-wait",st))
        dt=str(t.get("topup_date") or "")[:16].replace("T"," ")
        rows+=f"""<tr>
<td>#{t["topup_id"]}</td><td>{dt}</td><td>{t.get("amount_sent")} ₼</td>
<td><span class="pill {cls}">{label}</span></td>
</tr>"""
    return f"""<div class="table-card" style="overflow-x:auto;">
<table class="ot"><thead><tr><th>ID</th><th>Tarix</th><th>Məbləğ</th><th>Status</th></tr></thead>
<tbody>{rows}</tbody></table>
</div>"""

def render_support(user, bal_info, tickets, ok=""):
    ok_html=f'<div class="ok-box" data-toast>{ok}</div>' if ok else ""
    rows=""
    if tickets:
        for t in tickets:
            dt=(t["created_at"] or "")[:10]
            sc="pill-done" if t["status"]=="Bağlı" else "pill-wait"
            dot='<span class="unread-dot"></span>' if t.get("user_unread") else ""
            rows+=f"""<a class="ticket-row" href="/support/{t["ticket_id"]}">
<span>{dot}#{t["ticket_id"]}</span>
<span>{t["subject"]}</span>
<span><span class="pill {sc}">{t["status"]}</span></span>
<span style="font-size:12px;">{dt}</span>
</a>"""
    else:
        rows=empty_state("🎫","Hələ heç bir ticket yoxdur","Suallarınız üçün aşağıdan yeni müraciət yarada bilərsiniz.")

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

def render_ticket_detail(user, bal_info, ticket, replies):
    sc="pill-done" if ticket["status"]=="Bağlı" else "pill-wait"
    msgs=f"""<div class="chat-msg user">
  <div class="chat-bubble">{ticket["message"]}</div>
  <div class="chat-meta">Siz · {str(ticket["created_at"] or "")[:16].replace("T"," ")}</div>
</div>"""
    for r in replies:
        who = "user" if r["sender"]=="user" else "admin"
        label = "Siz" if who=="user" else "Dəstək Komandası"
        att = f'<div style="margin-top:6px;font-size:12px;opacity:.8;">📎 Fayl əlavə edilib</div>' if r.get("attachment_url") else ""
        msgs+=f"""<div class="chat-msg {who}">
  <div class="chat-bubble">{r["message"]}{att}</div>
  <div class="chat-meta">{label} · {str(r["created_at"] or "")[:16].replace("T"," ")}</div>
</div>"""

    body=f"""<section class="dash-page wrap" style="max-width:720px;">
<a class="chat-back" href="/support">← Bütün Ticketlərə qayıt</a>
<h1 class="page-title">🎫 Ticket #{ticket["ticket_id"]} — {ticket["subject"]}<span class="pill {sc} ticket-status-pill">{ticket["status"]}</span></h1>
<div class="chat-wrap" id="chat-wrap">
{msgs}
</div>
<form method="post" action="/support/{ticket["ticket_id"]}/reply" enctype="multipart/form-data" class="chat-form">
  <textarea name="message" placeholder="Mesajınızı yazın..." required></textarea>
  <div class="chat-form-row">
    <input type="file" name="attachment" accept="image/jpeg,image/png,image/jpg,.pdf">
    <button class="btn-or" style="width:auto;padding:10px 22px;" type="submit">Göndər</button>
  </div>
</form>
</section>
<script>
var cw=document.getElementById('chat-wrap');
if(cw)cw.scrollTop=cw.scrollHeight;
</script>"""
    return page_auth(f"Ticket #{ticket['ticket_id']}", body, user, bal_info, "support")

def render_news(user, bal_info, anns):
    if not anns:
        body_inner=empty_state("📢","Hələ xəbər yoxdur","Yeni elanlar burada görünəcək.")
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

def render_profile(user, bal_info, udb, msg="", err=""):
    name=udb.get("name") or user.get("name","İstifadəçi")
    email=udb.get("email") or user.get("email","")
    avatar=udb.get("avatar_url")
    avatar_html = f'<img class="avatar-img" src="{avatar}">' if avatar else f'<div class="avatar-placeholder">{name[:1].upper()}</div>'
    name,_bs=display_name_html(email, name)
    api_key=udb.get("api_key")

    msg_html=f'<div class="ok-box" data-toast>{msg}</div>' if msg else ""
    err_html=f'<div class="err-box" data-toast>{err}</div>' if err else ""

    if api_key:
        api_section=f"""<div class="api-key-box" id="api-key-val">{api_key}</div>
<div style="display:flex;gap:10px;flex-wrap:wrap;">
<button class="btn-or btn-sm btn-outline" type="button" onclick="copyApiKey()">📋 Kopyala</button>
<form method="post" action="/profile/apikey/revoke">
<button class="btn-or btn-sm btn-danger" type="submit">🗑️ Sil</button>
</form>
</div>"""
    else:
        api_section="""<p style="font-size:13px;color:var(--mu);margin-bottom:12px;">API vasitəsilə sifariş vermək və balans yoxlamaq üçün açar yaradın.</p>
<form method="post" action="/profile/apikey/generate">
<button class="btn-or btn-sm" type="submit">🔑 API açarı yarat</button>
</form>"""

    body=f"""<section class="dash-page wrap">
<h1 class="page-title">👤 Profil</h1>
<div class="profile-page">
{msg_html}{err_html}
<div class="profile-hero">
  <div class="avatar-wrap">
    {avatar_html}
    <label class="avatar-edit" for="avatar-input">✏️</label>
  </div>
  <form id="avatar-form" method="post" action="/profile/avatar" enctype="multipart/form-data">
    <input type="file" id="avatar-input" name="avatar" accept="image/png,image/jpeg,image/jpg" style="display:none;" onchange="document.getElementById('avatar-form').submit()">
  </form>
  <div style="text-align:center;">
    <div style="font-weight:700;font-size:16px;">{name}</div>
    <div style="font-size:13px;color:var(--mu);">{email}</div>
  </div>
</div>

<div class="profile-card">
  <h3>🔒 Şifrəni dəyiş</h3>
  <form method="post" action="/profile/password">
    <div class="field"><label>Cari şifrə</label><input type="password" name="current_password" required></div>
    <div class="field"><label>Yeni şifrə</label><input type="password" name="new_password" minlength="6" required></div>
    <div class="field"><label>Yeni şifrə (təkrar)</label><input type="password" name="new_password2" minlength="6" required></div>
    <button class="btn-or btn-sm" type="submit">Yenilə</button>
  </form>
</div>

<div class="profile-card">
  <h3>🔑 API Açarı</h3>
  {api_section}
</div>
</div>
</section>
<script>
function copyApiKey(){{
  var el=document.getElementById('api-key-val');
  if(!el)return;
  navigator.clipboard.writeText(el.innerText.trim()).then(function(){{
    if(typeof showToast==='function')showToast('API açarı kopyalandı','ok');
  }});
}}
</script>"""
    return page_auth("Profil", body, user, bal_info, "profile")

# ===== AUTH ROUTES =====
@app.get("/", response_class=HTMLResponse)
def root(request: Request, order_err: str = ""):
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
        c.execute("""SELECT o.*, COALESCE(p.name,'Silinmis xidmet') AS product_name
                   FROM orders o LEFT JOIN products p ON o.product_id=p.product_id
                   WHERE o.customer_email=%s ORDER BY o.order_id DESC LIMIT 6""",(user["email"],))
        recent_orders=c.fetchall()
    finally: put_conn(conn)
    by_cat={}
    for p in prods: by_cat.setdefault(p["category"],[]).append(p)
    bal_info=get_balance(user["email"])
    return HTMLResponse(render_dashboard(user,bal_info,by_cat,prods,anns,recent_orders,order_err=order_err))

@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, email: str=Form(...), password: str=Form(...)):
    email=email.lower().strip()
    user_row=get_user_db(email)
    if not user_row:
        log_event("LOGIN_FAIL", email, "hesab tapilmadi")
        return HTMLResponse(render_landing(err="Bu e-poçt ünvanı ilə hesab tapılmadı."))
    if not user_row.get("password"):
        log_event("LOGIN_FAIL", email, "google hesabi ile adi giris cehdi")
        return HTMLResponse(render_landing(err="Bu hesab Google ilə yaradılıb. Google ilə daxil olun."))
    if user_row["password"]!=hash_pw(password):
        log_event("LOGIN_FAIL", email, "yanlis sifre")
        return HTMLResponse(render_landing(err="Şifrə yanlışdır. Yenidən cəhd edin."))
    request.session["user"]={"email":user_row["email"],"name":user_row["name"]}
    log_event("LOGIN_OK", email)
    return RedirectResponse(url="/",status_code=status.HTTP_303_SEE_OTHER)

@app.get("/register", response_class=HTMLResponse)
def register_get(): return HTMLResponse(render_register())

@app.get("/terms", response_class=HTMLResponse)
def terms_page(): return HTMLResponse(render_terms())

@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(): return HTMLResponse(render_privacy())

@app.post("/register", response_class=HTMLResponse)
def register_post(request: Request, name:str=Form(...), email:str=Form(...), password:str=Form(...), password2:str=Form(...), agree_terms:str=Form("")):
    email=email.lower().strip()
    if not agree_terms:
        return HTMLResponse(render_register(err="Davam etmək üçün İstifadə şərtləri və Məxfilik siyasəti ilə razılaşmalısınız."))
    if password!=password2: return HTMLResponse(render_register(err="Şifrələr uyğun gəlmir."))
    if len(password)<6: return HTMLResponse(render_register(err="Şifrə ən azı 6 simvol olmalıdır."))
    ex=get_user_db(email)
    if ex:
        if ex.get("password"): return HTMLResponse(render_register(err="Bu e-poçtla hesab artıq mövcuddur."))
        else: return HTMLResponse(render_register(err="Bu e-poçt Google ilə qeydiyyatdan keçib."))
    token=gen_token()
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("""INSERT INTO panel_users (email,name,password,balance,total_spent,total_orders,created_at,email_verified,verify_token,verify_sent_at)
                     VALUES (%s,%s,%s,0,0,0,%s,FALSE,%s,%s)""",
                  (email,name.strip(),hash_pw(password),datetime.now().isoformat(),token,datetime.now().isoformat()))
        conn.commit()
    finally: put_conn(conn)
    sent = send_verification_email(request, email, token)
    log_event("REGISTER", email, f"qeydiyyat tamamlandi, tesdiq emaili gonderildi={sent}")
    request.session["user"]={"email":email,"name":name.strip()}
    return RedirectResponse(url="/",status_code=status.HTTP_303_SEE_OTHER)

@app.get("/login/google")
async def login_google(request: Request):
    if not GOOGLE_CLIENT_ID: return RedirectResponse(url="/")
    return await oauth.google.authorize_redirect(request, request.url_for("auth_callback"))

@app.get("/auth/callback")
async def auth_callback(request: Request):
    try: token=await oauth.google.authorize_access_token(request)
    except Exception as e:
        log_error("GOOGLE_AUTH_FAIL", e)
        return RedirectResponse(url="/")
    info=token.get("userinfo") or {}
    email=(info.get("email") or "").lower().strip()
    name=info.get("name") or email
    if not email: return RedirectResponse(url="/")
    conn=get_conn()
    try:
        c=conn.cursor()
        # Google emaili artiq tesdiqlediyi ucun email_verified=TRUE qoyuruq
        c.execute("""INSERT INTO panel_users (email,name,password,balance,total_spent,total_orders,created_at,email_verified)
                     VALUES (%s,%s,NULL,0,0,0,%s,TRUE)
                     ON CONFLICT (email) DO UPDATE SET name=EXCLUDED.name, email_verified=TRUE""",
                  (email,name,datetime.now().isoformat()))
        conn.commit()
    finally: put_conn(conn)
    request.session["user"]={"email":email,"name":name}
    log_event("LOGIN_OK", email, "google")
    return RedirectResponse(url="/")

@app.get("/logout")
def logout(request: Request):
    email=(current_user(request) or {}).get("email","")
    log_event("LOGOUT", email)
    request.session.clear()
    return RedirectResponse(url="/")

# ===== EMAIL VERIFICATION =====
@app.get("/verify-email", response_class=HTMLResponse)
def verify_email(token: str=""):
    if not token:
        return HTMLResponse(render_error_page(400,"Yanlış link","Təsdiq linki düzgün deyil."))
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT email FROM panel_users WHERE verify_token=%s",(token,))
        row=c.fetchone()
        if not row:
            return HTMLResponse(render_error_page(400,"Link etibarsızdır","Bu təsdiq linki artıq istifadə olunub və ya etibarsızdır."))
        c.execute("UPDATE panel_users SET email_verified=TRUE, verify_token=NULL WHERE email=%s",(row["email"],))
        conn.commit()
        log_event("EMAIL_VERIFIED", row["email"])
    finally: put_conn(conn)
    return HTMLResponse(render_landing(reg_ok="✅ E-poçtunuz uğurla təsdiqləndi! İndi daxil ola bilərsiniz."))

@app.post("/resend-verification")
def resend_verification(request: Request):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    email=user["email"]
    token=gen_token()
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("UPDATE panel_users SET verify_token=%s, verify_sent_at=%s WHERE email=%s",
                  (token,datetime.now().isoformat(),email))
        conn.commit()
    finally: put_conn(conn)
    sent=send_verification_email(request, email, token)
    log_event("RESEND_VERIFICATION", email, f"gonderildi={sent}")
    ref = request.headers.get("referer") or "/"
    return RedirectResponse(url=ref, status_code=status.HTTP_303_SEE_OTHER)

# ===== PASSWORD RESET =====
@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_get(): return HTMLResponse(render_forgot())

@app.post("/forgot-password", response_class=HTMLResponse)
def forgot_password_post(request: Request, email: str=Form(...)):
    email=email.lower().strip()
    user_row=get_user_db(email)
    ok_msg="Əgər bu e-poçtla hesab mövcuddursa, bərpa linki göndərildi. Zəhmət olmasa qutunuzu yoxlayın."
    if user_row and user_row.get("password"):
        token=gen_token()
        expires=(datetime.now()+timedelta(hours=1)).isoformat()
        conn=get_conn()
        try:
            c=conn.cursor()
            c.execute("UPDATE panel_users SET reset_token=%s, reset_expires=%s WHERE email=%s",(token,expires,email))
            conn.commit()
        finally: put_conn(conn)
        sent=send_reset_email(request, email, token)
        log_event("PASSWORD_RESET_REQUEST", email, f"gonderildi={sent}")
    else:
        log_event("PASSWORD_RESET_REQUEST", email, "hesab yoxdur ve ya google hesabidir - email gonderilmedi")
    return HTMLResponse(render_forgot(ok=ok_msg))

@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_get(token: str=""):
    if not token:
        return HTMLResponse(render_error_page(400,"Yanlış link","Bərpa linki düzgün deyil."))
    return HTMLResponse(render_reset(token))

@app.post("/reset-password", response_class=HTMLResponse)
def reset_password_post(token: str=Form(...), password: str=Form(...), password2: str=Form(...)):
    if password!=password2:
        return HTMLResponse(render_reset(token, err="Şifrələr uyğun gəlmir."))
    if len(password)<6:
        return HTMLResponse(render_reset(token, err="Şifrə ən azı 6 simvol olmalıdır."))
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT email,reset_expires FROM panel_users WHERE reset_token=%s",(token,))
        row=c.fetchone()
        if not row:
            return HTMLResponse(render_error_page(400,"Link etibarsızdır","Bu bərpa linki artıq istifadə olunub və ya etibarsızdır."))
        expires=row.get("reset_expires")
        if not expires or datetime.fromisoformat(expires) < datetime.now():
            return HTMLResponse(render_error_page(400,"Linkin vaxtı bitib","Bərpa linkinin vaxtı bitib. Yenisini tələb edin."))
        c.execute("UPDATE panel_users SET password=%s, reset_token=NULL, reset_expires=NULL WHERE email=%s",
                  (hash_pw(password),row["email"]))
        conn.commit()
        log_event("PASSWORD_RESET_OK", row["email"])
    finally: put_conn(conn)
    return HTMLResponse(render_landing(reg_ok="✅ Şifrəniz yeniləndi! İndi daxil ola bilərsiniz."))

# ===== MAIN ROUTES =====
@app.post("/order")
def create_order(request: Request, product_id:int=Form(...), quantity:int=Form(...), profile_link:str=Form(...)):
    user=current_user(request)
    is_ajax = request.headers.get("x-requested-with","")=="fetch"
    if not user:
        return JSONResponse({"ok":False,"error":"Sessiyanız bitib. Zəhmət olmasa yenidən daxil olun."},status_code=401) if is_ajax else RedirectResponse(url="/")
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM products WHERE product_id=%s",(product_id,))
        prod=c.fetchone()
        if not prod:
            if is_ajax: return JSONResponse({"ok":False,"error":"Xidmət tapılmadı."},status_code=404)
            raise HTTPException(status_code=404)
        min_q=int(prod.get("min_qty") or 1)
        max_q=int(prod.get("max_qty") or 1000000)
        if quantity<min_q or quantity>max_q:
            msg=f"Miqdar {min_q}-{max_q} aralığında olmalıdır."
            if is_ajax: return JSONResponse({"ok":False,"error":msg},status_code=200)
            raise HTTPException(status_code=400, detail=msg)
        link=profile_link.strip()
        parsed=urlparse(link)
        if parsed.scheme not in ("http","https") or "." not in (parsed.netloc or ""):
            msg="Link formatı düzgün deyil."
            if is_ajax: return JSONResponse({"ok":False,"error":msg},status_code=200)
            raise HTTPException(status_code=400, detail=msg)
        total=float(prod["price"])*quantity/1000
        bal=float(get_balance(user["email"])["balance"])
        if bal<total:
            log_event("ORDER_INSUFFICIENT_BALANCE", user["email"], f"product_id={product_id} qty={quantity} total={total} bal={bal}")
            msg="Balansda kifayət qədər vəsait yoxdur"
            if is_ajax: return JSONResponse({"ok":False,"error":msg},status_code=200)
            return RedirectResponse(url=f"/?order_err={quote(msg)}",status_code=status.HTTP_303_SEE_OTHER)
        c.execute("INSERT INTO orders (customer_email,product_id,quantity,profile_link,price,start_count,remains,status,order_date) VALUES (%s,%s,%s,%s,%s,0,%s,'Gözləmədə',%s) RETURNING order_id",
                  (user["email"],product_id,quantity,profile_link,total,quantity,datetime.now().isoformat()))
        new_order_id = c.fetchone()["order_id"]
        c.execute("UPDATE panel_users SET balance=balance-%s,total_spent=total_spent+%s,total_orders=total_orders+1 WHERE email=%s",
                  (total,total,user["email"]))
        conn.commit()
        log_event("ORDER_CREATED", user["email"], f"product_id={product_id} qty={quantity} total={total}")

        # ===== PanelBaku API-yə avtomatik ötürmə =====
        # Məhsulun admin tərəfindən təyin olunmuş provider_service_id-si varsa,
        # sifariş dərhal PanelBaku-ya göndərilir və gələn order id yadda saxlanılır.
        prov_service = prod.get("provider_service_id")
        if prov_service:
            resp = panelbaku_send_order(prov_service, link, quantity)
            c2 = conn.cursor()
            if resp and "order" in resp:
                c2.execute("UPDATE orders SET provider_order_id=%s, status='Davam Edir' WHERE order_id=%s",
                           (str(resp["order"]), new_order_id))
                log_event("PANELBAKU_ORDER_SENT", user["email"], f"local_order={new_order_id} provider_order={resp['order']}")
            else:
                err_msg = (resp or {}).get("error") if isinstance(resp, dict) else "Cavab alınmadı"
                c2.execute("UPDATE orders SET provider_error=%s WHERE order_id=%s", (str(err_msg), new_order_id))
                log_error("PANELBAKU_ORDER_FAIL", err_msg, user["email"])
            conn.commit()
    finally: put_conn(conn)
    if is_ajax: return JSONResponse({"ok":True,"redirect":"/orders?new=1"})
    return RedirectResponse(url="/orders?new=1",status_code=status.HTTP_303_SEE_OTHER)

@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, new:int=0):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    try:
        sync_orders_from_provider()
    except Exception as e:
        log_error("PANELBAKU_SYNC_ORDERS_PAGE", e, user["email"])
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("""SELECT o.*,
                   COALESCE(p.name,'Silinmis xidmet') AS product_name
                   FROM orders o LEFT JOIN products p ON o.product_id=p.product_id
                   WHERE o.customer_email=%s ORDER BY o.order_id DESC""",(user["email"],))
        orders=c.fetchall()
    finally: put_conn(conn)
    return HTMLResponse(render_orders(user,get_balance(user["email"]),orders,new=bool(new)))

@app.get("/api/orders/status")
def api_orders_status(request: Request):
    """Sifariş cədvəlinin real-time yenilənməsi üçün yüngül JSON endpoint."""
    user=current_user(request)
    if not user: raise HTTPException(status_code=401)
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT order_id,status,start_count,remains FROM orders WHERE customer_email=%s ORDER BY order_id DESC LIMIT 100",
                  (user["email"],))
        rows=c.fetchall()
    finally: put_conn(conn)
    return JSONResponse([dict(r) for r in rows])

@app.get("/balance", response_class=HTMLResponse)
def balance_page(request: Request, sent:int=0):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM topups WHERE customer_email=%s ORDER BY topup_id DESC LIMIT 30",(user["email"],))
        history=c.fetchall()
    finally: put_conn(conn)
    return HTMLResponse(render_balance(user,get_balance(user["email"]),sent=bool(sent),history=history))

@app.post("/balance")
async def submit_balance(request: Request, amount_sent:str=Form(...), receipt:UploadFile=File(...)):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    data=await receipt.read()
    conn=get_conn()
    try:
        c=conn.cursor()
        try:
            c.execute("INSERT INTO topups (customer_email,amount_sent,status,topup_date) VALUES (%s,%s,'Yoxlanılır',%s)",
                      (user["email"],amount_sent,datetime.now().isoformat()))
            conn.commit()
        except Exception as db_err:
            conn.rollback()
            log_error("TOPUP_DB_ERROR", db_err, user["email"])
    finally: put_conn(conn)
    log_event("TOPUP_SUBMITTED", user["email"], f"amount={amount_sent}")
    if BOT_TOKEN and ADMIN_TELEGRAM_ID:
        cap=f"💳 Balans sorğusu\n👤 {user['name']} ({user['email']})\n💰 Məbləğ: {amount_sent} ₼"
        try:
            async with httpx.AsyncClient() as cl:
                await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                    data={"chat_id":ADMIN_TELEGRAM_ID,"caption":cap},
                    files={"photo":(receipt.filename or "qebz.jpg",data,receipt.content_type)})
        except Exception as e:
            log_error("TELEGRAM_NOTIFY_FAIL", e, user["email"])
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
    if BOT_TOKEN and ADMIN_TELEGRAM_ID:
        cap=f"🎫 Yeni ticket\n👤 {user['name']} ({user['email']})\n📌 {subject}\n💬 {message}"
        try:
            with httpx.Client() as cl:
                cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",data={"chat_id":ADMIN_TELEGRAM_ID,"text":cap})
        except: pass
    return RedirectResponse(url="/support?ok=Ticketiniz+uğurla+yaradıldı.",status_code=status.HTTP_303_SEE_OTHER)

@app.get("/support/{ticket_id}", response_class=HTMLResponse)
def ticket_detail(request: Request, ticket_id:int):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM tickets WHERE ticket_id=%s AND customer_email=%s",(ticket_id,user["email"]))
        t=c.fetchone()
        if not t: raise HTTPException(status_code=404)
        c.execute("SELECT * FROM ticket_replies WHERE ticket_id=%s ORDER BY reply_id",(ticket_id,))
        replies=c.fetchall()
        c.execute("UPDATE tickets SET user_unread=FALSE WHERE ticket_id=%s",(ticket_id,))
        conn.commit()
    finally: put_conn(conn)
    return HTMLResponse(render_ticket_detail(user,get_balance(user["email"]),t,replies))

@app.post("/support/{ticket_id}/reply")
async def ticket_reply(request: Request, ticket_id:int, message:str=Form(...), attachment:UploadFile=File(None)):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM tickets WHERE ticket_id=%s AND customer_email=%s",(ticket_id,user["email"]))
        t=c.fetchone()
        if not t: raise HTTPException(status_code=404)
        has_att = bool(attachment and attachment.filename)
        c.execute("INSERT INTO ticket_replies (ticket_id,sender,message,created_at,attachment_url) VALUES (%s,'user',%s,%s,%s)",
                  (ticket_id,message,datetime.now().isoformat(),"1" if has_att else None))
        c.execute("UPDATE tickets SET user_unread=FALSE WHERE ticket_id=%s",(ticket_id,))
        conn.commit()
    finally: put_conn(conn)
    if BOT_TOKEN and ADMIN_TELEGRAM_ID:
        cap=f"🎫 Ticket #{ticket_id} — yeni cavab\n👤 {user['name']} ({user['email']})\n💬 {message}"
        try:
            async with httpx.AsyncClient() as cl:
                if attachment and attachment.filename:
                    data=await attachment.read()
                    await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                        data={"chat_id":ADMIN_TELEGRAM_ID,"caption":cap},
                        files={"document":(attachment.filename,data,attachment.content_type)})
                else:
                    await cl.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",data={"chat_id":ADMIN_TELEGRAM_ID,"text":cap})
        except: pass
    return RedirectResponse(url=f"/support/{ticket_id}",status_code=status.HTTP_303_SEE_OTHER)

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

# ===== PROFILE =====
@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, msg:str="", err:str=""):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    udb=get_user_db(user["email"]) or {}
    return HTMLResponse(render_profile(user,get_balance(user["email"]),udb,msg=msg,err=err))

@app.post("/profile/avatar")
async def profile_avatar(request: Request, avatar:UploadFile=File(...)):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    data=await avatar.read()
    if len(data) > 1_500_000:
        return RedirectResponse(url="/profile?err=Şəkil+çox+böyükdür+(maks+1.5MB).",status_code=status.HTTP_303_SEE_OTHER)
    import base64 as _b64
    mime = avatar.content_type or "image/png"
    data_uri = f"data:{mime};base64,{_b64.b64encode(data).decode()}"
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("UPDATE panel_users SET avatar_url=%s WHERE email=%s",(data_uri,user["email"]))
        conn.commit()
    finally: put_conn(conn)
    return RedirectResponse(url="/profile?msg=Profil+şəkli+yeniləndi.",status_code=status.HTTP_303_SEE_OTHER)

@app.post("/profile/password")
def profile_password(request: Request, current_password:str=Form(...), new_password:str=Form(...), new_password2:str=Form(...)):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    udb=get_user_db(user["email"]) or {}
    if not udb.get("password") or hash_pw(current_password)!=udb.get("password"):
        return RedirectResponse(url="/profile?err=Cari+şifrə+yanlışdır.",status_code=status.HTTP_303_SEE_OTHER)
    if new_password!=new_password2:
        return RedirectResponse(url="/profile?err=Yeni+şifrələr+uyğun+gəlmir.",status_code=status.HTTP_303_SEE_OTHER)
    if len(new_password)<6:
        return RedirectResponse(url="/profile?err=Şifrə+ən+azı+6+simvol+olmalıdır.",status_code=status.HTTP_303_SEE_OTHER)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("UPDATE panel_users SET password=%s WHERE email=%s",(hash_pw(new_password),user["email"]))
        conn.commit()
    finally: put_conn(conn)
    return RedirectResponse(url="/profile?msg=Şifrə+uğurla+yeniləndi.",status_code=status.HTTP_303_SEE_OTHER)

@app.post("/profile/apikey/generate")
def profile_apikey_generate(request: Request):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    key="bp_"+secrets.token_hex(20)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("UPDATE panel_users SET api_key=%s WHERE email=%s",(key,user["email"]))
        conn.commit()
    finally: put_conn(conn)
    return RedirectResponse(url="/profile?msg=API+açarı+yaradıldı.",status_code=status.HTTP_303_SEE_OTHER)

@app.post("/profile/apikey/revoke")
def profile_apikey_revoke(request: Request):
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("UPDATE panel_users SET api_key=NULL WHERE email=%s",(user["email"],))
        conn.commit()
    finally: put_conn(conn)
    return RedirectResponse(url="/profile?msg=API+açarı+silindi.",status_code=status.HTTP_303_SEE_OTHER)

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
        c.execute("SELECT * FROM panel_users ORDER BY created_at DESC LIMIT 50")
        users=c.fetchall()
        c.execute("SELECT * FROM tickets WHERE status!='Bağlı' ORDER BY ticket_id DESC LIMIT 20")
        open_tickets=c.fetchall()
        c.execute("SELECT COUNT(*) AS n FROM panel_users")
        total_users=c.fetchone()["n"]
        c.execute("SELECT COUNT(*) AS n FROM panel_users WHERE total_orders>0")
        active_users=c.fetchone()["n"]
        c.execute("SELECT email,name,total_spent,total_orders FROM panel_users ORDER BY total_spent DESC LIMIT 5")
        top_users=c.fetchall()
        c.execute("""SELECT COALESCE(SUBSTRING(order_date FROM 1 FOR 10),'—') AS d, SUM(price) AS rev
                     FROM orders GROUP BY d ORDER BY d DESC LIMIT 7""")
        daily_rev=list(reversed(c.fetchall()))
        c.execute("SELECT * FROM products ORDER BY category,product_id")
        all_products_adm=c.fetchall()
    finally: put_conn(conn)

    pending_html=""
    for t in pending:
        pending_html+=f"""<tr>
<td>#{t["topup_id"]}</td><td>{t["customer_email"]}</td><td>{t["amount_sent"]} ₼</td>
<td><a href="/admin/approve?topup_id={t["topup_id"]}&pw={pw}&amount={t["amount_sent"]}&email={t["customer_email"]}" style="color:green;font-weight:700;">✅ Təsdiqlə</a></td>
</tr>"""

    product_rows=""
    for p in all_products_adm:
        psid = p.get("provider_service_id") or ""
        product_rows+=f"""<tr>
<td>#{p["product_id"]}</td>
<td>{p["name"]}</td>
<td>{cat_label_html(p["category"])}</td>
<td>{p["price"]} ₼</td>
<td>
<form method="post" action="/admin/product/{p["product_id"]}/link?pw={pw}" style="display:flex;gap:6px;">
<input type="text" name="provider_service_id" value="{psid}" placeholder="PanelBaku Service ID" style="width:120px;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:6px;padding:6px 8px;font-size:12px;">
<button class="btn-or btn-sm" type="submit" style="width:auto;padding:6px 12px;">💾</button>
</form>
</td>
<td>{"🔗 Bağlıdır" if psid else "⛔ Bağlı deyil"}</td>
</tr>"""

    manage_rows=""
    for p in all_products_adm:
        manage_rows+=f"""<tr>
<form method="post" action="/admin/product/{p["product_id"]}/edit?pw={pw}">
<td>#{p["product_id"]}</td>
<td><input type="text" name="name" value="{p["name"]}" style="width:140px;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:6px;padding:6px 8px;font-size:12px;"></td>
<td><input type="text" name="category" value="{p["category"]}" style="width:100px;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:6px;padding:6px 8px;font-size:12px;"></td>
<td><input type="text" name="price" value="{p["price"]}" style="width:70px;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:6px;padding:6px 8px;font-size:12px;"></td>
<td><input type="number" name="min_qty" value="{p.get("min_qty") or 10}" style="width:70px;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:6px;padding:6px 8px;font-size:12px;"></td>
<td><input type="number" name="max_qty" value="{p.get("max_qty") or 100000}" style="width:80px;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:6px;padding:6px 8px;font-size:12px;"></td>
<td style="display:flex;gap:6px;">
<button class="btn-or btn-sm" type="submit" style="width:auto;padding:6px 10px;">💾</button>
</form>
<a href="/admin/product/{p["product_id"]}/delete?pw={pw}" onclick="return confirm('Bu məhsulu silmək istədiyinizə əminsiniz?');" style="color:#DC2626;font-weight:700;text-decoration:none;padding:6px 10px;">🗑️</a>
</td>
</tr>"""

    categories_set = sorted({p["category"] for p in all_products_adm})
    category_rows=""
    for cat in categories_set:
        cnt = sum(1 for p in all_products_adm if p["category"]==cat)
        category_rows+=f"""<tr>
<td>{cat_label_html(cat)}</td>
<td>{cnt} məhsul</td>
<td style="display:flex;gap:6px;">
<form method="post" action="/admin/category/rename?pw={pw}" style="display:flex;gap:6px;">
<input type="hidden" name="old_category" value="{cat}">
<input type="text" name="new_category" placeholder="Yeni ad" style="width:110px;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:6px;padding:6px 8px;font-size:12px;">
<button class="btn-or btn-sm" type="submit" style="width:auto;padding:6px 10px;">✏️</button>
</form>
<a href="/admin/category/delete?category={cat}&pw={pw}" onclick="return confirm('Bu kateqoriyadakı BÜTÜN məhsullar silinəcək. Əminsiniz?');" style="color:#DC2626;font-weight:700;text-decoration:none;padding:6px 10px;">🗑️ Sil</a>
</td>
</tr>"""

    user_rows=""
    for u in users:
        user_rows+=f"<tr><td>{u['email']}</td><td>{u['name']}</td><td>{u['balance']} ₼</td><td>{u['total_orders']}</td></tr>"

    ticket_html=""
    for t in open_tickets:
        msg_short = (t["message"] or "")[:120]
        ticket_html+=f"""<div style="border:1px solid #BFDBFE;border-radius:10px;padding:14px;margin-bottom:12px;background:#EFF6FF;">
<div style="font-size:13px;font-weight:700;color:var(--or);margin-bottom:4px;">#{t["ticket_id"]} · {t["subject"]} · {t["customer_email"]}</div>
<div style="font-size:13px;color:#4B5563;margin-bottom:10px;">{msg_short}</div>
<form method="post" action="/admin/ticket/{t["ticket_id"]}/reply?pw={pw}" style="display:flex;gap:8px;">
<input style="flex:1;background:#fff;border:1px solid #BFDBFE;border-radius:8px;padding:8px 10px;font-size:13px;" type="text" name="message" placeholder="Cavab yazın..." required>
<button class="btn-or" style="width:auto;padding:8px 16px;font-size:13px;" type="submit">Göndər</button>
</form>
</div>"""
    if not ticket_html:
        ticket_html="<p style='color:#6B7280;'>Açıq ticket yoxdur.</p>"

    top_users_html=""
    for i,u in enumerate(top_users):
        top_users_html+=f"""<tr><td>#{i+1}</td><td>{u['email']}</td><td>{u['name'] or '-'}</td>
<td>{float(u['total_spent'] or 0):.2f} ₼</td><td>{u['total_orders'] or 0}</td></tr>"""
    if not top_users_html:
        top_users_html='<tr><td colspan="5" style="color:#6B7280;">Məlumat yoxdur.</td></tr>'

    max_rev = max([float(d["rev"] or 0) for d in daily_rev], default=0) or 1
    rev_bars = "".join(
        f'<div class="chart-bar-col"><div class="chart-bar" style="height:{max(6,round((float(d["rev"] or 0)/max_rev)*100))}%;background:linear-gradient(180deg,#93C5FD,var(--or));" title="{d["d"]}: {float(d["rev"] or 0):.2f} ₼"></div><div class="chart-lbl" style="color:#6B7280;">{d["d"][5:] if d["d"] else "—"}</div></div>'
        for d in daily_rev
    ) if daily_rev else ""

    return HTMLResponse(f"""<!DOCTYPE html><html><head>{HEAD("Admin Panel")}<style>
.adm{{padding:28px 0 60px;max-width:1000px;margin:0 auto;}}
.adm-card{{background:#fff;border:1px solid #BFDBFE;border-radius:12px;padding:20px;margin-bottom:20px;}}
.adm-card h3{{color:var(--or);margin-bottom:14px;font-size:16px;}}
table.at{{width:100%;border-collapse:collapse;font-size:13px;}}
table.at th{{background:linear-gradient(90deg,var(--or),var(--ord));color:#fff;padding:10px 12px;text-align:left;}}
table.at td{{padding:10px 12px;border-bottom:1px solid #FEE2C8;}}
</style></head><body style="background:#f9fafb;">
<header class="adm-head-gradient" style="padding:0 20px;height:60px;display:flex;align-items:center;box-shadow:0 4px 20px rgba(59,130,246,.3);">
<span style="font-weight:800;color:#fff;font-size:18px;">⚡ {BRAND} — Admin Panel</span>
</header>
<div class="adm wrap">

<div class="adm-stat-grid">
  <div class="adm-stat"><div class="n">{total_users}</div><div class="l">👥 Ümumi İstifadəçi</div></div>
  <div class="adm-stat"><div class="n">{active_users}</div><div class="l">🟢 Aktiv İstifadəçi</div></div>
  <div class="adm-stat"><div class="n">{len(pending)}</div><div class="l">⏳ Gözləyən Sorğu</div></div>
  <div class="adm-stat"><div class="n">{len(open_tickets)}</div><div class="l">🎫 Açıq Ticket</div></div>
</div>

<div class="adm-card">
<h3>📈 Günlük Gəlir (son 7 gün)</h3>
{'<div class="chart-wrap">'+rev_bars+'</div>' if daily_rev else "<p style='color:#6B7280;'>Hələ məlumat yoxdur.</p>"}
</div>

<div class="adm-card">
<h3>🏆 Top İstifadəçilər</h3>
<table class="at"><thead><tr><th>#</th><th>Email</th><th>Ad</th><th>Xərclənən</th><th>Sifariş</th></tr></thead>
<tbody>{top_users_html}</tbody></table>
</div>

<div class="adm-card">
<h3>⏳ Gözləyən Balans Sorğuları</h3>
{"<table class='at'><thead><tr><th>ID</th><th>Email</th><th>Məbləğ</th><th>Əməliyyat</th></tr></thead><tbody>"+pending_html+"</tbody></table>" if pending else "<p style='color:#6B7280;'>Gözləyən sorğu yoxdur.</p>"}
</div>

<div class="adm-card">
<h3>💰 Balans əlavə et</h3>
<form method="post" action="/admin/credit?pw={pw}" style="display:flex;gap:10px;flex-wrap:wrap;">
<input style="flex:1;min-width:200px;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:10px 12px;font-size:14px;" type="email" name="email" placeholder="Email" required>
<input style="width:120px;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:10px 12px;font-size:14px;" type="text" name="amount" placeholder="Məbləğ" required>
<button class="btn-or" style="width:auto;padding:10px 20px;" type="submit">Əlavə et</button>
</form>
</div>

<div class="adm-card">
<h3>🔌 PanelBaku API</h3>
<p style="font-size:13px;color:#7c6f5f;margin-bottom:10px;">
Məhsula "PanelBaku Service ID" təyin etsəniz, o məhsuldan gələn sifarişlər avtomatik PanelBaku-ya ötürülür.
Aşağıdakı düymə ilə açıq sifarişlərin statusunu (Davam edir/Tamamlandı/Qalıq) əl ilə sinxronlaşdıra bilərsiniz — bu, istifadəçi "Sifarişlərim" səhifəsini açanda da avtomatik baş verir.</p>
<a href="/admin/sync?pw={pw}" class="btn-or" style="display:inline-block;width:auto;padding:10px 20px;text-decoration:none;">🔄 İndi sinxronlaşdır</a>
<h3 style="margin-top:18px;">🔗 Məhsulları PanelBaku xidmətlərinə bağla</h3>
<p style="font-size:13px;color:#7c6f5f;margin-bottom:10px;">Hər bir öz məhsulunuzun sağındakı xanaya PanelBaku-dakı uyğun xidmətin (service) ID-sini yazıb 💾 düyməsinə basın. Bundan sonra həmin məhsuldan sifariş verən müştərilərin sifarişi avtomatik PanelBaku-dakı bu ID-yə göndəriləcək.</p>
<div style="overflow-x:auto;">
<table class="at"><thead><tr><th>ID</th><th>Ad</th><th>Kateqoriya</th><th>Qiymət</th><th>PanelBaku Service ID</th><th>Status</th></tr></thead>
<tbody>{product_rows}</tbody></table>
</div>
</div>

<div class="adm-card">
<h3>🛠️ Kateqoriyalar (redaktə et / sil)</h3>
<div style="overflow-x:auto;">
<table class="at"><thead><tr><th>Kateqoriya</th><th>Məhsul sayı</th><th>Əməliyyat</th></tr></thead>
<tbody>{category_rows if category_rows else "<tr><td colspan='3' style='padding:14px;color:#6B7280;'>Kateqoriya yoxdur</td></tr>"}</tbody></table>
</div>
</div>

<div class="adm-card">
<h3>✏️ Məhsulları redaktə et / sil</h3>
<div style="overflow-x:auto;">
<table class="at"><thead><tr><th>ID</th><th>Ad</th><th>Kateqoriya</th><th>Qiymət (1000/₼)</th><th>Min</th><th>Max</th><th>Əməliyyat</th></tr></thead>
<tbody>{manage_rows if manage_rows else "<tr><td colspan='7' style='padding:14px;color:#6B7280;'>Məhsul yoxdur</td></tr>"}</tbody></table>
</div>
</div>

<div class="adm-card">
<h3>📦 Məhsul əlavə et</h3>
<form method="post" action="/admin/product?pw={pw}" style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
<input style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:10px 12px;font-size:13px;" type="text" name="name" placeholder="Xidmət adı" required>
<input style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:10px 12px;font-size:13px;" type="text" name="category" placeholder="Kateqoriya (məs. TikTok)" required>
<input style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:10px 12px;font-size:13px;" type="text" name="price" placeholder="Qiymət (₼) - 1000 ədəd üçün" required>
<input style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:10px 12px;font-size:13px;" type="number" name="min_qty" placeholder="Min miqdar" value="10">
<input style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:10px 12px;font-size:13px;" type="number" name="max_qty" placeholder="Max miqdar" value="100000">
<input style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:10px 12px;font-size:13px;" type="text" name="description" placeholder="Açıqlama (istəyə bağlı)">
<input style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:10px 12px;font-size:13px;" type="text" name="provider_service_id" placeholder="PanelBaku Service ID (avtomatik ötürmə üçün, istəyə bağlı)">
<button class="btn-or" style="grid-column:1/-1;" type="submit">Məhsul əlavə et</button>
</form>
</div>

<div class="adm-card">
<h3>📢 Xəbər əlavə et</h3>
<form method="post" action="/admin/announce?pw={pw}" style="display:flex;flex-direction:column;gap:10px;">
<input style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:10px 12px;font-size:13px;" type="text" name="title" placeholder="Başlıq" required>
<textarea style="background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;padding:10px 12px;font-size:13px;min-height:80px;" name="content" placeholder="Məzmun..." required></textarea>
<button class="btn-or" type="submit">Yayımla</button>
</form>
</div>

<div class="adm-card">
<h3>🎫 Açıq Ticketlər</h3>
{ticket_html}
</div>

<div class="adm-card">
<h3>👥 İstifadəçilər</h3>
<input class="adm-search" type="text" id="adm-user-search" placeholder="🔍 Email və ya ad üzrə axtar..." oninput="admFilterUsers(this.value)">
<table class="at"><thead><tr><th>Email</th><th>Ad</th><th>Balans</th><th>Sifariş</th></tr></thead>
<tbody id="adm-user-tbody">{user_rows}</tbody></table>
</div>

</div>
<script>
function admFilterUsers(q){{
  q=q.toLowerCase();
  document.querySelectorAll('#adm-user-tbody tr').forEach(function(r){{
    r.style.display = r.innerText.toLowerCase().includes(q) ? '' : 'none';
  }});
}}
</script>
{SCRIPTS()}
</body></html>""")

@app.get("/admin/sync")
def admin_sync(pw:str):
    if pw!=ADMIN_KEY: raise HTTPException(403)
    n = sync_orders_from_provider()
    log_event("ADMIN_MANUAL_SYNC", "", f"updated={n}")
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/product/{product_id}/link")
def admin_product_link(product_id:int, pw:str, provider_service_id:str=Form("")):
    """Mövcud (əvvəldən əlavə edilmiş) məhsulu PanelBaku-dakı bir xidmət ID-sinə bağlayır."""
    if pw!=ADMIN_KEY: raise HTTPException(403)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("UPDATE products SET provider_service_id=%s WHERE product_id=%s",
                  (provider_service_id.strip() or None, product_id))
        conn.commit()
        log_event("ADMIN_PRODUCT_LINKED", "", f"product_id={product_id} provider_service_id={provider_service_id}")
    finally: put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/product/{product_id}/edit")
def admin_product_edit(product_id:int, pw:str, name:str=Form(...), category:str=Form(...),
                        price:str=Form(...), min_qty:int=Form(10), max_qty:int=Form(100000)):
    """Mövcud məhsulun adını, kateqoriyasını, qiymətini və miqdar aralığını yeniləyir."""
    if pw!=ADMIN_KEY: raise HTTPException(403)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("UPDATE products SET name=%s,category=%s,price=%s,min_qty=%s,max_qty=%s WHERE product_id=%s",
                  (name.strip(),category.strip(),float(str(price).replace(",",".")),min_qty,max_qty,product_id))
        conn.commit()
        log_event("ADMIN_PRODUCT_EDITED", "", f"product_id={product_id}")
    finally: put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

@app.get("/admin/product/{product_id}/delete")
def admin_product_delete(product_id:int, pw:str):
    """Məhsulu tamamilə silir."""
    if pw!=ADMIN_KEY: raise HTTPException(403)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("DELETE FROM products WHERE product_id=%s",(product_id,))
        conn.commit()
        log_event("ADMIN_PRODUCT_DELETED", "", f"product_id={product_id}")
    finally: put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/category/rename")
def admin_category_rename(pw:str, old_category:str=Form(...), new_category:str=Form(...)):
    """Bir kateqoriyanın adını, ona bağlı bütün məhsullarda dəyişir."""
    if pw!=ADMIN_KEY: raise HTTPException(403)
    new_category=new_category.strip()
    if new_category:
        conn=get_conn()
        try:
            c=conn.cursor()
            c.execute("UPDATE products SET category=%s WHERE category=%s",(new_category,old_category))
            conn.commit()
            log_event("ADMIN_CATEGORY_RENAMED", "", f"{old_category} -> {new_category}")
        finally: put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

@app.get("/admin/category/delete")
def admin_category_delete(pw:str, category:str):
    """Bir kateqoriyadakı bütün məhsulları silir."""
    if pw!=ADMIN_KEY: raise HTTPException(403)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("DELETE FROM products WHERE category=%s",(category,))
        conn.commit()
        log_event("ADMIN_CATEGORY_DELETED", "", category)
    finally: put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

@app.get("/admin/approve")
def admin_approve(pw:str,topup_id:int,amount:str,email:str):
    if pw!=ADMIN_KEY:
        log_event("ADMIN_AUTH_FAIL", email, "admin_approve")
        raise HTTPException(403)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("UPDATE topups SET status='Təsdiqləndi' WHERE topup_id=%s",(topup_id,))
        try: amt=float(amount.replace(",","."))
        except: amt=0
        c.execute("INSERT INTO panel_users (email,name,balance) VALUES (%s,%s,%s) ON CONFLICT (email) DO UPDATE SET balance=panel_users.balance+EXCLUDED.balance",
                  (email,email,amt))
        conn.commit()
        log_event("ADMIN_TOPUP_APPROVED", email, f"topup_id={topup_id} amount={amt}")
    finally: put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/ticket/{ticket_id}/reply")
def admin_ticket_reply(ticket_id:int, pw:str, message:str=Form(...)):
    if pw!=ADMIN_KEY: raise HTTPException(403)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("INSERT INTO ticket_replies (ticket_id,sender,message,created_at) VALUES (%s,'admin',%s,%s)",
                  (ticket_id,message,datetime.now().isoformat()))
        c.execute("UPDATE tickets SET user_unread=TRUE WHERE ticket_id=%s",(ticket_id,))
        conn.commit()
    finally: put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/credit")
def admin_credit(pw:str,email:str=Form(...),amount:str=Form(...)):
    if pw!=ADMIN_KEY:
        log_event("ADMIN_AUTH_FAIL", email, "admin_credit")
        raise HTTPException(403)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("INSERT INTO panel_users (email,name,balance) VALUES (%s,%s,%s) ON CONFLICT (email) DO UPDATE SET balance=panel_users.balance+EXCLUDED.balance",
                  (email.lower(),email.lower(),float(amount)))
        conn.commit()
        log_event("ADMIN_CREDIT", email.lower(), f"amount={amount}")
    finally: put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/product")
def admin_product(pw:str,name:str=Form(...),category:str=Form(...),price:str=Form(...),
                  min_qty:int=Form(10),max_qty:int=Form(100000),description:str=Form(""),
                  provider_service_id:str=Form("")):
    if pw!=ADMIN_KEY:
        log_event("ADMIN_AUTH_FAIL", "", "admin_product")
        raise HTTPException(403)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("INSERT INTO products (name,category,price,min_qty,max_qty,stock,description,provider_service_id) VALUES (%s,%s,%s,%s,%s,999,%s,%s)",
                  (name,category,float(price),min_qty,max_qty,description or None,provider_service_id or None))
        conn.commit()
        log_event("ADMIN_PRODUCT_ADDED", "", f"name={name} price={price}")
    finally: put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/announce")
def admin_announce(pw:str,title:str=Form(...),content:str=Form(...)):
    if pw!=ADMIN_KEY:
        log_event("ADMIN_AUTH_FAIL", "", "admin_announce")
        raise HTTPException(403)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("INSERT INTO announcements (title,content,created_at) VALUES (%s,%s,%s)",
                  (title,content,datetime.now().isoformat()))
        conn.commit()
        log_event("ADMIN_ANNOUNCE", "", f"title={title}")
    finally: put_conn(conn)
    return RedirectResponse(url=f"/admin?pw={pw}",status_code=status.HTTP_303_SEE_OTHER)

# ===== GLOBAL ERROR HANDLING (loglama + professional error sehifeleri) =====
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        log_event("404", "", str(request.url))
        return HTMLResponse(render_error_page(404, "Səhifə tapılmadı", "Axtardığınız səhifə mövcud deyil və ya silinib."), status_code=404)
    if exc.status_code == 403:
        log_event("403", "", str(request.url))
        return HTMLResponse(render_error_page(403, "İcazə yoxdur", "Bu əməliyyat üçün icazəniz yoxdur."), status_code=403)
    log_event(f"HTTP_{exc.status_code}", "", f"{request.url} — {exc.detail}")
    return HTMLResponse(render_error_page(exc.status_code, "Xəta baş verdi", str(exc.detail) or "Xəhmalı bir xəta baş verdi."), status_code=exc.status_code)

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log_error("UNHANDLED_EXCEPTION", exc, str(request.url))
    return HTMLResponse(render_error_page(500, "Server xətası", "Nəsə səhv getdi. Zəhmət olmasa bir az sonra yenidən cəhd edin."), status_code=500)
