import os
import asyncio
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command

TOKEN = os.getenv("BOT_TOKEN")
DB_NAME = "budget.db"
USD_RATE_DEFAULT = 43

BUDGETS = ["Влад", "Валера", "Общий"]

EXPENSE_CATEGORIES = [
    "🛒 Продукты", "🍔 Кафе",
    "⛽ Топливо", "🚕 Такси",
    "🏠 Дом", "🚗 Авто",
    "👕 Одежда", "💊 Аптека",
    "🎮 Развлечения", "📦 Другое"
]

user_state = {}

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Пополнение"), KeyboardButton(text="➖ Расход")],
        [KeyboardButton(text="🔄 Старт месяца"), KeyboardButton(text="💱 Курс USD")],
        [KeyboardButton(text="💰 Баланс")],
        [KeyboardButton(text="📅 Отчёт за день"), KeyboardButton(text="📆 Отчёт за неделю")],
        [KeyboardButton(text="📊 Отчёт за месяц")]
    ],
    resize_keyboard=True
)

budget_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="👤 Влад"), KeyboardButton(text="👤 Валера")],
        [KeyboardButton(text="👥 Общий")],
        [KeyboardButton(text="⬅️ Назад")]
    ],
    resize_keyboard=True
)

category_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🛒 Продукты"), KeyboardButton(text="🍔 Кафе")],
        [KeyboardButton(text="⛽ Топливо"), KeyboardButton(text="🚕 Такси")],
        [KeyboardButton(text="🏠 Дом"), KeyboardButton(text="🚗 Авто")],
        [KeyboardButton(text="👕 Одежда"), KeyboardButton(text="💊 Аптека")],
        [KeyboardButton(text="🎮 Развлечения"), KeyboardButton(text="📦 Другое")],
        [KeyboardButton(text="⬅️ Назад")]
    ],
    resize_keyboard=True
)


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        budget TEXT,
        type TEXT,
        category TEXT,
        amount REAL,
        currency TEXT DEFAULT 'UAH',
        amount_uah REAL,
        usd_rate REAL DEFAULT 43,
        comment TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    cur.execute("""
    INSERT OR IGNORE INTO settings (key, value)
    VALUES ('usd_rate', ?)
    """, (str(USD_RATE_DEFAULT),))

    conn.commit()
    conn.close()


def get_usd_rate():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key='usd_rate'")
    row = cur.fetchone()
    conn.close()
    return float(row[0]) if row else USD_RATE_DEFAULT


