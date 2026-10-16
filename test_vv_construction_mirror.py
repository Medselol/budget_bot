"""Synthetic construction stream, isolated from v1, native cash and exports."""
from copy import deepcopy
import http.client
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

import bot
import vv_project_mirror as vv
import test_vv_project_mirror as fixtures
from test_vv_project_mirror import payload as legacy_payload, row, WRITE_TOKEN, READ_TOKEN
from vita_export_server import snapshot as export_snapshot


def payload(records=None, cursor=1, **changes):
    result = legacy_payload([row(1, cashbox='unknown')] if records is None else records,
                            cursor, version=vv.CONSTRUCTION_VERSION,
                            origin_namespace=vv.CONSTRUCTION_NAMESPACE, after_id=0)
    result.update(changes)
    result['fingerprint'] = vv.fingerprint(result)
    return result


class ConstructionMirrorTests(unittest.TestCase):
    setUp = fixtures.MirrorTests.setUp
    tearDown = fixtures.MirrorTests.tearDown

    def construction_scope(self, after_id=0):
        return vv.ConstructionScope('synthetic-vv', 1, {'denis':7, 'igor':8}, after_id)

    def apply(self, value):
        return vv.apply_snapshot(self.db, value, self.construction_scope())

    def state(self):
        tables = ('operations', 'accounts', 'projects', 'audit_log', 'vv_mirror_state',
                  'vv_project_expenses', 'vv_mirror_audit', 'vv_construction_mirror_state',
                  'vv_construction_project_expenses', 'vv_construction_mirror_audit', 'settings')
        return {table: [tuple(r) for r in self.db.execute('SELECT * FROM ' + table)] for table in tables}

    def rejected(self, value, code=None):
        before = self.state()
        with self.assertRaises(vv.SnapshotError) as caught:
            self.apply(value)
        if code:
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.state(), before)

    def test_unknown_source_payer_currency_and_namespace_preserved(self):
        p = payload([row(1, cashbox='unknown'), row(2, payer='igor', cashbox='vlad', currency='USD')], 2)
        ack = self.apply(p)
        self.assertEqual(ack, dict(version='vv-project-ack-v2', origin_namespace='construction_cash',
                                  cursor=2, count=2, fingerprint=p['fingerprint']))
        rows = vv.report_rows(self.db)
        self.assertEqual([(r['id'],r['cashbox'],r['project_id'],r['currency']) for r in rows],
                         [('VV:construction_cash:1','unknown',7,'UAH'),
                          ('VV:construction_cash:2','vlad',8,'USD')])
        self.assertTrue(all(r['origin_namespace']=='construction_cash' and r['project_only'] for r in rows))

    def test_overlapping_ids_and_independent_cursor_do_not_modify_v1(self):
        vv.apply_snapshot(self.db, legacy_payload(), self.scope)
        before = {k:v for k,v in self.state().items() if k.startswith('vv_') and not k.startswith('vv_construction_')}
        self.apply(payload([row(101,cashbox='unknown')],101))
        after = self.state()
        self.assertEqual({k:after[k] for k in before}, before)
        self.assertEqual([r['id'] for r in vv.report_rows(self.db)], ['VV:101','VV:construction_cash:101'])
        self.apply(payload([row(101,cashbox='unknown'),row(102,cashbox='valera')],102))
        self.assertEqual(self.db.execute('SELECT cursor FROM vv_mirror_state').fetchone()[0],101)

    def test_fresh_construction_starts_below_existing_personal_watermark(self):
        vv.apply_snapshot(self.db, legacy_payload(cursor=500), self.scope)
        self.apply(payload())
        self.assertEqual(self.db.execute('SELECT cursor FROM vv_construction_mirror_state').fetchone()[0],1)
        self.assertEqual(len(vv.report_rows(self.db)),2)

    def test_exact_retry_restart_and_schema_init_do_not_mutate_rows(self):
        p = payload()
        ack = self.apply(p)
        before = self.state()
        self.assertEqual(self.apply(payload()), ack)
        self.assertEqual(self.state(), before)
        self.db.close()
        self.db = bot.connect(self.path,123,(456,))
        self.assertEqual(self.apply(payload()), ack)
        after=self.state()
        self.assertEqual({k:v for k,v in after.items() if k!='settings'},
                         {k:v for k,v in before.items() if k!='settings'})
        self.assertEqual(dict(after['settings'])['ledger_revision'],dict(before['settings'])['ledger_revision'])

    def test_correction_void_and_funding_source_fix_keep_lineage(self):
        self.apply(payload())
        before=row(1,reversal=2,cashbox='unknown')
        revised=row(3,root=1,cashbox='vlad',amount_minor=900,currency='USD')
        self.apply(payload([before,revised],3))
        rows=vv.report_rows(self.db)
        self.assertEqual([(r['id'],r['root_operation_id'],r['cashbox']) for r in rows],
                         [('VV:construction_cash:3',1,'vlad')])
        revised=dict(revised,deleted=True,reversal_operation_id=4)
        self.apply(payload([before,revised],4))
        self.assertEqual(vv.report_rows(self.db),[])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM vv_construction_project_expenses').fetchone()[0],2)
        self.rejected(payload([before,dict(revised,deleted=False,reversal_operation_id=None)],5),'tombstone_conflict')

    def test_invalid_namespace_cashbox_scope_and_old_protocol_fail_closed(self):
        for change in ({'origin_namespace':'personal_ledger'}, {'source_instance':'other'},
                       {'generation':2}, {'after_id':1}, {'project_mapping':{'denis':8,'igor':7}},
                       {'version':vv.VERSION}):
            self.rejected(payload(**change))
        self.rejected(payload([row(1,cashbox='other')]))
        with self.assertRaises(vv.SnapshotError):
            vv.apply_snapshot(self.db,payload(),self.scope)
        with self.assertRaises(vv.SnapshotError):
            vv.apply_snapshot(self.db,legacy_payload([row(cashbox='unknown')]),self.scope)

    def test_missing_history_immutable_change_stale_and_broken_lineage_rejected(self):
        self.apply(payload())
        self.rejected(payload([],2),'missing_historical_record')
        self.rejected(payload([row(1,cashbox='vlad')],2),'immutable_record_conflict')
        self.rejected(payload([],0),'stale_cursor')
        self.rejected(payload([row(1,cashbox='unknown'),row(2,root=1)],2),'multiple_active_versions')
        self.rejected(payload([row(1,reversal=3,cashbox='unknown'),row(2,root=1)],3),'invalid_correction_lineage')
        self.rejected(payload([row(1,cashbox='unknown')],1,origin_namespace='construction_cash_v2'))

    def test_sql_tombstones_and_audit_are_immutable(self):
        self.apply(payload())
        for query in ('DELETE FROM vv_construction_project_expenses',
                      "UPDATE vv_construction_project_expenses SET payload_json='{}'",
                      'DELETE FROM vv_construction_mirror_audit',
                      'UPDATE vv_construction_mirror_audit SET cursor=0'):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.db:self.db.execute(query)

    def test_atomic_rollback_leaves_both_namespaces_and_native_cash_intact(self):
        vv.apply_snapshot(self.db,legacy_payload(),self.scope)
        before=self.state()
        self.db.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON vv_construction_project_expenses WHEN NEW.operation_id=2 BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply(payload([row(1),row(2)],2))
        self.assertEqual(self.state(),before)

    def test_no_native_cash_operations_personal_visibility_or_export_echo(self):
        account=bot.names(self.db,'accounts',123)[0]['id']
        bot.record(self.db,dict(kind='income',occurred_on='2026-10-01',project_id=7,account_id=account,
                               amount_kop=1000000,currency='UAH'),1,123)
        before=json.loads(export_snapshot(self.path,[7,8],'source',1))
        native=[dict(r) for r in bot.rows_for(self.db)]
        cash=bot.balances(self.db,123)
        self.apply(payload())
        after=json.loads(export_snapshot(self.path,[7,8],'source',1))
        self.assertEqual(before['fingerprint'],after['fingerprint'])
        self.assertEqual(before['cursor'],after['cursor'])
        self.assertEqual(native,[dict(r) for r in bot.rows_for(self.db)])
        self.assertEqual(cash,bot.balances(self.db,123))
        self.assertEqual(len(bot.report_rows_for(self.db,user_id=123)),1)
        totals=bot.summary(bot.report_rows_for(self.db))['UAH']
        self.assertEqual((totals['expense'],totals['mirrored_expense'],totals['cash_net']),
                         (250050,250050,1000000))
        self.assertIsNone(bot.operation_row(self.db,'VV:construction_cash:1',123))
        text, rows, name=bot.report_text(self.db,None,None,None)
        self.assertIn('VV:construction_cash:1',bot.csv_report(rows,None,None,name).decode())
        from pdf_report import pdf_report
        self.assertTrue(pdf_report(rows,None,None,name,'Общий отчёт').startswith(b'%PDF-'))

    def start(self, enabled):
        self.server=vv.VVReceiverServer(self.path,token=WRITE_TOKEN,export_token=READ_TOKEN,
            scope=self.scope,host='127.0.0.1',port=0,construction_after_id=0 if enabled else None).start()

    def request(self, value):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server.server_port,timeout=3)
        connection.request('POST','/v1/vv-expenses',body=vv.canonical(value).encode(),
                           headers={'Authorization':'Bearer '+WRITE_TOKEN,'Content-Type':'application/json'})
        response=connection.getresponse();data=json.loads(response.read());connection.close()
        return response.status,data

    def test_http_version_routing_enabled_accepts_both_on_same_endpoint(self):
        self.start(True)
        self.assertEqual(self.request(legacy_payload())[0],200)
        status,ack=self.request(payload())
        self.assertEqual(status,200)
        self.assertEqual(ack['origin_namespace'],'construction_cash')
        self.assertEqual(len(vv.report_rows(self.db)),2)

    def test_http_disabled_construction_rejects_without_affecting_v1(self):
        self.start(False)
        before=self.state()
        self.assertEqual(self.request(payload()),(403,{'error':'construction_stream_disabled'}))
        self.assertEqual(self.state(),before)
        self.assertEqual(self.request(legacy_payload())[0],200)

    def test_construction_scope_is_durably_pinned_on_restart(self):
        self.apply(payload())
        with self.assertRaisesRegex(ValueError,'durable lineage'):
            vv.VVReceiverServer(self.path,token=WRITE_TOKEN,scope=self.scope,
                               host='127.0.0.1',port=0,construction_after_id=1).start()

    def test_replace_cannot_overwrite_expenses_or_audit_with_recursive_triggers_off(self):
        self.db.execute('PRAGMA recursive_triggers=OFF')
        self.assertEqual(self.db.execute('PRAGMA recursive_triggers').fetchone()[0],0)
        vv.apply_snapshot(self.db,legacy_payload(),self.scope)
        self.apply(payload())
        before=self.state()
        for prefix in ('vv_','vv_construction_'):
            for table,key,changed in ((prefix+'project_expenses','operation_id','payload_json'),
                                      (prefix+'mirror_audit','id','action')):
                original=dict(self.db.execute('SELECT * FROM '+table+' ORDER BY 1 LIMIT 1').fetchone())
                # INTEGER PRIMARY KEY aliases cover explicitly supplied hidden rowids.
                for alias in (key,'rowid','_rowid_','oid'):
                    for command in ('INSERT OR REPLACE','REPLACE'):
                        candidate=dict(original)
                        candidate[alias]=candidate.pop(key)
                        if changed=='payload_json':
                            details=json.loads(candidate[changed]);details['amount_minor']=999
                            candidate[changed]=vv.canonical(details)
                        else:
                            candidate[changed]='tampered'
                        columns=list(candidate)
                        sql=command+' INTO '+table+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')'
                        with self.subTest(table=table,alias=alias,command=command), self.assertRaises(sqlite3.IntegrityError):
                            with self.db:self.db.execute(sql,tuple(candidate.values()))
                        self.assertEqual(self.state(),before)
        self.assertEqual([r['amount_kop'] for r in vv.report_rows(self.db)],[250050,250050])

    def test_replace_cannot_resurrect_tombstones_or_change_native_export(self):
        self.db.execute('PRAGMA recursive_triggers=OFF')
        vv.apply_snapshot(self.db,legacy_payload([row(reversal=102)],102),self.scope)
        self.apply(payload([row(1,reversal=2,cashbox='unknown')],2))
        before=self.state()
        exported_before=json.loads(export_snapshot(self.path,[7,8],'source',1))
        self.assertEqual(vv.report_rows(self.db),[])
        for table in ('vv_project_expenses','vv_construction_project_expenses'):
            original=dict(self.db.execute('SELECT * FROM '+table).fetchone())
            for alias in ('operation_id','rowid','_rowid_','oid'):
                candidate=dict(original,deleted=0,reversal_operation_id=None)
                candidate[alias]=candidate.pop('operation_id')
                sql=('INSERT OR REPLACE INTO '+table+' ('+','.join(candidate)+') VALUES ('+
                     ','.join('?' for _ in candidate)+')')
                with self.subTest(table=table,alias=alias), self.assertRaises(sqlite3.IntegrityError):
                    with self.db:self.db.execute(sql,tuple(candidate.values()))
                self.assertEqual(self.state(),before)
                self.assertEqual(vv.report_rows(self.db),[])
        exported_after=json.loads(export_snapshot(self.path,[7,8],'source',1))
        # Only capture time is allowed to vary between equivalent export reads.
        self.assertEqual({k:v for k,v in exported_before.items() if k!='captured_at'},
                         {k:v for k,v in exported_after.items() if k!='captured_at'})

    def test_collision_guards_install_on_existing_streams_without_rewriting_history(self):
        vv.apply_snapshot(self.db,legacy_payload(),self.scope)
        self.apply(payload())
        for trigger in ('vv_mirror_audit_no_replace','vv_expense_no_replace',
                        'vv_construction_mirror_audit_no_replace','vv_construction_expense_no_replace'):
            self.db.execute('DROP TRIGGER '+trigger)
        self.db.commit()
        before=self.state()
        vv.init(self.db)
        self.assertEqual(self.state(),before)
        self.assertEqual(self.apply(payload())['count'],1)
        self.assertEqual(self.state(),before)
        for table in ('vv_project_expenses','vv_construction_project_expenses',
                      'vv_mirror_audit','vv_construction_mirror_audit'):
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                with self.db:self.db.execute('INSERT OR REPLACE INTO '+table+' SELECT * FROM '+table+' LIMIT 1')
            self.assertEqual(self.state(),before)

    def test_enabling_requires_explicit_construction_cutoff(self):
        env=dict(VITA_VV_WRITE_ENABLED='1',VITA_VV_WRITE_TOKEN=WRITE_TOKEN,VITA_VV_SOURCE_INSTANCE='synthetic-vv',
                 VITA_VV_SOURCE_GENERATION='1',VITA_VV_PROJECT_MAPPING='{"denis":7,"igor":8}',
                 VITA_VV_AFTER_ID='100',VITA_VV_CONSTRUCTION_ENABLED='1')
        with patch.object(vv.VVReceiverServer,'start') as start:
            with self.assertLogs(level='ERROR'):
                self.assertIsNone(vv.start_optional_receiver(self.path,env))
            start.assert_not_called()
        with patch.object(vv.VVReceiverServer,'start') as start:
            server=vv.start_optional_receiver(self.path,dict(env,VITA_VV_CONSTRUCTION_AFTER_ID='0'))
            self.assertEqual(server.scope.after_id,100)
            self.assertEqual(server.construction_scope.after_id,0)
            start.assert_called_once()


if __name__=='__main__':unittest.main()
