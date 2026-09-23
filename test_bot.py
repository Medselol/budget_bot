import sqlite3
import tempfile
import unittest
from pathlib import Path

import bot


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = bot.connect(Path(self.temp.name) / "test.sqlite3", 123)
        self.db.execute("INSERT INTO projects(owner_id,name) VALUES (123,'Дуплекс №1')")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add(self, kind, amount, update_id, **kwargs):
        data = {"occurred_on": "2026-09-23", "kind": kind, "amount_kop": amount, "project_id": 1, "comment": ""}
        data.update(kwargs)
        return bot.record(self.db, data, update_id, 123)

    def test_financing_sales_transfers_and_balance(self):
        self.add("income", 1_000_000, 1, source="Личные средства", account_id=1)
        self.add("income", 300_000, 2, source="Аванс покупателя", account_id=2)
        self.add("expense", 400_000, 3, category="Фундамент", cost_type="Материалы", account_id=1)
        self.add("transfer", 200_000, 4, project_id=None, from_account_id=1, to_account_id=2)
        total = bot.summary(bot.rows_for(self.db, project_id=1))
        self.assertEqual((total["UAH"]["financing"], total["UAH"]["sales"], total["UAH"]["expense"], total["UAH"]["cash_net"]), (1_000_000, 300_000, 400_000, 900_000))
        balances = bot.balances(self.db, 123)
        self.assertEqual((balances[1][1]["UAH"], balances[2][1]["UAH"]), (400_000, 500_000))

    def test_date_boundaries_and_retry_are_exact(self):
        ident = self.add("expense", 5000, 10, category="Стены", cost_type="Работа", account_id=1)
        self.assertEqual(ident, self.add("expense", 5000, 10, category="Стены", cost_type="Работа", account_id=1))
        self.assertEqual(len(bot.rows_for(self.db, "2026-09-23", "2026-09-23")), 1)
        self.assertEqual(len(bot.rows_for(self.db, "2026-09-24", "2026-09-30")), 0)

    def test_csv_protects_formula_like_text_and_amount_parsing(self):
        self.add("expense", 125050, 15, category="Фундамент", cost_type="Доставка", account_id=1, comment="=1+1")
        csv_data = bot.csv_report(bot.rows_for(self.db), None, None, "Все объекты")
        self.assertTrue(csv_data.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"'=1+1", csv_data)
        self.assertEqual(bot.parse_amount("1 250,50"), 125050)
        with self.assertRaises(ValueError): bot.parse_amount("-5")

    def test_button_flow_records_expense_only_after_confirmation(self):
        class FakeBot:
            def __init__(self): self.messages = []
            def send(self, chat, text, options=None): self.messages.append((chat, text, options))
        api = FakeBot()
        user = 123
        bot.handle_callback(api, self.db, user, user, 1, "new:expense")
        for number, button in enumerate(("proj:1", "stage:2", "type:0", "acct:1", "currency:USD"), 2):
            bot.handle_callback(api, self.db, user, user, number, button)
        bot.handle_message(api, self.db, user, user, "1 250,50")
        bot.handle_callback(api, self.db, user, user, 7, "date:today")
        bot.handle_callback(api, self.db, user, user, 8, "comment:skip")
        self.assertEqual(len(bot.rows_for(self.db)), 0)
        self.assertIn("USD", api.messages[-1][1])
        bot.handle_callback(api, self.db, user, user, 9, "save")
        self.assertEqual(len(bot.rows_for(self.db)), 1)
        self.assertEqual(bot.rows_for(self.db)[0]["amount_kop"], 125050)
        self.assertEqual(bot.rows_for(self.db)[0]["currency"], "USD")

    def test_currencies_are_separate_in_report_balances_and_csv(self):
        self.add("income", 500_00, 20, source="Личные средства", account_id=1, currency="USD")
        self.add("expense", 200_00, 21, category="Фундамент", cost_type="Материалы", account_id=1, currency="USD")
        self.add("income", 1000_00, 22, source="Личные средства", account_id=1, currency="UAH")
        self.add("transfer", 100_00, 23, project_id=None, from_account_id=1, to_account_id=2, currency="USD")
        totals = bot.summary(bot.rows_for(self.db))
        self.assertEqual((totals["USD"]["cash_net"], totals["UAH"]["cash_net"]), (300_00, 1000_00))
        self.assertEqual(bot.balances(self.db, 123)[1][1], {"UAH": 1000_00, "USD": 200_00})
        report, rows, _ = bot.report_text(self.db, None, None, None)
        self.assertIn("300.00 USD", report)
        self.assertIn("1 000.00 грн", report)
        self.assertIn("Валюта", bot.csv_report(rows, None, None, "Все объекты").decode("utf-8-sig"))

    def test_old_database_migrates_transactions_to_uah(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy) as db:
            db.execute("""CREATE TABLE operations (id INTEGER PRIMARY KEY, update_id INTEGER UNIQUE NOT NULL,
                occurred_on TEXT NOT NULL, kind TEXT NOT NULL, project_id INTEGER, category TEXT,
                cost_type TEXT, source TEXT, account_id INTEGER, from_account_id INTEGER,
                to_account_id INTEGER, amount_kop INTEGER NOT NULL, comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("INSERT INTO operations (update_id, occurred_on, kind, account_id, amount_kop) VALUES (31, '2026-09-20', 'income', 1, 25000)")
        migrated = bot.connect(legacy, 123)
        try:
            self.assertEqual(migrated.execute("SELECT currency,amount_kop FROM operations WHERE update_id=31").fetchone()[:], ("UAH", 25000))
            self.assertEqual(bot.balances(migrated, 123)[1][1]["UAH"], 25000)
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
