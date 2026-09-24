import unittest
import test_site_reports as fixtures
import site_reports as site
import site_notifications as notices


class AlbumTests(unittest.TestCase):
    setUp=fixtures.SiteTests.setUp
    tearDown=fixtures.SiteTests.tearDown
    ready=fixtures.SiteTests.ready
    r=fixtures.SiteTests.r
    submit=fixtures.SiteTests.submit
    cb=fixtures.SiteTests.cb
    confirm=fixtures.SiteTests.confirm

    def accepted(self):
        ident=self.ready();self.submit(ident)
        site.transition(self.db,123,123,ident,self.r(ident)['version'],'accept')
        return ident

    def test_manager_edit_accepted_fields_photos_and_notifications(self):
        ident=self.accepted()
        self.cb(f'site:view:{ident}',123)
        self.assertIn(('Редактировать альбом',f'site:manage:{ident}'),self.api.messages[-1][2])
        self.cb(f'site:manage:{ident}',987)
        self.assertIn(('Добавить фотографии',f'site:upload:{ident}'),self.api.messages[-1][2])
        site.change(self.db,987,123,ident,self.r(ident)['version'],'work','Обновлённое описание')
        site.save_photo(self.db,123,123,ident,9000,b'new photo','.jpg')
        r=self.r(ident)
        self.assertEqual((r['status'],r['user_id'],r['work']),('accepted',456,'Обновлённое описание'))
        self.api.messages.clear();notices.deliver(self.api,self.db,123)
        self.assertEqual({m[0] for m in self.api.messages},{456,789})
        self.assertTrue(all('Обновлённое описание' in m[1] for m in self.api.messages))

    def test_delete_confirmation_cascade_audit_and_stale_links(self):
        ident=self.accepted();other=self.ready(654)
        self.cb(f'site:deletealbum:{ident}:{self.r(ident)["version"]}',987)
        self.assertIsNotNone(self.r(ident))
        self.confirm(987)
        self.assertIsNone(self.db.execute('SELECT id FROM site_reports WHERE id=?',(ident,)).fetchone())
        self.assertEqual(self.db.execute('SELECT count(*) FROM site_photos WHERE report_id=?',(ident,)).fetchone()[0],0)
        self.assertEqual(self.r(other,654)['status'],'draft')
        self.assertEqual(self.db.execute("SELECT actor_id FROM audit_log WHERE entity='site_reports' AND action='DELETE' ORDER BY id DESC LIMIT 1").fetchone()[0],987)
        self.api.messages.clear();notices.deliver(self.api,self.db,123)
        self.assertFalse(self.api.messages)
        self.cb(f'site:view:{ident}',789)
        self.assertIn('недоступен',self.api.messages[-1][1])

    def test_permissions_revocation_and_stale_confirmation(self):
        ident=self.accepted();version=self.r(ident)['version']
        for uid in (456,654,789):
            with self.assertRaises(ValueError): site.delete_album(self.db,uid,123,ident,version)
            with self.assertRaises(ValueError): site.change(self.db,uid,123,ident,version,'work','Нельзя')
            with self.assertRaises(ValueError): site.save_photo(self.db,uid,123,ident,9999,b'photo','.jpg')
        self.cb(f'site:deletealbum:{ident}:{version}',987)
        self.db.execute("UPDATE users SET role='investor' WHERE id=987");self.db.commit()
        self.confirm(987);self.assertIsNotNone(self.r(ident))
        site.change(self.db,123,123,ident,version,'area','Секция Б')
        with self.assertRaises(ValueError): site.delete_album(self.db,123,123,ident,version)
        site.delete_album(self.db,123,123,ident,self.r(ident)['version'])
