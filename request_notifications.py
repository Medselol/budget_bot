"""Durable funding-request notifications. One delivery worker per bot process."""
import logging
import time
import bot as ledger


def init(db):
    db.execute('''CREATE TABLE IF NOT EXISTS request_notifications (
        id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL,
        version INTEGER NOT NULL, recipient INTEGER NOT NULL, event TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0,
        UNIQUE(request_id,version,recipient))''')
    db.commit()


def enqueue(db, ident, owner):
    # Called inside the transaction changing the request: both commit together.
    r=db.execute('SELECT * FROM funding_requests WHERE id=?',(ident,)).fetchone()
    if r['status']=='pending':
        recipients=[u['id'] for u in db.execute('SELECT id FROM users WHERE active=1 AND deleted=0')
                    if ledger.manager(db,u['id'],owner)]
    elif r['status'] in ('approved','rejected','paid'): recipients=[r['user_id']]
    else: return
    for uid in recipients:
        db.execute('INSERT OR IGNORE INTO request_notifications(request_id,version,recipient,event) VALUES (?,?,?,?)',
                   (ident,r['version'],uid,r['status']))


def deliver(api, db, owner, now=None):
    now=time.time() if now is None else now
    jobs=db.execute("SELECT * FROM request_notifications WHERE state='pending' AND next_attempt<=? ORDER BY id LIMIT 20",(now,)).fetchall()
    for job in jobs:
        uid=job['recipient'];ident=job['request_id']
        r=db.execute('SELECT r.*,u.name AS user_name FROM funding_requests r LEFT JOIN users u ON u.id=r.user_id WHERE r.id=?',(ident,)).fetchone()
        permitted=ledger.manager(db,uid,owner) if job['event']=='pending' else bool(r and uid==r['user_id'] and ledger.role_for(db,uid,owner))
        if not r or not permitted or r['version']!=job['version']:
            with db: db.execute("UPDATE request_notifications SET state='skipped' WHERE id=?",(job['id'],))
            continue
        from control_data import REQUEST_STATUS
        title='💰 Новая заявка на деньги' if job['event']=='pending' else 'Заявка: '+REQUEST_STATUS[r['status']]
        text=f"{title}\n№{ident} · {r['user_name'] or r['user_id']}\nСумма: {ledger.money(r['amount_kop'],r['currency'])}\nНазначение: {r['purpose']}\nНужны к: {r['due_on']}"
        if r['decision']: text+='\nРешение: '+r['decision']
        if r['status']=='approved': text+='\nДеньги ещё не выданы. Выдача подтверждается отдельно.'
        count=db.execute('SELECT COUNT(*) FROM request_files WHERE request_id=?',(ident,)).fetchone()[0]
        opts=[('Открыть заявку',f'ctl:req:view:{ident}')]
        if count:
            text+=f'\n📎 Документов: {count}'
            opts.append(('Счета и документы',f'ctl:req:files:{ident}'))
        if r['status']=='pending':
            opts.extend([('Одобрить',f'ctl:req:approve:{ident}'),('Отклонить',f'ctl:req:reject:{ident}')])
        try: api.send(uid,text,opts)
        except Exception as exc:
            # Never log tokens, exception bodies or financial details.
            logging.warning('Request notification failed id=%s error=%s',job['id'],type(exc).__name__)
            attempts=job['attempts']+1
            with db:
                db.execute('UPDATE request_notifications SET attempts=?,next_attempt=?,state=? WHERE id=?',
                           (attempts,now+min(3600,30*2**min(attempts-1,7)),'failed' if attempts>=12 else 'pending',job['id']))
        else:
            with db: db.execute("UPDATE request_notifications SET state='sent' WHERE id=?",(job['id'],))
