import unittest
from unittest.mock import patch
import test_site_reports as fixtures
import site_reports as site
import site_notifications as notices
import background


class PhotoNotificationTests(unittest.TestCase):
    setUp=fixtures.SiteTests.setUp
    tearDown=fixtures.SiteTests.tearDown
    ready=fixtures.SiteTests.ready
    r=fixtures.SiteTests.r
    submit=fixtures.SiteTests.submit
    cb=fixtures.SiteTests.cb
    confirm=fixtures.SiteTests.confirm

    def test_submission_acceptance_and_investor_photos(self):
        ident=self.ready();notices.deliver(self.api,self.db,123)
        self.assertFalse(self.api.messages)
        self.submit(ident);version=self.r(ident)['version']
        notices.deliver(self.api,self.db,123)
        self.assertEqual({m[0] for m in self.api.messages},{123,987})
        self.assertIn('Фундамент',self.api.messages[0][1])
        notices.deliver(self.api,self.db,123);self.assertEqual(len(self.api.messages),2)
        site.transition(self.db,123,123,ident,version,'accept')
        with self.assertRaises(ValueError): site.transition(self.db,987,123,ident,version,'accept')
        notices.deliver(self.api,self.db,123)
        self.assertEqual({m[0] for m in self.api.messages[2:]},{456,789})
        msg=next(m for m in self.api.messages if m[0]==789)
        self.assertIn(('Смотреть фотографии',f'site:photos:{ident}'),msg[2])
        self.cb(f'site:photos:{ident}',789)
        self.assertTrue(any(v.startswith('site:photo:') for _,v in self.api.messages[-1][2]))

    def test_rework_only_to_author_and_resubmission(self):
        ident=self.ready();self.submit(ident)
        site.transition(self.db,987,123,ident,self.r(ident)['version'],'rework','Добавь общий план')
        notices.deliver(self.api,self.db,123)
        self.assertEqual([m[0] for m in self.api.messages],[456])
        self.assertIn('Добавь общий план',self.api.messages[0][1])
        self.submit(ident);notices.deliver(self.api,self.db,123)
        self.assertEqual({m[0] for m in self.api.messages[1:]},{123,987})

    def test_retry_restart_revocation_and_atomic_queue(self):
        ident=self.ready()
        with patch.object(notices,'enqueue',side_effect=RuntimeError('fail')):
            with self.assertRaises(RuntimeError): self.submit(ident)
        self.assertEqual(self.r(ident)['status'],'draft')
        self.submit(ident)
        def send(uid,text,opts=None):
            if uid==123: raise RuntimeError('offline')
            self.api.messages.append((uid,text,opts))
        with patch.object(self.api,'send',side_effect=send): notices.deliver(self.api,self.db,123,now=100)
        worker=background.connection(self.path)
        try: notices.deliver(self.api,worker,123,now=130)
        finally: worker.close()
        self.assertEqual({m[0] for m in self.api.messages},{123,987})
        site.transition(self.db,123,123,ident,self.r(ident)['version'],'accept')
        self.db.execute('UPDATE users SET active=0 WHERE id=789');self.db.commit()
        self.api.messages.clear();notices.deliver(self.api,self.db,123)
        self.assertEqual([m[0] for m in self.api.messages],[456])

    def test_manager_deletes_accepted_photo_with_confirmation_and_audit(self):
        ident=self.ready();site.save_photo(self.db,456,123,ident,7000,b'photo','.jpg');self.submit(ident)
        site.transition(self.db,123,123,ident,self.r(ident)['version'],'accept')
        ids=[r[0] for r in self.db.execute('SELECT id FROM site_photos ORDER BY id')]
        for uid in (456,654,789):
            with self.assertRaises(ValueError): site.remove_photo(self.db,uid,123,ident,ids[0],self.r(ident)['version'])
        for uid,photo in zip((987,123),ids):
            self.cb(f'site:photo:{ident}:{photo}',uid)
            self.assertTrue(any(v.startswith('site:remove:') for _,v in self.api.messages[-1][2]))
            self.cb(f'site:remove:{ident}:{photo}:{self.r(ident)["version"]}',uid)
            self.assertIsNotNone(self.db.execute('SELECT id FROM site_photos WHERE id=?',(photo,)).fetchone())
            self.confirm(uid)
            self.assertIsNone(self.db.execute('SELECT id FROM site_photos WHERE id=?',(photo,)).fetchone())
            audit=self.db.execute("SELECT actor_id FROM audit_log WHERE entity='site_photos' AND action='DELETE' ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(audit[0],uid)
        self.assertEqual(self.r(ident)['status'],'accepted')
        self.api.messages.clear();notices.deliver(self.api,self.db,123)
        self.assertEqual({m[0] for m in self.api.messages},{456,789})
        self.assertTrue(all('Фотографий: 0' in m[1] for m in self.api.messages))

    def test_deletion_rechecks_rights_version_and_report_binding(self):
        ident=self.ready();self.submit(ident);other=self.ready(654)
        photo=self.db.execute('SELECT id FROM site_photos WHERE report_id=?',(ident,)).fetchone()[0]
        version=self.r(ident)['version']
        self.cb(f'site:remove:{ident}:{photo}:{version}',987)
        self.db.execute("UPDATE users SET role='investor' WHERE id=987");self.db.commit()
        self.confirm(987)
        self.assertIsNotNone(self.db.execute('SELECT id FROM site_photos WHERE id=?',(photo,)).fetchone())
        with self.assertRaises(ValueError): site.remove_photo(self.db,654,123,other,photo,self.r(other,654)['version'])
        with self.assertRaises(ValueError): site.remove_photo(self.db,123,123,ident,photo,version-1)
