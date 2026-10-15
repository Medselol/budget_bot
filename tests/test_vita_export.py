import http.client,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from vita_export_server import ExportServer,FIELDS,project_ids,snapshot,start_optional_export
TOKEN='synthetic_test_only_'+'a'*32
class ExportTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'source.db';self.server=None
  with sqlite3.connect(self.path) as db:
   db.execute('CREATE TABLE projects(id INTEGER,name TEXT,owner_id INTEGER)');db.executemany('INSERT INTO projects VALUES(?,?,?)',[(7,'Вита',101),(8,'Other',202)])
   db.execute('CREATE TABLE audit_log(id INTEGER PRIMARY KEY,private_extra TEXT)');db.execute('INSERT INTO audit_log VALUES(9,"DO NOT EXPORT")')
   db.execute('CREATE TABLE operations('+','.join(f+' '+('INTEGER' if f in ('id','update_id','user_id','project_id','amount_kop','account_id') else 'TEXT') for f in FIELDS)+')')
   r=dict.fromkeys(FIELDS);r.update(user_id=101,occurred_on='2026-10-01',kind='expense',amount_kop=2500,currency='UAH',comment='synthetic only',created_at='2026-10-01 10:00:00',account_id=1)
   for n in (1,2):
    r.update(id=n,update_id=99+n,project_id=6+n);db.execute('INSERT INTO operations VALUES('+','.join('?' for _ in FIELDS)+')',[r[f] for f in FIELDS])
 def tearDown(self):
  if self.server:self.server.stop()
  self.tmp.cleanup()
 def start(self):self.server=ExportServer(self.path,token=TOKEN,ids=[7],instance='test-source',generation=1,host='127.0.0.1',port=0).start()
 def request(self,path='/v1/snapshot',token=TOKEN,method='GET',body=None):
  c=http.client.HTTPConnection('127.0.0.1',self.server.server.server_port,timeout=3);c.request(method,path,body=body,headers={'Authorization':'Bearer '+token});r=c.getresponse();data=r.read();code=r.status;c.close();return code,data
 def test_disabled_invalid_no_socket(self):
  with patch.object(ExportServer,'start',side_effect=AssertionError):
   self.assertIsNone(start_optional_export(self.path,{}));self.assertIsNone(start_optional_export(self.path,{'VITA_EXPORT_ENABLED':'1'}))
 def test_scope_and_readonly(self):
  before=self.path.read_bytes();p=json.loads(snapshot(self.path,[7],'test-source',1));self.assertEqual(len(p['rows']),1);self.assertEqual(p['rows'][0]['project_id'],7);self.assertNotIn('DO NOT EXPORT',json.dumps(p));self.assertEqual(p['cursor'],9);self.assertEqual(before,self.path.read_bytes())
 def test_auth_fixed_path_readonly_methods(self):
  self.start();self.assertEqual(self.request()[0],200);self.assertEqual(self.request(token='wrong')[0],401);self.assertEqual(self.request(path='/v1/snapshot?projects=8')[0],404)
  for method in ('POST','PUT','PATCH','DELETE'):self.assertEqual(self.request(method=method)[0],405)
  self.assertEqual(self.request(body='data')[0],404)
 def test_missing_source_project_fails(self):
  with self.assertRaises(Exception):snapshot(self.path,[9],'test-source',1)
  with self.assertRaises(Exception):snapshot(self.path.parent/'absent.db',[7],'test-source',1)
  self.assertFalse((self.path.parent/'absent.db').exists())
 def test_errors_no_secrets(self):
  self.start()
  with patch('vita_export_server.snapshot',side_effect=RuntimeError(TOKEN)):status,data=self.request()
  self.assertEqual(status,503);self.assertNotIn(TOKEN.encode(),data)
 def test_config_bounds(self):
  for v in ('','7,7','*','7 OR 1=1','0','7, 8'):
   with self.assertRaises(ValueError):project_ids(v)
  for token in ('','short','x'*31,'x'*32+'\n'):
   with self.assertRaises(ValueError):ExportServer(self.path,token=token,ids=[7],instance='test-source',generation=1)
  with patch('vita_export_server.MAX_ROWS',0):
   with self.assertRaises(ValueError):snapshot(self.path,[7],'test-source',1)
 def test_slow_headers_wallclock_and_shutdown(self):
  import socket,time,threading
  with patch('vita_export_server.REQUEST_SECONDS',0.15):
   self.start()
   c=socket.create_connection(('127.0.0.1',self.server.server.server_port));c.settimeout(1)
   c.sendall(b'GET /v1/snapshot HTTP/1.1\r\nX-Slow: ')
   # Keep the idle timeout alive, but the total request deadline must still close it.
   began=time.monotonic()
   for _ in range(20):
    try:c.sendall(b'x')
    except OSError:break
    time.sleep(0.025)
   try:response=c.recv(4096)
   except (ConnectionResetError,BrokenPipeError):response=b''
   c.close()
   self.assertLess(time.monotonic()-began,1)
   stopped=threading.Event()
   thread=threading.Thread(target=lambda:(self.server.stop(),stopped.set()),daemon=True);thread.start()
   self.assertTrue(stopped.wait(1));thread.join();self.server=None
