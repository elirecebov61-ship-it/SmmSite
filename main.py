import os
from datetime import datetime

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Request, Form, HTTPException, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/eren_smm")

app = FastAPI(title="SMM Panel")

# Railway/Git bəzən boş alt-qovluqları (məs. static/js) izləmir, ona görə
# qovluq yoxdursa serveri çökdürmək əvəzinə avtomatik yaradırıq.
os.makedirs("static/css", exist_ok=True)
os.makedirs("static/js", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)


def get_conn():
    return db_pool.getconn()


def put_conn(conn):
    db_pool.putconn(conn)


def init_db():
    """Reuses the same table layout as the Telegram bot, so the panel and the
    bot can share one PostgreSQL database without migration headaches."""
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


@app.get("/")
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

    return templates.TemplateResponse(
        "index.html", {"request": request, "categories": by_category}
    )


@app.post("/order")
def create_order(
    request: Request,
    product_id: int = Form(...),
    quantity: int = Form(...),
    profile_link: str = Form(...),
    telegram_username: str = Form(""),
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


@app.get("/order/{order_id}")
def order_status(request: Request, order_id: int):
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

    return templates.TemplateResponse(
        "order_status.html", {"request": request, "order": order}
    )

