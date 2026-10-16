"""Cashbox UI tests use isolated memory databases, never live transactions."""
import unittest
import bot
import access
from test_multiuser import FakeBot

class FundingPickerTests(unittest.TestCase):
    def setUp(self):
        self.db = bot.connect(':memory:', 123, (456, 789, 987))
        self.api = FakeBot()
        for uid, name in ((123,'Денис'), (456,'Игорь'), (789,'Татьяна'), (987,'Тестовый')):
            self.db.execute('UPDATE users SET name=? WHERE id=?', (name, uid))
        self.db.commit()
    def tearDown(self): self.db.close()
    def cb(self, value, uid=123): bot.handle_callback(self.api,self.db,uid,uid,100,value,123)
    def state(self): return bot.draft(self.db,123)
    def options(self): return self.api.messages[-1][2]
    def aid(self, uid, name='Наличные'):
        return self.db.execute('SELECT id FROM accounts WHERE owner_id=? AND name=?',(uid,name)).fetchone()[0]
    def pick_from(self):
        self.cb('fund:menu'); self.cb('fund:person:from:123'); self.cb(f'fund:box:from:123:{self.aid(123)}')
    def test_four_people_then_three_distinct_cashboxes(self):
        self.cb('fund:menu')
        people = [x for x in self.options() if x[1].startswith('fund:person:')]
        self.assertEqual(len(people), 4)
        self.assertEqual({x[0] for x in people}, {'👤 Денис','👤 Игорь','👤 Татьяна','👤 Тестовый'})
        self.cb('fund:person:from:123')
        labels = [x[0] for x in self.options() if x[1].startswith('fund:box:')]
        self.assertEqual(labels, ['💵 Наличные','💳 Карта','🏦 Счёт'])
        rendered = bot.keyboard(self.options())
        self.assertIn('💳 Карта', str(rendered)); self.assertNotIn('👛 💳', str(rendered))
        self.assertIn(('Отмена','cancel'),self.options())
        self.assertIn(('← Назад','nav:back'),self.options())
    def test_sender_recipient_and_repeat_buttons_are_scoped(self):
        self.pick_from(); before=self.state()
        self.cb(f'fund:box:from:123:{self.aid(123)}'); self.cb('fund:person:from:456')
        self.assertEqual(self.state(),before)
        self.cb('fund:person:to:456'); before=self.state()
        self.cb(f'fund:box:to:123:{self.aid(123)}')
        self.cb(f'fund:box:to:456:{self.aid(789)}')
        self.assertEqual(self.state(),before)
        self.cb(f'fund:box:to:456:{self.aid(456)}'); before=self.state()
        self.cb(f'fund:box:to:456:{self.aid(456)}')
        self.assertEqual(self.state(),before)
        self.assertEqual(before['step'],'fund_currency')
        self.assertFalse(bot.rows_for(self.db))
    def test_same_cashbox_excluded_but_own_other_cashbox_kept(self):
        self.pick_from(); self.cb('fund:person:to:123')
        self.assertEqual(len([v for _,v in self.options() if v.startswith('fund:box:')]),2)
        before=self.state(); self.cb(f'fund:box:to:123:{self.aid(123)}')
        self.assertEqual(self.state(),before)
        self.cb(f"fund:box:to:123:{self.aid(123,'Карта')}")
        self.assertEqual(self.state()['step'],'fund_currency')
    def test_back_and_cancel_never_write_operations(self):
        self.cb('menu'); self.pick_from(); self.cb('fund:person:to:456')
        for expected in ('fund_to','fund_from_account','fund_from'):
            self.cb('nav:back'); self.assertEqual(self.state()['step'],expected)
        self.cb('fund:person:from:987'); self.cb('cancel')
        self.assertIsNone(self.state())
        self.cb(f'fund:box:from:987:{self.aid(987)}'); self.cb('fund:save'); self.cb('nav:back')
        self.assertIsNone(self.state()); self.assertFalse(bot.rows_for(self.db))
    def test_acl_filters_and_rechecks_disabled_participants(self):
        self.db.execute("UPDATE users SET role='investor' WHERE id=789")
        self.db.execute('UPDATE users SET active=0 WHERE id=987'); self.db.commit()
        self.cb('fund:menu')
        self.assertEqual(len([v for _,v in self.options() if v.startswith('fund:person:')]),2)
        before=self.state(); self.cb('fund:person:from:789'); self.cb('fund:person:from:987')
        self.assertEqual(self.state(),before)
        self.cb('fund:person:from:456')
        self.db.execute('UPDATE users SET active=0 WHERE id=456'); self.db.commit()
        before=self.state(); self.cb(f'fund:box:from:456:{self.aid(456)}')
        self.assertEqual(self.state(),before); self.assertFalse(bot.rows_for(self.db))
    def test_nonmanagers_cannot_start_or_use_picker(self):
        for role in ('member','foreman','investor'):
            self.db.execute('UPDATE users SET role=? WHERE id=456',(role,)); self.db.commit()
            for value in ('fund:menu','fund:person:from:123',f'fund:box:from:123:{self.aid(123)}'):
                self.cb(value,456)
            self.assertIsNone(bot.draft(self.db,456))
        self.assertFalse(bot.rows_for(self.db))
    def test_legacy_and_malformed_buttons_cannot_advance(self):
        self.cb('fund:menu'); before=self.state()
        for value in ('fund:account:1','fund:person:from:no','fund:person:from:1:2','fund:box:to:no:1'):
            self.cb(value)
        self.assertEqual(self.state(),before)
        self.assertFalse(bot.rows_for(self.db))

if __name__=='__main__': unittest.main()
