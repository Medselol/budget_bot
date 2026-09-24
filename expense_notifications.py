"""Durable expense review alerts and personal manager preferences."""
import logging
import time
from decimal import Decimal, InvalidOperation
import bot as ledger


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS expense_alert_settings(
      user_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
      min_uah INTEGER NOT NULL DEFAULT 0, min_usd INTEGER NOT NULL DEFAULT 0,
      foreman_id INTEGER NOT NULL DEFAULT 0, missing_only INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS expense_alert_events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id INTEGER NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS expense_alert_deliveries(
      event_id INTEGER NOT NULL, recipient INTEGER NOT NULL,
      state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt REAL NOT NULL DEFAULT 0, PRIMARY KEY(event_id,recipient));
    CREATE INDEX IF NOT EXISTS expense_alert_latest ON expense_alert_events(operation_id,id);
    ''')


def changed(db, ident):
    """Called in the transaction saving the expense. No network calls."""
    r=db.execute("SELECT o.*,u.role FROM operations o JOIN users u ON u.id=o.user_id WHERE o.id=? AND o.kind='expense'",(ident,)).fetchone()
    if not r: return
    db.execute("INSERT INTO expense_reviews(operation_id) VALUES (?) ON CONFLICT(operation_id) DO UPDATE SET status='pending',reviewer_id=NULL,reviewed_at=NULL",(ident,))
    if r['role']!='foreman': return
    event=db.execute('INSERT INTO expense_alert_events(operation_id) VALUES (?)',(ident,)).lastrowid
    db.execute("INSERT INTO expense_alert_deliveries(event_id,recipient) SELECT ?,id FROM users WHERE active=1 AND deleted=0 AND role IN ('owner','editor')",(event,))


def settings(db, uid):
    r=db.execute('SELECT * FROM expense_alert_settings WHERE user_id=?',(uid,)).fetchone()
    return dict(r) if r else dict(enabled=1,min_uah=0,min_usd=0,foreman_id=0,missing_only=0)


def matches(db, uid, row):
    s=settings(db,uid)
    return (s['enabled'] and row['amount_kop']>=s['min_'+row['currency'].lower()]
            and (not s['foreman_id'] or s['foreman_id']==row['user_id'])
            and (not s['missing_only'] or not db.execute('SELECT 1 FROM receipts WHERE operation_id=?',(row['id'],)).fetchone()))


def deliver(api, db, owner, now=None):
    now=time.time() if now is None else now
    jobs=db.execute("SELECT d.*,e.operation_id FROM expense_alert_deliveries d JOIN expense_alert_events e ON e.id=d.event_id WHERE d.state='pending' AND d.next_attempt<=? ORDER BY d.event_id LIMIT 20",(now,)).fetchall()
    for job in jobs:
        ident=job['operation_id'];uid=job['recipient'];key=(job['event_id'],uid)
        row=db.execute("SELECT o.*,u.name FROM operations o LEFT JOIN users u ON u.id=o.user_id WHERE o.id=? AND o.kind='expense'",(ident,)).fetchone()
        from receipts import metadata
        latest=db.execute('SELECT MAX(id) FROM expense_alert_events WHERE operation_id=?',(ident,)).fetchone()[0]
        if (not row or not ledger.manager(db,uid,owner) or latest!=job['event_id']
                or metadata(db,ident)[0]=='approved' or not matches(db,uid,row)):
            with db: db.execute("UPDATE expense_alert_deliveries SET state='skipped' WHERE event_id=? AND recipient=?",key)
            continue
        status,reason,count=metadata(db,ident)
        text=(f"🧾 Расход прораба — требует проверки\n{row['name'] or row['user_id']}\n"
              +ledger.operation_text(db,row)+f'\nЧеков: {count}'
              +('\nПояснение: '+reason if reason else '')
              +'\nРасход уже учтён в балансе. Открой запись и проверь детали и чеки.')
        try:
            api.send(uid,text,[('Проверить расход',f'rc:view:{ident}'),('Очередь проверки','rc:list:pending:0'),('Настройки уведомлений','ec:settings')])
        except Exception as exc:
            logging.warning('Expense notification failed event=%s error=%s',job['event_id'],type(exc).__name__)
            attempts=job['attempts']+1
            with db: db.execute('UPDATE expense_alert_deliveries SET attempts=?,next_attempt=?,state=? WHERE event_id=? AND recipient=?',(attempts,now+min(3600,30*2**min(attempts-1,7)),'failed' if attempts>=12 else 'pending',*key))
        else:
            with db: db.execute("UPDATE expense_alert_deliveries SET state='sent' WHERE event_id=? AND recipient=?",key)


def show(api,db,chat,uid):
    s=settings(db,uid)
    person=db.execute('SELECT name FROM users WHERE id=?',(s['foreman_id'],)).fetchone()
    api.send(chat,'Мои уведомления о расходах прорабов\n'
        +('Включены' if s['enabled'] else 'Выключены')
        +f"\nОт {ledger.money(s['min_uah'],'UAH')} / {ledger.money(s['min_usd'],'USD')}"
        +'\nПрораб: '+(person[0] if person else 'Все')
        +'\nТип: '+('Только без чеков' if s['missing_only'] else 'Все расходы')
        +'\nНастройки действуют только на твои уведомления. Все расходы остаются в очереди проверки.',
        [('Выключить' if s['enabled'] else 'Включить','ec:off' if s['enabled'] else 'ec:on'),
         ('Минимум UAH','ec:min:UAH'),('Минимум USD','ec:min:USD'),('Выбрать прораба','ec:people:0'),
         ('Все расходы' if s['missing_only'] else 'Только без чеков','ec:alltypes' if s['missing_only'] else 'ec:missing'),
         ('Сбросить настройки','ec:reset'),('К проверке расходов','rc:list'),('Главное меню','menu')])


def callback(api,db,chat,uid,value,owner):
    if not value.startswith('ec:'): return False
    if chat!=uid or not ledger.manager(db,uid,owner): return True
    if value.startswith('ec:people:'):
        try: page=max(0,int(value.rsplit(':',1)[1]))
        except ValueError: return True
        rows=db.execute("SELECT id,name FROM users WHERE role='foreman' AND active=1 AND deleted=0 ORDER BY id LIMIT 9 OFFSET ?",(page*8,)).fetchall()
        opts=[('Все прорабы','ec:person:0')]+[(r['name'],f"ec:person:{r['id']}") for r in rows[:8]]
        if page: opts.append(('Назад',f'ec:people:{page-1}'))
        if len(rows)>8: opts.append(('Далее',f'ec:people:{page+1}'))
        api.send(chat,'Чьи расходы присылать?',opts+[('К настройкам','ec:settings'),('Главное меню','menu')]);return True
    if value in ('ec:min:UAH','ec:min:USD'):
        ledger.set_draft(db,uid,dict(step='expense_alert_min',currency=value.rsplit(':',1)[1]))
        api.send(chat,'Введи минимальную сумму. Например: 1000 или 1000,50. Ноль — любые суммы.',[('К настройкам','ec:settings'),('Главное меню','menu')]);return True
    field=None;v=None
    if value in ('ec:on','ec:off'): field='enabled';v=int(value=='ec:on')
    elif value in ('ec:missing','ec:alltypes'): field='missing_only';v=int(value=='ec:missing')
    elif value.startswith('ec:person:'):
        try: v=int(value.rsplit(':',1)[1])
        except ValueError: return True
        if v and not db.execute("SELECT 1 FROM users WHERE id=? AND role='foreman' AND active=1 AND deleted=0",(v,)).fetchone(): return True
        field='foreman_id'
    with db:
        if value=='ec:reset': db.execute('DELETE FROM expense_alert_settings WHERE user_id=?',(uid,))
        if field:
            db.execute('INSERT OR IGNORE INTO expense_alert_settings(user_id) VALUES (?)',(uid,))
            db.execute(f'UPDATE expense_alert_settings SET {field}=? WHERE user_id=?',(v,uid))
    ledger.clear_draft(db,uid);show(api,db,chat,uid);return True


def message(api,db,chat,uid,text,owner):
    d=ledger.draft(db,uid) or {}
    if text.startswith('/') or d.get('step')!='expense_alert_min': return False
    if chat!=uid or not ledger.manager(db,uid,owner): return True
    try:
        amount=Decimal(text.strip().replace(',','.'))*100
        if not amount.is_finite() or amount<0 or amount>10**14 or amount!=amount.to_integral_value(): raise ValueError()
        amount=int(amount)
    except (InvalidOperation,ValueError):
        api.send(chat,'Введи неотрицательную сумму, максимум два знака после запятой.',[('К настройкам','ec:settings')]);return True
    field={'UAH':'min_uah','USD':'min_usd'}[d['currency']]
    with db:
        db.execute('INSERT OR IGNORE INTO expense_alert_settings(user_id) VALUES (?)',(uid,))
        db.execute(f'UPDATE expense_alert_settings SET {field}=? WHERE user_id=?',(amount,uid))
    ledger.clear_draft(db,uid);show(api,db,chat,uid);return True
