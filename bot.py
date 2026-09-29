import asyncio
import io
import os
import re
import shutil
import sqlite3
import zipfile
from collections import defaultdict
from datetime import datetime, date, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from pypdf import PdfReader, PdfWriter, PageObject, Transformation
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

BOT_TOKEN = "8563063962:AAE8JCad_gceirSwOYM2162blqq_bD54MkQ"
ADMIN_ID = 1260202941  # Ваш Telegram ID
CHANNEL_LINK = "https://t.me/ksp_print"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Буферы для сбора файлов и задач
user_files_buffer = defaultdict(list)
user_tasks = {}
user_photo_tasks = {}
user_pick_lists = {}

# Регистрация кириллического шрифта
FONT_NAME = 'Helvetica'
font_candidates = [
    'arial.ttf',
    'Arial.ttf',
    'C:\\Windows\\Fonts\\arial.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
]

for font_path in font_candidates:
    if os.path.exists(font_path):
        try:
            pdfmetrics.registerFont(TTFont('CustomFont', font_path))
            FONT_NAME = 'CustomFont'
            break
        except Exception:
            pass


# --- ИНИЦИАЛИЗАЦИЯ И МИГРАЦИЯ БАЗЫ ДАННЫХ (SQLite) ---
def init_db():
    conn = sqlite3.connect("db.sqlite3")
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            daily_limit INTEGER DEFAULT 35,
            bonus_limit INTEGER DEFAULT 0,
            used_today INTEGER DEFAULT 0,
            last_active_date TEXT,
            user_batch_counter INTEGER DEFAULT 1,
            duplicate_mode INTEGER DEFAULT 0,
            paper_format TEXT DEFAULT 'thermal',
            referrer_id INTEGER DEFAULT NULL,
            referrals_count INTEGER DEFAULT 0,
            subscription_expires TEXT DEFAULT NULL,
            is_unlimited INTEGER DEFAULT 0,
            total_orders_count INTEGER DEFAULT 0
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS batch_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            orders_count INTEGER,
            processed_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sub_notifications (
            user_id INTEGER,
            notify_type TEXT,
            sent_date TEXT,
            PRIMARY KEY (user_id, notify_type, sent_date)
        )
    """)
    
    cursor.execute("PRAGMA table_info(users)")
    columns = [column[1] for column in cursor.fetchall()]
    
    if "user_batch_counter" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN user_batch_counter INTEGER DEFAULT 1")
    if "duplicate_mode" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN duplicate_mode INTEGER DEFAULT 0")
    if "paper_format" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN paper_format TEXT DEFAULT 'thermal'")
    if "referrer_id" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT NULL")
    if "referrals_count" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN referrals_count INTEGER DEFAULT 0")
    if "subscription_expires" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN subscription_expires TEXT DEFAULT NULL")
    if "is_unlimited" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN is_unlimited INTEGER DEFAULT 0")
    if "total_orders_count" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN total_orders_count INTEGER DEFAULT 0")
        
    conn.commit()
    conn.close()

init_db()


def get_or_create_user(user_id: int, username: str = None, referrer_id: int = None):
    today_str = date.today().isoformat()
    conn = sqlite3.connect("db.sqlite3")
    cursor = conn.cursor()
    
    cursor.execute(
        """SELECT daily_limit, bonus_limit, used_today, last_active_date, 
                  user_batch_counter, duplicate_mode, paper_format, referrals_count, 
                  subscription_expires, is_unlimited, total_orders_count 
           FROM users WHERE user_id = ?""", 
        (user_id,)
    )
    row = cursor.fetchone()
    
    is_new_user = False
    
    if row is None:
        is_new_user = True
        valid_referrer = referrer_id if referrer_id and referrer_id != user_id else None
        
        cursor.execute(
            """INSERT INTO users 
               (user_id, username, daily_limit, bonus_limit, used_today, last_active_date, 
                user_batch_counter, duplicate_mode, paper_format, referrer_id, referrals_count, subscription_expires, is_unlimited, total_orders_count) 
               VALUES (?, ?, 35, 0, 0, ?, 1, 0, 'thermal', ?, 0, NULL, 0, 0)""",
            (user_id, username, today_str, valid_referrer)
        )
        conn.commit()
        
        daily_limit, bonus_limit, used_today, batch_cnt, dup_mode, paper_fmt, refs_count = 35, 0, 0, 1, 0, 'thermal', 0
        sub_expires, is_unlimited, total_orders = None, 0, 0
        
        if valid_referrer:
            cursor.execute(
                "UPDATE users SET bonus_limit = bonus_limit + 10, referrals_count = referrals_count + 1 WHERE user_id = ?",
                (valid_referrer,)
            )
            conn.commit()
    else:
        daily_limit, bonus_limit, used_today, last_date, batch_cnt, dup_mode, paper_fmt, refs_count, sub_expires, is_unlimited, total_orders = row
        if username:
            cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            conn.commit()
            
        if last_date != today_str:
            used_today = 0
            bonus_limit = 0
            cursor.execute(
                "UPDATE users SET used_today = 0, bonus_limit = 0, last_active_date = ? WHERE user_id = ?",
                (today_str, user_id)
            )
            conn.commit()
            
    conn.close()

    has_active_sub = False
    if is_unlimited:
        has_active_sub = True
    elif sub_expires:
        try:
            exp_date = datetime.strptime(sub_expires, "%Y-%m-%d").date()
            if exp_date >= date.today():
                has_active_sub = True
        except ValueError:
            pass

    return {
        "daily_limit": daily_limit,
        "bonus_limit": bonus_limit,
        "used_today": used_today,
        "max_allowed": daily_limit + bonus_limit,
        "user_batch_counter": batch_cnt,
        "duplicate_mode": bool(dup_mode),
        "paper_format": paper_fmt or 'thermal',
        "referrals_count": refs_count,
        "is_new_user": is_new_user,
        "has_active_sub": has_active_sub,
        "sub_expires": sub_expires,
        "is_unlimited": bool(is_unlimited),
        "total_orders_count": total_orders
    }


def update_user_usage_and_batch(user_id: int, added_count: int):
    conn = sqlite3.connect("db.sqlite3")
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    
    cursor.execute("""
        UPDATE users 
        SET used_today = used_today + ?, 
            user_batch_counter = user_batch_counter + 1,
            total_orders_count = total_orders_count + ?
        WHERE user_id = ?
    """, (added_count, added_count, user_id))
    
    cursor.execute("""
        INSERT INTO batch_history (user_id, orders_count, processed_at)
        VALUES (?, ?, ?)
    """, (user_id, added_count, now_str))
    
    conn.commit()
    conn.close()


def set_user_paper_format(user_id: int, paper_format: str):
    conn = sqlite3.connect("db.sqlite3")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET paper_format = ? WHERE user_id = ?", (paper_format, user_id))
    conn.commit()
    conn.close()


def get_monthly_statistics(user_id: int):
    conn = sqlite3.connect("db.sqlite3")
    cursor = conn.cursor()
    first_day_of_month = date.today().replace(day=1).isoformat()
    cursor.execute("""
        SELECT SUM(orders_count), COUNT(id)
        FROM batch_history
        WHERE user_id = ? AND processed_at >= ?
    """, (user_id, first_day_of_month))
    row = cursor.fetchone()
    conn.close()
    monthly_orders = row[0] if row[0] else 0
    batches_count = row[1] if row[1] else 0
    saved_minutes_total = monthly_orders * 0.5
    hours = int(saved_minutes_total // 60)
    minutes = int(saved_minutes_total % 60)
    return {"monthly_orders": monthly_orders, "batches_count": batches_count, "hours": hours, "minutes": minutes}


def toggle_user_duplicate_mode(user_id: int) -> bool:
    conn = sqlite3.connect("db.sqlite3")
    cursor = conn.cursor()
    cursor.execute("SELECT duplicate_mode FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    current_mode = row[0] if row else 0
    new_mode = 1 if current_mode == 0 else 0
    cursor.execute("UPDATE users SET duplicate_mode = ? WHERE user_id = ?", (new_mode, user_id))
    conn.commit()
    conn.close()
    return bool(new_mode)


# --- ФУНКЦИИ РАСКЛАДКИ А4 И ОБРЕЗКИ ---
def create_a4_grid_background(mode="4in1") -> io.BytesIO:
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(595.27, 841.89))
    c.setDash(4, 4)
    c.setStrokeColor(HexColor("#AAAAAA"))
    c.setLineWidth(0.5)

    if mode == "4in1":
        c.line(0, 420.94, 595.27, 420.94)
        c.line(297.63, 0, 297.63, 841.89)
    elif mode == "8in1":
        c.line(297.63, 0, 297.63, 841.89)
        for i in range(1, 4):
            y = i * 210.47
            c.line(0, y, 595.27, y)
    elif mode == "9in1":
        for i in range(1, 3):
            x = i * 198.42
            c.line(x, 0, x, 841.89)
            y = i * 280.63
            c.line(0, y, 595.27, y)

    c.save()
    packet.seek(0)
    return packet


def merge_pages_n_up(pages_list, mode="4in1"):
    writer = PdfWriter()
    a4_w, a4_h = 595.27, 841.89

    if mode == "4in1":
        items_per_page, cols, rows = 4, 2, 2
        cell_w, cell_h = 297.63, 420.94
    elif mode == "8in1":
        items_per_page, cols, rows = 8, 2, 4
        cell_w, cell_h = 297.63, 210.47
    elif mode == "9in1":
        items_per_page, cols, rows = 9, 3, 3
        cell_w, cell_h = 198.42, 280.63
    else:
        items_per_page, cols, rows = 4, 2, 2
        cell_w, cell_h = 297.63, 420.94

    grid_bg_stream = create_a4_grid_background(mode)
    grid_bg_reader = PdfReader(grid_bg_stream)
    grid_bg_page = grid_bg_reader.pages[0]

    for i in range(0, len(pages_list), items_per_page):
        batch = pages_list[i:i + items_per_page]
        new_page = PageObject.create_blank_page(width=a4_w, height=a4_h)
        new_page.merge_page(grid_bg_page)

        for idx, original_p in enumerate(batch):
            row = rows - 1 - (idx // cols)
            col = idx % cols
            tx = col * cell_w
            ty = row * cell_h

            p_w = float(original_p.mediabox.width)
            p_h = float(original_p.mediabox.height)

            scale = min(cell_w / p_w, cell_h / p_h) * 0.95

            offset_x = tx + (cell_w - p_w * scale) / 2
            offset_y = ty + (cell_h - p_h * scale) / 2

            transform = Transformation().scale(scale, scale).translate(offset_x, offset_y)
            
            p_copy = PageObject.create_blank_page(width=p_w, height=p_h)
            p_copy.merge_page(original_p)
            p_copy.add_transformation(transform)
            new_page.merge_page(p_copy)

        writer.add_page(new_page)

    return writer


def crop_a4_top_left_to_thermal(page):
    page.mediabox.lower_left = (0, 420)
    page.mediabox.upper_right = (298, 842)
    page.cropbox.lower_left = (0, 420)
    page.cropbox.upper_right = (298, 842)
    return page


def detect_delivery_type(text: str) -> str:
    text_lower = text.lower()
    if "express" in text_lower or "яндекс" in text_lower or "достависта" in text_lower:
        return "⚡️ Kaspi Express / Яндекс"
    elif "самовывоз" in text_lower:
        return "🏬 Самовывоз"
    else:
        return "📦 Kaspi Доставка (ПВЗ/Курьер)"


def parse_and_sort_pdf_pages(pdf_files: list):
    grouped_pages = defaultdict(list)
    items_count = defaultdict(int)
    
    for pdf_path in pdf_files:
        try:
            reader = PdfReader(pdf_path)
            for page in reader.pages:
                text = page.extract_text() or ""
                delivery_type = detect_delivery_type(text)
                grouped_pages[delivery_type].append(page)
                
                lines = text.split('\n')
                for line in lines:
                    match = re.search(r'(.+?)\s+(\d+)\s*(?:шт|ед|\b)', line, re.IGNORECASE)
                    if match:
                        item_name = match.group(1).strip()
                        if any(kw in item_name.lower() for kw in ["наименование", "заказ", "клиент", "итого", "номер"]):
                            continue
                        qty = int(match.group(2))
                        if len(item_name) > 2:
                            items_count[item_name] += qty
        except Exception as e:
            print(f"Ошибка при извлечении текста из {pdf_path}: {e}")
            
    return grouped_pages, dict(items_count)


# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="🚀 Старт бота")
    builder.button(text="📊 Статистика за месяц")
    builder.button(text="⚙️ Настройки")
    builder.button(text="🎁 Приведи друга")
    builder.button(text="📖 Инструкция")
    builder.button(text="⭐ Тарифы и подписка")
    builder.button(text="🖨 Принтер (XP-365B)")
    builder.button(text="📢 Наш канал / Отзывы")
    builder.button(text="💬 Поддержка")
    builder.adjust(2, 2, 2, 2, 1)
    return builder.as_markup(resize_keyboard=True)


def get_batch_result_keyboard(batch_num: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="📦 Показать Лист сборки", callback_data=f"show_picklist_{batch_num}")
    builder.button(text="💬 Поддержка", url="https://t.me/baur_bkh")
    builder.adjust(1, 1)
    return builder.as_markup()


def get_settings_inline_keyboard(duplicate_enabled: bool, paper_format: str):
    builder = InlineKeyboardBuilder()
    dup_status = "🟢 ВКЛ" if duplicate_enabled else "🔴 ВЫКЛ"
    
    formats_labels = {
        "thermal": "🏷 Термопринтер (Без изм.)",
        "a4_crop_thermal": "✂️ А4 на термопринтер (Обрезка)",
        "a4_4in1": "📄 4 на 1 лист (А4)",
        "a4_8in1": "📄 8 на 1 лист (А4)",
        "a4_9in1": "📄 9 на 1 лист (А4)"
    }
    current_fmt_label = formats_labels.get(paper_format, "🏷 Термопринтер")
    
    builder.button(text=f"📄 Дублирование этикеток: [{dup_status}]", callback_data="toggle_duplicate")
    builder.button(text=f"🖨 Формат печати: [{current_fmt_label}]", callback_data="change_paper_format")
    builder.adjust(1, 1)
    return builder.as_markup()


def get_paper_format_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🏷 Термопринтер (Без изменений)", callback_data="set_fmt_thermal")
    builder.button(text="✂️ А4 на термопринтер (Авто-обрезка 1/4 листа)", callback_data="set_fmt_a4_crop_thermal")
    builder.button(text="📄 4 на 1 лист (А4 с пунктиром)", callback_data="set_fmt_a4_4in1")
    builder.button(text="📄 8 на 1 лист (А4 с пунктиром)", callback_data="set_fmt_a4_8in1")
    builder.button(text="📄 9 на 1 лист (А4 с пунктиром)", callback_data="set_fmt_a4_9in1")
    builder.button(text="⬅️ Назад", callback_data="back_to_settings")
    builder.adjust(1)
    return builder.as_markup()


TARIFFS_MAIN_TEXT = (
    "📦 <b>Пакеты документов (без срока годности)</b>\n\n"
    "• <b>Тестовый</b> — 0 ₸\n"
    "└ 30 документов/день для ознакомления.\n\n"
    "• 🔥 <b>Быстрый Старт</b> — <b>1 690 ₸</b> <i>(старая цена: 2 490 ₸)</i>\n"
    "└ 350 накладных (300 + 50 в подарок). ~4.8 ₸ за документ.\n\n"
    "• <b>Пакет «Бизнес»</b> — <b>4 990 ₸</b>\n"
    "└ 1 200 накладных. ~4.1 ₸ за документ.\n\n"
    "♾ <b>Безлимитные подписки (без ограничений)</b>\n\n"
    "• <b>1 месяц</b> — <b>2 990 ₸</b>\n"
    "└ Полный безлимит на 30 дней + Авто-сортировка курьеров.\n\n"
    "• <b>3 месяца</b> — <b>7 470 ₸</b> <i>(2 490 ₸/мес, скидка 15%)</i>\n"
    "└ Полный безлимит на 90 дней.\n\n"
    "• <b>6 месяцев</b> — <b>12 900 ₸</b> <i>(2 150 ₸/мес, скидка 28%)</i>\n"
    "└ Полный безлимит на 180 дней.\n\n"
    "• 👑 <b>12 месяцев</b> — <b>21 480 ₸</b> <i>(1 790 ₸/мес, скидка 40%)</i>\n"
    "└ Полный безлимит на год + персональный брендинг обложки.\n\n"
    "👇 <b>Выберите тип тарифа для перевода или онлайн-оплаты:</b>"
)

def get_main_tariff_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 Пакеты накладных", callback_data="category_packages"),
                InlineKeyboardButton(text="♾ Безлимитная подписка", callback_data="category_subscriptions")
            ]
        ]
    )

def get_packages_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Быстрый Старт (350 шт) — 1 690 ₸", callback_data="buy_package_start")],
            [InlineKeyboardButton(text="💼 Пакет «Бизнес» (1200 шт) — 4 990 ₸", callback_data="buy_package_biz")],
            [InlineKeyboardButton(text="🎁 Бесплатный тест (30 шт)", callback_data="buy_package_test")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_tariffs_main")]
        ]
    )

def get_subscriptions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗓 1 месяц — 2 990 ₸", callback_data="buy_sub_1m")],
            [InlineKeyboardButton(text="🗓 3 месяца — 7 470 ₸ (-15%)", callback_data="buy_sub_3m")],
            [InlineKeyboardButton(text="🗓 6 месяцев — 12 900 ₸ (-28%)", callback_data="buy_sub_6m")],
            [InlineKeyboardButton(text="👑 12 месяцев — 21 480 ₸ (-40%)", callback_data="buy_sub_12m")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_tariffs_main")]
        ]
    )

# --- ГЕНЕРАЦИЯ СТРАНИЦ И ОБЛОЖЕК В PDF ---
def create_cover_page(batch_number: int, total_orders: int, date_str: str) -> io.BytesIO:
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(212, 340))
    c.setFillColor(HexColor("#7A0000"))
    c.roundRect(10, 260, 192, 65, 12, fill=True, stroke=False)
    c.setFillColor(HexColor("#FFFFFF"))
    c.setFont(FONT_NAME, 18)
    c.drawCentredString(106, 287, "KaspiPrint")
    c.setFillColor(HexColor("#7A0000"))
    c.roundRect(25, 205, 162, 28, 14, fill=True, stroke=False)
    c.setFillColor(HexColor("#FFFFFF"))
    c.setFont(FONT_NAME, 9)
    c.drawCentredString(106, 214, f"ПАРТИЯ №{batch_number} сформирована!")
    c.setFillColor(HexColor("#0A0A0A"))
    c.roundRect(15, 115, 182, 70, 10, fill=True, stroke=False)
    c.setFillColor(HexColor("#FFFFFF"))
    c.setFont(FONT_NAME, 9)
    c.drawString(28, 158, f"Дата/Время: {date_str}")
    c.drawString(28, 132, f"ВСЕГО ЗАКАЗОВ В ПАРТИИ: {total_orders} ШТ.")
    c.setFillColor(HexColor("#444444"))
    c.setFont(FONT_NAME, 6)
    c.drawCentredString(106, 35, "Печатайте файл и собирайте заказы по порядку!")
    c.save()
    packet.seek(0)
    return packet

def create_delivery_group_cover(group_title: str, count: int) -> io.BytesIO:
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(212, 340))
    c.setFillColor(HexColor("#0A0A0A"))
    c.rect(0, 0, 212, 340, fill=True, stroke=False)
    c.setFillColor(HexColor("#FF3B30"))
    c.roundRect(10, 200, 192, 100, 10, fill=True, stroke=False)
    c.setFillColor(HexColor("#FFFFFF"))
    c.setFont(FONT_NAME, 10)
    c.drawCentredString(106, 265, group_title.upper())
    c.setFont(FONT_NAME, 14)
    c.drawCentredString(106, 230, f"ЗАКАЗОВ: {count} ШТ.")
    c.setFont(FONT_NAME, 8)
    c.drawCentredString(106, 120, "Далее идут накладные этой категории")
    c.save()
    packet.seek(0)
    return packet

def create_number_stamp(current_idx: int, total_orders: int, is_landscape: bool = False) -> io.BytesIO:
    packet = io.BytesIO()
    pagesize = (340, 212) if is_landscape else (212, 340)
    c = canvas.Canvas(packet, pagesize=pagesize)
    x_pos = pagesize[0] - 82
    c.setFillColor(HexColor("#FFFFFF"))
    c.rect(x_pos, 5, 75, 15, fill=True, stroke=False)
    c.setFillColor(HexColor("#000000"))
    c.setFont(FONT_NAME, 8)
    c.drawString(x_pos + 5, 10, f"№ {current_idx} из {total_orders}")
    c.save()
    packet.seek(0)
    return packet

# --- АДМИН-ФУНКЦИИ И СТАТИСТИКА ---
@dp.message(F.text == "📊 Статистика за месяц")
async def show_monthly_stats(message: types.Message):
    user_id = message.from_user.id
    stats = get_monthly_statistics(user_id)
    time_saved_str = ""
    if stats["hours"] > 0:
        time_saved_str += f"<b>{stats['hours']} ч.</b> "
    time_saved_str += f"<b>{stats['minutes']} мин.</b>"
    
    await message.answer(
        f"📊 <b>Ваша статистика продаж за текущий месяц:</b>\n\n"
        f"📦 <b>Всего распечатано заказов:</b> <code>{stats['monthly_orders']} шт.</code>\n"
        f"📁 <b>Обработано партий:</b> <code>{stats['batches_count']} партий</code>\n"
        f"⏱ <b>Сэкономлено времени:</b> {time_saved_str}\n\n"
        f"💡 <i>Расчёт сэкономленного времени произведён исходя из 30 секунд на ручную рутину с одним заказом!</i>",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )

@dp.message(Command("stats"))
async def admin_bot_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    conn = sqlite3.connect("db.sqlite3")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(user_id) FROM users")
    total_users = cursor.fetchone()[0] or 0
    today_str = date.today().isoformat()
    cursor.execute("SELECT COUNT(user_id) FROM users WHERE is_unlimited = 1 OR (subscription_expires IS NOT NULL AND subscription_expires >= ?)", (today_str,))
    active_subs = cursor.fetchone()[0] or 0
    cursor.execute("SELECT SUM(orders_count) FROM batch_history WHERE processed_at >= ?", (today_str,))
    orders_today = cursor.fetchone()[0] or 0
    seven_days_ago = (date.today() - timedelta(days=7)).isoformat()
    cursor.execute("SELECT SUM(orders_count) FROM batch_history WHERE processed_at >= ?", (seven_days_ago,))
    orders_7days = cursor.fetchone()[0] or 0
    first_day_month = date.today().replace(day=1).isoformat()
    cursor.execute("SELECT SUM(orders_count) FROM batch_history WHERE processed_at >= ?", (first_day_month,))
    orders_month = cursor.fetchone()[0] or 0
    conn.close()

    await message.answer(
        f"📊 <b>Общая статистика бота @ksp_print:</b>\n\n"
        f"👥 <b>Пользователей всего:</b> <code>{total_users} чел.</code>\n"
        f"👑 <b>Активных подписок:</b> <code>{active_subs}</code>\n\n"
        f"📈 <b>Обработка заказов (накладных):</b>\n"
        f"• За сегодня: <code>{orders_today} шт.</code>\n"
        f"• За 7 дней: <code>{orders_7days} шт.</code>\n"
        f"• За текущий месяц: <code>{orders_month} шт.</code>",
        parse_mode="HTML"
    )

@dp.message(Command("broadcast"))
async def admin_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    text_to_send = message.text.replace("/broadcast", "").strip()
    if not text_to_send:
        await message.answer("📢 <b>Использование рассылки:</b>\n\n<code>/broadcast Ваш текст</code>", parse_mode="HTML")
        return
    conn = sqlite3.connect("db.sqlite3")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    conn.close()
    status_msg = await message.answer(f"🚀 Начинаю рассылку для {len(users)} пользователей...")
    success_count, fail_count = 0, 0
    for u in users:
        try:
            await bot.send_message(u[0], text_to_send, parse_mode="HTML", disable_web_page_preview=True)
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail_count += 1
    await status_msg.edit_text(f"✅ <b>Рассылка завершена!</b>\n\n📥 Доставлено: <code>{success_count}</code>\n❌ Ошибок: <code>{fail_count}</code>", parse_mode="HTML")

async def check_subscription_expirations():
    while True:
        try:
            today = date.today()
            today_str, day_3_str, day_1_str = today.isoformat(), (today + timedelta(days=3)).isoformat(), (today + timedelta(days=1)).isoformat()
            conn = sqlite3.connect("db.sqlite3")
            cursor = conn.cursor()
            renew_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⭐ Продлить подписку", callback_data="category_subscriptions")],
                [InlineKeyboardButton(text="💬 Менеджер", url="https://t.me/baur_bkh")]
            ])
            cursor.execute("SELECT user_id FROM users WHERE is_unlimited = 0 AND subscription_expires = ?", (day_3_str,))
            for (uid,) in cursor.fetchall():
                cursor.execute("SELECT 1 FROM sub_notifications WHERE user_id = ? AND notify_type = '3d' AND sent_date = ?", (uid, today_str))
                if not cursor.fetchone():
                    try:
                        await bot.send_message(uid, f"⏰ <b>Ваша подписка KaspiPrint завершится через 3 дня!</b>\nПродлите подписку заранее.", reply_markup=renew_keyboard, parse_mode="HTML")
                        cursor.execute("INSERT INTO sub_notifications VALUES (?, '3d', ?)", (uid, today_str))
                        conn.commit()
                    except: pass
            cursor.execute("SELECT user_id FROM users WHERE is_unlimited = 0 AND subscription_expires = ?", (day_1_str,))
            for (uid,) in cursor.fetchall():
                cursor.execute("SELECT 1 FROM sub_notifications WHERE user_id = ? AND notify_type = '1d' AND sent_date = ?", (uid, today_str))
                if not cursor.fetchone():
                    try:
                        await bot.send_message(uid, f"⚠️ <b>Подписка истекает завтра!</b>\nЗавтра доступ к функциям будет приостановлен.", reply_markup=renew_keyboard, parse_mode="HTML")
                        cursor.execute("INSERT INTO sub_notifications VALUES (?, '1d', ?)", (uid, today_str))
                        conn.commit()
                    except: pass
            conn.close()
        except: pass
        await asyncio.sleep(86400)

@dp.message(Command("sub"))
async def admin_manage_subscriptions(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("👑 Управление подписками: /sub +30d @username | /sub inf @username | /sub del @username", parse_mode="HTML")
        return
    action, target = args[1].lower(), args[2].replace("@", "")
    conn = sqlite3.connect("db.sqlite3")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, username FROM users WHERE user_id = ?" if target.isdigit() else "SELECT user_id, username FROM users WHERE LOWER(username) = LOWER(?)", (target if not target.isdigit() else int(target),))
    row = cursor.fetchone()
    if not row:
        conn.close()
        await message.answer(f"❌ Пользователь не найден.", parse_mode="HTML")
        return
    target_id = row[0]
    if action == "del":
        cursor.execute("UPDATE users SET subscription_expires = NULL, is_unlimited = 0 WHERE user_id = ?", (target_id,))
        await message.answer("❌ Подписка аннулирована.", parse_mode="HTML")
    elif action == "inf":
        cursor.execute("UPDATE users SET is_unlimited = 1, subscription_expires = NULL WHERE user_id = ?", (target_id,))
        await message.answer("🎉 Выдан ВЕЧНЫЙ БЕЗЛИМИТ!", parse_mode="HTML")
    else:
        days = 30 if action in ["+30d", "+1m"] else 90 if action == "+3m" else 365 if action in ["+1y", "+12m"] else 0
        if days:
            exp = (date.today() + timedelta(days=days)).isoformat()
            cursor.execute("UPDATE users SET subscription_expires = ?, is_unlimited = 0 WHERE user_id = ?", (exp, target_id))
            await message.answer(f"✅ Подписка активирована до {exp}.", parse_mode="HTML")
    conn.commit()
    conn.close()

@dp.message(Command("ksprnt"))
async def admin_manage_limits(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("⚙️ Лимиты: /ksprnt +15 @username | /ksprnt set 500 @username", parse_mode="HTML")
        return
    action, target = args[1], args[2].replace("@", "")
    conn = sqlite3.connect("db.sqlite3")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?" if target.isdigit() else "SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (target if not target.isdigit() else int(target),))
    row = cursor.fetchone()
    if row:
        if action.startswith("+"):
            cursor.execute("UPDATE users SET bonus_limit = bonus_limit + ? WHERE user_id = ?", (int(action[1:]), row[0]))
            await message.answer("✅ Бонус добавлен.")
        elif action == "set" and len(args) >= 4:
            cursor.execute("UPDATE users SET daily_limit = ? WHERE user_id = ?", (int(args[2]), row[0]))
            await message.answer("✅ Лимит изменен.")
        conn.commit()
    conn.close()


# --- ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЯ ---
@dp.message(CommandStart())
async def start_handler(message: types.Message):
    referrer_id = int(message.text.split()[1].replace("ref", "")) if len(message.text.split()) > 1 and message.text.split()[1].startswith("ref") else None
    user_info = get_or_create_user(message.from_user.id, message.from_user.username, referrer_id)
    if user_info["is_new_user"] and referrer_id and referrer_id != message.from_user.id:
        try: await bot.send_message(referrer_id, "🎉 <b>По вашей ссылке зарегистрировался селлер!</b>\nВам зачислено +10 обработок!", parse_mode="HTML")
        except: pass
    await message.answer("🖨 <b>KaspiPrint — Сервис склейки накладных Kaspi</b>\nОтправьте файлы для сортировки и печати.", reply_markup=get_main_keyboard(), parse_mode="HTML")

@dp.message(F.text == "🚀 Старт бота")
async def start_work_button(message: types.Message):
    await message.answer("📤 <b>Жду ваши файлы!</b>\nОтправьте ZIP-архив или несколько PDF.", reply_markup=get_main_keyboard(), parse_mode="HTML")

@dp.message(F.text == "🎁 Приведи друга")
async def referral_program_handler(message: types.Message):
    bot_info = await bot.get_me()
    user_data = get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(f"🤝 <b>«Приведи друга»</b>\nПолучайте +10 обработок за каждого!\n🔗 Ваша ссылка: <code>https://t.me/{bot_info.username}?start=ref{message.from_user.id}</code>\n📊 Приведено: {user_data['referrals_count']}", reply_markup=get_main_keyboard(), parse_mode="HTML")

@dp.message(F.text == "⚙️ Настройки")
async def show_settings(message: types.Message):
    user_data = get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer("⚙️ <b>Настройки печати:</b>", reply_markup=get_settings_inline_keyboard(user_data["duplicate_mode"], user_data["paper_format"]), parse_mode="HTML")

@dp.callback_query(F.data == "change_paper_format")
async def process_change_paper_format(callback: CallbackQuery):
    await callback.message.edit_text("🖨 <b>Выберите используемый формат бумаги / принтера:</b>", reply_markup=get_paper_format_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("set_fmt_"))
async def process_set_format(callback: CallbackQuery):
    fmt = callback.data.replace("set_fmt_", "")
    set_user_paper_format(callback.from_user.id, fmt)
    user_data = get_or_create_user(callback.from_user.id)
    await callback.message.edit_text("✅ <b>Формат печати успешно обновлен!</b>", reply_markup=get_settings_inline_keyboard(user_data["duplicate_mode"], fmt), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "back_to_settings")
async def back_to_settings_callback(callback: CallbackQuery):
    user_data = get_or_create_user(callback.from_user.id)
    await callback.message.edit_text("⚙️ <b>Настройки печати:</b>", reply_markup=get_settings_inline_keyboard(user_data["duplicate_mode"], user_data["paper_format"]), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "toggle_duplicate")
async def toggle_duplicate_callback(callback: CallbackQuery):
    new_state = toggle_user_duplicate_mode(callback.from_user.id)
    user_data = get_or_create_user(callback.from_user.id)
    await callback.message.edit_reply_markup(reply_markup=get_settings_inline_keyboard(new_state, user_data["paper_format"]))
    await callback.answer(f"Дублирование этикеток {'включено' if new_state else 'выключено'}!")

@dp.message(F.text == "📖 Инструкция")
async def show_instruction(message: types.Message):
    await message.answer("📖 Зайдите в Kaspi Pay ➔ Заказы ➔ Выгрузите накладные ➔ Отправьте боту.", reply_markup=get_main_keyboard())

@dp.message(F.text == "📢 Наш канал / Отзывы")
async def show_channel_info(message: types.Message):
    await message.answer("📢 Наш канал: Первыми узнавайте об обновлениях", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📢 Перейти в канал", url=CHANNEL_LINK)]]))

@dp.message(F.text.in_(["⭐ Тарифы и подписка", "💳 Тарифы", "/tariffs", "/pay"]))
async def show_tariffs(message: types.Message):
    user_data = get_or_create_user(message.from_user.id, message.from_user.username)
    sub_info = "👑 <b>Ваш статус:</b> <code>БЕЗЛИМИТНАЯ ПОДПИСКА (ВЕЧНАЯ)</code>\n\n" if user_data["is_unlimited"] else f"🌟 <b>Ваш статус:</b> <code>ПОДПИСКА АКТИВНА до {user_data['sub_expires']}</code>\n\n" if user_data["has_active_sub"] else f"📊 <b>Ваш текущий лимит на сегодня:</b> <code>{user_data['used_today']} / {user_data['max_allowed']} шт.</code>\n\n"
    await message.answer(text=sub_info + TARIFFS_MAIN_TEXT, parse_mode="HTML", reply_markup=get_main_tariff_keyboard())

@dp.callback_query(F.data == "category_packages")
async def process_packages_category(callback: CallbackQuery):
    await callback.message.edit_text(text="📦 <b>Выберите подходящий пакет документов:</b>", parse_mode="HTML", reply_markup=get_packages_keyboard())

@dp.callback_query(F.data == "category_subscriptions")
async def process_subscriptions_category(callback: CallbackQuery):
    await callback.message.edit_text(text="♾ <b>Выберите период безлимитной подписки:</b>", parse_mode="HTML", reply_markup=get_subscriptions_keyboard())

@dp.callback_query(F.data == "back_to_tariffs_main")
async def back_to_main_tariffs(callback: CallbackQuery):
    user_data = get_or_create_user(callback.from_user.id, callback.from_user.username)
    sub_info = "👑 <b>Ваш статус:</b> <code>БЕЗЛИМИТНАЯ ПОДПИСКА (ВЕЧНАЯ)</code>\n\n" if user_data["is_unlimited"] else f"🌟 <b>Ваш статус:</b> <code>ПОДПИСКА АКТИВНА до {user_data['sub_expires']}</code>\n\n" if user_data["has_active_sub"] else f"📊 <b>Ваш текущий лимит на сегодня:</b> <code>{user_data['used_today']} / {user_data['max_allowed']} шт.</code>\n\n"
    await callback.message.edit_text(text=sub_info + TARIFFS_MAIN_TEXT, parse_mode="HTML", reply_markup=get_main_tariff_keyboard())

@dp.callback_query(F.data.startswith("buy_"))
async def process_tariff_selection(callback: CallbackQuery):
    try: await callback.message.delete()
    except: pass
    await callback.message.answer(f"💳 Для активации тарифа свяжитесь с менеджером:\n👨‍💻 @baur_bkh\nВаш ID: <code>{callback.from_user.id}</code>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("show_picklist_"))
async def process_show_picklist(callback: CallbackQuery):
    picklist_data = user_pick_lists.get(f"{callback.from_user.id}_{callback.data.split('_')[-1]}")
    if not picklist_data: return await callback.answer("⚠️ Данные Листа сборки устарели.", show_alert=True)
    text = f"📦 <b>Лист сборки заказов:</b>\n\n"
    for item, qty in picklist_data.items(): text += f"• {item} — <b>{qty} шт.</b>\n"
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

@dp.message(F.text == "🖨 Принтер (XP-365B)")
async def show_printer_settings(message: types.Message):
    await message.answer("🖨 <b>Настройка печати:</b>\nРазмер 75×120 мм, Масштаб: Фактический размер.", reply_markup=get_main_keyboard(), parse_mode="HTML")

@dp.message(F.text == "💬 Поддержка")
async def show_support(message: types.Message):
    await message.answer("💬 Служба поддержки:\n👨‍💻 @baur_bkh\n🕒 09:00 - 21:00", reply_markup=get_main_keyboard())

@dp.message(F.photo)
async def handle_photo(message: types.Message):
    await message.answer("⚠️ Бот работает только с PDF и ZIP файлами. Фотографии не поддерживаются.")

# --- ОСНОВНОЙ ПРОЦЕСС ОБРАБОТКИ ---
async def process_user_files(user_id: int, message: types.Message):
    await asyncio.sleep(2)
    files_list = user_files_buffer.pop(user_id, [])
    if not files_list: return

    user_data = get_or_create_user(user_id, message.from_user.username)
    duplicate_enabled = user_data["duplicate_mode"]
    paper_format = user_data["paper_format"]
    valid_files = [doc for doc in files_list if (os.path.splitext(doc.file_name)[1].lower() if doc.file_name else "") in [".pdf", ".zip"]]
    
    if not valid_files:
        return await message.answer("❌ Ошибка формата! Отправляйте .pdf или .zip", parse_mode="HTML")

    user_dir = f"./temp_{user_id}"
    os.makedirs(user_dir, exist_ok=True)
    status_msg = await message.answer(f"📥 Обработка {len(valid_files)} файл(ов)...")
    pdf_files = []

    try:
        current_batch_number = user_data["user_batch_counter"]
        for idx, doc in enumerate(valid_files):
            file_info = await bot.get_file(doc.file_id)
            ext = os.path.splitext(doc.file_name)[1].lower()
            downloaded_path = os.path.join(user_dir, f"file_{idx}{ext}")
            await bot.download_file(file_info.file_path, destination=downloaded_path)

            if ext == ".zip":
                extract_dir = os.path.join(user_dir, f"ext_{idx}")
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(downloaded_path, "r") as zip_ref: zip_ref.extractall(extract_dir)
                for root, _, files in os.walk(extract_dir):
                    pdf_files.extend([os.path.join(root, f) for f in files if f.lower().endswith(".pdf")])
            elif ext == ".pdf":
                pdf_files.append(downloaded_path)

        if not pdf_files:
            shutil.rmtree(user_dir, ignore_errors=True)
            return await status_msg.edit_text("❌ В файлах нет PDF.")

        grouped_pages, picklist_data = parse_and_sort_pdf_pages(pdf_files)
        total_orders = sum(len(pages) for pages in grouped_pages.values())
        
        if not user_data["has_active_sub"] and (user_data["used_today"] + total_orders > user_data["max_allowed"]):
            shutil.rmtree(user_dir, ignore_errors=True)
            return await status_msg.edit_text("🛑 Превышен лимит! Оформите подписку.")

        now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        await status_msg.edit_text(f"⚙️ Подготовка: ПАРТИЯ №{current_batch_number} ({total_orders} заказов)...")
        if picklist_data: user_pick_lists[f"{user_id}_{current_batch_number}"] = picklist_data

        writer = PdfWriter()
        cover_stream = create_cover_page(current_batch_number, total_orders, now_str)
        cover_reader = PdfReader(cover_stream)
        
        current_global_idx = 1
        current_page_counter = 2
        delivery_summary_text = ""
        processed_pages_stream = []

        # Форматы А4 обрабатываем через буфер
        is_a4_grid = paper_format in ["a4_4in1", "a4_8in1", "a4_9in1"]
        
        if is_a4_grid:
            processed_pages_stream.append(cover_reader.pages[0])
        else:
            writer.add_page(cover_reader.pages[0])

        for group_title, pages in grouped_pages.items():
            if not pages: continue
            
            group_cover_stream = create_delivery_group_cover(group_title, len(pages))
            group_cover_reader = PdfReader(group_cover_stream)
            
            if is_a4_grid:
                processed_pages_stream.append(group_cover_reader.pages[0])
            else:
                writer.add_page(group_cover_reader.pages[0])
            
            start_page = current_page_counter + 1
            current_page_counter += 1

            for page in pages:
                if paper_format == "a4_crop_thermal":
                    page = crop_a4_top_left_to_thermal(page)

                width, height = float(page.mediabox.width), float(page.mediabox.height)
                if page.get('/Rotate', 0) in (90, 270): width, height = height, width
                is_landscape = width > height

                stamp_stream = create_number_stamp(current_global_idx, total_orders, is_landscape=is_landscape)
                page.merge_page(PdfReader(stamp_stream).pages[0])

                if is_landscape: page.rotate(90)

                if is_a4_grid:
                    processed_pages_stream.append(page)
                    if duplicate_enabled: processed_pages_stream.append(page)
                else:
                    writer.add_page(page)
                    current_page_counter += 1
                    if duplicate_enabled:
                        writer.add_page(page)
                        current_page_counter += 1

                current_global_idx += 1

            end_page = current_page_counter - 1
            delivery_summary_text += f"• {group_title}: <b>{len(pages)} шт.</b>\n"

        if is_a4_grid:
            mode_type = paper_format.replace("a4_", "")
            n_up_writer = merge_pages_n_up(processed_pages_stream, mode=mode_type)
            for page_n in n_up_writer.pages: writer.add_page(page_n)

        output_pdf_path = os.path.join(user_dir, f"ПАРТИЯ_№{current_batch_number}.pdf")
        with open(output_pdf_path, "wb") as f_out: writer.write(f_out)

        update_user_usage_and_batch(user_id, total_orders)
        
        fmt_notes = {
            "thermal": "🏷 Термопринтер",
            "a4_crop_thermal": "✂️ Обрезка А4",
            "a4_4in1": "📄 4 на 1 лист (А4)",
            "a4_8in1": "📄 8 на 1 лист (А4)",
            "a4_9in1": "📄 9 на 1 лист (А4)"
        }

        summary_msg = (
            f"✅ <b>ПАРТИЯ №{current_batch_number} сформирована!</b>\n\n"
            f"🚚 <b>Службы доставки:</b>\n{delivery_summary_text}\n"
            f"📦 <b>Заказов:</b> <code>{total_orders} шт.</code>\n"
            f"🖨 <b>Формат бумаги:</b> {fmt_notes.get(paper_format, '')}\n"
            f"💡 <i>Нажмите «Показать Лист сборки» ниже.</i>"
        )

        await status_msg.delete()
        await message.answer_document(
            document=types.FSInputFile(output_pdf_path),
            caption=summary_msg,
            reply_markup=get_batch_result_keyboard(current_batch_number),
            parse_mode="HTML"
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка при обработке: {e}")
    finally:
        shutil.rmtree(user_dir, ignore_errors=True)
        if user_id in user_tasks: del user_tasks[user_id]


@dp.message(F.document)
async def handle_document(message: types.Message):
    user_id = message.from_user.id
    user_files_buffer[user_id].append(message.document)
    if user_id in user_tasks: user_tasks[user_id].cancel()
    user_tasks[user_id] = asyncio.create_task(process_user_files(user_id, message))

async def main():
    print("🚀 Бот @ksp_print с улучшенной раскладкой на А4 запущен!")
    asyncio.create_task(check_subscription_expirations())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())