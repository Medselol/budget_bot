"""Bounded, read/report jobs and independent maintenance; no ledger mutations in jobs."""
import logging
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
import bot as ledger
from audit_log import Connection, register


@contextmanager
def measure(event, min_ms=0):
    started = time.perf_counter()
    ok = False
    try:
        yield
        ok = True
    finally:
        elapsed = (time.perf_counter()-started)*1000
        if not ok or elapsed >= min_ms:
            logging.info('PERF event=%s ms=%.1f ok=%s', event, elapsed, ok)


def connection(path):
    # Schema initialization stays exclusively in the startup/main thread.
    db = sqlite3.connect(path, timeout=5, factory=Connection)
    register(db)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


class AccessChanged(Exception): pass


class GuardedAPI:
    def __init__(self, api, db, uid, owner, role):
        self.api,self.db,self.uid,self.owner,self.role = api,db,uid,owner,role
    def check(self):
        if ledger.role_for(self.db,self.uid,self.owner) != self.role:
            raise AccessChanged()
    def send(self, chat, text, options=None):
        self.check()
        if chat != self.uid: raise AccessChanged()
        options=list(options or [])
        if not any(value=='menu' for _,value in options):
            options.append(('Главное меню','menu'))
        return self.api.send(chat,text,options)
    def document(self, chat, name, content):
        self.check()
        if chat != self.uid: raise AccessChanged()
        return self.api.document(chat,name,content)
    def album(self, chat, photos, caption):
        self.check()
        if chat != self.uid: raise AccessChanged()
        return self.api.album(chat,photos,caption)
    def call(self, method, payload):
        self.check()
        return self.api.call(method,payload)


class Runtime:
    def __init__(self, api, path, owner, sync=None):
        self.api,self.path,self.owner,self.sync = api,path,owner,sync
        self.stop = threading.Event()
        self.jobs = queue.Queue(maxsize=8)
        self.pending = set()
        self.lock = threading.Lock()
        self.threads = []
    def start(self):
        targets = [self._jobs, self._maintenance, self._notifications]
        if self.sync: targets.append(self._sync)
        for target in targets:
            thread=threading.Thread(target=target,daemon=True)
            self.threads.append(thread);thread.start()
    def submit(self, uid, role, kind, work):
        with self.lock:
            if uid in self.pending: return 'pending'
            if self.stop.is_set(): return 'full'
            self.pending.add(uid)
            try: self.jobs.put_nowait((uid,role,kind,work,time.perf_counter()))
            except queue.Full:
                self.pending.discard(uid);return 'full'
        return 'queued'
    def _jobs(self):
        db=connection(self.path)
        try:
            while not self.stop.is_set():
                try: uid,role,kind,work,queued=self.jobs.get(timeout=.2)
                except queue.Empty: continue
                api=GuardedAPI(self.api,db,uid,self.owner,role)
                try:
                    api.check()
                    logging.info('PERF event=job_wait kind=%s ms=%.1f',kind,(time.perf_counter()-queued)*1000)
                    with measure(kind): work(api,db)
                except AccessChanged:
                    logging.info('Background delivery cancelled after access change')
                except Exception as exc:
                    db.rollback()
                    logging.error('Background job failed kind=%s error=%s',kind,type(exc).__name__)
                    try: api.send(uid,'Не удалось подготовить файл. Попробуй ещё раз.', [('Главное меню','menu')])
                    except Exception: pass
                finally:
                    if db.in_transaction: db.rollback()
                    with self.lock: self.pending.discard(uid)
                    self.jobs.task_done()
        finally: db.close()
    def _sync(self):
        db=connection(self.path)
        try:
            delay=0
            while not self.stop.wait(delay):
                try:
                    with measure('sheets_sync', min_ms=50): ok=self.sync(db)
                    delay=2 if ok else 30
                except Exception as exc:
                    db.rollback();delay=30
                    logging.error('Background sync failed error=%s',type(exc).__name__)
        finally: db.close()
    def _maintenance(self):
        from maintenance import Scheduler
        db=connection(self.path);scheduler=Scheduler()
        try:
            while not self.stop.is_set():
                try:
                    with measure('maintenance'): scheduler.tick(self.api,db,self.owner)
                except Exception as exc:
                    db.rollback()
                    logging.error('Background maintenance failed error=%s',type(exc).__name__)
                if self.stop.wait(60): break
        finally: db.close()
    def close(self):
        self.stop.set()
        for thread in self.threads: thread.join(timeout=1)

    def _notifications(self):
        from request_notifications import deliver
        from site_notifications import deliver as deliver_site
        db=connection(self.path)
        try:
            while not self.stop.is_set():
                for delivery in (deliver,deliver_site):
                    try: delivery(self.api,db,self.owner)
                    except Exception as exc:
                        db.rollback()
                        logging.error('Notification worker failed error=%s',type(exc).__name__)
                if self.stop.wait(2): break
        finally: db.close()


def defer(api, db, chat, kind, work):
    runtime=getattr(api,'background',None)
    if runtime is None: return False
    role=ledger.role_for(db,chat,runtime.owner)
    if role is None: return True
    ready=threading.Event()
    def after_notice(worker, conn):
        ready.wait()
        worker.check()
        return work(worker,conn)
    status=runtime.submit(chat,role,kind,after_notice)
    text={'queued':'Готовлю файл в фоне. Можно пользоваться меню — файл придёт сюда.',
          'pending':'Твой предыдущий файл ещё готовится. Можно пользоваться меню.',
          'full':'Очередь файлов заполнена. Попробуй чуть позже.'}[status]
    try: api.send(chat,text,[('Главное меню','menu')])
    finally: ready.set()
    return True
