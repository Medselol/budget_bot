"""Durable, project-only VV expenses. Never write native operations or cash accounts.

This module deliberately does not import the read-only Vita export server. Mirror
rows are foreign records, editable only in VV, and never re-exported to VV.
"""
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
from pathlib import Path
import re
import socket
import sqlite3
import threading
import time

MAX_ROWS = 10000
MAX_BYTES = 16 * 1024 * 1024
REQUEST_SECONDS = 10
MAX_INTEGER = 2**63 - 1
VERSION = 'vv-project-snapshot-v1'
PROVENANCE = 'VV · исправления только в VV'
TOP_FIELDS = {'version', 'source_instance', 'generation', 'project_mapping', 'after_id',
              'cursor', 'records', 'fingerprint', 'captured_at'}
RECORD_FIELDS = {'operation_id', 'root_operation_id', 'reversal_operation_id',
                 'deleted', 'amount_minor', 'currency', 'category', 'cost_type',
                 'day', 'comment', 'payer', 'cashbox', 'created_at'}
IMMUTABLE_FIELDS = RECORD_FIELDS - {'deleted', 'reversal_operation_id'}


class SnapshotError(ValueError):
    """Public, non-sensitive failure codes only."""
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def fingerprint(payload):
    return hashlib.sha256(canonical({k: v for k, v in payload.items()
                                    if k not in ('fingerprint', 'captured_at')}).encode('utf-8')).hexdigest()


def integer(value, minimum=0, maximum=MAX_INTEGER):
    return type(value) is int and minimum <= value <= maximum


def timestamp(value, *, sqlite_utc=False):
    if not isinstance(value, str) or len(value) > 40:
        raise SnapshotError('invalid_timestamp', 400)
    # Existing VV SQLite created_at values are UTC but omit a timezone.
    if sqlite_utc and re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', value):
        value = value.replace(' ', 'T') + '+00:00'
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', value):
        raise SnapshotError('invalid_timestamp', 400)
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise SnapshotError('invalid_timestamp', 400) from None


@dataclass(frozen=True)
class Scope:
    source_instance: str
    generation: int
    project_mapping: dict
    after_id: int

    def __post_init__(self):
        if (not isinstance(self.source_instance, str) or
                not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', self.source_instance) or
                not integer(self.generation, 1) or not isinstance(self.project_mapping, dict) or
                set(self.project_mapping) != {'denis', 'igor'} or
                any(not integer(v, 1) for v in self.project_mapping.values()) or
                len(set(self.project_mapping.values())) != 2 or not integer(self.after_id)):
            raise ValueError('Invalid VV mirror scope')
        object.__setattr__(self, 'project_mapping', dict(self.project_mapping))

    def values(self):
        return (self.source_instance, self.generation, canonical(self.project_mapping), self.after_id)


def init(db):
    """Add only isolated mirror tables; safe when the optional receiver is off."""
    db.executescript('''
    CREATE TABLE IF NOT EXISTS vv_mirror_state(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        source_instance TEXT NOT NULL, generation INTEGER NOT NULL,
        project_mapping_json TEXT NOT NULL, after_id INTEGER NOT NULL,
        cursor INTEGER NOT NULL, fingerprint TEXT NOT NULL, captured_at TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS vv_project_expenses(
        operation_id INTEGER PRIMARY KEY, root_operation_id INTEGER NOT NULL,
        source_instance TEXT NOT NULL, generation INTEGER NOT NULL,
        project_id INTEGER NOT NULL REFERENCES projects(id),
        payload_json TEXT NOT NULL, deleted INTEGER NOT NULL CHECK(deleted IN (0,1)),
        reversal_operation_id INTEGER, first_cursor INTEGER NOT NULL,
        last_cursor INTEGER NOT NULL,
        CHECK((deleted=0 AND reversal_operation_id IS NULL) OR
              (deleted=1 AND reversal_operation_id>operation_id)));
    CREATE INDEX IF NOT EXISTS vv_project_expenses_root ON vv_project_expenses(root_operation_id);
    CREATE TABLE IF NOT EXISTS vv_mirror_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT, happened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        source_instance TEXT NOT NULL, generation INTEGER NOT NULL, project_id INTEGER,
        project_mapping_json TEXT NOT NULL, cursor INTEGER NOT NULL, fingerprint TEXT NOT NULL, action TEXT NOT NULL,
        operation_id INTEGER, reversal_operation_id INTEGER, active_count INTEGER);
    CREATE TRIGGER IF NOT EXISTS vv_mirror_audit_no_update BEFORE UPDATE ON vv_mirror_audit
        BEGIN SELECT RAISE(ABORT,'VV audit is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS vv_mirror_audit_no_delete BEFORE DELETE ON vv_mirror_audit
        BEGIN SELECT RAISE(ABORT,'VV audit is immutable'); END;
    CREATE TRIGGER IF NOT EXISTS vv_expense_no_delete BEFORE DELETE ON vv_project_expenses
        BEGIN SELECT RAISE(ABORT,'VV records require source tombstones'); END;
    CREATE TRIGGER IF NOT EXISTS vv_expense_immutable BEFORE UPDATE ON vv_project_expenses
        WHEN OLD.operation_id IS NOT NEW.operation_id OR OLD.root_operation_id IS NOT NEW.root_operation_id
          OR OLD.source_instance IS NOT NEW.source_instance OR OLD.generation IS NOT NEW.generation
          OR OLD.project_id IS NOT NEW.project_id OR OLD.payload_json IS NOT NEW.payload_json
          OR OLD.first_cursor IS NOT NEW.first_cursor OR NEW.last_cursor<OLD.last_cursor
          OR (OLD.deleted=1 AND (NEW.deleted!=1 OR OLD.reversal_operation_id IS NOT NEW.reversal_operation_id))
        BEGIN SELECT RAISE(ABORT,'VV records are immutable'); END;
    ''')


