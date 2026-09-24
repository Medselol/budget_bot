import unittest
from unittest.mock import patch
import test_controls as fixtures
import bot
import receipts
import expense_notifications as alerts
import background


class ExpenseAlertsTests(unittest.TestCase):
    setUp=fixtures.ControlTests.setUp
    tearDown=fixtures.ControlTests.tearDown
    cb=fixtures.ControlTests.cb
    msg=fixtures.ControlTests.msg

    def expense(self,update=222,amount=12345,currency='UAH'):
        return bot.record(self.db,dict(kind='expense',occurred_on='2026-09-24',account_id=self.b,amount_kop=amount,currency=currency,comment='Бетон'),update,456)

    def test_expense_saved_pending_and_delivered_once_to_managers(self):
        ident=self.expense();self.assertEqual(self.expense(),ident)
        before=bot.balances(self.db,456)
        self.assertEqual(receipts.metadata(self.db,ident)[0],'pending')
        alerts.deliver(self.api,self.db,123)
        self.assertEqual({m[0] for m in self.api.messages},{123,987})
        self.assertIn('требует проверки',self.api.messages[0][1])
        self.assertIn(('Проверить расход',f'rc:view:{ident}'),self.api.messages[0][2])
        alerts.deliver(self.api,self.db,123);self.assertEqual(len(self.api.messages),2)
        self.cb(f'rc:approve:{ident}')
        self.assertEqual(receipts.metadata(self.db,ident)[0],'approved')
        self.assertEqual(bot.balances(self.db,456),before)

    def test_settings_personal_currency_limits_roles_and_navigation(self):
        self.cb('ec:min:UAH');self.msg('200,50')
        self.cb('ec:min:USD');self.msg('NaN')
        self.assertEqual(alerts.settings(self.db,123)['min_usd'],0)
        self.msg('10')
        self.cb('ec:off',987)
        self.cb('ec:on',456)
        self.assertFalse(alerts.settings(self.db,987)['enabled'])
        self.assertIsNone(self.db.execute('SELECT 1 FROM expense_alert_settings WHERE user_id=456').fetchone())
        self.api.messages.clear()
        self.expense();self.expense(223,1000,'USD')
        alerts.deliver(self.api,self.db,123)
        self.assertEqual(len(self.api.messages),1)
        self.assertEqual(self.api.messages[0][0],123)
        self.cb('rc:list')
        self.assertIn(('Настройки уведомлений','ec:settings'),self.api.messages[-1][2])
        self.cb('ec:reset');self.assertEqual(alerts.settings(self.db,123)['min_uah'],0)

    def test_atomic_save_and_retry_survives_worker_reconnection(self):
        with patch.object(alerts,'changed',side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError): self.expense()
        self.assertIsNone(self.db.execute('SELECT 1 FROM operations WHERE update_id=222').fetchone())
        self.expense()
        def send(uid,text,options=None):
            if uid==123: raise RuntimeError('blocked')
            self.api.messages.append((uid,text,options))
        with patch.object(self.api,'send',side_effect=send): alerts.deliver(self.api,self.db,123,100)
        self.assertEqual([m[0] for m in self.api.messages],[987])
        worker=background.connection(self.path)
        try:
            alerts.deliver(self.api,worker,123,129);self.assertEqual(len(self.api.messages),1)
            alerts.deliver(self.api,worker,123,130);self.assertEqual(len(self.api.messages),2)
        finally: worker.close()

    def test_revoked_access_deleted_and_approved_are_skipped(self):
        ident=self.expense()
        self.cb(f'rc:approve:{ident}')
        self.expense(223)
        with self.db:
            self.db.execute('DELETE FROM operations WHERE update_id=223')
        self.expense(224)
        with self.db: self.db.execute("UPDATE users SET role='investor' WHERE id=987")
        self.api.messages.clear();alerts.deliver(self.api,self.db,123)
        self.assertEqual([m[0] for m in self.api.messages],[123])

    def test_edit_invalidates_review_supersedes_old_event(self):
        ident=self.expense()
        self.cb(f'rc:approve:{ident}')
        d=dict(self.db.execute('SELECT * FROM operations WHERE id=?',(ident,)).fetchone());d['amount_kop']=98765
        bot.update_operation(self.db,d,ident,456)
        self.assertEqual(receipts.metadata(self.db,ident)[0],'pending')
        self.api.messages.clear();alerts.deliver(self.api,self.db,123)
        self.assertEqual(len(self.api.messages),2)
        self.assertIn('987.65',self.api.messages[0][1])

    def test_missing_receipt_and_person_filter_do_not_hide_review_queue(self):
        ident=self.expense()
        self.cb('ec:missing')
        with self.db:
            self.db.execute("INSERT INTO receipts(operation_id,update_id,uploader_id,file_id,filename,content) VALUES (?,999,456,'x','x.pdf',?)",(ident,b'%PDF-test'))
        self.api.messages.clear();alerts.deliver(self.api,self.db,123)
        self.assertEqual([m[0] for m in self.api.messages],[987])
        self.cb('rc:list:pending:0')
        self.assertTrue(any(v==f'rc:view:{ident}' for _,v in self.api.messages[-1][2]))
        self.cb('ec:person:456');self.assertEqual(alerts.settings(self.db,123)['foreman_id'],456)
        self.cb('ec:person:987');self.assertEqual(alerts.settings(self.db,123)['foreman_id'],456)
        self.cb('ec:person:0');self.assertEqual(alerts.settings(self.db,123)['foreman_id'],0)

    def test_old_expense_receipt_and_reopen_generate_alert_without_duplicates(self):
        import io
        with patch.object(alerts,'changed'):
            ident=self.expense()
        self.assertIsNone(self.db.execute('SELECT 1 FROM expense_alert_events').fetchone())
        self.cb(f'rc:add:{ident}',456)
        payload={'document':{'file_id':'abc','file_size':9,'mime_type':'application/pdf'}}
        with patch('receipts.urllib.request.urlopen',return_value=io.BytesIO(b'%PDF-test')):
            receipts.media(self.api,self.db,456,456,payload,666,123)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM expense_alert_events').fetchone()[0],1)
        with patch('receipts.urllib.request.urlopen',return_value=io.BytesIO(b'%PDF-test')):
            receipts.media(self.api,self.db,456,456,payload,667,123)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM expense_alert_events').fetchone()[0],1)
        self.api.messages.clear();alerts.deliver(self.api,self.db,123)
        self.assertEqual(len(self.api.messages),2)
        self.cb(f'rc:approve:{ident}');self.cb(f'rc:pending:{ident}');self.cb(f'rc:pending:{ident}')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM expense_alert_events').fetchone()[0],2)
        self.api.messages.clear();alerts.deliver(self.api,self.db,123)
        self.assertEqual(len(self.api.messages),2)

    def test_new_receipt_after_approval_and_reason_reset_notify_again(self):
        import io
        ident=self.expense();alerts.deliver(self.api,self.db,123)
        self.cb(f'rc:approve:{ident}');self.cb(f'rc:add:{ident}',456)
        payload={'document':{'file_id':'abc','file_size':9,'mime_type':'application/pdf'}}
        with patch('receipts.urllib.request.urlopen',return_value=io.BytesIO(b'%PDF-test')):
            receipts.media(self.api,self.db,456,456,payload,666,123)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM expense_alert_events').fetchone()[0],2)
        self.cb(f'rc:approve:{ident}');self.cb(f'rc:reason:{ident}',456);self.msg('Чек утерян',456)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM expense_alert_events').fetchone()[0],3)
        self.api.messages.clear();alerts.deliver(self.api,self.db,123)
        self.assertEqual(len(self.api.messages),2)
        self.assertIn('Чек утерян',self.api.messages[0][1])

    def test_delivery_reason_and_retry_only_unsent_with_role_guard(self):
        ident=self.expense();self.cb('ec:off')
        alerts.deliver(self.api,self.db,123)
        self.cb(f'ec:delivery:{ident}')
        self.assertIn('уведомления выключены',self.api.messages[-1][1])
        self.cb('ec:on');self.api.messages.clear()
        self.cb(f'ec:retry:{ident}',456)
        self.assertFalse(self.api.messages)
        self.cb(f'ec:retry:{ident}')
        self.api.messages.clear();alerts.deliver(self.api,self.db,123)
        self.assertEqual([m[0] for m in self.api.messages],[123])
        self.cb(f'ec:retry:{ident}');self.api.messages.clear();alerts.deliver(self.api,self.db,123)
        self.assertFalse(self.api.messages)

    def test_http_failure_diagnostic_no_token_or_body(self):
        from urllib.error import HTTPError
        ident=self.expense()
        with patch.object(self.api,'send',side_effect=HTTPError('secret-token-url',403,'private response',None,None)):
            alerts.deliver(self.api,self.db,123,100)
        self.cb(f'ec:delivery:{ident}')
        text=self.api.messages[-1][1]
        self.assertIn('проверь блокировку бота',text)
        self.assertNotIn('secret-token',text)
        self.assertNotIn('private response',text)

    def test_real_background_loop_delivers_callback_expense(self):
        import threading
        delivered=threading.Event()
        original_send=self.api.send
        def send(uid,text,options=None):
            original_send(uid,text,options)
            if uid==123 and 'требует проверки' in text: delivered.set()
        runtime=background.Runtime(self.api,self.path,123)
        with patch.object(self.api,'send',side_effect=send):
            bot.set_draft(self.db,456,dict(step='confirm',kind='expense',account_id=self.b,amount_kop=345000,currency='UAH',occurred_on='2026-09-24'))
            self.cb('save',456)
            worker=threading.Thread(target=runtime._notifications)
            runtime.threads.append(worker);worker.start()
            try:
                self.assertTrue(delivered.wait(3),'No alert from background runtime')
            finally: runtime.close()
        self.assertTrue(self.db.execute("SELECT 1 FROM settings WHERE key='notification_heartbeat'").fetchone())
        self.assertTrue(any(uid==123 and '3 450.00' in text for uid,text,_ in self.api.messages))
