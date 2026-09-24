import unittest
import test_controls as fixtures
import bot
import receipts


class ReviewFlowTests(unittest.TestCase):
    setUp=fixtures.ControlTests.setUp
    tearDown=fixtures.ControlTests.tearDown
    cb=fixtures.ControlTests.cb

    def expense(self,update=1000,occurred_on='2026-09-25',uid=456):
        account=bot.names(self.db,'accounts',uid)[0]['id']
        return bot.record(self.db,dict(kind='expense',occurred_on=occurred_on,account_id=account,amount_kop=345000,currency='UAH',comment='Материалы'),update,uid)

    def routes(self): return [value for _,value in self.api.messages[-1][2]]

    def test_approve_result_and_idempotent_old_button(self):
        ident=self.expense();before=bot.balances(self.db,456)
        self.cb(f'rc:view:{ident}')
        self.assertIn(f'rc:approve:{ident}',self.routes())
        self.assertNotIn(f'rc:pending:{ident}',self.routes())
        self.api.messages.clear();self.cb(f'rc:approve:{ident}')
        self.assertEqual(len(self.api.messages),1)
        self.assertIn(f'✅ Расход №{ident} проверен.',self.api.messages[-1][1])
        self.assertNotIn('Операция №',self.api.messages[-1][1])
        self.assertNotIn('rc:next',self.routes())
        original=dict(self.db.execute('SELECT * FROM expense_reviews WHERE operation_id=?',(ident,)).fetchone())
        audits=self.db.execute("SELECT COUNT(*) FROM audit_log WHERE entity='expense_reviews'").fetchone()[0]
        self.cb(f'rc:approve:{ident}',987)
        self.assertIn('уже проверен',self.api.messages[-1][1])
        self.assertEqual(dict(self.db.execute('SELECT * FROM expense_reviews WHERE operation_id=?',(ident,)).fetchone()),original)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM audit_log WHERE entity='expense_reviews'").fetchone()[0],audits)
        self.assertEqual(bot.balances(self.db,456),before)
        self.cb(f'rc:view:{ident}')
        self.assertNotIn(f'rc:approve:{ident}',self.routes())
        self.assertIn(f'rc:pending:{ident}',self.routes())

    def test_next_expense_respects_filters_and_rechecks_live_queue(self):
        ident=self.expense();other=self.expense(1001);third=self.expense(1002)
        self.expense(1003,'2026-08-01');self.expense(1004,uid=123)
        bot.set_draft(self.db,123,dict(step='receipt_filter',review_month='2026-09',review_user=456))
        self.cb(f'rc:approve:{ident}')
        self.assertIn('осталось: 2',self.api.messages[-1][1]);self.assertIn('rc:next',self.routes())
        self.cb(f'rc:approve:{third}',987)
        self.cb('rc:next')
        self.assertIn(f'Операция №{other}\n',self.api.messages[-1][1])
        self.cb(f'rc:approve:{other}')
        self.assertNotIn('rc:next',self.routes())
        self.cb('rc:next');self.assertIn('больше нет',self.api.messages[-1][1])
        self.assertEqual(receipts.pending_summary(self.db,123),(0,None))

    def test_reopen_once_restores_approve_button_and_notices(self):
        ident=self.expense();self.cb(f'rc:approve:{ident}')
        self.cb(f'rc:pending:{ident}')
        self.assertIn('возвращён на проверку',self.api.messages[-1][1])
        events=self.db.execute('SELECT COUNT(*) FROM expense_alert_events').fetchone()[0]
        self.cb(f'rc:pending:{ident}',987)
        self.assertIn('уже находится на проверке',self.api.messages[-1][1])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM expense_alert_events').fetchone()[0],events)
        self.cb(f'rc:view:{ident}')
        self.assertIn(f'rc:approve:{ident}',self.routes());self.assertNotIn(f'rc:pending:{ident}',self.routes())

    def test_non_managers_cannot_review_or_open_next_queue(self):
        ident=self.expense()
        for uid in (456,789):
            self.cb(f'rc:approve:{ident}',uid)
            self.assertEqual(receipts.metadata(self.db,ident)[0],'pending')
            self.assertNotIn(f'rc:approve:{ident}',self.routes())
            self.api.messages.clear();self.cb('rc:next',uid);self.assertFalse(self.api.messages)
            with self.assertRaises(ValueError): receipts.set_review_status(self.db,uid,123,ident,'approved')
