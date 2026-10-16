"""All receiver fixtures are synthetic; no services or credentials are used."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import bot
import control_data
import controls
import receipts
from test_multiuser import FakeBot
from vita_export_server import ExportServer, snapshot as export_snapshot
import vv_project_mirror as vv

WRITE_TOKEN = 'synthetic_write_only_' + 'a' * 32
READ_TOKEN = 'synthetic_read_only_' + 'b' * 32


def row(op=101, root=None, reversal=None, **changes):
    result = dict(operation_id=op, root_operation_id=root or op, reversal_operation_id=reversal,
                  deleted=reversal is not None, amount_minor=250050, currency='UAH',
                  category='Материалы', cost_type='materials', day='2026-10-01',
                  comment='Synthetic project-only fixture', payer='denis', cashbox='common',
                  created_at='2026-10-01T08:00:00+00:00')
    result.update(changes)
    return result


def payload(records=None, cursor=101, **changes):
    result = dict(version=vv.VERSION, source_instance='synthetic-vv', generation=1,
                  project_mapping={'denis': 7, 'igor': 8}, after_id=100, cursor=cursor,
                  records=[row()] if records is None else records,
                  captured_at=datetime.now(timezone.utc).isoformat())
    result.update(changes)
    result['fingerprint'] = vv.fingerprint(result)
    return result


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'synthetic.db'
        self.db = bot.connect(self.path, 123, (456,))
        self.db.executemany('INSERT INTO projects(id,owner_id,name) VALUES (?,?,?)',
                            [(7,123,'Synthetic Vita Denis'),(8,456,'Synthetic Vita Igor')])
        self.db.commit()
        self.scope = vv.Scope('synthetic-vv',1,{'denis':7,'igor':8},100)
        self.server = None
        self.export_server = None

    def tearDown(self):
        if self.server: self.server.stop()
        if self.export_server: self.export_server.stop()
        self.db.close()
        self.tmp.cleanup()

    def apply(self, value):
        return vv.apply_snapshot(self.db, value, self.scope)

    def state(self):
        return {table: [tuple(r) for r in self.db.execute('SELECT * FROM ' + table)]
                for table in ('operations','accounts','projects','audit_log','vv_mirror_state',
                              'vv_project_expenses','vv_mirror_audit','settings')}

    def rejected(self, value, code=None):
        before = self.state()
        with self.assertRaises(vv.SnapshotError) as caught:
            self.apply(value)
        if code: self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.state(), before)

    def test_idempotent_create_and_durable_restart(self):
        p = payload()
        ack = self.apply(p)
        self.assertEqual(ack, dict(version='vv-project-ack-v1',cursor=101,fingerprint=p['fingerprint'],count=1))
        before = self.state()
        self.assertEqual(self.apply(payload()), ack)
        self.assertEqual(self.state(), before)
        self.db.close()
        self.db = bot.connect(self.path,123,(456,))
        self.assertEqual(self.apply(payload()), ack)
        self.assertEqual(len(vv.report_rows(self.db)),1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM vv_mirror_audit').fetchone()[0],2)

    def test_correction_tombstones_and_cancellation_preserve_ids(self):
        self.apply(payload())
        p = payload([row(reversal=102),row(103,101,amount_minor=125099)],103)
        self.apply(p)
        active = vv.report_rows(self.db)
        self.assertEqual([(r['id'],r['root_operation_id'],r['amount_kop']) for r in active], [('VV:103',101,125099)])
        self.apply(payload([row(reversal=102),row(103,101,104,amount_minor=125099)],104))
        self.assertEqual(vv.report_rows(self.db),[])
        stored = self.db.execute('SELECT operation_id,reversal_operation_id,deleted FROM vv_project_expenses ORDER BY operation_id').fetchall()
        self.assertEqual([tuple(r) for r in stored],[(101,102,1),(103,104,1)])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM vv_mirror_audit WHERE action='tombstone'").fetchone()[0],2)

    def test_original_uah_and_payer_projects_are_preserved(self):
        self.apply(payload([row(),row(102,payer='igor',amount_minor=123456789,currency='USD')],102))
        rows = vv.report_rows(self.db)
        self.assertEqual([(r['amount_kop'],r['currency'],r['project_id']) for r in rows],[(250050,'UAH',7),(123456789,'USD',8)])
        self.assertEqual(len(vv.report_rows(self.db,project_id=8)),1)
        self.assertEqual(vv.report_rows(self.db,start='2026-10-02'),[])
        self.assertEqual(vv.report_rows(self.db,end='2026-09-30'),[])

    def test_scope_and_lineage_are_pinned_and_fail_closed(self):
        self.apply(payload())
        for changes in ({'source_instance':'other'}, {'generation':2}, {'after_id':99},
                        {'project_mapping':{'denis':8,'igor':7}}, {'project_mapping':{'denis':7,'igor':True}}):
            self.rejected(payload(cursor=102,**changes),'scope_conflict')
        other = vv.Scope('other',2,{'denis':7,'igor':8},100)
        with self.assertRaisesRegex(vv.SnapshotError,'lineage_conflict'):
            vv.apply_snapshot(self.db,payload(source_instance='other',generation=2),other)

    def test_stale_and_same_cursor_changed_content_rejected(self):
        p = payload()
        self.apply(p)
        self.rejected(payload([],100),'stale_cursor')
        self.rejected(payload([row(amount_minor=42)]),'cursor_content_conflict')
        self.rejected(payload(cursor=102,captured_at='2026-10-01T00:00:00Z'),'stale_snapshot')
        self.rejected(payload(cursor=102,captured_at='2099-01-01T00:00:00Z'),'future_snapshot')

    def test_tamper_missing_record_and_immutable_change_rejected(self):
        self.apply(payload())
        tampered = payload(cursor=102);tampered['records'][0]['comment']='tampered'
        self.rejected(tampered,'fingerprint_mismatch')
        self.rejected(payload([],102),'missing_historical_record')
        for key,value in (('payer','igor'),('amount_minor',1),('currency','USD'),('day','2026-10-02'),('comment','new'),('cashbox','vlad'),('category','Прочее'),('cost_type','work'),('created_at','2026-10-02T00:00:00Z')):
            self.rejected(payload([row(**{key:value})],102),'immutable_record_conflict')

    def test_undelete_and_reversal_identity_change_rejected(self):
        self.apply(payload([row(reversal=102)],102))
        self.rejected(payload(cursor=103),'tombstone_conflict')
        self.rejected(payload([row(reversal=103)],103),'tombstone_conflict')

    def test_missing_historical_changes_rejected(self):
        self.apply(payload([],110))
        self.rejected(payload(cursor=111),'historical_record_conflict')
        self.apply(payload([row(111)],111))
        self.apply(payload([row(111)],115))
        self.rejected(payload([row(111,reversal=114)],116),'historical_reversal_conflict')

    def test_invalid_lineage_and_multiple_active_versions_rejected(self):
        cases = [([row(),row(102,101)],102,'multiple_active_versions'),
                 ([row(102,101)],102,'orphan_root'),
                 ([row(reversal=103),row(102,101)],103,'invalid_correction_lineage'),
                 ([row(reversal=103),row(102,reversal=103)],103,'duplicate_reversal'),
                 ([row(reversal=102),row(102)],102,'reversal_identity_conflict')]
        for records,cursor,code in cases:
            self.rejected(payload(records,cursor),code)

    def test_schema_numeric_bounds_and_unknown_payer(self):
        for changes in ({'operation_id':True},{'root_operation_id':100},{'amount_minor':0},
                        {'amount_minor':10**12+1},{'currency':'EUR'},{'payer':'unknown'},
                        {'cashbox':'other'},{'category':'x'*101},{'comment':'x'*2001},
                        {'created_at':'not a timestamp'},{'day':'2026-02-30'},
                        {'deleted':1},{'deleted':True},{'reversal_operation_id':101}):
            self.rejected(payload([row(**changes)]))
        self.apply(payload([row(amount_minor=10**12)]))
        self.assertEqual(vv.report_rows(self.db)[0]['amount_kop'],10**12)

    def test_duplicate_unsorted_and_extra_fields_rejected(self):
        self.rejected(payload([row(),row()]))
        self.rejected(payload([row(102),row()],102))
        self.rejected(payload([row(account_id=1)]))
        self.rejected(payload(extra='unknown'))
        self.rejected(payload(cursor=True))
        self.rejected(payload([],99))
        self.rejected(payload([row(origin='vita')]))

    def test_json_duplicate_keys_nonfinite_and_unicode_rejected(self):
        for raw in (b'{"x":1,"x":2}',b'{"x":NaN}',b'\xff',b'[]\x00'):
            with self.assertRaises(vv.SnapshotError):vv.decode(raw)
        surrogate = payload([row(comment='bad')]);surrogate['records'][0]['comment']='\ud800'
        with self.assertRaises(vv.SnapshotError):vv.validate(surrogate,self.scope)

    def test_snapshot_limits_and_deadline_are_atomic(self):
        with patch.object(vv,'MAX_ROWS',0):self.rejected(payload(),'invalid_record_count')
        with patch.object(vv,'MAX_BYTES',30):self.rejected(payload(),'invalid_size')
        before = self.state()
        with self.assertRaisesRegex(vv.SnapshotError,'deadline'):
            vv.apply_snapshot(self.db,payload(),self.scope,deadline=time.monotonic()-1)
        self.assertEqual(self.state(),before)

    def test_concurrent_snapshots_never_regress_cursor(self):
        import threading
        barrier = threading.Barrier(2)
        errors = []
        def apply(value):
            connection = sqlite3.connect(self.path, timeout=3)
            connection.row_factory = sqlite3.Row
            try:
                barrier.wait()
                vv.apply_snapshot(connection, value, self.scope)
            except vv.SnapshotError as exc:
                errors.append(exc.code)
            finally:
                connection.close()
        older=payload()
        newer=payload([row(),row(102)],102)
        threads=[threading.Thread(target=apply,args=(value,)) for value in (older,newer)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertTrue(all(code=='stale_cursor' for code in errors))
        self.assertEqual(self.db.execute('SELECT cursor FROM vv_mirror_state').fetchone()[0],102)
        self.assertEqual(len(vv.report_rows(self.db)),2)

    def test_storage_error_rolls_back_whole_snapshot(self):
        self.db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON vv_project_expenses WHEN NEW.operation_id=102 BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        self.db.commit()
        before = self.state()
        with self.assertRaises(sqlite3.IntegrityError):self.apply(payload([row(),row(102)],102))
        self.assertEqual(self.state(),before)

    def test_sql_immutable_audit_and_mirror_constraints(self):
        self.apply(payload())
        for sql in ('DELETE FROM vv_project_expenses','UPDATE vv_project_expenses SET payload_json=\'{}\'',
                    'DELETE FROM vv_mirror_audit','UPDATE vv_mirror_audit SET cursor=0'):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.db:self.db.execute(sql)

    def test_no_cash_debit_no_native_operations_and_no_export_echo(self):
        account = bot.names(self.db,'accounts',123)[0]['id']
        bot.record(self.db,dict(kind='income',occurred_on='2026-10-01',project_id=7,account_id=account,amount_kop=1000000,currency='UAH'),1,123)
        bot.record(self.db,dict(kind='expense',occurred_on='2026-10-01',project_id=7,account_id=account,amount_kop=10000,currency='UAH'),2,123)
        native = bot.rows_for(self.db)
        cash = bot.balances(self.db,123)
        before = json.loads(export_snapshot(self.path,[7,8],'source',1))
        self.apply(payload())
        after = json.loads(export_snapshot(self.path,[7,8],'source',1))
        self.assertEqual(before['fingerprint'],after['fingerprint'])
        self.assertEqual(before['cursor'],after['cursor'])
        self.assertEqual(bot.balances(self.db,123),cash)
        self.assertEqual([dict(r) for r in bot.rows_for(self.db)],[dict(r) for r in native])
        self.assertEqual(len(bot.report_rows_for(self.db,user_id=123)),2)
        totals = bot.summary(bot.report_rows_for(self.db))['UAH']
        self.assertEqual((totals['expense'],totals['mirrored_expense'],totals['cash_net']),(260050,250050,990000))
        self.assertIsNone(bot.operation_row(self.db,'VV:101',123))
        with self.assertRaises(ValueError):bot.update_operation(self.db,dict(kind='expense',occurred_on='2026-10-01',amount_kop=1), 'VV:101',123)

    def test_aggregate_report_csv_pdf_receipts_and_budgets(self):
        self.apply(payload())
        text,rows,name = bot.report_text(self.db,None,None,None)
        self.assertIn('В том числе VV: 2 500.50 грн',text)
        self.assertIn('Изменение денег: 0.00 грн',text)
        self.assertIn('VV · исправления только в VV',bot.csv_report(rows,None,None,name).decode())
        self.assertIn('VV:101',bot.csv_report(rows,None,None,name).decode())
        self.assertEqual(bot.report_rows_for(self.db,user_id=123),[])
        self.assertEqual(bot.report_rows_for(self.db,user_id=456),[])
        api = FakeBot()
        enriched = receipts.enrich(api,self.db,rows)
        self.assertNotIn('receipt_url',enriched[0])
        self.assertIn('VV',enriched[0]['receipt_label'])
        from pdf_report import pdf_report
        self.assertTrue(pdf_report(enriched,None,None,name,'Общий отчёт').startswith(b'%PDF-'))
        self.assertEqual(dict((s,a) for s,_,a in control_data.budget_rows(self.db,'UAH'))['Материалы'],250050)
        self.assertIn('В факте учтено VV:',controls.budget_text(self.db,'UAH'))
        from datetime import date
        weekly = control_data.weekly_text(self.db,date(2026,10,4))
        self.assertIn('В том числе VV:',weekly)
        self.assertIn('Изменение денег: 0.00 грн',weekly)
        bot.handle_callback(api,self.db,123,123,999,'csvall:0:0',123)
        self.assertIn('VV:101',api.documents[-1][1])

    def test_no_implicit_cutoff_or_token_reuse(self):
        with patch.object(vv.VVReceiverServer,'start',side_effect=AssertionError('must not bind')):
            self.assertIsNone(vv.start_optional_receiver(self.path,{}))
            self.assertIsNone(vv.start_optional_receiver(self.path,{'VITA_VV_WRITE_ENABLED':'1'}))
            env = dict(VITA_VV_WRITE_ENABLED='1',VITA_VV_WRITE_TOKEN=WRITE_TOKEN,VITA_VV_SOURCE_INSTANCE='synthetic-vv',VITA_VV_SOURCE_GENERATION='1',VITA_VV_PROJECT_MAPPING='{"denis":7,"igor":8}')
            self.assertIsNone(vv.start_optional_receiver(self.path,env))
        with self.assertRaises(ValueError):vv.VVReceiverServer(self.path,token=READ_TOKEN,export_token=READ_TOKEN,scope=self.scope)
        for mapping in ({'denis':7},{'denis':7,'igor':7},{'denis':7,'igor':True},{'denis':7,'igor':0},{'denis':7,'igor':8,'other':9}):
            with self.assertRaises(ValueError):vv.Scope('synthetic-vv',1,mapping,100)

    def test_nonexistent_database_and_missing_projects_never_created(self):
        missing = self.path.parent/'never-created.db'
        with self.assertRaises(sqlite3.OperationalError):vv.VVReceiverServer(missing,token=WRITE_TOKEN,scope=self.scope).start()
        self.assertFalse(missing.exists())
        other = vv.Scope('synthetic-vv',1,{'denis':7,'igor':9},100)
        with self.assertRaises(ValueError):vv.VVReceiverServer(self.path,token=WRITE_TOKEN,scope=other).start()

    def start(self):
        self.server = vv.VVReceiverServer(self.path,token=WRITE_TOKEN,export_token=READ_TOKEN,
                                         scope=self.scope,host='127.0.0.1',port=0).start()

    def request(self, body=None, path='/v1/vv-expenses', token=WRITE_TOKEN, method='POST', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1',self.server.server.server_port,timeout=3)
        data = vv.canonical(payload()).encode() if body is None else body
        headers = headers if headers is not None else {'Authorization':'Bearer '+token,'Content-Type':'application/json'}
        connection.request(method,path,body=data,headers=headers)
        response = connection.getresponse();status=response.status;raw=response.read();connection.close()
        return status,raw

    def test_http_auth_path_verb_and_read_export_stays_read_only(self):
        self.start()
        self.assertEqual(self.request(token=READ_TOKEN)[0],401)
        self.assertEqual(self.request(path='/v1/vv-expenses?project_id=9')[0],404)
        for method in ('GET','PUT','PATCH','DELETE'):self.assertEqual(self.request(method=method)[0],405)
        status,data = self.request()
        self.assertEqual(status,200)
        self.assertEqual(json.loads(data)['count'],1)
        self.export_server = ExportServer(self.path,token=READ_TOKEN,ids=[7,8],instance='source',generation=1,host='127.0.0.1',port=0).start()
        conn = http.client.HTTPConnection('127.0.0.1',self.export_server.server.server_port,timeout=3)
        conn.request('POST','/v1/vv-expenses',body=vv.canonical(payload()).encode('utf-8'),headers={'Authorization':'Bearer '+READ_TOKEN})
        response=conn.getresponse();self.assertEqual(response.status,405);response.read();conn.close()

    def test_http_duplicate_auth_and_framing_rejected(self):
        self.start()
        for header,expected in ((f'Authorization: Bearer {WRITE_TOKEN}\r\n',401),('Content-Length: 1\r\n',400),('Transfer-Encoding: chunked\r\n',400)):
            sock=socket.create_connection(('127.0.0.1',self.server.server.server_port),timeout=3)
            request=(f'POST /v1/vv-expenses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {WRITE_TOKEN}\r\nContent-Type: application/json\r\nContent-Length: 2\r\n'+header+'\r\n{}')
            sock.sendall(request.encode());status=sock.recv(4096).splitlines()[0];sock.close()
            self.assertIn(str(expected).encode(),status)
        self.assertEqual(self.request(body=b'not-json')[0],400)
        self.assertEqual(self.request(headers={'Authorization':'Bearer '+WRITE_TOKEN})[0],400)
        with patch.object(vv,'MAX_BYTES',10):self.assertEqual(self.request()[0],413)

    def test_errors_do_not_disclose_secrets(self):
        self.start()
        with patch.object(vv,'apply_snapshot',side_effect=RuntimeError(WRITE_TOKEN)):
            status,data=self.request()
        self.assertEqual(status,503);self.assertNotIn(WRITE_TOKEN.encode(),data)

    def test_slow_request_has_wall_clock_deadline(self):
        with patch.object(vv,'REQUEST_SECONDS',0.15):
            self.start()
            sock=socket.create_connection(('127.0.0.1',self.server.server.server_port),timeout=1)
            sock.sendall(b'POST /v1/vv-expenses HTTP/1.1\r\nX-Slow: ')
            began=time.monotonic()
            for _ in range(20):
                try:sock.sendall(b'x')
                except OSError:break
                time.sleep(0.025)
            try:sock.recv(4096)
            except OSError:pass
            sock.close()
            self.assertLess(time.monotonic()-began,1)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM vv_project_expenses').fetchone()[0],0)


if __name__ == '__main__':unittest.main()