def decode(data):
    if not isinstance(data, bytes) or not data or len(data) > MAX_BYTES:
        raise SnapshotError('invalid_size', 413)
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise SnapshotError('duplicate_json_key', 400)
            obj[key] = value
        return obj
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, RecursionError):
        raise SnapshotError('invalid_json', 400) from None


def validate(payload, scope):
    if not isinstance(payload, dict) or set(payload) != TOP_FIELDS:
        raise SnapshotError('invalid_snapshot', 400)
    if payload['version'] != VERSION:
        raise SnapshotError('unsupported_version', 400)
    if any(type(payload[key]) is not type(getattr(scope, key)) or payload[key] != getattr(scope, key)
           for key in ('source_instance', 'generation', 'project_mapping', 'after_id')):
        raise SnapshotError('scope_conflict')
    if not integer(payload['cursor'], scope.after_id):
        raise SnapshotError('invalid_cursor', 400)
    if (any(type(v) is not int for v in payload['project_mapping'].values()) or
            canonical(payload['project_mapping']) != canonical(scope.project_mapping)):
        raise SnapshotError('scope_conflict')
    captured = timestamp(payload['captured_at'])
    if captured > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise SnapshotError('future_snapshot')
    if not isinstance(payload['fingerprint'], str) or not re.fullmatch(r'[0-9a-f]{64}', payload['fingerprint']):
        raise SnapshotError('invalid_fingerprint', 400)
    try:
        encoded = canonical(payload).encode('utf-8')
        expected = fingerprint(payload)
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        raise SnapshotError('invalid_snapshot', 400) from None
    if len(encoded) > MAX_BYTES:
        raise SnapshotError('invalid_size', 413)
    if not hmac.compare_digest(expected, payload['fingerprint']):
        raise SnapshotError('fingerprint_mismatch')
    records = payload['records']
    if not isinstance(records, list) or len(records) > MAX_ROWS:
        raise SnapshotError('invalid_record_count', 413)
    by_id, roots, reversals = {}, {}, set()
    previous_id = 0
    for row in records:
        if not isinstance(row, dict) or set(row) != RECORD_FIELDS:
            raise SnapshotError('invalid_record', 400)
        op, root, reversal = row['operation_id'], row['root_operation_id'], row['reversal_operation_id']
        if (not integer(op, scope.after_id + 1, payload['cursor']) or
                not integer(root, scope.after_id + 1, op) or op <= previous_id or
                type(row['deleted']) is not bool or
                (reversal is not None and not integer(reversal, op + 1, payload['cursor'])) or
                row['deleted'] != (reversal is not None) or
                not integer(row['amount_minor'], 1, 1_000_000_000_000) or
                row['currency'] not in ('UAH', 'USD') or row['payer'] not in ('denis', 'igor') or
                row['cashbox'] not in ('vlad', 'valera', 'common')):
            raise SnapshotError('invalid_record', 400)
        for key, limit in (('category', 100), ('cost_type', 30), ('comment', 2000)):
            if not isinstance(row[key], str) or len(row[key]) > limit or '\x00' in row[key]:
                raise SnapshotError('invalid_record', 400)
        try:
            if not isinstance(row['day'], str) or date.fromisoformat(row['day']).isoformat() != row['day']:
                raise ValueError()
        except (TypeError, ValueError):
            raise SnapshotError('invalid_record_date', 400) from None
        timestamp(row['created_at'], sqlite_utc=True)
        if reversal is not None:
            if reversal in reversals:
                raise SnapshotError('duplicate_reversal')
            reversals.add(reversal)
        by_id[op] = row
        roots.setdefault(root, []).append(row)
        previous_id = op
    if reversals & by_id.keys():
        raise SnapshotError('reversal_identity_conflict')
    for root, versions in roots.items():
        if root not in by_id or by_id[root]['root_operation_id'] != root:
            raise SnapshotError('orphan_root')
        if sum(not row['deleted'] for row in versions) > 1:
            raise SnapshotError('multiple_active_versions')
        # A correction appends a new record after reversing its predecessor.
        for before, after in zip(versions, versions[1:]):
            if not before['deleted'] or before['reversal_operation_id'] >= after['operation_id']:
                raise SnapshotError('invalid_correction_lineage')
    return by_id, captured


