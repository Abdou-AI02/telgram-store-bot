# bot_app.py
# =========================================
# Telegram Shop Bot with:
# - Smart Notifications (segmentation, scheduling, delivery tracking, event triggers)
# - Advanced Search (filters, sort, highlight)
# - Nested Categories (parent/child) + "button inside button"
# - Category buttons when adding a product
# =========================================

import asyncio
import os
import logging
import sys
import secrets
import json
import re
import aiohttp
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from dotenv import load_dotenv
import aiosqlite
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# -----------------------------------------
# Configuration
# -----------------------------------------
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMINS = [int(x) for x in os.getenv("ADMINS", "").replace(" ", "").split(",") if x]
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "YOUR_TELEGRAM_USERNAME")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/database.sqlite")
DEFAULT_CURRENCY = "USD"
DZD_TO_USD_RATE = 250
POINTS_PER_DOLLAR = 1000
REFERRAL_BONUS_POINTS = 100
REFEREE_BONUS_POINTS = 50
REFERRAL_PURCHASE_BONUS_POINTS = 100
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# -----------------------------------------
# DB Helpers
# -----------------------------------------
async def get_conn():
    conn = await aiosqlite.connect(DATABASE_PATH)
    conn.row_factory = aiosqlite.Row
    return conn

def rows_to_list(rows):
    return [{k: row[k] for k in row.keys()} for row in rows]

async def init_db():
    os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
    conn = await get_conn()
    await conn.executescript("""
    PRAGMA foreign_keys = ON;

    CREATE TABLE IF NOT EXISTS users (
      user_id INTEGER PRIMARY KEY,
      first_name TEXT,
      points INTEGER DEFAULT 0,
      referrals INTEGER DEFAULT 0,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      ref_code TEXT,
      referred_by INTEGER,
      role TEXT DEFAULT 'user',
      last_daily_task TEXT,
      last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS products (
      product_id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      price REAL NOT NULL,
      stock INTEGER DEFAULT 0,
      category TEXT,
      subcategory TEXT,
      description TEXT,
      file_url TEXT
    );

    CREATE TABLE IF NOT EXISTS cart (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      product_id INTEGER,
      quantity INTEGER,
      FOREIGN KEY(user_id) REFERENCES users(user_id),
      FOREIGN KEY(product_id) REFERENCES products(product_id)
    );

    CREATE TABLE IF NOT EXISTS orders (
      order_id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      status TEXT DEFAULT 'pending',
      total REAL,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS order_items (
      order_id INTEGER,
      product_id INTEGER,
      quantity INTEGER,
      PRIMARY KEY (order_id, product_id),
      FOREIGN KEY(order_id) REFERENCES orders(order_id),
      FOREIGN KEY(product_id) REFERENCES products(product_id)
    );

    CREATE TABLE IF NOT EXISTS payments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      order_id INTEGER,
      payment_method TEXT,
      payment_code TEXT,
      status TEXT DEFAULT 'pending',
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS coupons (
      code TEXT PRIMARY KEY,
      discount REAL,
      is_active INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS payment_methods (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT,
      details TEXT
    );

    -- Nested categories storage
    CREATE TABLE IF NOT EXISTS categories (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      parent_id INTEGER,
      UNIQUE(name, parent_id)
    );

    -- Smart notifications
    CREATE TABLE IF NOT EXISTS notifications (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      text TEXT NOT NULL,
      segment TEXT DEFAULT 'all',    -- all | recent | buyers | inactive | custom
      custom_user_ids TEXT,          -- comma separated
      schedule_at TIMESTAMP,         -- nullable (immediate if NULL)
      status TEXT DEFAULT 'queued'   -- queued | sent | partial | failed
    );

    CREATE TABLE IF NOT EXISTS notification_deliveries (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      notification_id INTEGER,
      user_id INTEGER,
      delivered_at TIMESTAMP,
      success INTEGER,
      error TEXT
    );
    """)
    await conn.commit()
    await conn.close()
    await create_sample_products()
    await ensure_default_categories()

async def ensure_default_categories():
    conn = await get_conn()
    # Insert a root category if none
    await conn.execute("INSERT OR IGNORE INTO categories(id, name, parent_id) VALUES (1, 'عام', NULL)")
    await conn.commit()
    await conn.close()

async def create_sample_products():
    conn = await get_conn()
    cur = await conn.execute("SELECT COUNT(*) as c FROM products")
    r = await cur.fetchone()
    if r and r["c"] == 0:
        await conn.execute(
            "INSERT INTO products(name, price, stock, category, subcategory, description, file_url) VALUES (?,?,?,?,?,?,?)",
            ("Python Course", 19.99, 100, "دورات", "برمجة", "دورة بايثون للمبتدئين", "https://example.com/python-course.pdf")
        )
        await conn.execute(
            "INSERT INTO products(name, price, stock, category, subcategory, description, file_url) VALUES (?,?,?,?,?,?,?)",
            ("Support Info", 2.99, 9999, "مستندات", "دعم", "معلومات الدعم الفني", "https://example.com/support-info.txt")
        )
        await conn.execute(
            "INSERT INTO products(name, price, stock, category, subcategory, description, file_url) VALUES (?,?,?,?,?,?,?)",
            ("AI Intro", 49.99, 50, "دورات", "ذكاء اصطناعي", "مقدمة الذكاء الاصطناعي", "https://example.com/ai-intro.pdf")
        )
        await conn.commit()
    await conn.close()

# -----------------------------------------
# Users
# -----------------------------------------
async def create_user_if_not_exists(user_id: int, first_name: str, referred_by_id: int | None = None):
    conn = await get_conn()
    cur = await conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if not await cur.fetchone():
        ref_code = secrets.token_hex(4)
        role = "owner" if user_id in ADMINS else "user"
        await conn.execute(
            "INSERT INTO users(user_id, first_name, ref_code, referred_by, role) VALUES (?,?,?,?,?)",
            (user_id, first_name, ref_code, referred_by_id, role)
        )
        if referred_by_id:
            await conn.execute("UPDATE users SET referrals = referrals + 1, points = points + ? WHERE user_id=?",
                               (REFERRAL_BONUS_POINTS, referred_by_id))
            await conn.execute("UPDATE users SET points = points + ? WHERE user_id=?",
                               (REFEREE_BONUS_POINTS, user_id))
        await conn.commit()
    await conn.close()

async def get_user_data(user_id: int):
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = await cur.fetchone()
    await conn.close()
    return user

async def touch_user_activity(user_id: int):
    conn = await get_conn()
    await conn.execute("UPDATE users SET last_active=CURRENT_TIMESTAMP WHERE user_id=?", (user_id,))
    await conn.commit()
    await conn.close()

async def add_points(user_id: int, points: int):
    conn = await get_conn()
    await conn.execute("UPDATE users SET points = points + ? WHERE user_id=?", (points, user_id))
    await conn.commit()
    await conn.close()

async def deduct_points(user_id: int, points: int):
    conn = await get_conn()
    await conn.execute("UPDATE users SET points = points - ? WHERE user_id=?", (points, user_id))
    await conn.commit()
    await conn.close()

async def update_last_daily_task(user_id: int):
    conn = await get_conn()
    await conn.execute("UPDATE users SET last_daily_task=CURRENT_TIMESTAMP WHERE user_id=?", (user_id,))
    await conn.commit()
    await conn.close()

# -----------------------------------------
# Products and Categories
# -----------------------------------------
async def list_products(category: str | None = None, subcategory: str | None = None):
    conn = await get_conn()
    if category and subcategory:
        cur = await conn.execute(
            "SELECT * FROM products WHERE category=? AND subcategory=? ORDER BY product_id",
            (category, subcategory)
        )
    elif category:
        cur = await conn.execute(
            "SELECT * FROM products WHERE category=? ORDER BY product_id",
            (category,)
        )
    else:
        cur = await conn.execute("SELECT * FROM products ORDER BY product_id")
    rows = await cur.fetchall()
    await conn.close()
    return rows_to_list(rows)

async def get_product_by_id(product_id: int):
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM products WHERE product_id=?", (product_id,))
    product = await cur.fetchone()
    await conn.close()
    return product

async def get_all_categories_flat():
    conn = await get_conn()
    cur = await conn.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category<>''")
    rows = await cur.fetchall()
    await conn.close()
    return [row["category"] for row in rows]

async def get_nested_categories(parent_id: int | None = None):
    conn = await get_conn()
    if parent_id is None:
        cur = await conn.execute("SELECT * FROM categories WHERE parent_id IS NULL ORDER BY name")
    else:
        cur = await conn.execute("SELECT * FROM categories WHERE parent_id=? ORDER BY name", (parent_id,))
    rows = await cur.fetchall()
    await conn.close()
    return rows_to_list(rows)

