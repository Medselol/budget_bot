from datetime import date
import unittest
import bot
import reports
import test_access

class ReportTests(unittest.TestCase):
    setUp = test_access.AccessTests.setUp
    tearDown = test_access.AccessTests.tearDown
    account = test_access.AccessTests.account
    cb = test_access.AccessTests.cb
    msg = test_access.AccessTests.msg
    seed = test_access.AccessTests.seed
    fund = test_access.AccessTests.fund
    def test_calendar_boundaries(self):
        d=date(2026,1,1)
        self.assertEqual(reports.period_dates('year',d),('2026-01-01','2026-01-01'))
        self.assertEqual(reports.period_dates('month',date(2024,2,29)),('2024-02-01','2024-02-29'))
        self.assertEqual(reports.period_dates('week',d),('2025-12-29','2026-01-01'))
        self.assertEqual(reports.period_dates('day',d),('2026-01-01','2026-01-01'))
    def test_investor_views_any_participant_and_pdf_without_edit_controls(self):
        self.seed();self.fund()
        self.cb(789,'all:menu')
        labels=[x[0] for x in self.api.messages[-1][2]]
        self.assertTrue({'За год','За месяц','За неделю','За день'}.issubset(labels))
        self.cb(789,'overview:period:all')
        self.assertIn('overview:who:all:456',[x[1] for x in self.api.messages[-1][2]])
        self.cb(789,'overview:who:all:456')
        self.assertTrue(self.api.pdf_documents[-1][1].startswith(b'%PDF-'))
        self.assertIn('Участник:',self.api.messages[-1][1])
        csv_button=self.api.messages[-1][2][0][1]
        self.cb(789,csv_button)
        self.assertIn('transfer',self.api.documents[-1][1])
        self.assertNotIn(';income;',self.api.documents[-1][1])
        self.cb(789,'people:view:456')
        self.assertIn('400.00 грн',self.api.messages[-2][1])
        self.cb(789,'people:ops:456:0')
        op_button=self.api.messages[-1][2][0][1]
        self.cb(789,op_button)
        self.assertNotIn('op:view:',str(self.api.messages[-1][2]))
        self.cb(789,'team:balances');self.assertIn('600.00 грн',self.api.messages[-1][1])
    def test_foreman_income_full_flow_and_balance_menu(self):
        self.msg(456,'/start')
        self.assertEqual({x[0] for x in self.api.messages[-1][2]},{'Приход','Расход','Мой баланс'})
        for v in ('new:income','source:0',f'acct:{self.b}','currency:UAH'):self.cb(456,v)
        self.msg(456,'100');self.cb(456,'date:today');self.cb(456,'comment:skip');self.cb(456,'save',71)
        self.assertEqual(bot.balances(self.db,456)[self.b][1]['UAH'],10000)
        self.assertEqual(bot.rows_for(self.db)[0]['kind'],'income')
        self.cb(456,'balance')
        self.assertIn('100.00 грн',self.api.messages[-1][1])
        self.cb(456,'overview:who:all:123');self.cb(456,'csvuser:123:0:0');self.cb(456,'people:view:123')
        self.assertFalse(self.api.pdf_documents);self.assertFalse(self.api.documents)
    def test_investor_cannot_mutate_via_new_views(self):
        self.seed()
        for v in ('people:op:123:1','op:delconfirm:1','op:edit:1','save','fund:save','role:set:789:editor'):
            self.cb(789,v)
        self.assertEqual(len(bot.rows_for(self.db)),1)
        self.assertEqual(bot.role_for(self.db,789),'investor')
    def test_editor_personal_view_excludes_transfers_only_authored_by_them(self):
        self.seed();self.fund()
        self.assertEqual(len(bot.rows_for(self.db,user_id=987)),0)
        for uid in (123,987):
            self.cb(uid,'overview:who:all:456');self.assertTrue(self.api.pdf_documents[-1][1].startswith(b'%PDF-'))
    def test_invalid_participant_does_not_export(self):
        self.cb(789,'overview:who:all:999999');self.cb(789,'csvuser:999999:0:0')
        self.assertFalse(self.api.pdf_documents);self.assertFalse(self.api.documents)

if __name__=='__main__':unittest.main()
