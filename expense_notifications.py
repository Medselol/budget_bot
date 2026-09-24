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
    if 'detail' not in {r['name'] for r in db.execute('PRAGMA table_info(expense_alert_deliveries)')}:
        db.execute("ALTER TABLE expense_alert_deliveries ADD COLUMN detail TEXT NOT NULL DEFAULT ''")
    db.commit()


def changed(db, ident, *, reset_review=True):
    """Called in the transaction saving the expense. No network calls."""
    r=db.execute("SELECT o.*,u.role FROM operations o JOIN users u ON u.id=o.user_id WHERE o.id=? AND o.kind='expense'",(ident,)).fetchone()
    if not r: return
    if reset_review: db.execute("INSERT INTO expense_reviews(operation_id) VALUES (?) ON CONFLICT(operation_id) DO UPDATE SET status='pending',reviewer_id=NULL,reviewed_at=NULL",(ident,))
    if r['role']!='foreman': return
    event=db.execute('INSERT INTO expense_alert_events(operation_id) VALUES (?)',(ident,)).lastrowid
    db.execute("INSERT INTO expense_alert_deliveries(event_id,recipient) SELECT ?,id FROM users WHERE active=1 AND deleted=0 AND role IN ('owner','editor')",(event,))


def settings(db, uid):
    r=db.execute('SELECT * FROM expense_alert_settings WHERE user_id=?',(uid,)).fetchone()
    return dict(r) if r else dict(enabled=1,min_uah=0,min_usd=0,foreman_id=0,missing_only=0)


def exclusion(db, uid, row):
    s=settings(db,uid)
    if not s['enabled']: return 'disabled'
    if row['amount_kop']<s['min_'+row['currency'].lower()]: return 'minimum'
    if s['foreman_id'] and s['foreman_id']!=row['user_id']: return 'person'
    if s['missing_only'] and db.execute('SELECT 1 FROM receipts WHERE operation_id=?',(row['id'],)).fetchone(): return 'has_receipt'
    return ''


def matches(db,uid,row):
    return not exclusion(db,uid,row)


def review_reopened(db,ident,previous_status):
    # Runs in the receipt/review transaction. Existing pending expenses do not
    # generate one alert for every uploaded page of a receipt.
    existing=db.execute('SELECT 1 FROM expense_alert_events WHERE operation_id=?',(ident,)).fetchone()
    if previous_status=='approved' or not existing:
        changed(db,ident,reset_review=False)


def deliver(api, db, owner, now=None):
    now=time.time() if now is None else now
    jobs=db.execute("SELECT d.*,e.operation_id FROM expense_alert_deliveries d JOIN expense_alert_events e ON e.id=d.event_id WHERE d.state='pending' AND d.next_attempt<=? ORDER BY d.event_id LIMIT 20",(now,)).fetchall()
    for job in jobs:
        ident=job['operation_id'];uid=job['recipient'];key=(job['event_id'],uid)
        row=db.execute("SELECT o.*,u.name FROM operations o LEFT JOIN users u ON u.id=o.user_id WHERE o.id=? AND o.kind='expense'",(ident,)).fetchone()
        from receipts import metadata
        latest=db.execute('SELECT MAX(id) FROM expense_alert_events WHERE operation_id=?',(ident,)).fetchone()[0]
        reason=('deleted' if not row else 'access' if not ledger.manager(db,uid,owner)
                else 'superseded' if latest!=job['event_id'] else 'approved' if metadata(db,ident)[0]=='approved'
                else exclusion(db,uid,row))
        if reason:
            with db: db.execute("UPDATE expense_alert_deliveries SET state='skipped',detail=? WHERE event_id=? AND recipient=?",(reason,*key))
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
            with db: db.execute('UPDATE expense_alert_deliveries SET attempts=?,next_attempt=?,state=?,detail=? WHERE event_id=? AND recipient=?',(attempts,now+min(3600,30*2**min(attempts-1,7)),'failed' if attempts>=12 else 'pending','telegram_'+str(getattr(exc,'code','error')),*key))
        else:
            with db: db.execute("UPDATE expense_alert_deliveries SET state='sent',detail='' WHERE event_id=? AND recipient=?",key)


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
    if value.startswith(('ec:delivery:','ec:retry:')):
        try: ident=int(value.rsplit(':',1)[1])
        except ValueError: return True
        if value.startswith('ec:retry:'): retry(db,ident)
        delivery_status(api,db,chat,ident)
        return True
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


