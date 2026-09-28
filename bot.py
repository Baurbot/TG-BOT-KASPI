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

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

BOT_TOKEN = "8563063962:AAEeI8rgLBv8ZWjGQddqjW1QVCy78sF5cpc"
ADMIN_ID = 1260202941  # Ваш Telegram ID
CHANNEL_LINK = "https://t.me/ksp_print"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Буферы для сбора файлов и задач
user_files_buffer = defaultdict(list)
user_tasks = {}
user_photo_tasks = {}
user_pick_lists = {}  # Хранение листов сборки и данных по доставке

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
            referrer_id INTEGER DEFAULT NULL,
            referrals_count INTEGER DEFAULT 0,
            subscription_expires TEXT DEFAULT NULL,
            is_unlimited INTEGER DEFAULT 0,
            total_orders_count INTEGER DEFAULT 0
        )
    """)
    
    # Таблица истории обработанных партий для помесячной статистики
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS batch_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            orders_count INTEGER,
            processed_at TEXT
        )
    """)
    
    cursor.execute("PRAGMA table_info(users)")
    columns = [column[1] for column in cursor.fetchall()]
    
    if "user_batch_counter" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN user_batch_counter INTEGER DEFAULT 1")
    if "duplicate_mode" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN duplicate_mode INTEGER DEFAULT 0")
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
                  user_batch_counter, duplicate_mode, referrals_count, 
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
                user_batch_counter, duplicate_mode, referrer_id, referrals_count, subscription_expires, is_unlimited, total_orders_count) 
               VALUES (?, ?, 35, 0, 0, ?, 1, 0, ?, 0, NULL, 0, 0)""",
            (user_id, username, today_str, valid_referrer)
        )
        conn.commit()
        
        daily_limit, bonus_limit, used_today, batch_cnt, dup_mode, refs_count = 35, 0, 0, 1, 0, 0
        sub_expires, is_unlimited, total_orders = None, 0, 0
        
        if valid_referrer:
            cursor.execute(
                "UPDATE users SET bonus_limit = bonus_limit + 10, referrals_count = referrals_count + 1 WHERE user_id = ?",
                (valid_referrer,)
            )
            conn.commit()
    else:
        daily_limit, bonus_limit, used_today, last_date, batch_cnt, dup_mode, refs_count, sub_expires, is_unlimited, total_orders = row
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
    
    # Обновление счетчиков пользователя
    cursor.execute("""
        UPDATE users 
        SET used_today = used_today + ?, 
            user_batch_counter = user_batch_counter + 1,
            total_orders_count = total_orders_count + ?
        WHERE user_id = ?
    """, (added_count, added_count, user_id))
    
    # Запись в историю для точной месячной статистики
    cursor.execute("""
        INSERT INTO batch_history (user_id, orders_count, processed_at)
        VALUES (?, ?, ?)
    """, (user_id, added_count, now_str))
    
    conn.commit()
    conn.close()


def get_monthly_statistics(user_id: int):
    """Возвращает статистику за текущий календарный месяц."""
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
    
    # Расчет сэкономленного времени (~30 секунд на заказ)
    saved_minutes_total = monthly_orders * 0.5
    hours = int(saved_minutes_total // 60)
    minutes = int(saved_minutes_total % 60)
    
    return {
        "monthly_orders": monthly_orders,
        "batches_count": batches_count,
        "hours": hours,
        "minutes": minutes
    }


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


# --- ФУНКЦИЯ ОПРЕДЕЛЕНИЯ ТИПА ДОСТАВКИ И ПАРСИНГА ---
def detect_delivery_type(text: str) -> str:
    """Определяет тип доставки по тексту накладной Kaspi."""
    text_lower = text.lower()
    if "express" in text_lower or "яндекс" in text_lower or "достависта" in text_lower:
        return "⚡️ Kaspi Express / Яндекс"
    elif "самовывоз" in text_lower:
        return "🏬 Самовывоз"
    else:
        return "📦 Kaspi Доставка (ПВЗ/Курьер)"


def parse_and_sort_pdf_pages(pdf_files: list):
    """
    Разбирает PDF-файлы, группирует их страницы по типам доставки 
    и формирует единый Лист сборки.
    """
    grouped_pages = defaultdict(list)
    items_count = defaultdict(int)
    
    for pdf_path in pdf_files:
        try:
            reader = PdfReader(pdf_path)
            for page in reader.pages:
                text = page.extract_text() or ""
                delivery_type = detect_delivery_type(text)
                grouped_pages[delivery_type].append(page)
                
                # Поиск наименований товаров и количества в накладных
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


def get_settings_inline_keyboard(duplicate_enabled: bool):
    builder = InlineKeyboardBuilder()
    status_text = "🟢 ВКЛ" if duplicate_enabled else "🔴 ВЫКЛ"
    builder.button(
        text=f"📄 Дублировать этикетки: [{status_text}]", 
        callback_data="toggle_duplicate"
    )
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
    
    DARK_RED = HexColor("#7A0000")
    BLACK_CARD = HexColor("#0A0A0A")
    WHITE = HexColor("#FFFFFF")
    TEXT_MUTED = HexColor("#444444")
    
    c.setFillColor(DARK_RED)
    c.roundRect(10, 260, 192, 65, 12, fill=True, stroke=False)
    
    c.setFillColor(WHITE)
    c.setFont(FONT_NAME, 18)
    c.drawCentredString(106, 287, "KaspiPrint")
    
    c.setFillColor(DARK_RED)
    c.roundRect(25, 205, 162, 28, 14, fill=True, stroke=False)
    
    c.setFillColor(WHITE)
    c.setFont(FONT_NAME, 9)
    c.drawCentredString(106, 214, f"ПАРТИЯ №{batch_number} сформирована!")
    
    c.setFillColor(BLACK_CARD)
    c.roundRect(15, 115, 182, 70, 10, fill=True, stroke=False)
    
    c.setFillColor(WHITE)
    c.setFont(FONT_NAME, 9)
    c.drawString(28, 158, f"Дата/Время: {date_str}")
    c.drawString(28, 132, f"ВСЕГО ЗАКАЗОВ В ПАРТИИ: {total_orders} ШТ.")
    
    c.setFillColor(TEXT_MUTED)
    c.setFont(FONT_NAME, 6)
    c.drawCentredString(106, 35, "Печатайте файл и собирайте заказы по порядку!")
    
    c.save()
    packet.seek(0)
    return packet


def create_delivery_group_cover(group_title: str, count: int) -> io.BytesIO:
    """Генерация разделительной титульной страницы для службы доставки."""
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


# --- ОБРАБОТЧИК СТАТИСТИКИ ---
@dp.message(F.text == "📊 Статистика за месяц")
async def show_monthly_stats(message: types.Message):
    user_id = message.from_user.id
    stats = get_monthly_statistics(user_id)
    
    time_saved_str = ""
    if stats["hours"] > 0:
        time_saved_str += f"<b>{stats['hours']} ч.</b> "
    time_saved_str += f"<b>{stats['minutes']} мин.</b>"
    
    month_name = datetime.now().strftime("%B")
    
    await message.answer(
        f"📊 <b>Ваша статистика продаж за текущий месяц:</b>\n\n"
        f"📦 <b>Всего распечатано заказов:</b> <code>{stats['monthly_orders']} шт.</code>\n"
        f"📁 <b>Обработано партий:</b> <code>{stats['batches_count']} партий</code>\n"
        f"⏱ <b>Сэкономлено времени:</b> {time_saved_str}\n\n"
        f"💡 <i>Расчёт сэкономленного времени произведён исходя из 30 секунд на ручную рутину с одним заказом!</i>",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )


# --- АДМИН-КОМАНДЫ ---
@dp.message(Command("sub"))
async def admin_manage_subscriptions(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    args = message.text.split()
    if len(args) < 3:
        await message.answer(
            "👑 <b>Админ-панель управления подписками:</b>\n\n"
            "• <code>/sub +30d @username</code> — выдать подписку на 30 дней\n"
            "• <code>/sub +3m @username</code> — выдать подписку на 3 месяца\n"
            "• <code>/sub +1y @username</code> — выдать подписку на 1 год\n"
            "• <code>/sub inf @username</code> — выдать <b>вечный безлимит</b>\n"
            "• <code>/sub del @username</code> — ❌ <b>удалить подписку</b>\n\n"
            "<i>Вместо @username можно писать ID Telegram</i>",
            parse_mode="HTML"
        )
        return

    action = args[1].lower()
    target = args[2].replace("@", "")

    conn = sqlite3.connect("db.sqlite3")
    cursor = conn.cursor()

    if target.isdigit():
        cursor.execute("SELECT user_id, username FROM users WHERE user_id = ?", (int(target),))
    else:
        cursor.execute("SELECT user_id, username FROM users WHERE LOWER(username) = LOWER(?)", (target,))

    row = cursor.fetchone()
    if not row:
        conn.close()
        await message.answer(f"❌ Пользователь <code>{target}</code> не найден в базе данных.", parse_mode="HTML")
        return

    target_id, target_username = row
    user_label = f"@{target_username}" if target_username else f"ID: {target_id}"

    if action == "del":
        cursor.execute(
            "UPDATE users SET subscription_expires = NULL, is_unlimited = 0 WHERE user_id = ?", 
            (target_id,)
        )
        conn.commit()
        conn.close()
        await message.answer(f"❌ Подписка пользователя {user_label} <b>аннулирована</b>.", parse_mode="HTML")
        try:
            await bot.send_message(target_id, "ℹ️ Ваша безлимитная подписка была завершена администратором.")
        except Exception:
            pass
        return

    days_to_add = 0
    is_inf = False

    if action in ["+30d", "+1m"]:
        days_to_add = 30
    elif action == "+3m":
        days_to_add = 90
    elif action in ["+1y", "+12m"]:
        days_to_add = 365
    elif action == "inf":
        is_inf = True
    else:
        conn.close()
        await message.answer("❌ Неизвестная команда длительности.")
        return

    if is_inf:
        cursor.execute("UPDATE users SET is_unlimited = 1, subscription_expires = NULL WHERE user_id = ?", (target_id,))
        conn.commit()
        conn.close()
        await message.answer(f"🎉 Пользователю {user_label} выдан <b>ВЕЧНЫЙ БЕЗЛИМИТ</b>!", parse_mode="HTML")
        try:
            await bot.send_message(target_id, "👑 Вам активирован <b>ВЕЧНЫЙ БЕЗЛИМИТ</b>! Наслаждайтесь свободной печатью!", parse_mode="HTML")
        except Exception:
            pass
    else:
        new_exp_date = date.today() + timedelta(days=days_to_add)
        exp_str = new_exp_date.isoformat()
        cursor.execute("UPDATE users SET subscription_expires = ?, is_unlimited = 0 WHERE user_id = ?", (exp_str, target_id))
        conn.commit()
        conn.close()
        await message.answer(f"✅ Подписка для {user_label} успешно активирована до <b>{exp_str}</b> (+{days_to_add} дней).", parse_mode="HTML")
        try:
            await bot.send_message(target_id, f"🎉 Вам активирована подписка до <b>{exp_str}</b>!", parse_mode="HTML")
        except Exception:
            pass


@dp.message(Command("ksprnt"))
async def admin_manage_limits(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    args = message.text.split()
    if len(args) < 3:
        await message.answer(
            "⚙️ <b>Использование команды администратора:</b>\n\n"
            "• <code>/ksprnt +15 @username</code> — добавить разово +15 лимитов на сегодня\n"
            "• <code>/ksprnt set 500 @username</code> — установить ежедневный лимит в 500 штук\n",
            parse_mode="HTML"
        )
        return

    action = args[1]
    target = args[2].replace("@", "")

    conn = sqlite3.connect("db.sqlite3")
    cursor = conn.cursor()

    if target.isdigit():
        cursor.execute("SELECT user_id, username FROM users WHERE user_id = ?", (int(target),))
    else:
        cursor.execute("SELECT user_id, username FROM users WHERE LOWER(username) = LOWER(?)", (target,))

    row = cursor.fetchone()
    if not row:
        conn.close()
        await message.answer(f"❌ Пользователь <code>{target}</code> не найден в базе данных бота.", parse_mode="HTML")
        return

    target_id, target_username = row

    if action.startswith("+"):
        try:
            add_val = int(action.replace("+", ""))
            cursor.execute("UPDATE users SET bonus_limit = bonus_limit + ? WHERE user_id = ?", (add_val, target_id))
            conn.commit()
            await message.answer(f"✅ Добавлено <b>+{add_val}</b> доп. лимитов пользователю @{target_username or target_id}.", parse_mode="HTML")
        except ValueError:
            await message.answer("❌ Неверное число бонуса.")

    elif action == "set" and len(args) >= 4:
        try:
            new_limit = int(args[2])
            target_user = args[3].replace("@", "")
            
            if target_user.isdigit():
                cursor.execute("UPDATE users SET daily_limit = ? WHERE user_id = ?", (new_limit, int(target_user)))
            else:
                cursor.execute("UPDATE users SET daily_limit = ? WHERE LOWER(username) = LOWER(?)", (new_limit, target_user))
            
            conn.commit()
            await message.answer(f"✅ Новый ежедневный лимит для @{target_user}: <b>{new_limit} шт/день</b>.", parse_mode="HTML")
        except ValueError:
            await message.answer("❌ Неверно указан лимит.")

    conn.close()


@dp.message(CommandStart())
async def start_handler(message: types.Message):
    referrer_id = None
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref"):
        try:
            referrer_id = int(args[1].replace("ref", ""))
        except ValueError:
            referrer_id = None

    user_info = get_or_create_user(message.from_user.id, message.from_user.username, referrer_id)

    if user_info["is_new_user"] and referrer_id and referrer_id != message.from_user.id:
        try:
            await bot.send_message(
                chat_id=referrer_id,
                text="🎉 <b>По вашей реферальной ссылке зарегистрировался новый селлер!</b>\n\n"
                     "🎁 Вам зачислено <b>+10 дополнительных обработок</b>!",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await message.answer(
        "🖨 <b>KaspiPrint — Сервис склейки накладных Kaspi</b>\n\n"
        "Я помогу объединить сотни PDF-накладных или ZIP-архивов в <b>один файл</b> "
        "для быстрой печати на термопринтере (Xprinter, Zebra и др.), сгруппирую их по службам доставки и сформирую <b>Лист сборки</b>.\n\n"
        "Выберите нужный раздел в меню ниже 👇",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )


@dp.message(F.text == "🚀 Старт бота")
async def start_work_button(message: types.Message):
    await message.answer(
        "📤 <b>Жду ваши файлы!</b>\n\n"
        "Отправьте сюда ZIP-архив из Kaspi Pay или сразу несколько PDF-файлов с накладными.",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )


@dp.message(F.text == "🎁 Приведи друга")
async def referral_program_handler(message: types.Message):
    bot_info = await bot.get_me()
    user_id = message.from_user.id
    user_data = get_or_create_user(user_id, message.from_user.username)
    
    ref_link = f"https://t.me/{bot_info.username}?start=ref{user_id}"
    refs_count = user_data["referrals_count"]
    
    await message.answer(
        f"🤝 <b>Партнёрская программа «Приведи друга»</b>\n\n"
        f"Делитесь своей персональной ссылкой с коллегами-селлерами Kaspi! "
        f"За каждого подключённого продавца вы получаете <b>+10 бесплатных обработок</b>.\n\n"
        f"🔗 <b>Ваша реферальная ссылка:</b>\n<code>{ref_link}</code>\n\n"
        f"📊 <b>Приведено селлеров:</b> <code>{refs_count} чел.</code>",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )


@dp.message(F.text == "⚙️ Настройки")
async def show_settings(message: types.Message):
    user_data = get_or_create_user(message.from_user.id, message.from_user.username)
    dup_enabled = user_data["duplicate_mode"]
    
    await message.answer(
        "⚙️ <b>Настройки печати:</b>\n\n"
        "📌 <b>Дублирование этикеток:</b>\n"
        "Если включено, бот сделает 2 копии каждой накладной подряд.\n\n"
        "💡 <i>Дубликаты копируются бесплатно и <b>НЕ списывают</b> ваши дневные лимиты!</i>",
        reply_markup=get_settings_inline_keyboard(dup_enabled),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "toggle_duplicate")
async def toggle_duplicate_callback(callback: CallbackQuery):
    new_state = toggle_user_duplicate_mode(callback.from_user.id)
    await callback.message.edit_reply_markup(
        reply_markup=get_settings_inline_keyboard(new_state)
    )
    status_text = "включено" if new_state else "выключено"
    await callback.answer(f"Дублирование этикеток {status_text}!")


@dp.message(F.text == "📖 Инструкция")
async def show_instruction(message: types.Message):
    await message.answer(
        "📖 <b>Инструкция по работе:</b>\n\n"
        "1️⃣ <b>Зайдите в Kaspi Pay</b> ➔ Раздел «Заказы» ➔ Выгрузите накладные (ZIP или отдельные PDF).\n"
        "2️⃣ <b>Отправьте файлы в этот чат</b>.\n"
        "3️⃣ <b>Бот объединит их</b>, сгруппирует по курьерам, пронумерует страницы и сформирует обложки.\n"
        "4️⃣ <b>Получите готовую партию и Лист сборки</b> по кнопке под готовым файлом!",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )


@dp.message(F.text == "📢 Наш канал / Отзывы")
async def show_channel_info(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Перейти в канал", url=CHANNEL_LINK)]
        ]
    )
    await message.answer(
        "📢 <b>Наш Telegram-канал и Отзывы</b>\n\n"
        "Подписывайтесь на наш канал, чтобы:\n"
        "• Первыми узнавать о новых функциях и обновлениях бота\n"
        "• Читать реальные отзывы селлеров Kaspi\n"
        "• Получать полезные инструкции по настройке принтеров и оптимизации работы\n\n"
        "Нажмите на кнопку ниже, чтобы перейти 👇",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@dp.message(F.text.in_(["⭐ Тарифы и подписка", "💳 Тарифы", "/tariffs", "/pay"]))
async def show_tariffs(message: types.Message):
    user_data = get_or_create_user(message.from_user.id, message.from_user.username)
    
    if user_data["is_unlimited"]:
        sub_info = "👑 <b>Ваш статус:</b> <code>БЕЗЛИМИТНАЯ ПОДПИСКА (ВЕЧНАЯ)</code>\n\n"
    elif user_data["has_active_sub"]:
        sub_info = f"🌟 <b>Ваш статус:</b> <code>ПОДПИСКА АКТИВНА до {user_data['sub_expires']}</code>\n\n"
    else:
        sub_info = f"📊 <b>Ваш текущий лимит на сегодня:</b> <code>{user_data['used_today']} / {user_data['max_allowed']} шт.</code>\n\n"
    
    await message.answer(
        text=sub_info + TARIFFS_MAIN_TEXT,
        parse_mode="HTML",
        reply_markup=get_main_tariff_keyboard()
    )


@dp.callback_query(F.data == "category_packages")
async def process_packages_category(callback: CallbackQuery):
    await callback.message.edit_text(
        text="📦 <b>Выберите подходящий пакет документов:</b>\n\nБаланс расходуется по мере работы и не сгорает со временем.",
        parse_mode="HTML",
        reply_markup=get_packages_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "category_subscriptions")
async def process_subscriptions_category(callback: CallbackQuery):
    await callback.message.edit_text(
        text="♾ <b>Выберите период безлимитной подписки:</b>\n\nСоздавайте неограниченное количество накладных.",
        parse_mode="HTML",
        reply_markup=get_subscriptions_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "back_to_tariffs_main")
async def back_to_main_tariffs(callback: CallbackQuery):
    user_data = get_or_create_user(callback.from_user.id, callback.from_user.username)
    
    if user_data["is_unlimited"]:
        sub_info = "👑 <b>Ваш статус:</b> <code>БЕЗЛИМИТНАЯ ПОДПИСКА (ВЕЧНАЯ)</code>\n\n"
    elif user_data["has_active_sub"]:
        sub_info = f"🌟 <b>Ваш статус:</b> <code>ПОДПИСКА АКТИВНА до {user_data['sub_expires']}</code>\n\n"
    else:
        sub_info = f"📊 <b>Ваш текущий лимит на сегодня:</b> <code>{user_data['used_today']} / {user_data['max_allowed']} шт.</code>\n\n"
        
    await callback.message.edit_text(
        text=sub_info + TARIFFS_MAIN_TEXT,
        parse_mode="HTML",
        reply_markup=get_main_tariff_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("buy_"))
async def process_tariff_selection(callback: CallbackQuery):
    tariff_names = {
        "buy_package_start": "🔥 Быстрый Старт (350 шт) — 1 690 ₸",
        "buy_package_biz": "💼 Пакет «Бизнес» (1200 шт) — 4 990 ₸",
        "buy_package_test": "🎁 Бесплатный тест (30 шт)",
        "buy_sub_1m": "🗓 Подписка 1 месяц — 2 990 ₸",
        "buy_sub_3m": "🗓 Подписка 3 месяца — 7 470 ₸",
        "buy_sub_6m": "🗓 Подписка 6 месяцев — 12 900 ₸",
        "buy_sub_12m": "👑 Подписка 12 месяцев — 21 480 ₸"
    }
    
    selected = tariff_names.get(callback.data, "Выбранный тариф")
    
    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.message.answer(
        f"💳 <b>Оплата тарифа:</b> {selected}\n\n"
        f"Для активации тарифа или получения счета на оплату свяжитесь с менеджером:\n"
        f"👨‍💻 <b>Администратор:</b> @baur_bkh\n\n"
        f"Укажите ваш ID при обращении: <code>{callback.from_user.id}</code>",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("show_picklist_"))
async def process_show_picklist(callback: CallbackQuery):
    batch_num = callback.data.split("_")[-1]
    picklist_data = user_pick_lists.get(f"{callback.from_user.id}_{batch_num}")

    if not picklist_data:
        await callback.answer("⚠️ Данные Листа сборки устарели или не найдены.", show_alert=True)
        return

    text = f"📦 <b>Лист сборки заказов (Партия №{batch_num}):</b>\n\n"
    for item, qty in picklist_data.items():
        text += f"• {item} — <b>{qty} шт.</b>\n"

    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@dp.message(F.text == "🖨 Принтер (XP-365B)")
async def show_printer_settings(message: types.Message):
    await message.answer(
        "🖨 <b>Настройка печати для Xprinter XP-365B:</b>\n\n"
        "📏 <b>Размер бумаги в драйвере:</b>\n"
        "• Стандарт Kaspi: <b>75 × 120 мм</b> или <b>100 × 150 мм</b>\n\n"
        "⚙️ <b>Рекомендуемые параметры в Acrobat / PDF Viewer:</b>\n"
        "• Масштаб: <b>«Фактический размер» (Actual size)</b> или <b>100%</b>\n"
        "• Ориентация: <b>Книжная (Portrait)</b>\n"
        "• Автоповорот: <b>Включен</b>\n\n"
        "💡 <i>Склеенный файл сохраняет идеальную чёткость штрихкодов!</i>",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )


@dp.message(F.text == "💬 Поддержка")
async def show_support(message: types.Message):
    await message.answer(
        "💬 <b>Служба поддержки:</b>\n\n"
        "Если у вас возникли вопросы по работе бота или оплате подписки:\n"
        "👨‍💻 Менеджер: @baur_bkh\n"
        "🕒 Время работы: 09:00 - 21:00",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )


async def send_single_photo_error(user_id: int, message: types.Message):
    await asyncio.sleep(1.5)
    await message.answer(
        "⚠️ <b>Формат не поддерживается!</b>\n\n"
        "Вы отправили изображение (фото). Бот работает только с <b>PDF-файлами</b> и <b>ZIP-архивами</b> с накладными Kaspi.\n\n"
        "Пожалуйста, выгрузите накладные из Kaspi Pay в формате <b>.pdf</b> или <b>.zip</b> и отправьте их документом.",
        parse_mode="HTML"
    )
    if user_id in user_photo_tasks:
        del user_photo_tasks[user_id]


@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id
    if user_id in user_photo_tasks:
        user_photo_tasks[user_id].cancel()

    user_photo_tasks[user_id] = asyncio.create_task(send_single_photo_error(user_id, message))


# ОСНОВНОЙ ПРОЦЕСС ОБРАБОТКИ
async def process_user_files(user_id: int, message: types.Message):
    await asyncio.sleep(2)

    files_list = user_files_buffer.pop(user_id, [])
    if not files_list:
        return

    user_data = get_or_create_user(user_id, message.from_user.username)
    duplicate_enabled = user_data["duplicate_mode"]

    valid_files = []
    invalid_found = False

    for doc in files_list:
        ext = os.path.splitext(doc.file_name)[1].lower() if doc.file_name else ""
        if ext in [".pdf", ".zip"]:
            valid_files.append(doc)
        else:
            invalid_found = True

    if invalid_found and not valid_files:
        await message.answer(
            "❌ <b>Ошибка формата!</b>\n\n"
            "Вы отправили неподдерживаемый файл.\n"
            "Пожалуйста, отправляйте только файлы <b>.pdf</b> или <b>.zip</b> архивы.",
            parse_mode="HTML"
        )
        return

    user_dir = f"./temp_{user_id}"
    os.makedirs(user_dir, exist_ok=True)

    status_msg = await message.answer(f"📥 Скачиваю и обрабатываю {len(valid_files)} фaйл(а/ов)...")

    pdf_files = []

    try:
        current_batch_number = user_data["user_batch_counter"]

        for idx, doc in enumerate(valid_files):
            file_info = await bot.get_file(doc.file_id)
            
            ext = os.path.splitext(doc.file_name)[1].lower()
            safe_name = f"file_{idx}{ext}"
            downloaded_path = os.path.join(user_dir, safe_name)
            
            await bot.download_file(file_info.file_path, destination=downloaded_path)

            if ext == ".zip":
                extract_dir = os.path.join(user_dir, f"ext_{idx}")
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(downloaded_path, "r") as zip_ref:
                    zip_ref.extractall(extract_dir)

                extracted_pdfs = []
                for root, _, files in os.walk(extract_dir):
                    for f in files:
                        if f.lower().endswith(".pdf"):
                            extracted_pdfs.append(os.path.join(root, f))
                
                extracted_pdfs.sort()
                pdf_files.extend(extracted_pdfs)

            elif ext == ".pdf":
                pdf_files.append(downloaded_path)

        if not pdf_files:
            await status_msg.edit_text("❌ В отправленных файлах не найдено PDF-накладных.")
            shutil.rmtree(user_dir, ignore_errors=True)
            return

        # Парсинг и группировка страниц по службам доставки
        grouped_pages, picklist_data = parse_and_sort_pdf_pages(pdf_files)
        total_orders = sum(len(pages) for pages in grouped_pages.values())
        
        already_used = user_data["used_today"]
        max_allowed = user_data["max_allowed"]
        has_active_sub = user_data["has_active_sub"]

        if not has_active_sub and (already_used + total_orders > max_allowed):
            remains = max(0, max_allowed - already_used)
            await status_msg.edit_text(
                f"🛑 <b>Превышен дневной лимит!</b>\n\n"
                f"Вы пытаетесь обработать: <code>{total_orders} шт.</code>\n"
                f"Ваш остаток на сегодня: <code>{remains} шт.</code> (Обработано сегодня: {already_used}/{max_allowed})\n\n"
                f"Для снятия ограничений выберите подходящий тариф через кнопку «⭐ Тарифы и подписка».\n"
                f"📞 Обратитесь к менеджеру: @baur_bkh",
                parse_mode="HTML"
            )
            shutil.rmtree(user_dir, ignore_errors=True)
            return

        now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

        await status_msg.edit_text(f"⚙️ Подготовка файла: ПАРТИЯ №{current_batch_number} ({total_orders} накладных)...")

        if picklist_data:
            user_pick_lists[f"{user_id}_{current_batch_number}"] = picklist_data

        writer = PdfWriter()

        # Титульная обложка партии
        cover_stream = create_cover_page(current_batch_number, total_orders, now_str)
        cover_reader = PdfReader(cover_stream)
        writer.add_page(cover_reader.pages[0])

        current_global_idx = 1
        current_page_counter = 2  # Учитываем первую обложку
        delivery_summary_text = ""

        # Проходим по каждой группе доставки
        for group_title, pages in grouped_pages.items():
            if not pages:
                continue

            # Добавляем обложку-разделитель для группы
            group_cover_stream = create_delivery_group_cover(group_title, len(pages))
            group_cover_reader = PdfReader(group_cover_stream)
            writer.add_page(group_cover_reader.pages[0])
            
            start_page = current_page_counter + 1
            current_page_counter += 1

            for page in pages:
                box = page.mediabox
                width = float(box.width)
                height = float(box.height)
                rotation = page.get('/Rotate', 0)
                
                if rotation in (90, 270):
                    width, height = height, width

                is_landscape = width > height

                stamp_stream = create_number_stamp(current_global_idx, total_orders, is_landscape=is_landscape)
                stamp_reader = PdfReader(stamp_stream)
                page.merge_page(stamp_reader.pages[0])

                if is_landscape:
                    page.rotate(90)

                writer.add_page(page)
                current_page_counter += 1

                if duplicate_enabled:
                    writer.add_page(page)
                    current_page_counter += 1

                current_global_idx += 1

            end_page = current_page_counter - 1
            delivery_summary_text += f"• {group_title}: <b>{len(pages)} шт.</b> <i>(стр. {start_page}–{end_page})</i>\n"

        output_pdf_path = os.path.join(user_dir, f"ПАРТИЯ_№{current_batch_number}.pdf")
        with open(output_pdf_path, "wb") as f_out:
            writer.write(f_out)

        update_user_usage_and_batch(user_id, total_orders)
        new_used = already_used + total_orders

        saved_minutes_total = total_orders * 0.5
        saved_minutes = int(saved_minutes_total)
        saved_seconds = int((saved_minutes_total - saved_minutes) * 60)

        if saved_minutes > 0 and saved_seconds > 0:
            time_str = f"~{saved_minutes} мин {saved_seconds} сек"
        elif saved_minutes > 0:
            time_str = f"~{saved_minutes} мин"
        else:
            time_str = f"~{saved_seconds} сек"

        mode_note = "\n📄 <i>Режим дублирования этикеток: ВКЛ (по 2 шт)</i>" if duplicate_enabled else ""
        sub_text = "♾ <i>Безлимитная подписка</i>" if has_active_sub else f"<code>{new_used} из {max_allowed} шт.</code>"

        summary_msg = (
            f"✅ <b>ПАРТИЯ №{current_batch_number} сформирована!</b>\n\n"
            f"🚚 <b>Сортировка по службам доставки:</b>\n"
            f"{delivery_summary_text}\n"
            f"📦 <b>Всего накладных:</b> <code>{total_orders} шт.</code>{mode_note}\n"
            f"⏱ <b>Сэкономлено времени:</b> <code>{time_str}</code>\n\n"
            f"📅 <b>Дата/Время:</b> <code>{now_str}</code>\n"
            f"📊 <b>Использовано лимита:</b> {sub_text}\n\n"
            f"💡 <i>Все накладные сгруппированы по курьерам! Нажмите «Показать Лист сборки» ниже.</i>"
        )

        await status_msg.delete()
        
        output_file = types.FSInputFile(output_pdf_path)
        await message.answer_document(
            document=output_file,
            caption=summary_msg,
            reply_markup=get_batch_result_keyboard(current_batch_number),
            parse_mode="HTML"
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ Произошла ошибка при обработке: {e}")

    finally:
        shutil.rmtree(user_dir, ignore_errors=True)
        if user_id in user_tasks:
            del user_tasks[user_id]


@dp.message(F.document)
async def handle_document(message: types.Message):
    user_id = message.from_user.id
    user_files_buffer[user_id].append(message.document)

    if user_id in user_tasks:
        user_tasks[user_id].cancel()

    user_tasks[user_id] = asyncio.create_task(process_user_files(user_id, message))


async def main():
    print("🚀 Бот @ksp_print с поддержкой авто-сортировки и статистики запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())