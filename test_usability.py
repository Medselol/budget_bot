import unittest
from datetime import timedelta
import bot
from test_multiuser import FakeBot


class UsabilityTests(unittest.TestCase):
    def setUp(self):
        self.db = bot.connect(':memory:', 123, (456,789,987))
        for uid, role in ((456,'foreman'),(789,'investor'),(987,'editor')):
            self.db.execute('UPDATE users SET role=? WHERE id=?',(role,uid))
        self.db.commit(); self.api = FakeBot()
    def tearDown(self): self.db.close()
    def cb(self, value, uid=123): bot.handle_callback(self.api,self.db,uid,uid,999,value,123)
    def routes(self): return [v for _,v in self.api.messages[-1][2]]
    def test_compact_home_and_reachable_sections(self):
        for uid in (123,456,789,987):
            self.cb('menu',uid)
            self.assertLessEqual(len(self.routes()),10 if uid in (123,987) else 9)
            self.assertEqual('rc:list' in self.routes(),uid in (123,987))
            self.assertEqual(len(self.routes()),len(set(self.routes())))
            self.assertIn('ui:help',self.routes())
            self.assertIn('site:home',self.routes())
        for section, expected in (('finance','ops:list'),('reports','all:menu'),('team','users:list')):
            self.cb('ui:'+section); self.assertIn(expected,self.routes())
            self.assertIn('menu',self.routes()); self.assertIn('nav:back',self.routes())
    def test_role_guards_even_on_old_menu_buttons(self):
        for uid in (456,789):
            self.cb('ui:team',uid); self.assertNotIn('users:list',self.routes())
            self.cb('ui:finance',uid); self.assertNotIn('ops:list',self.routes())
        self.cb('ui:reports',789); self.assertIn('all:menu',self.routes())
        self.assertNotIn('report:menu',self.routes())
        self.db.execute("UPDATE users SET role='foreman' WHERE id=987"); self.db.commit()
        self.cb('ui:team',987); self.assertNotIn('users:list',self.routes())
    def test_help_preserves_draft_and_back(self):
        self.cb('new:expense',456); before=bot.draft(self.db,456)
        bot.handle_message(self.api,self.db,456,456,'/help',123)
        self.assertIn('Как пользоваться',self.api.messages[-1][1])
        self.assertEqual(bot.draft(self.db,456),before)
        self.cb('nav:back',456); self.assertIn('шаг 1 из 8',self.api.messages[-1][1])
        self.cb('ui:help',789); self.assertNotIn('Расход →',self.api.messages[-1][1])
    def test_yesterday_and_progress_roundtrip(self):
        self.cb('new:income')
        aid=bot.names(self.db,'accounts',123)[0]['id']
        for v in ('source:0',f'acct:{aid}','currency:UAH'): self.cb(v)
        bot.handle_message(self.api,self.db,123,123,'125',123)
        self.assertIn('date:yesterday',self.routes())
        self.cb('date:yesterday')
        self.assertEqual(bot.draft(self.db,123)['occurred_on'],(bot.datetime.now(bot.TZ).date()-timedelta(days=1)).isoformat())
        self.cb('nav:back'); self.assertEqual(bot.draft(self.db,123)['step'],'date')
        self.cb('date:today');self.cb('comment:skip')
        self.assertIn('шаг 7 из 7',self.api.messages[-1][1])
        self.cb('save');self.cb('nav:back')
        self.assertEqual(len(bot.rows_for(self.db)),1)
        self.assertIsNone(bot.draft(self.db,123))
    def test_navigation_footer_and_long_labels(self):
        rows=bot.keyboard([('Главное меню','menu'),('Обычная','a'),('Длинное название операции для чтения','b'),('← Назад','nav:back')])['inline_keyboard']
        self.assertEqual([b['callback_data'] for b in rows[-1]],['nav:back','menu'])
        self.assertEqual(len(rows[1]),1)
        self.assertEqual(rows[1][0]['callback_data'],'b')
