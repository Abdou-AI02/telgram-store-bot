# bot_app_complete.py

import asyncio
import os
import logging
import sys
import secrets
import json
import aiohttp
import asyncpg  # New import for PostgreSQL
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from datetime import datetime, timedelta

# إعداد logging
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# ====== Configuration ======
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMINS = [int(x) for x in os.getenv("ADMINS", "").replace('،', ',').split(',') if x]
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "YOUR_TELEGRAM_USERNAME")
DATABASE_URL = os.getenv("DATABASE_URL") # New config, removed default value
DEFAULT_CURRENCY = "USD"
DZD_TO_USD_RATE = 250
POINTS_PER_DOLLAR = 1000
REFERRAL_BONUS_POINTS = 100
REFEREE_BONUS_POINTS = 50
# -- تعديل: مكافأة شراء ثابتة للمحيل
REFERRAL_PURCHASE_BONUS_POINTS = 100
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Connection pool for PostgreSQL
pool = None

# ====== Database Functions ======
async def init_db():
    global pool
    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
        async with pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, 
                first_name TEXT, 
                points INTEGER DEFAULT 0, 
                referrals INTEGER DEFAULT 0, 
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP, 
                ref_code TEXT, 
                referred_by BIGINT,
                role TEXT DEFAULT 'user',
                last_daily_task TEXT
            );
            CREATE TABLE IF NOT EXISTS products (
                product_id SERIAL PRIMARY KEY, 
                name TEXT NOT NULL, 
                price REAL NOT NULL, 
                stock INTEGER DEFAULT 0, 
                category TEXT, 
                description TEXT, 
                file_url TEXT
            );
            CREATE TABLE IF NOT EXISTS cart (
                id SERIAL PRIMARY KEY, 
                user_id BIGINT, 
                product_id INTEGER, 
                quantity INTEGER, 
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id SERIAL PRIMARY KEY, 
                user_id BIGINT, 
                status TEXT DEFAULT 'pending', 
                total REAL, 
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS coupons (
                code TEXT PRIMARY KEY, 
                discount REAL, 
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY, 
                order_id INTEGER, 
                payment_method TEXT, 
                payment_code TEXT, 
                status TEXT DEFAULT 'pending', 
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS payment_methods (
                id SERIAL PRIMARY KEY, 
                name TEXT, 
                details TEXT
            );
            CREATE TABLE IF NOT EXISTS order_items (
                order_id INTEGER, 
                product_id INTEGER, 
                quantity INTEGER, 
                PRIMARY KEY (order_id, product_id), 
                FOREIGN KEY(order_id) REFERENCES orders(order_id), 
                FOREIGN KEY(product_id) REFERENCES products(product_id)
            );
            """)
        await create_sample_products()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to connect to or initialize database: {e}")
        sys.exit(1)


async def create_sample_products():
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM products")
        if count == 0:
            await conn.executemany(
                """INSERT INTO products (name, price, stock, category, description, file_url) VALUES ($1, $2, $3, $4, $5, $6)""",
                [
                    ("دورة بايثون للمبتدئين", 19.99, 100, "دورة", 
                     "دورة شاملة لتعلم أساسيات لغة البرمجة بايثون من الصفر.", 
                     "https://example.com/python-course.pdf"),
                    ("اشتراك دعم تقني شهري", 2.99, 9999, "خدمة", 
                     "دعم فني على مدار الساعة لحل مشاكلك التقنية.", 
                     "https://example.com/support-info.txt"),
                    ("مقدمة في الذكاء الاصطناعي", 49.99, 50, "دورة", 
                     "نظرة عامة على مفاهيم الذكاء الاصطناعي والتعلم الآلي.", 
                     "https://example.com/ai-intro.pdf")
                ]
            )
            logger.info("Sample products created.")


async def create_user_if_not_exists(user_id: int, first_name: str, referred_by_id: int = None):
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE user_id=$1", user_id)
        if not user:
            ref_code = secrets.token_hex(4)
            role = 'owner' if user_id in ADMINS else 'user'
            await conn.execute(
                "INSERT INTO users (user_id, first_name, ref_code, referred_by, role) VALUES ($1, $2, $3, $4, $5)", 
                user_id, first_name, ref_code, referred_by_id, role
            )
            if referred_by_id:
                await conn.execute(
                    "UPDATE users SET referrals = referrals + 1, points = points + $1 WHERE user_id = $2", 
                    REFERRAL_BONUS_POINTS, referred_by_id
                )
                await conn.execute(
                    "UPDATE users SET points = points + $1 WHERE user_id = $2", 
                    REFEREE_BONUS_POINTS, user_id
                )


async def get_user_data(user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)


async def add_points(user_id: int, points: int):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET points = points + $1 WHERE user_id = $2", points, user_id)


async def deduct_points(user_id: int, points: int):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET points = points - $1 WHERE user_id = $2", points, user_id)


async def update_last_daily_task(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET last_daily_task = CURRENT_TIMESTAMP WHERE user_id = $1", user_id)


async def list_products(category: str = None):
    async with pool.acquire() as conn:
        if category:
            return await conn.fetch("SELECT * FROM products WHERE category = $1 ORDER BY product_id", category)
        else:
            return await conn.fetch("SELECT * FROM products ORDER BY product_id")


async def get_product_by_id(product_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM products WHERE product_id=$1", product_id)


async def get_all_categories():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT DISTINCT category FROM products")
        return [row['category'] for row in rows]


async def add_to_cart(user_id, product_id, quantity=1):
    async with pool.acquire() as conn:
        cart_item = await conn.fetchrow("SELECT id, quantity FROM cart WHERE user_id=$1 AND product_id=$2", user_id, product_id)
        if cart_item:
            await conn.execute("UPDATE cart SET quantity=$1 WHERE id=$2", cart_item['quantity'] + quantity, cart_item['id'])
        else:
            await conn.execute("INSERT INTO cart (user_id, product_id, quantity) VALUES ($1, $2, $3)", user_id, product_id, quantity)


async def get_cart_items(user_id):
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT c.id, c.product_id, c.quantity, p.name, p.price, p.file_url 
            FROM cart c JOIN products p ON c.product_id = p.product_id 
            WHERE c.user_id = $1
        """, user_id)


async def clear_cart(user_id):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM cart WHERE user_id=$1", user_id)


async def create_order(user_id, payment_method, payment_code=None):
    items = await get_cart_items(user_id)
    if not items:
        return None
    total = sum(item["price"] * item["quantity"] for item in items)
    async with pool.acquire() as conn:
        order_id = await conn.fetchval(
            "INSERT INTO orders (user_id, total) VALUES ($1, $2) RETURNING order_id", 
            user_id, total
        )
        for item in items:
            await conn.execute("INSERT INTO order_items (order_id, product_id, quantity) VALUES ($1, $2, $3)", 
                              order_id, item['product_id'], item['quantity'])
        await conn.execute("INSERT INTO payments (order_id, payment_method, payment_code, status) VALUES ($1, $2, $3, $4)", 
                          order_id, payment_method, payment_code, 'pending')
    return order_id


async def get_order_items(order_id):
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT p.product_id, p.name, p.price, p.file_url, p.category, oi.quantity 
            FROM order_items oi JOIN products p ON oi.product_id = p.product_id 
            WHERE oi.order_id = $1
        """, order_id)


async def list_user_orders(user_id):
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM orders WHERE user_id=$1 ORDER BY created_at DESC", user_id)


async def list_pending_orders():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM orders WHERE status='pending' ORDER BY created_at DESC")


async def update_order_status(order_id, status):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE orders SET status=$1 WHERE order_id=$2", status, order_id)


async def update_payment_status(order_id, status):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE payments SET status=$1 WHERE order_id=$2", status, order_id)


async def apply_coupon_db(code):
    async with pool.acquire() as conn:
        discount = await conn.fetchval("SELECT discount FROM coupons WHERE code=$1 AND is_active=1", code)
        return discount


# ====== Admin Database Functions ======
async def add_product_db(name: str, price: float, stock: int, category: str, description: str, file_url: str):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO products (name, price, stock, category, description, file_url) VALUES ($1, $2, $3, $4, $5, $6)", 
                          name, price, stock, category, description, file_url)


async def edit_product_db(product_id: int, name: str, price: float, stock: int, category: str, description: str):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE products SET name=$1, price=$2, stock=$3, category=$4, description=$5 WHERE product_id=$6", 
                          name, price, stock, category, description, product_id)


async def delete_product_db(product_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM products WHERE product_id=$1", product_id)


async def add_coupon_db(code: str, discount: float):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO coupons (code, discount, is_active) VALUES ($1, $2, $3)", code, discount, 1)


async def get_coupon_db(code: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM coupons WHERE code=$1", code)


async def delete_coupon_db(code: str):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM coupons WHERE code=$1", code)


async def list_coupons_db():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM coupons")


async def get_total_sales_db():
    async with pool.acquire() as conn:
        total_sales = await conn.fetchval("SELECT SUM(total) FROM orders WHERE status='مقبول ✅'")
        return total_sales if total_sales else 0


async def get_total_orders_db():
    async with pool.acquire() as conn:
        total_orders = await conn.fetchval("SELECT COUNT(order_id) FROM orders")
        return total_orders if total_orders else 0


async def get_user_by_id(user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)


async def add_user_points_db(user_id: int, points: int):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET points = points + $1 WHERE user_id = $2", points, user_id)


async def deduct_user_points_db(user_id: int, points: int):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET points = points - $1 WHERE user_id = $2", points, user_id)


async def add_payment_method_db(name: str, details: str):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO payment_methods (name, details) VALUES ($1, $2)", name, details)


async def list_payment_methods_db():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM payment_methods")


async def delete_payment_method_db(method_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM payment_methods WHERE id=$1", method_id)


async def get_payment_by_code(code: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM payments WHERE payment_code=$1", code)


async def get_order_by_id(order_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM orders WHERE order_id=$1", order_id)


async def update_user_role(user_id: int, role: str):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET role = $1 WHERE user_id = $2", role, user_id)


# ====== New DB Functions for Analytics ======
async def get_most_popular_products():
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT p.name, SUM(oi.quantity) as total_sold
            FROM order_items oi
            JOIN products p ON oi.product_id = p.product_id
            GROUP BY p.product_id
            ORDER BY total_sold DESC
            LIMIT 5
        """)


