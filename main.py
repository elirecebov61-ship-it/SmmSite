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
TELEGRAM_ICON_B64 = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAOoklEQVR42tWbeWwc133HP+/NzN5LUhQviRajw5bquvGRw0cS23WcwLIdF2ncFgUcoEXtohdQBCkKtCiQv1oUNZKmSQsUNZqjKZC2qZvCgW2kdWI3hpP4rG3ZlmUpkmlJFClyuVwul7s7x3uvf8zMcpbisUuRlDqLkSiBO/P7fd/3d78njDGGbbpM9IdJ/hsQ8d9i6eftuuwtVdiAjn6WsXIdKKkjkET0va28xGYzwEQKyBUUbQSaiqupeop6YAiMQQpBxhIUHElfStKbti54pjJbB8amARCvtpUQcnIx4PXZJq/Puhyf95lqKOY9TVMZlDHoSDFLCFIWFGzJYNZib8Hm/f1pbhhIc7AvhRSixQw2GYiLBiBe8VjxOVfx9ESdH07UeaPsUfY0xoAjBbYU2CKy9QRDYvC0AV8bfG0wBnK24EDR4bZdGe66Is+VfalNB+KiAIipDjBVD/iXEws8cXqRibrCloKsHSocAxW/abUXijZHKNDG0FQGVxmKjuCjwxk+e1UPHxzKtEzDEpcIgPjljUDzT+/M8+2f1ZhpKvKOJCVFpLDhYu0r9CUCZQw13+BIuHN3lt+/po8DvalWZBFimwBIhq6fTNV5+LU5js37FB1JSobAbFVctUT47KqnKTqCBw/18tDVvQgh2ti4ZQAkX/KVI2W+dqyKlIKcLVHasF0JhS0gMDDvaT42nOHPbxxgV97ekEl0DECs/IKn+ZPnZ3hqokF/RrY5pe28BGBJQcVVjGQtvnjLIB8YzHQNQkcAxMqXGoo/eO48r8969Gcsgkuh+QVsENSVxgK+eMsgd4zmugJhXQBi5Webiof+Z4pjFZ8daYl/GSi/5BtEK3x+5aOD3DGa7xiENQEw0d0MNA89M8Wrsy59aUmguewuKSDQoI3hH39xhA8OZjpyjHLd1Qe+8GKJl2dc+lIWvlqGzuVwA1qHztEY+PyPpzm3GCDF+v5Jrhfnv3lsnv98d5H+jNWivTGXl/6xPMpAxhJMNxR/+vwMKpa3WwDi1PbtssvfHJljR1q2Hna5X4GB3pTkx1MNHjlaWZcFK/qAsBw1/MYPJnml5FJ0BOoS65+sBs06oVdEv6O04Tt3jXKwL7WqP5ArUV8KeOzUAs9PN+lx5CVTXkTZnyXA04Y5V1P1NDVfr0lrE32vqQxffq3ceUPERCjXA80jR+fJWmEOzhamt8tXTSTyf08b6oFGAHsKDrftznL77hynaz5feq2MFRUAZoVnBQaKjsUzE3V+OtXglpHsiqHRXsn2Hx+vcWLeoz9tbd/qC7AiBi74mkAbhrI2d4zmOTxW4CO7shSdkLDNQPMPb1VY8E2r2lypXjEYjICvv13hlpHsip0oe3ksVcbwnRNV0jIsR7d66eMCpxEYmkFY9t40lOHwWIE7rsgxklsS0dehjOMLPguewhKCtdI4baBgC3461eCtsss1/ekLfIG9PON7dabJm2WPvCO2LMePKe5rQ9XTWBKu6k3xiT15Do/l+bkd6TafFH9HRv7gzEJAPTD0ptZ3zhJBPdA8dmqBa/rTF6ynTYIuIPj+e4t4ylCwJWxiu1AIkCJk1aJn8LRmJGdx154in9pb4OaRLClryaaVNthSLLPZUMYT8x5KA0asK6PGkLUEP5qo8/nrDRlbtHxNGwBxPv38VJ2MBRq9KeyXQiAAVxnqgSJrCz4wmOHevQU+sSfPcILiypjICYbts0rdx7EE+bQduYlQ7ONzLlIYTAcyGgNpKzSbN2abfHg4i0k0UOwk/U/Ne4xXfVJSoPXFeXQpw/S05isCDWNFm0/uKfKpfUWuG8i02WmSISJa/fHZOjU34OBwoS1CKWM4Ne/hRE2QTlZJIPCU5sXzjRCABAfsJP3fmHVZDEyY+W2A/snwtdjUZG3BzSM5Pr2/yJ1X5Npa3iqqM6SgjZJzdZ9TpRopS3LN7h7sZPYjYHpRcW5R4UhBpz5aY7AEvF5y25h0QRQ4VnZDk+rY+4dSxTLWfY2rDKN5m1890MsvHyhybWK1k/39pG0LwA00Z+caTFWbDBRSHBwqIhLgaMIwebLqUXEVxZTs2EkbDI4UjFc9XGVIW0t+wE4iMr7gYwkTxs8OU1NlDPOeRgLvH8jwmQNF7n5fgYGsfcGgJKl0ctWnF1wmKg3qnmK0L8Penfm29yS/cazs4msNSDpuwpmwUiw1AkqNgNGC0xLAjqlrDMzUA6x1bMskau+ar+lxJHePFfi1q3q4dTTfYsNqqx0rLoC6pzg9V2e+7mMM7N2ZY7QvuwbkcLTsIhGYLnKUWOZF3zDTUIwWnCUGxD80VZhny3UsQEZUH8jaPHCol/uvLHKoLW6H4y5LrGYw4fPPzTeYmm8SaIMQofLDPZk2ZixPmLSBExWvK/tPwudrQ8VVbdliywe4yuAqjRBm1YfHKN40kuWvbxtphbDkpMZaoUGfXPVqM+B0uc6iG4RAScG+gTw786lVlY/D1vm6z0TNx5Fh56c7AAQqqi2SUtnxW7VhKfSZ1ZIZga81YwW7LX6blqsRFygQK6W04WylwflqMwqTofO8crBAb9ZZVXmSDrASOsCCI7vOUmPfrvTyTFAsUUwIMBqMXBmDQBuKjuQ/frZAxVV8en8PN+3K0ZexWuKraFQTx3QBlOseZ8oNGr7CscKVSEnBweECuZS9pvJJCrxddvGUQaQiH7CBgc6q1WDaEqStqABibSeYsuDJ8RpPvltjtGBz83CWT76vwM27c+zI2C11vEBzZq5OqeYhBKQsia80uZTFweEiaVuur3zEPIC3Zt3QYWvTdZpuMEgMOUe2OdVWNZm2JcVE88OssyA9KQkIZl3Fv59c4NGTVUbzNjcOZ7l3Xw83DKQ4M9fAUxon2izgKU1vxuGqoQK2JTpSPlmlHp9zcaRA0/0UShuwpWBHlIyJZEcontMPZW18ZRAddCKVjgoWIehLWfSmbcqu4bsna/zWUxM8/e48tginNwC+MvTnUhwa6U752NanFgPO1oKlNL2LrqkwoWnmbclAdgUAYjz39jitaY/p8KONITCaQGssAf0ZiWVJjpQ9UlbIlkAbhotpDg4XWm5SdEFdgONll4obRP2D7j4Ig680O7MWg1GCFgvQ1hO8uj+99MoN9KeNMQTaYGF4bdajGZiojhe8N12jXHNDR9sFf+PffaPUDNm5AdmEAU8Z9vc4pCzRYnwLgHhVrh3MUHCs1qR3I7cykLIEpxYCzi6GlEXAdLXBfx85x6nphRYInYSyeHvMG6VmlKV2LxtRqX/9UDYyK9PeFY7T1/29Kfb1OOHKXcT0xyKcIh+Z9UhZAl/pSBHBT47P8MLJUlT+smbVGaewbqA5Pue2Vq9bebQ2ZCzBLbtybaC2mUDcgfnYaI5GoKIu7cY+mpD6/1tyw81Q2qCilNe2JGfOV/nqc2c4XnaxhFgVhPi/x6s+56IMMNx10vlHYGgEmn29KX4hqkyTyaps61kB9+7rIW3JpYbohhCHtBQcr/icryskIQAayFuC16uGh99c4P7vjfPkyWrY3Fxh2BEnO2+VmtQ8vdQE7eKWQlD3NXfuKZCOkjCx0mDEiuzy+qEs1w9mqPm6VY9v5LYlzLqao3M+0hiCqEP7zLTPt874FNI2DQW//dQEf/n8+UjYlU3i1elGuOFyA2sSaEPekdx/sLctqVpxMqRNSN3PXr0Dr+UHzIbuuFx9eaaJNIaCBd+f9Hj0nB82JKKwWUxJvvxKiQceH2ey5mMJQRA5YRlVfW+WmmH8N7orGUJfpLj9ijxXr9ASvwCA+IX3Hejh5wfSLF4EC7SBrC14adrluXMNnpj0eWwqIBNRTUeAa2MYyNk8c7bOfd99l2dPL2DLpX5/1VOcrCw5wG7lkMDvXrezLadYFQAR9c8ytuAPbxig4WskYoM5QdRnCAxffafBD0qaXFzHL/s9Xxn60halpuKBJ8/wd6/MQNQh/vbROUoNhdOl/VtCUGkq7t4XFmzarFyqrzodFsCvP/4ez55dpDdlbahJ2uZf12lgmETTo+opPjSUpeBIXphqYFui613k8bv+61f2sb8vjVllOrwqAFLAO2WXex49BSKkynaNCS0hWAw0yhgKjux6PmNLwXQ94C9uHeH3rh9AGbPi6q+6QSL2xof603zhI8NUmqpV1GzHpYwhZwuKG1S+3Ay4a2+R37luoNWi62qDRDI5sqTgc09P8K035xjKh9Xi5XpZMoz5I3mbJ+7fz1DeXpX6azIgGRW0gb+6fTcfHytQqgdhbX85Ki8EbqDJWIKvHd7DcAfKrwuASHSLvnb3Hm7claPUCHAuplDY5NtEUx9XhRspvnHPGNcNZSPqd+Cgu9kpWmkqfvPJ9/jRmUUGc/a27g9ey+ZrniLnSL5xzxi3XlFomW5HEarbvcLNQPO5H07wr8cq7MxYyDWKmS3dUBK14GcbAVftSPPI4T1cO5gliIq6jp+z0d3if/vKDA+/MI2vDT1pq1XsiG1ydp4yVF3FL13Zy5c+vpuBrL1muNsUAJL9dSngpck6f/bsJC9N1imkJBk7nCpvBSHiWYKOpjtDOZs/vmmIB6/d2WrHW1t9XmB5rI43VXz9SJm/f7XEe/MeOUeStZcOOW3GiREpwnR5wdPkHMlnDvXxRx8eZKwn1cpat+3EyGomMdsI+Oe3yvzb2xVOzDUByEXHZ4QQreMzq43dkmcK45I10GEzw1OGwazN4f09PHjtTq6LWlsbofymArA0/l4SpO5rnhpf4Hsn5nlxcpHJxSB0TELgWAJbhk0KmYizcX9QGYOvTGtPcl/G4pqBLHfvL3Lflb2M9aTaahVxqU+NXQDEsvBTagS8er7By5N1js42OT3vMdsMWPQ1vorODYowlGUsQV/GYrSQ4mB/mhuGs3xoJMeBZTvGNvsA5RadHA3HpMsFVZEDq7qaeqDxdZisZCxJMS3pS1tkbbliSi6F2JQV33IAljc1NSaKGqKjlQvNwbTaV//vzg6vW6N34AS3NZPc1uztEim51vV/+JZ7VaIAVXcAAAAASUVORK5CYII="
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
.topbar-inner{display:flex;justify-content:space-between;align-items:center;height:84px;}
.brand{display:flex;align-items:center;gap:8px;font-weight:800;font-size:20px;color:var(--or);transition:filter .2s;}
.brand:hover{filter:brightness(1.15);}
.brand svg{width:28px;height:28px;}
.brand img{filter:drop-shadow(0 0 14px rgba(59,130,246,.45));}
.brand-text{display:inline-flex;font-weight:800;font-size:25px;letter-spacing:.2px;}
.brand-white{color:var(--tx);}
.brand-navy{color:#1E3A8A;}
.brand-orange{color:var(--or);}
.hamburger{background:none;border:none;cursor:pointer;padding:10px;color:var(--or);}
.hamburger span{display:block;width:28px;height:3px;background:var(--or);margin:6px 0;border-radius:2px;transition:.3s;}
.topuser{display:flex;align-items:center;gap:12px;font-size:14.5px;color:var(--mu);}
.bal-badge{background:linear-gradient(135deg,rgba(59,130,246,.22),rgba(37,99,235,.1));border:1px solid var(--ln);color:#93C5FD;font-weight:700;padding:8px 16px;border-radius:999px;font-size:14.5px;box-shadow:0 0 18px rgba(59,130,246,.12) inset;}

/* SIDEBAR */
.sb-ov{display:none;position:fixed;top:var(--topbar-h,64px);left:0;right:0;bottom:0;background:rgba(0,0,0,.6);backdrop-filter:blur(2px);z-index:48;}
.sb-ov.open{display:block;animation:pageFadeIn .2s ease;}
.sidebar{position:fixed;top:var(--topbar-h,64px);left:-300px;width:280px;height:calc(100% - var(--topbar-h,64px));background:rgba(255,255,255,.98);backdrop-filter:blur(20px) saturate(140%);-webkit-backdrop-filter:blur(20px) saturate(140%);border-right:1px solid rgba(0,0,0,.08);z-index:49;transition:left .32s cubic-bezier(.4,0,.2,1);box-shadow:8px 0 32px rgba(0,0,0,.25);display:flex;flex-direction:column;overflow-y:auto;}
.sidebar.open{left:0;}
.sb-head{background:linear-gradient(135deg,var(--or),var(--ord));padding:24px 20px 22px;color:#fff;position:relative;overflow:hidden;}
.sb-head::after{content:'';position:absolute;top:-30px;right:-30px;width:120px;height:120px;background:rgba(255,255,255,.12);border-radius:50%;}
.sb-head .sb-name{font-weight:700;font-size:15px;display:flex;align-items:center;flex-wrap:wrap;}
.sb-head .sb-email{font-size:12px;opacity:.85;margin-top:2px;}
.sb-head .sb-bal{margin-top:10px;font-size:13px;background:rgba(255,255,255,.2);display:inline-block;padding:4px 12px;border-radius:999px;}
.sb-topbal{margin:14px 16px 0;font-size:14px;font-weight:700;color:#fff;background:linear-gradient(135deg,var(--or),var(--ord));display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:10px;box-shadow:0 4px 14px rgba(59,130,246,.35);}
.sb-head-btns{display:flex;gap:8px;margin-top:14px;}
.sb-head-btn{flex:1;text-align:center;background:rgba(255,255,255,.22);color:#fff;font-weight:700;font-size:13.5px;padding:11px 6px;border-radius:10px;text-decoration:none;transition:background .15s;}
.sb-head-btn:hover{background:rgba(255,255,255,.34);}
.sb-nav{flex:1;padding:8px 0;margin-top:10px;}
.sb-nav a{display:flex;align-items:center;gap:12px;padding:15px 20px;font-size:15.5px;color:var(--tx);transition:all .18s;border-left:3px solid transparent;}
.sb-nav a:hover{background:rgba(59,130,246,.08);color:var(--or);}
.sb-nav a.active{background:linear-gradient(90deg,rgba(59,130,246,.16),transparent);color:var(--or);border-left:3px solid var(--or);}
.sb-nav a .icon{font-size:19px;width:24px;text-align:center;}
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
@media(max-width:560px){
  .hero-login-card{padding:20px;border-radius:14px;}
  .topbar-inner{height:72px;}
  .brand-text{font-size:21px;}
  .brand img{height:42px!important;}
  .bal-badge{padding:7px 13px;font-size:13px;}
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
.trust-badge{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:#fff;background:rgba(59,130,246,.55);border:1px solid rgba(255,255,255,.25);padding:6px 12px;border-radius:999px;}
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
  function fmtMoney(n){
    n = Number(n)||0;
    /* 1 manatdan yuxarı olanda 2 rəqəmli onluq, aşağı məbləğlərdə (məs. 0.012) tam görünsün deyə 4 rəqəm */
    return n>=1 ? n.toFixed(2) : n.toFixed(4);
  }
  function updatePrice(){
    if(!si||!pi)return;
    var opt=si.options[si.selectedIndex];
    if(!opt)return;
    var price=parseFloat(opt.dataset.price||0);
    var qty=parseInt(qi?qi.value:1)||1;
    pi.innerText=fmtMoney(price*qty/1000)+' ₼';
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

LOGO_SVG = '<img src="/logo.png" alt="PanelimAz" style="height:52px;width:auto;margin-right:2px;"><span class="brand-text"><span class="brand-white">Panelim</span><span class="brand-orange">Az</span></span>'
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
    <span class="bal-badge">{bal:.2f} ₼</span>
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
        ("💳","Balansı artır","/balance","balance",""),
        ("🧩","Servislər","/","services",""),
        ("🎫","Dəstək","/support","support",badge),
        ("📋","Qaydalar","/terms","terms",""),
    ]
    nav = ""
    for ic,lbl,href,key,bd in links:
        cls = "active" if active==key else ""
        nav += f'<a href="{href}" class="{cls}"><span class="icon">{ic}</span>{lbl}{bd}</a>'
    verified_tag = '<span class="sb-verified-tag">✓ Təsdiqlənmiş</span>' if (verified and (email or "").strip().lower()!=OWNER_EMAIL) else ""
    return f"""
<div class="sb-ov" id="sb-ov" onclick="toggleSidebar(false)"></div>
<div class="sidebar" id="sb">
  <div class="sb-topbal">{bal:.3f} ₼</div>
  <div class="sb-head">
    <div class="sb-name">{name}{badge_svg}{verified_tag}</div>
    <div class="sb-bal">Balans: {bal:.3f} ₼</div>
    <div class="sb-head-btns">
      <a class="sb-head-btn" href="/profile">👤 Hesabım</a>
      <a class="sb-head-btn" href="/logout">🚪 Çıxış</a>
    </div>
  </div>
  <nav class="sb-nav">
    {nav}
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
<body style="background:var(--bg,#F3F1F8);">
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
<body style="background:var(--bg,#F3F1F8);">
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
<body style="background:var(--bg,#F3F1F8);">
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
<body style="background:var(--bg,#F3F1F8);">
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
      <span>Qeydiyyatdan keçməklə <a href="/terms" target="_blank">Qaydalar</a> ilə razılaşırsınız.</span>
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
<body style="background:var(--bg,#F3F1F8);">
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
<body style="background:var(--bg,#F3F1F8);">
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
<body style="background:var(--bg,#F3F1F8);">
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
<p>Bu Qaydalar {BRAND} platformasından ("Panel") istifadə edərkən tərəflərin hüquq və vəzifələrini müəyyən edir. Panelə qeydiyyatdan keçməklə və ya istifadə etməklə bu qaydaları qəbul etmiş sayılırsınız.</p>
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
<p>Bu qaydaların pozulması halında {BRAND} istənilən hesabı xəbərdarlıq etmədən müvəqqəti və ya daimi olaraq bloklaya bilər.</p>
<h2>6. Məxfilik və məlumatların qorunması</h2>
<p>{BRAND} olaraq istifadəçilərimizin məxfiliyinə önəm veririk. Aşağıda hansı məlumatları topladığımız və onlardan necə istifadə etdiyimiz izah olunur.</p>
<h3>6.1. Topladığımız məlumatlar</h3>
<ul>
<li>Ad, e-poçt ünvanı və hesabınızı idarə etmək üçün lazım olan digər qeydiyyat məlumatları.</li>
<li>Sifariş tarixçəsi, balans hərəkətləri və dəstək müraciətləri.</li>
<li>Texniki məlumatlar (IP ünvanı, brauzer növü) — təhlükəsizlik və xidmət keyfiyyəti məqsədilə.</li>
</ul>
<h3>6.2. Məlumatlardan istifadə</h3>
<p>Toplanan məlumatlar yalnız hesabınızın idarə olunması, sifarişlərin icrası, dəstək xidməti göstərilməsi və platformanın təhlükəsizliyinin təmin edilməsi məqsədilə istifadə olunur.</p>
<h3>6.3. Məlumatların paylaşılması</h3>
<p>Şəxsi məlumatlarınız üçüncü tərəflərə satılmır. Sifarişin icrası üçün zəruri olan minimal məlumat (məsələn, profil linki) yalnız xidməti təmin edən provayderlə paylaşılır.</p>
<h3>6.4. Məlumatların saxlanması</h3>
<p>Məlumatlarınız hesabınız aktiv olduğu müddətdə, habelə qanuni öhdəliklərin icrası üçün lazım olan müddət ərzində saxlanılır.</p>
<h3>6.5. Sizin hüquqlarınız</h3>
<p>İstənilən vaxt Profil bölməsindən şəxsi məlumatlarınıza baxa, onları yeniləyə və ya hesabınızın silinməsini Dəstək bölməsi vasitəsilə tələb edə bilərsiniz.</p>
<h3>6.6. Kukilər (Cookies)</h3>
<p>Sayt seansınızı idarə etmək və dil seçimini yadda saxlamaq üçün zəruri kukilərdən istifadə olunur.</p>
<h2>7. Dəyişikliklər</h2>
<p>Bu qaydalar zaman-zaman yenilənə bilər. Yenilənmiş qaydalar saytda dərc olunduğu andan qüvvəyə minir.</p>
<h2>8. Əlaqə</h2>
<p>Suallarınız üçün "Dəstək" bölməsi vasitəsilə bizimlə əlaqə saxlaya bilərsiniz.</p>
"""
    return render_legal_page("Qaydalar", body)

def render_privacy():
    return render_terms()

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
        return HTMLResponse(render_register(err="Davam etmək üçün Qaydalar ilə razılaşmalısınız."))
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