def apply_snapshot(db, payload, scope, *, deadline=None):
    """Full append-only snapshot reconciliation in one bounded transaction."""
    deadline = deadline if deadline is not None else time.monotonic() + REQUEST_SECONDS
    incoming, captured = validate(payload, scope)
    if time.monotonic() >= deadline:
        raise SnapshotError('deadline', 503)
    if db.in_transaction:
        raise SnapshotError('transaction_unavailable', 503)
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            found={r[0] for r in db.execute('SELECT id FROM projects WHERE id IN (?,?)', tuple(scope.project_mapping.values()))}
            if found != set(scope.project_mapping.values()):
                raise SnapshotError('project_unavailable')
            state = db.execute('SELECT * FROM vv_mirror_state WHERE singleton=1').fetchone()
            existing = {r['operation_id']: r for r in db.execute('SELECT * FROM vv_project_expenses')}
            if state:
                if tuple(state[k] for k in ('source_instance', 'generation', 'project_mapping_json', 'after_id')) != scope.values():
                    raise SnapshotError('lineage_conflict')
                if payload['cursor'] < state['cursor']:
                    raise SnapshotError('stale_cursor')
                if captured < timestamp(state['captured_at']):
                    raise SnapshotError('stale_snapshot')
                if payload['cursor'] == state['cursor'] and payload['fingerprint'] != state['fingerprint']:
                    raise SnapshotError('cursor_content_conflict')
            elif existing:
                raise SnapshotError('missing_lineage_state')
            if not existing.keys() <= incoming.keys():
                raise SnapshotError('missing_historical_record')
            changes = []
            for op, row in incoming.items():
                old = existing.get(op)
                immutable = canonical({key: row[key] for key in IMMUTABLE_FIELDS})
                if old:
                    if (old['source_instance'], old['generation'], old['project_id']) != (scope.source_instance, scope.generation, scope.project_mapping[row['payer']]) or old['payload_json'] != immutable:
                        raise SnapshotError('immutable_record_conflict')
                    if old['deleted'] and (not row['deleted'] or old['reversal_operation_id'] != row['reversal_operation_id']):
                        raise SnapshotError('tombstone_conflict')
                    if not old['deleted'] and row['deleted']:
                        if state and row['reversal_operation_id'] <= state['cursor']:
                            raise SnapshotError('historical_reversal_conflict')
                        changes.append(('tombstone', op, row, immutable))
                else:
                    if state and op <= state['cursor']:
                        raise SnapshotError('historical_record_conflict')
                    changes.append(('insert', op, row, immutable))
            count = sum(not row['deleted'] for row in incoming.values())
            ack = dict(version='vv-project-ack-v1', cursor=payload['cursor'],
                       fingerprint=payload['fingerprint'], count=count)
            if state and payload['cursor'] == state['cursor']:
                # Never turn a retry into another mutation or audit record.
                return ack
            for action, op, row, immutable in changes:
                if action == 'insert':
                    db.execute('''INSERT INTO vv_project_expenses(operation_id,root_operation_id,source_instance,
                        generation,project_id,payload_json,deleted,reversal_operation_id,first_cursor,last_cursor)
                        VALUES (?,?,?,?,?,?,?,?,?,?)''',
                        (op, row['root_operation_id'], scope.source_instance, scope.generation, scope.project_mapping[row['payer']],
                         immutable, int(row['deleted']), row['reversal_operation_id'], payload['cursor'], payload['cursor']))
                else:
                    db.execute('UPDATE vv_project_expenses SET deleted=1,reversal_operation_id=?,last_cursor=? WHERE operation_id=?',
                               (row['reversal_operation_id'], payload['cursor'], op))
                db.execute('''INSERT INTO vv_mirror_audit(source_instance,generation,project_mapping_json,project_id,cursor,fingerprint,
                    action,operation_id,reversal_operation_id) VALUES (?,?,?,?,?,?,?,?,?)''',
                    (*scope.values()[:3], scope.project_mapping[row['payer']], payload['cursor'], payload['fingerprint'], action, op, row['reversal_operation_id']))
            db.execute('''INSERT INTO vv_mirror_state(singleton,source_instance,generation,project_mapping_json,after_id,cursor,fingerprint,captured_at)
                VALUES (1,?,?,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET cursor=excluded.cursor,
                fingerprint=excluded.fingerprint,captured_at=excluded.captured_at,updated_at=CURRENT_TIMESTAMP''',
                (*scope.values(), payload['cursor'], payload['fingerprint'], payload['captured_at']))
            db.execute('''INSERT INTO vv_mirror_audit(source_instance,generation,project_mapping_json,cursor,fingerprint,action,active_count)
                VALUES (?,?,?,?,?,'snapshot',?)''', (*scope.values()[:3], payload['cursor'], payload['fingerprint'], count))
            if changes:
                db.execute("INSERT INTO settings(key,value) VALUES ('ledger_revision','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
            if time.monotonic() >= deadline:
                raise SnapshotError('deadline', 503)
        return ack
    except sqlite3.OperationalError:
        raise SnapshotError('storage_unavailable', 503) from None
    finally:
        db.set_progress_handler(None, 0)


def report_rows(db, start=None, end=None, project_id=None):
    """Read-only presentation rows with non-native identifiers and no account."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vv_project_expenses'").fetchone():
        return []
    rows = []
    for stored in db.execute('''SELECT m.*,p.name AS project FROM vv_project_expenses m
                               JOIN projects p ON p.id=m.project_id WHERE m.deleted=0'''):
        if project_id is not None and stored['project_id'] != project_id:
            continue
        row = json.loads(stored['payload_json'])
        if (start and row['day'] < start) or (end and row['day'] > end):
            continue
        rows.append(dict(id=f"VV:{row['operation_id']}", update_id=None, user_id=None,
                         occurred_on=row['day'], kind='expense', project_id=stored['project_id'],
                         project=stored['project'], category=row['category'],
                         cost_type={'materials':'Материалы', 'labor':'Работа', 'mixed':'Материалы и работа', 'other':'Прочее'}.get(row['cost_type'], row['cost_type']),
                         source='VV', account_id=None, from_account_id=None, to_account_id=None,
                         account=None, from_account=None, to_account=None, amount_kop=row['amount_minor'],
                         currency=row['currency'], comment=row['comment'], created_at=row['created_at'],
                         target_amount_kop=None, target_currency=None, exchange_rate=None,
                         user_name='VV · ' + ('Денис' if row['payer'] == 'denis' else 'Игорь'),
                         provenance=PROVENANCE, origin='vv', origin_instance=stored['source_instance'],
                         origin_generation=stored['generation'], origin_operation_id=row['operation_id'],
                         root_operation_id=row['root_operation_id'], cashbox=row['cashbox'], project_only=True))
    return rows


class VVReceiverServer:
    """Independent opt-in write listener. The export token can never authorize it."""
    def __init__(self, path, *, token, scope, export_token='', host='::', port=8788):
        if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token) or
                (export_token and hmac.compare_digest(token, export_token))):
            raise ValueError('Invalid independent write authentication')
        if not isinstance(scope, Scope) or type(port) is not int or not 0 <= port <= 65535:
            raise ValueError('Invalid receiver configuration')
        self.path, self.scope, self.host, self.port = path, scope, host, port
        self._token = token.encode('ascii')
        self.server = self.thread = None

    def _connect(self):
        db = sqlite3.connect(Path(self.path).resolve().as_uri() + '?mode=rw', uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def _handler(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            server_version = 'VitaVVReceiver'
            sys_version = ''
            def log_message(self, *args): pass
            def setup(self):
                super().setup()
                self.connection.settimeout(REQUEST_SECONDS)
                self.expires = time.monotonic() + REQUEST_SECONDS
                def expire():
                    try: self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                    try: self.connection.close()
                    except OSError: pass
                self.timer = threading.Timer(REQUEST_SECONDS, expire)
                self.timer.daemon = True
                self.timer.start()
            def finish(self):
                try: super().finish()
                finally: self.timer.cancel()
            def reply(self, status, data):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(data)
            def do_POST(self):
                auth = self.headers.get_all('Authorization') or []
                try:
                    provided = auth[0][7:].encode('ascii') if len(auth) == 1 and auth[0].startswith('Bearer ') else b''
                except UnicodeError:
                    provided = b''
                if not hmac.compare_digest(provided, owner._token):
                    return self.reply(401, b'{"error":"unauthorized"}')
                if self.path != '/v1/vv-expenses':
                    return self.reply(404, b'{"error":"not_found"}')
                lengths = self.headers.get_all('Content-Length') or []
                types = self.headers.get_all('Content-Type') or []
                if (self.headers.get_all('Transfer-Encoding') or len(lengths) != 1 or
                        not re.fullmatch(r'[1-9][0-9]{0,8}', lengths[0]) or
                        len(types) != 1 or types[0].lower() not in ('application/json', 'application/json; charset=utf-8')):
                    return self.reply(400, b'{"error":"invalid_framing"}')
                size = int(lengths[0])
                if size > MAX_BYTES:
                    return self.reply(413, b'{"error":"invalid_size"}')
                try:
                    data = self.rfile.read(size)
                    if len(data) != size:
                        raise SnapshotError('incomplete_body', 400)
                    payload = decode(data)
                    with closing(owner._connect()) as db:
                        ack = apply_snapshot(db, payload, owner.scope, deadline=self.expires)
                    return self.reply(200, canonical(ack).encode('utf-8'))
                except SnapshotError as exc:
                    return self.reply(exc.status, canonical({'error': exc.code}).encode('ascii'))
                except Exception:
                    logging.warning('VV mirror request unavailable')
                    return self.reply(503, b'{"error":"unavailable"}')
            def do_GET(self): self.reply(405, b'{"error":"write_only"}')
            do_PUT = do_GET
            do_PATCH = do_GET
            do_DELETE = do_GET
        return Handler

    def start(self):
        if self.thread and self.thread.is_alive():
            return self
        with closing(self._connect()) as db:
            found={r[0] for r in db.execute('SELECT id FROM projects WHERE id IN (?,?)', tuple(self.scope.project_mapping.values()))}
            if found != set(self.scope.project_mapping.values()):
                raise ValueError('Missing target project')
            init(db)
            state = db.execute('SELECT * FROM vv_mirror_state WHERE singleton=1').fetchone()
            if state and tuple(state[k] for k in ('source_instance', 'generation', 'project_mapping_json', 'after_id')) != self.scope.values():
                raise ValueError('Configured scope conflicts with durable lineage')
        class Server(HTTPServer):
            address_family = socket.AF_INET6 if ':' in self.host else socket.AF_INET
            allow_reuse_address = True
            def handle_error(self, request, client_address):
                logging.warning('VV mirror request interrupted')
        self.server = Server((self.host, self.port), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, name='vv-project-receiver', daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=12)


def start_optional_receiver(path, env):
    if env.get('VITA_VV_WRITE_ENABLED', '0') != '1':
        return None
    try:
        # No implicit zero cutoff: an operator must choose the bounded scope.
        def configured_int(key, default=None):
            value = env.get(key, default)
            if not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]*', value):
                raise ValueError('Missing or invalid configuration')
            return int(value)
        scope = Scope(env.get('VITA_VV_SOURCE_INSTANCE', ''), configured_int('VITA_VV_SOURCE_GENERATION'),
                      decode(env.get('VITA_VV_PROJECT_MAPPING', '').encode('utf-8')), configured_int('VITA_VV_AFTER_ID'))
        server = VVReceiverServer(path, token=env.get('VITA_VV_WRITE_TOKEN', ''), scope=scope,
                                  export_token=env.get('VITA_SYNC_TOKEN', ''),
                                  port=configured_int('VITA_VV_WRITE_PORT', '8788'))
        if env.get('VITA_EXPORT_ENABLED', '0') == '1' and server.port == configured_int('VITA_EXPORT_PORT', '8787'):
            raise ValueError('Receiver needs an independent port')
        server.start()
        logging.info('Private VV project-only receiver ready')
        return server
    except Exception:
        logging.error('VV receiver disabled: invalid or unavailable configuration')
        return None
