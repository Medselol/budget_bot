import unittest
import bot
from test_multiuser import FakeBot

class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.db=bot.connect(':memory:',123,(456,789,987));self.api=FakeBot()
        self.db.execute("UPDATE users SET role='foreman' WHERE id=456")
        self.db.execute("UPDATE users SET role='investor' WHERE id=789");self.db.commit()
    def tearDown(self): self.db.close()
    def cb(self,v,uid=123): bot.handle_callback(self.api,self.db,uid,uid,900,v,123)
    def msg(self,v,uid=123): bot.handle_message(self.api,self.db,uid,uid,v,123)
    def assertNavigation(self):
        buttons=dict((v,k) for k,v in self.api.messages[-1][2])
        self.assertEqual(buttons['menu'],'Главное меню');self.assertIn('nav:back',buttons)
    def test_all_sections_have_navigation(self):
        for route in ('all:menu','overview:period:month','people:menu','team:balances','team:archive','users:list','participant:add','fund:menu','rc:list','rc:list:missing:0','rc:filtermonth','ops:list','balance','report:menu'):
            self.cb('menu');self.cb(route);self.assertNavigation()
        for uid,route in ((456,'rc:mine'),(456,'new:expense'),(789,'all:menu'),(789,'people:menu')):
            self.cb(route,uid);self.assertNavigation()
    def test_back_restores_previous_form_step_and_main_clears(self):
        self.cb('menu');self.cb('new:expense');self.cb('stage:0')
        self.assertEqual(bot.draft(self.db,123)['step'],'cost_type')
        self.cb('nav:back');self.assertEqual(bot.draft(self.db,123)['step'],'stage')
        self.cb('stage:1');self.cb('menu');self.assertIsNone(bot.draft(self.db,123))
        self.cb('nav:back');self.assertIsNone(bot.draft(self.db,123))
    def test_back_does_not_replay_actions_or_saved_drafts(self):
        self.cb('menu');self.cb('new:income');self.cb('source:0')
        aid=bot.names(self.db,'accounts',123)[0]['id']
        self.cb(f'acct:{aid}');self.cb('currency:UAH');self.msg('200');self.cb('date:today');self.cb('comment:skip');self.cb('save')
        self.cb('nav:back');self.assertIsNone(bot.draft(self.db,123))
        self.cb('save');self.assertEqual(len(bot.rows_for(self.db)),1)
    def test_role_change_invalidates_private_history(self):
        self.cb('users:list',987)
        self.db.execute("UPDATE users SET role='foreman' WHERE id=987");self.db.commit()
        self.cb('nav:back',987)
        self.assertNotIn('users:list',str(self.api.messages[-1]))
        self.assertIn('Мой баланс',str(self.api.messages[-1]))
    def test_reports_back_is_screen_restore(self):
        self.cb('menu');self.cb('all:menu');self.cb('overview:period:month')
        self.cb('nav:back');self.assertIn('Выбери период',self.api.messages[-1][1])
        self.assertFalse(self.api.pdf_documents)
    def test_transfer_field_back(self):
        aid=bot.names(self.db,'accounts',123)[0]['id'];bid=bot.names(self.db,'accounts',123)[1]['id']
        ident=bot.record(self.db,dict(kind='transfer',occurred_on='2026-09-24',from_account_id=aid,to_account_id=bid,amount_kop=100,currency='UAH'),1,123)
        self.cb(f'op:edit:{ident}');self.cb('tx:field:amount_kop');self.msg('300')
        self.cb('nav:back');self.assertEqual(bot.draft(self.db,123)['field'],'amount_kop')
        self.cb('nav:back');self.assertNotIn('field',bot.draft(self.db,123))
        self.cb('menu');self.assertIsNone(bot.draft(self.db,123))
