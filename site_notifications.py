"""Persistent photo-report notifications; investors receive accepted reports only."""
import logging
import time
import bot as ledger


def init(db):
    db.execute('''CREATE TABLE IF NOT EXISTS site_notifications (
        id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL,
        version INTEGER NOT NULL, recipient INTEGER NOT NULL, audience TEXT NOT NULL,
        event TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
        UNIQUE(report_id,version,recipient))''')
    db.commit()


def enqueue(db, ident, owner):
    # Caller transaction includes both state transition and recipient queue.
    r=db.execute('SELECT * FROM site_reports WHERE id=?',(ident,)).fetchone()
    recipients={}
    if r['status'] in ('submitted','accepted'):
        for u in db.execute('SELECT id FROM users WHERE active=1 AND deleted=0'):
            role=ledger.role_for(db,u['id'],owner)
            if r['status']=='submitted' and role in ('owner','editor'): recipients[u['id']]='manager'
            elif r['status']=='accepted' and role=='investor': recipients[u['id']]='investor'
    if r['status'] in ('accepted','rework'): recipients[r['user_id']]='author'
    for uid,audience in recipients.items():
        db.execute('INSERT OR IGNORE INTO site_notifications(report_id,version,recipient,audience,event) VALUES (?,?,?,?,?)',
                   (ident,r['version'],uid,audience,r['status']))


def deliver(api, db, owner, now=None):
    now=time.time() if now is None else now
    jobs=db.execute("SELECT * FROM site_notifications WHERE state='pending' AND next_attempt<=? ORDER BY id LIMIT 20",(now,)).fetchall()
    for job in jobs:
        uid=job['recipient'];ident=job['report_id']
        r=db.execute('SELECT s.*,u.name AS author FROM site_reports s JOIN users u ON u.id=s.user_id WHERE s.id=?',(ident,)).fetchone()
        role=ledger.role_for(db,uid,owner)
        allowed=bool(r and role and (
            (job['audience']=='manager' and role in ('owner','editor')) or
            (job['audience']=='investor' and role=='investor' and r['status']=='accepted') or
            (job['audience']=='author' and uid==r['user_id'])))
        if not allowed or r['version']!=job['version'] or r['status']!=job['event']:
            with db: db.execute("UPDATE site_notifications SET state='skipped' WHERE id=?",(job['id'],))
            continue
        title={'submitted':'📸 Новый фотоотчёт на проверку','accepted':'✅ Фотоотчёт подтверждён','rework':'📝 Фотоотчёт возвращён на доработку'}[r['status']]
        count=db.execute('SELECT COUNT(*) FROM site_photos WHERE report_id=?',(ident,)).fetchone()[0]
        text=f"{title}\n№{ident} · {ledger.DEFAULT_PROJECT_NAME}\nАвтор: {r['author']}\nДата работ: {r['work_date']}\nЭтап: {r['stage']}\nМесто / секция: {r['area'] or '—'}\nВыполнено: {r['work']}\nФотографий: {count}"
        if r['status']=='rework': text+='\nЗамечание: '+r['review_note']
        if job['audience']=='investor': text+='\nОтчёт принят администратором. Можете посмотреть фотографии.'
        opts=[('Открыть фотоотчёт',f'site:view:{ident}'),('Смотреть фотографии',f'site:photos:{ident}')]
        if r['status']=='submitted':
            opts.extend([('Подтвердить',f'site:accept:{ident}:{r["version"]}'),('На доработку',f'site:rework:{ident}:{r["version"]}')])
        try: api.send(uid,text,opts)
        except Exception as exc:
            logging.warning('Photo notification failed id=%s error=%s',job['id'],type(exc).__name__)
            attempts=job['attempts']+1
            with db:
                db.execute('UPDATE site_notifications SET attempts=?,next_attempt=?,state=? WHERE id=?',
                           (attempts,now+min(3600,30*2**min(attempts-1,7)),'failed' if attempts>=12 else 'pending',job['id']))
        else:
            with db: db.execute("UPDATE site_notifications SET state='sent' WHERE id=?",(job['id'],))
