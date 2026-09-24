import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import bot
import background
import maintenance
from test_multiuser import FakeBot


class API(FakeBot):
    def call(self, method, payload): return {'username':'TestBot'}
    def document(self, chat, name, content):
        if name.endswith('.pdf'): self.pdf_documents.append((chat,content))
        else: self.documents.append((chat,content))


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db.sqlite3'
        self.db=bot.connect(self.path,123,(456,))
        self.db.execute("UPDATE users SET role='editor' WHERE id=456");self.db.commit()
        self.api=API();self.runtime=background.Runtime(self.api,self.path,123)
        self.api.background=self.runtime
        self.releases=[]
        self.patch=patch('maintenance.Scheduler.tick',return_value=None);self.patch.start()
    def tearDown(self):
        for event in self.releases: event.set()
        self.runtime.close();self.patch.stop();self.db.close();self.tmp.cleanup()
    def start_jobs(self):
        t=threading.Thread(target=self.runtime._jobs,daemon=True);self.runtime.threads.append(t);t.start()
    def block(self):
        entered=threading.Event();release=threading.Event();self.releases.append(release)
        def work(api,db): entered.set();release.wait(3)
        return entered,release,work
    def test_slow_file_does_not_block_menu_or_financial_write(self):
        entered,release,work=self.block();self.start_jobs()
        self.assertEqual(self.runtime.submit(123,'owner','test',work),'queued')
        self.assertTrue(entered.wait(2))
        start=time.perf_counter()
        bot.handle_callback(self.api,self.db,123,123,100,'menu',123)
        aid=bot.names(self.db,'accounts',123)[0]['id']
        ident=bot.record(self.db,dict(kind='income',occurred_on='2026-09-24',account_id=aid,amount_kop=100,currency='UAH'),888,123)
        self.assertLess(time.perf_counter()-start,.5)
        self.assertEqual(bot.operation_row(self.db,ident,123)['amount_kop'],100)
        self.assertFalse(release.is_set())
        release.set();self.runtime.jobs.join()
    def test_deferred_finance_pdf_and_single_job_per_user(self):
        self.assertTrue(background.defer(self.api,self.db,123,'test',lambda api,db:None))
        bot.show_report(self.api,self.db,123,None,None,None)
        self.assertIn('предыдущий',self.api.messages[-1][1]);self.assertFalse(self.api.pdf_documents)
        self.start_jobs();self.runtime.jobs.join()
        bot.show_report(self.api,self.db,123,None,None,None)
        self.runtime.jobs.join()
        self.assertTrue(self.api.pdf_documents[-1][1].startswith(b'%PDF'))
        self.assertFalse(self.runtime.pending)
    def test_revoked_before_execution_or_delivery_never_gets_file(self):
        entered,release,work=self.block();self.start_jobs()
        self.runtime.submit(123,'owner','blocking',work);self.assertTrue(entered.wait(2))
        self.runtime.submit(456,'editor','test',lambda api,db:api.document(456,'secret.pdf',b'%PDF'))
        self.db.execute('UPDATE users SET active=0 WHERE id=456');self.db.commit()
        release.set();self.runtime.jobs.join();self.assertFalse(self.api.pdf_documents)
        self.db.execute('UPDATE users SET active=1 WHERE id=456');self.db.commit()
        entered,release,_=self.block()
        def slow(api,db):
            entered.set();release.wait(3);api.document(456,'secret.pdf',b'%PDF')
        self.runtime.submit(456,'editor','test',slow);self.assertTrue(entered.wait(2))
        self.db.execute("UPDATE users SET role='foreman' WHERE id=456");self.db.commit()
        release.set();self.runtime.jobs.join();self.assertFalse(self.api.pdf_documents)
    def test_bounded_queue_failure_cleanup_and_retry(self):
        for uid in range(10,18): self.assertEqual(self.runtime.submit(uid,'member','test',lambda a,d:None),'queued')
        self.assertEqual(self.runtime.submit(123,'owner','test',lambda a,d:None),'full')
        self.assertNotIn(123,self.runtime.pending)
        self.start_jobs();self.runtime.jobs.join()
        def broken(api,db): raise RuntimeError('private text should not be logged')
        with self.assertLogs(level='ERROR') as logs:
            self.runtime.submit(123,'owner','test',broken);self.runtime.jobs.join()
        self.assertNotIn('private text',' '.join(logs.output))
        self.assertNotIn(123,self.runtime.pending)
        self.assertIn('Не удалось',self.api.messages[-1][1])
    def test_sync_and_maintenance_are_independent_of_polling(self):
        sync_entered,sync_release,_=self.block();maint_entered,maint_release,_=self.block()
        def sync(db):
            db.execute('SELECT 1');sync_entered.set();sync_release.wait(3);return True
        def tick(*args): maint_entered.set();maint_release.wait(3)
        self.runtime.sync=sync
        with patch('maintenance.Scheduler.tick',side_effect=tick):
            self.runtime.start()
            self.assertTrue(sync_entered.wait(2));self.assertTrue(maint_entered.wait(2))
            start=time.perf_counter();bot.handle_callback(self.api,self.db,123,123,1,'menu',123)
            self.assertLess(time.perf_counter()-start,.5)
            sync_release.set();maint_release.set();self.runtime.close()
    def test_budget_pdf_and_backup_routes_defer_and_deliver(self):
        for route in ('ctl:budgetpdf','ctl:backup:create','ctl:backup:download'):
            before=len(self.api.messages)
            bot.handle_callback(self.api,self.db,123,123,1234,route,123)
            self.assertTrue(any('фоне' in m[1] for m in self.api.messages[before:]))
            if not self.runtime.threads: self.start_jobs()
            self.runtime.jobs.join()
        self.assertTrue(self.api.pdf_documents)
        self.assertIsNotNone(maintenance.latest_backup(self.db))
        self.assertTrue(self.api.documents[-1][1].startswith(b'PK'))
    def test_worker_connection_has_foreign_keys_and_audit_actor(self):
        db=background.connection(self.path)
        self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0],1)
        self.assertEqual(db.execute('SELECT audit_actor()').fetchone()[0],0);db.close()
    def test_ack_failure_does_not_drop_callback(self):
        update={'update_id':20,'callback_query':{'id':'old','data':'menu','from':{'id':123},'message':{'chat':{'id':123}}}}
        def call(method,payload):
            if method=='answerCallbackQuery': raise RuntimeError('expired')
            if method=='getUpdates':
                if payload['offset']==0:return [update]
                raise StopIteration()
        self.api.call=call
        with self.assertRaises(StopIteration): bot._poll(self.api,self.db,123)
        self.assertIn('Выбери действие',self.api.messages[-1][1])
        self.assertEqual(self.db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()[0],'21')
    def test_failed_update_is_not_skipped_by_next_update(self):
        updates=[{'update_id':i,'message':{'from':{'id':123},'chat':{'id':123},'text':'anything'}} for i in (20,21)]
        seen=[]
        def call(method,payload):
            seen.append(payload['offset'])
            if len(seen)==1:return updates
            raise StopIteration()
        self.api.call=call
        with patch('bot.handle_message',side_effect=RuntimeError('test')) as handler:
            with self.assertRaises(StopIteration): bot._poll(self.api,self.db,123)
            self.assertEqual(handler.call_count,1)
        self.assertEqual(seen,[0,0])
