import io
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import bot
import site_reports as site
import maintenance
from participants import purge


class API:
    url='https://api.telegram.org/botTEST/'
    def __init__(self): self.messages=[];self.files=[]
    def send(self,chat,text,options=None): self.messages.append((chat,text,options or []))
    def document(self,chat,name,content): self.files.append((chat,name,content))
    def call(self,method,payload): return {'file_path':'photos/test.jpg'}


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.sqlite3'
        self.db=bot.connect(self.path,123,(456,789,987,654))
        for uid,role in ((456,'foreman'),(654,'foreman'),(789,'investor'),(987,'editor')):
            self.db.execute('UPDATE users SET role=? WHERE id=?',(role,uid))
        self.db.commit();self.api=API();self.update=1000
    def tearDown(self): self.db.close();self.tmp.cleanup()
    def cb(self,value,uid=456):
        self.update+=1;bot.handle_callback(self.api,self.db,uid,uid,self.update,value,123)
    def msg(self,text,uid=456): bot.handle_message(self.api,self.db,uid,uid,text,123)
    def confirm(self,uid=456): self.cb('site:confirm:'+bot.draft(self.db,uid)['nonce'],uid)
    def r(self,ident,uid=456): return site.report(self.db,ident,uid,123)
    def ready(self,uid=456,work_date=None):
        ident=site.new(self.db,uid,123)
        for field,value in (('stage','Фундамент'),('work','Залито 12 м³ бетона'),('work_date',work_date or site.today().isoformat())):
            site.change(self.db,uid,123,ident,self.r(ident,uid)['version'],field,value)
        site.save_photo(self.db,uid,123,ident,ident*100,b'\xff\xd8-test-photo','.jpg')
        return ident
    def submit(self,ident,uid=456): site.transition(self.db,uid,123,ident,self.r(ident,uid)['version'],'submit')
    def pick(self,value,uid=456): self.cb('site:pick:'+bot.draft(self.db,uid)['nonce']+':'+value,uid)
    def test_foreman_wizard_upload_submit_review_and_rework(self):
        self.cb('site:new');d=bot.draft(self.db,456);ident=d['ident']
        self.cb(f'site:stage:{d["nonce"]}:{bot.STAGES.index("Фундамент")}')
        self.cb('site:date:'+bot.draft(self.db,456)['nonce']+':today')
        self.msg('Дуплекс 2, секция А');self.msg('Залито 12 м³ бетона')
        self.msg('Нужна проверка узла примыкания');self.msg('Гидроизоляция')
        self.assertEqual(bot.draft(self.db,456)['step'],'site_upload')
        payload={'photo':[{'file_id':'fake','file_size':14}], 'media_group_id':'album'}
        with patch('attachments.urllib.request.urlopen',side_effect=lambda *a,**k:io.BytesIO(b'\xff\xd8-test-photo')):
            for update in (900,901,901): bot.handle_media(self.api,self.db,456,456,payload,update,123)
        self.assertEqual(self.db.execute('SELECT count(*) FROM site_photos').fetchone()[0],2)
        self.cb(f'site:view:{ident}');r=self.r(ident)
        self.cb(f'site:submit:{ident}:{r["version"]}');nonce=bot.draft(self.db,456)['nonce'];self.confirm()
        self.cb('site:confirm:'+nonce);self.assertEqual(self.r(ident)['status'],'submitted')
        self.cb(f'site:rework:{ident}:{self.r(ident)["version"]}',987);self.msg('Нужно фото крупным планом.',987);self.confirm(987)
        self.assertEqual(self.r(ident)['status'],'rework')
        self.cb(f'site:edit:{ident}:work');self.msg('Узел исправлен. Залито 12 м³ бетона.')
        self.submit(ident);self.cb(f'site:accept:{ident}:{self.r(ident)["version"]}',123);self.confirm(123)
        self.assertEqual(self.r(ident)['status'],'accepted')
        self.assertEqual(self.db.execute('SELECT count(*) FROM operations').fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT actor_id FROM audit_log WHERE entity='site_reports' ORDER BY id DESC LIMIT 1").fetchone()[0],123)
        self.cb(f'site:view:{ident}',789);self.assertIn('Принят',self.api.messages[-1][1])
    def test_private_drafts_foreman_isolation_and_readonly_investor(self):
        ident=self.ready()
        for uid in (123,987,789,654):
            with self.assertRaises(ValueError): self.r(ident,uid)
        self.submit(ident)
        for uid in (123,987,789): self.assertEqual(self.r(ident,uid)['id'],ident)
        with self.assertRaises(ValueError): self.r(ident,654)
        for uid in (789,654):
            with self.assertRaises(ValueError): site.change(self.db,uid,123,ident,self.r(ident)['version'],'work','test')
            with self.assertRaises(ValueError): site.transition(self.db,uid,123,ident,self.r(ident)['version'],'accept')
        self.cb('site:new',789);self.assertEqual(self.db.execute('SELECT count(*) FROM site_reports').fetchone()[0],1)
        self.assertEqual(site.list_rows(self.db,654,123,{'author':456}),[])
    def test_required_fields_future_date_stale_updates_and_submission_lock(self):
        ident=site.new(self.db,456,123)
        with self.assertRaises(ValueError): self.submit(ident)
        with self.assertRaises(ValueError): site.change(self.db,456,123,ident,1,'work_date',(site.today()+timedelta(days=1)).isoformat())
        ident=self.ready();version=self.r(ident)['version']
        site.change(self.db,456,123,ident,version,'area','Секция А')
        with self.assertRaises(ValueError): site.change(self.db,456,123,ident,version,'area','Секция Б')
        self.submit(ident)
        with self.assertRaises(ValueError): site.save_photo(self.db,456,123,ident,7890,b'\xff\xd8-photo','.jpg')
        with self.assertRaises(ValueError): site.change(self.db,456,123,ident,self.r(ident)['version'],'work','test')
        self.cb(f'site:submit:{ident}:{version}');self.assertIn('изменился',self.api.messages[-1][1])
    def test_photo_access_bound_to_report_and_limits_remove_confirmation(self):
        ident=self.ready();photo=self.db.execute('SELECT id FROM site_photos').fetchone()[0]
        self.cb(f'site:photo:{ident}:{photo}',654);self.assertFalse(self.api.files)
        self.cb(f'site:photo:{ident}:{photo}');self.assertEqual(self.api.files[-1][2],b'\xff\xd8-test-photo')
        other=self.ready(654);self.cb(f'site:photo:{other}:{photo}',654);self.assertEqual(len(self.api.files),1)
        for i in range(9): site.save_photo(self.db,456,123,ident,3000+i,b'\xff\xd8-image','.jpg')
        with self.assertRaises(ValueError): site.save_photo(self.db,456,123,ident,4000,b'\xff\xd8-image','.jpg')
        self.cb(f'site:remove:{ident}:{photo}:{self.r(ident)["version"]}')
        self.assertEqual(self.db.execute('SELECT count(*) FROM site_photos WHERE report_id=?',(ident,)).fetchone()[0],10)
        self.confirm();self.assertEqual(self.db.execute('SELECT count(*) FROM site_photos WHERE report_id=?',(ident,)).fetchone()[0],9)
        self.cb(f'site:upload:{ident}')
        bot.handle_media(self.api,self.db,456,456,{'document':{'mime_type':'application/pdf','file_id':'fake'}},5000,123)
        self.assertIn('JPG / PNG',self.api.messages[-1][1])
    def test_filters_custom_dates_investor_pagination_and_reset(self):
        with patch('site_reports.today',return_value=date(2026,9,24)):
            for work_date in ('2026-08-30','2026-09-01','2026-09-21','2026-09-24'):
                ident=self.ready(work_date=work_date);self.submit(ident)
            other=self.ready(654,'2026-09-23');self.submit(other,654)
            self.assertEqual(len(site.list_rows(self.db,789,123,{'period':'month'})),4)
            self.assertEqual(len(site.list_rows(self.db,789,123,{'period':'week','author':456})),2)
            self.assertEqual(len(site.list_rows(self.db,789,123,{'period':'day','stage':'Фундамент'})),1)
            self.cb('site:filter:period:0',789);self.pick('custom',789);self.msg('2026-09-01 2026-09-02',789)
            opts=self.api.messages[-1][2];self.assertEqual(len([v for _,v in opts if v.startswith('site:view:')]),1)
            self.assertIn('2026-09-01 — 2026-09-02',self.api.messages[-1][1])
            self.cb('site:reset',789)
            for _ in range(4): ident=self.ready();self.submit(ident)
            self.cb('site:list:0',789);self.assertIn('site:list:1',[v for _,v in self.api.messages[-1][2]])
            self.cb('site:list:1',789);self.assertEqual(len([v for _,v in self.api.messages[-1][2] if v.startswith('site:view:')]),3)
    def test_demotion_and_archive_block_stale_form_and_media(self):
        ident=self.ready();self.cb(f'site:edit:{ident}:work')
        self.db.execute("UPDATE users SET role='investor' WHERE id=456");self.db.commit();self.msg('Подмена')
        self.assertNotEqual(self.r(ident)['work'],'Подмена')
        self.db.execute("UPDATE users SET role='foreman' WHERE id=456");self.db.commit()
        self.cb(f'site:upload:{ident}')
        self.db.execute('UPDATE users SET active=0 WHERE id=456');self.db.commit()
        bot.handle_media(self.api,self.db,456,456,{'photo':[{'file_id':'fake','file_size':2}]},6000,123)
        self.assertEqual(self.db.execute('SELECT count(*) FROM site_photos').fetchone()[0],1)
    def test_review_demotion_before_confirmation(self):
        ident=self.ready();self.submit(ident)
        self.cb(f'site:accept:{ident}:{self.r(ident)["version"]}',987)
        self.db.execute("UPDATE users SET role='investor' WHERE id=987");self.db.commit();self.confirm(987)
        self.assertEqual(self.r(ident)['status'],'submitted')
    def test_restart_backup_purge_preserves_submitted_photos_and_audit(self):
        ident=self.ready();self.submit(ident)
        self.db.execute('UPDATE users SET active=0 WHERE id=456');self.db.commit()
        self.assertTrue(purge(self.db,456,123));self.assertEqual(self.r(ident,789)['status'],'submitted')
        path=maintenance.create_backup(self.db)
        with zipfile.ZipFile(path) as z: data=z.read('ledger.sqlite3')
        restore=Path(self.tmp.name)/'restore.sqlite3';restore.write_bytes(data)
        other=bot.connect(restore,123)
        self.assertEqual(other.execute('SELECT content FROM site_photos').fetchone()[0],b'\xff\xd8-test-photo')
        self.assertFalse(other.execute('PRAGMA foreign_key_check').fetchall());other.close()
        self.db.close();self.db=bot.connect(self.path,123)
        self.assertEqual(self.r(ident,123)['work'],'Залито 12 м³ бетона')
    def test_draft_resume_delete_and_navigation_all_roles(self):
        ident=self.ready();self.cb('menu');self.cb('site:new')
        self.assertEqual(bot.draft(self.db,456)['ident'],ident)
        self.cb(f'site:discard:{ident}:{self.r(ident)["version"]}');self.confirm()
        self.assertEqual(self.db.execute('SELECT count(*) FROM site_photos').fetchone()[0],0)
        for uid in (123,456,789,987):
            self.cb('/start',uid);self.assertIn('site:home',[v for _,v in self.api.messages[-1][2]])
            for route in ('site:home','site:list:0','site:filter:stage:0'):
                self.cb(route,uid);values=[v for _,v in self.api.messages[-1][2]]
                self.assertIn('menu',values);self.assertIn('nav:back',values)
                for label,value in self.api.messages[-1][2]: self.assertLessEqual(len(value.encode()),64)
        self.cb('site:home');self.cb('site:list:0');self.cb('nav:back');self.assertIn('Фотоотчёты стройки',self.api.messages[-1][1])


if __name__=='__main__': unittest.main()