async def add_category(name: str, parent_id: int | None):
    conn = await get_conn()
    await conn.execute("INSERT OR IGNORE INTO categories(name, parent_id) VALUES (?,?)", (name, parent_id))
    await conn.commit()
    await conn.close()

async def rename_category(old_id: int, new_name: str):
    conn = await get_conn()
    await conn.execute("UPDATE categories SET name=? WHERE id=?", (new_name, old_id))
    await conn.commit()
    await conn.close()

async def delete_category(cat_id: int):
    conn = await get_conn()
    # Cascade delete children
    await conn.execute("DELETE FROM categories WHERE id IN (WITH RECURSIVE cte(id) AS (SELECT ? UNION ALL SELECT c.id FROM categories c JOIN cte ON c.parent_id=cte.id) SELECT id FROM cte)", (cat_id,))
    await conn.commit()
    await conn.close()

async def map_product_category(cat_id: int):
    # helper to get full path (parent->child)
    conn = await get_conn()
    cur = await conn.execute("WITH RECURSIVE cte(id, name, parent_id) AS (SELECT id, name, parent_id FROM categories WHERE id=? UNION ALL SELECT c.id, c.name, c.parent_id FROM categories c JOIN cte ON c.id=cte.parent_id) SELECT id,name,parent_id FROM cte", (cat_id,))
    rows = await cur.fetchall()
    await conn.close()
    # rows contains path upward; last is root
    names = [r["name"] for r in rows]
    # Make category = root, subcategory = leaf if any
    if len(names) == 1:
        return names[0], None
    return names[-1], names[0]

async def add_product_db(name: str, price: float, stock: int, category: str, subcategory: str | None, description: str, file_url: str):
    conn = await get_conn()
    await conn.execute(
        "INSERT INTO products(name, price, stock, category, subcategory, description, file_url) VALUES (?,?,?,?,?,?,?)",
        (name, price, stock, category, subcategory, description, file_url)
    )
    await conn.commit()
    await conn.close()

async def edit_product_db(product_id: int, name: str, price: float, stock: int, category: str, subcategory: str | None, description: str):
    conn = await get_conn()
    await conn.execute(
        "UPDATE products SET name=?, price=?, stock=?, category=?, subcategory=?, description=? WHERE product_id=?",
        (name, price, stock, category, subcategory, description, product_id)
    )
    await conn.commit()
    await conn.close()

async def delete_product_db(product_id: int):
    conn = await get_conn()
    await conn.execute("DELETE FROM products WHERE product_id=?", (product_id,))
    await conn.commit()
    await conn.close()

# -----------------------------------------
# Cart / Orders / Payments / Coupons
# -----------------------------------------
async def add_to_cart(user_id, product_id, quantity=1):
    conn = await get_conn()
    cur = await conn.execute("SELECT id, quantity FROM cart WHERE user_id=? AND product_id=?", (user_id, product_id))
    r = await cur.fetchone()
    if r:
        await conn.execute("UPDATE cart SET quantity=? WHERE id=?", (r["quantity"] + quantity, r["id"]))
    else:
        await conn.execute("INSERT INTO cart(user_id, product_id, quantity) VALUES (?,?,?)", (user_id, product_id, quantity))
    await conn.commit()
    await conn.close()

async def get_cart_items(user_id):
    conn = await get_conn()
    cur = await conn.execute("""
        SELECT c.id, c.product_id, c.quantity, p.name, p.price, p.file_url
        FROM cart c JOIN products p ON c.product_id = p.product_id
        WHERE c.user_id=?
    """, (user_id,))
    rows = await cur.fetchall()
    await conn.close()
    return rows_to_list(rows)

async def clear_cart(user_id):
    conn = await get_conn()
    await conn.execute("DELETE FROM cart WHERE user_id=?", (user_id,))
    await conn.commit()
    await conn.close()

async def create_order(user_id, payment_method, payment_code=None):
    items = await get_cart_items(user_id)
    if not items:
        return None
    total = sum(item["price"] * item["quantity"] for item in items)
    conn = await get_conn()
    cur = await conn.execute("INSERT INTO orders(user_id, total) VALUES (?,?)", (user_id, total))
    order_id = cur.lastrowid
    for item in items:
        await conn.execute("INSERT INTO order_items(order_id, product_id, quantity) VALUES (?,?,?)",
                           (order_id, item["product_id"], item["quantity"]))
    await conn.execute("INSERT INTO payments(order_id, payment_method, payment_code, status) VALUES (?,?,?,?)",
                       (order_id, payment_method, payment_code, "pending"))
    await conn.commit()
    await conn.close()
    return order_id

async def get_order_items(order_id):
    conn = await get_conn()
    cur = await conn.execute("""
      SELECT p.product_id, p.name, p.price, p.file_url, p.category, p.subcategory, oi.quantity
      FROM order_items oi JOIN products p ON oi.product_id = p.product_id
      WHERE oi.order_id=?
    """, (order_id,))
    rows = await cur.fetchall()
    await conn.close()
    return rows_to_list(rows)

async def list_user_orders(user_id):
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC", (user_id,))
    rows = await cur.fetchall()
    await conn.close()
    return rows_to_list(rows)

async def list_pending_orders():
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM orders WHERE status='pending' ORDER BY created_at DESC")
    rows = await cur.fetchall()
    await conn.close()
    return rows_to_list(rows)

async def update_order_status(order_id, status):
    conn = await get_conn()
    await conn.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
    await conn.commit()
    await conn.close()

async def update_payment_status(order_id, status):
    conn = await get_conn()
    await conn.execute("UPDATE payments SET status=? WHERE order_id=?", (status, order_id))
    await conn.commit()
    await conn.close()

async def apply_coupon_db(code):
    conn = await get_conn()
    cur = await conn.execute("SELECT discount FROM coupons WHERE code=? AND is_active=1", (code,))
    r = await cur.fetchone()
    await conn.close()
    return r["discount"] if r else None

async def add_coupon_db(code: str, discount: float):
    conn = await get_conn()
    await conn.execute("INSERT INTO coupons(code, discount, is_active) VALUES (?,?,?)", (code, discount, 1))
    await conn.commit()
    await conn.close()

async def get_coupon_db(code: str):
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM coupons WHERE code=?", (code,))
    coupon = await cur.fetchone()
    await conn.close()
    return coupon

async def delete_coupon_db(code: str):
    conn = await get_conn()
    await conn.execute("DELETE FROM coupons WHERE code=?", (code,))
    await conn.commit()
    await conn.close()

async def list_coupons_db():
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM coupons")
    rows = await cur.fetchall()
    await conn.close()
    return rows_to_list(rows)

# -----------------------------------------
# Admin dashboards / stats
# -----------------------------------------
async def get_total_sales_db():
    conn = await get_conn()
    cur = await conn.execute("SELECT SUM(total) FROM orders WHERE status='completed'")
    totalsales = await cur.fetchone()
    await conn.close()
    return totalsales[0] if totalsales and totalsales[0] else 0

async def get_total_orders_db():
    conn = await get_conn()
    cur = await conn.execute("SELECT COUNT(order_id) FROM orders")
    totalorders = await cur.fetchone()
    await conn.close()
    return totalorders[0] if totalorders and totalorders[0] else 0

async def get_user_by_id(user_id: int):
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = await cur.fetchone()
    await conn.close()
    return user

async def add_user_points_db(user_id: int, points: int):
    await add_points(user_id, points)

async def deduct_user_points_db(user_id: int, points: int):
    await deduct_points(user_id, points)

async def add_payment_method_db(name: str, details: str):
    conn = await get_conn()
    await conn.execute("INSERT INTO payment_methods(name, details) VALUES (?,?)", (name, details))
    await conn.commit()
    await conn.close()

async def list_payment_methods_db():
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM payment_methods")
    rows = await cur.fetchall()
    await conn.close()
    return rows_to_list(rows)

async def delete_payment_method_db(method_id: int):
    conn = await get_conn()
    await conn.execute("DELETE FROM payment_methods WHERE id=?", (method_id,))
    await conn.commit()
    await conn.close()

async def get_payment_by_code(code: str):
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM payments WHERE payment_code=?", (code,))
    payment = await cur.fetchone()
    await conn.close()
    return payment

async def get_order_by_id(order_id: int):
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,))
    order = await cur.fetchone()
    await conn.close()
    return order

async def update_user_role(user_id: int, role: str):
    conn = await get_conn()
    await conn.execute("UPDATE users SET role=? WHERE user_id=?", (role, user_id))
    await conn.commit()
    await conn.close()

