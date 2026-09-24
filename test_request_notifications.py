import unittest
from unittest.mock import patch
import test_controls as fixtures
import control_data as data
import request_notifications as notices
import background


class NotificationTests(unittest.TestCase):
    setUp=fixtures.ControlTests.setUp
    tearDown=fixtures.ControlTests.tearDown
    request=fixtures.ControlTests.request

    def test_submit_targets_active_managers_once_and_decision_to_author(self):
        ident=self.request()
        notices.deliver(self.api,self.db,123)
        self.assertEqual(self.api.messages,[])
        data.decide_request(self.db,456,123,ident,1,'submit')
        with self.assertRaises(ValueError): data.decide_request(self.db,456,123,ident,1,'submit')
        notices.deliver(self.api,self.db,123)
        self.assertEqual({m[0] for m in self.api.messages},{123,987})
        self.assertIn(('Одобрить',f'ctl:req:approve:{ident}'),self.api.messages[0][2])
        notices.deliver(self.api,self.db,123)
        self.assertEqual(len(self.api.messages),2)
        data.decide_request(self.db,987,123,ident,2,'approve')
        notices.deliver(self.api,self.db,123)
        self.assertEqual(self.api.messages[-1][0],456)
        self.assertIn('Деньги ещё не выданы',self.api.messages[-1][1])
        with self.assertRaises(ValueError): data.decide_request(self.db,123,123,ident,2,'approve')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],1)
        op=data.pay_request(self.db,123,123,ident,3,self.a,999)
        self.assertEqual(data.pay_request(self.db,123,123,ident,3,self.a,999),op)
        notices.deliver(self.api,self.db,123)
        self.assertIn('Деньги выданы',self.api.messages[-1][1])

    def test_retry_persists_and_one_blocked_recipient_does_not_block_others(self):
        ident=self.request();data.decide_request(self.db,456,123,ident,1,'submit')
        def send(uid,text,options=None):
            if uid==123: raise RuntimeError('blocked')
            self.api.messages.append((uid,text,options))
        with patch.object(self.api,'send',side_effect=send): notices.deliver(self.api,self.db,123,now=100)
        self.assertEqual([m[0] for m in self.api.messages],[987])
        worker=background.connection(self.path)
        try:
            notices.deliver(self.api,worker,123,now=129)
            self.assertEqual(len(self.api.messages),1)
            notices.deliver(self.api,worker,123,now=130)
            self.assertEqual(self.api.messages[-1][0],123)
        finally: worker.close()

    def test_revoked_manager_and_cancelled_request_are_not_delivered(self):
        ident=self.request();data.decide_request(self.db,456,123,ident,1,'submit')
        self.db.execute("UPDATE users SET role='investor' WHERE id=987");self.db.commit()
        notices.deliver(self.api,self.db,123)
        self.assertEqual([m[0] for m in self.api.messages],[123])
        ident2=self.request('r2');data.decide_request(self.db,456,123,ident2,1,'submit')
        data.decide_request(self.db,456,123,ident2,2,'cancel')
        notices.deliver(self.api,self.db,123)
        self.assertEqual(len(self.api.messages),1)

    def test_rejection_reason_and_atomic_enqueue(self):
        ident=self.request()
        with patch.object(notices,'enqueue',side_effect=RuntimeError('db failure')):
            with self.assertRaises(RuntimeError): data.decide_request(self.db,456,123,ident,1,'submit')
        self.assertEqual(data.request(self.db,ident,456,123)['status'],'draft')
        data.decide_request(self.db,456,123,ident,1,'submit')
        data.decide_request(self.db,123,123,ident,2,'reject','Нужна смета')
        notices.deliver(self.api,self.db,123)
        self.assertEqual(len(self.api.messages),1)
        self.assertEqual(self.api.messages[0][0],456)
        self.assertIn('Нужна смета',self.api.messages[0][1])
