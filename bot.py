import asyncio
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command

import os

TOKEN = os.getenv("8617225972:AAEDYCyM6tlAWx4PjUKJ7BSxf0dTiSJ37w4")
DB_NAME = "budget.db"

BUDGETS = ["Влад", "Валера", "Общий"]
user_state = {}

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Пополнение"), KeyboardButton(text="➖ Расход")],
        [KeyboardButton(text="🔄 Старт месяца")],
        [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="📊 Отчёт")]
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
        comment TEXT
    )
    """)

    conn.commit()
    conn.close()


def add_operation(budget, op_type, category, amount, comment=""):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO operations (date, budget, type, category, amount, comment)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        budget,
        op_type,
        category,
        amount,
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
            SUM(CASE WHEN type IN ('Пополнение', 'Старт месяца') THEN amount ELSE 0 END),
            SUM(CASE WHEN type = 'Расход' THEN amount ELSE 0 END)
        FROM operations
        WHERE budget = ?
        """, (budget,))

        income, expense = cur.fetchone()
        income = income or 0
        expense = expense or 0

        result[budget] = income - expense

    conn.close()
    return result


def get_month_report():
    current_month = datetime.now().strftime("%Y-%m")

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    text = f"📊 Отчёт за месяц {current_month}\n\n"

    for budget in BUDGETS:
        cur.execute("""
        SELECT 
            SUM(CASE WHEN type IN ('Пополнение', 'Старт месяца') THEN amount ELSE 0 END),
            SUM(CASE WHEN type = 'Расход' THEN amount ELSE 0 END)
        FROM operations
        WHERE budget = ? AND date LIKE ?
        """, (budget, f"{current_month}%"))

        income, expense = cur.fetchone()
        income = income or 0
        expense = expense or 0
        balance = income - expense

        text += (
            f"🔹 {budget}\n"
            f"➕ Пополнения: {income:.2f}\n"
            f"➖ Расходы: {expense:.2f}\n"
            f"💰 Остаток: {balance:.2f}\n\n"
        )

    conn.close()
    return text


bot = Bot(token=TOKEN)
dp = Dispatcher()


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Привет 👋\n\nВыбери действие:",
        reply_markup=main_kb
    )


@dp.message(F.text.in_(["➕ Пополнение", "➖ Расход", "🔄 Старт месяца"]))
async def choose_action(message: Message):
    user_id = message.from_user.id

    user_state[user_id] = {
        "action": message.text.replace("➕ ", "").replace("➖ ", "").replace("🔄 ", ""),
        "budget": None
    }

    await message.answer(
        "Теперь выбери, для кого:",
        reply_markup=budget_kb
    )


@dp.message(F.text.in_(["👤 Влад", "👤 Валера", "👥 Общий"]))
async def choose_budget(message: Message):
    user_id = message.from_user.id

    if user_id not in user_state or not user_state[user_id].get("action"):
        await message.answer("Сначала выбери действие.", reply_markup=main_kb)
        return

    if message.text == "👤 Влад":
        budget = "Влад"
    elif message.text == "👤 Валера":
        budget = "Валера"
    else:
        budget = "Общий"

    user_state[user_id]["budget"] = budget

    action = user_state[user_id]["action"]

    if action == "Старт месяца":
        await message.answer(f"Введи стартовую сумму для {budget}:")
    elif action == "Пополнение":
        await message.answer(f"Введи сумму пополнения для {budget}:")
    else:
        await message.answer(f"Введи сумму расхода для {budget}:")


@dp.message(F.text == "⬅️ Назад")
async def back(message: Message):
    await message.answer("Выбери действие:", reply_markup=main_kb)


@dp.message(F.text == "💰 Баланс")
async def balance(message: Message):
    data = get_balance()

    text = "💰 Баланс:\n\n"
    for budget, balance_value in data.items():
        text += f"🔹 {budget}: {balance_value:.2f}\n"

    await message.answer(text)


@dp.message(F.text == "📊 Отчёт")
async def report(message: Message):
    await message.answer(get_month_report())


@dp.message()
async def handle_amount(message: Message):
    user_id = message.from_user.id

    if user_id not in user_state:
        await message.answer("Сначала выбери действие.", reply_markup=main_kb)
        return

    action = user_state[user_id].get("action")
    budget = user_state[user_id].get("budget")

    if not action:
        await message.answer("Сначала выбери пополнение или расход.", reply_markup=main_kb)
        return

    if not budget:
        await message.answer("Сначала выбери Влад / Валера / Общий.", reply_markup=budget_kb)
        return

    parts = message.text.strip().split()

    try:
        amount = float(parts[0].replace(",", "."))
    except ValueError:
        await message.answer("Введи сумму числом. Например: 1200")
        return

    comment = " ".join(parts[1:]) if len(parts) > 1 else ""

    if action == "Пополнение":
        category = comment if comment else "Пополнение"
    elif action == "Расход":
        category = comment if comment else "Расход"
    else:
        category = "Старт месяца"

    add_operation(budget, action, category, amount, comment)

    balance_now = get_balance()[budget]

    await message.answer(
        f"✅ Записано\n\n"
        f"Кому: {budget}\n"
        f"Тип: {action}\n"
        f"Сумма: {amount:.2f}\n"
        f"Остаток: {balance_now:.2f}",
        reply_markup=main_kb
    )

    user_state[user_id] = {
        "action": None,
        "budget": None
    }


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