async def get_most_active_users():
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT first_name, COUNT(order_id) as total_orders
            FROM orders o
            JOIN users u ON o.user_id = u.user_id
            GROUP BY u.user_id
            ORDER BY total_orders DESC
            LIMIT 5
        """)


async def get_referral_sources_stats():
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT u.first_name, COUNT(r.user_id) as total_referrals
            FROM users u
            JOIN users r ON u.user_id = r.referred_by
            GROUP BY u.user_id
            ORDER BY total_referrals DESC
            LIMIT 5
        """)

# ====== LLM Function ======
async def generate_product_data_with_ai(user_text: str) -> dict:
    """
    يتصل بنموذج الذكاء الاصطناعي لاستخراج بيانات المنتج.
    """
    prompt = (
        "من النص التالي، استخرج البيانات في تنسيق JSON مع المفاتيح: 'name', 'price', 'category', 'description', و 'file_url'. "
        "اجعل السعر كعدد (float) فقط بدون رمز العملة. استنتج الفئة من المحتوى. "
        "اذا لم يتم العثور على حقل معين، اجعله فارغاً. "
        "يجب أن يكون الرد عبارة عن كود JSON فقط، لا يوجد أي نص آخر.\n\n"
        f"النص: '{user_text}'"
    )

    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not found in .env file.")
        return None

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key={GEMINI_API_KEY}"
        headers = {'Content-Type': 'application/json'}
        payload = {
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {
                'responseMimeType': 'application/json',
                'responseSchema': {
                    'type': 'OBJECT',
                    'properties': {
                        'name': {'type': 'STRING'},
                        'price': {'type': 'NUMBER'},
                        'category': {'type': 'STRING'},
                        'description': {'type': 'STRING'},
                        'file_url': {'type': 'STRING'}
                    },
                    'propertyOrdering': ['name', 'price', 'category', 'description', 'file_url']
                }
            }
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=json.dumps(payload)) as response:
                if response.status != 200:
                    logger.error(f"API call failed with status: {response.status}")
                    return None
                
                result = await response.json()
                if 'candidates' in result and len(result['candidates']) > 0:
                    json_str = result['candidates'][0]['content']['parts'][0]['text']
                    return json.loads(json_str)
                else:
                    return None

    except Exception as e:
        logger.error(f"AI generation failed: {e}")
        return None

# ====== FSM States ======
router = Router()

class AddProductState(StatesGroup):
    name = State()
    price = State()
    stock = State()
    category = State()
    description = State()
    file_url = State()

class AddProductAIState(StatesGroup):
    waiting_for_text = State()
    confirm_data = State()
    
# FSM for manual editing after AI extraction
class EditAIProductState(StatesGroup):
    name = State()
    price = State()
    stock = State()
    category = State()
    description = State()
    file_url = State()

class EditProductState(StatesGroup):
    product_id = State()
    name = State()
    price = State()
    stock = State()
    category = State()
    description = State()

class DeleteProductState(StatesGroup):
    product_id = State()

class AddCouponState(StatesGroup):
    code = State()
    discount = State()

class DeleteCouponState(StatesGroup):
    code = State()

class AddPointsState(StatesGroup):
    user_id = State()
    points = State()

class DeductPointsState(StatesGroup):
    user_id = State()
    points = State()

class GetUserInfoState(StatesGroup):
    user_id = State()

class AddPaymentState(StatesGroup):
    name = State()
    details = State()

class DeletePaymentState(StatesGroup):
    id = State()

class VerifyPaymentState(StatesGroup):
    code = State()

class ManageRolesState(StatesGroup):
    user_id = State()
    role = State()

class ViewOrderDetailsState(StatesGroup):
    order_id = State()

class ApplyCouponState(StatesGroup):
    waiting_for_code = State()

# New: FSM for managing store categories and buttons
class ManageStoreState(StatesGroup):
    action = State()
    category_name = State()
    old_category_name = State()
    
class NotifyUsersState(StatesGroup):
    message_text = State()
    target = State()

# ====== Keyboards ======
main_kb_user = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🛒 المتجر"), KeyboardButton(text="📄 طلباتي")],
        [KeyboardButton(text="💳 السلة"), KeyboardButton(text="🌟 حسابي")],
        [KeyboardButton(text="🎁 مهام اليوم")]
    ],
    resize_keyboard=True,
    input_field_placeholder="اختر أمراً..."
)

main_kb_admin = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🛒 المتجر"), KeyboardButton(text="📄 طلباتي")],
        [KeyboardButton(text="💳 السلة"), KeyboardButton(text="🌟 حسابي")],
        [KeyboardButton(text="👑 لوحة المشرف")],
        [KeyboardButton(text="🎁 مهام اليوم")]
    ],
    resize_keyboard=True,
    input_field_placeholder="اختر أمراً..."
)

admin_panel_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📦 إدارة المنتجات"), KeyboardButton(text="📝 إدارة الطلبات"), KeyboardButton(text="🔍 تفاصيل طلب")],
        [KeyboardButton(text="🏷️ إدارة الكوبونات"), KeyboardButton(text="📊 الإحصائيات")],
        [KeyboardButton(text="👤 إدارة المستخدمين"), KeyboardButton(text="💰 طرق الدفع")],
        [KeyboardButton(text="📢 إرسال إشعار"), KeyboardButton(text="🛍️ إدارة المتجر")],
        [KeyboardButton(text="🔙 العودة للقائمة الرئيسية"), KeyboardButton(text="🚹 تجربة كـ مستخدم")]
    ],
    resize_keyboard=True,
    input_field_placeholder="اختر أمراً..."
)

owner_panel_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="👑 لوحة المشرف"), KeyboardButton(text="⚙️ إدارة الصلاحيات"), KeyboardButton(text="🔍 تفاصيل طلب")],
        [KeyboardButton(text="✨ إضافة منتج بالذكاء الاصطناعي"), KeyboardButton(text="📢 إرسال إشعار"), KeyboardButton(text="🛍️ إدارة المتجر")],
        [KeyboardButton(text="🚹 تجربة كـ مستخدم"), KeyboardButton(text="🔙 العودة للقائمة الرئيسية")]
    ],
    resize_keyboard=True,
    input_field_placeholder="اختر أمراً..."
)

manage_products_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ إضافة منتج"), KeyboardButton(text="📝 تعديل منتج"), KeyboardButton(text="🗑️ حذف منتج")],
        [KeyboardButton(text="📜 عرض المنتجات"), KeyboardButton(text="🔙 العودة للقائمة الرئيسية")]
    ],
    resize_keyboard=True,
    input_field_placeholder="اختر أمراً..."
)

manage_coupons_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ إضافة كوبون"), KeyboardButton(text="🗑️ حذف كوبون")],
        [KeyboardButton(text="📜 عرض الكوبونات"), KeyboardButton(text="🔙 العودة للقائمة الرئيسية")]
    ],
    resize_keyboard=True,
    input_field_placeholder="اختر أمراً..."
)

manage_users_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ إضافة نقاط"), KeyboardButton(text="➖ حذف نقاط")],
        [KeyboardButton(text="🔍 عرض بيانات مستخدم"), KeyboardButton(text="🔙 العودة للقائمة الرئيسية")]
    ],
    resize_keyboard=True,
    input_field_placeholder="اختر أمراً..."
)

manage_payments_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ إضافة طريقة دفع"), KeyboardButton(text="🗑️ حذف طريقة دفع")],
        [KeyboardButton(text="📜 عرض طرق الدفع"), KeyboardButton(text="🔙 العودة للقائمة الرئيسية")],
        [KeyboardButton(text="✔️ التحقق من الدفع")]
    ],
    resize_keyboard=True,
    input_field_placeholder="اختر أمراً..."
)

manage_roles_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="تعيين مشرف", callback_data="set_role:admin")],
        [InlineKeyboardButton(text="إزالة مشرف", callback_data="set_role:user")],
    ]
)

manage_store_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ إضافة فئة"), KeyboardButton(text="📝 تعديل فئة"), KeyboardButton(text="🗑️ حذف فئة")],
        [KeyboardButton(text="🔙 العودة للقائمة الرئيسية")]
    ],
    resize_keyboard=True,
    input_field_placeholder="اختر أمراً..."
)

notify_users_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="إلى كل المستخدمين", callback_data="notify_all")],
        [InlineKeyboardButton(text="إلى مجموعة محددة", callback_data="notify_group")],
    ]
)

