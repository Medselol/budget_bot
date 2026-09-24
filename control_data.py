"""Budgets, funding requests and obligations. Money is stored in integer minor units."""
import json
import sqlite3
from datetime import timedelta
import bot as ledger

REQUEST_STATUS = {'draft':'Черновик','pending':'На согласовании','approved':'Одобрено, деньги не выданы','rejected':'Отклонено','paid':'Деньги выданы','cancelled':'Отменено'}


def today():
    return ledger.datetime.now(ledger.TZ).date()


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT NOT NULL, currency TEXT NOT NULL CHECK(currency IN ('UAH','USD')),
        amount_kop INTEGER NOT NULL CHECK(amount_kop>=0), version INTEGER NOT NULL DEFAULT 1, UNIQUE(stage,currency));
    CREATE TABLE IF NOT EXISTS funding_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL REFERENCES accounts(id), currency TEXT NOT NULL CHECK(currency IN ('UAH','USD')),
        amount_kop INTEGER NOT NULL CHECK(amount_kop>0), purpose TEXT NOT NULL, due_on TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','pending','approved','rejected','paid','cancelled')),
        decision TEXT NOT NULL DEFAULT '', reviewer_id INTEGER, operation_id INTEGER UNIQUE REFERENCES operations(id) ON DELETE SET NULL,
        version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS request_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL REFERENCES funding_requests(id),
        update_id INTEGER NOT NULL UNIQUE, filename TEXT NOT NULL, content BLOB NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS obligations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL UNIQUE, direction TEXT NOT NULL CHECK(direction IN ('payable','receivable')),
        counterparty TEXT NOT NULL, purpose TEXT NOT NULL, stage TEXT NOT NULL, currency TEXT NOT NULL CHECK(currency IN ('UAH','USD')),
        amount_kop INTEGER NOT NULL CHECK(amount_kop>0), due_on TEXT NOT NULL, classification TEXT NOT NULL DEFAULT 'Прочее', cancelled INTEGER NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 1, created_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS obligation_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL UNIQUE, obligation_id INTEGER NOT NULL REFERENCES obligations(id),
        operation_id INTEGER NOT NULL UNIQUE REFERENCES operations(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS weekly_subscriptions(user_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, last_week TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS weekly_deliveries(user_id INTEGER NOT NULL, week TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(user_id,week));
    CREATE TRIGGER IF NOT EXISTS request_transfer_removed AFTER UPDATE OF operation_id ON funding_requests
    WHEN OLD.operation_id IS NOT NULL AND NEW.operation_id IS NULL AND NEW.status='paid'
    BEGIN UPDATE funding_requests SET status='approved',version=version+1,decision='Связанный перевод удалён. Проверь фактическую выдачу.' WHERE id=NEW.id; END;
    CREATE TRIGGER IF NOT EXISTS linked_money_immutable BEFORE UPDATE ON operations
    WHEN (EXISTS(SELECT 1 FROM funding_requests WHERE operation_id=OLD.id) OR EXISTS(SELECT 1 FROM obligation_payments WHERE operation_id=OLD.id))
    AND (NEW.kind IS NOT OLD.kind OR NEW.amount_kop IS NOT OLD.amount_kop OR NEW.currency IS NOT OLD.currency
         OR NEW.account_id IS NOT OLD.account_id OR NEW.from_account_id IS NOT OLD.from_account_id OR NEW.to_account_id IS NOT OLD.to_account_id OR NEW.user_id IS NOT OLD.user_id)
    BEGIN SELECT RAISE(ABORT,'Связанная операция: для исправления суммы или счетов удали её и проведи заново из заявки или платежа.'); END;
    ''')
    if 'classification' not in {r['name'] for r in db.execute('PRAGMA table_info(obligations)')}:
        db.execute("ALTER TABLE obligations ADD COLUMN classification TEXT NOT NULL DEFAULT 'Прочее'")
        db.commit()


def require_manager(db, uid, owner):
    if not ledger.manager(db,uid,owner): raise ValueError('Это действие доступно администратору.')


def require_reader(db, uid, owner):
    if not ledger.reader(db,uid,owner): raise ValueError('Нет доступа к общему учёту.')


def active_account(db, aid):
    row=db.execute("SELECT a.*,u.name AS user_name FROM accounts a JOIN users u ON u.id=a.owner_id WHERE a.id=? AND u.active=1 AND u.deleted=0 AND u.role!='investor'",(aid,)).fetchone()
    if not row: raise ValueError('Счёт или участник недоступен.')
    return row


def amount_valid(amount, currency):
    if type(amount) is not int or not 0 < amount <= 100_000_000_000 or currency not in ledger.CURRENCIES:
        raise ValueError('Проверь сумму и валюту.')


def request(db, ident, uid, owner):
    r=db.execute('SELECT r.*,u.name AS user_name FROM funding_requests r LEFT JOIN users u ON u.id=r.user_id WHERE r.id=?',(ident,)).fetchone()
    if not r or not ledger.role_for(db,uid,owner) or (r['user_id']!=uid and not ledger.reader(db,uid,owner)):
        raise ValueError('Заявка недоступна.')
    if r['status']=='draft' and r['user_id']!=uid and not ledger.manager(db,uid,owner):
        raise ValueError('Черновик заявки недоступен.')
    return r


def create_request(db, uid, owner, values, token):
    if ledger.role_for(db,uid,owner) not in ('owner','editor','foreman','member'): raise ValueError('Нет права создавать заявку.')
    a=active_account(db,values['account_id'])
    if a['owner_id']!=uid: raise ValueError('Выбери свой счёт.')
    amount_valid(values['amount_kop'],values['currency'])
    ledger.valid_date(values['due_on'])
    if not 3<=len(values['purpose'])<=500: raise ValueError('Назначение: от 3 до 500 символов.')
    with db:
        db.execute('INSERT OR IGNORE INTO funding_requests(token,user_id,account_id,currency,amount_kop,purpose,due_on) VALUES (?,?,?,?,?,?,?)',
                   (token,uid,values['account_id'],values['currency'],values['amount_kop'],values['purpose'],values['due_on']))
    return db.execute('SELECT id FROM funding_requests WHERE token=? AND user_id=?',(token,uid)).fetchone()[0]


def decide_request(db, uid, owner, ident, version, action, reason=''):
    with db:
        db.execute('BEGIN IMMEDIATE')
        r=request(db,ident,uid,owner)
        if ledger.role_for(db,uid,owner)=='investor': raise ValueError('Инвестор может только просматривать заявки.')
        if r['version']!=version: raise ValueError('Заявка уже изменилась. Открой карточку заново.')
        if action=='submit' and r['user_id']==uid and r['status']=='draft': status='pending'
        elif action=='cancel' and r['user_id']==uid and r['status'] in ('draft','pending'): status='cancelled'
        elif action in ('approve','reject') and r['status']=='pending':
            require_manager(db,uid,owner)
            if action=='reject' and len(reason.strip())<3: raise ValueError('Укажи причину отказа.')
            active_account(db,r['account_id'])
            status='approved' if action=='approve' else 'rejected'
        else: raise ValueError('Действие недоступно для текущего статуса.')
        db.execute('UPDATE funding_requests SET status=?,decision=?,reviewer_id=?,version=version+1 WHERE id=?',
                   (status,reason[:500],uid if action in ('approve','reject') else None,ident))


def pay_request(db, uid, owner, ident, version, source_id, update_id):
    require_manager(db,uid,owner)
    with db:
        db.execute('BEGIN IMMEDIATE')
        r=request(db,ident,uid,owner)
        if r['status']=='paid': return r['operation_id']
        if r['version']!=version or r['status']!='approved': raise ValueError('Заявка изменилась или ещё не одобрена.')
        a,b=active_account(db,source_id),active_account(db,r['account_id'])
        if a['id']==b['id']: raise ValueError('Выбери другой счёт отправителя.')
        if ledger.balances(db,a['owner_id'])[a['id']][1][r['currency']]<r['amount_kop']: raise ValueError('В кассе отправителя недостаточно денег.')
        cur=db.execute("INSERT INTO operations(update_id,user_id,occurred_on,kind,from_account_id,to_account_id,amount_kop,currency,comment) VALUES (?,?,?,'transfer',?,?,?,?,?)",
            (update_id,uid,today().isoformat(),a['id'],b['id'],r['amount_kop'],r['currency'],f"Выдача по заявке №{ident}: {r['purpose']}"))
        db.execute("UPDATE funding_requests SET status='paid',operation_id=?,reviewer_id=?,version=version+1 WHERE id=?",(cur.lastrowid,uid,ident))
        ledger.bump_revision(db)
    return cur.lastrowid


def set_budget(db, uid, owner, stage, currency, amount, version):
    require_manager(db,uid,owner)
    if stage not in ledger.STAGES or currency not in ledger.CURRENCIES or type(amount) is not int or not 0<=amount<=100_000_000_000:
        raise ValueError('Неверный бюджет.')
    with db:
        db.execute('BEGIN IMMEDIATE')
        r=db.execute('SELECT * FROM budgets WHERE stage=? AND currency=?',(stage,currency)).fetchone()
        if (r['version'] if r else 0)!=version: raise ValueError('План изменён другим администратором. Открой заново.')
        db.execute('INSERT INTO budgets(stage,currency,amount_kop) VALUES (?,?,?) ON CONFLICT(stage,currency) DO UPDATE SET amount_kop=excluded.amount_kop,version=budgets.version+1',(stage,currency,amount))


def budget_rows(db, currency):
    plans={r['stage']:r['amount_kop'] for r in db.execute('SELECT * FROM budgets WHERE currency=?',(currency,))}
    actual={r[0]:r[1] for r in db.execute("SELECT COALESCE(category,'Прочее'),SUM(amount_kop) FROM operations WHERE kind='expense' AND currency=? GROUP BY COALESCE(category,'Прочее')",(currency,))}
    return [(stage,plans.get(stage),actual.get(stage,0)) for stage in dict.fromkeys([*ledger.STAGES,*actual])]


def create_obligation(db, uid, owner, v, token):
    require_manager(db,uid,owner);amount_valid(v['amount_kop'],v['currency']);ledger.valid_date(v['due_on'])
    if v['direction'] not in ('payable','receivable') or v['stage'] not in ledger.STAGES: raise ValueError('Проверь вид и этап платежа.')
    if v.get('classification','Прочее') not in (ledger.COST_TYPES if v['direction']=='payable' else ledger.SOURCES): raise ValueError('Проверь категорию оплаты.')
    if not 2<=len(v['counterparty'])<=120 or not 3<=len(v['purpose'])<=500: raise ValueError('Заполни контрагента и назначение.')
    with db:
        db.execute('INSERT OR IGNORE INTO obligations(token,direction,counterparty,purpose,stage,currency,amount_kop,due_on,created_by,classification) VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (token,v['direction'],v['counterparty'],v['purpose'],v['stage'],v['currency'],v['amount_kop'],v['due_on'],uid,v.get('classification','Прочее')))
    return db.execute('SELECT id FROM obligations WHERE token=?',(token,)).fetchone()[0]


def obligation(db, ident):
    return db.execute('''SELECT d.*,COALESCE((SELECT SUM(o.amount_kop) FROM obligation_payments p JOIN operations o ON o.id=p.operation_id WHERE p.obligation_id=d.id),0) AS paid FROM obligations d WHERE d.id=?''',(ident,)).fetchone()


def settle(db, uid, owner, ident, version, amount, account_id, token, update_id):
    require_manager(db,uid,owner)
    with db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT operation_id FROM obligation_payments WHERE token=?',(token,)).fetchone()
        if old: return old[0]
        r=obligation(db,ident)
        if not r or r['cancelled'] or r['version']!=version: raise ValueError('Платёж изменился. Открой его заново.')
        amount_valid(amount,r['currency'])
        if amount>r['amount_kop']-r['paid']: raise ValueError('Сумма выше оставшегося долга.')
        a=active_account(db,account_id)
        kind='expense' if r['direction']=='payable' else 'income'
        if kind=='expense' and ledger.balances(db,a['owner_id'])[a['id']][1][r['currency']]<amount: raise ValueError('На счёте недостаточно денег.')
        project=db.execute('SELECT id FROM projects WHERE owner_id=? ORDER BY id LIMIT 1',(a['owner_id'],)).fetchone()
        cur=db.execute('''INSERT INTO operations(update_id,user_id,occurred_on,kind,project_id,category,cost_type,source,account_id,amount_kop,currency,comment) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (update_id,a['owner_id'],today().isoformat(),kind,project[0] if project else None,r['stage'],r['classification'] if kind=='expense' else 'Прочее',r['classification'] if kind=='income' else 'Прочее',a['id'],amount,r['currency'],f"Платёж №{ident}, {r['counterparty']}: {r['purpose']}"))
        db.execute('INSERT INTO obligation_payments(token,obligation_id,operation_id) VALUES (?,?,?)',(token,ident,cur.lastrowid))
        db.execute('UPDATE obligations SET version=version+1 WHERE id=?',(ident,))
        ledger.bump_revision(db)
    return cur.lastrowid


def change_obligation(db, uid, owner, ident, version, action, value=None):
    require_manager(db,uid,owner)
    with db:
        db.execute('BEGIN IMMEDIATE')
        r=obligation(db,ident)
        if not r or r['version']!=version: raise ValueError('Запись изменилась. Открой заново.')
        if action=='cancel':
            if r['paid'] or r['cancelled']: raise ValueError('Платёж с проведённой оплатой отменить нельзя. Сначала исправь связанные операции.')
            db.execute('UPDATE obligations SET cancelled=1,version=version+1 WHERE id=?',(ident,))
        elif action=='due' and not r['cancelled']:
            db.execute('UPDATE obligations SET due_on=?,version=version+1 WHERE id=?',(ledger.valid_date(value),ident))
        else: raise ValueError('Действие недоступно.')


def week_start(day): return day-timedelta(days=day.weekday())


def weekly_text(db, end=None):
    end=end or week_start(today())-timedelta(days=1)
    start=end-timedelta(days=6)
    rows=ledger.rows_for(db,start.isoformat(),end.isoformat())
    result=[f'Недельная сводка: {start:%d.%m.%Y} - {end:%d.%m.%Y}']
    for currency in ledger.CURRENCIES:
        inc=sum(r['amount_kop'] for r in rows if r['kind']=='income' and r['currency']==currency)
        exp=sum(r['amount_kop'] for r in rows if r['kind']=='expense' and r['currency']==currency)
        result += [f'\n{currency}',f'Приход: {ledger.money(inc,currency)}',f'Расход: {ledger.money(exp,currency)}',f'Изменение денег: {ledger.money(inc-exp,currency)}']
        stages={}
        for r in rows:
            if r['kind']=='expense' and r['currency']==currency: stages[r['category'] or 'Прочее']=stages.get(r['category'] or 'Прочее',0)+r['amount_kop']
        for label,amount in sorted(stages.items(),key=lambda p:-p[1])[:3]: result.append(f'  {label}: {ledger.money(amount,currency)}')
    missing=db.execute("SELECT COUNT(*) FROM operations o WHERE kind='expense' AND occurred_on BETWEEN ? AND ? AND NOT EXISTS(SELECT 1 FROM receipts WHERE operation_id=o.id)",(start.isoformat(),end.isoformat())).fetchone()[0]
    pending=db.execute("SELECT COUNT(*) FROM funding_requests WHERE status='pending'").fetchone()[0]
    overdue=sum(1 for r in db.execute('SELECT id FROM obligations WHERE cancelled=0 AND due_on<?',(today().isoformat(),)) if (lambda d:d['paid']<d['amount_kop'])(obligation(db,r[0])))
    result += [f'\nРасходов без чека за неделю: {missing}',f'Сейчас заявок на согласовании: {pending}',f'Сейчас просроченных платежей: {overdue}','Переводы внутри команды исключены из прихода и расхода.']
    return '\n'.join(result)
