import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from pypdf import PdfReader
import bot
import controls
import control_data as data
import maintenance
import audit_log
from control_pdf import budget_pdf

class API:
    url='https://api.telegram.org/botFAKE/'
    def __init__(self): self.messages=[];self.files=[]
    def send(self,chat,text,options=None): self.messages.append((chat,text,options or []))
    def document(self,chat,name,content): self.files.append((chat,name,content))
    def call(self,method,payload): return {'file_path':'documents/invoice.pdf'} if method=='getFile' else {'username':'TestLedgerBot'}

class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.sqlite3'
        self.db=bot.connect(self.path,123,(456,789,987))
        self.db.execute("UPDATE users SET role='foreman' WHERE id=456")
        self.db.execute("UPDATE users SET role='investor' WHERE id=789")
        self.db.execute("UPDATE users SET role='editor' WHERE id=987");self.db.commit()
        self.api=API();self.up=100
        self.a=bot.names(self.db,'accounts',123)[0]['id'];self.b=bot.names(self.db,'accounts',456)[0]['id']
        bot.record(self.db,dict(kind='income',occurred_on=data.today().isoformat(),account_id=self.a,amount_kop=1000000,currency='UAH'),1,123)
    def tearDown(self): self.db.close();self.tmp.cleanup()
    def cb(self,v,uid=123):
        self.up+=1;bot.handle_callback(self.api,self.db,uid,uid,self.up,v,123)
    def msg(self,v,uid=123): bot.handle_message(self.api,self.db,uid,uid,v,123)
    def choose(self,v,uid=123): self.cb('ctl:choose:'+bot.draft(self.db,uid)['nonce']+':'+str(v),uid)
    def save(self,uid=123): self.cb('ctl:save:'+bot.draft(self.db,uid)['nonce'],uid)
    def confirm(self,uid=123): self.cb('ctl:confirm:'+bot.draft(self.db,uid)['nonce'],uid)
    def request(self,token='r1'):
        v=dict(account_id=self.b,currency='UAH',amount_kop=50000,purpose='Материалы для фундамента',due_on=data.today().isoformat())
        return data.create_request(self.db,456,123,v,token)
    def approved(self):
        ident=self.request();data.decide_request(self.db,456,123,ident,1,'submit');data.decide_request(self.db,123,123,ident,2,'approve');return ident
    def debt(self,token='d1',direction='payable',amount=30000):
        v=dict(direction=direction,counterparty='Поставщик',purpose='Бетон для фундамента',stage='Фундамент',currency='UAH',amount_kop=amount,due_on=data.today().isoformat())
        return data.create_obligation(self.db,123,123,v,token)
    def test_request_full_flow_invoice_and_duplicate_clicks(self):
        self.cb('ctl:new:request',456);self.choose(self.b,456);self.choose('UAH',456);self.msg('500',456);self.msg('Покупка бетона',456);self.choose('today',456)
        nonce=bot.draft(self.db,456)['nonce'];self.save(456);self.cb('ctl:save:'+nonce,456)
        self.assertEqual(self.db.execute('SELECT count(*) FROM funding_requests').fetchone()[0],1)
        ident=self.db.execute('SELECT id FROM funding_requests').fetchone()[0]
        self.cb(f'ctl:req:attach:{ident}',456)
        with patch('attachments.urllib.request.urlopen',return_value=io.BytesIO(b'%PDF-test')):
            bot.handle_media(self.api,self.db,456,456,{'document':{'file_id':'test','file_size':9,'mime_type':'application/pdf'}},700,123)
        self.assertEqual(self.db.execute('SELECT count(*) FROM request_files').fetchone()[0],1)
        self.cb(f'ctl:req:submit:{ident}',456);self.confirm(456)
        self.assertEqual(bot.balances(self.db,456)[self.b][1]['UAH'],0)
        self.cb(f'ctl:req:approve:{ident}',987);self.confirm(987)
        self.cb(f'ctl:req:fund:{ident}',123);self.choose(self.a);d=bot.draft(self.db,123);self.save();self.cb('ctl:save:'+d['nonce'])
        r=data.request(self.db,ident,123,123);self.assertEqual(r['status'],'paid')
        self.assertEqual(bot.balances(self.db,123)[self.a][1]['UAH'],950000)
        self.assertEqual(bot.balances(self.db,456)[self.b][1]['UAH'],50000)
        self.assertEqual(self.db.execute("SELECT count(*) FROM operations WHERE kind='transfer'").fetchone()[0],1)
        self.assertEqual(self.db.execute("SELECT actor_id FROM audit_log WHERE entity='operations' AND action='INSERT' ORDER BY id DESC LIMIT 1").fetchone()[0],123)
    def test_request_privacy_and_forged_mutations(self):
        ident=self.request()
        for uid in (456,789):
            self.cb(f'ctl:req:approve:{ident}',uid)
            self.assertNotEqual(data.request(self.db,ident,123,123)['status'],'approved')
        bot.handle_message(self.api,self.db,123,123,'/adduser 333 Другой',123)
        self.cb(f'ctl:req:view:{ident}',333);self.assertIn('недоступна',self.api.messages[-1][1])
        self.cb(f'ctl:req:files:{ident}',333);self.assertFalse(self.api.files)
        for route in ('ctl:new:budget','ctl:new:debt','ctl:audit:0','ctl:backup:download','ctl:budget:UAH:0','ctl:debt:list:open:0'):
            self.cb(route,456)
            self.assertTrue(any(word in self.api.messages[-1][1].lower() for word in ('доступ','администратор','владельц'))) 
        self.cb('ctl:new:debt',789);self.assertNotEqual((bot.draft(self.db,789) or {}).get('flow'),'debt')
    def test_request_concurrency_insufficient_funds_and_deleted_transfer(self):
        ident=self.approved()
        with self.assertRaises(ValueError): data.pay_request(self.db,123,123,ident,2,self.a,50)
        with self.assertRaises(ValueError): data.pay_request(self.db,123,123,ident,3,self.b,50)
        empty=bot.names(self.db,'accounts',123)[1]['id']
        with self.assertRaises(ValueError): data.pay_request(self.db,123,123,ident,3,empty,50)
        self.assertEqual(self.db.execute('SELECT count(*) FROM operations').fetchone()[0],1)
        op=data.pay_request(self.db,123,123,ident,3,self.a,50)
        self.assertEqual(data.pay_request(self.db,123,123,ident,3,self.a,51),op)
        self.cb(f'op:delete:{op}');self.cb(f'op:delconfirm:{op}')
        self.assertEqual(data.request(self.db,ident,123,123)['status'],'approved')
        self.assertEqual(bot.balances(self.db,456)[self.b][1]['UAH'],0)
    def test_archive_with_request_keeps_account_and_blocks_payout(self):
        ident=self.approved()
        self.db.execute('UPDATE users SET active=0 WHERE id=456');self.db.commit()
        from participants import purge
        self.assertTrue(purge(self.db,456,123))
        with self.assertRaises(ValueError): data.pay_request(self.db,123,123,ident,3,self.a,50)
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())
    def test_budget_missing_plan_zero_and_currency_separation(self):
        data.set_budget(self.db,123,123,'Фундамент','UAH',10000,0)
        bot.record(self.db,dict(kind='expense',occurred_on=data.today().isoformat(),account_id=self.b,category='Фундамент',amount_kop=15000,currency='UAH'),2,456)
        bot.record(self.db,dict(kind='expense',occurred_on=data.today().isoformat(),account_id=self.b,category='Стены',amount_kop=7000,currency='USD'),3,456)
        self.assertIn(('Фундамент',10000,15000),data.budget_rows(self.db,'UAH'))
        self.assertIn(('Стены',None,7000),data.budget_rows(self.db,'USD'))
        with self.assertRaises(ValueError): data.set_budget(self.db,123,123,'Фундамент','UAH',20000,0)
        data.set_budget(self.db,123,123,'Фундамент','UAH',0,1)
        self.assertIn(('Фундамент',0,15000),data.budget_rows(self.db,'UAH'))
        for uid in (456,789):
            with self.assertRaises(ValueError): data.set_budget(self.db,uid,123,'Стены','USD',50,0)
        self.cb('ctl:budgetpdf',789)
        pdf=PdfReader(io.BytesIO(self.api.files[-1][2]));self.assertGreaterEqual(len(pdf.pages),2)
        self.assertIn('Бюджет',pdf.pages[0].extract_text())
        Path('/tmp/control-budget.pdf').write_bytes(self.api.files[-1][2])
    def test_budget_form_and_back(self):
        self.cb('ctl:new:budget');self.choose(2);self.choose('UAH');self.msg('50000');self.save()
        self.assertIn(('Фундамент',5000000,0),data.budget_rows(self.db,'UAH'))
        self.cb('ctl:new:budget');self.choose(2);self.cb('nav:back')
        self.assertEqual(bot.draft(self.db,123)['index'],0)
    def test_partial_debt_payments_balance_dedup_and_reopen(self):
        ident=self.debt()
        op=data.settle(self.db,123,123,ident,1,10000,self.a,'pay1',50)
        self.assertEqual(data.settle(self.db,123,123,ident,1,10000,self.a,'pay1',51),op)
        r=data.obligation(self.db,ident);self.assertEqual(r['paid'],10000)
        with self.assertRaises(ValueError): data.settle(self.db,123,123,ident,r['version'],25000,self.a,'pay2',52)
        op2=data.settle(self.db,123,123,ident,r['version'],20000,self.a,'pay2',52)
        self.assertEqual(controls.debt_state(data.obligation(self.db,ident)),'Оплачен')
        self.assertEqual(bot.balances(self.db,123)[self.a][1]['UAH'],970000)
        with self.db: self.db.execute('DELETE FROM operations WHERE id=?',(op2,))
        self.assertEqual(data.obligation(self.db,ident)['paid'],10000)
        self.assertNotEqual(controls.debt_state(data.obligation(self.db,ident)),'Оплачен')
        with self.assertRaises(sqlite3.IntegrityError),self.db: self.db.execute('UPDATE operations SET amount_kop=40000 WHERE id=?',(op,))
    def test_debt_income_and_form(self):
        self.cb('ctl:new:debt');self.choose('receivable');self.msg('Покупатель');self.msg('Аванс по договору');self.choose(0);self.choose(3);self.choose('USD');self.msg('300');self.choose('today');self.save()
        ident=self.db.execute('SELECT id FROM obligations').fetchone()[0]
        self.cb(f'ctl:debt:pay:{ident}');self.choose(self.a);self.msg('150');self.save()
        self.assertEqual(bot.balances(self.db,123)[self.a][1]['USD'],15000)
        self.assertEqual(bot.summary(bot.rows_for(self.db))['USD']['sales'],15000)
        self.cb(f'ctl:debt:view:{ident}',789);self.assertIn('Осталось: 150.00 USD',self.api.messages[-1][1])
        self.cb(f'ctl:debt:pay:{ident}',789);self.assertNotEqual((bot.draft(self.db,789) or {}).get('flow'),'pay')
    def test_audit_records_editor_and_old_values_and_survives_reopen(self):
        ident=self.debt()
        with audit_log.actor(self.db,987): data.change_obligation(self.db,987,123,ident,1,'due','2026-12-31')
        r=self.db.execute("SELECT * FROM audit_log WHERE entity='obligations' AND action='UPDATE' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(r['actor_id'],987);self.assertEqual(json.loads(r['after_json'])['due_on'],'2026-12-31')
        with self.assertRaises(sqlite3.IntegrityError),self.db: self.db.execute('DELETE FROM audit_log')
        self.db.close();self.db=bot.connect(self.path,123)
        self.assertTrue(self.db.execute('SELECT 1 FROM audit_log WHERE id=?',(r['id'],)).fetchone())
        self.cb('ctl:audit:0',987);self.assertIn('Журнал',self.api.messages[-1][1])
    def test_backup_restores_all_records_and_receipts(self):
        ident=self.request()
        self.db.execute('INSERT INTO request_files(request_id,update_id,filename,content) VALUES (?,?,?,?)',(ident,77,'sample.pdf',b'%PDF-private'));self.db.commit()
        path=maintenance.create_backup(self.db)
        with zipfile.ZipFile(path) as z: content=z.read('ledger.sqlite3')
        restored=Path(self.tmp.name)/'restored.sqlite3';restored.write_bytes(content)
        restored_db=bot.connect(restored,123)
        try:
            self.assertEqual(restored_db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(restored_db.execute('SELECT content FROM request_files').fetchone()[0],b'%PDF-private')
            self.assertEqual(bot.balances(restored_db,123),bot.balances(self.db,123))
        finally: restored_db.close()
        self.cb('ctl:backup:download',789);self.assertFalse(self.api.files)
        self.cb('ctl:backup:download');self.assertTrue(self.api.files[-1][1].endswith('.zip'))
    def test_backup_rotation_and_weekly_opt_in_revocation(self):
        start=datetime(2026,9,21,10,tzinfo=bot.TZ)
        for n in range(9): maintenance.create_backup(self.db,start+timedelta(days=n))
        self.assertEqual(len(list(maintenance.backup_dir(self.db).glob('*.zip'))),7)
        self.assertFalse(list(maintenance.backup_dir(self.db).glob('*.part')))
        with patch('control_data.today',return_value=start.date()):
            maintenance.subscribe(self.db,123,123,True);maintenance.subscribe(self.db,789,123,True)
        self.db.execute("UPDATE users SET role='foreman' WHERE id=789");self.db.commit()
        scheduler=maintenance.Scheduler();scheduler.tick(self.api,self.db,123,start+timedelta(days=7))
        scheduler.tick(self.api,self.db,123,start+timedelta(days=7,hours=1))
        msgs=[m for m in self.api.messages if 'Недельная сводка:' in m[1]]
        self.assertEqual(len(msgs),1);self.assertEqual(msgs[0][0],123)
        self.assertEqual(self.db.execute('SELECT enabled FROM weekly_subscriptions WHERE user_id=789').fetchone()[0],0)
    def test_demotion_blocks_existing_confirmation(self):
        ident=self.approved();self.cb(f'ctl:req:fund:{ident}',987);self.choose(self.a,987)
        token=bot.draft(self.db,987)['nonce']
        self.db.execute("UPDATE users SET role='foreman' WHERE id=987");self.db.commit()
        self.cb('ctl:save:'+token,987)
        self.assertEqual(data.request(self.db,ident,123,123)['status'],'approved')
    def test_weekly_excludes_transfers_and_uses_closed_week(self):
        end=data.week_start(data.today())-timedelta(days=1)
        bot.record(self.db,dict(kind='expense',occurred_on=end.isoformat(),account_id=self.b,amount_kop=12300,currency='UAH'),22,456)
        text=data.weekly_text(self.db,end)
        self.assertIn('Расход: 123.00 грн',text);self.assertIn('Приход: 0.00 грн',text)
    def test_investor_cannot_mutate_own_old_request(self):
        ident=self.request()
        self.db.execute("UPDATE users SET role='investor' WHERE id=456");self.db.commit()
        with self.assertRaises(ValueError): data.decide_request(self.db,456,123,ident,1,'submit')
        self.cb(f'ctl:req:attach:{ident}',456)
        self.assertNotEqual((bot.draft(self.db,456) or {}).get('step'),'ctl_upload')
        self.cb(f'ctl:req:cancel:{ident}',456)
        if (bot.draft(self.db,456) or {}).get('step')=='ctl_action': self.confirm(456)
        self.assertEqual(data.request(self.db,ident,123,123)['status'],'draft')

    def test_restore_utility_refuses_overwrite(self):
        from restore_backup import restore
        archive=maintenance.create_backup(self.db)
        target=Path(self.tmp.name)/'restored-copy.sqlite3'
        restore(archive,target)
        size=target.stat().st_size
        with self.assertRaises(ValueError): restore(archive,target)
        self.assertEqual(target.stat().st_size,size)

    def test_all_new_sections_have_navigation(self):
        for route in ('ctl:home','ctl:req:list:open:0','ctl:new:request','ctl:budget:UAH:0','ctl:new:budget','ctl:debt:list:open:0','ctl:new:debt','ctl:weekly','ctl:audit:0','ctl:backup'):
            self.cb(route)
            routes={v for _,v in self.api.messages[-1][2]}
            self.assertIn('menu',routes);self.assertIn('nav:back',routes)

if __name__=='__main__': unittest.main()