# ====== State Reset Handler - الحل الرئيسي للمشكلة ======
@router.message(F.text.in_([
    "🛒 المتجر", "📄 طلباتي", "💳 السلة", "🌟 حسابي", "👑 لوحة المشرف",
    "📦 إدارة المنتجات", "📝 إدارة الطلبات", "🏷️ إدارة الكوبونات", "📊 الإحصائيات",
    "👤 إدارة المستخدمين", "💰 طرق الدفع", "🔙 العودة للقائمة الرئيسية",
    "➕ إضافة منتج", "📝 تعديل منتج", "🗑️ حذف منتج", "📜 عرض المنتجات",
    "➕ إضافة كوبون", "🗑️ حذف كوبون", "📜 عرض الكوبونات",
    "➕ إضافة نقاط", "➖ حذف نقاط", "🔍 عرض بيانات مستخدم",
    "➕ إضافة طريقة دفع", "🗑️ حذف طريقة دفع", "📜 عرض طرق الدفع", "✔️ التحقق من الدفع",
    "⚙️ إدارة الصلاحيات", "✨ إضافة منتج بالذكاء الاصطناعي", "🔍 تفاصيل طلب",
    "🚹 تجربة كـ مستخدم", "🔙 العودة كـ مسؤول", "🛍️ إدارة المتجر", "📢 إرسال إشعار", "🎁 مهام اليوم"
]))
async def handle_menu_buttons_with_state_reset(message: types.Message, state: FSMContext):
    """معالج شامل لإعادة تعيين حالات FSM عند الضغط على أزرار القائمة"""
    current_state = await state.get_state()
    if current_state is not None:
        logger.info(f"Clearing state {current_state} for user {message.from_user.id}")
        await state.clear()
    
    text = message.text
    user_data = await get_user_data(message.from_user.id)
    if not user_data:
        await create_user_if_not_exists(message.from_user.id, message.from_user.full_name)
        user_data = await get_user_data(message.from_user.id)
        
    user_role = user_data['role']

    # New functionality: Switch between user and admin view
    if text == "🚹 تجربة كـ مستخدم":
        await cmd_start_as_user(message, state)
        return
    elif text == "🔙 العودة كـ مسؤول":
        await cmd_start_as_admin(message, state)
        return
    
    # الأوامر الرئيسية
    if text == "🛒 المتجر":
        await cmd_shop(message)
    elif text == "📄 طلباتي":
        await cmd_orders(message)
    elif text == "💳 السلة":
        await cmd_cart(message, state)
    elif text == "🌟 حسابي":
        await cmd_my_account(message)
    elif text == "🎁 مهام اليوم":
        await cmd_daily_tasks(message)
    elif text == "👑 لوحة المشرف" and user_role in ['admin', 'owner']:
        await admin_panel(message)
    elif text == "⚙️ إدارة الصلاحيات" and user_role == 'owner':
        await manage_roles_panel(message, state)
    elif text == "✨ إضافة منتج بالذكاء الاصطناعي" and user_role == 'owner':
        await start_add_product_ai(message, state)
    elif text == "🔍 تفاصيل طلب" and user_role in ['admin', 'owner']:
        await start_view_order_details(message, state)
    
    # إدارة المتجر (أزرار)
    elif text == "🛍️ إدارة المتجر" and user_role in ['admin', 'owner']:
        await manage_store_panel(message)
    
    # إدارة المنتجات
    elif text == "📦 إدارة المنتجات" and user_role in ['admin', 'owner']:
        await manage_products_panel(message)
    elif text == "➕ إضافة منتج" and user_role in ['admin', 'owner']:
        await start_add_product(message, state)
    elif text == "📝 تعديل منتج" and user_role in ['admin', 'owner']:
        await start_edit_product(message, state)
    elif text == "🗑️ حذف منتج" and user_role in ['admin', 'owner']:
        await start_delete_product(message, state)
    elif text == "📜 عرض المنتجات" and user_role in ['admin', 'owner']:
        await list_products_admin_handler(message)
    
    # إدارة الكوبونات
    elif text == "🏷️ إدارة الكوبونات" and user_role in ['admin', 'owner']:
        await manage_coupons_panel(message)
    elif text == "➕ إضافة كوبون" and user_role in ['admin', 'owner']:
        await start_add_coupon(message, state)
    elif text == "🗑️ حذف كوبون" and user_role in ['admin', 'owner']:
        await start_delete_coupon(message, state)
    elif text == "📜 عرض الكوبونات" and user_role in ['admin', 'owner']:
        await list_coupons_admin_handler(message)
    
    # إدارة المستخدمين
    elif text == "👤 إدارة المستخدمين" and user_role in ['admin', 'owner']:
        await manage_users_panel(message)
    elif text == "➕ إضافة نقاط" and user_role in ['admin', 'owner']:
        await start_add_points(message, state)
    elif text == "➖ حذف نقاط" and user_role in ['admin', 'owner']:
        await start_deduct_points(message, state)
    elif text == "🔍 عرض بيانات مستخدم" and user_role in ['admin', 'owner']:
        await start_get_user_info(message, state)
    
    # إدارة طرق الدفع
    elif text == "💰 طرق الدفع" and user_role in ['admin', 'owner']:
        await manage_payments_panel(message)
    elif text == "➕ إضافة طريقة دفع" and user_role in ['admin', 'owner']:
        await start_add_payment_method(message, state)
    elif text == "🗑️ حذف طريقة دفع" and user_role in ['admin', 'owner']:
        await start_delete_payment_method(message, state)
    elif text == "📜 عرض طرق الدفع" and user_role in ['admin', 'owner']:
        await list_payments_admin_handler(message)
    elif text == "✔️ التحقق من الدفع" and user_role in ['admin', 'owner']:
        await start_verify_payment(message, state)
    elif text == "📢 إرسال إشعار" and user_role in ['admin', 'owner']:
        await start_notify_users(message, state)
    
    # الإحصائيات وإدارة الطلبات
    elif text == "📊 الإحصائيات" and user_role in ['admin', 'owner']:
        await get_stats_panel(message)
    elif text == "📝 إدارة الطلبات" and user_role in ['admin', 'owner']:
        await manage_orders_panel(message)
    
    # العودة للقائمة الرئيسية
    elif text == "🔙 العودة للقائمة الرئيسية":
        await back_to_main_menu(message, state)

# ====== Main Handlers ======
@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    
    # Handle referral link
    args = message.text.split()
    referred_by_id = None
    if len(args) > 1 and args[1].startswith('ref_'):
        ref_code = args[1][4:]
        async with pool.acquire() as conn:
            referrer_row = await conn.fetchrow("SELECT user_id FROM users WHERE ref_code=$1", ref_code)
        if referrer_row:
            referrer_id = referrer_row['user_id']
            if referrer_id != message.from_user.id:
                referred_by_id = referrer_id

    await create_user_if_not_exists(message.from_user.id, message.from_user.full_name, referred_by_id)

    if referred_by_id:
        await message.answer(f"أهلاً بك! لقد انضممت عبر دعوة وحصلت على {REFEREE_BONUS_POINTS} نقطة.")
        try:
            await message.bot.send_message(
                referred_by_id,
                f"🎉 تهانينا! {message.from_user.full_name} انضم للبوت عبر رابط إحالتك. لقد حصلت على {REFERRAL_BONUS_POINTS} نقطة."
            )
        except Exception as e:
            logger.error(f"Failed to notify referrer {referred_by_id}: {e}")

    user_data = await get_user_data(message.from_user.id)

    # Check for temporary user view state
    user_view_state = (await state.get_data()).get('user_view', False)
    if user_view_state:
        await message.answer(f"مرحباً بك في وضع المستخدم، {message.from_user.full_name}!", reply_markup=main_kb_user)
        return
    
    if user_data['role'] == 'owner':
        await message.answer(f"مرحباً بك يا مسؤول، {message.from_user.full_name}!", reply_markup=owner_panel_kb)
    elif user_data['role'] == 'admin':
        await message.answer(f"مرحباً بك يا مشرف، {message.from_user.full_name}!", reply_markup=admin_panel_kb)
    else:
        await message.answer(f"مرحباً بك في بوت المتجر، {message.from_user.full_name}!", reply_markup=main_kb_user)


# New: Toggle to user view
@router.message(F.text == "🚹 تجربة كـ مستخدم")
async def cmd_start_as_user(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return

    await state.set_state(None) # Clear state to avoid FSM conflicts
    await state.update_data(user_view=True)
    await message.answer("تم التبديل إلى وضع المستخدم العادي.", reply_markup=main_kb_user)

# New: Toggle back to admin view
@router.message(F.text == "🔙 العودة كـ مسؤول")
async def cmd_start_as_admin(message: types.Message, state: FSMContext):
    await state.set_state(None) # Clear state to avoid FSM conflicts
    await state.update_data(user_view=False)
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] == 'owner':
        await message.answer(f"مرحباً بك يا مسؤول، {message.from_user.full_name}!", reply_markup=owner_panel_kb)
    else:
        await message.answer(f"مرحباً بك يا مشرف، {message.from_user.full_name}!", reply_markup=admin_panel_kb)

@router.message(F.text == "🌟 حسابي")
async def cmd_my_account(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    if not user_data:
        await message.answer("⚠️ لم يتم العثور على بيانات حسابك. يرجى إعادة تشغيل البوت باستخدام /start.")
        return
    
    bot_info = await message.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_data['ref_code']}"
    
    text = (
        f"🌟 **بيانات حسابك**\n\n"
        f"• النقاط: <b>{user_data['points']}</b>\n"
        f"• عدد الإحالات: <b>{user_data['referrals']}</b>\n"
        f"• رابط الإحالة: <code>{ref_link}</code>\n\n"
        f"شارك رابط الإحالة مع أصدقائك للحصول على نقاط إضافية!"
    )
    await message.answer(text, parse_mode="HTML")

