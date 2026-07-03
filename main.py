import hashlib, os, secrets, logging, smtplib, ssl, sys, html as html_lib
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

# ===== "Ümumi Sifarişlər" göstəricisi üçün başlanğıc rəqəm =====
# Dashboard-da hər istifadəçiyə göstərilən "Ümumi Sifarişlər" sayı bu bazadan
# başlayır və istifadəçinin real sifariş sayı ona əlavə olunur (yəni hər yeni
# sifarişdə 1 artır). Railway Variables-da BASE_ORDER_COUNT ilə də dəyişə bilərsiniz.
BASE_ORDER_COUNT = int(os.getenv("BASE_ORDER_COUNT", "1029817"))

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

# DİQQƏT: SimpleConnectionPool THREAD-SAFE DEYİL, amma FastAPI-nin sync
# route-ları (bu fayldakı bütün "def" ilə yazılmış route-lar) arxa planda
# bir thread-pool-da işə düşür — yəni eyni anda bir neçə müştəri bir neçə
# fərqli thread-dən eyni anda get_conn()/put_conn() çağıra bilər. Müştəri
# sayı artdıqca SimpleConnectionPool bu şəraitdə korlanıb qəribə/təhlükəli
# xətalara səbəb ola bilərdi — ona görə ThreadedConnectionPool istifadə
# olunur (thread-safe) və həcmi də bir az artırılıb.
db_pool = psycopg2.pool.ThreadedConnectionPool(2, 20, DATABASE_URL)
def get_conn(): return db_pool.getconn()
def put_conn(c): db_pool.putconn(c)

# ===== ERROR/ACTIVITY LOGGING HELPERS =====
def log_event(kind, email="", detail=""):
    """Butun vacib hereketleri Railway loglarina yazir (login, sifaris, odenis ve s.)"""
    logger.info(f"[{kind}] user={email or '-'} | {detail}")

def log_error(kind, err, email=""):
    logger.error(f"[{kind}] user={email or '-'} | XETA: {err}")

def safe_name(n, max_len=80):
    """İstifadəçi adı (qeydiyyat forması və ya Google profili) HTML-in
    daxilinə birbaşa yazıldığı üçün (məs. menyu, admin panel, liderlik
    lövhəsi) burda təmizlənir ki, kimsə ad yerinə zərərli HTML/JS
    yazaraq başqa istifadəçilərə (və ya admin panelinə) təsir edə bilməsin."""
    n = (n or "").strip()[:max_len]
    return html_lib.escape(n, quote=True)

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

def panelbaku_cancel_order(provider_order_id):
    """Sifarişi PanelBaku-da ləğv etməyə cəhd edir (best-effort)."""
    return panelbaku_call({"action": "cancel", "order": provider_order_id})

