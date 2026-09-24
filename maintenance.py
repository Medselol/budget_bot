"""Daily verified SQLite snapshots and opt-in weekly notifications."""
import logging
import threading
from functools import wraps
import os
import sqlite3
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
import bot as ledger
import control_data as data

BACKUP_LOCK = threading.RLock()


def backup_locked(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with BACKUP_LOCK: return fn(*args, **kwargs)
    return wrapped


MAX_DOWNLOAD = 45*1024*1024


def backup_dir(db):
    filename=db.execute('PRAGMA database_list').fetchone()[2]
    if not filename: raise ValueError('Резервные копии доступны для базы на диске.')
    return Path(filename).resolve().parent / 'backups'


@backup_locked
def create_backup(db, now=None):
    if db.in_transaction: raise ValueError('Дождись завершения операции и повтори.')
    now=now or datetime.now(ledger.TZ)
    folder=backup_dir(db);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    name=now.strftime('ledger-%Y%m%d-%H%M%S-%f')
    snapshot=folder/(name+'.sqlite3');archive=folder/(name+'.zip');part=folder/(name+'.zip.part')
    try:
        target=sqlite3.connect(snapshot)
        try:
            db.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise RuntimeError('Backup integrity check failed')
            if target.execute('PRAGMA foreign_key_check').fetchone(): raise RuntimeError('Backup foreign key check failed')
        finally: target.close()
        os.chmod(snapshot,0o600)
        with zipfile.ZipFile(part,'w',zipfile.ZIP_DEFLATED) as z:
            z.write(snapshot,'ledger.sqlite3')
        os.chmod(part,0o600);part.replace(archive)
        # Keep the newest snapshot for each of the last seven available days.
        kept=set()
        for p in sorted(folder.glob('ledger-*.zip'),reverse=True):
            day=p.name.split('-')[1]
            if day in kept or len(kept)>=7: p.unlink()
            else: kept.add(day)
        with db:
            db.execute("INSERT INTO settings(key,value) VALUES('backup_last',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(now.isoformat(),))
            db.execute("DELETE FROM settings WHERE key='backup_error'")
        return archive
    finally:
        snapshot.unlink(missing_ok=True);part.unlink(missing_ok=True)


def latest_backup(db):
    files=sorted(backup_dir(db).glob('ledger-*.zip'),reverse=True)
    return files[0] if files else None


@backup_locked
def backup_download(db):
    path=latest_backup(db)
    if not path: path=create_backup(db)
    if path.stat().st_size>MAX_DOWNLOAD:
        raise ValueError('Копия больше 45 МБ. Скачай её из постоянного диска сервиса; отправка через бота недоступна.')
    return path.name,path.read_bytes()


def subscribe(db,uid,owner,enabled):
    data.require_reader(db,uid,owner)
    with db:
        db.execute('INSERT INTO weekly_subscriptions(user_id,enabled,last_week) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,last_week=excluded.last_week',
                   (uid,int(enabled),data.week_start(data.today()).isoformat()))


class Scheduler:
    def __init__(self): self.checked=0
    def tick(self,api,db,owner,now=None):
        if now is None and time.monotonic()-self.checked<60: return
        self.checked=time.monotonic();now=now or datetime.now(ledger.TZ)
        last=db.execute("SELECT value FROM settings WHERE key='backup_last'").fetchone()
        if not last or last[0][:10]!=now.date().isoformat():
            try: create_backup(db,now)
            except Exception:
                logging.exception('Daily database backup failed')
                with db: db.execute("INSERT INTO settings(key,value) VALUES('backup_error','Копия не создана. Проверь свободное место и журнал сервиса.') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        monday=data.week_start(now.date());week=monday.isoformat()
        if now < datetime.combine(monday,datetime.min.time(),tzinfo=ledger.TZ)+timedelta(hours=9): return
        for sub in db.execute('SELECT * FROM weekly_subscriptions WHERE enabled=1 AND last_week<?',(week,)).fetchall():
            uid=sub['user_id']
            if not ledger.reader(db,uid,owner):
                with db: db.execute('UPDATE weekly_subscriptions SET enabled=0 WHERE user_id=?',(uid,))
                continue
            delivery=db.execute('SELECT * FROM weekly_deliveries WHERE user_id=? AND week=?',(uid,week)).fetchone()
            if delivery and delivery['status']=='sent':
                with db: db.execute('UPDATE weekly_subscriptions SET last_week=? WHERE user_id=?',(week,uid))
                continue
            if delivery and now-datetime.fromisoformat(delivery['updated_at'])<timedelta(hours=1): continue
            with db:
                db.execute("INSERT INTO weekly_deliveries(user_id,week,status,updated_at) VALUES (?,?,'sending',?) ON CONFLICT(user_id,week) DO UPDATE SET status='sending',updated_at=excluded.updated_at",(uid,week,now.isoformat()))
            try:
                api.send(uid,data.weekly_text(db,monday-timedelta(days=1)),[('Общий отчёт PDF','all:menu'),('Настроить сводку','ctl:weekly')])
            except Exception:
                logging.exception('Weekly summary delivery failed for user %s',uid)
                with db: db.execute("UPDATE weekly_deliveries SET status='failed' WHERE user_id=? AND week=?",(uid,week))
            else:
                with db:
                    db.execute("UPDATE weekly_deliveries SET status='sent' WHERE user_id=? AND week=?",(uid,week))
                    db.execute('UPDATE weekly_subscriptions SET last_week=? WHERE user_id=?',(week,uid))
