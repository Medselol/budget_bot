#!/usr/bin/env python3
"""Private construction cash ledger over Telegram Bot API. Python 3.10+, stdlib only."""
import csv
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Kyiv")
STAGES = ["Земля и оформление", "Проектирование", "Фундамент", "Стены", "Перекрытия", "Кровля", "Окна и двери", "Фасад", "Электрика", "Сантехника", "Отопление", "Вентиляция", "Внутренние работы", "Коммуникации", "Благоустройство", "Прочее"]
COST_TYPES = ["Материалы", "Работа", "Техника", "Доставка", "Услуги", "Прочее"]
SOURCES = ["Личные средства", "Инвестор", "Кредит", "Аванс покупателя", "Оплата покупателя", "Возврат поставщика", "Прочее"]
SALES = {"Аванс покупателя", "Оплата покупателя"}
CURRENCIES = ("UAH", "USD")
DEFAULT_PROJECT_NAME = "Вита-Почтовая Дуплексы"


def money(kop, currency="UAH"):
    value = f"{Decimal(kop) / 100:,.2f}".replace(",", " ")
    return value + (" грн" if currency == "UAH" else " USD")


def parse_amount(value):
    value = value.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", value):
        raise ValueError("Напиши сумму числом, например 12500 или 12500,50.")
    try:
        amount = int((Decimal(value) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, OverflowError):
        raise ValueError("Некорректная сумма.") from None
    if amount <= 0 or amount > 100_000_000_000:
        raise ValueError("Сумма должна быть больше нуля и не выше 1 млрд единиц валюты.")
    return amount


def valid_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("Дата нужна в формате ГГГГ-ММ-ДД, например 2026-09-23.") from None


def connect(path, owner_id=0, initial_users=()):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, name TEXT NOT NULL, UNIQUE(owner_id,name));
        CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, name TEXT NOT NULL, UNIQUE(owner_id,name));
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS telegram_contacts (id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS drafts (user_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY,
            update_id INTEGER UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            occurred_on TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('income','expense','transfer')),
            project_id INTEGER REFERENCES projects(id),
            category TEXT, cost_type TEXT, source TEXT,
            account_id INTEGER REFERENCES accounts(id),
            from_account_id INTEGER REFERENCES accounts(id),
            to_account_id INTEGER REFERENCES accounts(id),
            amount_kop INTEGER NOT NULL CHECK(amount_kop > 0),
            currency TEXT NOT NULL DEFAULT 'UAH' CHECK(currency IN ('UAH','USD')),
            comment TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS operations_date ON operations(occurred_on, project_id);
    """)
    # Rebuild old globally unique names so each user can have their own accounts/projects.
    db.commit()
    for table in ("projects", "accounts"):
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if "owner_id" not in columns:
            db.execute("PRAGMA foreign_keys=OFF")
            with db:
                db.execute(f"CREATE TABLE {table}_new (id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, name TEXT NOT NULL, UNIQUE(owner_id,name))")
                db.execute(f"INSERT INTO {table}_new(id,owner_id,name) SELECT id,?,name FROM {table}", (owner_id,))
                db.execute(f"DROP TABLE {table}")
                db.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
            db.execute("PRAGMA foreign_keys=ON")
    # Older databases contained hryvnia-only, owner-only transactions.
    columns = {row["name"] for row in db.execute("PRAGMA table_info(operations)")}
    if "currency" not in columns:
        db.execute("ALTER TABLE operations ADD COLUMN currency TEXT NOT NULL DEFAULT 'UAH' CHECK(currency IN ('UAH','USD'))")
    if "user_id" not in columns:
        db.execute("ALTER TABLE operations ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE operations SET user_id=?", (owner_id,))
    if not db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        for uid in (owner_id, *initial_users):
            db.execute("INSERT OR IGNORE INTO users(id,name) VALUES (?,?)", (uid, "Денис" if uid == owner_id else f"Участник {uid}"))
    if "role" not in {r["name"] for r in db.execute("PRAGMA table_info(users)")}:
        db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
    if "deleted" not in {r["name"] for r in db.execute("PRAGMA table_info(users)")}:
        db.execute("ALTER TABLE users ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
    db.execute("UPDATE users SET role='owner',active=1 WHERE id=?", (owner_id,))
    # Keep the owner's display name aligned with the configured owner account.
    db.execute("UPDATE users SET name='Денис' WHERE id=? AND name='Владелец'", (owner_id,))
    for name in ("Наличные", "Карта", "Счёт"):
        for row in db.execute("SELECT id FROM users"):
            db.execute("INSERT OR IGNORE INTO accounts(owner_id,name) VALUES (?,?)", (row[0], name))
    # This bot tracks one construction site. Create the same default project
    # for each active user so new operations never need a project picker.
    for row in db.execute("SELECT id FROM users WHERE active=1"):
        db.execute("INSERT OR IGNORE INTO projects(owner_id,name) VALUES (?,?)", (row[0], DEFAULT_PROJECT_NAME))
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise RuntimeError("Нарушены связи в базе после обновления")
    from receipts import init as init_receipts
    init_receipts(db)
    return db


def names(db, table, user_id=0):
    assert table in ("projects", "accounts")
    return db.execute(f"SELECT id,name FROM {table} WHERE owner_id=? ORDER BY id", (user_id,)).fetchall()


def default_project_id(db, user_id):
    row = db.execute("SELECT id FROM projects WHERE owner_id=? AND name=?", (user_id, DEFAULT_PROJECT_NAME)).fetchone()
    if row:
        return row[0]
    with db:
        db.execute("INSERT OR IGNORE INTO projects(owner_id,name) VALUES (?,?)", (user_id, DEFAULT_PROJECT_NAME))
    return db.execute("SELECT id FROM projects WHERE owner_id=? AND name=?", (user_id, DEFAULT_PROJECT_NAME)).fetchone()[0]


def draft(db, user_id):
    row = db.execute("SELECT data FROM drafts WHERE user_id=?", (user_id,)).fetchone()
    return json.loads(row[0]) if row else None


def set_draft(db, user_id, data):
    db.execute("INSERT INTO drafts(user_id,data) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data", (user_id, json.dumps(data, ensure_ascii=False)))
    db.commit()


def clear_draft(db, user_id):
    db.execute("DELETE FROM drafts WHERE user_id=?", (user_id,))
    db.commit()


def record(db, data, update_id, user_id=0):
    currency = data.get("currency", "UAH")
    if currency not in CURRENCIES:
        raise ValueError("Неизвестная валюта")
    for key, table in (("project_id", "projects"), ("account_id", "accounts"), ("from_account_id", "accounts"), ("to_account_id", "accounts")):
        if data.get(key) and not db.execute(f"SELECT 1 FROM {table} WHERE id=? AND owner_id=?", (data[key], user_id)).fetchone():
            raise ValueError("Объект или счёт не принадлежит пользователю")
    keys = ("user_id", "occurred_on", "kind", "project_id", "category", "cost_type", "source", "account_id", "from_account_id", "to_account_id", "amount_kop", "currency", "comment")
    vals = [currency if key == "currency" else user_id if key == "user_id" else data.get("comment", "") if key == "comment" else data.get(key) for key in keys]
    with db:
        cursor = db.execute("INSERT OR IGNORE INTO operations(update_id," + ",".join(keys) + ") VALUES (" + ",".join("?" for _ in range(len(keys) + 1)) + ")", [update_id] + vals)
        row = db.execute("SELECT id FROM operations WHERE update_id=?", (update_id,)).fetchone()
        if row is None:
            raise ValueError("Операция не сохранена. Проверь сумму и счета.")
        if cursor.rowcount:
            bump_revision(db)
    return row[0]


def bump_revision(db):
    db.execute("INSERT INTO settings(key,value) VALUES ('ledger_revision','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")


def update_operation(db, data, operation_id, user_id):
    if data.get("currency", "UAH") not in CURRENCIES:
        raise ValueError("Неизвестная валюта")
    if data.get("kind") not in ("income", "expense", "transfer"):
        raise ValueError("Неизвестный тип операции")
    if not isinstance(data.get("amount_kop"), int) or data["amount_kop"] <= 0:
        raise ValueError("Некорректная сумма")
    for key, table in (("project_id", "projects"), ("account_id", "accounts"), ("from_account_id", "accounts"), ("to_account_id", "accounts")):
        if data.get(key) and not db.execute(f"SELECT 1 FROM {table} WHERE id=? AND owner_id=?", (data[key], user_id)).fetchone():
            raise ValueError("Объект или счёт не принадлежит пользователю")
    with db:
        cursor = db.execute("""UPDATE operations SET occurred_on=?,kind=?,project_id=?,category=?,cost_type=?,source=?,
            account_id=?,from_account_id=?,to_account_id=?,amount_kop=?,currency=?,comment=? WHERE id=? AND user_id=?""",
            (data["occurred_on"], data["kind"], data.get("project_id"), data.get("category"), data.get("cost_type"),
             data.get("source"), data.get("account_id"), data.get("from_account_id"), data.get("to_account_id"),
             data["amount_kop"], data.get("currency", "UAH"), data.get("comment", ""), operation_id, user_id))
        if not cursor.rowcount:
            raise ValueError("Операция не найдена или принадлежит другому пользователю")
        bump_revision(db)


def operation_row(db, operation_id, user_id):
    return next((row for row in rows_for(db, user_id=user_id) if row["id"] == operation_id), None)


def operation_text(db, row):
    data = {key: row[key] for key in ("kind", "occurred_on", "project_id", "category", "cost_type", "source", "account_id", "from_account_id", "to_account_id", "amount_kop", "currency", "comment")}
    return f"Операция №{row['id']}\n" + description(db, data, row["user_id"])


def rows_for(db, start=None, end=None, project_id=None, user_id=None):
    sql = """SELECT o.*,p.name AS project,a.name AS account,(SELECT name FROM users WHERE id=af.owner_id) || ' · ' || af.name AS from_account,(SELECT name FROM users WHERE id=at.owner_id) || ' · ' || at.name AS to_account,u.name AS user_name
        FROM operations o LEFT JOIN projects p ON p.id=o.project_id
        LEFT JOIN accounts a ON a.id=o.account_id
        LEFT JOIN accounts af ON af.id=o.from_account_id
        LEFT JOIN accounts at ON at.id=o.to_account_id
        LEFT JOIN users u ON u.id=o.user_id WHERE 1=1"""
    args = []
    if user_id is not None:
        sql += " AND ((o.kind!='transfer' AND o.user_id=?) OR (o.kind='transfer' AND (af.owner_id=? OR at.owner_id=?)))"; args.extend([user_id]*3)
    if start:
        sql += " AND o.occurred_on>=?"; args.append(start)
    if end:
        sql += " AND o.occurred_on<=?"; args.append(end)
    if project_id is not None:
        sql += " AND o.project_id=?"; args.append(project_id)
    return db.execute(sql + " ORDER BY o.occurred_on,o.id", args).fetchall()


def summary(rows):
    result = {code: {"financing": 0, "sales": 0, "refunds": 0, "expense": 0, "by_stage": {}, "by_source": {}, "by_type": {}} for code in CURRENCIES}
    for row in rows:
        currency = row["currency"]
        if currency not in result:
            raise ValueError("Неизвестная валюта в базе")
        part = result[currency]
        if row["kind"] == "income":
            source = row["source"] or "Прочее"
            group = "sales" if source in SALES else "refunds" if source == "Возврат поставщика" else "financing"
            part[group] += row["amount_kop"]
            part["by_source"][source] = part["by_source"].get(source, 0) + row["amount_kop"]
        elif row["kind"] == "expense":
            part["expense"] += row["amount_kop"]
            for key, field in (("by_stage", "category"), ("by_type", "cost_type")):
                label = row[field] or "Прочее"
                part[key][label] = part[key].get(label, 0) + row["amount_kop"]
    for part in result.values():
        part["cash_net"] = part["financing"] + part["sales"] + part["refunds"] - part["expense"]
    return result


def balances(db, user_id=0):
    result = {row["id"]: [row["name"], {code: 0 for code in CURRENCIES}] for row in names(db, "accounts", user_id)}
    for row in rows_for(db, user_id=user_id):
        amount = row["amount_kop"]
        currency = row["currency"]
        if row["kind"] == "transfer":
            if row["from_account_id"] in result: result[row["from_account_id"]][1][currency] -= amount
            if row["to_account_id"] in result: result[row["to_account_id"]][1][currency] += amount
        elif row["kind"] == "income":
            result[row["account_id"]][1][currency] += amount
        else:
            result[row["account_id"]][1][currency] -= amount
    return result


def safe_cell(value):
    text = str(value or "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def csv_report(rows, start, end, project):
    totals = summary(rows)
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=";")
    writer.writerow(["Отчёт по стройке", project, f"{start or 'начало'} — {end or 'сегодня'}"])
    for currency in CURRENCIES:
        for label, key in (("Вложения и займы", "financing"), ("Поступления от покупателей", "sales"), ("Возвраты", "refunds"), ("Расходы", "expense"), ("Чистое изменение денег", "cash_net")):
            writer.writerow([label, f"{Decimal(totals[currency][key]) / 100:.2f}", currency])
    writer.writerow([])
    writer.writerow(["ID", "Пользователь ID", "Пользователь", "Дата", "Операция", "Объект", "Этап", "Тип затрат", "Источник прихода", "Счёт", "Счёт отправителя", "Счёт получателя", "Сумма", "Валюта", "Комментарий"])
    for row in rows:
        writer.writerow([row["id"], row["user_id"], safe_cell(row["user_name"]), row["occurred_on"], row["kind"], safe_cell(row["project"]), safe_cell(row["category"]), safe_cell(row["cost_type"]), safe_cell(row["source"]), safe_cell(row["account"]), safe_cell(row["from_account"]), safe_cell(row["to_account"]), f"{Decimal(row['amount_kop']) / 100:.2f}", row["currency"], safe_cell(row["comment"])])
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def keyboard(options, width=2):
    buttons = [{"text": label, "callback_data": callback} for label, callback in options]
    return {"inline_keyboard": [buttons[i:i + width] for i in range(0, len(buttons), width)]}


class Telegram:
    def __init__(self, token):
        self.url = f"https://api.telegram.org/bot{token}/"

    def call(self, method, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url + method, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(request, timeout=50) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError(result.get("description", "Telegram API error"))
        return result["result"]

    def send(self, chat, text, options=None):
        payload = {"chat_id": chat, "text": text}
        if options:
            payload["reply_markup"] = keyboard(options)
        self.call("sendMessage", payload)

    def document(self, chat, name, content):
        mime = {".pdf":"application/pdf", ".jpg":"image/jpeg", ".png":"image/png"}.get(Path(name).suffix.lower(), "text/csv")
        boundary = "----buildledger" + str(int(time.time() * 1000))
        data = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{name}\"\r\nContent-Type: {mime}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(self.url + "sendDocument", data=data, headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise RuntimeError(result.get("description", "Telegram upload error"))


ROLE_NAMES = {"owner": "Владелец", "editor": "Главный редактор", "foreman": "Прораб", "investor": "Инвестор", "member": "Участник"}


def role_for(db, uid, owner_id=0):
    row = db.execute("SELECT role,active,deleted FROM users WHERE id=?", (uid,)).fetchone()
    if not row or not row["active"] or row["deleted"]: return None
    return "owner" if uid == owner_id else row["role"]


def manager(db, uid, owner_id=0):
    return role_for(db, uid, owner_id) in ("owner", "editor")


def reader(db, uid, owner_id=0):
    return role_for(db, uid, owner_id) in ("owner", "editor", "investor")


def menu(bot, chat, is_owner=False, role=None):
    role = role or ("owner" if is_owner else "member")
    options = []
    if role == "foreman":
        options = [("Приход", "new:income"), ("Расход", "new:expense"), ("Мой баланс", "balance"), ("Мои чеки", "rc:mine")]
    elif role != "investor":
        options = [("Расход", "new:expense"), ("Приход", "new:income"), ("Перевод", "new:transfer"),
                   ("Операции", "ops:list"), ("Мой отчёт PDF", "report:menu"), ("Мои остатки", "balance")]
    if role in ("owner", "editor", "investor"):
        options += [("Общий отчёт PDF", "all:menu"), ("Участники и счета", "people:menu"), ("Балансы участников", "team:balances")]
    if role in ("owner", "editor"):
        options += [("Пользователи", "users:list"), ("Выдать деньги", "fund:menu"), ("Проверка расходов", "rc:list")]
    bot.send(chat, "Учёт стройки. Выбери действие:", options)


def prompt(bot, db, chat, d, uid=0):
    uid = d.get("ledger_user_id", uid)
    step = d["step"]
    if step == "project":
        options = [(r["name"], f"proj:{r['id']}") for r in names(db, "projects", uid)]
        options.append(("Общие расходы / без объекта", "proj:0"))
        bot.send(chat, "Выбери объект. Добавить: /project Название", options)
    elif step == "stage":
        bot.send(chat, "Этап расходов:", [(x, f"stage:{i}") for i, x in enumerate(STAGES)])
    elif step == "cost_type":
        bot.send(chat, "Тип затрат:", [(x, f"type:{i}") for i, x in enumerate(COST_TYPES)])
    elif step == "source":
        bot.send(chat, "Откуда поступили деньги?", [(x, f"source:{i}") for i, x in enumerate(SOURCES)])
    elif step in ("account", "from_account", "to_account"):
        excluded = d.get("from_account_id") if step == "to_account" else None
        options = [(r["name"], f"acct:{r['id']}") for r in names(db, "accounts", uid) if r["id"] != excluded]
        label = "Счёт отправителя:" if step == "from_account" else "Счёт получателя:" if step == "to_account" else "Откуда оплачено?" if d["kind"] == "expense" else "Куда поступили деньги?"
        bot.send(chat, label + " Добавить счёт: /account Название", options)
    elif step == "currency":
        bot.send(chat, "Выбери валюту операции:", [("Гривны (UAH)", "currency:UAH"), ("Доллары (USD)", "currency:USD")])
    elif step == "amount":
        bot.send(chat, f"Напиши сумму в {'гривнах' if d.get('currency', 'UAH') == 'UAH' else 'долларах'}, например 12500,50.")
    elif step == "date":
        bot.send(chat, "Дата операции:", [("Сегодня", "date:today"), ("Другая дата", "date:custom")])
    elif step == "date_text":
        bot.send(chat, "Напиши дату ГГГГ-ММ-ДД.")
    elif step == "comment":
        bot.send(chat, "Напиши комментарий или нажми «Пропустить».", [("Пропустить", "comment:skip")])
    elif step == "confirm":
        verb = "Подтвердить изменение" if d.get("edit_id") else "Сохранить"
        bot.send(chat, description(db, d, uid) + "\n\n" + ("Применить изменения к операции?" if d.get("edit_id") else "Сохранить?"), [(verb, "save"), ("Отмена", "cancel")])


def description(db, d, uid=0):
    title = {"income": "Приход", "expense": "Расход", "transfer": "Перевод"}[d["kind"]]
    line = [title + ": " + money(d["amount_kop"], d.get("currency", "UAH")), "Дата: " + d["occurred_on"]]
    if d["kind"] != "transfer":
        r = db.execute("SELECT name FROM projects WHERE id=? AND owner_id=?", (d.get("project_id"), uid)).fetchone()
        line.append("Объект: " + (r[0] if r else "Общие / без объекта"))
    if d["kind"] == "expense":
        line.extend(["Этап: " + (d.get("category") or "Прочее"), "Тип: " + (d.get("cost_type") or "Прочее")])
    if d["kind"] == "income":
        line.append("Источник: " + (d.get("source") or "Прочее"))
    for key, label in (("account_id", "Счёт"), ("from_account_id", "Откуда"), ("to_account_id", "Куда")):
        if key in d:
            if d["kind"] == "transfer":
                r = db.execute("SELECT u.name || ' · ' || a.name FROM accounts a JOIN users u ON u.id=a.owner_id WHERE a.id=?", (d[key],)).fetchone()
            else:
                r = db.execute("SELECT name FROM accounts WHERE id=? AND owner_id=?", (d[key], uid)).fetchone()
            line.append(label + ": " + (r[0] if r else "Недоступен"))
    if d.get("comment"):
        line.append("Комментарий: " + d["comment"])
    return "\n".join(line)


def next_step(d, step):
    if step == "project":
        return "stage" if d["kind"] == "expense" else "source"
    if step == "stage": return "cost_type"
    if step in ("cost_type", "source"): return "account"
    if step == "from_account": return "to_account"
    if step in ("account", "to_account"): return "currency"
    if step == "currency": return "amount"
    if step == "amount": return "date"
    if step in ("date", "date_text"): return "comment"
    if step == "comment": return "confirm"
    raise ValueError("Unknown step")


def advance(bot, db, chat, uid, d):
    d["step"] = next_step(d, d["step"])
    set_draft(db, uid, d)
    prompt(bot, db, chat, d, uid)


def report_text(db, start, end, pid, user_id=None):
    rows = rows_for(db, start, end, pid, user_id)
    totals = summary(rows)
    name = "Все объекты" if pid is None else db.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()[0]
    lines = [f"Отчёт: {name}", f"Период: {start or 'с начала'} — {end or 'сегодня'}"]
    for currency in CURRENCIES:
        t = totals[currency]
        lines.extend(["", "Гривны (UAH):" if currency == "UAH" else "Доллары (USD):", "Приход денег:", "  Вложения и займы: " + money(t["financing"], currency), "  От покупателей: " + money(t["sales"], currency), "  Возвраты: " + money(t["refunds"], currency), "Расходы: " + money(t["expense"], currency), "Изменение денег: " + money(t["cash_net"], currency)])
        if t["by_stage"]:
            lines.extend(["По этапам:"] + [f"  {k}: {money(v, currency)}" for k, v in sorted(t["by_stage"].items())])
        if t["by_source"]:
            lines.extend(["Источники прихода:"] + [f"  {k}: {money(v, currency)}" for k, v in sorted(t["by_source"].items())])
    if user_id is None:
        lines.append("\nПо пользователям:")
        for user in db.execute("SELECT id,name FROM users ORDER BY id"):
            per_user = summary(row for row in rows if row["user_id"] == user["id"])
            lines.append(f"  {user['name']} (ID {user['id']}): " + "; ".join(money(per_user[c]["cash_net"], c) for c in CURRENCIES))
    return "\n".join(lines), rows, name


def show_report(bot, db, chat, start, end, pid, user_id=None, viewer_id=None):
    txt, rows, name = report_text(db, start, end, pid, user_id)
    csv_button = f"csvall:{start or '0'}:{end or '0'}" if user_id is None else f"csv:{start or '0'}:{end or '0'}:{pid or 0}"
    if viewer_id is not None and user_id is not None:
        csv_button = f"csvuser:{user_id}:{start or '0'}:{end or '0'}"
    from pdf_report import pdf_report
    scope = "Общий отчёт" if user_id is None else "Личный отчёт"
    if user_id is not None:
        participant = db.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone()
        scope = "Участник: " + participant[0] if participant else scope
        txt = scope + "\n" + txt
    from receipts import enrich
    rows = enrich(bot, db, rows)
    bot.document(chat, "stroika_report.pdf", pdf_report(rows, start, end, DEFAULT_PROJECT_NAME if name == "Все объекты" else name, scope))
    bot.send(chat, txt[:4000] + ("\n…" if len(txt) > 4000 else ""), [("Скачать CSV для Excel", csv_button), ("Меню", "menu")])


def show_operations(bot, db, chat, user_id):
    rows = rows_for(db, user_id=user_id)[-10:][::-1]
    if not rows:
        bot.send(chat, "Пока нет сохранённых операций.", [("Меню", "menu")]); return
    options = []
    for row in rows:
        label = {"expense": "Расход", "income": "Приход", "transfer": "Перевод"}[row["kind"]]
        options.append((f"№{row['id']} · {label} · {money(row['amount_kop'], row['currency'])}", f"op:view:{row['id']}"))
    options.append(("Меню", "menu"))
    bot.send(chat, "Последние операции. Выбери запись для просмотра, изменения или удаления:", options)


def _handle_callback(bot, db, chat, uid, update_id, value, owner_id=0):
    d = draft(db, uid)
    if value == "menu": menu(bot, chat, role=role_for(db, uid, owner_id)); return
    if value == "cancel":
        clear_draft(db, uid); bot.send(chat, "Отменено."); menu(bot, chat, role=role_for(db, uid, owner_id)); return
    if value.startswith("new:"):
        kind = value.split(":")[1]
        if kind not in ("income", "expense", "transfer"): return
        d = {"kind": kind, "step": "from_account" if kind == "transfer" else ("stage" if kind == "expense" else "source")}
        if kind != "transfer":
            d["project_id"] = default_project_id(db, uid)
        set_draft(db, uid, d); prompt(bot, db, chat, d, uid); return
    if value == "ops:list":
        show_operations(bot, db, chat, None if manager(db, uid, owner_id) else uid); return
    if value.startswith(("op:view:", "op:edit:", "op:delete:", "op:delconfirm:")):
        try:
            action, _, raw_id = value.split(":", 2)
            if action != "op" or not raw_id.isdigit(): return
            operation_id = int(raw_id)
            row = operation_row(db, operation_id, None if manager(db, uid, owner_id) else uid)
        except (ValueError, TypeError):
            return
        if row is None:
            bot.send(chat, "Операция не найдена в твоём учёте.", [("К списку", "ops:list"), ("Меню", "menu")]); return
        if row["user_id"] != uid and not manager(db, uid, owner_id) and not value.startswith("op:view:"):
            bot.send(chat, "Выдачу денег может менять только редактор."); return
        if row["kind"] == "transfer" and row["from_account_id"] and row["to_account_id"]:
            owners = {r[0] for r in db.execute("SELECT owner_id FROM accounts WHERE id IN (?,?)", (row["from_account_id"], row["to_account_id"]))}
            if len(owners) > 1:
                bot.send(chat, operation_text(db, row) + "\nВыдача денег сохранена. Возврат оформляет редактор обратным переводом через «Выдать деньги».", [("Меню", "menu")]); return
        action = value.split(":", 2)[1]
        if action == "view":
            bot.send(chat, operation_text(db, row), [("Редактировать", f"op:edit:{operation_id}"),
                                                     ("Удалить", f"op:delete:{operation_id}"),
                                                     ("Чеки", f"rc:view:{operation_id}"), ("К списку", "ops:list")]); return
        if action == "edit":
            edit_draft = {"kind": row["kind"], "step": "from_account" if row["kind"] == "transfer" else ("stage" if row["kind"] == "expense" else "source"), "edit_id": operation_id}
            edit_draft["ledger_user_id"] = row["user_id"]
            if row["kind"] != "transfer":
                edit_draft["project_id"] = row["project_id"] or default_project_id(db, row["user_id"])
            set_draft(db, uid, edit_draft)
            bot.send(chat, f"Редактирование операции №{operation_id}. Пройди форму заново и подтверди сохранение; текущая запись изменится на месте.")
            prompt(bot, db, chat, edit_draft, uid); return
        if action == "delete":
            bot.send(chat, f"Удалить операцию №{operation_id}? Это действие нельзя отменить.",
                     [("Да, удалить", f"op:delconfirm:{operation_id}"), ("Назад", f"op:view:{operation_id}")]); return
        if action == "delconfirm":
            with db:
                cursor = db.execute("DELETE FROM operations WHERE id=? AND user_id=?", (operation_id, row["user_id"]))
                if cursor.rowcount:
                    bump_revision(db)
            if cursor.rowcount:
                bot.send(chat, f"Операция №{operation_id} удалена. Отчёты и таблица обновятся.")
            else:
                bot.send(chat, "Операция уже удалена или не найдена.")
            show_operations(bot, db, chat, None if manager(db, uid, owner_id) else uid); return
    if value == "balance":
        b = balances(db, uid)
        bot.send(chat, "Остатки по кассам (с начала учёта):\n" + "\n".join(f"{x[0]}: {money(x[1]['UAH'], 'UAH')}; {money(x[1]['USD'], 'USD')}" for x in b.values()), [("Меню", "menu")]); return
    if value == "report:menu":
        bot.send(chat, "Выбери период. Свой период: /report ГГГГ-ММ-ДД ГГГГ-ММ-ДД", [("Текущий месяц", "rp:month"), ("Весь период", "rp:all")]); return
    if value == "users:list" and manager(db, uid, owner_id):
        from access import show_users
        show_users(bot, db, chat, owner_id); return
    if value == "all:menu" and reader(db, uid, owner_id):
        bot.send(chat, "Общий отчёт всех пользователей:", [("Текущий месяц", "all:month"), ("Весь период", "all:all")]); return
    if value in ("all:month", "all:all") and reader(db, uid, owner_id):
        today = datetime.now(TZ).date().isoformat()
        show_report(bot, db, chat, today[:7] + "-01" if value == "all:month" else None, today, None, None); return
    if value.startswith("rp:"):
        period = value.split(":")[1]
        if period not in ("month", "all"): return
        today = datetime.now(TZ).date().isoformat()
        show_report(bot, db, chat, today[:7]+"-01" if period == "month" else None, today, None, uid); return
    if value.startswith("rproj:"):
        _, period, sid = value.split(":")
        if period not in ("month", "all") or not sid.isdigit(): return
        today = datetime.now(TZ).date().isoformat()
        start = today[:7] + "-01" if period == "month" else None
        pid = int(sid) or None
        if pid and not db.execute("SELECT 1 FROM projects WHERE id=? AND owner_id=?", (pid, uid)).fetchone(): return
        show_report(bot, db, chat, start, today, pid, uid); return
    if value.startswith(("csv:", "csvall:")):
        try:
            parts = value.split(":")
            aggregate = parts[0] == "csvall"
            if aggregate and not reader(db, uid, owner_id): return
            start, end = parts[1:3]
            start = valid_date(start) if start != "0" else None
            end = valid_date(end) if end != "0" else None
            pid = None if aggregate else int(parts[3]) or None
            if pid and not db.execute("SELECT 1 FROM projects WHERE id=? AND owner_id=?", (pid, uid)).fetchone(): return
        except (ValueError, TypeError, IndexError): return
        rows = rows_for(db, start, end, pid, None if aggregate else uid)
        name = "Все объекты" if not pid else db.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()[0]
        bot.document(chat, "stroika_report.csv", csv_report(rows, start, end, name)); return
    if not d:
        bot.send(chat, "Это действие уже завершено. Начни новую запись через /start."); return
    ledger_uid = d.get("ledger_user_id", uid)
    step = d["step"]
    try:
        if value.startswith("proj:") and step == "project":
            pid = int(value.split(":")[1]);
            if pid and not db.execute("SELECT 1 FROM projects WHERE id=? AND owner_id=?", (pid, uid)).fetchone(): return
            d["project_id"] = pid or None
        elif value.startswith("stage:") and step == "stage":
            d["category"] = STAGES[int(value.split(":")[1])]
        elif value.startswith("type:") and step == "cost_type":
            d["cost_type"] = COST_TYPES[int(value.split(":")[1])]
        elif value.startswith("source:") and step == "source":
            d["source"] = SOURCES[int(value.split(":")[1])]
        elif value.startswith("acct:") and step in ("account", "from_account", "to_account"):
            aid = int(value.split(":")[1])
            if not db.execute("SELECT 1 FROM accounts WHERE id=? AND owner_id=?", (aid, ledger_uid)).fetchone(): return
            if step == "to_account" and aid == d.get("from_account_id"):
                bot.send(chat, "Выбери другой счёт."); return
            d[{"account": "account_id", "from_account": "from_account_id", "to_account": "to_account_id"}[step]] = aid
        elif value.startswith("currency:") and step == "currency":
            currency = value.split(":", 1)[1]
            if currency not in CURRENCIES: return
            d["currency"] = currency
        elif value == "date:today" and step == "date":
            d["occurred_on"] = datetime.now(TZ).date().isoformat()
        elif value == "date:custom" and step == "date":
            d["step"] = "date_text"; set_draft(db, uid, d); prompt(bot, db, chat, d); return
        elif value == "comment:skip" and step == "comment":
            d["comment"] = ""
        elif value == "save" and step == "confirm":
            if d.get("edit_id"):
                ident = d["edit_id"]
                update_operation(db, d, ident, ledger_uid)
                clear_draft(db, uid); bot.send(chat, f"Операция №{ident} изменена.")
            else:
                ident = record(db, d, update_id, uid)
                clear_draft(db, uid); bot.send(chat, f"Сохранено. Операция №{ident}.")
            if d['kind'] == 'expense':
                from receipts import begin
                with db:
                    db.execute("UPDATE expense_reviews SET status='pending',reviewer_id=NULL,reviewed_at=NULL WHERE operation_id=?", (ident,))
                begin(bot, db, chat, uid, ident)
                return
            menu(bot, chat, role=role_for(db, uid, owner_id)); return
        else:
            bot.send(chat, "Кнопка устарела. Продолжи текущий шаг:"); prompt(bot, db, chat, d, uid); return
    except (ValueError, IndexError, KeyError):
        bot.send(chat, "Кнопка устарела. Продолжи текущий шаг:"); prompt(bot, db, chat, d, uid); return
    advance(bot, db, chat, uid, d)


def _handle_message(bot, db, chat, uid, text, owner_id=0):
    if text.startswith(("/start", "/menu", "/help")):
        menu(bot, chat, role=role_for(db, uid, owner_id)); return
    if text.startswith("/cancel"):
        clear_draft(db, uid); bot.send(chat, "Отменено."); menu(bot, chat, role=role_for(db, uid, owner_id)); return
    if text.startswith("/adduser ") and manager(db, uid, owner_id):
        match = re.fullmatch(r"/adduser\s+(\d{3,20})\s+([^\n]{1,80})", text)
        if not match or int(match[1]) >= 2**63:
            bot.send(chat, "Формат: /adduser 123456789 Имя"); return
        new_id, name = int(match[1]), match[2].strip()
        if new_id == owner_id and uid != owner_id:
            bot.send(chat, "Владельца может менять только он сам."); return
        if not name:
            bot.send(chat, "Укажи имя."); return
        with db:
            db.execute("INSERT INTO users(id,name,active) VALUES (?,?,1) ON CONFLICT(id) DO UPDATE SET name=excluded.name,active=1,deleted=0", (new_id, name))
            for account in ("Наличные", "Карта", "Счёт"):
                db.execute("INSERT OR IGNORE INTO accounts(owner_id,name) VALUES (?,?)", (new_id, account))
        from access import role_picker
        role_picker(bot, chat, new_id, owner_id)
        bot.send(chat, f"Добавлен: {name} (ID {new_id}). Пусть напишет боту /start."); return
    if text.startswith("/renameuser ") and manager(db, uid, owner_id):
        match = re.fullmatch(r"/renameuser\s+(\d{3,20})\s+([^\n]{1,80})", text)
        if not match or int(match[1]) >= 2**63:
            bot.send(chat, "Формат: /renameuser 123456789 Имя"); return
        target, name = int(match[1]), match[2].strip()
        if target == owner_id and uid != owner_id:
            bot.send(chat, "Владельца может менять только он сам."); return
        if not name:
            bot.send(chat, "Укажи имя."); return
        with db:
            changed = db.execute("UPDATE users SET name=? WHERE id=?", (name, target)).rowcount
        bot.send(chat, f"Имя обновлено: {name}." if changed else "Пользователь не найден."); return
    if text.startswith("/removeuser ") and manager(db, uid, owner_id):
        match = re.fullmatch(r"/removeuser\s+(\d{3,20})", text)
        if not match or int(match[1]) >= 2**63 or int(match[1]) == owner_id:
            bot.send(chat, "Формат: /removeuser 123456789 (владельца отключить нельзя)"); return
        with db:
            changed = db.execute("UPDATE users SET active=0 WHERE id=?", (int(match[1]),)).rowcount
            db.execute("DELETE FROM drafts WHERE user_id=?", (int(match[1]),))
        bot.send(chat, "Доступ отключён. Старые записи сохранены." if changed else "Пользователь не найден."); return
    if text == "/users" and manager(db, uid, owner_id):
        handle_callback(bot, db, chat, uid, 0, "users:list", owner_id); return
    if text == "/operations":
        show_operations(bot, db, chat, None if manager(db, uid, owner_id) else uid); return
    if text.startswith("/allreport ") and reader(db, uid, owner_id):
        parts = text.split()
        try:
            if len(parts) != 3: raise ValueError("Пример: /allreport 2026-09-01 2026-09-30")
            start, end = map(valid_date, parts[1:])
            if end < start: raise ValueError("Конец периода раньше начала.")
        except ValueError as exc:
            bot.send(chat, str(exc)); return
        show_report(bot, db, chat, start, end, None, None); return
    if text.startswith("/project ") or text.startswith("/account "):
        command, name = text.split(" ", 1)
        name = name.strip()
        if not name or len(name) > 80:
            bot.send(chat, "Название: от 1 до 80 символов."); return
        table = "projects" if command == "/project" else "accounts"
        with db:
            db.execute(f"INSERT OR IGNORE INTO {table}(owner_id,name) VALUES (?,?)", (uid, name))
        bot.send(chat, "Добавлено: " + name)
        d = draft(db, uid)
        if d and d["step"] in ("project", "account", "from_account", "to_account"): prompt(bot, db, chat, d, uid)
        return
    if text.startswith("/report "):
        parts = text.split()
        if len(parts) != 3:
            bot.send(chat, "Пример: /report 2026-09-01 2026-09-30"); return
        try:
            start, end = map(valid_date, parts[1:])
            if end < start: raise ValueError("Конец периода раньше начала.")
        except ValueError as exc:
            bot.send(chat, str(exc)); return
        show_report(bot, db, chat, start, end, None, uid); return
    d = draft(db, uid)
    if not d:
        bot.send(chat, "Нажми /start, чтобы открыть меню."); return
    try:
        if d["step"] == "amount":
            d["amount_kop"] = parse_amount(text)
        elif d["step"] == "date_text":
            d["occurred_on"] = valid_date(text.strip())
        elif d["step"] == "comment":
            if len(text) > 500: raise ValueError("Комментарий до 500 символов.")
            d["comment"] = text.strip()
        else:
            bot.send(chat, "Выбери вариант кнопкой."); prompt(bot, db, chat, d, uid); return
    except ValueError as exc:
        bot.send(chat, str(exc)); return
    advance(bot, db, chat, uid, d)


def handle_callback(bot, db, chat, uid, update_id, value, owner_id=0):
    from access import callback, permitted
    if not permitted(bot, db, chat, uid, owner_id, value, False): return
    from receipts import callback as receipt_callback
    if receipt_callback(bot, db, chat, uid, value, owner_id): return
    from transfer_edit import callback as transfer_callback
    if transfer_callback(bot, db, chat, uid, value, owner_id): return
    from participants import callback as participant_callback
    if participant_callback(bot, db, chat, uid, value, owner_id): return
    from reports import callback as report_callback
    if report_callback(bot, db, chat, uid, value, owner_id): return
    if callback(bot, db, chat, uid, update_id, value, owner_id): return
    _handle_callback(bot, db, chat, uid, update_id, value, owner_id)


def handle_message(bot, db, chat, uid, text, owner_id=0):
    from access import message, permitted
    if not permitted(bot, db, chat, uid, owner_id, text, True): return
    from receipts import message as receipt_message
    if receipt_message(bot, db, chat, uid, text, owner_id): return
    from transfer_edit import message as transfer_message
    if transfer_message(bot, db, chat, uid, text, owner_id): return
    from participants import message as participant_message
    if participant_message(bot, db, chat, uid, text, owner_id): return
    if message(bot, db, chat, uid, text, owner_id): return
    _handle_message(bot, db, chat, uid, text, owner_id)


def run(bot, db, owner_id, sync=None):
    saved = db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()
    offset = int(saved[0]) if saved else 0
    if sync:
        sync(db)
    while True:
        try:
            updates = bot.call("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]})
            for update in updates:
                uid = (update.get("message") or update.get("callback_query") or {}).get("from", {}).get("id")
                payload = update.get("message") or update.get("callback_query") or {}
                chat = (payload.get("chat") or (payload.get("message") or {}).get("chat") or {}).get("id")
                if uid is not None and chat == uid:
                    from participants import remember
                    remember(db, payload.get("from", {}))
                active = uid is not None and db.execute("SELECT 1 FROM users WHERE id=? AND active=1 AND deleted=0", (uid,)).fetchone()
                if active and chat == uid:  # private chat only
                    try:
                        if "callback_query" in update:
                            bot.call("answerCallbackQuery", {"callback_query_id": payload["id"]})
                            handle_callback(bot, db, chat, uid, update["update_id"], payload.get("data", ""), owner_id)
                        elif "photo" in payload or "document" in payload:
                            from receipts import media
                            media(bot, db, chat, uid, payload, update["update_id"], owner_id)
                        elif "text" in payload:
                            handle_message(bot, db, chat, uid, payload["text"].strip(), owner_id)
                        if sync:
                            sync(db)
                    except Exception:
                        logging.exception("Update %s failed", update["update_id"])
                        continue  # retry this update rather than silently lose a financial record
                elif chat == uid and "text" in payload and payload["text"].strip().startswith("/start"):
                    bot.send(chat, f"Доступ закрыт. Твой Telegram ID: {uid}. Передай владельцу свой @ник (или этот ID, если ника нет). Он добавит тебя через «Пользователи».")
                offset = update["update_id"] + 1
                with db:
                    db.execute("INSERT INTO settings(key,value) VALUES ('offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))
            if sync:
                sync(db)
        except (urllib.error.URLError, TimeoutError, RuntimeError):
            logging.exception("Telegram connection error")
            time.sleep(5)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    raw_users = os.environ.get("ALLOWED_USER_IDS", "")
    try: allowed = [int(x.strip()) for x in raw_users.split(",") if x.strip()]
    except ValueError: raise SystemExit("ALLOWED_USER_IDS: comma separated numeric Telegram IDs")
    if not token or not allowed:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS before starting")
    owner_id = int(os.environ.get("OWNER_USER_ID") or allowed[0])
    path = Path(os.environ.get("BOT_DB_PATH", "./data/ledger.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = connect(path, owner_id, allowed)
    sync = None
    if os.environ.get("GOOGLE_SHEET_ID"):
        from sheets_sync import build_sync
        sync = build_sync(os.environ["GOOGLE_SHEET_ID"], os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""))
    run(Telegram(token), db, owner_id, sync)


if __name__ == "__main__":
    main()