async def get_most_popular_products():
    conn = await get_conn()
    cur = await conn.execute("""
        SELECT p.name, SUM(oi.quantity) as total_sold
        FROM order_items oi JOIN products p ON oi.product_id = p.product_id
        GROUP BY p.product_id
        ORDER BY total_sold DESC
        LIMIT 5
    """)
    rows = await cur.fetchall()
    await conn.close()
    return rows_to_list(rows)

async def get_most_active_users():
    conn = await get_conn()
    cur = await conn.execute("""
        SELECT first_name, COUNT(o.order_id) as total_orders
        FROM orders o JOIN users u ON o.user_id = u.user_id
        GROUP BY u.user_id
        ORDER BY total_orders DESC
        LIMIT 5
    """)
    rows = await cur.fetchall()
    await conn.close()
    return rows_to_list(rows)

async def get_referral_sources_stats():
    conn = await get_conn()
    cur = await conn.execute("""
        SELECT u.first_name, COUNT(r.user_id) as total_referrals
        FROM users u JOIN users r ON u.user_id = r.referred_by
        GROUP BY u.user_id
        ORDER BY total_referrals DESC
        LIMIT 5
    """)
    rows = await cur.fetchall()
    await conn.close()
    return rows_to_list(rows)

# -----------------------------------------
# AI Extraction for product from text
# -----------------------------------------
async def generate_product_data_with_ai(user_text: str) -> dict | None:
    prompt = (
        "Extract product JSON with fields: name (string), price (number), "
        "category (string), description (string), file_url (string). "
        "Respond only JSON. Input:\n" + user_text
    )
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not found in .env file.")
        return None
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key={GEMINI_API_KEY}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json"
            }
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=json.dumps(payload)) as response:
                if response.status != 200:
                    logger.error(f"API call failed with status {response.status}")
                    return None
                result = await response.json()
        if "candidates" in result and len(result["candidates"]) > 0:
            json_str = result["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(json_str)
        return None
    except Exception as e:
        logger.error(f"AI generation failed: {e}")
        return None

# -----------------------------------------
# FSM States
# -----------------------------------------
router = Router()

class AddProductState(StatesGroup):
    name = State()
    price = State()
    stock = State()
    category_pick = State()  # NEW: pick category via buttons
    subcategory_pick = State()  # NEW: pick subcategory via buttons (optional)
    description = State()
    fileurl = State()

class AddProductAIState(StatesGroup):
    waiting_for_text = State()
    confirm_data = State()
    category_pick = State()
    subcategory_pick = State()

class EditAIProductState(StatesGroup):
    name = State()
    price = State()
    stock = State()
    category = State()
    description = State()
    fileurl = State()

class EditProductState(StatesGroup):
    productid = State()
    name = State()
    price = State()
    stock = State()
    category = State()
    description = State()

class DeleteProductState(StatesGroup):
    productid = State()

class AddCouponState(StatesGroup):
    code = State()
    discount = State()

class DeleteCouponState(StatesGroup):
    code = State()

class AddPointsState(StatesGroup):
    userid = State()
    points = State()

class DeductPointsState(StatesGroup):
    userid = State()
    points = State()

class GetUserInfoState(StatesGroup):
    userid = State()

class AddPaymentState(StatesGroup):
    name = State()
    details = State()

class DeletePaymentState(StatesGroup):
    id = State()

class VerifyPaymentState(StatesGroup):
    code = State()

class ManageRolesState(StatesGroup):
    userid = State()
    role = State()

class ViewOrderDetailsState(StatesGroup):
    orderid = State()

class ApplyCouponState(StatesGroup):
    waitingforcode = State()

class ManageStoreState(StatesGroup):
    action = State()
    categoryname = State()
    oldcategoryname = State()

class NotifyUsersState(StatesGroup):
    messagetext = State()
    target = State()
    schedule = State()  # NEW: scheduling
    custom_ids = State()  # NEW: custom segment ids

class SearchState(StatesGroup):
    q = State()
    filter_price_min = State()
    filter_price_max = State()
    filter_stock = State()
    sort = State()

# -----------------------------------------
# Keyboards
# -----------------------------------------
main_kb_user = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="المتجر")],
        [KeyboardButton(text="طلباتي")],
        [KeyboardButton(text="السلة")],
        [KeyboardButton(text="حسابي")],
        [KeyboardButton(text="مهام يومية")]
    ],
    resize_keyboard=True,
    input_field_placeholder="اختر من القائمة"
)

main_kb_admin = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="لوحة الإدارة")],
        [KeyboardButton(text="إرسال إشعارات")],
        [KeyboardButton(text="إدارة المنتجات")],
        [KeyboardButton(text="إدارة القسائم")],
        [KeyboardButton(text="إدارة المستخدمين")],
        [KeyboardButton(text="إدارة المدفوعات")],
        [KeyboardButton(text="إدارة المتجر")],
        [KeyboardButton(text="إحصائيات")],
        [KeyboardButton(text="الطلبات")]
    ],
    resize_keyboard=True,
    input_field_placeholder="لوحة الإدارة"
)

owner_panel_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="لوحة الإدارة")],
        [KeyboardButton(text="تبديل إلى المستخدم")],
        [KeyboardButton(text="إضافة منتج بالذكاء")],
        [KeyboardButton(text="إدارة الأدوار")],
        [KeyboardButton(text="إدارة المتجر")],
        [KeyboardButton(text="إرسال إشعارات")]
    ],
    resize_keyboard=True,
    input_field_placeholder="لوحة المالك"
)

manage_products_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="إضافة منتج"), KeyboardButton(text="تعديل منتج"), KeyboardButton(text="حذف منتج")],
        [KeyboardButton(text="قائمة المنتجات")],
        [KeyboardButton(text="رجوع للقائمة")]
    ],
    resize_keyboard=True,
    input_field_placeholder="إدارة المنتجات"
)

manage_coupons_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="إضافة قسيمة"), KeyboardButton(text="حذف قسيمة")],
        [KeyboardButton(text="قائمة القسائم")],
        [KeyboardButton(text="رجوع للقائمة")]
    ],
    resize_keyboard=True,
    input_field_placeholder="إدارة القسائم"
)

manage_users_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="إضافة نقاط"), KeyboardButton(text="خصم نقاط")],
        [KeyboardButton(text="معلومات مستخدم")],
        [KeyboardButton(text="رجوع للقائمة")]
    ],
    resize_keyboard=True
)

manage_payments_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="إضافة طريقة دفع"), KeyboardButton(text="حذف طريقة دفع")],
        [KeyboardButton(text="قائمة طرق الدفع")],
        [KeyboardButton(text="التحقق من الدفع")],
        [KeyboardButton(text="رجوع للقائمة")]
    ],
    resize_keyboard=True
)

manage_roles_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="تعيين مشرف", callback_data="setrole:admin")],
    [InlineKeyboardButton(text="تعيين مستخدم", callback_data="setrole:user")]
])

manage_store_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="إضافة فئة"), KeyboardButton(text="تعديل فئة"), KeyboardButton(text="حذف فئة")],
        [KeyboardButton(text="عرض الفئات")],
        [KeyboardButton(text="رجوع للقائمة")]
    ],
    resize_keyboard=True
)

notify_users_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="لكل المستخدمين", callback_data="notify:all"),
     InlineKeyboardButton(text="آخر المتفاعلين", callback_data="notify:recent")],
    [InlineKeyboardButton(text="الذين اشتروا", callback_data="notify:buyers"),
     InlineKeyboardButton(text="غير النشطين", callback_data="notify:inactive")],
    [InlineKeyboardButton(text="مُخصص IDs", callback_data="notify:custom")],
    [InlineKeyboardButton(text="جدولة لاحقاً", callback_data="notify:schedule")]
])

