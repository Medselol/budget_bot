import io
import json
import unittest
from unittest.mock import patch
from email.parser import BytesParser
from email.policy import default
import test_site_reports as fixtures
import site_reports as site
import telegram_album
import background


class AlbumTransportTests(unittest.TestCase):
    def test_media_group_and_single_photo_multipart(self):
        for count in (1,2,10):
            photos=[(f'p{i}.jpg',b'jpeg'+bytes([i])) for i in range(count)]
            with patch('telegram_album.urllib.request.urlopen',return_value=io.BytesIO(b'{"ok":true,"result":[]}')) as call:
                telegram_album.send('https://example.invalid/',123,photos,'Этап: фундамент')
            request=call.call_args.args[0]
            self.assertTrue(request.full_url.endswith('sendPhoto' if count==1 else 'sendMediaGroup'))
            msg=BytesParser(policy=default).parsebytes(('Content-Type: '+request.get_header('Content-type')+'\r\n\r\n').encode()+request.data)
            parts={p.get_param('name',header='content-disposition'):p.get_payload(decode=True) for p in msg.iter_parts()}
            if count>1:
                media=json.loads(parts['media']);self.assertEqual(len(media),count)
                self.assertEqual(media[0]['caption'],'Этап: фундамент')
                for i,m in enumerate(media): self.assertEqual(parts[m['media'].removeprefix('attach://')],photos[i][1])
            else: self.assertEqual(parts['photo'],photos[0][1])
    def test_invalid_counts_and_api_error(self):
        for photos in ([],[('p.jpg',b'p')]*11):
            with self.assertRaises(ValueError): telegram_album.send('',1,photos,'')
        with patch('telegram_album.urllib.request.urlopen',return_value=io.BytesIO(b'{"ok":false}')):
            with self.assertRaises(RuntimeError): telegram_album.send('https://example.invalid/',1,[('p.jpg',b'p')],'')


class AlbumViewTests(unittest.TestCase):
    setUp=fixtures.SiteTests.setUp
    tearDown=fixtures.SiteTests.tearDown
    ready=fixtures.SiteTests.ready
    r=fixtures.SiteTests.r
    submit=fixtures.SiteTests.submit
    cb=fixtures.SiteTests.cb
    def test_whole_album_access_empty_and_management(self):
        ident=self.ready();site.save_photo(self.db,456,123,ident,10001,b'photo2','.jpg');self.submit(ident)
        self.cb(f'site:photos:{ident}',789)
        self.assertEqual(len(self.api.albums),1);self.assertEqual(len(self.api.albums[0][1]),2)
        self.assertFalse(self.api.files)
        self.assertFalse(any(v.startswith('site:photo:') for _,v in self.api.messages[-1][2]))
        self.cb(f'site:photos:{ident}',654);self.assertEqual(len(self.api.albums),1)
        self.cb(f'site:photomanage:{ident}',123)
        self.assertTrue(any(v.startswith('site:photo:') for _,v in self.api.messages[-1][2]))
        self.cb(f'site:photomanage:{ident}',789)
        self.assertFalse(any(v.startswith('site:photo:') for _,v in self.api.messages[-1][2]))
        for p in self.db.execute('SELECT id FROM site_photos WHERE report_id=?',(ident,)).fetchall():
            site.remove_photo(self.db,123,123,ident,p[0],self.r(ident)['version'])
        self.cb(f'site:photos:{ident}',789)
        self.assertEqual(len(self.api.albums),1);self.assertIn('нет фотографий',self.api.messages[-1][1])
    def test_background_guard_rechecks_role(self):
        worker=background.GuardedAPI(self.api,self.db,789,123,'investor')
        worker.album(789,[('p.jpg',b'p')],'')
        self.db.execute('UPDATE users SET active=0 WHERE id=789');self.db.commit()
        with self.assertRaises(background.AccessChanged): worker.album(789,[('p.jpg',b'p')],'')
