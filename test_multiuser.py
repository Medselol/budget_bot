import sqlite3
import tempfile
import unittest
from pathlib import Path

import bot
import sheets_sync


class FakeBot:
    def __init__(self):
        self.messages = []
        self.documents = []
        self.pdf_documents = []

    def send(self, chat, text, options=None):
        self.messages.append((chat, text, options))

    def document(self, chat, name, content):
        if name.endswith(".pdf"):
            self.pdf_documents.append((chat, content))
        else:
            self.documents.append((chat, content.decode("utf-8-sig")))


class MultiuserTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = bot.connect(Path(self.folder.name) / "db.sqlite3", 123)
        self.api = FakeBot()

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()

    def test_private_views_and_admin_total(self):
        bot.handle_message(self.api, self.db, 123, 123, "/adduser 456 Сотрудник", 123)
        bot.handle_message(self.api, self.db, 123, 123, "/project Дуплекс", 123)
        bot.handle_message(self.api, self.db, 456, 456, "/project Дуплекс", 123)
        self.assertEqual(len(self.db.execute("SELECT id FROM projects WHERE name='Дуплекс'").fetchall()), 2)
        own = self.db.execute("SELECT id FROM projects WHERE owner_id=123").fetchone()[0]
        other = self.db.execute("SELECT id FROM projects WHERE owner_id=456").fetchone()[0]
        bot.record(self.db, {"occurred_on": "2026-09-23", "kind": "expense", "project_id": own, "category": "Стены", "cost_type": "Работа", "account_id": 1, "amount_kop": 10000, "currency": "USD"}, 20, 123)
        second_account = self.db.execute("SELECT id FROM accounts WHERE owner_id=456 LIMIT 1").fetchone()[0]
        bot.record(self.db, {"occurred_on": "2026-09-23", "kind": "expense", "project_id": other, "category": "Стены", "cost_type": "Работа", "account_id": second_account, "amount_kop": 30000, "currency": "USD"}, 21, 456)
        self.assertEqual(len(bot.rows_for(self.db, user_id=456)), 1)
        self.assertEqual(bot.balances(self.db, 456)[second_account][1]["USD"], -30000)
        with self.assertRaises(ValueError):
            bot.record(self.db, {"occurred_on": "2026-09-23", "kind": "expense", "project_id": own, "account_id": second_account, "amount_kop": 100}, 22, 456)
        bot.handle_callback(self.api, self.db, 456, 456, 23, f"csv:0:0:{own}", 123)
        self.assertEqual(len(self.api.documents), 0)
        bot.handle_callback(self.api, self.db, 456, 456, 24, "csvall:0:0", 123)
        self.assertEqual(len(self.api.documents), 0)
        bot.handle_message(self.api, self.db, 456, 456, "/report 2026-09-01 2026-09-30", 123)
        self.assertTrue(self.api.pdf_documents[-1][1].startswith(b"%PDF-"))
        pdf_count = len(self.api.pdf_documents)
        bot.handle_callback(self.api, self.db, 456, 456, 24, "all:all", 123)
        self.assertEqual(len(self.api.pdf_documents), pdf_count)
        bot.handle_message(self.api, self.db, 456, 456, "/report 2026-09-01 2026-09-30", 123)
        self.assertIn("300.00 USD", self.api.messages[-1][1])
        self.assertNotIn("400.00 USD", self.api.messages[-1][1])
        bot.handle_callback(self.api, self.db, 123, 123, 25, "all:all", 123)
        self.assertIn("400.00 USD", self.api.messages[-1][1])
        self.assertIn("Сотрудник", self.api.messages[-1][1])
        bot.handle_callback(self.api, self.db, 123, 123, 26, "csvall:0:0", 123)
        self.assertEqual(len(self.api.documents), 1)
        self.assertIn("Сотрудник", self.api.documents[-1][1])
        bot.handle_message(self.api, self.db, 123, 123, "/removeuser 456", 123)
        self.assertEqual(len(bot.rows_for(self.db, user_id=456)), 1)
        self.assertEqual(self.db.execute("SELECT active FROM users WHERE id=456").fetchone()[0], 0)

    def test_legacy_relations_and_currency_survive_migration(self):
        old = Path(self.folder.name) / "old.sqlite3"
        with sqlite3.connect(old) as db:
            db.executescript("""CREATE TABLE projects(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL);
                CREATE TABLE accounts(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL);
                CREATE TABLE operations(id INTEGER PRIMARY KEY,update_id INTEGER UNIQUE NOT NULL,
                    occurred_on TEXT NOT NULL,kind TEXT NOT NULL,project_id INTEGER REFERENCES projects(id),
                    category TEXT,cost_type TEXT,source TEXT,
                    account_id INTEGER REFERENCES accounts(id),from_account_id INTEGER REFERENCES accounts(id),
                    to_account_id INTEGER REFERENCES accounts(id),amount_kop INTEGER NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'UAH',comment TEXT NOT NULL DEFAULT '');
                INSERT INTO projects VALUES (7,'Дуплекс'); INSERT INTO accounts VALUES (9,'Карта');
                INSERT INTO operations(update_id,occurred_on,kind,project_id,account_id,amount_kop)
                    VALUES (99,'2026-09-23','income',7,9,12345);""")
        migrated = bot.connect(old, 123)
        try:
            row = bot.rows_for(migrated, user_id=123)[0]
            self.assertEqual((row["user_id"], row["currency"], row["project"], row["account"]), (123, "UAH", "Дуплекс", "Карта"))
            self.assertFalse(migrated.execute("PRAGMA foreign_key_check").fetchall())
            bot.handle_message(self.api, migrated, 123, 123, "/adduser 456 Сотрудник", 123)
            bot.handle_message(self.api, migrated, 456, 456, "/project Дуплекс", 123)
            self.assertEqual(len(migrated.execute("SELECT id FROM projects WHERE name='Дуплекс'").fetchall()), 2)
        finally:
            migrated.close()
        reopened = bot.connect(old, 123)
        try:
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 1)
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM projects WHERE name='Дуплекс'").fetchone()[0], 2)
        finally:
            reopened.close()

    def test_runtime_blocks_uninvited_and_disabled_ids(self):
        bot.handle_message(self.api, self.db, 123, 123, "/adduser 456 Сотрудник", 123)
        bot.handle_message(self.api, self.db, 123, 123, "/removeuser 456", 123)

        class PollBot(FakeBot):
            def __init__(self):
                super().__init__()
                self.polls = 0

            def call(self, method, payload):
                if method == "getUpdates":
                    self.polls += 1
                    if self.polls > 1: raise KeyboardInterrupt()
                    return [
                        {"update_id": 100, "message": {"from": {"id": 456}, "chat": {"id": 456}, "text": "/project Секрет"}},
                        {"update_id": 101, "message": {"from": {"id": 789}, "chat": {"id": 789}, "text": "/start"}},
                    ]
                return True

        api = PollBot()
        with self.assertRaises(KeyboardInterrupt):
            bot.run(api, self.db, 123)
        self.assertFalse(self.db.execute("SELECT 1 FROM projects WHERE name='Секрет'").fetchone())
        self.assertEqual(len(api.messages), 1)
        self.assertIn("789", api.messages[0][1])

    def test_owner_can_name_self_and_add_named_user(self):
        bot.handle_message(self.api, self.db, 123, 123, "/renameuser 123 Денис", 123)
        bot.handle_message(self.api, self.db, 123, 123, "/adduser 456 Игорь", 123)
        users = {r["id"]: r["name"] for r in self.db.execute("SELECT id,name FROM users")}
        self.assertEqual(users, {123: "Денис", 456: "Игорь"})

    def test_single_default_project_skips_picker_for_each_user(self):
        project = bot.default_project_id(self.db, 123)
        self.assertEqual(self.db.execute("SELECT name FROM projects WHERE id=?", (project,)).fetchone()[0], "Вита-Почтовая Дуплексы")
        bot.handle_callback(self.api, self.db, 123, 123, 41, "new:expense", 123)
        self.assertEqual(bot.draft(self.db, 123)["step"], "stage")
        self.assertEqual(bot.draft(self.db, 123)["project_id"], project)
        self.assertNotIn("Выбери объект", self.api.messages[-1][1])
        bot.handle_message(self.api, self.db, 123, 123, "/adduser 456 Игорь", 123)
        self.assertEqual(self.db.execute("SELECT name FROM users WHERE id=123").fetchone()[0], "Денис")
        self.assertEqual(bot.default_project_id(self.db, 456), self.db.execute("SELECT id FROM projects WHERE owner_id=456 AND name=?", ("Вита-Почтовая Дуплексы",)).fetchone()[0])

    def test_edit_and_confirmed_delete_operation_in_bot(self):
        operation_id = bot.record(self.db, {"occurred_on": "2026-09-23", "kind": "expense", "project_id": None,
                                            "category": "Стены", "cost_type": "Работа", "account_id": 1,
                                            "amount_kop": 10000, "currency": "UAH", "comment": "старое"}, 30, 123)
        revision_before = int(self.db.execute("SELECT value FROM settings WHERE key='ledger_revision'").fetchone()[0])
        bot.handle_callback(self.api, self.db, 123, 123, 31, f"op:edit:{operation_id}", 123)
        self.assertEqual(bot.draft(self.db, 123)["step"], "stage")
        self.assertEqual(bot.draft(self.db, 123)["project_id"], bot.default_project_id(self.db, 123))
        bot.handle_callback(self.api, self.db, 123, 123, 33, "stage:0", 123)
        bot.handle_callback(self.api, self.db, 123, 123, 34, "type:0", 123)
        bot.handle_callback(self.api, self.db, 123, 123, 35, "acct:1", 123)
        bot.handle_callback(self.api, self.db, 123, 123, 36, "currency:USD", 123)
        bot.handle_message(self.api, self.db, 123, 123, "25.50", 123)
        bot.handle_callback(self.api, self.db, 123, 123, 37, "date:custom", 123)
        bot.handle_message(self.api, self.db, 123, 123, "2026-09-24", 123)
        bot.handle_message(self.api, self.db, 123, 123, "исправлено", 123)
        bot.handle_callback(self.api, self.db, 123, 123, 38, "save", 123)
        row = bot.operation_row(self.db, operation_id, 123)
        self.assertEqual((row["amount_kop"], row["currency"], row["occurred_on"], row["comment"]), (2550, "USD", "2026-09-24", "исправлено"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 1)
        self.assertEqual(int(self.db.execute("SELECT value FROM settings WHERE key='ledger_revision'").fetchone()[0]), revision_before + 1)
        with self.assertRaises(ValueError):
            bot.update_operation(self.db, {"kind": "expense", "occurred_on": "2026-09-24", "amount_kop": 100,
                                           "currency": "UAH", "account_id": 1}, operation_id, 456)
        bot.handle_callback(self.api, self.db, 123, 123, 39, f"op:delete:{operation_id}", 123)
        self.assertIn("нельзя отменить", self.api.messages[-1][1])
        bot.handle_callback(self.api, self.db, 123, 123, 40, f"op:delconfirm:{operation_id}", 123)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)


