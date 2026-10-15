"""Opt-in private read-only Vita snapshot endpoint. Never receives project IDs from callers."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import threading
import time

FIELDS = ('id','update_id','user_id','occurred_on','kind','project_id','category','cost_type','source','account_id','from_account_id','to_account_id','amount_kop','currency','comment','created_at','target_amount_kop','target_currency','exchange_rate')
REQUEST_SECONDS=10
MAX_ROWS=10000
MAX_BYTES=16*1024*1024


def canonical(v):
    return json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def project_ids(raw):
    if not isinstance(raw,str) or not re.fullmatch(r'[1-9][0-9]*(,[1-9][0-9]*){0,99}',raw):
        raise ValueError('Invalid project scope')
    ids=[int(v) for v in raw.split(',')]
    if len(set(ids))!=len(ids) or any(v>=2**63 for v in ids):raise ValueError('Invalid project scope')
    return sorted(ids)


def snapshot(path,ids,instance,generation):
    if not ids:raise ValueError('Empty project scope')
    deadline=time.monotonic()+REQUEST_SECONDS
    uri=Path(path).resolve().as_uri()+'?mode=ro'
    with closing(sqlite3.connect(uri,uri=True,timeout=3)) as db:
        db.row_factory=sqlite3.Row
        db.set_progress_handler(lambda: int(time.monotonic()>=deadline),1000)
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        marks=','.join('?' for _ in ids)
        found={r[0] for r in db.execute('SELECT id FROM projects WHERE id IN ('+marks+')',ids)}
        if found!=set(ids):raise ValueError('Missing project')
        cursor=db.execute('SELECT COALESCE(MAX(id),0) FROM audit_log').fetchone()[0]
        rows=[dict(r) for r in db.execute('SELECT '+','.join(FIELDS)+' FROM operations WHERE project_id IN ('+marks+') ORDER BY update_id LIMIT ?',(*ids,MAX_ROWS+1))]
        if len(rows)>MAX_ROWS:raise ValueError('Too many rows')
        p=dict(version='vita-snapshot-v1',source_instance=instance,generation=generation,cursor=cursor,project_ids=ids,rows=rows)
        p['fingerprint']=hashlib.sha256(canonical(p).encode()).hexdigest()
        p['captured_at']=datetime.now(timezone.utc).isoformat()
        if time.monotonic()>=deadline:raise TimeoutError('Snapshot deadline')
        data=canonical(p).encode()
        if time.monotonic()>=deadline:raise TimeoutError('Snapshot deadline')
        if len(data)>MAX_BYTES:raise ValueError('Snapshot too large')
        return data


class ExportServer:
    """Configuration is operator-supplied. Binding creates no public Railway domain."""
    def __init__(self,path,*,token,ids,instance,generation,host='::',port=8787):
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}',token):raise ValueError('Invalid authentication configuration')
        if not isinstance(instance,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',instance):raise ValueError('Invalid source identity')
        if type(generation) is not int or not 1<=generation<2**63:raise ValueError('Invalid source generation')
        checked=project_ids(','.join(str(v) for v in ids))
        if type(port) is not int or not 0<=port<=65535:raise ValueError('Invalid listen port')
        self.path,self.ids,self.instance,self.generation=path,checked,instance,generation
        self._token=token.encode('ascii');self.server=None;self.thread=None
        self.host,self.port=host,port

    def _handler(self):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            server_version='VitaSnapshot'
            sys_version=''
            def log_message(self,*args):pass
            def setup(self):
                super().setup();self.connection.settimeout(REQUEST_SECONDS)
                def expire():
                    try:self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    try:self.connection.close()
                    except OSError:pass
                self.deadline=threading.Timer(REQUEST_SECONDS,expire)
                self.deadline.daemon=True
                self.deadline.start()
            def finish(self):
                try:super().finish()
                finally:self.deadline.cancel()
            def reply(self,status,data=b''):
                self.send_response(status)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.send_header('Content-Length',str(len(data)))
                self.send_header('Connection','close')
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                # Reject duplicate/malformed auth, queries, encoded aliases and bodies.
                values=self.headers.get_all('Authorization') or []
                auth=values[0] if len(values)==1 else ''
                try:provided=auth.removeprefix('Bearer ').encode('ascii') if auth.startswith('Bearer ') else b''
                except UnicodeEncodeError:provided=b''
                if not hmac.compare_digest(provided,owner._token):return self.reply(401,b'{"error":"unauthorized"}')
                if self.path!='/v1/snapshot' or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length','0')!='0':
                    return self.reply(404,b'{"error":"not_found"}')
                try:data=snapshot(owner.path,owner.ids,owner.instance,owner.generation)
                except Exception:
                    # No path, SQL, token, record or exception payload in logs/responses.
                    logging.warning('Vita export unavailable')
                    return self.reply(503,b'{"error":"unavailable"}')
                return self.reply(200,data)
            def do_POST(self):self.reply(405,b'{"error":"read_only"}')
            do_PUT=do_POST
            do_DELETE=do_POST
            do_PATCH=do_POST
        return Handler

    def start(self):
        if self.thread and self.thread.is_alive():return self
        class Server(HTTPServer):
            address_family=socket.AF_INET6 if ':' in self.host else socket.AF_INET
            allow_reuse_address=True
            def handle_error(self,request,client_address):
                logging.warning('Vita export request interrupted')
        self.server=Server((self.host,self.port),self._handler())
        self.thread=threading.Thread(target=self.server.serve_forever,name='vita-export',daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown();self.server.server_close()
        if self.thread:self.thread.join(timeout=12)


def start_optional_export(path,env):
    if env.get('VITA_EXPORT_ENABLED','0')!='1':return None
    try:
        server=ExportServer(path,token=env.get('VITA_SYNC_TOKEN',''),ids=project_ids(env.get('VITA_PROJECT_IDS','')),
                            instance=env.get('VITA_SOURCE_INSTANCE',''),generation=int(env.get('VITA_SOURCE_GENERATION','0')),
                            host='::',port=int(env.get('VITA_EXPORT_PORT','8787')))
        server.start()
        logging.info('Private Vita read-only export ready')
        return server
    except Exception:
        logging.error('Vita export disabled: invalid or unavailable configuration')
        return None