def panelbaku_refill_order(provider_order_id):
    """Sifariş üçün PanelBaku-ya bərpa (refill) tələbi göndərir (best-effort)."""
    return panelbaku_call({"action": "refill", "order": provider_order_id})

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
WHATSAPP_ICON_B64 = "iVBORw0KGgoAAAANSUhEUgAAAH8AAACACAYAAAAiebbfAAAxAklEQVR42u19eXRdZ3Xvb+/vO+dKV4OHJA4kJMRx7NiKZzkjKVcupIRAbMvmGh7lQUtbhkLfKqW8QleprL7S1dKWwitpy/gIlMmXeEhCGgjUuoVABstz5DkkKYHgJLY1XEn3nu/b+/1x7pVlx7qDLMcTJytLy5bvcM6efvu3h49wrl0KSiPNh7oOUXZp1p3461uffvuUYclf5Z3OYsJM9bgakFeBeJpCp5CiSVXrAbJEsFAVEByAAkA5AH0KehGEX4LwNAMHFdhPoRwY6g/+u2dupnD89+ngFLp4WmaaZtIZAUHPlUdJ55LAASBDGT/6wbc+tXOWCC0hxY2kuhCKGQqdxgljyBKggIoCUvqpUInfdERMBIAIxACYQEzFn4AqoAWBOskB9CwYuwnaDeZHrRa2Pzrjvl+N/qqpTSmbbWsTUKf8WvinJPQOTnV18WgLv+mZ9FTn3K2i9EYoXgvV2aYpZCJAI4HEggJERQEFAaQgEAAlKt41jaFkRXVQVSr+iQBSYjJEFDAoZJAlaEEgQ65XibaC6CFVfH/r1+ZuQWdR6ApKdaVMti3rz1ZvcFYKP702bTLpFi1ZT8uudGN9g389lNIq+jpTZy6FZWjeQ4Y9VNWDSAElUuIxhXtq3kdBqqpQgBRQZsvMdQYUMPyAA6A7QbgPrPd0X7V+y3HeoKtNRhTj18I/idA1bTI4FjcXHVw1n6H/E8BbuM5cBSbIoIMURMAkpMogojN2HwpVUoGSEmC53oATBn4gAhg/VuBroTXfeeTKzOFj93dMqX8t/BFLPyb0xU+ufAOBPqAid5jG0Migg+S9gKCE02TZE6IMKgoSAiw3WFBAkJz/JTG+5h1/YevMzIGTKfmFKXzt4A4AnUVLaH1y1XIAf0oB30pM8AMRVOGIlBHDsXMoKVEPBTgwxjRY+MEoB6KvA/pP3dPX7TkGDs8cJjhT7jIGQ0Ug13qw/TYw/wUH/FpVwOciieEZ8TmTkZQLDYAnJmuaA/hcNASiL3HOfXLzvI3/PeL5Vo/KYs5b4a9NGxRvdNHeFXNMaP4Kht4CJvj+SEBAUegTDdcAiuHaCJIf9RSoqJRAhYzglJUA1jaH8IPuRUA/2btn8DMH7ngwfyZCwcsq/NSmlM0uzbprHrg90Twr+WfE9Gdcb5K+tyAxVCczYSAMpIjlzWBiYgIMgUws3+Ngosb5PEShXgGvUK8aMwJUTBeV4xee4jMrKgFbsqYpgB902xHJR7pnrX/o5fYCL4/wtYOBTgVBW/esuJUS5jOctItdbwEq6k9J6IpYSERKCkMhE4cMsgwFoAUPGfICoA/QXgX1QTUH0DBIpWjtloCkEhoBbQZoEltKcp2Nc3rRmEPIC1TEA6SnnGkUlcAkjYUC6vVfcbj3Y91LftBbMpJzXvhpTZsiK8etB9v/EswfJ8PsB50jwIzLkkoCB8CWDdcbkGH4IQd18hwRdkNppxKeUPiDhsOfUxi9kDsa9L+Enj0Bi8x/7h1J6stPtuwuBehKkF4rSvMImKuqM23SJmG5pFRQwBUVYVyhSlWFANgpCfaDbh+ce2/3zI1dow3mnBR+SYMX7F5+lU3YL5tGu9QdKaiKKvE4HpbGrB1bNpy0AACfc0cAPALof8Lix7Yp2fPYxV/vqwQ4gY5j974GwJrKD3rJ/uVXwPAiEWoDtA2KBbYpZCkIZNBBCY50fAqtqs7UW6uiCqcf75657hOnOwycHuEXufgMZfzCPSveaBLmK5ww01xfwRHI1vqpquoJIE5aptBA+gu9MPR9El5nyG56dMa3juPX0dHBqbYuBoBpbdM0gxYFOrV4x1r2eSiANSCs6UAaPXSo6xBNa5umx9UUilfrweXzQPYOqLYDdCM3WEgugkTiCaBavYGqChHBTg7ZDUQbh3818O6eW75/+HSFgdNBg8ZWRZ2yaP/KPzEh/6N6heS9J64ttsdCJzZNAalXqPObobjbObN++7WZZ0djilRXF097/jRX1jo6OL0mVogThdF6MH2DsryDRN/KDcE0HXbww752JYjrTc5ODq0Mu93RUVm9Y+H6XSlN2SxNrAJMcDrTweBOgQKLDqz8bNAUfMAdLQhUUcsDKMVB0xSyFDyg+K6QfHbr9PUPjmYFkQbOKFN2rPDkUawRLtyXvsQG+tuq+n5Omlky5CHD3hPVxkyqqjPJwKqXI35I3rqtZf1DE60ANKGCp0655oHbE80zk9+wk4KV0ZFCbTFQoaoqpjEwUIU6uV+VPrllxj0/Og5HnI2VMu3gNHqoFB5aN785iSnhu0D0YZO0M3x/BPFSU2ajqp5DY8Dk/FD0rm1zNn5jIhWAJlLw87fd1mAbmzfYZvv66Eg+IlBQ041aNtwYQIaiR9WhY8vMdd8bebCZHjoTLNipspfX7l7W1BQGf6SEj3C9mex6C0IKAhNV7QUNE9cZksHovVuu3fD5iVIAmijBt+xKN9bX+QdMY/Ab0dF8RFSl4Itpm20OjQy7w/C6pvvf19+FTgi0g5HpIZwLQq+gBPP3pKfbwP+NSZi3SUFqw0CiCktikoHxvfkPbGnZ+C+tm1uD7iXd0ZkTfgcYa6A3/TRdV5jmH7BNQVt0OO+IyVb1fESFLLNptJAhv8Hl3Ye2z9n4FBSUzqQ5cy4KvYIStO5rfwsC/ieuM69yfVH1YVFVwSSmMTC+t/DeLXNO3QPQqdxUOpPmlnSL3rd/x312UnhHdKR6i1eFM0lr1UsOHn/afc09/zaaG8D5dhUzhQxlfMujd7yi7pL6z5qkXeV7CwpVrQoQKxQGwnXW+KPuf2ydt/5bp6IA4xU+pTbF2rxoX/tXgsmJd0WHh2sSvJ0cWhmMdrpB/87tczduS69Nm8wTLXq2dbucLuILABbtX/EnHJhPQmCk4KsDg6KKgJUsiw74N2yZu/4/R7Gop1/4JW1btLu9M7go/MvoSCEiIKhGc5Xgg8mh9bkoM/AL93t7b723/3TksGd7KCiRYIv2LH8d19mvk+FLfc45ItjKxqPCoSEAvRjI39w9//49HdrBnTV2CNF4NXdRT/vb7OTgm26gGLcqvZdCwRDTFBg/4P5uy8x1Hz3d9OVZ7wVKRrTrzmsoGa43dWau6ys4IqpGAbxpCIzm3R4jyRsfm3nNQK21gJrox/TatMkuzbrW3cvncR192Q86IalC8KIKhnLSGt9f+OMtM9d9NK1pAwVdqIIHgCxlXWpTym6de9+B4ef6UzLofmQnJ6wqKnpBIjI+FznTGM52fvCroE5JdaVqYlBroR0JAOZvu60B1nybrKlXJ1oRqaoqDCnXGfb97t1bZm34TEpTNkPn1oDDaVOApVmXXps2Pbd8/3BwyLxBcu57dnJoVbUaBbDR0YKzF4XLF+5a8dHs0liZJlz4qa6UyazOeBs2/LNpDub4wchVBCgKhSHhesuSi35365z1/691c2tQjO8XvOBLV2Z1xkM7+JFbMkNDQ7xMBtz3Yg9QhQIAxvVFzjSaTyzqWXFzSZkmTPhpjd39wl3L3mKmhL8bHa0iLsUx3puGwPiBwvu2zNn4lYkgJs7bizoF2sE9czNRcIjbpT/6sW0OK4cAAmkkTERMlu6ev+0dDaM99akJvwOcQYsu3Je+xNTbu2TYx/3ylaIEwdtJoXW9hb/YOmfj534t+KoVgB65JTNUGMovkyHXYxqsVdWyuIiY2A85ZyeHMznR/7eZ1RlfTfyvKMT0mjSBOoUk+ifTEEyTYV+RkFCFCyaHNjpS+MLWORs+kdKUPWXB6znexVuDAqTXps3O+d89Qrn8nRrJCxwahqpUiv/uSMHbZPDB1j0rbq3G/XMld5+hjF+4r/020xj8dtSbr8hHq6i3TYGN+go/uuZa+/60pk0WWT8eYac2pWxa03E2UQSHaU2bUqZwPmOA1KaU3Tz/u08iF60GQ2FIT+g5Pim2BgFKdFfr5tagktFwuYffghZt2ZUOWfAZiCppRWQvnDAsw/4QnH9bhjI+Hk+qDdyl16YNCJpdmnVF5ko7tGNkSjdDmbikex4rQHZp1qU0Zbvn3btJBv1HbHNoFPAV079B7+zkcL42XPGHmdUZX5puronkKVl96+72/2Wmhp+JjhQqs08Ex/XW+l73xq1z1z84Htqx9JoFm5ZPDq6yK0X0NlLMVNFmEOXIYp8S37vlMfoWVmc89JhXOK+JoN3tG+zkYLk7WijvfRVKAamKHhW1s7fNzLyANSB0QqoTftGi5j3zpsnBcLiXAr5IIynrKVTUB1MTJnoh/+mt12340Hgo2xJ7uPiJFau50fw9JeyVEC2OXMfflgIGGYLPRT9xkVm9fVbmF2Pd3HlxdYCBDrT+j+6pasOdZGia5n3ZzihVuGBKaKPD+U9vnbPhQ2MZIY+V04OgdjD8Yzs5vEQKImUFryomadn1FvY2Jyd9LL229jif0iJt/MSK95nJ4bdVcKU7WnCur+Bl0IkMO5EhJ66v4N3RfME0h7cYdZkUUgZrOs5fANgJSa/poe7Z97+AgvtDDg1rBY6kmPsLWXpv68H0lRlk4t6IisJXULYt61v3vPliMvgj3x8pAaYC1aQgkPfyvuz0u4eRBmpxxalNsZdYuHPZO+3k4F/9YOQl74UIlogMiLj0f0wsURi9MByZScHNvXun3AbqlCIwPD8BIMUAcEvLveuj3sI9dlJoVMqkfwRSr2Kbw3px0f8GQdPooYrCL1m9h3m/nRROEae+HIWrot5OCo3Lua9tn7OxK7UpZWuJ86V6waInVr7WNAdf9kPOwylXnNcjIiiUCe+6IGjgtjaBgmygH/KDrp8CpnLov2j9Ssy/s2Bv+vIMMoKO462fT2b1LbvSjQR6v885JSqbESgHTD4X9VFSPhq/vq362NsBzqRb9PqeFRdRQr9BSkYjraq/jRRGBh0p6I0L96UvyVAR/J3H+X+qK2U2z9z431qQv7NNAWu53D+2fm+bgwaj7v0gaGmW4aTCL1l9wkSr7eTEK6Xgy8d6wJumgJGXT2199b2/iF9ffU05fV1MIHnG39um8HI/5FzVkzwEEifeTgqbDbnlI9//vLb+rId2sM/nPh31Fp7huvLkDxHY55wq9Pdu2b2sKbs0e5yBHPegs11ZgYKI8H6NpJIdCYds3NH8IUoEn0YHONuWrcndZ1ZnfOv+9sWUML/jegu+2t6/4y4nUI93jLjG85r9g6a6unjHwody8PI3pt4Wh87HJvGk4MVOCl8xyKYdgI42ED6OWOmEtO5bcT0lzBIZdEoYO59UhZgGSxD9TPeMTG+qLcU15dvpEmbA/+Y6U+kmxiY1ck5h6TXzD66YVSqOnPfW3wFurpt8tztaeMrUV6Z+4VVJ9PdPNJCRB3XokkMEAAK809RbqJbJmxXKARnXWzg87MN/K2GFmmI9ZfyCvW+6XAl3+gGnxW6g2llghbdNgbUR3gYAKXTxeW/9bSnOTr97GJDPcL0lBUkFAwEFdMuivSvmjDYQHgF6S7Ou9dk3J0nR7occygE9BbxpDEi9frVnbuZwCStUndq1pRgADII7bHOYVCmfUVR4FizDHlB627jrCOdk7AdF9e7uqLfwIgdkyyH/2EBCA9BbY2zUdUz4Jf7X99uUaQwuk3x5oEcE4wYix8SfA2pE+IgnZ4s69/q4GnEqlkAsQ044aeY8uTe6GQQ9n3P+Y7E/ZXa++rtHSPBN0xigHO9PUJa8B1RXQjtGsBkDwKGu2OUTqB2WFTS2G1FVbxotIZKu7tnr9kDjidxaSYv02rSBYoEWhKrpDyjr+omEEwYK+p+j7+d8vqY9P00BEDN/2Q+48kQcEcuQVw74utYnt10XF8U6mEsuv2VXOgTp63XYVRYGExT81XgapcYYW8wh9i52FxH08rhmQKckLFIYP+gA1fYb9t3enF2aded7/T9u/QI2z7pnq0a+mxssAWOzfqrwpjFgifDGEjbidCZ2+aF1Czgw02VYxm7W0Hgrhu+LjlqRB0DQmoBejPZiL6N8CZga1eupb74ikBS8N5PDSyKtv+NCyPljARbvkejbHDJUScv5R3UCIv4tAMiiTfhQuuTyZalpsGVjhxI8Jy1UddPjLRteLNXda/nC6UzMMZtImjgwxQRjgi6BQvT3AKDtfM/5iwKMDSm63/UVPNHYrp9ALMMeKnr9vKffPgXUKTwtE4MvsHmteo1FXB5ggYjvhYJK6eG45ETCE+mYiYh9LiIYSrXuXDGj8wLI+eO0DdR97f171elOrjc0Zs5PIIlETGPQHERDiwCAM6sz/qZn0vUQWSx5DyIacx05EazvLxR86Ltil1+7dWVGeAceVi8T2ZtH8FATcuBCXBR/WM95D/xSKKbZhB8UQW8ZmZBwggGPm0bQfpQrXMOWX6kF0bFTPFWuM1DRJ7ZftfHpuINmHEOV6RYFAFtHL6qTApgIOgGdOKrC9QzJy1M6lHsCCkI6c967/mmIPTeTborBczmwrvFuI+D6YySPMfNMY6V4T8KhAQg/LeWZ4/u68VasZmr6FRSHKIjXW56y7Ik811sS6J/vWPhQLo00XwgTQRnECs5RuMXnohxb5rGMiQDSggDAnNbN7wmKDJ/OA1PFGRpVBVQfOVWCAtoR05NEe2OUempCUtUomJoIosP5L267dsM302vHN7J8rhI+UNDjczPPqWIvhYx4/ezJkZFGAiiuiBp/+SqOwznmVAJ7BBjJOVXiHTHFOH40PcK/K35KllHakzs+wcMFUxKBO5J/YOvsBe8d2d1/AV2jvPAOCs3Yz5NA6lQ55GTA9hqGgqB6lRYXXo8J9iyTeHkxpOTPRrvvccWpYoahRA/JsAdhfAxfaUbA9RUeDl+wbwE6z7kTribkahuR7g4ilPXgSioUMlQwixc8tXySgl6hTsdk2hSqFBII9PRjs77ed6rt0iXLbA5ffMwPuWcoYRhQqdHkxSStkWF30ES6/JFbMkPx8scLbwC0BPqUZK9EAlCZdF1jKSvpNRwKTSNgcpFpGytUCBkGFE8BxwpBpxKnUptSNjs9OwzidSZpoUq1CF9jjKKRG5K3P96y4cXUppQ9F44zOz2gL86gArZPV+rDABXNTPFqFmAaGUpAtPirMbTFEKD6DAAcwqkXTkqYQaFf8blIKnYIHw/wxDQF7Ib8PdvnbnjsvF3iVGMGFSkfUkF/LKsxEL+CirK+jNXRJRQwqnG7wvzsRLJTHdrB265dv13zssk0BVRpGnX0LZAhGNbN0A4+FvMu7KtQQC+RHoEplz4TxeCeLmZRmgpTPGSkrKtQGOjzAICuifmyPcVecjL0d6gl1SclKQhE6c2gThmhqC/Uq4hzeuZmClAcLYboMlasIGgzE7SZKqX4qgRRCOMocKwZ45RjFWV8h3Zw96z1D7m+6GHbGJhqrJ9Axucib5vDttY9y1dkVmf8ed/AUTEWdhTTduojLp4jNFa6Fx812sAgbUCF9ICAmBYUPzjR3/mY9evHVLTq/WCkIC2ICvE/3PSTdD0yuHBm+E9ylaqlIAyikjWrAtAEA1XvyAU5FIrwcuKQKsVWu+XajT+SnPu2nZQoP4p0LHSxH3ISTA5nFKYW/rzabRTnr/RH0vJ8RRPQuLGToah6MzZZOi2xNYMWhYJ8Q/BhyUW9HHJVxR4iYtdX8Fxn/2zB3uULa1lGdP6Gf6phDx+h6nUpWqnWfwrIP400b78y8yzy/k+5IWAl+Kru1CtgODDMX05ph0X6wnb/VMOGNQa0UPW/JJMY7WIm1PqL7r+7ZeMXfW/+h7YpsFWBv7gv3dnmcFHf7u1/m6GMH2lvupCInsyI8BMVMyeKayJMqrnSOfFlQoQSEwxp8rQzVQpikQ/IsB8mOzZZcbwCwEa9BWebgw+3PrF8RWmz5YUV82OWT6ANleQZnw2JPCtzr1YC2UQKJqjwFOA0tkYX3f/mOffu9cO+s5o9NCMv9Wok7wVJe3frjjfPzi7Nugsq/RuhtqlZRTHm/iSFEgMKzTELH4YvQ+2W0CETVDENAE4no5ZBRtKaNsvnLPhkdDj/qG20Vbl/MJEUBGSoWZPhxpZd6anx2PZ53seHYxjn9n23J1CpThM/KxDQy17xvMS981U8JLn85WCrMmjRTuoUBv+OFPwQBVyd+2din3PeJO2s+nq/8dU/S9Wd9uHNswhcvljgKaQ6FT4+mXisJJ+YAOB5Jl84pCJDxU4eHRMgeAWIrgSOlRBPpwtLbUrZ7tnr9kjef8g2BtW7fybj+iLHDcGtF/upG6554PYEqFMw0SmgdnCptJ3WtDmzKWY8CxEFiUvJcoM6HXMhthIUhqBEz3IQRocIdIQslWHTlOOGf1xVcs2n+3ZKG6S3ztn4uejw8NpgSnWbqEsA0B3NO9MYvqF5dvK+lk2pRkwkBVxqXiVocYO4Ly1PPhNhZmTfjupVXG+g5Yp0xXo+FE/xY7Me7FPV5+J2qjKVoEgA0VfP2/GmKS/XAsRsW9Z3aAfXQ3/f9UX7TLLK+I/SOvK8M/X2tvqrLvrBou5ll2Uo41N6allAaVBl4c72Ba1Pr/rJwFMXP9H65MovLXlq1S2xQsTrU19OJRhVYp9TqS2OivV8It3P8V/wk2QJOtaAJoE0UqXATAmC4GoAKI15ne7435PpoZ/MubdffZSG10GyTJDqSoBEZF1v5Di0N/LU4McL97RfP5IGjkd5tYMz6Yxc37PiIk7SfabO3gzFLE4G71bFw60/W7Wx9cDK12RWZ/zIhrCO068EI+3boPmV6yPEmvcQwj4uhvTdxQaAse+b4LnBQg0WAMeWOZx28qK4h3br7Pt2yKD/XVNvGYZ8tb3+RLCur+CJabpJcHbRvlXvzi7NuppHuRVUcq/e0rdMvb0iOpKPNBJxvQWneVFOmGUw9OPWn61c27q/fXGGMh6dRSU4jZ4yQxkfK5nO14JgzEFbhZIhEid9qtGB+B+x7qxmVKtY9b/55Y5ppT20W67bsNYdLXzcTg6tEqru3CEm4wedqNN622i+1Hpw5VduKqaC1broFFImQxm/eG/7v9lJ4etdX+SKp4kxARYEcn0FHyuBTavhR1ufXPml+T0rZpV2BY/b45TFevF3b31nz6tAdI0Uyk09q1LIANHT22ct+SUDgIju8gNOy52cQVoa8MdN6Ojg2qdzT1EBKFaArddt+OvocOELwZQwUGhUgwIwvKrrK3huCN4VJWXzon0rV5VcdDnBlE4HWbR7xV/YKeF74sMmXrqHOF4YGSsBIrGctO8O6syWxU+u+sdFu5dddoLHmRAlKK1X84X8DaYpSKgXPzbSJ6HQgBS7QJ3CANBPw0+qk59TWKadi4gl70GGZy9cvXNGafjiZVUAZH1a02br7PXv8UcLG4IpiUC1egUAgYjIuCN5D8J0mzTfaX1y1frWg8vnlQST2pSyo+N06+b3BN1LuqOFPct/304K/4/rLTjS8guoS0bkjhS8Om2wSfsnnLDblhxc9ec37Lu9ubRJfCKfH5N5HTHFbGxZsg4A4zEA4LSmzYFZD+bB1F1p0E8VzjQFllh/Ewp62ZcfETTeIwseKti3ut7ohzUrQDEMaN6LG4iE68wKkH289eCqT7buefPF2aVZh854+jW9Nm26l3w+WtyzrN02BF/wuchDYKpuOGEyENXoaMFBcAk32k9427B18ZOr/mCk2/gUw0B2adanNqWsAr8peY9yizWIlGXIQ50+CgBcShNIJUtcsQMk7vwkvRMEPSO9c6P61Vyuf7nrj7LjUYDiHl92fQWvThKmwX4EQbht8cGVH7vhqfR0EDSzOuOX7G9fRvXBNzUShVOueZEEgYhg1alGRwoOhKtNvfn8wPRLHlvYsyIFgsabtceZdgLaf8WUBRzyTBnyZRdrUGBYhtwhP6VxBwBwtihAId7kByKtOOA/6ECE1I077rw0s/oMrTyl+ITtHQsfyrmB/jf5gfF5gBEXLdDoSN6D6HLTEPyNK7gnFu1ZsWXxnvatangjvCY0ElR77HlZJch7cb2R44AXsaUHr9u5/Ap0QseTEpYWa8BRu2mwVHGxRp2BEv10xyv/PZfWtGGszggAmvSLw0+I0/2cqDDg78SbSYlGF9o7gTO4/qTI2e9Y+FDuhWdefLMbiDYGUxOBKlzNMzsEIiajBVHXW3AgqjdJu4iTZqEWRNWpnpLgX+JxYF1/5Dhh6kxArwag6etq3yWQRcnl61uqGXsjJkD0wRIxxAC0dCgyQb7PdUbLD/gDcAJRfWfMwp3B9SdFBXh6aXZ4y9Xr2n1f9MVgSmhB8NUSQS9RAsDCq8qgExl0Air+N+HOC0adwloMjcvlFzmKgVddfKtJ2mv9ULxwZ0zDJ1jXXyh4Cr4XK05bjPaPtWLzeil4KqdBpZWnHPJrFu1ZNR/UqTiTRY0SaFoD6r5m3R9ER6OPc4M1FHANQyAvVYKRHf+n44oPmySJfD4S+hUAZIrNGLXiH3HyHgoYZdfnQYWTFur08R2zMz9DRwePpHoZigs1fTz0sOTc05Qov89VFd40BKyQ9wPQdBpn9iIo1sT589Zr1/21Hyy8hSwdLc4BnIVjXKocMIjomanPHn6uqMTVC187OIOM3LCv/VVsaXnF9bVKygGDmdaO5gZKmq2pTSlbTPnuMUlbNuUjwPiBSNng7XN33HlpvMgffKYVoHQixdZZG+/RPr1ZCvJIMDVhoVAVPWuGOJVIKMEK6JZRHUfVr6/t6mIQNBJ9v2kOkurLrK+NKV3regtDBcI6oLhdfZTwkY03OsJH8rWKg5MEEqfeNofNQcL+YWkZ8NnwYEul4O756/bg8FOvdf3R31GCyTQEXASDehYYPsU2xDH4qqUtrrjket7Tb58CQ+/xA+WPwVGCNw1W1eP7O2et/3lpu/pxwkexHr197sZtkpefxjt61FeyfmL6wPU/X3FRaVf/2aIA0A7uXtIdbZmx7qN+yLepky3BlNCSJTqjSqBQMsy+L8oFNvqPGDRXT5WXllwHQ4N/FDSHF0tUYWm1KkGVmOnzMVIcxQq+xJ0AYMP/AkPl1xwQSCL1tjm4yPfjI+iEnFUTM3FDI6U2pey2lg1ZHH7qJpdzHwHTC8GU0MKcGSVQqLOTAlLFdx6dcd+v0lrDIssOcLYrK/MPtE+D4T+uePiVqph6y67f7UX/xQ9BQaN3FR1/0kZxlXdQZ9b73sLTnDCMMhsyR47wCvmPFuxeflXpGJCzCVmVpni6l3RHW66+5x9cNLzQ5dynYNAXTAntSFagpx8TqKg3dTbw/dELAeMvSodUV231bSlGJ8RE+pe2OZgikUjZw69AwnWGQLire8nnoxONk08ETamulHnkyswQiD5rkpbKHrpAIPWinLRJVvr7sY7wOtNXZnXGl7zA9mu/++yWq+/5sM/7BT7n/hbQX9jJCcP1llVViunhhHsDVXWm3hoAgzrsVz42a/3PsaYD1W4TGTmFbM+q+Vxn3ut6C1LhPKLiMTiFX3G//erJDsR4yYtL1u8H5YvuaP4QB2zKWj+RcX0Fb5uDtyza0357afLm7EuvYi8ABaU1bbbP2fhU99X3fGxoyMzzOfdBdbrdNAYcH0qAOCRgArxBUaHs5IRV6H+7/ui2Lddt/FHs7mtYI1OK1SR3ccBWYxKLyqTjYhosqehnu5dkek92IAafLGVKdaXM9kUbj4rin0xjBetHcVzaqQJ6V+vB9KRi5e3snJcrpoToAKc2pWzP3MzhLTPuuevqbm5FwS2XvP8uDHwwObQUGlZRUcChxgXRqioq6rnOsm0OjQz6dfJidNO2eRt/UusZw6WzChftWvHBYHJ4q+uPfLneCwDCARt3tPC8i6K7xjoGZ+yzdNeAbnnbsoZhY/ZwaC6T4bL0YbwP76KE9S8O/2H37A3/es7syVFQqiumt0t/tejgqvnMeId6TZs6cxUMQYY8tCB6bIBUafRUTLwMgUrTL5aTFhQwZNDtFpG/2nrN+m+V3HcxDFVN6IA6ZeGBlTONoa0qWoeofHWxdJZu4ejwn267duM/jiWLiqdoL9q94t22Kfiiy0VSdssTVDhhWYfkN7vnrNtU802eBT4hrekYgBXd8fxttzWEzY2vU6WVECyFpSvjw6cU8BrPMpScLxPIEkAEPxAJCI8w6EvPP/PCN55emh2OgXBnbcfJK6i0+ezAfvewTdob3UAFq1cVrrPkC/5niTo795ErWvJjfS6V1Th0auveldeq0ScgIJRb0hgyScE/xwNuRveS+wfP6aPNtYNTXV082lpan31zUgr1C0j9Dep1IYGmQ+QigOoAFIjpsBKeJMLjgMl2z8jsPNGQav0arZtbg+4l3dHi3Ss+ZacmPlTNMfaq8fG20dEovW3O+u+U++wxhT9ybvveFR8MJiX+OeotOMLJP1gBZ5sC6/uidVtmr1813ps9G0NCyfLGup8x77X42gzGtxG09PwX72r/bTM1+Hc3EFVsH1NVb5tD43oLD22ds+G3KslhzDcrdekQ6PUqGjNFZUraxARlfQiYmD19Zw04RPHhKSidSXOpZT3b1iagThl5uB3gEsU98juMzwBKp4q3PrHsRjTwl/yQ81SpfSxekQsZcsMa8AegoEocwtiAj6Atu9KNdSY6SAkzTQuiZUCGgiEqOm/rtRt2l0AKLoRrgsNbyVrn7Vh5ddiIh0H0ivg4+fLlZVW4YGpo3YvDH9kyZ+M/VON9T/qGI4cuhVErJ+00LfixmSTVmEVy2HfN9mDfuA9hOIe9w0QLvuXRO14RNNJ/UGBeIXnvKws+XkAdHSn8eMvsDf9YfJ+KMjjpm5Z6w4zidVxXoaO3eKYdMbou+I1YEyD4hVtuv6T+orrvmYSZ5Qcq5vMjx9hL3vcb0LtApEV3r+MSfnZNtrgXF6+TQqUtziNHdzz0axGO7yqROK2b219pJid/wEk73/VHjpgqGlLxxDP2w/69m69d92QtzOFLAV8HGJ2QRbuXXaaghTrsQUonPwlLoWzY+IEoZ6L8T0fo4V9fNaP6hVveNBNT+H4OeVY8CoaK08Qq6oKL6mzhheF/3tay4ZtFJaqaWHuJ5ZcQK5N5jW0KklJ2/EeF6g1UseXxuQ88F/eGQX8t0iqZxaLgW3etuNVMrfsvsjzL9UW+KsErnJ0cWnck37VtzoI/Tmva1Gp4Y9O1jNdXHv+Je8MI+kMASK3p4l9LtToSCYjnDxfvXfk7aDQ/gOIVPhf5qly9qjdJY2XQHSSN0kCnZtZktFbDe2lVrzT+I9JW7fgPAT8czQ2Mi0x5mRcanMn4DuqUlkxLsPjAyv9rmuz/04ImiulcZcGLCieMUa+HZVDu7J59/wvpTJpLrVm1JSonaiR1yvX7V13nSXfAK5VhAYUCZin457jJzei+vCZKl6AdlOrq4mlt03R0PprWtBkvK3ZWX2vTBsXzfxbubF9gGulzJhncGB3N+yKmoiosXjhggqFhN6y3bZ+97uFTYVOPiy0pdHEWECd+aTApwWONIhczATH1hjWSh7svv3+w4pfo6OBSy3A8Edup2WKZ9Jp9tycm1Tcs9Hk9nKHM/rK06bl2dXRwek0PlcrIi/e3f5gsd5LlZMzVk62m+K0SC54MOT9QWLl97n0PF/cBjbtyepxgX0LpQqlc7YfiNr+TU7pFbvtQ1yHKLs16dHZKtuSatINveLpnthN5DVSXQvQmeEw3jIHWJ1d9Mtrf96kMZXIj/Pi5qAQjpeJOl+kEWg+sfA0YnzTJ4BbXF0HykSeiqvYDqahwyEyGIhn0K7fOve/BElg8NX7qBJrylt3LmoaJD1BYHaVLnuZ2z163J61pg0y8riXb1SboPD7XvOGp9HTv5CZAlyr0FgjmmOaAoYAMe+iwV1gm2xzA59weIurcPP073xqtSOdEONAOTqNnpFFyyd6VV2tAHwPwexQwFYmbqqd9VdVzwhgiGvQDftXWuesnRPDHCb/kZlt3L2+jhmBTPKc21rivCtdb9sNuTz4KFlx3HfyJ1nnjc2+7NBqOrofDUqj+BlTnmcagDgxoXmKBxx0yxQFDYihUAW/qjKWA4fP+YfL+77tnbtg4GjCVCidnk5XHynmsF2DJ/uVXwNoPiur7TDJo9kcLqqpaoe/uRME7kwysenkROb+ie+6GH0+U4I9z+yW3rYZfZxIGftDLWOu7i12h7Ifcd3vmZgo9ABb8bPlk68wisKZUKVXoyy+ySTuJ6hhS8JBhgesreBApqXLxuG57nA4WByVl2IkOQ01D8BqE/JrWgyt/quC7qG54ffby+weB7DG3eqYU4YRyb6mCt+RA+1w19Acq9E6ut5PRH8EdyXtiMmOeUD6G4O2k0Mqw3+dzUfu2uff2lKp9E3ULNBqYoLNTFu9p/wk32Jv9YOTH6tzR+EBD8jn3l8zUp5bfAJFWDs2lnDAQJ9BhD3EiIJI4XaSap1212E5tGgImQ5AhdxDAN1jo24/PvOeJE1OoaW3TNLOmRdHZWRW3XauwgWKG8vw0Hd2l1LIr3Ziok98i0ncR8EZuCAIZiCBOHQGmpvsWVWVIMCVh3ID7gRzNvX3b4gefPx1tcXQcpfv0ssswzAeYuV69avlJEIAsgRst4ItxO/KiIAEpVZu+1KAEygljTL2FG4gcCD8movUg/X739HV7TnxNWtPmEA4RuoBpz0/TeAp21DDk8djh2J/WgLCmA2n0UGmM6mTe5ZbnlzXl+4ObAV0O0TdxvX01QPADERRwpDUKPQZ2ngM2nLRwOffprf++7sPohJyuljgaiaNLs27xnhWrTXP4bddX8NUQDkUdcFAlwsQJu+zjURIiWG6wIMvw/ZEDsI1Is8T4L1W7rXtG5pmJ/uSWXenG+kY3Sz1uIqU2hd7CobmcQgMZcpBhLyBosfxKNXoVVcDbpsBKwR/xBf+Bbddu+GapkXY8BE7NqZ4yvR6VKN2Xao8FvVyNO8REYADqc8VDYwmW68wSDs0SdfJhGXRDi/evPEjQ3QL0MNM+RPKMt/wriDlq+oZyV7fW508EqC270mFjGNbl6wpNHMlFELmMFFcDmKNELaRuNjxdZhoCQBQy7CGDTnQoxka1ALkT0TwZNkFzYH0u+qEMRu/bNve+A8X47gGcNjxDo2Nm3yunPmHqzSwpv+Xh7MuoVTVeHatEzIZDBgUMMgQVjbOLvPMKDBAoB2AQ0PyxPgUNCFQH1QYlNDJzPdUbxHtsEW8iKQi0IFCaIE+ncXOcbQ6N5N0ABB3dM9Z9arQnPu2mVKJ0W3cvn6eB2VaB0p1YkUGlhP7jDfFkJuJ9gaIylA5qL27aIKa4xZqL3BUdi10qCkjppxYnlIseUJWKSP3UDUJVFeRNvbFkCVKQe5HP/1n37Pv3lEDly5W92BKlK8xtQWNQltI9ZWGTCpSUoEyBYVNvDAhQpyAGfL+DQv0pKQGBYu4RxR4EOvb5TgFS1ZOt6yEcO6CAQMdlOhMR1opC59BYk7RWBqMn4NDRPWPdPSPWTll3HCg93cIvbWwmxW3VULrjs0BltsymzhqyDMl7SME/J4O6WUV/KCr7GPwerjPLKWTj+6JYCSYWRJbeaewu5NPh74runUNjbIO1fjB6xg9Gn2p6+sXPZUeGOYAsdb7s000EALfsXtY0BH6SE+biCpRudbFXlciw4QSDwzjvlyF/lEi3KihLhrIQ3to9I9M7+g0WH1z1G2D9MAmWcdKSDDiIV0dQPocwyEhII4A4aZlCAxl0TwH6r4WB/Bd2zv/ukdGs6pn6mgQAC3Yvbwsaw02Si2oEeiol0ESA5ToDSjAggM9FQwDtItL/grWbrDebH53xrV+dyIOPlHVHU6MH268XovcRsIqTwSTNe8iQVyX4IjvIZ6vAAYAtG26w0EihkWwB0eeNG/jmY7Me7DtGUWf9ma5TEAAs2r3ir4KLEh+PDleI98eBNFhKMLjOAAT4vkjA2E3ED5PKJmPkkUenb3zquNcXy7ox6XKSIs3atEH6eH5cAvtWeH07MS3iehszh8NeFfATTSaNy6WP8nQmaQAmSM4dBdMDAO7uvvqeh0ps49ki9OOFv7f9R7bB3upyJ1C6JwFpXGcAQ5Ccg4r8DKBHiPk/vaefbJvZsucEpEqpTSkTW3YNFbkTKmNAXBJVppUkuAMGs029HaGR1UnRAynFgIUmfnFi6VmMmsSlsKj8AHzO9RPhYRi+R4YKD2ydc+8vRrONZ2NFkpbsX36FeOohy43qVaCK0SCN6+J8twTSQHicDG9ixo+S0rgrO/3u4TIcu5ziA3/J+HTr5tZAL7qylTxuU9BvQrGQ68xkDhnqNc7FI4F6LbrhUYPUqnEuoGVOnlLEJNfI6gMlAhmyDCryB1CFDDiAcEAJP4Hh79uCzz42a/3PRwscmZGtIGflRYv3tf82J/irfsgLW7aUMOCQoU4gg65XibYC6BoLpKU1bQ51HaIi/z3xBZXS56xNm0OXHKITyY8bD955qaNwAaA3QKhVVeao4gqTMElKMEqbxFXjPB6ixePjT/iaMW0T8wBUrGdqnIb6QQdAn1eigwTdQUSPSUiP53t5T8/cTOFEDHM2ufbywt/Tfk8wrW6l6ytAI+0HYRdIf0RK2bBBN//0lesPnQykxTt3O/Vlv8lR1bVsV1ZO5L07tIPvfWbvK0jyV7LHVR50JYtcruBpqjqVgCYAdYCGpbxeoUJEwwByIOqF4kUl/JKBn4PxtLfmqbAx/PljF3+972QFJADIrMno6eLgT5vwF+1d0W0SvBeC/4DBw5tfve7JlwgbXTwNNcbtl1EZ0kjzIRyiaTi+GbSyEh3jOWsJQ2iLFxefEeWfwOv/A2mqzRn0sTWMAAAAAElFTkSuQmCC"
DISCORD_ICON_B64 = "iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAYAAADDPmHLAAAa5klEQVR42u19e5RcxXnn76u693b39PQ8NaPHSELojQAZEBJCII15Yx7Ow4yA4NgxYEiyf9gne5zddXZ3IJvYeZw9jnP2rBMZvCbBjqLGThxsjI0dEASQQcYYJPEWIAGSZsRoHv2891Z9+0f17e6RRmJ6dHumZ6ZLZyTNzO1769b3q9/3qO+rIkyw9fSwBIBkklTws7vu4oaUzJyltHsuMZYy64Ug7iSNViY0A9wIphjAEYBsgCWDBMAAoDGbGhETMzGBiclnsEegHAtkiCnF4GGABgjcJ4R4DxBvkeS9drrl1QceoNxoOSSRTG5VE+pGpR/o7WUBAPfeSxoAbrm9fwEc5ypofS2DNxD4DCljkkgCYPOHNcAazBoMBpiLvzOyB4gw6xoDoKIICEQEQJh/SYBIFH5PYFbw/awPorcJYhcLPGpD/uzBv0v0jSWXKgCAqacHIpjxN985sFla1t3MfINlNzYDGsrPQSkXzFoTFUULZibzgkzlUwD1Vj6+HKAimBZEVJwhzCAiIaSMQFpRAIDvpQdA9G/K57/fcV/zroARylk5FAD09rIozfhj55Mt7yGiT0oZheelwNpTIAIzERFEXZjVhAkXJxeRJW2nEb6XBZgfUr5/745vte8BmHp7QeNhg48EQIConp4djmy7ppeIviRl1Hbzg0yABpGoz+YpZA1mDZBwIi3kq2wO7H/VH7j7z5PJpBoPG5xScN29bO28l/zfvPO9lTHZ/IDtxDfmcwMAawUSsi6AWsKCViApI9FWeO7Ik5ncwGd/8MCZ73R3P27t3HmZXzEAAuFvvf3QZZaT2CGEPcd1h3wikvUZX8MKglk5kRZLqfwhNzd00/e+vfCZQJbjBkDwgZ7bD11vO43fZ7Cj/Vx91k8fGPiWFbUYyLhu+pPf+9a8n5+MCehkOv/mOw59XNqJn2j2ba1cJhJ14256wUAJ4UgimfH91BU77pu3ayybQBxv7SeTpHruOLKcrPj3mbVTF/60jTRJrV0FcIMQ0R/c/AcfLkomSQXxgjEYwPj5WAMpDg08a1vxCzx3uE77018f+Hak2XLzw8/wUPsWAEgmoQHiUQwQBHnovf4/jUTaLnDdYb8u/JlABGS5+UE/GmvfJJqO/Ilx6UtyJyP8HTKZ3Kpu+YOBtcTWC6w9MOu6fz+DaIBIaiKpfMLa5N81vR4EigQArFnTY1ZjPP+vpYxK1gp14c8sGmDtw7IaHPj5vwCI9+1LEgBQYBneetfApSQjT/leRhNR3eibkUQALSyH2M1v2P6tjt09PSyLgtZafVEIBzTblmVnFwK0ZTWQFvSFUTbALbf3L2Ap3iSiGLPiOv3PZFvAImY9LCi37J+2dR0VAKAtcZ0TbYmxVn5d+DPdFvCUE2luUtq5puQGsr4OrLku+9nQhMnGEXSdMQL/8EijcMWrwop0aT/PhbSUepuxSoC1ZcWEUtm3o25qjUDOOpeEWKCVi7rwZ4MSIKFUHgAtzsv4WYKkXiutBgKg6sMzW1hAa8tqkIrEWiFAy0wWF9dHZtawAJhIQhIvE5rRxXXhz0YeAANdgsCdJmUbdf0/ezjAJOUTdQoAbcz14N8s8wTI1GpwmwChiVmDUGeA2cQAYA0Qmi0wxQE9LcVPVPgCoDlAd/WfCQCCjNnMXP1nhv8OBQYAGi0QYjxN3qBc4EoDrstwPSOAiAMIQbCs6gmECPB9QGtG3jUgsG3AtglSTDdAMMAUs5g5SqRRi2sABCAoO9Ea8DwjdAbQECN0zZdYusTCyqU2li2x8P0fZbBrt4t4nKBDNmuEAFJpRvfFEdxwdRRv7Pfxxn4f+9/1caRfI5VmCAE4NsG2zfXMCL0fIdoBIHDEIkGylhAbzHIAUArIZxi+AiIRYG6HxIqlFs5eZWPVchtd8yWssqS1226K44WXvaoMutYGdLfd1IB5nRKrltsAAM8H3v/Ax6tv+tj7moc33/bR16+QdwHLAiIOQcqSeqqNsSYquIGOBWarFroU5B17PpDPM5iBlmaB1SssrF3j4JzVNpYslog4dIJgAupd3CWxZWMEP30ih+YEFe0CCv7ijw53jXWtEMDQMOPGq6OY1ynh+wVmAmBbwJLFFpYstnDt5VHk8oy3D/jY84qHl/d52H/Ax9CwWWeLRoyKCvo95SoAEHTz54/yVAteayCbM0Kf0yZw9iob6z7m4OzVNjraxZgCFzRaaWltvj/4gcIf/Y9BMDOUNixSPusEldTKaEo0hrHm0fNEStNH2yL8zZ+3YF6HLD6/+OzCzKYyIAet76jGnlc8/PLXLva97uHogIYQQCxKxXefslgAa8+aauGnM4xohHDRBQ4uvSiCj51jozkhRglGFwaX6MQBLr9XwAKbL3aw5xUPC+dLtDQLtDQLNCcEGuOEWIwQiRBsi4ogYAY8j5F3GZksI5VmDA9rHBvSGBzWOPi+woXnOZjfKaH1iX0QhKIXFdB8AJLOOQKXb47g8s0RDA5pvLjHw3/8Io+X9nnI5hjxBpoyEBARTRkDCAFksoxLNkSw9ZMxnLHIGjWbj7cHKiE25RsNZ1vh2LWexyBhLP2K+1Om98uBs/9dH9v/JYPnf+UiFpt8EBARmLUrpkz4GcY5q2186T8lcMYiC1oX6J3N78UEk9IJxviyLSpa4YEqUIVnnOrr+GuZjZtnTbAkNmCtcq9Aa2DpGRb+2xeasGKphWyWMRW1V8zgKQGA1kAkQrj7M41Fa/90hH4yJggGXwqjy6UoPedkX8dfSxTeOmk5GJQy3//+ZxshJWGqovGTDgApgZEU46YbY1jUJaE0im5S2DGEWrxX+TgoDSxdYuE3PxHDSJqrMg41BQBBxtpffqaF3/hEzBhUs3gFQpAxcG+6MYbFCyVy+ckPx00uA5Chvs/e0gDHpiItztZGhXhDNEr4zNa4MTZnKgCCUOqm9Q4uONcZ052alSxQiAVsXOdg/XkO0pnJNQgn7VFam+DHbZ+K16V+knbbTQ1F72VGAUBKM/uv6o5i4QJZn/0nYYGlZ1i47NIIUmmGFDMIAL4PtLYI/Nb1MRMyraeejGkPcMEgbIwTfDVDACClCfde8/Eo2ltFHQAfAYC5HRJXbIkinZkct7CqACCYNfy2VoHrr4rWhT9OEHzymiiaEwTfn+YAENLE+y+/NILWlvrsHy8AOudIbNkUMSwgpjEAfB9oShA+cUV01vv8FdEmgOuvjCHeQFB6mgJAFlb7Nq6LYG6HLK7X19tHCKSQ/rZwgcSF5znIVHmhqGq31mwSJq+5LFqX6gTbtZdHq+4uV+X2QpiY/1krbKxcbhWXeOtt/OPHDJy9yjbLxTmu2ppJVcRCBf1/2SWRUTn79VYZgwoBdG+KwPNMGtu0AACRSezsaBfYsM4p6rV6q9wWAIBNF0bQ2lI9l1BUo+PZHOP8cx00NYq68XcaE0lrE0NZu8YxakBMAwBwQYddssGpSzGEsQQKY8nTgAGIANcD5nVKnL3Krvv+IamBtWscdMwR8Kqwh5sIu8P5POPcs2xEo1Q1+i9P9iwmc6rJybEvf1Z5H6qxhBuogcY4Yc0qG/k81zYAgkTMdR9zRlFY2AIoT/YsJnPKkvtUDWEE9y1/VnkfAmFVSw2sW+sUi0/CbKEVhgSuX2uzwFkrrapY/4FrlMszdr/o4pXXfQwMKtgWYcE8ifPPdbBquVUESlhGU/m99r3u4cU9Hg4dUVAKaGsRWLPKxoUfs+E4FHquQzCGZ6+20VRYIAqTBcIDgADcHOPsVTZamsJf+AkG9qldeXz3exm8d0iNsjG0Bnb8WwYXrHVwx+/EMX9uOIknwT3e+0Dh/u+k8eJeF55Xuq/WwA8ezWJxl8Sne+K4+MJw092CBaKOdoEzFll45TUv1EKScBlAAWtWlWZgWOvZwYBu/5cMHnwog2jELDKVF3AGA/WLX7p4/S0ff/LFJqxabhnWoNNjnD2vevjq14cxkmI0xgkNsZKaCYpJD/cpfOVvhvG5W+P47etjoTOQlMCaFTZe2uuhoRZtAGZTKbt6RbjWfzCQP3k8h3/YkUZTguDYdEKlT2CYNScIqbTGV74+jL6jyhT68gSFT8D7hxW++vVh5PKMRCONaQQqZQpdEo2E+7+TxuNP50Mt/AzGcvVKC1KGa1uJsDroK1POfcZCGRoAmI1qOdyn8O3taSQaRbFY9GTNV0BDlDBwTOP+76QnXNkTAOeb/5jGSMoUsCp1aqAyA/EGwre+m8aHx7Q5F53DA8CZiyw0NYpT9mPKAOB5jPlzJZoS4en/wOp9+Cc5jKR43Nu/+ApINBKee8HFq296xSXWSj2Nl1/x8KuXXSTiNK5B58IK6MAxjR89lg1tHSQYy7ZWgbmdItT6gXAAUBj0MxbJIn2GIfxgVfH5F13EopUZPsGaxFO73Am7pE8+m6+YxoP091+84MLzwsvoCUC5uMsKNSAUqg2wZFF42w0EM/3A+wpHB1TFmz9pDTg28MZ+H8yoSBCB/n7rHR+2XRmgAxbo69dFTyUMWyB49yWLZKgeVigACJI/Fs6XJcs4pABIX78yy6ETuKmUhMEhhVyeRw3ieAY6ndEYGtawZOUPJgLyLqP/aHiRoeD9Fy6QxhDkWgJAYQOlzjnhGYBBC7aOmegtlcKEllI9H6dlbDGjBLwQATC3Q1asDqsKACoUfDYnBJqbCaFRQKHFojRhS54ZsCyzbVulzbFPb89BIlP0GfJwoLVFIFHwBMKYaKEAwFeM1hYBx6bQ4tXBPeZ2SDh25YIgAEox2lsFohEat94Mrok3EFpbBJSqHAGaTVwgYMQwVUBDjNDSRPAV1wYDBCqgtUWMW8+O64ULPVu8UGJOu6zY8iVhaLy4NlBBv1TB4l6x1FjclUT0Apd4XodA1zxRNCpDMYx1iQV0zTDA8QAIC/GF+wY7iOUqzIhhbbaPvfSiSMU0HFy75eJIxQaXFEAuB2y8MALbDjevP7hVS7MILc9ShCIpAC1NAqEiAKX9eW68JobmJho3C1gSGE4xNq2PYNkSq+K4vBCGMc5aYWPD+Y4JQo2DzQUBeQ+Y0y5w3ZWmFC7UFVEujXVYrqAIq1OJxvANQCrsat7RLnD778SRSvEp9woMhJ/OMuZ2CPzerfEJD1QQCr7jtjjaWgQyuVODQBQ2i85mGZ//dBytzdUrhSuOdS0wQJAEEo+Fb/GWB2Wu2BzFHbfFMZJi5PMmwnZCQggBQyOMlmaBL3+xCe2tYsJuacA+czskvvzFJjQ2CAyN8JgJIbKQo5DOMO7+TByXXhSpzh4IRQM1PLa1whJS4PJUowUg+NQNMXTNl3gwmcaB91RxuTYwRCMOsGm9gztui6NzzunnAwRrCKuWW/jL/9mM+7+TxgsvuXCPywcQwkRBP7O1AReeV73tb4IRjkZLjDP1ACjouWDTp2qDYOM6BxestfHLFz3se93DwKCGZQFdhYygFUtLVn8YQgieO3+uxH//oya89qaPX73s4tARBV8B7a2lvY0tC5Oy+4ljU2iqxQpB/iBCcRfsyQCBYxMuXu/g4vXOmMGfYPaG+dzgvquWW0XXcix3eDJK4Mr3Oa4JFWAMs8nJ/y5P/OQyWgwMrmoJoDz1rNy4C/pAYvLqH4UsvHet2AATNbRO51lTVW9QC0WuYb56KK9j8vTrFaCT1YIzE1ArkUBmhJqmVG+nbr7iGloOLux363p1wUxWC05KqykVkM9Xpy5rOp7LV61+B7cLzlSiWgBAoAIyudGdDEvXBQaf1tNjownNJ/Y77JbJhqcCTt8LKBRkpFLadIrDm0FCAP1HzSFL7W1i1MyaSk/gZLOdqHR+UHm/Q1sTKDwnldahuQNWWAOQK1SuhlERFAzYzmfy+Pt/SCEaJVyyIYKPbzKre+WHPQUFHJMNhuOfHTz/jf0+nng6j6efz8P3gT/8XByb1kdCAYFmQFIpTa4m4gBBGvQzz7u4ZEMEXfPlKAo8nTY4pCEIONKn8P0fZvDYEzmsWm5h0/oILljroKNdQNKJ7hGFHCs4PvAUVAMHzz7Sr/HLX7t4dncer7/lF2doW6vE4BCH83yYyqt3D/p47leFNPkwag7CODWMCvsCNMQIn70ljqu6zdZwSmPCefHBjDk2pPHwo1k8/nQe/R/qYrSvuUlg5TILF5xrDpVcuECOGaQp+sxjMSZhTMOl/Pqx7qkUcPADczjkCy97eGO/j+ERDV1whTs6BK7YHMWNV0dPu1CmfAx//HNTHue6DMc5vW3liQha63xox8YJYbJvsznG5o0RfO7WODraxai6+omwS/C5/g81HnsihyefzeNQnyoOqO+b/L0F8ySWn2lh5TILSxZZmNcpQ1s3Hx7RONyn8fYBc17wm2/7+OCwQiZrqpUCxCyYJ9G9KYKruqNoKyxFT3R9oJxFD/eZyuRnd7uIN5ijaE/XuAwdAAETCAJG0oy2FoFbf7uhuFHkRNXC8QBKpRnP7s7j50/msf9dH45tqpJcj+EVYhERh9DcRJjTJtA5R6JjjkB7q0RzE6ExLhCLlg54DvrmeQa8qbQ5LPLDAY3+oxp9RxWODmgMjWjkC2f62HYha1gan3z5UgtXboli4zoHDYW8iDDelxl45GdZbP/XLIaGNRJxKp5SevqyMgAIdQ2PGVAMJOKETJbxf+5P4enn8vj0TXGsXGYVKa0Soy0YxGBgGuOEq7qjOHREYc+rXvFwyIhDiEZQrMcbTjEGBn288oZfnC2CzEKKSeSgUSpAaYYuVByXH/QopUktd2xC1Bl9RDyzccnOO9vG5ZdGRgm+0llfLngisxHFg8kMXtrnoSFGpj6xCi5l1U4ODdggnWHYNuGKLRF86oaG4lnAE5khwRGyHxxW+M+9g6fUrUSl2v3jVX1gQZfvLTDq37LrAzCMNeuIDGBsC/ja/2pBR3vlhTHHM9zhPoWHHs7i8adzUArFo2XDDioFDCDPWffH91TTVXIcs3a99zUfT+3KI59nLFooiwUf5cfEjjc2cP9303j9LR+x6KkNoeNn60dF5yq5tuhGSZOG5vnAhvOdcRt8x8/4Y0Ma33s4i//77RT2vmbezbGrd6SsOTqWVdXTOIIXaEoQsjnGgw9l8LMnc7jmsiiu6o6ipVmMuu5k1BkYU3te9fDkM/mqUeJErPREI+HnT+Zx5ZYoVi47dRZy+XsSAR8e0/jpEzk89kQOfUc14g2EpkYapYqq2Sb18OhAN7quMbg65wh0b4rgyi1RdM2Xowa13E4IZovWwB//6SDeOeAXt6GrhRYchL16hY2vfLn5BCAHQaNyl/jg+wqP7TRezdEBjViM4NjV23JuUryAioHgmTTqpgRh3VoHl2+OYO0apxhJLAqeDdXu+EEGD+zIoKWJam75WUpgaJjx+d+N4zeujcEvVBSRKNkUvg+8uNfFvz+VxwsvuUilTezEnkTBjwIA6xxtvbNfEYWVYzoxIATxAyGAMxdbuHi9g43rIljcVWKFN9/28V//bGhcBRpTvS7wV70txa1yAOCdgz527Xbx7G4X7x70odnU+MlCAcpUrHYSCbDWKdp6Z39eCOHwFK+5BpSZyzNc17h7y8+0cNE6B6uX2/jGt1N4+4Afaml0tVTBquU27rwtjr2veXjuBRdvveMjnWFEHFM0Wm4LTBVMiSzSWh2jrXceHRZCJJhrY1QDVlDKgEEp4+MHmce1nhtAZBiN2WwSIaWpbwyid7XRf2YhbNLKP2wRkAWJBFjVxJle5ellDTEatdHSdEgMMXsSlCKSjNI2djXGVwBx1gJxmiBQi2Nbq1Q/HhAAJipam/0jJiICIyUADJugAOppvbOmcbABw5Bg0DEiWR+TWdSIiIkEGDQgiLkfJEBUZ4DZxAAEAQL6BAjvE+rHesxOKuD3BUBv1dX/LJv/DGJWYKb9Aqx/rfwsV+9kunqrPRtACN/PKKH1y0KLkZdY+0eEcGh6lmDUW4WzX0sZAcAHIqrjFZHctmwIRM9LKwaUNqKqt5k6+wEtrSgDtOuBByhX2ESHfyyEBOrGwCxo2mTNk3gECErDpPhhPjeYJ4OCOghmsAIgYUs3PzSiPOsnACB6elj+8zfaD4L9n1p2AmCqF3rPXPdf2U4CzPxw8v819ff0cClHhYX9NWZFIK4HBWas/CGUn4MQ+NvgZyKZJNXby2LHttbHPXfkCdtpkgDXWWDGSV8rJ9IifD/7o+3b5vyip2eHTCbJJIXu22dCgUI4X1LKfc6sDnL5Hkz1Nu11vwXlZ3wB8V8ApjVrjMEvACCZJNXTw3L7tpbdysv8rRNtk8y6zgIzRvysnEirVCr3l9vva9/b0wNx772kMXqGM/X0QGAhHJk69py0Y+f47ogCifpS4TSnfttplq6X2t1CbZuOHYNOJqEBKjFAIUTAa9aAk1+jrPJzN2vtjwjpCGZdDw5N25mvtbCiUqn8gOXLm7dtI89QPxVd/RN0fE8Py2SSVM8dH1xrO00/1MoVWvtsMofrbVoJXzokSHqeGrk6+c0FOwPZll83ppHX3cvWznvJ77nzg5tsu2k7ayW1ytfVwTSifWFFJYFc3xv5rR33L3iku5utnTvphOOzxpzVO+8lv7uXreR9Cx5SfuZGIjlkOQnJrP366Nb8zPeNKy8+9PzUJ3bcv+CR7t6xhX9SAJSD4J+/2fljLzewWSvvxWhsTiExux4nqEHRKwAcjc2xtPae893BS5L3zf/3gM1P9qmP9PMDvXHDDbsbEgtW/hkIX5AyIlx3WBfSyETt7Nc1++Y7AM0MciJNQvl5n8H/e/Ddp3sfffS6/Fg6v2IAAEBvLxf9xlvv+vBikH2PENbVJCz4XgqslQ8CgSFAdTBU26kHQYMZJCxp2Y3Q2oNm9YhW+Xt2fLPj+eNldtoAKI8TBIi65e7Bqwj0+wy+1rYTDcwaSmWhlQtmDtgBzGX7NBSfVgfJyQY5qNEM0vRL48hEJISQDqSMgUjA80bSAvIRML7x3W2Jx0uMXfLzQwRAiQ3uuQdMZB5w893HlgjIaxj6Wma+kMALpRVDIZwMsAazBkMX/m+25+Awd5WcCY3IzBASJmOXzJf5nsBgKD8DgA4yaLeEeNSD/2hyW9uBQC4AMJ5Zf1oAKLcNABNGLv7sLm62aeQczfpcQC9jRhdDdxKhFYwmMMVBHAMQAcMCsVVnAxjGBHlMnAcoS0AawDCYjhFRH5F4D8BbROIlj/09yW1tQ6eSQyXt/wOnHxFH2EAHAQAAAABJRU5ErkJggg=="
TWITCH_ICON_B64 = "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"
VK_ICON_B64 = "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"
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
        "twitch": TWITCH_ICON_B64,
        "vk": VK_ICON_B64,
        "all": MENU_ICON_B64,
        "menu": MENU_ICON_B64,
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
.brand-white{color:#FFFFFF;}
.brand-navy{color:#1E3A8A;}
.brand-orange{color:var(--or);}
.hamburger{width:42px;height:42px;display:inline-flex;flex-direction:column;justify-content:center;align-items:center;gap:5px;background:none;border:none;cursor:pointer;padding:10px;color:var(--or);}
.hamburger span{display:block;width:22px;height:2.5px;background:var(--or);border-radius:2px;transition:transform .3s cubic-bezier(.4,0,.2,1),opacity .2s ease;transform-origin:center;}
.hamburger.active span:nth-child(1){transform:translateY(7.5px) rotate(45deg);}
.hamburger.active span:nth-child(2){opacity:0;}
.hamburger.active span:nth-child(3){transform:translateY(-7.5px) rotate(-45deg);}
.topuser{display:flex;align-items:center;gap:12px;font-size:14.5px;color:var(--mu);}
.bal-badge{background:linear-gradient(135deg,rgba(59,130,246,.22),rgba(37,99,235,.1));border:1px solid var(--ln);color:#93C5FD;font-weight:700;padding:8px 16px;border-radius:999px;font-size:14.5px;box-shadow:0 0 18px rgba(59,130,246,.12) inset;}

/* SIDEBAR */
.sb-ov{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:52;}
.sb-ov.open{display:block;}
.sidebar{position:fixed;top:0;left:-300px;width:280px;height:100%;background:rgba(255,255,255,.98);backdrop-filter:blur(20px) saturate(140%);-webkit-backdrop-filter:blur(20px) saturate(140%);border-right:1px solid rgba(0,0,0,.08);z-index:53;box-shadow:8px 0 32px rgba(0,0,0,.25);display:flex;flex-direction:column;overflow-y:auto;}
.sidebar.open{left:0;}
.sb-head{background:linear-gradient(135deg,var(--or),var(--ord));padding:24px 20px 22px;color:#fff;position:relative;overflow:hidden;}
.sb-head::after{content:'';position:absolute;top:-30px;right:-30px;width:120px;height:120px;background:rgba(255,255,255,.12);border-radius:50%;}
.sb-head .sb-name{font-weight:700;font-size:15px;display:flex;align-items:center;flex-wrap:wrap;}
.sb-head .sb-email{font-size:12px;opacity:.85;margin-top:2px;}
.sb-head .sb-bal{margin-top:10px;font-size:13px;background:rgba(255,255,255,.2);display:inline-block;padding:4px 12px;border-radius:999px;position:relative;overflow:hidden;}
.sb-topstrip{background:rgba(0,0,0,.12);padding:10px 20px;display:flex;}
.sb-topbadge{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-width:140px;background:linear-gradient(90deg,#3B82F6,#2563EB);color:#fff;font-weight:800;font-size:14px;padding:8px 26px;border-radius:999px;box-shadow:0 4px 14px rgba(37,99,235,.35),inset 0 1px 0 rgba(255,255,255,.25);border:1px solid rgba(255,255,255,.3);}
.sb-head .sb-bal::after{content:'';position:absolute;top:0;left:-150%;width:55%;height:100%;background:linear-gradient(115deg,transparent,rgba(255,255,255,.65),transparent);transform:skewX(-20deg);animation:btnSheen 2.6s ease-in-out infinite;pointer-events:none;}
.sb-head-btns{display:flex;gap:8px;margin-top:14px;}
.sb-head-btn{flex:1;text-align:center;background:rgba(255,255,255,.22);color:#fff;font-weight:700;font-size:13.5px;padding:11px 6px;border-radius:10px;text-decoration:none;transition:background .15s;position:relative;overflow:hidden;}
.sb-head-btn:hover{background:rgba(255,255,255,.34);}
.sb-head-btn::after{content:'';position:absolute;top:0;left:-150%;width:55%;height:100%;background:linear-gradient(115deg,transparent,rgba(255,255,255,.65),transparent);transform:skewX(-20deg);animation:btnSheen 2.6s ease-in-out infinite;pointer-events:none;}
.sb-head-btn:nth-child(2)::after{animation-delay:.5s;}
.sb-nav{flex:1;padding:8px 0;margin-top:10px;}
.sb-nav a{display:flex;align-items:center;gap:12px;padding:15px 20px;font-size:15.5px;color:var(--tx);transition:all .18s;border-left:3px solid transparent;}
.sb-nav a:hover{background:rgba(59,130,246,.08);color:var(--or);}
.sb-nav a.active{background:linear-gradient(90deg,rgba(59,130,246,.16),transparent);color:var(--or);border-left:3px solid var(--or);}
.sb-nav a.active::after{content:'';position:absolute;top:0;left:-150%;width:35%;height:100%;background:linear-gradient(115deg,transparent,rgba(59,130,246,.35),transparent);transform:skewX(-20deg);animation:btnSheen 2.6s ease-in-out infinite;pointer-events:none;}
.sb-nav a .icon{width:24px;display:inline-flex;align-items:center;justify-content:center;}
.sb-head-btn{display:flex;align-items:center;justify-content:center;}
.nav-badge{margin-left:auto;background:linear-gradient(135deg,#F87171,#DC2626);color:#fff;font-size:10px;font-weight:800;padding:2px 7px;border-radius:999px;box-shadow:0 0 10px rgba(248,113,113,.5);animation:floatGlow 2s ease-in-out infinite;}
.sb-nav a{position:relative;overflow:hidden;}
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
.cs-trigger{display:flex;align-items:center;justify-content:space-between;width:100%;background:rgba(255,255,255,.04);border:1.5px solid var(--ln);border-radius:12px;padding:15px 16px;color:var(--tx);font-family:'Inter',sans-serif;font-size:15px;font-weight:600;cursor:pointer;transition:border-color .2s,box-shadow .2s;}
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
.btn-or{position:relative;overflow:hidden;width:100%;background:linear-gradient(90deg,#93C5FD,var(--or),var(--ord));color:#1a1220;border:none;border-radius:10px;padding:14px;font-weight:800;font-size:15px;cursor:pointer;font-family:'Inter',sans-serif;letter-spacing:.3px;box-shadow:0 4px 20px rgba(59,130,246,.35);transition:all .2s;}
.btn-or::after{content:'';position:absolute;top:0;left:-150%;width:55%;height:100%;background:linear-gradient(115deg,transparent,rgba(255,255,255,.65),transparent);transform:skewX(-20deg);animation:btnSheen 2.6s ease-in-out infinite;pointer-events:none;}
@keyframes btnSheen{0%{left:-150%;}35%{left:150%;}100%{left:150%;}}
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
.stat-card{background:#fff;border:1.5px solid rgba(59,130,246,.18);border-left:4px solid var(--or);border-radius:14px;padding:16px 16px;display:flex;align-items:center;gap:14px;box-shadow:0 4px 16px rgba(20,10,40,.05);animation:fadeUp .45s ease both;transition:transform .2s,border-color .2s;}
.stat-card:hover{transform:translateY(-3px);border-color:rgba(59,130,246,.4);}
.stat-icon-wrap{width:46px;height:58px;background:#fff;border-radius:12px;display:flex;align-items:center;justify-content:center;flex-shrink:0;border:1.6px solid var(--or);}
.stat-icon{font-size:26px;}
.stat-info .stat-name{font-size:16px;font-weight:700;color:var(--tx);line-height:1.2;}
.stat-info .stat-label{font-size:12px;color:#6B7280;margin-top:3px;display:flex;align-items:center;}
.stat-label{font-size:12px;color:#6B7280;}
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
.search-bar{display:flex;gap:10px;margin-bottom:16px;position:relative;align-items:center;}
.search-bar-ic{position:absolute;left:14px;top:50%;transform:translateY(-50%);pointer-events:none;display:flex;}
.search-bar input{flex:1;background:rgba(255,255,255,.04);border:1px solid var(--ln);border-radius:10px;padding:11px 14px 11px 40px;font-size:14px;font-family:'Inter',sans-serif;color:var(--tx);}
.search-bar input:focus{outline:2px solid var(--or);}
.price-display{background:linear-gradient(90deg,#93C5FD,#3B82F6,#2563EB);color:#1a1220;border-radius:12px;padding:16px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px;box-shadow:0 4px 20px rgba(59,130,246,.35);position:relative;overflow:hidden;}
.price-display::after{content:'';position:absolute;top:0;left:-150%;width:45%;height:100%;background:linear-gradient(115deg,transparent,rgba(255,255,255,.65),transparent);transform:skewX(-20deg);animation:btnSheen 2.6s ease-in-out infinite;pointer-events:none;}
.price-display .p-label{font-size:13px;opacity:.85;font-weight:600;}
.price-display .p-val{font-size:22px;font-weight:800;display:flex;align-items:center;gap:8px;}
.price-display .label{font-size:13px;opacity:.85;font-weight:400;}
.plat-tabs{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:20px;}
.plat-tab{display:flex;align-items:center;justify-content:center;gap:9px;background:var(--sf);border:1.5px solid var(--ln);border-radius:12px;padding:15px 16px;font-size:15px;font-weight:700;cursor:pointer;color:var(--tx);transition:all .15s;font-family:'Inter',sans-serif;position:relative;overflow:hidden;}
.plat-tab:hover{border-color:var(--or);color:var(--or);transform:translateY(-1px);}
.plat-tab.active{background:linear-gradient(90deg,var(--or),var(--ord));color:#fff;border-color:var(--or);box-shadow:0 4px 18px rgba(59,130,246,.4);}
.plat-tab .plat-ic{width:22px;height:22px;font-size:18px;line-height:1;display:inline-block;}
.info-box{background:rgba(59,130,246,.06);border:1.5px solid var(--ln);border-radius:12px;padding:16px;margin-bottom:16px;position:relative;}
.info-box-title{display:flex;align-items:center;font-weight:700;font-size:13.5px;color:var(--or);margin-bottom:10px;}
.info-box p{font-size:12.5px;color:var(--mu);line-height:1.6;display:flex;align-items:flex-start;gap:8px;margin-bottom:5px;padding:6px 10px;background:rgba(255,255,255,.03);border-radius:6px;}
.info-box p:last-child{margin-bottom:0;}
.hint-text{font-size:12px;color:var(--mu);margin-top:4px;}

/* ORDERS TABLE */
.filter-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px;}
.ftab{background:var(--sf);border:1px solid var(--ln);border-radius:999px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer;color:var(--tx);transition:all .15s;white-space:nowrap;position:relative;overflow:hidden;}
.ftab:hover{border-color:var(--or);color:var(--or);}
.ftab.active{background:linear-gradient(90deg,var(--or),var(--ord));color:#1a1220;border-color:var(--or);box-shadow:0 3px 14px rgba(59,130,246,.35);}
.ftab.active::after{content:'';position:absolute;top:0;left:-150%;width:55%;height:100%;background:linear-gradient(115deg,transparent,rgba(255,255,255,.65),transparent);transform:skewX(-20deg);animation:btnSheen 2.6s ease-in-out infinite;pointer-events:none;}
.table-card{background:var(--sf);backdrop-filter:blur(16px) saturate(140%);-webkit-backdrop-filter:blur(16px) saturate(140%);border:1px solid var(--glass-brd);border-radius:var(--ra);overflow:hidden;box-shadow:var(--glow);}
.search-orders{display:flex;gap:10px;padding:14px;border-bottom:1px solid var(--ln);}
.search-orders input{flex:1;background:rgba(255,255,255,.04);border:1px solid var(--ln);border-radius:10px;padding:9px 13px;font-size:13px;font-family:'Inter',sans-serif;color:var(--tx);}
.tbl-head{background:linear-gradient(90deg,var(--or),var(--ord));color:#1a1220;}
table.ot{width:100%;border-collapse:collapse;font-size:13px;}
table.ot th{padding:12px 12px;text-align:left;font-weight:700;white-space:nowrap;}
table.ot td{padding:11px 12px;border-bottom:1px solid var(--glass-brd);}
table.ot tr:last-child td{border-bottom:none;}
table.ot tr:hover td{background:rgba(59,130,246,.06);}
table.svc-tbl th{background:linear-gradient(90deg,var(--or),var(--ord));color:#fff;font-size:12.5px;}
table.svc-tbl td{vertical-align:middle;}
.svc-id{color:var(--mu);font-weight:700;font-size:12.5px;}
.svc-name{font-weight:600;max-width:340px;}
.svc-eta{color:var(--mu);font-size:12.5px;white-space:nowrap;}
.svc-desc{min-width:60px;}
.svc-pill{display:inline-flex;align-items:center;gap:4px;font-weight:800;font-size:12.5px;color:#fff;padding:6px 12px;border-radius:999px;white-space:nowrap;}
.svc-pill-price{background:linear-gradient(90deg,var(--or),var(--ord));box-shadow:0 3px 10px rgba(59,130,246,.35);}
.svc-pill-sub{font-weight:600;opacity:.9;}
.svc-pill-mm{background:#EF4444;box-shadow:0 3px 10px rgba(239,68,68,.3);}
.btn-al{display:inline-flex;align-items:center;gap:4px;background:linear-gradient(90deg,var(--or),var(--ord));color:#fff;font-weight:800;font-size:12.5px;padding:7px 16px;border-radius:999px;text-decoration:none;white-space:nowrap;box-shadow:0 3px 10px rgba(59,130,246,.35);transition:transform .15s,box-shadow .15s;}
.btn-al:hover{transform:translateY(-1px);box-shadow:0 5px 16px rgba(59,130,246,.45);}
@media(max-width:640px){
  table.svc-tbl{min-width:760px;}
}
.pill{padding:3px 10px;border-radius:999px;font-size:11px;font-weight:700;}
.pill-wait{background:rgba(251,191,36,.15);color:#FBBF24;}
.pill-done{background:rgba(52,211,153,.15);color:#34D399;}
.pill-run{background:rgba(96,165,250,.15);color:#60A5FA;}
.pill-cancel{background:rgba(248,113,113,.15);color:#F87171;}
.empty-tbl{padding:48px;text-align:center;color:var(--mu);font-size:14px;}
.btn-row-action{border:none;border-radius:8px;padding:6px 12px;font-size:11.5px;font-weight:700;cursor:pointer;font-family:'Inter',sans-serif;white-space:nowrap;transition:all .15s;}
.btn-row-cancel{background:rgba(248,113,113,.15);color:#F87171;}
.btn-row-cancel:hover{background:rgba(248,113,113,.28);}
.btn-row-restore{background:rgba(96,165,250,.15);color:#60A5FA;}
.btn-row-restore:hover{background:rgba(96,165,250,.28);}
.btn-row-action:disabled{opacity:.55;cursor:default;}

/* BALANCE PAGE */
.bal-page{max-width:560px;padding:24px 0 64px;margin:0 auto;}
.bal-grid{display:block;}
.bal-right{margin-top:28px;}
@media(min-width:880px){
  .bal-page{max-width:980px;}
  .bal-grid{display:grid;grid-template-columns:360px 1fr;gap:36px;align-items:start;}
  .bal-right{margin-top:0;}
}
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
  .stat-icon-wrap{width:34px;height:44px;border-radius:9px;}
  .stat-icon{font-size:18px;}
  .stat-info .stat-name{font-size:13px;}
  .stat-info .stat-label{font-size:10.5px;}
  .order-panel{padding:14px;border-radius:14px;margin-bottom:16px;}
  .search-bar{margin-bottom:12px;}
  .search-bar input{padding:9px 12px;font-size:14px;}
  .plat-tabs{grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px;}
  .plat-tab{padding:12px 10px;font-size:13.5px;gap:7px;}
  .plat-tab .plat-ic{width:18px;height:18px;font-size:15px;}
  .cs-trigger{padding:10px 12px;font-size:13px;}
  .field{margin-bottom:11px;gap:5px;}
  .field label{font-size:12px;}
  .field input,.field select,.field textarea{padding:10px 12px;}
  .price-display{padding:12px 14px;margin-bottom:12px;}
  .price-display .p-val{font-size:18px;}
  .price-display .p-label{font-size:11.5px;}
  .btn-or{padding:12px;font-size:14px;}
  .info-box{padding:12px;}
  .info-box-title{font-size:12px;margin-bottom:8px;}
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
  var hb=document.getElementById('hamburger-btn');
  var op=f!==undefined?f:!sb.classList.contains('open');
  sb.classList.toggle('open',op);ov.classList.toggle('open',op);
  if(hb)hb.classList.toggle('active',op);
  if(op&&sb)sb.scrollTop=0; /* menyu her acilanda basliq (ad/verified) hemise tam gorunsun */
}

/* ===== SEHIFE KECIDI ZAMANI YUKLENME SKELETONU / PROGRESS BAR =====
   light=true olanda (form submit kimi kicik AJAX emeliyyatlar) yalniz incə
   progress-bar gosterilir, tam skelet (yeni sehife yuklenirmis kimi gorunen
   overlay) gosterilmir ki, "sehife yenilenir" tessuratı yaranmasin. */
function showPageLoading(light){
  var bar=document.getElementById('page-loading-bar');
  var skel=document.getElementById('page-skeleton');
  if(bar){bar.classList.add('active');}
  if(skel && !light){skel.classList.add('active');}
}
function hidePageLoading(){
  var bar=document.getElementById('page-loading-bar');
  var skel=document.getElementById('page-skeleton');
  if(bar)bar.classList.remove('active');
  if(skel)skel.classList.remove('active');
}
/* ===== AJAX (PJAX-STYLE) NAVIGASIYA — SEHIFE TAM YENILENMIR =====
   Bütün daxili linkler ve formlar klikde/submit-de tam sehife reload etmir;
   evezine yeni sehifeni fetch() ile geterib yalniz <body> icini deyisir ve
   URL-i (history.pushState) yenileyir. Beleliklə brauzer "aq ekran" ile
   yenilenmir, keçid smooth olur. */
var __pjaxBusy=false;

function pjaxRunScripts(container){
  var scripts=container.querySelectorAll('script');
  scripts.forEach(function(old){
    if(old.id==='global-js')return; /* qlobal JS artiq yuklenib, teze icra lazim deyil */
    var s=document.createElement('script');
    for(var i=0;i<old.attributes.length;i++){
      s.setAttribute(old.attributes[i].name, old.attributes[i].value);
    }
    s.textContent=old.textContent;
    old.parentNode.replaceChild(s, old);
  });
}

function pjaxInit(){
  /* sehife deyisende bir daha lazim olan kicik init funksiyalari */
  if(typeof applyThemeIcon==='function')applyThemeIcon();
  if(typeof syncTopbarHeight==='function')syncTopbarHeight();
  if(typeof renderFavChips==='function')renderFavChips();
  if(typeof updateFavHeart==='function')updateFavHeart();
  if(typeof initOrderPriceCalc==='function')initOrderPriceCalc();
  if(typeof initAutoToasts==='function')initAutoToasts();
  if(typeof initOrderStatusPoll==='function')initOrderStatusPoll();
  var sel=document.getElementById('lang-select');
  if(sel){
    var m=document.cookie.match(/googtrans=\\/az\\/([a-z]{2})/);
    var lang=m?m[1]:(function(){try{return localStorage.getItem('bp_lang')||'az';}catch(e){return 'az';}})();
    sel.value=lang;
  }
}

function pjaxSwap(html, finalUrl, push){
  var doc=new DOMParser().parseFromString(html,'text/html');
  var newBody=doc.body;
  if(!newBody){window.location.href=finalUrl;return;}
  document.title=doc.title||document.title;
  document.body.innerHTML=newBody.innerHTML;
  document.body.className=newBody.className;
  if(push)history.pushState({pjax:true}, document.title, finalUrl);
  pjaxRunScripts(document.body);
  hidePageLoading();
  window.scrollTo(0,0);
  pjaxInit();
  __pjaxBusy=false;
}

function pjaxGo(url, push){
  pjaxFetch(url, {method:'GET'}, push!==false, true);
}

function pjaxFetch(url, opts, push, light){
  if(__pjaxBusy)return;
  __pjaxBusy=true;
  showPageLoading(!!light);
  fetch(url, Object.assign({credentials:'same-origin'}, opts||{}))
    .then(function(res){
      if(!res.ok && res.status>=500)throw new Error('server-error');
      return res.text().then(function(html){return {html:html,url:res.url};});
    })
    .then(function(r){pjaxSwap(r.html, r.url, push!==false);})
    .catch(function(){ __pjaxBusy=false; hidePageLoading(); window.location.href=url; });
}

document.addEventListener('DOMContentLoaded',function(){
  document.addEventListener('click',function(e){
    var a=e.target.closest('a');
    if(!a)return;
    var href=a.getAttribute('href')||'';
    if(!href||href.charAt(0)==='#'||href.indexOf('javascript:')===0)return;
    if(a.target==='_blank'||a.hasAttribute('download')||a.hasAttribute('data-no-ajax'))return;
    if(e.metaKey||e.ctrlKey||e.shiftKey||e.button===1)return;
    var url;
    try{
      url=new URL(href,location.href);
      if(url.origin!==location.origin)return;
    }catch(err){return;}
    if(e.defaultPrevented)return; /* mes. bir confirm() legv edibse, kecid olmasin */
    e.preventDefault();
    var sbEl=document.getElementById('sb'), ovEl=document.getElementById('sb-ov'), hbEl=document.getElementById('hamburger-btn');
    if(sbEl&&sbEl.classList.contains('open')){sbEl.classList.remove('open');if(ovEl)ovEl.classList.remove('open');if(hbEl)hbEl.classList.remove('active');}
    pjaxGo(url.href, true);
  });

  document.body.addEventListener('submit',function(e){
    var f=e.target;
    if(!f||f.tagName!=='FORM')return;
    if(f.hasAttribute('data-no-ajax')||f.target)return;
    if(e.defaultPrevented)return; /* sehifeye xas ozel submit handleri artiq idare edibse */
    var action=f.getAttribute('action')||location.pathname;
    var url;
    try{
      url=new URL(action,location.href);
      if(url.origin!==location.origin)return;
    }catch(err){return;}
    var method=(f.method||'get').toLowerCase();
    e.preventDefault();
    if(method==='get'){
      var fd=new FormData(f);
      url.search=new URLSearchParams(fd).toString();
      pjaxFetch(url.href, {method:'GET'}, true, true);
    }else{
      var fd2=new FormData(f);
      pjaxFetch(url.href, {method:'POST', body:fd2}, true, true);
    }
  });

  window.addEventListener('popstate',function(){
    pjaxFetch(location.href, {method:'GET'}, false, true);
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
function initOrderPriceCalc(){
  var si=document.getElementById('svc-select');
  var pi=document.getElementById('price-val');
  var qi=document.getElementById('qty-input');
  var minEl=document.getElementById('min-qty');
  var maxEl=document.getElementById('max-qty');
  var etaEl=document.getElementById('eta-val');
  var linkInput=document.getElementById('link-input');
  var linkWarn=document.getElementById('link-warn');
  var submitBtn=document.getElementById('order-submit');
  if(!si&&!linkInput)return; /* bu sehifede sifaris formu yoxdur */

  function isValidLink(v){
    if(!v)return false;
    try{
      var u=new URL(v);
      return (u.protocol==='http:'||u.protocol==='https:') && u.hostname.indexOf('.')>-1;
    }catch(e){return false;}
  }
  function checkAll(){
    var linkOk=true;
    if(linkInput){
      var v=linkInput.value.trim();
      linkOk = v.length===0 || isValidLink(v);
      if(linkWarn)linkWarn.style.display=(v.length>0&&!isValidLink(v))?'block':'none';
      linkInput.style.borderColor=(v.length>0&&!isValidLink(v))?'#F87171':'';
    }
    if(submitBtn)submitBtn.disabled=!linkOk;
    return linkOk;
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
    if(opt.dataset.min&&minEl)minEl.innerText='Min: '+opt.dataset.min;
    if(opt.dataset.max&&maxEl)maxEl.innerText=' - Max: '+opt.dataset.max;
    if(etaEl){
      etaEl.value = '~30 dəqiqə - 1 saat';
    }
    var qtyRaw=(qi?qi.value:'').toString().trim();
    if(qtyRaw===''){ pi.innerText='0.00 ₼'; checkAll(); return; }
    var price=parseFloat(opt.dataset.price||0);
    var max=parseInt(opt.dataset.max||1000000)||1000000;
    var qty=parseInt(qtyRaw)||0;
    var qtyForCalc=Math.min(qty,max); /* mebleğ hesablamasi mehsulun max miqdarindan asagi saxlanilir ki, mena kesb etmeyen nehong reqem cixmasin */
    pi.innerText=fmtMoney(price*qtyForCalc/1000)+' ₼';
    checkAll();
  }
  if(si){si.addEventListener('change',updatePrice);}
  if(qi){qi.addEventListener('input',updatePrice);qi.addEventListener('keyup',updatePrice);qi.addEventListener('change',updatePrice);}
  if(linkInput){linkInput.addEventListener('input',checkAll);}
  updatePrice();
}
document.addEventListener('DOMContentLoaded',initOrderPriceCalc);

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
function initAutoToasts(){
  document.querySelectorAll('.ok-box[data-toast],.err-box[data-toast]').forEach(function(el){
    showToast(el.textContent.trim(),el.classList.contains('err-box')?'err':'ok');
  });
}
document.addEventListener('DOMContentLoaded',initAutoToasts);

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
var __pollOrderTimer=null;
function initOrderStatusPoll(){
  if(__pollOrderTimer){clearInterval(__pollOrderTimer);__pollOrderTimer=null;}
  if(document.querySelector('tr.order-row[data-order-id]')){
    pollOrderStatus();
    __pollOrderTimer=setInterval(pollOrderStatus, 8000);
  }
}
document.addEventListener('DOMContentLoaded',initOrderStatusPoll);

/* ===== SIFARIŞ LƏĞV / BƏRPA (ekran yenilənmədən, AJAX) ===== */
function _orderActionBtnBusy(btn,text){
  btn.dataset.origText=btn.innerHTML;
  btn.disabled=true;
  btn.innerHTML='<span class="spin"></span>'+text;
}
function _orderActionBtnDone(btn){
  if(btn && btn.parentNode){ btn.parentNode.innerHTML='—'; }
}
function _orderActionBtnReset(btn){
  if(!btn) return;
  btn.disabled=false;
  btn.innerHTML=btn.dataset.origText||btn.innerHTML;
}
function cancelOrder(btn, orderId){
  if(!confirm('Sifarişi ləğv etmək istədiyinizə əminsiniz? Ödədiyiniz məbləğ balansınıza geri qaytarılacaq.')) return;
  _orderActionBtnBusy(btn,'Ləğv edilir...');
  fetch('/orders/cancel',{
    method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded','X-Requested-With':'fetch'},
    credentials:'same-origin',
    body:'order_id='+encodeURIComponent(orderId)
  }).then(function(r){return r.json();}).then(function(data){
    if(data.ok){
      showToast(data.message||'Ləğv tələbiniz dəstək xidmətinə göndərildi.','ok');
      var tr=document.querySelector('tr.order-row[data-order-id="'+orderId+'"]');
      if(tr){
        tr.setAttribute('data-status','Ləğv Edildi');
        var pill=tr.querySelector('.pill');
        if(pill){ pill.textContent='Ləğv Edildi'; pill.className='pill pill-cancel'; }
      }
      _orderActionBtnDone(btn);
    }else{
      showToast(data.error||'Xəta baş verdi.','err');
      _orderActionBtnReset(btn);
    }
  }).catch(function(){
    showToast('Şəbəkə xətası. Yenidən cəhd edin.','err');
    _orderActionBtnReset(btn);
  });
}
function restoreOrder(btn, orderId){
  if(!confirm('Bu sifariş üçün bərpa (refill) tələbi göndərmək istəyirsiniz?')) return;
  _orderActionBtnBusy(btn,'Göndərilir...');
  fetch('/orders/restore',{
    method:'POST',
    headers:{'Content-Type':'application/x-www-form-urlencoded','X-Requested-With':'fetch'},
    credentials:'same-origin',
    body:'order_id='+encodeURIComponent(orderId)
  }).then(function(r){return r.json();}).then(function(data){
    if(data.ok){
      showToast(data.message||'Bərpa tələbiniz dəstək xidmətinə göndərildi.','ok');
      _orderActionBtnReset(btn);
    }else{
      showToast(data.error||'Xəta baş verdi.','err');
      _orderActionBtnReset(btn);
    }
  }).catch(function(){
    showToast('Şəbəkə xətası. Yenidən cəhd edin.','err');
    _orderActionBtnReset(btn);
  });
}
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
    return f"<script id=\"global-js\">{JS}</script>"

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
    ("vkontakte", "🔵"), ("vk", "🔵"),
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
    ("twitch", "twitch"),
    ("vk", "vk"), ("vkontakte", "vk"),
]
def cat_label_html(category_name):
    """Kateqoriya adının önünə real platforma loqosu (mövcud olduqda) və ya
    emoji əlavə edir. Adi HTML üçündür, <select> daxilində istifadə olunmaz."""
    n = (category_name or "").strip().lower()
    for key, icon_name in PLATFORM_LOGO_ICONS:
        if key in n:
            return f'<img src="/icons/{icon_name}.png" alt="" style="width:16px;height:16px;vertical-align:-3px;margin-right:5px;display:inline-block;">{category_name}'
    return cat_label(category_name)

# ===== KATEQORİYA SIRASI ("Hamısı" seçilərkən məhsulların göstərilmə sırası) =====
# İstək: TikTok, Instagram, Facebook, Telegram, YouTube, Twitter, WhatsApp, Discord
# kateqoriyaları HƏMİŞƏ bu sıra ilə öndə gəlsin; Twitch, VK və digər bütün
# kateqoriyalar bunlardan sonra (öz aralarında istənilən sırada) gəlsin.
CATEGORY_PRIORITY_ORDER = ["tiktok","instagram","facebook","telegram","youtube","twitter","whatsapp","discord"]
CATEGORY_PRIORITY_KEYWORDS = [
    ("tiktok","tiktok"), ("tik tok","tiktok"),
    ("instagram","instagram"), ("insta","instagram"),
    ("facebook","facebook"), ("fb","facebook"),
    ("telegram","telegram"),
    ("youtube","youtube"), ("you tube","youtube"),
    ("twitter","twitter"), ("x.com","twitter"),
    ("whatsapp","whatsapp"),
    ("discord","discord"),
]
def category_sort_key(category_name):
    n = (category_name or "").strip().lower()
    for key, plat in CATEGORY_PRIORITY_KEYWORDS:
        if key in n:
            try:
                return CATEGORY_PRIORITY_ORDER.index(plat)
            except ValueError:
                pass
    return len(CATEGORY_PRIORITY_ORDER)  # Twitch, VK, Digər — əsas 8 platformadan sonra

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

def wico(size=16, color="#3B82F6", mr=0):
    """Bütün saytda tutarlı istifadə olunan 'pəncərə' ikonu — dar şaquli
    düzbucaqlı çərçivə içində diaqonal X xətləri ilə (istifadəçinin göstərdiyi
    şəkildəki kimi). Real şəkil (SVG) olduğu üçün heç bir cihazda/brauzerdə
    boş kvadrat/tofu kimi sınmır — həmişə eyni cür görünür."""
    mrg = f"margin-right:{mr}px;" if mr else ""
    w = round(size*0.72)
    return (f'<svg width="{w}" height="{size}" viewBox="0 0 17 24" fill="none" '
            f'xmlns="http://www.w3.org/2000/svg" style="flex-shrink:0;display:inline-block;'
            f'vertical-align:-3px;{mrg}"><rect x="2" y="2" width="13" height="20" rx="3" '
            f'stroke="{color}" stroke-width="1.6"/><path d="M2.5 2.5L14.5 21.5M14.5 2.5L2.5 21.5" '
            f'stroke="{color}" stroke-width="1.2"/></svg>')

def display_name_html(email, name, verified=False):
    """Sahibin (OWNER_EMAIL) adı heç yerdə göstərilmir — onun əvəzinə rəngli
    '👑 Sahib' yazısı çıxır (adi mavi təsdiq nişanı əvəzinə). Digər istifadəçilər
    üçün adi ad + (varsa) mavi təsdiq nişanı göstərilir."""
    if (email or "").strip().lower() == OWNER_EMAIL:
        return OWNER_NAME_HTML, ""
    badge_svg = VERIFIED_BADGE_SVG if verified else ""
    return name, badge_svg

def topbar_auth(user, bal_info):
    return f"""<header class="topbar" id="site-topbar">
<div class="wrap topbar-inner">
  <a class="brand" href="/">{LOGO_SVG}</a>
  <div class="topuser">
    <button class="hamburger" id="hamburger-btn" onclick="toggleSidebar()" aria-label="Menyu">
      <span></span><span></span><span></span>
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
        ("🧩","Servislər","/services","services",""),
        ("🎫","Dəstək","/support","support",badge),
        ("📋","Qaydalar","/terms","terms",""),
        ("📢","Telegram Kanalımız","https://t.me/AzPanelim","telegram",""),
    ]
    nav = ""
    for ic,lbl,href,key,bd in links:
        cls = "active" if active==key else ""
        extra = ' target="_blank" rel="noopener noreferrer"' if href.startswith("http") else ""
        nav += f'<a href="{href}" class="{cls}"{extra}><span class="icon">{wico(18)}</span>{lbl}{bd}</a>'
    verified_tag = '<span class="sb-verified-tag">✓ Təsdiqlənmiş</span>' if (verified and (email or "").strip().lower()!=OWNER_EMAIL) else ""
    return f"""
<div class="sb-ov" id="sb-ov" onclick="toggleSidebar(false)"></div>
<div class="sidebar" id="sb">
  <div class="sb-topstrip">
    <div class="sb-topbadge">{wico(14,"#fff",5)}{bal:.2f} ₼</div>
  </div>
  <div class="sb-head">
    <div class="sb-name">{name}{badge_svg}{verified_tag}</div>
    <div class="sb-bal">Balans: {bal:.2f} ₼</div>
    <div class="sb-head-btns">
      <a class="sb-head-btn" href="/profile">{wico(15,"#fff",6)}Hesabım</a>
      <a class="sb-head-btn" href="/logout">{wico(15,"#fff",6)}Çıxış</a>
    </div>
  </div>
  <nav class="sb-nav">
    {nav}
  </nav>
</div>"""

def footer():
    return ""

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

def _js_str(v):
    """Python string -> təhlükəsiz JS string literalı (boşdursa null qaytarır)."""
    if not v:
        return "null"
    esc = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")
    return f'"{esc}"'

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

def render_dashboard(user, bal_info, categories, all_products, ann_list, recent_orders=None, order_err="", preselect_category="", preselect_product=""):
    recent_orders = recent_orders or []
    order_err_html = f"""<div class="err-box" id="order-err-box" style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:16px;">
  <span>⚠️ {order_err}</span>
  <span onclick="this.closest('.err-box').style.display='none'" style="cursor:pointer;font-size:16px;line-height:1;opacity:.75;">×</span>
</div>""" if order_err else ""
    name=user.get("name","İstifadəçi")
    name,_bs=display_name_html(user.get("email",""), name)
    bal=float(bal_info["balance"])
    spent=float(bal_info.get("total_spent",0))
    total_ord=BASE_ORDER_COUNT+int(bal_info.get("total_orders",0))

    stats=f"""<div class="stat-grid">
<div class="stat-card">
  <div class="stat-icon-wrap"><div class="stat-icon">{wico(22)}</div></div>
  <div class="stat-info">
    <div class="stat-name" style="color:var(--or);">{name}</div>
    <div class="stat-label">Xoş Gəlmisiniz</div>
  </div>
</div>
<div class="stat-card">
  <div class="stat-icon-wrap"><div class="stat-icon">{wico(22)}</div></div>
  <div class="stat-info">
    <div class="stat-name" style="color:var(--or);">&#8380; {spent:.2f}</div>
    <div class="stat-label">Ümumi Xərclənən</div>
  </div>
</div>
<div class="stat-card">
  <div class="stat-icon-wrap"><div class="stat-icon">{wico(22)}</div></div>
  <div class="stat-info">
    <div class="stat-name" style="color:var(--or);">{total_ord}</div>
    <div class="stat-label">Ümumi Sifarişlər</div>
  </div>
</div>
<div class="stat-card">
  <div class="stat-icon-wrap"><div class="stat-icon">{wico(22)}</div></div>
  <div class="stat-info">
    <div class="stat-name" style="color:var(--or);">&#8380; {bal:.2f}</div>
    <div class="stat-label">Balans</div>
  </div>
</div>
</div>"""

    cat_opts="".join(f'<option value="{c}">{cat_label(c)}</option>' for c in categories.keys())
    first_cat=list(categories.keys())[0] if categories else ""
    first_prods=categories.get(first_cat,[])
    svc_opts="".join(f'<option value="{p["product_id"]}" data-price="{p["price"]}" data-min="{p["min_qty"]}" data-max="{p["max_qty"]}">{p["name"]} — {p["price"]} ₼</option>' for p in first_prods)
    all_opts=""
    for cat,prods in categories.items():
        for p in prods:
            all_opts+=f'<option value="{p["product_id"]}" data-price="{p["price"]}" data-min="{p["min_qty"]}" data-max="{p["max_qty"]}">{p["name"]} — {p["price"]} ₼</option>'

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
    <span class="search-bar-ic">{wico(18)}</span>
    <input type="text" placeholder="Xidmət axtar..." oninput="filterSvc(this.value)">
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
      <input type="number" id="qty-input" name="quantity" min="1" required>
      <span class="hint-text" id="min-qty">Min: 10</span><span class="hint-text" id="max-qty"> - Max: 100000</span>
    </div>
    
    <div class="price-display">
      <div>
        <div class="p-label">Toplam Məbləğ</div>
        <div class="p-val" style="font-size:17px;"><span id="price-val">0.00 ₼</span></div>
      </div>
    </div>
    <button class="btn-or" id="order-submit" type="submit">{wico(18,"#1a1220",8)}SİFARİŞİ TƏSDİQLƏ</button>
    <div class="info-box" style="margin-top:24px;">
      <div class="info-box-title">{wico(16,mr=8)}Sifariş Qaydaları</div>
      <p>{wico(14,mr=8)}Profil Gizli Olmamalıdır! Sifariş verərkən profiliniz mütləq "Public" (Açıq) olmalıdır.</p>
      <p>{wico(14,mr=8)}İkinci Sifarişi Vurmayın. Eyni linkə edilən bir sifariş bitmədən, ikinci sifarişi verməyin.</p>
      <p>{wico(14,mr=8)}Link Formatı Düzgün Olsun. Linki tam şəkildə kopyalayın.</p>
      <p>{wico(14,mr=8)}Dəstək Tələbi. Hər hansı bir xəta baş verərsə "Dəstək" bölməsindən bizə yazın.</p>
    </div>
  </form>
</div>
{ann_html if ann_html else ""}
</section>
<script>
(function(){{
  // Qeyd: ekran öz-özünə sürüşməsin deyə burda artıq avtomatik scroll edilmir —
  // xəta olduqda səhifə "yerində" qalır, istifadəçi özü aşağı/yuxarı baxa bilər.
}})();
var allProds={{}};
{_build_prods_js(categories)}
function changeCat(cat){{
  var prods=cat==='all'?Object.values(allProds).flat():allProds[cat]||[];
  populateSvcSelect(prods);
}}
function svcOptLabel(p){{return p.name+' — '+p.price+' ₼';}}
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
  closeCatList();
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
var CAT_ICON_KEYWORDS=Object.assign({{}},PLAT_KEYWORDS,{{
  twitch:['twitch'],
  vk:['vkontakte','vk']
}});
function catMatchesPlatform(cat,plat){{
  var n=cat.toLowerCase();
  if(plat==='all')return true;
  if(plat==='other'){{
    return !Object.values(PLAT_KEYWORDS).some(function(kws){{return kws.some(function(k){{return n.indexOf(k)>-1;}});}});
  }}
  var kws=PLAT_KEYWORDS[plat]||[plat];
  return kws.some(function(k){{return n.indexOf(k)>-1;}});
}}
var PLAT_ICON_FILES={{tiktok:'tiktok.png',instagram:'instagram.png',facebook:'facebook.png',telegram:'telegram.png',youtube:'youtube.png',twitter:'twitter.png',whatsapp:'whatsapp.png',discord:'discord.png',twitch:'twitch.png',vk:'vk.png'}};
function catIconHtml(cat){{
  var n=(cat||'').toLowerCase();
  var plats=Object.keys(CAT_ICON_KEYWORDS);
  for(var i=0;i<plats.length;i++){{
    var p=plats[i];
    if(CAT_ICON_KEYWORDS[p].some(function(k){{return n.indexOf(k)>-1;}})){{
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
  closeSvcList();
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
(function(){{
  var allBtn=document.querySelector('.plat-tab[data-plat="all"]');
  selectPlatform('all',allBtn);
  var PRESELECT_CAT={_js_str(preselect_category)};
  var PRESELECT_PID={_js_str(str(preselect_product) if preselect_product else "")};
  if(PRESELECT_CAT && allProds[PRESELECT_CAT]){{
    selectCatItem(PRESELECT_CAT,PRESELECT_CAT,catIconHtml(PRESELECT_CAT));
    if(PRESELECT_PID){{
      var __prods=allProds[PRESELECT_CAT]||[];
      var __match=null;
      for(var __i=0;__i<__prods.length;__i++){{ if(String(__prods[__i].id)===String(PRESELECT_PID)){{__match=__prods[__i];break;}} }}
      if(__match){{ selectSvcItem(__match.id,svcOptLabel(__match),catIconHtml(PRESELECT_CAT)); }}
    }}
    var __panel=document.querySelector('.order-panel');
    if(__panel) __panel.scrollIntoView({{behavior:'smooth',block:'start'}});
  }}
  var form=document.getElementById('order-form');
  var linkInput=document.getElementById('link-input');
  if(form&&linkInput){{
    form.addEventListener('submit',function(e){{
      e.preventDefault();
      var btn=document.getElementById('order-submit');
      var qi=document.getElementById('qty-input');
      var si=document.getElementById('svc-select');
      if(qi&&si){{
        var opt=si.options[si.selectedIndex];
        var min=parseInt((opt&&opt.dataset.min)||1), max=parseInt((opt&&opt.dataset.max)||1e9);
        var qty=parseInt(qi.value)||0;
        if(qty<min){{
          if(btn){{btn.disabled=false;btn.innerHTML=btn.dataset.origText||'SİFARİŞİ TƏSDİQLƏ';}}
          showOrderInlineError('Minumum Saydan Aşağı Sifariş vermək olmaz. ('+min+')');
          return;
        }}
        if(qty>max){{
          if(btn){{btn.disabled=false;btn.innerHTML=btn.dataset.origText||'SİFARİŞİ TƏSDİQLƏ';}}
          showOrderInlineError('Miqdar maksimumdan çoxdur. ('+max+')');
          return;
        }}
      }}
      var fd=new FormData(form);
      fetch('/order',{{method:'POST',body:fd,headers:{{'X-Requested-With':'fetch'}}}})
        .then(function(r){{return r.json();}})
        .then(function(data){{
          hidePageLoading();
          if(data.ok){{
            if(btn){{btn.innerHTML='✅ Uğurla göndərildi!';}}
            setTimeout(function(){{if(typeof pjaxGo==='function')pjaxGo(data.redirect||'/orders?new=1',true);else window.location.href=data.redirect||'/orders?new=1';}},550);
          }}else{{
            if(btn){{btn.disabled=false;btn.innerHTML=btn.dataset.origText||'SİFARİŞİ TƏSDİQLƏ';}}
            showOrderInlineError(data.error||'Xəta baş verdi.');
          }}
        }})
        .catch(function(){{
          hidePageLoading();
          if(btn){{btn.disabled=false;btn.innerHTML=btn.dataset.origText||'SİFARİŞİ TƏSDİQLƏ';}}
          showOrderInlineError('Şəbəkə xətası. Yenidən cəhd edin.');
        }});
    }});
  }}
}})();
function showOrderInlineError(msg){{
  var panel=document.querySelector('.order-panel');
  if(!panel)return;
  var old=panel.querySelector('.err-box');
  if(old)old.remove();
  var div=document.createElement('div');
  div.className='err-box';
  div.style.cssText='display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:14px;';
  div.innerHTML='<span>⚠️ '+msg+'</span><span onclick="this.closest(\\'.err-box\\').style.display=\\'none\\'" style="cursor:pointer;font-size:16px;line-height:1;opacity:.75;">×</span>';
  var btn=document.getElementById('order-submit');
  if(btn&&btn.parentNode){{btn.parentNode.insertBefore(div,btn);}}else{{panel.insertBefore(div,panel.firstChild);}}
}}
</script>"""
    return page_auth("Yeni Sifariş", body, user, bal_info, "new")

def render_services(user, bal_info, prods, q=""):
    """Xidmətlər (Servislər) siyahısı — istifadəçinin göstərdiyi nümunə saytdakı
    kimi: ID, Servis adı, 1000 Ədəd Qiyməti, Minimum/Maximum, Tamamlanma Vaxtı,
    Açıqlama (boş) və Al! düyməsi. Al! düyməsinə basdıqda həmin məhsulun
    kateqoriyası və özü "Yeni Sifariş" formunda avtomatik seçili şəkildə açılır.
    Axtarış canlı (səhifə yenilənmədən, JS ilə) işləyir."""
    rows = ""
    for p in prods:
        name = str(p["name"] or "")
        pid = p["product_id"]
        cat = str(p["category"] or "")
        price = float(p["price"] or 0)
        price_txt = f"{price:.4f}".rstrip("0").rstrip(".") if price < 1 else f"{price:.2f}"
        min_q = p["min_qty"]; max_q = p["max_qty"]
        buy_url = f"/?category={quote(cat)}&product_id={pid}"
        name_attr = name.replace('"', '&quot;').lower()
        rows += f"""<tr class="svc-row" data-q="{name_attr} {pid}">
<td class="svc-id">{pid}</td>
<td class="svc-name">{name}</td>
<td><span class="svc-pill svc-pill-price">{wico(13,"#fff",5)}{price_txt} <span class="svc-pill-sub">1000 Ədəd Qiyməti</span></span></td>
<td><span class="svc-pill svc-pill-mm">{min_q} / {max_q}</span></td>
<td class="svc-eta">Not enough data</td>
<td class="svc-desc"></td>
<td><a href="{buy_url}" class="btn-al">{wico(13,"#fff",5)}Al!</a></td>
</tr>"""
    if not rows:
        rows = f"""<tr><td colspan="7" style="padding:0;border:none;">{empty_state("📭","Hələ xidmət yoxdur","")}</td></tr>"""

    body = f"""<section class="wrap" style="padding:24px 0 60px;">
  <div class="table-card">
    <div class="search-bar" style="margin:16px;">
      <span class="search-bar-ic">{wico(18)}</span>
      <input type="text" id="svc-search-input" oninput="filterServicesTable(this.value)" placeholder="Xidmət axtar...">
    </div>
    <div style="overflow-x:auto;">
    <table class="ot svc-tbl">
      <thead><tr>
        <th>ID</th><th>Servis</th><th>1000 Ədəd Qiyməti</th><th>Minimum/Maximum</th><th>Tamamlanma Vaxtı</th><th>Açıqlama</th><th>Al!</th>
      </tr></thead>
      <tbody id="svc-tbl-body">{rows}</tbody>
    </table>
    </div>
    <div id="svc-empty" style="display:none;">{empty_state("🔍","Xidmət tapılmadı","Axtarışınıza uyğun xidmət tapılmadı, başqa açar söz yoxlayın.")}</div>
  </div>
</section>
<script>
function filterServicesTable(q){{
  q=(q||'').trim().toLowerCase();
  var rows=document.querySelectorAll('#svc-tbl-body tr.svc-row');
  var visible=0;
  rows.forEach(function(r){{
    var match = !q || (r.dataset.q||'').indexOf(q)>-1;
    r.style.display = match ? '' : 'none';
    if(match) visible++;
  }});
  var empty=document.getElementById('svc-empty');
  if(empty) empty.style.display = (rows.length && visible===0) ? '' : 'none';
}}
</script>"""
    return page_auth("Servislər", body, user, bal_info, "services")

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
            pname_lc = (pname or "").lower()
            prod_supports_cancel = "ləğv" in pname_lc or "legv" in pname_lc
            prod_supports_restore = "bərpa" in pname_lc or "berpa" in pname_lc
            can_cancel = status=="Gözləmədə" and prod_supports_cancel
            can_restore = status=="Tamamlandı" and prod_supports_restore
            btns=[]
            if can_cancel:
                btns.append(f'<button type="button" class="btn-row-action btn-row-cancel" onclick="cancelOrder(this,{oid})">Ləğv et</button>')
            if can_restore:
                btns.append(f'<button type="button" class="btn-row-action btn-row-restore" onclick="restoreOrder(this,{oid})">Bərpa et</button>')
            action_html='<div style="display:flex;gap:6px;flex-wrap:wrap;">'+"".join(btns)+'</div>' if btns else "—"
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
<td>{action_html}</td>
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
  <div class="tbl-head" style="display:grid;grid-template-columns:50px 110px 1fr 70px 80px 60px 120px 90px 60px 90px;padding:10px 12px;font-size:12px;font-weight:600;">
    <span>ID</span><span>Tarix</span><span>Link</span><span>Məbləğ</span><span>Başl.sayı</span><span>Miqdar</span><span>Xidmət Adı</span><span>Status</span><span>Qalıq</span><span>Əməliyyat</span>
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
<div class="bal-page">
{ok}
<div class="bal-grid">
<div class="bal-left">
<p style="font-size:14px;color:var(--mu);margin-bottom:16px;text-align:center;">Balans artırmaq üçün kartı seçin</p>
<div style="display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;justify-content:center;">
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
  <h3 style="font-size:15px;font-weight:700;margin-bottom:16px;text-align:center;">Ödənişi Təsdiqlə</h3>
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
<div class="bal-right">
<h3 style="font-size:15px;font-weight:700;margin:0 0 14px;">📜 Ödəniş Tarixçəsi</h3>
{_render_topup_history(history)}
</div>
</div>
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
<form method="post" action="/support/{ticket["ticket_id"]}/reply" enctype="multipart/form-data" class="chat-form" id="ticket-reply-form" data-no-ajax>
  <textarea name="message" id="ticket-reply-msg" placeholder="Mesajınızı yazın..." required></textarea>
  <div class="chat-form-row">
    <input type="file" name="attachment" id="ticket-reply-file" accept="image/jpeg,image/png,image/jpg,.pdf">
    <button class="btn-or" style="width:auto;padding:10px 22px;" type="submit" id="ticket-reply-btn">Göndər</button>
  </div>
</form>
</section>
<script>
var cw=document.getElementById('chat-wrap');
if(cw)cw.scrollTop=cw.scrollHeight;
(function(){{
  var form=document.getElementById('ticket-reply-form');
  var msgEl=document.getElementById('ticket-reply-msg');
  var fileEl=document.getElementById('ticket-reply-file');
  var btn=document.getElementById('ticket-reply-btn');
  if(!form)return;
  form.addEventListener('submit',function(e){{
    e.preventDefault();
    var text=(msgEl.value||'').trim();
    if(!text)return;
    var fd=new FormData(form);
    btn.disabled=true;
    var origTxt=btn.innerHTML;
    btn.innerHTML='<span class="spin"></span>Göndərilir...';
    fetch(form.getAttribute('action'),{{method:'POST',body:fd,headers:{{'X-Requested-With':'fetch'}},credentials:'same-origin'}})
      .then(function(r){{return r.json();}})
      .then(function(data){{
        btn.disabled=false;btn.innerHTML=origTxt;
        if(!data.ok){{ if(typeof showToast==='function')showToast(data.error||'Xəta baş verdi.','err'); return; }}
        var wrap=document.getElementById('chat-wrap');
        if(wrap){{
          var div=document.createElement('div');
          div.className='chat-msg user';
          var att=data.has_attachment?'<div style="margin-top:6px;font-size:12px;opacity:.8;">📎 Fayl əlavə edilib</div>':'';
          var dt=(data.created_at||'').slice(0,16).replace('T',' ');
          div.innerHTML='<div class="chat-bubble"></div><div class="chat-meta">Siz · '+dt+'</div>';
          div.querySelector('.chat-bubble').textContent=data.message;
          if(att)div.querySelector('.chat-bubble').insertAdjacentHTML('beforeend',att);
          wrap.appendChild(div);
          wrap.scrollTop=wrap.scrollHeight;
        }}
        msgEl.value='';
        if(fileEl)fileEl.value='';
      }})
      .catch(function(){{
        btn.disabled=false;btn.innerHTML=origTxt;
        if(typeof showToast==='function')showToast('Şəbəkə xətası. Yenidən cəhd edin.','err');
      }});
  }});
}})();
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
def root(request: Request, order_err: str = "", category: str = "", product_id: str = ""):
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
        c.execute("""SELECT o.*, COALESCE(p.name,'Silinmis xidmet') AS product_name, p.description AS product_desc
                   FROM orders o LEFT JOIN products p ON o.product_id=p.product_id
                   WHERE o.customer_email=%s ORDER BY o.order_id DESC LIMIT 6""",(user["email"],))
        recent_orders=c.fetchall()
    finally: put_conn(conn)
    by_cat={}
    for p in prods: by_cat.setdefault(p["category"],[]).append(p)
    by_cat = dict(sorted(by_cat.items(), key=lambda kv: category_sort_key(kv[0])))
    bal_info=get_balance(user["email"])
    return HTMLResponse(render_dashboard(user,bal_info,by_cat,prods,anns,recent_orders,order_err=order_err,
                                          preselect_category=category,preselect_product=product_id))

@app.get("/services", response_class=HTMLResponse)
def services_page(request: Request, q: str = ""):
    """Servislər (Xidmətlər) siyahısı — ID, 1000 ədəd qiyməti, min/max, tamamlanma
    vaxtı və "Al!" düyməsi ilə (istifadəçinin göstərdiyi nümunə panel kimi)."""
    user=current_user(request)
    if not user: return RedirectResponse(url="/")
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM products WHERE stock>0 ORDER BY category,price")
        prods=c.fetchall()
    finally: put_conn(conn)
    prods = sorted(prods, key=lambda p: category_sort_key(p["category"]))
    bal_info=get_balance(user["email"])
    return HTMLResponse(render_services(user,bal_info,prods,q=q))

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
    safe_n = safe_name(name)
    conn=get_conn()
    try:
        c=conn.cursor()
        c.execute("""INSERT INTO panel_users (email,name,password,balance,total_spent,total_orders,created_at,email_verified,verify_token,verify_sent_at)
                     VALUES (%s,%s,%s,0,0,0,%s,FALSE,%s,%s)""",
                  (email,safe_n,hash_pw(password),datetime.now().isoformat(),token,datetime.now().isoformat()))
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
    name=safe_name(info.get("name") or email)
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
        if quantity<min_q:
            msg=f"Minumum Saydan Aşağı Sifariş vermək olmaz. ({min_q})"
            if is_ajax: return JSONResponse({"ok":False,"error":msg},status_code=200)
            raise HTTPException(status_code=400, detail=msg)
        if quantity>max_q:
            msg=f"Miqdar maksimumdan çoxdur. ({max_q})"
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
                   COALESCE(p.name,'Silinmis xidmet') AS product_name, p.description AS product_desc
                   FROM orders o LEFT JOIN products p ON o.product_id=p.product_id
                   WHERE o.customer_email=%s ORDER BY o.order_id DESC""",(user["email"],))
        orders=c.fetchall()
    finally: put_conn(conn)
    return HTMLResponse(render_orders(user,get_balance(user["email"]),orders,new=bool(new)))

@app.post("/orders/cancel")
def cancel_order_route(request: Request, order_id:int=Form(...)):
    """Sifariş hələ başlamayıbsa (Gözləmədə statusunda) istifadəçi onu ləğv edə bilər:
    ödənilmiş məbləğ balansa geri qaytarılır və status 'Ləğv Edildi' olur."""
    user = current_user(request)
    is_ajax = request.headers.get("x-requested-with","")=="fetch"
    if not user:
        return JSONResponse({"ok":False,"error":"Sessiyanız bitib. Zəhmət olmasa yenidən daxil olun."},status_code=401)
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM orders WHERE order_id=%s AND customer_email=%s",(order_id,user["email"]))
        o=c.fetchone()
        if not o:
            return JSONResponse({"ok":False,"error":"Sifariş tapılmadı."},status_code=404)
        if (o.get("status") or "") != "Gözləmədə":
            return JSONResponse({"ok":False,"error":"Bu sifariş üçün ləğv əməliyyatı artıq mümkün deyil."},status_code=200)
        # PanelBaku-da hələ prosesə başlanmayıbsa, orada da legv etmeye cehd edirik (best-effort)
        prov_id = o.get("provider_order_id")
        if prov_id:
            try: panelbaku_cancel_order(prov_id)
            except Exception as e: log_error("PANELBAKU_CANCEL_FAIL", e, user["email"])
        price = float(o.get("price") or 0)
        c.execute("UPDATE orders SET status='Ləğv Edildi' WHERE order_id=%s",(order_id,))
        c.execute("UPDATE panel_users SET balance=balance+%s WHERE email=%s",(price,user["email"]))
        conn.commit()
        log_event("ORDER_CANCELLED", user["email"], f"order_id={order_id} refund={price}")
        msg="Ləğv tələbiniz dəstək xidmətinə göndərildi. Ödədiyiniz məbləğ balansınıza qaytarıldı."
        if is_ajax: return JSONResponse({"ok":True,"message":msg})
        return RedirectResponse(url="/orders",status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        conn.rollback()
        log_error("ORDER_CANCEL_FAIL", e, user["email"])
        return JSONResponse({"ok":False,"error":"Xəta baş verdi. Yenidən cəhd edin."},status_code=500)
    finally:
        put_conn(conn)

@app.post("/orders/restore")
def restore_order_route(request: Request, order_id:int=Form(...)):
    """Tamamlanmış sifariş üçün bərpa (refill) tələbi PanelBaku-ya göndərilir."""
    user = current_user(request)
    is_ajax = request.headers.get("x-requested-with","")=="fetch"
    if not user:
        return JSONResponse({"ok":False,"error":"Sessiyanız bitib. Zəhmət olmasa yenidən daxil olun."},status_code=401)
    conn=get_conn()
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM orders WHERE order_id=%s AND customer_email=%s",(order_id,user["email"]))
        o=c.fetchone()
        if not o:
            return JSONResponse({"ok":False,"error":"Sifariş tapılmadı."},status_code=404)
        if (o.get("status") or "") != "Tamamlandı":
            return JSONResponse({"ok":False,"error":"Bərpa yalnız tamamlanmış sifarişlər üçün mümkündür."},status_code=200)
        prov_id = o.get("provider_order_id")
        if prov_id:
            try: panelbaku_refill_order(prov_id)
            except Exception as e: log_error("PANELBAKU_REFILL_FAIL", e, user["email"])
        log_event("ORDER_RESTORE_REQUESTED", user["email"], f"order_id={order_id}")
        msg="Bərpa tələbiniz dəstək xidmətinə göndərildi."
        if is_ajax: return JSONResponse({"ok":True,"message":msg})
        return RedirectResponse(url="/orders",status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        conn.rollback()
        log_error("ORDER_RESTORE_FAIL", e, user["email"])
        return JSONResponse({"ok":False,"error":"Xəta baş verdi. Yenidən cəhd edin."},status_code=500)
    finally:
        put_conn(conn)

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
    if request.headers.get("X-Requested-With")=="fetch":
        return JSONResponse({"ok":True,"message":message,"has_attachment":has_att,
                              "created_at":datetime.now().isoformat()})
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
def admin_page(pw:str="", msg:str=""):
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

{f'<div style="background:#DCFCE7;border:1px solid #86EFAC;color:#166534;padding:12px 16px;border-radius:10px;margin-bottom:16px;font-weight:600;">{msg}</div>' if msg else ""}
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
<h3 style="margin-top:18px;">📥 PanelBaku-dan bütün xidmətləri idxal et</h3>
<p style="font-size:13px;color:#7c6f5f;margin-bottom:10px;">Bu düymə PanelBaku-dakı BÜTÜN xidmətləri (Service ID, ad, kateqoriya, minimum, maksimum, qiymət) birbaşa saytınıza köçürür — təkbətək əlavə etməyə ehtiyac qalmır. PanelBaku qiymətinin üzərinə sabit <b>20%</b> mənfəət əlavə olunaraq sizin satış qiymətiniz hesablanır. Artıq idxal edilmiş (eyni Service ID-li) xidmətlər təkrar əlavə olunmur — <b>əllə əlavə etdiyiniz məhsullar da adına görə tanınır və təkrar yaradılmır, əvəzinə avtomatik PanelBaku-ya bağlanır</b> ki, onların sifarişləri də avtomatik ötürülsün. Ona görə bu düyməni istənilən vaxt təhlükəsiz basa bilərsiniz.</p>
<form method="post" action="/admin/import-panelbaku?pw={pw}" onsubmit="return confirm('PanelBaku-dakı bütün xidmətlər saytınıza əlavə olunacaq (mövcud olanlar təkrarlanmayacaq, əllə əlavə etdikləriniz isə avtomatik bağlanacaq). Xidmət sayından asılı olaraq bu bir az vaxt ala bilər. Davam edilsin?');">
<button class="btn-or" style="width:auto;padding:10px 20px;" type="submit">📥 İndi idxal et (sabit 20% mənfəətlə)</button>
</form>
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
<p style="font-size:13px;color:#7c6f5f;margin-bottom:10px;">Adı və ya PanelBaku Service ID-si eyni olan (təkrarlanan) məhsullar avtomatik tapılıb, hər qrupdan yalnız ən əvvəl əlavə olunan qalır, qalanları silinir.</p>
<a href="/admin/dedupe-products?pw={pw}" onclick="return confirm('Adı və ya Service ID-si təkrarlanan bütün məhsullar silinəcək (hər qrupdan yalnız ən köhnəsi qalacaq). Bu geri qaytarıla bilməz. Davam edilsin?');" class="btn-or" style="display:inline-block;width:auto;padding:9px 18px;text-decoration:none;margin-bottom:14px;">🧹 Təkrarlanan məhsulları təmizlə</a>
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

PANELBAKU_IMPORT_MARKUP_PERCENT = 20  # Sabit mənfəət faizi — istəyə görə dəyişə bilərsiniz

@app.post("/admin/import-panelbaku")
def admin_import_panelbaku(pw:str):
    """PanelBaku-dakı BÜTÜN xidmətləri (action=services) çəkib, hələ əlavə
    edilməyənləri avtomatik məhsul olaraq saytımıza əlavə edir. Qiymətə
    sabit 20% mənfəət əlavə olunur. Admin-in ƏLLƏ əlavə etdiyi (provider_service_id-i
    olmayan) məhsullar adına görə tanınır — onlar TƏKRAR əlavə olunmur, əksinə
    avtomatik PanelBaku ID-sinə bağlanır ki, sifarişləri də avtomatik ötürülsün."""
    if pw!=ADMIN_KEY:
        log_event("ADMIN_AUTH_FAIL", "", "admin_import_panelbaku")
        raise HTTPException(403)
    markup = 1 + (PANELBAKU_IMPORT_MARKUP_PERCENT / 100.0)

    services = panelbaku_services()
    if not services or not isinstance(services, list):
        log_error("PANELBAKU_IMPORT_FAIL", "PanelBaku-dan xidmət siyahısı alına bilmədi (boş/yanlış cavab)", "")
        return RedirectResponse(url=f"/admin?pw={pw}&msg={quote('⛔ PanelBaku-dan xidmət siyahısı alına bilmədi. API açarını/şəbəkəni yoxlayın.')}",status_code=status.HTTP_303_SEE_OTHER)

    conn=get_conn()
    added=0; skipped=0; linked=0; failed=0
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT product_id,name,provider_service_id FROM products")
        all_prods = c.fetchall()
        existing_by_sid = {str(r["provider_service_id"]): r["product_id"] for r in all_prods if r["provider_service_id"]}
        # Adı ilə tanıma — admin-in ƏLLƏ (provider_service_id olmadan) əlavə etdiyi
        # 70+ məhsulu təkrar əlavə etməmək üçün. Eyni ad üçün yalnız hələ bağlanmamış
        # (provider_service_id boş olan) məhsul nəzərə alınır.
        existing_by_name = {}
        for r in all_prods:
            if not r["provider_service_id"]:
                existing_by_name.setdefault(str(r["name"] or "").strip().lower(), r["product_id"])
        for s in services:
            try:
                sid = str(s.get("service") or s.get("service_id") or s.get("id") or "").strip()
                if not sid:
                    skipped += 1
                    continue
                if sid in existing_by_sid:
                    skipped += 1
                    continue
                name = str(s.get("name") or f"Xidmət #{sid}").strip()[:255]
                name_norm = name.lower()
                if name_norm in existing_by_name:
                    # Admin bunu artıq əllə əlavə edib — təkrar yaratmaq əvəzinə
                    # mövcud məhsulu PanelBaku ID-sinə bağlayırıq.
                    pid = existing_by_name[name_norm]
                    c.execute("UPDATE products SET provider_service_id=%s WHERE product_id=%s AND provider_service_id IS NULL",(sid,pid))
                    existing_by_sid[sid] = pid
                    del existing_by_name[name_norm]
                    linked += 1
                    continue
                cat = str(s.get("category") or "Digər").strip()[:100] or "Digər"
                rate_raw = s.get("rate", s.get("price", 0))
                try: rate = float(str(rate_raw).replace(",","."))
                except Exception: rate = 0.0
                price = round(rate * markup, 4)
                try: min_qty = int(float(s.get("min") or 10))
                except Exception: min_qty = 10
                try: max_qty = int(float(s.get("max") or 100000))
                except Exception: max_qty = 100000
                c.execute("""INSERT INTO products (name,category,price,min_qty,max_qty,stock,description,provider_service_id)
                             VALUES (%s,%s,%s,%s,%s,999,%s,%s)""",
                          (name, cat, price, min_qty, max_qty, None, sid))
                existing_by_sid[sid] = None
                added += 1
            except Exception as e:
                failed += 1
                log_error("PANELBAKU_IMPORT_ROW_FAIL", e, "")
        conn.commit()
        log_event("ADMIN_PANELBAKU_IMPORT", "", f"added={added} linked={linked} skipped={skipped} failed={failed} markup={PANELBAKU_IMPORT_MARKUP_PERCENT}%")
    finally: put_conn(conn)
    msg = f"✅ İdxal tamamlandı: {added} yeni xidmət əlavə olundu, {linked} artıq mövcud (əllə əlavə etdiyiniz) məhsul avtomatik PanelBaku-ya bağlandı, {skipped} təkrar idi." + (f" ({failed} sətirdə xəta oldu)" if failed else "")
    return RedirectResponse(url=f"/admin?pw={pw}&msg={quote(msg)}",status_code=status.HTTP_303_SEE_OTHER)

@app.get("/admin/dedupe-products")
def admin_dedupe_products(pw:str):
    """Adı və ya PanelBaku Service ID-si eyni olan (təkrarlanan) məhsulları
    tapıb silir — hər təkrar qrupdan yalnız ən əvvəl əlavə olunan (ən kiçik
    product_id) qalır."""
    if pw!=ADMIN_KEY:
        log_event("ADMIN_AUTH_FAIL", "", "admin_dedupe_products")
        raise HTTPException(403)
    conn=get_conn()
    removed=0
    try:
        c=conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT product_id,name,provider_service_id FROM products ORDER BY product_id")
        rows=c.fetchall()
        seen_sid={}
        seen_name={}
        to_delete=[]
        for r in rows:
            pid=r["product_id"]
            sid=str(r["provider_service_id"]).strip() if r["provider_service_id"] else ""
            nm=str(r["name"] or "").strip().lower()
            dup=False
            if sid:
                if sid in seen_sid: dup=True
                else: seen_sid[sid]=pid
            if not dup and nm:
                if nm in seen_name: dup=True
                else: seen_name[nm]=pid
            if dup: to_delete.append(pid)
        if to_delete:
            c.execute("DELETE FROM products WHERE product_id = ANY(%s)", (to_delete,))
            conn.commit()
            removed=len(to_delete)
        log_event("ADMIN_DEDUPE_PRODUCTS", "", f"removed={removed}")
    finally: put_conn(conn)
    msg = f"🧹 Təkrarlanan {removed} məhsul silindi." if removed else "✅ Təkrarlanan məhsul tapılmadı, hər şey təmizdir."
    return RedirectResponse(url=f"/admin?pw={pw}&msg={quote(msg)}",status_code=status.HTTP_303_SEE_OTHER)

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