class SheetsTests(unittest.TestCase):
    def test_snapshot_has_user_totals_and_separate_currency(self):
        with tempfile.TemporaryDirectory() as folder:
            db = bot.connect(Path(folder) / "db.sqlite3", 123, (456,))
            try:
                bot.record(db, {"occurred_on": "2026-09-23", "kind": "income", "account_id": 1, "amount_kop": 50000, "currency": "USD"}, 31, 123)
                other = db.execute("SELECT id FROM accounts WHERE owner_id=456 LIMIT 1").fetchone()[0]
                bot.record(db, {"occurred_on": "2026-09-23", "kind": "expense", "account_id": other, "amount_kop": 20000, "currency": "UAH"}, 32, 456)
                operations, totals = sheets_sync.snapshot(db)
                self.assertEqual(len(operations), 3)
                self.assertEqual(operations[1][13], 500.0)
                self.assertEqual([r[5] for r in totals if r[1] == "ВСЕ ПОЛЬЗОВАТЕЛИ"], [-200.0, 500.0])

                class Session:
                    def __init__(self):
                        self.calls = []; self.fail_once = True
                        self.sheets = [{"properties": {"sheetId": 1, "title": "Лист1", "gridProperties": {}}}]
                    def request(self, method, url, json=None, timeout=None):
                        self.calls.append((method, url, json))
                        session = self
                        class Response:
                            def raise_for_status(self):
                                if session.fail_once and url.endswith("/values:batchUpdate"):
                                    session.fail_once = False
                                    raise RuntimeError("temporary")
                            def json(self):
                                if method == "GET":
                                    return {"sheets": session.sheets}
                                if url.endswith(":batchUpdate"):
                                    for request in json.get("requests", []):
                                        if "addSheet" in request:
                                            p = request["addSheet"]["properties"]
                                            session.sheets.append({"properties": {"sheetId": len(session.sheets) + 1,
                                                                                   "title": p["title"],
                                                                                   "gridProperties": {}}})
                                        elif "updateSheetProperties" in request:
                                            p = request["updateSheetProperties"]["properties"]
                                            next(s for s in session.sheets if s["properties"]["sheetId"] == p["sheetId"])["properties"].update(p)
                                    return {}
                                return {}
                        return Response()
                session = Session()
                sync = sheets_sync.SheetsSync("sheet-id", session)
                with self.assertLogs(level="ERROR"):
                    self.assertFalse(sync(db))
                self.assertTrue(sync(db))
                self.assertTrue(sync(db))
                self.assertEqual(sum(url.endswith("/values:batchUpdate") for _, url, _ in session.calls), 2)
                value_update = next(body for _, url, body in reversed(session.calls) if url.endswith("/values:batchUpdate"))
                self.assertEqual(value_update["valueInputOption"], "RAW")
                self.assertEqual({s["properties"]["title"] for s in session.sheets}, {"Инструкция", "Операции", "Итоги"})
                self.assertTrue(any("addBanding" in req for _, url, body in session.calls
                                    if url.endswith(":batchUpdate") and "requests" in body for req in body["requests"]))
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
