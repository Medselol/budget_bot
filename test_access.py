import unittest
import bot
import access
from test_multiuser import FakeBot

class AccessTests(unittest.TestCase):
    def setUp(self):
        self.db=bot.connect(':memory:',123,(456,789,987))
        self.api=FakeBot()
        for uid,role in ((456,'foreman'),(789,'investor'),(987,'editor')):
            self.db.execute('UPDATE users SET role=? WHERE id=?',(role,uid))
        self.db.commit()
        self.a=self.account(123); self.b=self.account(456)
    def tearDown(self): self.db.close()
    def account(self,uid): return bot.names(self.db,'accounts',uid)[0]['id']
    def cb(self,uid,v,update=100): bot.handle_callback(self.api,self.db,uid,uid,update,v,123)
    def msg(self,uid,text): bot.handle_message(self.api,self.db,uid,uid,text,123)
    def seed(self):
        bot.record(self.db,dict(kind='income',occurred_on='2026-09-24',account_id=self.a,amount_kop=100000,currency='UAH'),1,123)
    def fund(self,update=2):
        return access.save_funding(self.db,987,123,dict(from_account_id=self.a,to_account_id=self.b,amount_kop=40000,currency='UAH'),update)
    def test_transfer_conserves_cash_and_not_income_or_expense(self):
        self.seed();ident=self.fund();self.assertEqual(self.fund(),ident)
        self.assertEqual(bot.balances(self.db,123)[self.a][1]['UAH'],60000)
        self.assertEqual(bot.balances(self.db,456)[self.b][1]['UAH'],40000)
        self.assertEqual(sum(v[1]['UAH'] for v in bot.balances(self.db,987).values()),0)
        bot.record(self.db,dict(kind='expense',occurred_on='2026-09-24',account_id=self.b,amount_kop=15000,currency='UAH'),3,456)
        totals=bot.summary(bot.rows_for(self.db))['UAH']
        self.assertEqual((totals['financing'],totals['expense']),(100000,15000))
        self.assertEqual(bot.balances(self.db,456)[self.b][1]['UAH'],25000)
        self.assertIn(ident,[r['id'] for r in bot.rows_for(self.db,user_id=456)])
    def test_funding_authorization_and_insufficient_money(self):
        with self.assertRaises(ValueError): self.fund()
        self.seed()
        d=dict(from_account_id=self.a,to_account_id=self.b,amount_kop=40000,currency='UAH')
        for uid in (456,789):
            with self.assertRaises(ValueError): access.save_funding(self.db,uid,123,d,9)
        self.assertEqual(len(bot.rows_for(self.db)),1)
    def test_investor_read_only_including_stale_draft(self):
        self.seed()
        bot.set_draft(self.db,789,dict(step='confirm',kind='expense',account_id=self.account(789),amount_kop=100,currency='UAH',occurred_on='2026-09-24'))
        for v in ('new:expense','save','op:delconfirm:1','role:set:789:editor','fund:menu','users:list'):
            self.cb(789,v)
        for text in ('/adduser 555 Bad','/removeuser 123','/account Bad','/renameuser 123 Bad'):
            self.msg(789,text)
        self.assertEqual(len(bot.rows_for(self.db)),1)
        self.assertEqual(bot.role_for(self.db,789),'investor')
        self.cb(789,'all:all')
        self.assertTrue(self.api.pdf_documents[-1][1].startswith(b'%PDF-'))
    def test_foreman_cannot_access_others_or_create_transfer(self):
        self.seed()
        self.cb(456,'op:delconfirm:1');self.cb(456,'new:transfer');self.cb(456,'all:all')
        self.assertEqual(len(bot.rows_for(self.db)),1)
        self.assertIsNone(bot.draft(self.db,456))
        self.assertFalse(self.api.pdf_documents)
        self.cb(456,'new:expense');self.assertEqual(bot.draft(self.db,456)['step'],'stage')
    def test_editor_edits_others_with_correct_account_owner(self):
        self.seed();self.cb(987,'op:edit:1')
        self.assertEqual(bot.draft(self.db,987)['ledger_user_id'],123)
        for v in ('source:0',f'acct:{self.a}','currency:UAH'): self.cb(987,v)
        self.msg(987,'900');self.cb(987,'date:today');self.cb(987,'comment:skip');self.cb(987,'save')
        row=bot.rows_for(self.db)[0]
        self.assertEqual((row['user_id'],row['account_id'],row['amount_kop']),(123,self.a,90000))
    def test_role_change_clears_drafts_and_owner_protected(self):
        self.cb(987,'new:income');self.cb(123,'role:set:987:investor')
        self.assertIsNone(bot.draft(self.db,987))
        self.cb(987,'save');self.assertFalse(bot.rows_for(self.db))
        self.cb(123,'role:set:123:investor');self.assertEqual(bot.role_for(self.db,123),'owner')
    def test_funding_full_confirmation_flow_and_foreman_cannot_delete(self):
        self.seed()
        for v in ('fund:menu',f'fund:account:{self.a}',f'fund:account:{self.b}','fund:currency:UAH'): self.cb(123,v)
        self.msg(123,'200');self.assertEqual(len(bot.rows_for(self.db)),1)
        self.cb(123,'fund:save',7);self.cb(123,'fund:save',8)
        self.assertEqual(len(bot.rows_for(self.db)),2)
        ident=bot.rows_for(self.db)[1]['id'];self.cb(456,f'op:delconfirm:{ident}')
        self.assertEqual(len(bot.rows_for(self.db)),2)
    def test_disabled_user_blocked_at_handlers(self):
        self.db.execute('UPDATE users SET active=0 WHERE id=987');self.db.commit()
        self.msg(987,'/adduser 555 Bad');self.cb(987,'role:set:456:editor')
        self.assertEqual(bot.role_for(self.db,456),'foreman')
        self.assertIsNone(bot.role_for(self.db,555))

if __name__=='__main__': unittest.main()