@router.message(F.text == "🎁 مهام اليوم")
async def cmd_daily_tasks(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    
    # Check if a day has passed since the last task
    last_daily_task_str = user_data.get('last_daily_task')
    if last_daily_task_str:
        last_daily_task_date = datetime.fromisoformat(last_daily_task_str)
        if datetime.now() - last_daily_task_date < timedelta(hours=24):
            await message.answer("لقد أكملت مهام اليوم بالفعل. عد غداً!")
            return

    # Task: visit the shop and earn 10 points
    await add_points(message.from_user.id, 10)
    await update_last_daily_task(message.from_user.id)
    
    await message.answer("🎉 لقد أكملت مهمة اليوم وحصلت على 10 نقاط إضافية!")
    
# ====== تعديل: نظام المتجر المتداخل ======
async def show_categories(message_or_callback, is_edit=False):
    """دالة مساعدة لعرض فئات المتجر الرئيسية."""
    categories = await get_all_categories()
    if not categories:
        text = "لا توجد فئات منتجات متاحة حالياً."
        kb = None
    else:
        text = "🛒 **المتجر**\n\nاختر فئة من القائمة أدناه:"
        kb_buttons = [[InlineKeyboardButton(text=cat, callback_data=f"shop_category:{cat}")] for cat in categories]
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    if is_edit:
        # إذا كان استدعاء من زر
        await message_or_callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        # إذا كان استدعاء من رسالة نصية
        await message_or_callback.answer(text, reply_markup=kb, parse_mode="HTML")

@router.message(F.text == "🛒 المتجر")
async def cmd_shop(message: types.Message):
    await show_categories(message, is_edit=False)

@router.callback_query(F.data == "shop_main")
async def back_to_shop_main(callback: types.CallbackQuery):
    await show_categories(callback, is_edit=True)
    await callback.answer()

@router.callback_query(F.data.startswith("shop_category:"))
async def show_products_in_category(callback: types.CallbackQuery):
    category = callback.data.split(":")[1]
    products = await list_products(category)

    text = f"📦 **منتجات فئة: {category}**"
    kb_buttons = []
    if not products:
        text += "\n\nلا توجد منتجات في هذه الفئة حالياً."
    else:
        for product in products:
            kb_buttons.append([InlineKeyboardButton(text=product['name'], callback_data=f"product_details:{product['product_id']}")])

    kb_buttons.append([InlineKeyboardButton(text="🔙 العودة للفئات", callback_data="shop_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("product_details:"))
async def show_product_details(callback: types.CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    product = await get_product_by_id(product_id)

    if not product:
        await callback.answer("⚠️ المنتج غير موجود.", show_alert=True)
        return

    text = (
        f"🛍️ **{product['name']}**\n\n"
        f"• **الوصف:** {product['description']}\n"
        f"• **السعر:** {product['price']:.2f} {DEFAULT_CURRENCY} ({product['price'] * DZD_TO_USD_RATE:.2f} دينار جزائري)\n"
        f"• **المخزون:** {'متوفر' if product['stock'] > 0 else 'غير متوفر'}\n"
    )

    kb_buttons = []
    if product['stock'] > 0:
        kb_buttons.append([
            InlineKeyboardButton(text="➕ إضافة للسلة", callback_data=f"add_to_cart:{product['product_id']}"),
            InlineKeyboardButton(text="✅ شراء الآن", callback_data=f"buy_now:{product['product_id']}")
        ])
    
    kb_buttons.append([InlineKeyboardButton(text=f"🔙 العودة لمنتجات {product['category']}", callback_data=f"shop_category:{product['category']}")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()
# ====== نهاية تعديل المتجر ======


@router.callback_query(F.data.startswith("add_to_cart:"))
async def add_to_cart_callback(callback: types.CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    try:
        await add_to_cart(callback.from_user.id, product_id)
        product = await get_product_by_id(product_id)
        await callback.answer(f"✅ تم إضافة {product['name']} إلى سلتك.", show_alert=True)
    except Exception as e:
        logger.error(f"Failed to add to cart: {e}")
        await callback.answer("⚠️ حدث خطأ. لم يتم إضافة المنتج إلى سلتك.", show_alert=True)

@router.callback_query(F.data.startswith("buy_now:"))
async def buy_now_callback(callback: types.CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[1])
    try:
        product = await get_product_by_id(product_id)
        if not product:
            await callback.answer("⚠️ المنتج غير موجود.", show_alert=True)
            return

        user_id = callback.from_user.id
        await clear_cart(user_id) # Clear cart before buying now
        await add_to_cart(user_id, product_id, quantity=1)
        await show_payment_options(callback, state)
            
    except Exception as e:
        logger.error(f"Buy now error: {e}")
        await callback.answer("⚠️ حدث خطأ أثناء إتمام الشراء. حاول لاحقاً.", show_alert=True)

@router.message(F.text == "💳 السلة")
async def cmd_cart(message: types.Message, state: FSMContext):
    try:
        items = await get_cart_items(message.from_user.id)
    except Exception as e:
        logger.error(f"DB Error: {e}")
        await message.answer("⚠️ تعذر جلب سلة التسوق حالياً.")
        return
    if not items:
        await message.answer("سلة التسوق فارغة 🛒")
        return
    
    text = "<b>سلتك:</b>\n"
    total_price = sum(item['price'] * item['quantity'] for item in items)
    
    # تطبيق الخصم إذا كان هناك كوبون
    state_data = await state.get_data()
    coupon_discount = state_data.get('coupon_discount', 0)
    if coupon_discount > 0:
        discounted_price = total_price * (1 - coupon_discount / 100)
        text += f"\nخصم الكوبون: {coupon_discount:.0f}%\n"
        text += f"السعر الأصلي: {total_price:.2f} {DEFAULT_CURRENCY}\n"
        total_price = discounted_price
        
    points_cost = int(total_price * POINTS_PER_DOLLAR)
    
    text += f"\nالمجموع: {total_price:.2f} {DEFAULT_CURRENCY} ({total_price * DZD_TO_USD_RATE:.2f} دينار جزائري) أو <b>{points_cost}</b> نقطة\n\n"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ الدفع", callback_data="pay_options"),
         InlineKeyboardButton(text="🗑️ إفراغ السلة", callback_data="clear_cart")],
        [InlineKeyboardButton(text="🎁 استخدام كوبون", callback_data="apply_coupon")]
    ])
    
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "apply_coupon")
async def apply_coupon_handler(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("أرسل رمز الكوبون:")
    await state.set_state(ApplyCouponState.waiting_for_code)
    await callback.answer()

@router.message(ApplyCouponState.waiting_for_code)
async def process_coupon_code_from_user(message: types.Message, state: FSMContext):
    code = message.text
    discount = await apply_coupon_db(code)
    
    if discount:
        await message.answer(f"🎉 تم تفعيل الكوبون! خصم {discount:.0f}% على سلتك.")
        await state.update_data(coupon_discount=discount)
    else:
        await message.answer("❌ الكوبون غير صالح أو منتهي.")
    
    await state.set_state(None) # Clear state after applying coupon
    await cmd_cart(message, state)


@router.callback_query(F.data == "clear_cart")
async def clear_cart_callback(callback: types.CallbackQuery):
    await clear_cart(callback.from_user.id)
    await callback.message.edit_text("🗑️ تم إفراغ سلة التسوق بنجاح!")

@router.callback_query(F.data == "pay_options")
async def show_payment_options(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    items = await get_cart_items(user_id)
    if not items:
        await callback.answer("⚠️ سلتك فارغة.", show_alert=True)
        return
        
    total_price = sum(item["price"] * item["quantity"] for item in items)
    
    # تطبيق الخصم إذا كان هناك كوبون
    state_data = await state.get_data()
    coupon_discount = state_data.get('coupon_discount', 0)
    if coupon_discount > 0:
        total_price = total_price - (total_price * coupon_discount / 100)
    
    points_cost = int(total_price * POINTS_PER_DOLLAR)
    user_data = await get_user_data(user_id)
    user_points = user_data['points']
    
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    
    if user_points >= points_cost:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"✅ ادفع بـ {points_cost} نقطة", callback_data="pay_with_points")])

    kb.inline_keyboard.append([InlineKeyboardButton(text="💬 تواصل مع المسؤول", callback_data="contact_admin_payment")])

    await callback.message.edit_text("اختر طريقة الدفع:", reply_markup=kb)
    
@router.callback_query(F.data == "pay_with_points")
async def pay_with_points(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    items = await get_cart_items(user_id)
    if not items:
        await callback.answer("⚠️ سلتك فارغة.", show_alert=True)
        return

    total_price = sum(item["price"] * item["quantity"] for item in items)
    
    # تطبيق الخصم إذا كان هناك كوبون
    state_data = await state.get_data()
    coupon_discount = state_data.get('coupon_discount', 0)
    if coupon_discount > 0:
        total_price = total_price - (total_price * coupon_discount / 100)
    
    points_cost = int(total_price * POINTS_PER_DOLLAR)
    
    user_data = await get_user_data(user_id)
    if user_data['points'] < points_cost:
        await callback.answer("⚠️ نقاطك لا تكفي لإتمام الشراء.", show_alert=True)
        return
        
    await deduct_points(user_id, points_cost)
    order_id = await create_order(user_id, "Points")
    
    for item in items:
        product = await get_product_by_id(item['product_id'])
        if product['file_url']:
            await callback.message.answer(f"📦 المنتج: **{product['name']}**\n\nرابط التحميل: {product['file_url']}", parse_mode="Markdown")

    await clear_cart(user_id)

    # إضافة نقاط إحالة عند الشراء
    if user_data['referred_by']:
        referrer_id = user_data['referred_by']
        # -- تعديل: استخدام المكافأة الثابتة
        purchase_points = REFERRAL_PURCHASE_BONUS_POINTS
        await add_points(referrer_id, purchase_points)
        try:
            await Bot.get_current().send_message(
                referrer_id,
                f"🎉 تهانينا! الشخص الذي قمت بإحالته قام بالشراء، وحصلت على {purchase_points} نقطة إضافية."
            )
        except Exception as e:
            logger.error(f"Failed to notify referrer {referrer_id} on purchase: {e}")

    await callback.message.edit_text(f"✅ تم الدفع بنجاح! تم خصم <b>{points_cost}</b> نقطة من حسابك.", parse_mode="HTML")
    await callback.answer("تم إتمام عملية الدفع.", show_alert=True)
    
@router.callback_query(F.data == "contact_admin_payment")
async def contact_admin_payment(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    items = await get_cart_items(user_id)
    if not items:
        await callback.answer("⚠️ سلتك فارغة.", show_alert=True)
        return
    
    payment_code = secrets.token_hex(8)
    order_id = await create_order(user_id, "Admin", payment_code)
    
    text = (
        f"💬 **التواصل مع المسؤول**\n\n"
        f"لإتمام عملية الشراء، يرجى التواصل مع المسؤول وإرسال الرمز التالي:\n\n"
        f"رمز الدفع: <code>{payment_code}</code>\n"
        f"اسم المسؤول: @{ADMIN_USERNAME}\n\n"
        f"بعد الدفع، قم بإرسال هذا الرمز للمسؤول لتأكيد طلبك."
    )
    
    await callback.message.edit_text(text, parse_mode="HTML")
    await clear_cart(user_id)
    
@router.message(F.text == "📄 طلباتي")
async def cmd_orders(message: types.Message):
    try:
        orders = await list_user_orders(message.from_user.id)
        if not orders:
            await message.answer("📭 ليس لديك طلبات سابقة.")
            return
        text = "<b>طلباتك السابقة:</b>\n\n"
        for o in orders:
            text += f"#{o['order_id']} — {o['status']} — {o['total']:.2f} {DEFAULT_CURRENCY}\n"
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Orders list error: {e}")
        await message.answer("⚠️ تعذر جلب طلباتك.")

@router.message(F.text == "👑 لوحة المشرف")
async def admin_panel(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    
    await message.answer("اختر من لوحة تحكم المشرف:", reply_markup=admin_panel_kb)

# New: Manage Store Panel
@router.message(F.text == "🛍️ إدارة المتجر")
async def manage_store_panel(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    
    await message.answer("اختر إجراءً لإدارة فئات المتجر:", reply_markup=manage_store_kb)

@router.message(F.text == "➕ إضافة فئة")
async def start_add_category(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    await message.answer("أرسل اسم الفئة الجديدة:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(ManageStoreState.category_name)

@router.message(ManageStoreState.category_name, F.text != "📝 تعديل فئة")
async def process_add_category_name(message: types.Message, state: FSMContext):
    category_name = message.text
    # Check if category already exists
    products = await list_products(category_name)
    if products:
        await message.answer(f"⚠️ الفئة '{category_name}' موجودة بالفعل. يرجى اختيار اسم آخر.")
        return
    
    # Create a dummy product to add the category
    await add_product_db(name=f"منتج وهمي للفئة {category_name}", price=0, stock=0, category=category_name, description="منتج وهمي لإنشاء الفئة", file_url="")
    await message.answer(f"✅ تم إضافة الفئة '{category_name}' بنجاح.", reply_markup=manage_store_kb)
    await state.clear()


@router.message(F.text == "📝 تعديل فئة")
async def start_edit_category(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    await message.answer("أرسل اسم الفئة التي تود تعديلها:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(ManageStoreState.old_category_name)

@router.message(ManageStoreState.old_category_name)
async def process_edit_category_name(message: types.Message, state: FSMContext):
    old_name = message.text
    products = await list_products(old_name)
    if not products:
        await message.answer(f"⚠️ الفئة '{old_name}' غير موجودة. يرجى إدخال اسم صحيح.")
        return
    await state.update_data(old_category_name=old_name)
    await message.answer("أرسل الاسم الجديد للفئة:")
    await state.set_state(ManageStoreState.category_name)

@router.message(ManageStoreState.category_name)
async def process_new_category_name(message: types.Message, state: FSMContext):
    data = await state.get_data()
    old_name = data.get('old_category_name')
    if not old_name: # This state is also used by add/delete, so check
        return
    new_name = message.text
    
    async with pool.acquire() as conn:
        await conn.execute("UPDATE products SET category = $1 WHERE category = $2", new_name, old_name)
    
    await message.answer(f"✅ تم تعديل اسم الفئة من '{old_name}' إلى '{new_name}' بنجاح.", reply_markup=manage_store_kb)
    await state.clear()

@router.message(F.text == "🗑️ حذف فئة")
async def start_delete_category(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    await message.answer("أرسل اسم الفئة التي تود حذفها (سيتم حذف جميع منتجاتها):", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(ManageStoreState.category_name)
    
@router.message(ManageStoreState.category_name)
async def process_delete_category(message: types.Message, state: FSMContext):
    category_name = message.text
    products = await list_products(category_name)
    if not products:
        await message.answer(f"⚠️ الفئة '{category_name}' غير موجودة. يرجى إدخال اسم صحيح.")
        return
        
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM products WHERE category = $1", category_name)
    
    await message.answer(f"✅ تم حذف الفئة '{category_name}' وجميع منتجاتها بنجاح.", reply_markup=manage_store_kb)
    await state.clear()


# ====== Product Management FSM ======
@router.message(F.text == "📦 إدارة المنتجات")
async def manage_products_panel(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    
    await message.answer("اختر إجراءً لإدارة المنتجات:", reply_markup=manage_products_kb)

# ====== Product Management FSM ======
@router.message(F.text == "➕ إضافة منتج")
async def start_add_product(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    await message.answer("أرسل اسم المنتج:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AddProductState.name)

@router.message(AddProductState.name)
async def process_product_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("أرسل سعر المنتج (مثلاً: 19.99):")
    await state.set_state(AddProductState.price)

@router.message(AddProductState.price)
async def process_product_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text)
        await state.update_data(price=price)
        await message.answer("أرسل الكمية المتوفرة في المخزون:")
        await state.set_state(AddProductState.stock)
    except ValueError:
        await message.answer("⚠️ السعر يجب أن يكون رقماً. أرسل السعر مرة أخرى.")

@router.message(AddProductState.stock)
async def process_product_stock(message: types.Message, state: FSMContext):
    try:
        stock = int(message.text)
        await state.update_data(stock=stock)
        await message.answer("أرسل تصنيف المنتج (مثلاً: دورة، خدمة، أخرى):")
        await state.set_state(AddProductState.category)
    except ValueError:
        await message.answer("⚠️ الكمية يجب أن تكون رقماً صحيحاً. أرسل الكمية مرة أخرى.")

@router.message(AddProductState.category)
async def process_product_category(message: types.Message, state: FSMContext):
    await state.update_data(category=message.text)
    await message.answer("أرسل وصف المنتج:")
    await state.set_state(AddProductState.description)
    
@router.message(AddProductState.description)
async def process_product_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await message.answer("أرسل رابط أو ملف المنتج:")
    await state.set_state(AddProductState.file_url)

@router.message(AddProductState.file_url)
async def process_product_file(message: types.Message, state: FSMContext):
    file_url = message.text
    if message.document:
        file_url = message.document.file_id
    elif message.photo:
        file_url = message.photo[-1].file_id

    await state.update_data(file_url=file_url)
    user_data = await state.get_data()
    
    await add_product_db(user_data['name'], user_data['price'], user_data['stock'], 
                        user_data['category'], user_data['description'], user_data['file_url'])
    await message.answer(f"✅ تم إضافة المنتج <b>{user_data['name']}</b> بنجاح.", 
                        reply_markup=manage_products_kb, parse_mode="HTML")
    await state.clear()

# ====== AI Product Addition FSM ======
@router.message(F.text == "✨ إضافة منتج بالذكاء الاصطناعي")
async def start_add_product_ai(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] != 'owner':
        await message.answer("🚫 ليس لديك صلاحيات المسؤول الكاملة.")
        return
    await message.answer("أرسل وصف المنتج الكامل (بما في ذلك الاسم، السعر، الروابط، إلخ). سيقوم الذكاء الاصطناعي باستخراج البيانات تلقائياً:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AddProductAIState.waiting_for_text)

@router.message(AddProductAIState.waiting_for_text)
async def process_product_text_ai(message: types.Message, state: FSMContext):
    await message.answer("جارٍ تحليل المنتج باستخدام الذكاء الاصطناعي... ⏳")
    
    product_data = await generate_product_data_with_ai(message.text)

    if not product_data or not product_data.get('name') or not product_data.get('price'):
        await message.answer("⚠️ تعذر استخراج بيانات المنتج. يرجى التأكد من أن الوصف يحتوي على اسم وسعر ورابط.", reply_markup=owner_panel_kb)
        await state.clear()
        return

    # حفظ البيانات المستخرجة في حالة FSM
    await state.update_data(**product_data)

    # عرض البيانات للمسؤول للتأكيد أو التعديل
    text = (
        "✅ تم استخراج البيانات التالية. هل ترغب في تأكيدها أو تعديلها؟\n\n"
        f"• **الاسم**: {product_data.get('name', 'غير متوفر')}\n"
        f"• **السعر**: {product_data.get('price', 'غير متوفر')}\n"
        f"• **التصنيف**: {product_data.get('category', 'غير متوفر')}\n"
        f"• **الوصف**: {product_data.get('description', 'غير متوفر')}\n"
        f"• **رابط الملف**: {product_data.get('file_url', 'غير متوفر')}\n"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأكيد الإضافة", callback_data="ai_confirm_add")],
        [InlineKeyboardButton(text="📝 تعديل يدوياً", callback_data="ai_edit_manually")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="ai_cancel")]
    ])
    
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(AddProductAIState.confirm_data)

@router.callback_query(F.data == "ai_confirm_add", AddProductAIState.confirm_data)
async def confirm_add_product_ai(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    try:
        await add_product_db(
            name=data.get('name'),
            price=data.get('price'),
            stock=100,
            category=data.get('category'),
            description=data.get('description'),
            file_url=data.get('file_url')
        )
        await callback.message.edit_text(
            f"✅ تم إضافة المنتج <b>{data['name']}</b> بنجاح إلى قاعدة البيانات.",
            parse_mode="HTML",
            reply_markup=None
        )
    except Exception as e:
        logger.error(f"Failed to add AI-generated product to DB: {e}")
        await callback.message.edit_text("⚠️ حدث خطأ أثناء حفظ المنتج في قاعدة البيانات. يرجى المحاولة مرة أخرى.", reply_markup=None)

    await state.clear()
    await callback.answer()

@router.callback_query(F.data == "ai_edit_manually", AddProductAIState.confirm_data)
async def edit_product_ai(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.set_state(EditAIProductState.name)
    await state.update_data(**data)
    await callback.message.edit_text("يرجى إرسال اسم المنتج الجديد:", reply_markup=None)
    await callback.answer()

@router.message(EditAIProductState.name)
async def process_edit_ai_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("أرسل السعر الجديد للمنتج:")
    await state.set_state(EditAIProductState.price)

@router.message(EditAIProductState.price)
async def process_edit_ai_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text)
        await state.update_data(price=price)
        await message.answer("أرسل الكمية الجديدة المتوفرة:")
        await state.set_state(EditAIProductState.stock)
    except ValueError:
        await message.answer("⚠️ السعر يجب أن يكون رقماً. أرسل السعر مرة أخرى.")

@router.message(EditAIProductState.stock)
async def process_edit_ai_stock(message: types.Message, state: FSMContext):
    try:
        stock = int(message.text)
        await state.update_data(stock=stock)
        await message.answer("أرسل التصنيف الجديد للمنتج:")
        await state.set_state(EditAIProductState.category)
    except ValueError:
        await message.answer("⚠️ الكمية يجب أن تكون رقماً صحيحاً. أرسل الكمية مرة أخرى.")

@router.message(EditAIProductState.category)
async def process_edit_ai_category(message: types.Message, state: FSMContext):
    await state.update_data(category=message.text)
    await message.answer("أرسل وصف المنتج الجديد:")
    await state.set_state(EditAIProductState.description)
    
@router.message(EditAIProductState.description)
async def process_edit_ai_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await message.answer("أرسل رابط أو ملف المنتج:")
    await state.set_state(EditAIProductState.file_url)

@router.message(EditAIProductState.file_url)
async def process_edit_ai_file(message: types.Message, state: FSMContext):
    file_url = message.text
    if message.document:
        file_url = message.document.file_id
    elif message.photo:
        file_url = message.photo[-1].file_id

    await state.update_data(file_url=file_url)
    user_data = await state.get_data()
    
    await add_product_db(user_data['name'], user_data['price'], user_data['stock'], 
                        user_data['category'], user_data['description'], user_data['file_url'])
    await message.answer(f"✅ تم إضافة المنتج <b>{user_data['name']}</b> بنجاح.", 
                        reply_markup=manage_products_kb, parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data == "ai_cancel", AddProductAIState.confirm_data)
async def cancel_add_product_ai(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("تم إلغاء عملية إضافة المنتج.", reply_markup=None)
    await callback.answer()

# ====== Edit Product FSM ======
@router.message(F.text == "📝 تعديل منتج")
async def start_edit_product(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    await message.answer("أرسل رقم المنتج الذي تود تعديله:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(EditProductState.product_id)

@router.message(EditProductState.product_id)
async def process_edit_product_id(message: types.Message, state: FSMContext):
    try:
        pid = int(message.text)
        product = await get_product_by_id(pid)
        if not product:
            await message.answer("⚠️ لم يتم العثور على المنتج. أرسل رقماً صحيحاً.")
            return
        await state.update_data(product_id=pid)
        await message.answer(f"أرسل الاسم الجديد للمنتج <code>{product['name']}</code>:", parse_mode="HTML")
        await state.set_state(EditProductState.name)
    except ValueError:
        await message.answer("⚠️ رقم المنتج يجب أن يكون رقماً. أرسل الرقم مرة أخرى.")

@router.message(EditProductState.name)
async def process_edit_product_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("أرسل السعر الجديد للمنتج:")
    await state.set_state(EditProductState.price)

@router.message(EditProductState.price)
async def process_edit_product_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text)
        await state.update_data(price=price)
        await message.answer("أرسل الكمية الجديدة المتوفرة:")
        await state.set_state(EditProductState.stock)
    except ValueError:
        await message.answer("⚠️ السعر يجب أن يكون رقماً. أرسل السعر مرة أخرى.")

@router.message(EditProductState.stock)
async def process_edit_product_stock(message: types.Message, state: FSMContext):
    try:
        stock = int(message.text)
        await state.update_data(stock=stock)
        await message.answer("أرسل التصنيف الجديد للمنتج:")
        await state.set_state(EditProductState.category)
    except ValueError:
        await message.answer("⚠️ الكمية يجب أن تكون رقماً صحيحاً. أرسل الكمية مرة أخرى.")

@router.message(EditProductState.category)
async def process_edit_product_category(message: types.Message, state: FSMContext):
    await state.update_data(category=message.text)
    await message.answer("أرسل وصف المنتج الجديد:")
    await state.set_state(EditProductState.description)

@router.message(EditProductState.description)
async def process_edit_product_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    user_data = await state.get_data()
    await edit_product_db(user_data['product_id'], user_data['name'], user_data['price'], 
                         user_data['stock'], user_data['category'], user_data['description'])
    await message.answer(f"✅ تم تعديل المنتج #{user_data['product_id']} بنجاح.", reply_markup=manage_products_kb)
    await state.clear()

# ====== Delete Product FSM ======
@router.message(F.text == "🗑️ حذف منتج")
async def start_delete_product(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    await message.answer("أرسل رقم المنتج الذي تود حذفه:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(DeleteProductState.product_id)

@router.message(DeleteProductState.product_id)
async def process_delete_product_id(message: types.Message, state: FSMContext):
    try:
        pid = int(message.text)
        product = await get_product_by_id(pid)
        if not product:
            await message.answer("⚠️ لم يتم العثور على المنتج. أرسل رقماً صحيحاً.")
            return
        await delete_product_db(pid)
        await message.answer(f"✅ تم حذف المنتج <b>{product['name']}</b> بنجاح.", 
                           reply_markup=manage_products_kb, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer("⚠️ رقم المنتج يجب أن يكون رقماً. أرسل الرقم مرة أخرى.")

@router.message(F.text == "📜 عرض المنتجات")
async def list_products_admin_handler(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    products = await list_products()
    if not products:
        await message.answer("لا توجد منتجات حالياً.")
        return
    text = "📦 **قائمة المنتجات:**\n\n"
    for p in products:
        text += f"- <code>#{p['product_id']}</code>: <b>{p['name']}</b>\n  السعر: {p['price']:.2f} {DEFAULT_CURRENCY} ({p['price'] * DZD_TO_USD_RATE:.2f} دينار جزائري)\n  المخزون: {p['stock']}\n  التصنيف: {p['category']}\n"
    await message.answer(text, parse_mode="HTML")

# ====== Coupons Management ======
@router.message(F.text == "🏷️ إدارة الكوبونات")
async def manage_coupons_panel(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    
    await message.answer("اختر إجراءً لإدارة الكوبونات:", reply_markup=manage_coupons_kb)

@router.message(F.text == "➕ إضافة كوبون")
async def start_add_coupon(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    
    await message.answer("أرسل رمز الكوبون:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AddCouponState.code)

@router.message(AddCouponState.code)
async def process_coupon_code(message: types.Message, state: FSMContext):
    await state.update_data(code=message.text)
    await message.answer("أرسل قيمة الخصم (نسبة مئوية، مثلاً: 10):")
    await state.set_state(AddCouponState.discount)

@router.message(AddCouponState.discount)
async def process_coupon_discount(message: types.Message, state: FSMContext):
    try:
        discount = float(message.text)
        await state.update_data(discount=discount)
        user_data = await state.get_data()
        await add_coupon_db(user_data['code'], user_data['discount'])
        await message.answer(f"✅ تم إضافة الكوبون <b>{user_data['code']}</b> بنجاح.", 
                           reply_markup=manage_coupons_kb, parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer("⚠️ الخصم يجب أن يكون رقماً. أرسل القيمة مرة أخرى.")

@router.message(F.text == "🗑️ حذف كوبون")
async def start_delete_coupon(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    await message.answer("أرسل رمز الكوبون الذي تود حذفه:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(DeleteCouponState.code)

@router.message(DeleteCouponState.code)
async def process_delete_coupon_code(message: types.Message, state: FSMContext):
    code = message.text
    coupon = await get_coupon_db(code)
    if not coupon:
        await message.answer("⚠️ لم يتم العثور على الكوبون. أرسل رمزاً صحيحاً.")
        return
    await delete_coupon_db(code)
    await message.answer(f"✅ تم حذف الكوبون <b>{code}</b> بنجاح.", 
                        reply_markup=manage_coupons_kb, parse_mode="HTML")
    await state.clear()

@router.message(F.text == "📜 عرض الكوبونات")
async def list_coupons_admin_handler(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    
    coupons = await list_coupons_db()
    if not coupons:
        await message.answer("لا توجد كوبونات حالياً.")
        return
    
    text = "🏷️ **قائمة الكوبونات:**\n\n"
    for c in coupons:
        text += f"- <code>{c['code']}</code>: خصم {c['discount']:.0f}%\n"
    await message.answer(text, parse_mode="HTML")

# ====== Orders Management ======
@router.message(F.text == "📝 إدارة الطلبات")
async def manage_orders_panel(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    try:
        orders = await list_pending_orders()
        if not orders:
            await message.answer("✅ لا توجد طلبات معلقة حالياً.")
            return
        for o in orders:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ قبول", callback_data=f"accept:{o['order_id']}"),
                 InlineKeyboardButton(text="❌ رفض", callback_data=f"reject:{o['order_id']}")]
            ])
            await message.answer(f"طلب جديد #{o['order_id']}\nالمجموع: {o['total']:.2f} {DEFAULT_CURRENCY} ({o['total'] * DZD_TO_USD_RATE:.2f} دينار جزائري)", reply_markup=kb)
    except Exception as e:
        logger.error(f"Admin panel error: {e}")
        await message.answer("⚠️ حدث خطأ في جلب الطلبات.")

@router.callback_query(F.data.startswith(("accept", "reject")))
async def process_order_action(callback: types.CallbackQuery, bot: Bot):
    user_data = await get_user_data(callback.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await callback.answer("🚫 لا تملك صلاحيات.", show_alert=True)
        return
    action, order_id = callback.data.split(":")
    status = "مقبول ✅" if action == "accept" else "مرفوض ❌"
    try:
        order = await get_order_by_id(int(order_id))
        if not order:
            await callback.answer("الطلب غير موجود.", show_alert=True)
            return

        await update_order_status(int(order_id), status)
        
        # إرسال المنتجات والفاتورة للمستخدم عند القبول
        if action == "accept":
            order_items = await get_order_items(int(order_id))
            
            # Prepare invoice
            invoice_text = (
                f"✅ **تم تأكيد طلبك رقم {order_id}**\n\n"
                f"إليك المنتجات التي قمت بشرائها:\n"
            )
            
            for item in order_items:
                product = await get_product_by_id(item['product_id'])
                if product:
                    invoice_text += f"- {product['name']} (الكمية: {item['quantity']})\n"
                    # Send download link if available
                    if product['file_url']:
                        await bot.send_message(order['user_id'], 
                                               f"📦 رابط منتجك <b>{product['name']}</b>:\n{product['file_url']}", 
                                               parse_mode="HTML")
            
            invoice_text += f"\n• الإجمالي: <b>{order['total']:.2f} {DEFAULT_CURRENCY}</b> ({order['total'] * DZD_TO_USD_RATE:.2f} دينار جزائري)\n"
            invoice_text += f"• رمز الفاتورة: <code>{order_id}</code>"
            
            await bot.send_message(order['user_id'], invoice_text, parse_mode="HTML")
            await callback.message.edit_text(f"✅ تم قبول الطلب #{order_id} بنجاح.")
        else:
            await callback.message.edit_text(f"❌ تم رفض الطلب #{order_id}.")

        await callback.answer(f"تم تحديث حالة الطلب.", show_alert=True)

    except Exception as e:
        logger.error(f"Order update error: {e}")
        await callback.answer("⚠️ تعذر تحديث الطلب.", show_alert=True)

# New: View Order Details
@router.message(F.text == "🔍 تفاصيل طلب")
async def start_view_order_details(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    await message.answer("أرسل رقم الطلب الذي تود عرض تفاصيله:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(ViewOrderDetailsState.order_id)

@router.message(ViewOrderDetailsState.order_id)
async def process_view_order_details(message: types.Message, state: FSMContext):
    try:
        order_id = int(message.text)
        order = await get_order_by_id(order_id)
        if not order:
            await message.answer("⚠️ لم يتم العثور على الطلب. يرجى إدخال رقم صحيح.")
            return

        items = await get_order_items(order_id)
        if not items:
            await message.answer(f"الطلب #{order_id} لا يحتوي على أي منتجات.", reply_markup=admin_panel_kb)
            await state.clear()
            return

        user = await get_user_by_id(order['user_id'])
        
        text = (
            f"📝 **تفاصيل الطلب #{order_id}**\n\n"
            f"• حالة الطلب: <b>{order['status']}</b>\n"
            f"• إجمالي السعر: <b>{order['total']:.2f} {DEFAULT_CURRENCY}</b> ({order['total'] * DZD_TO_USD_RATE:.2f} دينار جزائري)\n"
            f"• تاريخ الطلب: {order['created_at']}\n"
            f"• المستخدم: {user['first_name']} (<code>{user['user_id']}</code>)\n\n"
            f"📦 **المنتجات:**\n"
        )
        
        for item in items:
            text += f"- {item['name']} (الكمية: {item['quantity']})\n"
        
        await message.answer(text, parse_mode="HTML", reply_markup=admin_panel_kb)
        await state.clear()
        
    except ValueError:
        await message.answer("⚠️ رقم الطلب يجب أن يكون رقماً. أرسل الرقم مرة أخرى.")
    except Exception as e:
        logger.error(f"Error viewing order details: {e}")
        await message.answer("⚠️ حدث خطأ أثناء جلب تفاصيل الطلب.")


# ====== Statistics ======
@router.message(F.text == "📊 الإحصائيات")
async def get_stats_panel(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    
    total_sales = await get_total_sales_db()
    total_orders = await get_total_orders_db()
    most_popular_products = await get_most_popular_products()
    most_active_users = await get_most_active_users()
    referral_sources = await get_referral_sources_stats()
    
    text = "📊 **إحصائيات المتجر**\n\n"
    text += f"• إجمالي المبيعات: <b>{total_sales:.2f} {DEFAULT_CURRENCY}</b> ({total_sales * DZD_TO_USD_RATE:.2f} دينار جزائري)\n"
    text += f"• إجمالي عدد الطلبات: <b>{total_orders}</b>\n\n"
    
    # Popular Products
    text += "🏆 **المنتجات الأكثر مبيعاً:**\n"
    if most_popular_products:
        for p in most_popular_products:
            text += f"- {p['name']}: {p['total_sold']} مرة\n"
    else:
        text += "لا توجد بيانات.\n"
    
    # Active Users
    text += "\n👥 **المستخدمون الأكثر نشاطاً (حسب الطلبات):**\n"
    if most_active_users:
        for u in most_active_users:
            text += f"- {u['first_name']}: {u['total_orders']} طلب\n"
    else:
        text += "لا توجد بيانات.\n"
        
    # Referral Sources
    text += "\n🔗 **مصادر الإحالة الأكثر فاعلية:**\n"
    if referral_sources:
        for r in referral_sources:
            text += f"- {r['first_name']}: {r['total_referrals']} إحالة\n"
    else:
        text += "لا توجد بيانات.\n"
    
    await message.answer(text, parse_mode="HTML")

# ====== Users Management ======
@router.message(F.text == "👤 إدارة المستخدمين")
async def manage_users_panel(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    
    await message.answer("اختر إجراءً لإدارة المستخدمين:", reply_markup=manage_users_kb)

@router.message(F.text == "➕ إضافة نقاط")
async def start_add_points(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    await message.answer("أرسل رقم المستخدم الذي تود إضافة نقاط إليه:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AddPointsState.user_id)

@router.message(AddPointsState.user_id)
async def process_add_points_user_id(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        user = await get_user_by_id(user_id)
        if not user:
            await message.answer("⚠️ لم يتم العثور على المستخدم. أرسل رقماً صحيحاً.")
            return
        await state.update_data(user_id=user_id)
        await message.answer("أرسل عدد النقاط التي تود إضافتها:")
        await state.set_state(AddPointsState.points)
    except ValueError:
        await message.answer("⚠️ رقم المستخدم يجب أن يكون رقماً. أرسل الرقم مرة أخرى.")

@router.message(AddPointsState.points)
async def process_add_points(message: types.Message, state: FSMContext):
    try:
        points = int(message.text)
        user_data = await state.get_data()
        await add_user_points_db(user_data['user_id'], points)
        await message.answer(f"✅ تم إضافة {points} نقطة للمستخدم #{user_data['user_id']} بنجاح.", reply_markup=manage_users_kb)
        await state.clear()
    except ValueError:
        await message.answer("⚠️ عدد النقاط يجب أن يكون رقماً صحيحاً. أرسل العدد مرة أخرى.")

@router.message(F.text == "➖ حذف نقاط")
async def start_deduct_points(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    await message.answer("أرسل رقم المستخدم الذي تود خصم نقاط منه:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(DeductPointsState.user_id)

@router.message(DeductPointsState.user_id)
async def process_deduct_points_user_id(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        user = await get_user_by_id(user_id)
        if not user:
            await message.answer("⚠️ لم يتم العثور على المستخدم. أرسل رقماً صحيحاً.")
            return
        await state.update_data(user_id=user_id)
        await message.answer("أرسل عدد النقاط التي تود خصمها:")
        await state.set_state(DeductPointsState.points)
    except ValueError:
        await message.answer("⚠️ رقم المستخدم يجب أن يكون رقماً. أرسل الرقم مرة أخرى.")

@router.message(DeductPointsState.points)
async def process_deduct_points(message: types.Message, state: FSMContext):
    try:
        points = int(message.text)
        user_data = await state.get_data()
        await deduct_user_points_db(user_data['user_id'], points)
        await message.answer(f"✅ تم خصم {points} نقطة من المستخدم #{user_data['user_id']} بنجاح.", reply_markup=manage_users_kb)
        await state.clear()
    except ValueError:
        await message.answer("⚠️ عدد النقاط يجب أن يكون رقماً صحيحاً. أرسل العدد مرة أخرى.")

@router.message(F.text == "🔍 عرض بيانات مستخدم")
async def start_get_user_info(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    await message.answer("أرسل رقم المستخدم الذي تود عرض بياناته:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(GetUserInfoState.user_id)

@router.message(GetUserInfoState.user_id)
async def process_get_user_info_id(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        user = await get_user_by_id(user_id)
        if not user:
            await message.answer(f"⚠️ لم يتم العثور على المستخدم #{user_id}.")
            await state.clear()
            return
        text = (
            f"👤 **بيانات المستخدم #{user_id}**\n\n"
            f"• الاسم: {user['first_name']}\n"
            f"• النقاط: {user['points']}\n"
            f"• الإحالات: {user['referrals']}\n"
            f"• كود الإحالة: <code>{user['ref_code']}</code>"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=manage_users_kb)
        await state.clear()
    except ValueError:
        await message.answer("⚠️ رقم المستخدم يجب أن يكون رقماً. أرسل الرقم مرة أخرى.")

# ====== Payment Methods Management ======
@router.message(F.text == "💰 طرق الدفع")
async def manage_payments_panel(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    
    await message.answer("اختر إجراءً لإدارة طرق الدفع:", reply_markup=manage_payments_kb)

@router.message(F.text == "➕ إضافة طريقة دفع")
async def start_add_payment_method(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    await message.answer("أرسل اسم طريقة الدفع:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AddPaymentState.name)

@router.message(AddPaymentState.name)
async def process_add_payment_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("أرسل تفاصيل طريقة الدفع (مثلاً: رقم الحساب، اسم البنك، إلخ):")
    await state.set_state(AddPaymentState.details)

@router.message(AddPaymentState.details)
async def process_add_payment_details(message: types.Message, state: FSMContext):
    await state.update_data(details=message.text)
    user_data = await state.get_data()
    await add_payment_method_db(user_data['name'], user_data['details'])
    await message.answer(f"✅ تم إضافة طريقة الدفع <b>{user_data['name']}</b> بنجاح.", 
                        reply_markup=manage_payments_kb, parse_mode="HTML")
    await state.clear()

@router.message(F.text == "🗑️ حذف طريقة دفع")
async def start_delete_payment_method(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    await message.answer("أرسل رقم طريقة الدفع التي تود حذفها:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(DeletePaymentState.id)

@router.message(DeletePaymentState.id)
async def process_delete_payment_id(message: types.Message, state: FSMContext):
    try:
        payment_id = int(message.text)
        await delete_payment_method_db(payment_id)
        await message.answer(f"✅ تم حذف طريقة الدفع #{payment_id} بنجاح.", reply_markup=manage_payments_kb)
        await state.clear()
    except ValueError:
        await message.answer("⚠️ رقم طريقة الدفع يجب أن يكون رقماً. أرسل الرقم مرة أخرى.")

@router.message(F.text == "📜 عرض طرق الدفع")
async def list_payments_admin_handler(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    
    payments = await list_payment_methods_db()
    if not payments:
        await message.answer("لا توجد طرق دفع حالياً.")
        return
    
    text = "💰 **طرق الدفع المتوفرة:**\n\n"
    for p in payments:
        text += f"- <code>#{p['id']}</code>: <b>{p['name']}</b>\n  التفاصيل: {p['details']}\n"
    await message.answer(text, parse_mode="HTML")

@router.message(F.text == "✔️ التحقق من الدفع")
async def start_verify_payment(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    await message.answer("أرسل رمز الدفع الذي تود التحقق منه:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(VerifyPaymentState.code)

@router.message(VerifyPaymentState.code)
async def process_verify_payment_code(message: types.Message, state: FSMContext, bot: Bot):
    code = message.text
    payment = await get_payment_by_code(code)
    if not payment:
        await message.answer("⚠️ رمز الدفع غير صالح أو غير موجود.")
        await state.clear()
        return

    order = await get_order_by_id(payment['order_id'])
    if not order:
        await message.answer("⚠️ لم يتم العثور على الطلب المرتبط بهذا الرمز.")
        await state.clear()
        return
    
    order_items = await get_order_items(order['order_id'])
    for item in order_items:
        product = await get_product_by_id(item['product_id'])
        if product and product['file_url']:
            await bot.send_message(order['user_id'], 
                                 f"✅ تم تأكيد دفعك! إليك رابط منتجك <b>{product['name']}</b>:\n{product['file_url']}", 
                                 parse_mode="HTML")

    await update_order_status(order['order_id'], "مقبول ✅")
    await update_payment_status(order['order_id'], "completed")
    
    # -- تعديل: إضافة نقاط الإحالة عند الشراء اليدوي
    user_data = await get_user_by_id(order['user_id'])
    if user_data and user_data['referred_by']:
        referrer_id = user_data['referred_by']
        await add_points(referrer_id, REFERRAL_PURCHASE_BONUS_POINTS)
        try:
            await bot.send_message(
                referrer_id,
                f"🎉 تهانينا! الشخص الذي قمت بإحالته أكمل عملية شراء، وحصلت على {REFERRAL_PURCHASE_BONUS_POINTS} نقطة إضافية."
            )
        except Exception as e:
            logger.error(f"Failed to notify referrer {referrer_id} on manual purchase: {e}")

    await message.answer(f"✅ تم تأكيد الدفع للطلب #{order['order_id']} بنجاح.", reply_markup=admin_panel_kb)
    await state.clear()


# ====== Back to Main Menu ======
@router.message(F.text == "🔙 العودة للقائمة الرئيسية")
async def back_to_main_menu(message: types.Message, state: FSMContext):
    await state.clear()
    user_data = await get_user_data(message.from_user.id)
    
    if user_data['role'] == 'owner':
        await message.answer(f"مرحباً بك يا مسؤول، {message.from_user.full_name}!", reply_markup=owner_panel_kb)
    elif user_data['role'] == 'admin':
        await message.answer(f"مرحباً بك يا مشرف، {message.from_user.full_name}!", reply_markup=admin_panel_kb)
    else:
        await message.answer(f"مرحباً بك في بوت المتجر، {message.from_user.full_name}!", reply_markup=main_kb_user)

# ====== Owner-only Role Management ======
@router.message(F.text == "⚙️ إدارة الصلاحيات")
async def manage_roles_panel(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] != 'owner':
        await message.answer("🚫 ليس لديك صلاحيات المسؤول الكاملة.")
        return
    
    await message.answer("أرسل رقم المستخدم الذي تود تعديل صلاحياته:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(ManageRolesState.user_id)

@router.message(ManageRolesState.user_id)
async def process_manage_roles_user_id(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        user = await get_user_by_id(user_id)
        if not user:
            await message.answer("⚠️ لم يتم العثور على المستخدم. أرسل رقماً صحيحاً.")
            await state.clear()
            return

        if user['role'] == 'owner':
            await message.answer("لا يمكنك تعديل صلاحيات المسؤول.")
            await state.clear()
            return

        await state.update_data(user_id=user_id)
        
        await message.answer(f"اختر صلاحية جديدة للمستخدم <b>{user['first_name']}</b>:", 
                             reply_markup=manage_roles_kb, parse_mode="HTML")
        await state.set_state(ManageRolesState.role)
    except ValueError:
        await message.answer("⚠️ رقم المستخدم يجب أن يكون رقماً. أرسل الرقم مرة أخرى.")

@router.callback_query(F.data.startswith("set_role:"), ManageRolesState.role)
async def process_manage_roles_callback(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = data['user_id']
    new_role = callback.data.split(":")[1]
    
    user_to_update = await get_user_by_id(user_id)
    if not user_to_update:
        await callback.answer("⚠️ المستخدم غير موجود.", show_alert=True)
        await state.clear()
        return

    if user_to_update['role'] == 'owner':
        await callback.answer("لا يمكنك تعديل صلاحيات المسؤول.", show_alert=True)
        await state.clear()
        return

    await update_user_role(user_id, new_role)
    await callback.message.edit_text(f"✅ تم تعيين صلاحية المستخدم <b>{user_to_update['first_name']}</b> إلى <b>{new_role}</b> بنجاح.", parse_mode="HTML")
    await state.clear()

# ====== Notification System ======
@router.message(F.text == "📢 إرسال إشعار")
async def start_notify_users(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data['role'] not in ['admin', 'owner']:
        await message.answer("🚫 ليس لديك صلاحيات.")
        return
    
    await message.answer("اختر مجموعة المستخدمين التي تود إرسال الإشعار إليها:", reply_markup=notify_users_kb)
    await state.set_state(NotifyUsersState.target)

@router.callback_query(F.data == "notify_all", NotifyUsersState.target)
async def notify_all_users(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(target='all')
    await callback.message.edit_text("أرسل رسالة الإشعار:")
    await state.set_state(NotifyUsersState.message_text)

@router.callback_query(F.data == "notify_group", NotifyUsersState.target)
async def notify_group_of_users(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(target='group')
    await callback.message.edit_text("أرسل رسالة الإشعار:")
    await state.set_state(NotifyUsersState.message_text)

@router.message(NotifyUsersState.message_text)
async def process_notification_message(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target = data['target']
    message_text = message.text
    
    async with pool.acquire() as conn:
        if target == 'all':
            users_to_notify = await conn.fetch("SELECT user_id FROM users")
        else: # group - send to users who interacted recently
            users_to_notify = await conn.fetch("SELECT user_id FROM users ORDER BY created_at DESC LIMIT 10")
        
    for user in users_to_notify:
        try:
            await message.bot.send_message(user['user_id'], f"📢 **إشعار من المسؤول:**\n\n{message_text}", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to send message to user {user['user_id']}: {e}")
            
    await message.answer("✅ تم إرسال الإشعار بنجاح.", reply_markup=admin_panel_kb)
    await state.clear()


# ====== Commands ======
@router.message(Command("coupon"))
async def cmd_coupon(message: types.Message, state: FSMContext):
    await state.clear()
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("استخدم: /coupon <code>")
        return
    code = parts[1]
    try:
        discount = await apply_coupon_db(code)
        if discount:
            await message.answer(f"🎉 تم تفعيل الكوبون! خصم {discount:.0f}% على سلتك.")
            await state.update_data(coupon_discount=discount)
        else:
            await message.answer("❌ الكوبون غير صالح أو منتهي.")
    except Exception as e:
        logger.error(f"Coupon error: {e}")
        await message.answer("⚠️ حدث خطأ أثناء تطبيق الكوبون.")

# ====== Main Function =====
async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not found in .env file.")
        return
    
    await init_db()
    
    # تأكد من أن المستخدمين في قائمة المسؤولين لديهم دور 'owner'
    if ADMINS:
        async with pool.acquire() as conn:
            for admin_id in ADMINS:
                await conn.execute("UPDATE users SET role = 'owner' WHERE user_id = $1", admin_id)
    
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    
    logger.info("Starting bot...")
    try:
        await dp.start_polling(bot)
    finally:
        if pool:
            await pool.close()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")
    except Exception as e:
        logger.error(f"An error occurred: {e}")
