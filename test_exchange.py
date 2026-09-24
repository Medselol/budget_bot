import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pypdf import PdfReader
import bot
import exchange as fx
import sheets_sync
from test_multiuser import FakeBot


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db.sqlite3'
        self.db=bot.connect(self.path,123,(456,789,987))
        for uid,role in ((456,'foreman'),(789,'investor'),(987,'editor')): self.db.execute('UPDATE users SET role=? WHERE id=?',(role,uid))
        self.db.commit();self.api=FakeBot();self.update=100
        self.a,self.b=[r['id'] for r in bot.names(self.db,'accounts',123)][:2]
        bot.record(self.db,dict(kind='income',occurred_on='2026-09-24',account_id=self.a,amount_kop=100000,currency='USD'),1,123)
    def tearDown(self): self.db.close();self.tmp.cleanup()
    def cb(self,v,uid=123):
        self.update+=1;bot.handle_callback(self.api,self.db,uid,uid,self.update,v,123)
    def choose(self,v,uid=123): self.cb('fx:'+bot.draft(self.db,uid)['nonce']+':'+str(v),uid)
    def msg(self,text,uid=123):bot.handle_message(self.api,self.db,uid,uid,text,123)
    def data(self,**overrides):
        d=dict(ledger_user_id=123,from_account_id=self.a,to_account_id=self.b,amount_kop=10000,currency='USD',exchange_rate='41.50',occurred_on='2026-09-24',comment='Обмен')
        d.update(overrides);return d
    def test_full_flow_roundtrip_and_no_income_expense_inflation(self):
        self.cb('ui:finance');self.assertIn('fx:new',[v for _,v in self.api.messages[-1][2]])
        self.cb('fx:new');self.choose('USD');self.choose(self.a);self.choose(self.b);self.msg('100');self.msg('41,50');self.choose('today');self.choose('skip')
        self.assertIn('4 150.00 грн',self.api.messages[-1][1]);self.assertEqual(len(bot.rows_for(self.db)),1)
        nonce=bot.draft(self.db,123)['nonce'];self.choose('save');self.cb('fx:'+nonce+':save')
        self.assertEqual(len(bot.rows_for(self.db)),2)
        bal=bot.balances(self.db,123);self.assertEqual(bal[self.a][1]['USD'],90000);self.assertEqual(bal[self.b][1]['UAH'],415000)
        fx.save(self.db,123,123,self.data(from_account_id=self.b,to_account_id=self.a,currency='UAH',amount_kop=415000),1000)
        bal=bot.balances(self.db,123);self.assertEqual(bal[self.a][1]['USD'],100000);self.assertEqual(bal[self.b][1]['UAH'],0)
        sums=bot.summary(bot.rows_for(self.db));self.assertEqual(sums['USD']['financing'],100000);self.assertEqual(sums['UAH']['financing'],0)
        self.assertEqual(sums['UAH']['expense'],0);self.assertEqual(sums['UAH']['exchange_net'],0)
    def test_same_cash_account_two_currency_balances(self):
        ident=fx.save(self.db,123,123,self.data(to_account_id=self.a),500)
        balances=bot.balances(self.db,123)[self.a][1]
        self.assertEqual(balances,{'UAH':415000,'USD':90000})
        self.assertEqual(fx.save(self.db,123,123,self.data(to_account_id=self.a),500),ident)
        self.assertEqual(len(bot.rows_for(self.db)),2)
    def test_permissions_foreign_accounts_and_demotion(self):
        for uid in (456,789):
            self.cb('fx:new',uid);self.assertIsNone(bot.draft(self.db,uid))
            with self.assertRaises(ValueError):fx.save(self.db,uid,123,self.data(),900)
        other=bot.names(self.db,'accounts',987)[0]['id']
        with self.assertRaises(ValueError):fx.save(self.db,123,123,self.data(to_account_id=other),900)
        self.cb('fx:new',987);self.choose('USD',987)
        self.db.execute("UPDATE users SET role='foreman' WHERE id=987");self.db.commit()
        self.choose(other,987);self.assertEqual(len(bot.rows_for(self.db)),1)
    def test_editor_can_exchange_their_own_funds(self):
        a,b=[r['id'] for r in bot.names(self.db,'accounts',987)][:2]
        bot.record(self.db,dict(kind='income',occurred_on='2026-09-24',account_id=a,amount_kop=415000,currency='UAH'),2,987)
        fx.save(self.db,987,123,self.data(ledger_user_id=987,from_account_id=a,to_account_id=b,currency='UAH',amount_kop=415000),900)
        self.assertEqual(bot.balances(self.db,987)[b][1]['USD'],10000)
    def test_insufficient_funds_and_rounding(self):
        with self.assertRaises(ValueError):fx.save(self.db,123,123,self.data(amount_kop=100001),500)
        self.assertEqual(fx.converted(100,'UAH','41.5'),2)
        self.assertEqual(fx.converted(1,'USD','41.5'),42)
        for r in ('0','-1','nan','Infinity','1e2','10001','1.1234567'):
            with self.assertRaises(ValueError):fx.rate(r)
        for r in ('10000','0.000001','41.50'): self.assertEqual(fx.rate(fx.rate(r)),fx.rate(r))
        with self.assertRaises(ValueError):fx.converted(1,'UAH','10000')
    def test_edit_and_delete_reverse_both_legs_and_audit(self):
        ident=fx.save(self.db,123,123,self.data(),500)
        self.cb(f'op:edit:{ident}');self.assertEqual(bot.draft(self.db,123)['step'],'fx_direction')
        self.choose('USD');self.choose(self.a);self.choose(self.b);self.msg('200');self.msg('40');self.choose('today');self.choose('skip');self.choose('save')
        self.assertEqual(bot.balances(self.db,123)[self.b][1]['UAH'],800000)
        self.cb(f'op:view:{ident}');self.assertIn('8 000.00 грн',self.api.messages[-1][1])
        audit=self.db.execute("SELECT after_json FROM audit_log WHERE entity='operations' AND action='UPDATE' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(json.loads(audit[0])['exchange_rate'],'40')
        self.cb(f'op:delete:{ident}');self.cb(f'op:delconfirm:{ident}')
        self.assertEqual(bot.balances(self.db,123)[self.b][1]['UAH'],0);self.assertEqual(bot.balances(self.db,123)[self.a][1]['USD'],100000)
    def test_stale_edit_and_legacy_editor_cannot_corrupt_exchange(self):
        ident=fx.save(self.db,123,123,self.data(),500);row=self.db.execute('SELECT * FROM operations WHERE id=?',(ident,)).fetchone()
        edit=self.data(edit_id=ident,original=fx.snapshot(row),amount_kop=20000)
        fx.save(self.db,123,123,edit,501)
        with self.assertRaises(ValueError):fx.save(self.db,123,123,edit,502)
        with self.assertRaises(ValueError):bot.update_operation(self.db,self.data(kind='transfer'),ident,123)
    def test_pdf_csv_sheets_include_both_legs(self):
        fx.save(self.db,123,123,self.data(),500)
        rows=bot.rows_for(self.db);s=bot.summary(rows)
        self.assertEqual(s['USD']['cash_net'],90000);self.assertEqual(s['UAH']['cash_net'],415000)
        text=bot.csv_report(rows,None,None,'Test').decode('utf-8-sig');self.assertIn('4150.00;UAH;41.5',text)
        ops,totals=sheets_sync.snapshot(self.db);self.assertEqual(ops[-1][-3:],['UAH',4150.0,'41.5'])
        bot.show_report(self.api,self.db,123,None,None,None)
        text='\n'.join(p.extract_text() for p in PdfReader(io.BytesIO(self.api.pdf_documents[-1][1])).pages)
        self.assertIn('Обмен валют',text);self.assertIn('4 150',text);self.assertIn('41.5',text)
    def test_migration_of_existing_database_preserves_operations(self):
        oldpath=Path(self.tmp.name)/'old.sqlite3'
        with patch('exchange.init',return_value=None): old=bot.connect(oldpath,123)
        aid=bot.names(old,'accounts',123)[0]['id']
        bot.record(old,dict(kind='income',occurred_on='2026-09-24',account_id=aid,amount_kop=100,currency='USD'),1,123);old.close()
        migrated=bot.connect(oldpath,123)
        self.assertEqual(bot.balances(migrated,123)[aid][1]['USD'],100)
        ident=fx.save(migrated,123,123,self.data(from_account_id=aid,to_account_id=aid,amount_kop=10),900)
        self.assertIn('target_amount_kop',migrated.execute("SELECT after_json FROM audit_log WHERE entity='operations' AND entity_id=? ORDER BY id DESC LIMIT 1",(ident,)).fetchone()[0])
        migrated.close();migrated=bot.connect(oldpath,123)
        self.assertEqual(bot.balances(migrated,123)[aid][1]['UAH'],415);migrated.close()