def set_usd_rate(rate):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
    INSERT OR REPLACE INTO settings (key, value)
    VALUES ('usd_rate', ?)
    """, (str(rate),))
    conn.commit()
    conn.close()


def parse_amount(text):
    parts = text.lower().replace(",", ".").split()

    amount = float(parts[0])
    currency = "UAH"

    if len(parts) > 1:
        if parts[1] in ["usd", "$", "дол", "доллар", "долларов"]:
            currency = "USD"
        elif parts[1] in ["uah", "грн", "гривна", "гривен"]:
            currency = "UAH"

    comment = " ".join(parts[2:]) if len(parts) > 2 else ""

    rate = get_usd_rate()
    amount_uah = amount * rate if currency == "USD" else amount

    return amount, currency, amount_uah, rate, comment


def add_operation(budget, op_type, category, amount, currency, amount_uah, usd_rate, comment=""):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO operations 
    (date, budget, type, category, amount, currency, amount_uah, usd_rate, comment)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        budget,
        op_type,
        category,
        amount,
        currency,
        amount_uah,
        usd_rate,
        comment
    ))

    conn.commit()
    conn.close()


def get_balance():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    result = {}

    for budget in BUDGETS:
        cur.execute("""
        SELECT 
            SUM(CASE WHEN type IN ('Пополнение', 'Старт месяца') THEN amount_uah ELSE 0 END),
            SUM(CASE WHEN type = 'Расход' THEN amount_uah ELSE 0 END)
        FROM operations
        WHERE budget = ?
        """, (budget,))

        income, expense = cur.fetchone()
        income = income or 0
        expense = expense or 0

        result[budget] = income - expense

    conn.close()
    return result


def make_report(period):
    now = datetime.now()

    if period == "day":
        title = "за день"
        date_from = now.strftime("%Y-%m-%d 00:00")
    elif period == "week":
        title = "за неделю"
        date_from = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    else:
        title = "за месяц"
        date_from = now.strftime("%Y-%m-01 00:00")

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    text = f"📊 Отчёт {title}\n\n"

    for budget in BUDGETS:
        cur.execute("""
        SELECT 
            SUM(CASE WHEN type IN ('Пополнение', 'Старт месяца') THEN amount_uah ELSE 0 END),
            SUM(CASE WHEN type = 'Расход' THEN amount_uah ELSE 0 END)
        FROM operations
        WHERE budget = ? AND date >= ?
        """, (budget, date_from))

        income, expense = cur.fetchone()
        income = income or 0
        expense = expense or 0
        balance = income - expense

        text += (
            f"🔹 {budget}\n"
            f"➕ Пополнения: {income:.2f} грн\n"
            f"➖ Расходы: {expense:.2f} грн\n"
            f"💰 Остаток: {balance:.2f} грн\n"
        )

        cur.execute("""
        SELECT category, SUM(amount_uah)
        FROM operations
        WHERE budget = ? AND type = 'Расход' AND date >= ?
        GROUP BY category
        ORDER BY SUM(amount_uah) DESC
        """, (budget, date_from))

        categories = cur.fetchall()

        if categories:
            text += "Категории:\n"
            for category, total in categories:
                text += f"• {category}: {total:.2f} грн\n"

        text += "\n"

    conn.close()
    return text


bot = Bot(token=TOKEN)
dp = Dispatcher()


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer("ВЕРСИЯ 999 ✅", reply_markup=main_kb)


@dp.message(F.text.in_(["➕ Пополнение", "➖ Расход", "🔄 Старт месяца"]))
async def choose_action(message: Message):
    user_id = message.from_user.id
    action = message.text.replace("➕ ", "").replace("➖ ", "").replace("🔄 ", "")

    user_state[user_id] = {
        "action": action,
        "budget": None,
        "category": None
    }

    await message.answer("Теперь выбери, для кого:", reply_markup=budget_kb)


@dp.message(F.text.in_(["👤 Влад", "👤 Валера", "👥 Общий"]))
async def choose_budget(message: Message):
    user_id = message.from_user.id

    if user_id not in user_state:
        await message.answer("Сначала выбери действие.", reply_markup=main_kb)
        return

    budget = message.text.replace("👤 ", "").replace("👥 ", "")
    user_state[user_id]["budget"] = budget

    action = user_state[user_id]["action"]

    if action == "Расход":
        await message.answer("Выбери категорию расхода:", reply_markup=category_kb)
    else:
        await message.answer(
            f"Введи сумму для {budget}.\n\n"
            f"Примеры:\n"
            f"1000 грн\n"
            f"100 usd\n"
            f"100 $"
        )


@dp.message(F.text.in_(EXPENSE_CATEGORIES))
async def choose_category(message: Message):
    user_id = message.from_user.id

    if user_id not in user_state:
        await message.answer("Сначала выбери расход.", reply_markup=main_kb)
        return

    user_state[user_id]["category"] = message.text

    await message.answer(
        "Введи сумму расхода.\n\n"
        "Примеры:\n"
        "1200 грн\n"
        "50 usd\n"
        "50 $"
    )


@dp.message(F.text == "💱 Курс USD")
async def usd_rate(message: Message):
    user_state[message.from_user.id] = {"action": "Курс USD"}
    await message.answer(f"Текущий курс: {get_usd_rate()} грн\nВведи новый курс, например: 43")


@dp.message(F.text == "💰 Баланс")
async def balance(message: Message):
    data = get_balance()
    text = "💰 Баланс:\n\n"

    for budget, value in data.items():
        text += f"🔹 {budget}: {value:.2f} грн\n"

    await message.answer(text)


@dp.message(F.text == "📅 Отчёт за день")
async def report_day(message: Message):
    await message.answer(make_report("day"))


@dp.message(F.text == "📆 Отчёт за неделю")
async def report_week(message: Message):
    await message.answer(make_report("week"))


@dp.message(F.text == "📊 Отчёт за месяц")
async def report_month(message: Message):
    await message.answer(make_report("month"))


@dp.message(F.text == "⬅️ Назад")
async def back(message: Message):
    await message.answer("Выбери действие:", reply_markup=main_kb)


@dp.message()
async def handle_amount(message: Message):
    user_id = message.from_user.id

    if user_id not in user_state:
        await message.answer("Сначала выбери действие.", reply_markup=main_kb)
        return

    action = user_state[user_id].get("action")

    if action == "Курс USD":
        try:
            rate = float(message.text.replace(",", "."))
            set_usd_rate(rate)
            await message.answer(f"✅ Курс USD установлен: {rate} грн", reply_markup=main_kb)
        except ValueError:
            await message.answer("Введи курс числом. Например: 43")
        return

    budget = user_state[user_id].get("budget")
    category = user_state[user_id].get("category")

    if not budget:
        await message.answer("Сначала выбери Влад / Валера / Общий.", reply_markup=budget_kb)
        return

    if action == "Расход" and not category:
        await message.answer("Сначала выбери категорию расхода.", reply_markup=category_kb)
        return

    try:
        amount, currency, amount_uah, rate, comment = parse_amount(message.text)
    except Exception:
        await message.answer("Введи сумму правильно. Например: 1200 грн или 50 usd")
        return

    if action == "Пополнение":
        category = "Пополнение"
    elif action == "Старт месяца":
        category = "Старт месяца"

    add_operation(
        budget=budget,
        op_type=action,
        category=category,
        amount=amount,
        currency=currency,
        amount_uah=amount_uah,
        usd_rate=rate,
        comment=comment
    )

    await message.answer(
        f"✅ Записано\n\n"
        f"Кому: {budget}\n"
        f"Тип: {action}\n"
        f"Категория: {category}\n"
        f"Сумма: {amount:.2f} {currency}\n"
        f"В гривне: {amount_uah:.2f} грн\n"
        f"Курс USD: {rate}",
        reply_markup=main_kb
    )

    user_state[user_id] = {}


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