DETAILS={'disabled':'уведомления выключены','minimum':'сумма ниже заданного минимума',
         'person':'в настройках выбран другой прораб','has_receipt':'включён фильтр «только без чеков»',
         'deleted':'операция удалена','access':'права администратора закрыты',
         'superseded':'расход изменился, создано новое уведомление','approved':'расход уже проверен',
         'telegram_403':'Telegram отказал в доступе к чату — проверь блокировку бота',
         'telegram_400':'Telegram отклонил сообщение — проверь, что администратор нажал /start',
         'telegram_429':'ограничение частоты Telegram; будет повтор',
         'telegram_error':'ошибка соединения с Telegram'}


def retry(db,ident):
    from receipts import row_for,metadata
    row=row_for(db,ident)
    if not row or metadata(db,ident)[0]=='approved': return
    with db:
        event=db.execute('SELECT MAX(id) FROM expense_alert_events WHERE operation_id=?',(ident,)).fetchone()[0]
        if event is None:
            changed(db,ident,reset_review=False)
            event=db.execute('SELECT MAX(id) FROM expense_alert_events WHERE operation_id=?',(ident,)).fetchone()[0]
        if event is None: return
        # Include newly appointed managers, but never re-send a confirmed delivery.
        db.execute("INSERT OR IGNORE INTO expense_alert_deliveries(event_id,recipient) SELECT ?,id FROM users WHERE active=1 AND deleted=0 AND role IN ('owner','editor')",(event,))
        db.execute("UPDATE expense_alert_deliveries SET state='pending',attempts=0,next_attempt=0,detail='' WHERE event_id=? AND state IN ('failed','skipped')",(event,))


def delivery_status(api,db,chat,ident):
    from receipts import row_for,metadata
    row=row_for(db,ident)
    if not row: api.send(chat,'Расход недоступен.');return
    author=db.execute('SELECT name,role FROM users WHERE id=?',(row['user_id'],)).fetchone()
    text=f"Доставка уведомлений · расход №{ident}\nАвтор: {author['name'] if author else row['user_id']}"
    if author: text+=' · '+ledger.ROLE_NAMES.get(author['role'],author['role'])
    heartbeat=db.execute("SELECT value FROM settings WHERE key='notification_heartbeat'").fetchone()
    if not heartbeat or time.time()-float(heartbeat[0])>120:
        text+='\nФоновая отправка пока не подтверждена. Если статус сохраняется, нужны Deploy Logs Railway.'
    event=db.execute('SELECT MAX(id) FROM expense_alert_events WHERE operation_id=?',(ident,)).fetchone()[0]
    if event is None:
        text+=('\nАвтоуведомления предназначены для расходов прорабов.' if author and author['role']!='foreman'
               else '\nУведомление не было поставлено в очередь. Можно отправить его сейчас.')
    else:
        rows=db.execute('SELECT d.*,u.name FROM expense_alert_deliveries d LEFT JOIN users u ON u.id=d.recipient WHERE event_id=? ORDER BY recipient',(event,)).fetchall()
        if not rows: text+='\nНа момент сохранения не было активных администраторов.'
        labels={'pending':'В очереди','sent':'Отправлено в Telegram','skipped':'Пропущено','failed':'Ошибка доставки'}
        for r in rows:
            text+=f"\n{r['name'] or r['recipient']}: {labels.get(r['state'],r['state'])}"
            if r['detail']: text+=' — '+DETAILS.get(r['detail'],'ошибка Telegram')
            if r['attempts']: text+=f" · попыток: {r['attempts']}"
    opts=[('Обновить статус',f'ec:delivery:{ident}')]
    if metadata(db,ident)[0]!='approved' and (event is not None or (author and author['role']=='foreman')):
        opts.append(('Повторить недоставленные',f'ec:retry:{ident}'))
    api.send(chat,text,opts+[('К расходу',f'rc:view:{ident}'),('Настройки','ec:settings'),('Главное меню','menu')])