# -----------------------------------------
# Helpers: keyboards for nested categories
# -----------------------------------------
async def build_category_menu(parent_id: int | None = None, for_pick=False, back_payload=None):
    cats = await get_nested_categories(parent_id)
    buttons = []
    for c in cats:
        payload = f"cat:open:{c['id']}" if not for_pick else f"catpick:open:{c['id']}"
        buttons.append([InlineKeyboardButton(text=c["name"], callback_data=payload)])
    # Add action buttons
    if parent_id is not None:
        buttons.append([InlineKeyboardButton(text="◀️ رجوع", callback_data=back_payload or "cat:open:root")])
    if not buttons:
        buttons.append([InlineKeyboardButton(text="لا توجد فئات", callback_data="noop")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def build_category_pick_menu(parent_id: int | None = None, picked_path: list[int] | None = None):
    cats = await get_nested_categories(parent_id)
    buttons = []
    for c in cats:
        buttons.append([InlineKeyboardButton(text=f"📁 {c['name']}", callback_data=f"catpick:open:{c['id']}")])
    if parent_id is not None:
        buttons.append([InlineKeyboardButton(text="تحديد هذه الفئة", callback_data=f"catpick:choose:{parent_id}")])
        buttons.append([InlineKeyboardButton(text="◀️ رجوع", callback_data="catpick:open:root")])
    elif not cats:
        buttons.append([InlineKeyboardButton(text="لا توجد فئات", callback_data="noop")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# -----------------------------------------
# Shop views
# -----------------------------------------
async def show_categories(message_or_callback, is_edit=False, parent_id: int | None = None):
    kb = await build_category_menu(parent_id=parent_id, for_pick=False, back_payload="cat:open:root")
    text = "اختر فئة:"
    if isinstance(message_or_callback, types.CallbackQuery):
        if is_edit:
            await message_or_callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await message_or_callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await message_or_callback.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)

async def show_products_in_category(callback: types.CallbackQuery, category: str | None, subcategory: str | None):
    products = await list_products(category, subcategory)
    if not products:
        await callback.message.edit_text("لا توجد منتجات هنا.", reply_markup=await build_category_menu(), parse_mode=ParseMode.HTML)
        await callback.answer()
        return
    kb_buttons = []
    for p in products:
        kb_buttons.append([InlineKeyboardButton(text=p["name"], callback_data=f"productdetails:{p['product_id']}")])
    kb_buttons.append([InlineKeyboardButton(text="◀️ رجوع للفئات", callback_data="cat:open:root")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    title = f"المنتجات ضمن: {category or ''} / {subcategory or ''}".strip(" /")
    await callback.message.edit_text(title, reply_markup=kb, parse_mode=ParseMode.HTML)
    await callback.answer()

# -----------------------------------------
# Advanced Search
# -----------------------------------------
def highlight(text: str, query: str) -> str:
    if not query:
        return text
    pattern = re.escape(query)
    return re.sub(pattern, lambda m: f"<b>{m.group(0)}</b>", text, flags=re.IGNORECASE)

async def search_products(q: str | None, price_min: float | None, price_max: float | None,
                          in_stock_only: bool, sort: str | None, category: str | None = None):
    clauses = []
    params = []
    if q:
        clauses.append("(LOWER(name) LIKE ? OR LOWER(description) LIKE ? OR LOWER(category) LIKE ? OR LOWER(subcategory) LIKE ?)")
        like = f"%{q.lower()}%"
        params += [like, like, like, like]
    if price_min is not None:
        clauses.append("price >= ?")
        params.append(price_min)
    if price_max is not None:
        clauses.append("price <= ?")
        params.append(price_max)
    if in_stock_only:
        clauses.append("stock > 0")
    if category:
        clauses.append("(category=? OR subcategory=?)")
        params += [category, category]
    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    order_sql = "ORDER BY "
    if sort == "price_asc":
        order_sql += "price ASC"
    elif sort == "price_desc":
        order_sql += "price DESC"
    elif sort == "stock_desc":
        order_sql += "stock DESC"
    else:
        order_sql += "product_id DESC"
    conn = await get_conn()
    cur = await conn.execute(f"SELECT * FROM products {where_sql} {order_sql}")
    rows = await cur.fetchall()
    await conn.close()
    return rows_to_list(rows)

# -----------------------------------------
# Notifications (smart)
# -----------------------------------------
async def queue_notification(text: str, segment: str = "all", schedule_at: datetime | None = None, custom_user_ids: list[int] | None = None):
    conn = await get_conn()
    ids_str = ",".join(map(str, custom_user_ids)) if custom_user_ids else None
    await conn.execute(
        "INSERT INTO notifications(text, segment, custom_user_ids, schedule_at, status) VALUES (?,?,?,?,?)",
        (text, segment, ids_str, schedule_at.isoformat() if schedule_at else None, "queued")
    )
    await conn.commit()
    await conn.close()

async def pick_segment_user_ids(segment: str, custom_user_ids: list[int] | None = None):
    conn = await get_conn()
    if segment == "all":
        cur = await conn.execute("SELECT user_id FROM users")
    elif segment == "recent":
        cur = await conn.execute("SELECT user_id FROM users ORDER BY last_active DESC LIMIT 50")
    elif segment == "buyers":
        cur = await conn.execute("SELECT DISTINCT user_id FROM orders")
    elif segment == "inactive":
        cur = await conn.execute("SELECT user_id FROM users WHERE last_active < datetime('now','-7 day')")
    elif segment == "custom":
        if custom_user_ids:
            placeholders = ",".join("?" * len(custom_user_ids))
            cur = await conn.execute(f"SELECT user_id FROM users WHERE user_id IN ({placeholders})", custom_user_ids)
        else:
            await conn.close()
            return []
    else:
        cur = await conn.execute("SELECT user_id FROM users")
    rows = await cur.fetchall()
    await conn.close()
    return [r["user_id"] for r in rows]

async def process_notifications_task(bot: Bot):
    while True:
        try:
            conn = await get_conn()
            # pick due queued notifications
            cur = await conn.execute("""
                SELECT * FROM notifications
                WHERE status='queued' AND (schedule_at IS NULL OR schedule_at <= CURRENT_TIMESTAMP)
                ORDER BY id ASC
                LIMIT 3
            """)
            notes = await cur.fetchall()
            await conn.close()
            for note in notes:
                note_id = note["id"]
                text = note["text"]
                segment = note["segment"]
                ids_list = [int(x) for x in note["custom_user_ids"].split(",")] if note["custom_user_ids"] else None
                user_ids = await pick_segment_user_ids(segment, ids_list)
                success_count = 0
                for uid in user_ids:
                    try:
                        await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
                        success = 1
                        error = None
                        success_count += 1
                    except Exception as e:
                        success = 0
                        error = str(e)
                        logger.error(f"Failed to send notification {note_id} to {uid}: {e}")
                    conn2 = await get_conn()
                    await conn2.execute(
                        "INSERT INTO notification_deliveries(notification_id, user_id, delivered_at, success, error) VALUES (?,?,?,?,?)",
                        (note_id, uid, datetime.utcnow().isoformat(), success, error)
                    )
                    await conn2.commit()
                    await conn2.close()
                # update status
                conn3 = await get_conn()
                await conn3.execute("UPDATE notifications SET status=? WHERE id=?",
                                    ("sent" if success_count == len(user_ids) else "partial", note_id))
                await conn3.commit()
                await conn3.close()
        except Exception as e:
            logger.error(f"Notification loop error: {e}")
        await asyncio.sleep(5)

# -----------------------------------------
# UI Flows
# -----------------------------------------
@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    args = message.text.split()
    referred_by_id = None
    if len(args) > 1 and args[1].startswith("ref"):
        ref_code = args[1][4:]
        conn = await get_conn()
        cur = await conn.execute("SELECT user_id FROM users WHERE ref_code=?", (ref_code,))
        referrer_row = await cur.fetchone()
        await conn.close()
        if referrer_row:
            referrer_id = referrer_row["user_id"]
            if referrer_id != message.from_user.id:
                referred_by_id = referrer_id
    await create_user_if_not_exists(message.from_user.id, message.from_user.full_name, referred_by_id)
    user = await get_user_data(message.from_user.id)
    if user["role"] == "owner":
        await message.answer(f"مرحباً {message.from_user.full_name}!", reply_markup=owner_panel_kb)
    elif user["role"] == "admin":
        await message.answer(f"مرحباً {message.from_user.full_name}!", reply_markup=main_kb_admin)
    else:
        await message.answer(f"مرحباً {message.from_user.full_name}!", reply_markup=main_kb_user)
    await touch_user_activity(message.from_user.id)

@router.message(F.text == "تبديل إلى المستخدم")
async def cmd_start_as_user(message: types.Message, state: FSMContext):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await state.clear()
    await message.answer("عرض المستخدم مفعّل.", reply_markup=main_kb_user)
    await touch_user_activity(message.from_user.id)

@router.message(F.text == "لوحة الإدارة")
async def cmd_start_as_admin(message: types.Message, state: FSMContext):
    await state.clear()
    user = await get_user_data(message.from_user.id)
    if user["role"] == "owner":
        await message.answer(f"مرحباً {message.from_user.full_name}!", reply_markup=owner_panel_kb)
    else:
        await message.answer(f"مرحباً {message.from_user.full_name}!", reply_markup=main_kb_admin)
    await touch_user_activity(message.from_user.id)

@router.message(F.text == "حسابي")
async def cmd_my_account(message: types.Message):
    user = await get_user_data(message.from_user.id)
    if not user:
        await message.answer("سجل الدخول بالأمر /start.")
        return
    bot_info = await message.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user['ref_code']}"
    text = (
        f"<b>نقاطك:</b> {user['points']}\n"
        f"<b>الإحالات:</b> {user['referrals']}\n"
        f"<b>رابط الإحالة:</b> <code>{ref_link}</code>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)
    await touch_user_activity(message.from_user.id)

@router.message(F.text == "مهام يومية")
async def cmd_daily_tasks(message: types.Message):
    user = await get_user_data(message.from_user.id)
    last_daily = user["last_daily_task"]
    if last_daily:
        last_dt = datetime.fromisoformat(last_daily)
        if datetime.now() - last_dt < timedelta(hours=24):
            await message.answer("لقد حصلت على نقاط اليوم. حاول لاحقاً!")
            return
    await add_points(message.from_user.id, 10)
    await update_last_daily_task(message.from_user.id)
    await message.answer("مهمة اليوم: زر المتجر لتحصل على 10 نقاط!")
    await touch_user_activity(message.from_user.id)

# -------- Shop ----------
@router.message(F.text == "المتجر")
async def cmd_shop(message: types.Message):
    await show_categories(message, is_edit=False, parent_id=None)
    await touch_user_activity(message.from_user.id)

@router.callback_query(F.data.startswith("cat:open:"))
async def cb_open_category(callback: types.CallbackQuery):
    code = callback.data.split(":")[-1]
    if code == "root":
        # show root
        await show_categories(callback, is_edit=True, parent_id=None)
        await callback.answer()
        return
    # fetch children; if no children then show products mapped to that name
    try:
        cat_id = int(code)
    except:
        await callback.answer()
        return
    # get cat name and parent to build title and products
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM categories WHERE id=?", (cat_id,))
    cat = await cur.fetchone()
    await conn.close()
    if not cat:
        await callback.answer()
        return
    # If has children -> open nested; else list products using category/subcategory mapping heuristic
    children = await get_nested_categories(cat_id)
    if children:
        kb = await build_category_menu(parent_id=cat_id, for_pick=False, back_payload="cat:open:root")
        await callback.message.edit_text(f"الفئة: {cat['name']}", reply_markup=kb, parse_mode=ParseMode.HTML)
        await callback.answer()
    else:
        # Use cat name as either category or subcategory match
        await show_products_in_category(callback, category=cat["name"], subcategory=None)
    await touch_user_activity(callback.from_user.id)

@router.callback_query(F.data.startswith("productdetails:"))
async def show_product_details(callback: types.CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    product = await get_product_by_id(product_id)
    if not product:
        await callback.answer("المنتج غير موجود.", show_alert=True)
        return
    text = (
        f"<b>{product['name']}</b>\n"
        f"{product['description']}\n"
        f"السعر: {product['price']:.2f} {DEFAULT_CURRENCY} / {product['price']*DZD_TO_USD_RATE:.2f} دج\n"
        f"المتوفر: {'متاح' if product['stock'] > 0 else 'غير متوفر'}"
    )
    kb_buttons = []
    if product["stock"] > 0:
        kb_buttons.append([InlineKeyboardButton(text="أضف للسلة", callback_data=f"addtocart:{product_id}")])
        kb_buttons.append([InlineKeyboardButton(text="اشتري الآن بالنقاط", callback_data=f"buynow:{product_id}")])
    if product["category"]:
        kb_buttons.append([InlineKeyboardButton(text=f"الفئة: {product['category']}", callback_data="cat:open:root")])
    kb_buttons.append([InlineKeyboardButton(text="◀️ رجوع للفئات", callback_data="cat:open:root")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await callback.answer()
    await touch_user_activity(callback.from_user.id)

@router.callback_query(F.data.startswith("addtocart:"))
async def cb_add_to_cart(callback: types.CallbackQuery):
    pid = int(callback.data.split(":")[1])
    await add_to_cart(callback.from_user.id, pid, 1)
    await callback.answer("تمت الإضافة للسلة!")
    await touch_user_activity(callback.from_user.id)

@router.callback_query(F.data.startswith("buynow:"))
async def cb_buy_now(callback: types.CallbackQuery):
    pid = int(callback.data.split(":")[1])
    items = [{"product_id": pid, "quantity": 1, "price": (await get_product_by_id(pid))["price"]}]
    total = sum(i["price"]*i["quantity"] for i in items)
    points_cost = int(total * POINTS_PER_DOLLAR)
    user = await get_user_data(callback.from_user.id)
    if user["points"] < points_cost:
        await callback.answer("نقاطك غير كافية.", show_alert=True)
        return
    await deduct_points(callback.from_user.id, points_cost)
    order_id = await create_order(callback.from_user.id, "Points")
    if order_id:
        pro = await get_product_by_id(pid)
        if pro and pro["file_url"]:
            await callback.message.answer(f"{pro['name']} {pro['file_url']}", parse_mode=ParseMode.HTML)
    await callback.message.edit_text(f"تم الشراء بنجاح! الخصم: <b>{points_cost}</b> نقطة.", parse_mode=ParseMode.HTML)
    await callback.answer("تم")
    await touch_user_activity(callback.from_user.id)

# -------- Orders / Payments ----------
@router.message(F.text == "طلباتي")
async def cmd_orders(message: types.Message):
    orders = await list_user_orders(message.from_user.id)
    if not orders:
        await message.answer("لا توجد طلبات.")
        return
    lines = []
    for o in orders:
        lines.append(f"#{o['order_id']} | الحالة: {o['status']} | الإجمالي: {o['total']:.2f} {DEFAULT_CURRENCY}")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
    await touch_user_activity(message.from_user.id)

@router.message(Command("coupon"))
async def cmd_coupon(message: types.Message, state: FSMContext):
    await state.clear()
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("استخدم: /coupon CODE")
        return
    code = parts[1]
    try:
        discount = await apply_coupon_db(code)
        if discount:
            await message.answer(f"تم تطبيق القسيمة! الخصم: {discount:.0f}%.")
            await state.update_data(coupon_discount=discount)
        else:
            await message.answer("القسيمة غير صالحة.")
    except Exception:
        await message.answer("حدث خطأ أثناء تطبيق القسيمة.")
    await touch_user_activity(message.from_user.id)

# -------- Admin Panels ----------
@router.message(F.text == "إدارة المدفوعات")
async def manage_payments_panel(message: types.Message):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("إدارة المدفوعات", reply_markup=manage_payments_kb)

@router.message(F.text == "إضافة طريقة دفع")
async def start_add_payment_method(message: types.Message, state: FSMContext):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("أرسل اسم الطريقة:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AddPaymentState.name)

@router.message(AddPaymentState.name)
async def process_add_payment_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("أرسل التفاصيل:")
    await state.set_state(AddPaymentState.details)

@router.message(AddPaymentState.details)
async def process_add_payment_details(message: types.Message, state: FSMContext):
    await state.update_data(details=message.text)
    data = await state.get_data()
    await add_payment_method_db(data["name"], data["details"])
    await message.answer(f"تمت إضافة: <b>{data['name']}</b>.", reply_markup=manage_payments_kb, parse_mode=ParseMode.HTML)
    await state.clear()

@router.message(F.text == "قائمة طرق الدفع")
async def list_payments_admin_handler(message: types.Message):
    methods = await list_payment_methods_db()
    if not methods:
        await message.answer("لا طرق دفع بعد.")
        return
    txt = "\n".join([f"{m['id']}. {m['name']}" for m in methods])
    await message.answer(txt)

@router.message(F.text == "حذف طريقة دفع")
async def start_delete_payment_method(message: types.Message, state: FSMContext):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("أرسل رقم الطريقة للحذف:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(DeletePaymentState.id)

@router.message(DeletePaymentState.id)
async def process_delete_payment(message: types.Message, state: FSMContext):
    try:
        method_id = int(message.text.strip())
        await delete_payment_method_db(method_id)
        await message.answer("تم الحذف.", reply_markup=manage_payments_kb)
    except ValueError:
        await message.answer("رقم غير صالح.")
    await state.clear()

# -------- Users ----------
@router.message(F.text == "إدارة المستخدمين")
async def manage_users_panel(message: types.Message):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("إدارة المستخدمين", reply_markup=manage_users_kb)

@router.message(F.text == "إضافة نقاط")
async def start_add_points(message: types.Message, state: FSMContext):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("أرسل ID المستخدم:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AddPointsState.userid)

@router.message(AddPointsState.userid)
async def process_add_points_user(message: types.Message, state: FSMContext):
    await state.update_data(userid=message.text.strip())
    await message.answer("أرسل عدد النقاط:")
    await state.set_state(AddPointsState.points)

@router.message(AddPointsState.points)
async def process_add_points(message: types.Message, state: FSMContext):
    data = await state.get_data()
    try:
        uid = int(data["userid"])
        pts = int(message.text.strip())
        await add_user_points_db(uid, pts)
        await message.answer("تمت إضافة النقاط.", reply_markup=manage_users_kb)
    except ValueError:
        await message.answer("إدخال غير صالح.")
    await state.clear()

@router.message(F.text == "خصم نقاط")
async def start_deduct_points(message: types.Message, state: FSMContext):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("أرسل ID المستخدم:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(DeductPointsState.userid)

@router.message(DeductPointsState.userid)
async def process_deduct_points_user(message: types.Message, state: FSMContext):
    await state.update_data(userid=message.text.strip())
    await message.answer("أرسل عدد النقاط:")
    await state.set_state(DeductPointsState.points)

@router.message(DeductPointsState.points)
async def process_deduct_points(message: types.Message, state: FSMContext):
    data = await state.get_data()
    try:
        uid = int(data["userid"])
        pts = int(message.text.strip())
        await deduct_user_points_db(uid, pts)
        await message.answer("تم خصم النقاط.", reply_markup=manage_users_kb)
    except ValueError:
        await message.answer("إدخال غير صالح.")
    await state.clear()

@router.message(F.text == "معلومات مستخدم")
async def start_get_user_info(message: types.Message, state: FSMContext):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("أرسل ID المستخدم:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(GetUserInfoState.userid)

@router.message(GetUserInfoState.userid)
async def process_get_user_info(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
        user = await get_user_by_id(uid)
        if not user:
            await message.answer(f"لا يوجد مستخدم بالرقم {uid}.")
            await state.clear()
            return
        text = (
            f"ID: {uid}\n"
            f"الاسم: {user['first_name']}\n"
            f"النقاط: {user['points']}\n"
            f"الإحالات: {user['referrals']}\n"
            f"كود الإحالة: <code>{user['ref_code']}</code>"
        )
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=manage_users_kb)
        await state.clear()
    except ValueError:
        await message.answer("أدخل رقماً صحيحاً.")

# -------- Store manage (Nested categories) ----------
@router.message(F.text == "إدارة المتجر")
async def manage_store_panel(message: types.Message):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("إدارة المتجر", reply_markup=manage_store_kb)

@router.message(F.text == "عرض الفئات")
async def show_store_categories(message: types.Message):
    kb = await build_category_menu(parent_id=None, for_pick=False)
    await message.answer("الفئات:", reply_markup=kb)

@router.message(F.text == "إضافة فئة")
async def start_add_category(message: types.Message, state: FSMContext):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    # Ask parent selection via inline
    kb = await build_category_menu(parent_id=None, for_pick=True)
    await message.answer("اختر الفئة الأب أو اترك الجذر:", reply_markup=kb)
    await state.update_data(add_cat_mode="await_parent")

@router.callback_query(F.data.startswith("catpick:open:"))
async def cb_pick_cat_open(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split(":")[-1]
    if code == "root":
        kb = await build_category_pick_menu(parent_id=None)
        await callback.message.edit_text("اختر فئة:", reply_markup=kb)
        await callback.answer()
        return
    parent_id = int(code)
    kb = await build_category_pick_menu(parent_id=parent_id)
    await callback.message.edit_text("اختر فئة:", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("catpick:choose:"))
async def cb_pick_cat_choose(callback: types.CallbackQuery, state: FSMContext):
    chosen = int(callback.data.split(":")[-1])
    data = await state.get_data()
    mode = data.get("add_cat_mode")
    if mode == "await_parent":
        await state.update_data(parent_id=chosen)
        await callback.message.edit_text("أرسل اسم الفئة الجديدة:")
        await state.set_state(ManageStoreState.categoryname)
    elif mode in ("add_product", "add_ai_product"):
        await state.update_data(cat_id=chosen)
        # After category choose, maybe pick subcategory if children exist, else confirm
        children = await get_nested_categories(chosen)
        if children:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                *[[InlineKeyboardButton(text=c["name"], callback_data=f"catpick:chooseleaf:{c['id']}")] for c in children],
                [InlineKeyboardButton(text="بدون فرع", callback_data=f"catpick:chooseleaf:none")]
            ])
            await callback.message.edit_text("اختر فئة فرعية إن وجدت:", reply_markup=kb)
            await state.set_state(AddProductState.subcategory_pick if mode=="add_product" else AddProductAIState.subcategory_pick)
        else:
            # Map to category/subcategory
            category, subcategory = await map_product_category(chosen)
            await state.update_data(category=category, subcategory=subcategory)
            if mode == "add_product":
                await callback.message.edit_text(f"تم اختيار الفئة: {category} / {subcategory or '-'}\nأرسل الوصف:")
                await state.set_state(AddProductState.description)
            else:
                # AI flow continues to confirm
                data2 = await state.get_data()
                await callback.message.edit_text(
                    f"سيتم إضافة المنتج:\n"
                    f"الاسم: {data2['name']}\nالسعر: {data2['price']}\n"
                    f"الفئة: {category} / {subcategory or '-'}\nالوصف: {data2['description']}\n"
                    f"الرابط/الملف: {data2['fileurl']}\nأرسل 'تأكيد' للإضافة."
                )
                await state.set_state(AddProductAIState.confirm_data)
    await callback.answer()

@router.message(ManageStoreState.categoryname, F.text)
async def process_add_category_name(message: types.Message, state: FSMContext):
    data = await state.get_data()
    parent_id = data.get("parent_id")
    await add_category(message.text.strip(), parent_id)
    await message.answer("تمت إضافة الفئة.", reply_markup=manage_store_kb)
    await state.clear()

@router.message(F.text == "تعديل فئة")
async def start_edit_category(message: types.Message, state: FSMContext):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    # Ask for category id to rename
    cats = await get_nested_categories(None)
    if not cats:
        await message.answer("لا توجد فئات.")
        return
    listing = []
    conn = await get_conn()
    cur = await conn.execute("SELECT id,name,parent_id FROM categories ORDER BY parent_id,name")
    rows = await cur.fetchall()
    await conn.close()
    for r in rows:
        listing.append(f"{r['id']}: {r['name']} (parent={r['parent_id']})")
    await message.answer("أرسل ID الفئة لإعادة تسميتها، ثم الاسم الجديد بسطرين:\nمثال:\n12\nاسم جديد")
    await state.set_state(ManageStoreState.oldcategoryname)

@router.message(ManageStoreState.oldcategoryname)
async def process_edit_category_id_then_name(message: types.Message, state: FSMContext):
    lines = message.text.strip().splitlines()
    if len(lines) < 2:
        await message.answer("أرسل ID في سطر والاسم الجديد في سطر آخر.")
        return
    try:
        cat_id = int(lines[0].strip())
        new_name = lines[1].strip()
        await rename_category(cat_id, new_name)
        await message.answer("تم تعديل الاسم.", reply_markup=manage_store_kb)
        await state.clear()
    except ValueError:
        await message.answer("ID غير صالح.")

@router.message(F.text == "حذف فئة")
async def start_delete_category(message: types.Message):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("أرسل ID الفئة لحذفها (سيتم حذف الفروع أيضاً).")

@router.message(F.text.regexp(r"^\d{1,10}$"))
async def process_delete_category_by_id(message: types.Message):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        return
    cat_id = int(message.text.strip())
    try:
        await delete_category(cat_id)
        await message.answer("تم حذف الفئة.")
    except Exception as e:
        await message.answer(f"تعذر الحذف: {e}")

# -------- Product Management (with category buttons on add) ----------
@router.message(F.text == "إدارة المنتجات")
async def manage_products_panel(message: types.Message):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("إدارة المنتجات", reply_markup=manage_products_kb)

@router.message(F.text == "إضافة منتج")
async def start_add_product(message: types.Message, state: FSMContext):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("أرسل اسم المنتج:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AddProductState.name)

@router.message(AddProductState.name)
async def process_product_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("أرسل السعر:")
    await state.set_state(AddProductState.price)

@router.message(AddProductState.price)
async def process_product_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text.strip())
        await state.update_data(price=price)
        await message.answer("أرسل الكمية (المخزون):")
        await state.set_state(AddProductState.stock)
    except ValueError:
        await message.answer("أدخل سعراً صحيحاً.")

@router.message(AddProductState.stock)
async def process_product_stock(message: types.Message, state: FSMContext):
    try:
        stock = int(message.text.strip())
        await state.update_data(stock=stock)
        # Show categories as buttons (nested)
        await state.update_data(add_cat_mode="add_product")
        kb = await build_category_pick_menu(parent_id=None)
        await message.answer("اختر الفئة:", reply_markup=kb)
        await state.set_state(AddProductState.category_pick)
    except ValueError:
        await message.answer("أدخل رقماً صحيحاً.")

@router.callback_query(AddProductState.subcategory_pick, F.data.startswith("catpick:chooseleaf:"))
async def cb_choose_leaf_for_product(callback: types.CallbackQuery, state: FSMContext):
    leaf_code = callback.data.split(":")[-1]
    data = await state.get_data()
    cat_id = data.get("cat_id")
    if leaf_code == "none":
        category, subcategory = await map_product_category(cat_id)
    else:
        category, subcategory = await map_product_category(int(leaf_code))
    await state.update_data(category=category, subcategory=subcategory)
    await callback.message.edit_text(f"تم اختيار: {category} / {subcategory or '-'}\nأرسل وصف المنتج:")
    await state.set_state(AddProductState.description)
    await callback.answer()

@router.message(AddProductState.description)
async def process_product_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await message.answer("أرسل الرابط (أو أرسل ملف/صورة وسيُحفظ FileID):")
    await state.set_state(AddProductState.fileurl)

@router.message(AddProductState.fileurl, F.document | F.photo | F.text)
async def process_product_file(message: types.Message, state: FSMContext):
    fileurl = message.text or ""
    if message.document:
        fileurl = message.document.file_id
    elif message.photo:
        fileurl = message.photo[-1].file_id
    data = await state.get_data()
    await add_product_db(
        data["name"], data["price"], data["stock"],
        data.get("category"), data.get("subcategory"),
        data["description"], fileurl
    )
    await message.answer(f"تمت إضافة: <b>{data['name']}</b>.", reply_markup=manage_products_kb, parse_mode=ParseMode.HTML)
    await state.clear()

# ---- AI Add Product with category pick
@router.message(F.text == "إضافة منتج بالذكاء")
async def start_add_product_ai(message: types.Message, state: FSMContext):
    user = await get_user_data(message.from_user.id)
    if user["role"] != "owner":
        await message.answer("هذه الميزة للمالك.")
        return
    await message.answer("أرسل نص تفاصيل المنتج (اسم، سعر، فئة، وصف، رابط):", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AddProductAIState.waiting_for_text)

@router.message(AddProductAIState.waiting_for_text)
async def process_product_text_ai(message: types.Message, state: FSMContext):
    await message.answer("جارٍ استخراج البيانات...")
    product_data = await generate_product_data_with_ai(message.text)
    if not product_data or not product_data.get("name") or not product_data.get("price"):
        await message.answer("تعذر استخراج البيانات. حاول يدوياً.", reply_markup=owner_panel_kb)
        await state.clear()
        return
    await state.update_data(
        name=product_data.get("name"),
        price=float(product_data.get("price")),
        stock=product_data.get("stock", 0),
        description=product_data.get("description", ""),
        fileurl=product_data.get("file_url", "")
    )
    await state.update_data(add_cat_mode="add_ai_product")
    kb = await build_category_pick_menu(parent_id=None)
    await message.answer("اختر الفئة:", reply_markup=kb)
    await state.set_state(AddProductAIState.category_pick)

@router.callback_query(AddProductAIState.subcategory_pick, F.data.startswith("catpick:chooseleaf:"))
async def cb_choose_leaf_for_product_ai(callback: types.CallbackQuery, state: FSMContext):
    leaf_code = callback.data.split(":")[-1]
    data = await state.get_data()
    cat_id = data.get("cat_id")
    if leaf_code == "none":
        category, subcategory = await map_product_category(cat_id)
    else:
        category, subcategory = await map_product_category(int(leaf_code))
    await state.update_data(category=category, subcategory=subcategory)
    data2 = await state.get_data()
    await callback.message.edit_text(
        f"سيتم إضافة المنتج:\n"
        f"الاسم: {data2['name']}\nالسعر: {data2['price']}\n"
        f"الفئة: {category} / {subcategory or '-'}\nالوصف: {data2['description']}\n"
        f"الرابط/الملف: {data2['fileurl']}\nأرسل 'تأكيد' للإضافة."
    )
    await state.set_state(AddProductAIState.confirm_data)
    await callback.answer()

@router.message(AddProductAIState.confirm_data, F.text.lower() == "تأكيد")
async def confirm_ai_product(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await add_product_db(
        data["name"], data["price"], data.get("stock", 0),
        data.get("category"), data.get("subcategory"),
        data["description"], data["fileurl"]
    )
    await message.answer("تمت إضافة المنتج.", reply_markup=owner_panel_kb)
    await state.clear()

# ------- Coupons panel -------
@router.message(F.text == "إدارة القسائم")
async def manage_coupons_panel(message: types.Message):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("إدارة القسائم", reply_markup=manage_coupons_kb)

@router.message(F.text == "إضافة قسيمة")
async def start_add_coupon(message: types.Message, state: FSMContext):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("أرسل كود القسيمة:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AddCouponState.code)

@router.message(AddCouponState.code)
async def process_coupon_code(message: types.Message, state: FSMContext):
    await state.update_data(code=message.text.strip())
    await message.answer("أرسل نسبة الخصم (0-100):")
    await state.set_state(AddCouponState.discount)

@router.message(AddCouponState.discount)
async def process_coupon_discount(message: types.Message, state: FSMContext):
    try:
        disc = float(message.text.strip())
        data = await state.get_data()
        await add_coupon_db(data["code"], disc)
        await message.answer("تمت إضافة القسيمة.", reply_markup=manage_coupons_kb)
        await state.clear()
    except ValueError:
        await message.answer("أدخل رقماً صحيحاً.")

@router.message(F.text == "قائمة القسائم")
async def list_coupons_admin_handler(message: types.Message):
    coupons = await list_coupons_db()
    if not coupons:
        await message.answer("لا توجد قسائم.")
        return
    txt = "\n".join([f"{c['code']}: {c['discount']}%" for c in coupons])
    await message.answer(txt)

@router.message(F.text == "حذف قسيمة")
async def start_delete_coupon(message: types.Message, state: FSMContext):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("أرسل كود القسيمة للحذف:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(DeleteCouponState.code)

@router.message(DeleteCouponState.code)
async def process_delete_coupon(message: types.Message, state: FSMContext):
    await delete_coupon_db(message.text.strip())
    await message.answer("تم حذف القسيمة.", reply_markup=manage_coupons_kb)
    await state.clear()

# ------- Roles -------
@router.message(F.text == "إدارة الأدوار")
async def manage_roles_panel(message: types.Message, state: FSMContext):
    user = await get_user_data(message.from_user.id)
    if user["role"] != "owner":
        await message.answer("هذه الميزة للمالك.")
        return
    await message.answer("أرسل ID المستخدم لتغيير دوره:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(ManageRolesState.userid)

@router.message(ManageRolesState.userid)
async def process_manage_roles_userid(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
        user = await get_user_by_id(uid)
        if not user:
            await message.answer("المستخدم غير موجود.")
            await state.clear()
            return
        if user["role"] == "owner":
            await message.answer("لا يمكن تغيير دور المالك.")
            await state.clear()
            return
        await state.update_data(userid=uid)
        await message.answer(f"{user['first_name']}", reply_markup=manage_roles_kb, parse_mode=ParseMode.HTML)
        await state.set_state(ManageRolesState.role)
    except ValueError:
        await message.answer("أدخل رقماً صحيحاً.")

@router.callback_query(F.data.startswith("setrole:"), ManageRolesState.role)
async def process_manage_roles_callback(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid = data["userid"]
    newrole = callback.data.split(":")[1]
    user_to_update = await get_user_by_id(uid)
    if not user_to_update:
        await callback.answer("المستخدم غير موجود.", show_alert=True)
        await state.clear()
        return
    if user_to_update["role"] == "owner":
        await callback.answer("لا يمكن تغيير دور المالك.", show_alert=True)
        await state.clear()
        return
    await update_user_role(uid, newrole)
    await callback.message.edit_text("تم تحديث الدور.")
    await callback.answer()
    await state.clear()

# ------- Smart Notifications UI -------
@router.message(F.text == "إرسال إشعارات")
async def start_notify_users(message: types.Message, state: FSMContext):
    user = await get_user_data(message.from_user.id)
    if user["role"] not in ("admin", "owner"):
        await message.answer("غير مسموح.")
        return
    await message.answer("اختر الشريحة أو جدولة:", reply_markup=notify_users_kb)
    await state.set_state(NotifyUsersState.target)

@router.callback_query(F.data.startswith("notify:"), NotifyUsersState.target)
async def pick_notify_target(callback: types.CallbackQuery, state: FSMContext):
    kind = callback.data.split(":")[1]
    if kind == "schedule":
        await state.update_data(target="schedule")
        await callback.message.edit_text("أرسل وقت الجدولة بصيغة YYYY-MM-DD HH:MM (توقيت UTC):")
        await state.set_state(NotifyUsersState.schedule)
    elif kind == "custom":
        await state.update_data(target="custom")
        await callback.message.edit_text("أرسل IDs مفصولة بفواصل مثل: 123,456,789 ثم أرسل نص الرسالة.")
        await state.set_state(NotifyUsersState.custom_ids)
    else:
        await state.update_data(target=kind)
        await callback.message.edit_text("أرسل نص الإشعار:")
        await state.set_state(NotifyUsersState.messagetext)
    await callback.answer()

@router.message(NotifyUsersState.schedule)
async def process_notify_schedule(message: types.Message, state: FSMContext):
    try:
        dt = datetime.strptime(message.text.strip(), "%Y-%m-%d %H:%M")
        await state.update_data(schedule_at=dt)
        await message.answer("أرسل نص الإشعار:")
        await state.set_state(NotifyUsersState.messagetext)
    except ValueError:
        await message.answer("صيغة غير صحيحة. مثال: 2025-09-20 13:30")

@router.message(NotifyUsersState.custom_ids)
async def process_notify_custom_ids(message: types.Message, state: FSMContext):
    # parse IDs then ask for text
    ids = []
    for part in message.text.replace(" ", "").split(","):
        if part.isdigit():
            ids.append(int(part))
    await state.update_data(custom_ids=ids)
    await message.answer("أرسل نص الإشعار:")
    await state.set_state(NotifyUsersState.messagetext)

@router.message(NotifyUsersState.messagetext)
async def process_notification_message(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("target")
    text = message.text
    schedule_at = data.get("schedule_at")
    custom_ids = data.get("custom_ids")
    # Queue notification
    await queue_notification(
        text=text,
        segment=("custom" if target == "custom" else target if target != "schedule" else "all"),
        schedule_at=schedule_at,
        custom_user_ids=custom_ids
    )
    await message.answer("تمت إضافة الإشعار إلى قائمة الانتظار.")
    await state.clear()

# ------- Advanced Search UI -------
def search_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="فلتر: متوفر", callback_data="s:stock")],
        [InlineKeyboardButton(text="سعر ⬆️", callback_data="s:sort:price_asc"),
         InlineKeyboardButton(text="سعر ⬇️", callback_data="s:sort:price_desc")],
        [InlineKeyboardButton(text="كمية ⬇️", callback_data="s:sort:stock_desc")],
        [InlineKeyboardButton(text="تعيين حد أدنى", callback_data="s:pmin"),
         InlineKeyboardButton(text="تعيين حد أقصى", callback_data="s:pmax")],
        [InlineKeyboardButton(text="بحث ضمن فئة", callback_data="s:cat")],
        [InlineKeyboardButton(text="تنفيذ البحث", callback_data="s:run")]
    ])
    return kb

@router.message(Command("search"))
async def cmd_search(message: types.Message, state: FSMContext):
    await message.answer("أرسل كلمة البحث (أو اتركها فارغة):", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(SearchState.q)

@router.message(SearchState.q)
async def search_set_q(message: types.Message, state: FSMContext):
    await state.update_data(q=message.text.strip())
    await state.update_data(price_min=None, price_max=None, in_stock=False, sort=None, category=None)
    await message.answer("اضبط خيارات البحث من الأزرار ثم نفّذ:", reply_markup=search_kb())

@router.callback_query(F.data == "s:stock")
async def search_toggle_stock(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    in_stock = not data.get("in_stock", False)
    await state.update_data(in_stock=in_stock)
    await callback.answer("تم تبديل التوافر.")
    await callback.message.edit_reply_markup(reply_markup=search_kb())

@router.callback_query(F.data.startswith("s:sort:"))
async def search_set_sort(callback: types.CallbackQuery, state: FSMContext):
    sort = callback.data.split(":")[-1]
    await state.update_data(sort=sort)
    await callback.answer("تم اختيار الترتيب.")
    await callback.message.edit_reply_markup(reply_markup=search_kb())

@router.callback_query(F.data == "s:pmin")
async def search_set_pmin(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("أرسل الحد الأدنى للسعر:")
    await state.set_state(SearchState.filter_price_min)
    await callback.answer()

@router.message(SearchState.filter_price_min)
async def search_receive_pmin(message: types.Message, state: FSMContext):
    try:
        v = float(message.text.strip())
        await state.update_data(price_min=v)
        await message.answer("تم، عدّل باقي الإعدادات أو نفّذ:", reply_markup=search_kb())
    except ValueError:
        await message.answer("رقم غير صالح.")

@router.callback_query(F.data == "s:pmax")
async def search_set_pmax(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("أرسل الحد الأقصى للسعر:")
    await state.set_state(SearchState.filter_price_max)
    await callback.answer()

@router.message(SearchState.filter_price_max)
async def search_receive_pmax(message: types.Message, state: FSMContext):
    try:
        v = float(message.text.strip())
        await state.update_data(price_max=v)
        await message.answer("تم، عدّل باقي الإعدادات أو نفّذ:", reply_markup=search_kb())
    except ValueError:
        await message.answer("رقم غير صالح.")

@router.callback_query(F.data == "s:cat")
async def search_pick_category(callback: types.CallbackQuery, state: FSMContext):
    # Reuse nested category picker
    await state.update_data(add_cat_mode="search")
    kb = await build_category_pick_menu(parent_id=None)
    await callback.message.edit_text("اختر الفئة لتقييد البحث:", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("catpick:choose:"), SearchState.q)
async def search_pick_category_choice(callback: types.CallbackQuery, state: FSMContext):
    chosen = int(callback.data.split(":")[-1])
    category, subcategory = await map_product_category(chosen)
    await state.update_data(category=category)
    await callback.message.edit_text(f"تم تقييد البحث بالفئة: {category}. اضبط باقي الإعدادات ثم نفّذ:", reply_markup=search_kb())
    await callback.answer()

@router.callback_query(F.data == "s:run")
async def search_run(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    results = await search_products(
        q=data.get("q"),
        price_min=data.get("price_min"),
        price_max=data.get("price_max"),
        in_stock_only=data.get("in_stock", False),
        sort=data.get("sort"),
        category=data.get("category")
    )
    if not results:
        await callback.message.edit_text("لا نتائج مطابقة.")
        await callback.answer()
        return
    lines = []
    for r in results[:25]:
        name_h = highlight(r["name"], data.get("q") or "")
        desc_h = highlight(r["description"] or "", data.get("q") or "")
        lines.append(f"{name_h}\n{desc_h[:100]}...\nالسعر: {r['price']}")
    await callback.message.edit_text("\n\n".join(lines), parse_mode=ParseMode.HTML)
    await callback.answer()

# ------- Notifications trigger on purchase completion (example) -------
@router.message(F.text == "التحقق من الدفع")
async def start_verify_payment(message: types.Message, state: FSMContext):
    await message.answer("أرسل كود الدفع للتحقق:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(VerifyPaymentState.code)

@router.message(VerifyPaymentState.code)
async def process_verify_payment_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    payment = await get_payment_by_code(code)
    if not payment:
        await message.answer("غير موجود.")
        await state.clear()
        return
    order = await get_order_by_id(payment["order_id"])
    if not order:
        await message.answer("الطلب غير موجود.")
        await state.clear()
        return
    order_items = await get_order_items(order["order_id"])
    bot = message.bot
    for item in order_items:
        product = await get_product_by_id(item["product_id"])
        if product and product["file_url"]:
            await bot.send_message(order["user_id"], f"تحميل: <b>{product['name']}</b> {product['file_url']}", parse_mode=ParseMode.HTML)
    await update_order_status(order["order_id"], "completed")
    await update_payment_status(order["order_id"], "completed")
    # Smart notification example: thank-you
    await queue_notification(f"شكراً لطلبك #{order['order_id']}! ✨", segment="custom", custom_user_ids=[order["user_id"]])
    await message.answer("تم التحقق والتسليم.")
    await state.clear()

# ------- Misc ----------
@router.message(F.text == "رجوع للقائمة")
async def back_to_main_menu(message: types.Message, state: FSMContext):
    await state.clear()
    user = await get_user_data(message.from_user.id)
    if user["role"] == "owner":
        await message.answer(f"مرحباً {message.from_user.full_name}!", reply_markup=owner_panel_kb)
    elif user["role"] == "admin":
        await message.answer(f"مرحباً {message.from_user.full_name}!", reply_markup=main_kb_admin)
    else:
        await message.answer(f"مرحباً {message.from_user.full_name}!", reply_markup=main_kb_user)

# -----------------------------------------
# App bootstrap
# -----------------------------------------
async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not found in .env file.")
        return
    await init_db()
    # Promote ADMINS to owner on first run
    if ADMINS:
        conn = await get_conn()
        for admin_id in ADMINS:
            await conn.execute("UPDATE users SET role='owner' WHERE user_id=?", (admin_id,))
        await conn.commit()
        await conn.close()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    # Start background notification processor
    asyncio.create_task(process_notifications_task(bot))

    logger.info("Starting bot...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")
    except Exception as e:
        logger.error(f"An error occurred: {e}")
