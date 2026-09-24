"""Append-only application audit. Does not log tokens or attachment contents."""
import json
import sqlite3
from contextlib import contextmanager

class Connection(sqlite3.Connection):
    actor_id = 0


def register(db):
    db.create_function('audit_actor',0,lambda: db.actor_id)


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, happened_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        actor_id INTEGER NOT NULL, actor_name TEXT NOT NULL DEFAULT '', entity TEXT NOT NULL, entity_id INTEGER, action TEXT NOT NULL, before_json TEXT, after_json TEXT);
    CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT,'Журнал изменений нельзя редактировать'); END;
    CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT,'Журнал изменений нельзя удалять'); END;
    ''')
    tables=('operations','users','accounts','projects','expense_reviews','receipts','budgets','funding_requests','request_files','obligations','obligation_payments','weekly_subscriptions')
    for table in tables:
        cols=[r['name'] for r in db.execute(f'PRAGMA table_info({table})') if r['name'] not in ('content','file_id','token')]
        pk='operation_id' if table=='expense_reviews' else 'user_id' if table=='weekly_subscriptions' else 'id'
        def obj(prefix):
            return 'json_object('+','.join(f"'{k}',{prefix}.{k}" for k in cols)+')'
        for action in ('INSERT','UPDATE','DELETE'):
            before=obj('OLD') if action!='INSERT' else 'NULL'
            after=obj('NEW') if action!='DELETE' else 'NULL'
            ident=f"{'OLD' if action=='DELETE' else 'NEW'}.{pk}"
            when=f'WHEN {before} IS NOT {after}' if action=='UPDATE' else ''
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS audit_{table}_{action.lower()} AFTER {action} ON {table} {when}
                BEGIN INSERT INTO audit_log(actor_id,actor_name,entity,entity_id,action,before_json,after_json)
                VALUES(audit_actor(),COALESCE((SELECT name FROM users WHERE id=audit_actor()),''),'{table}',{ident},'{action}',{before},{after}); END''')
    db.commit()


@contextmanager
def actor(db, uid):
    old=db.actor_id;db.actor_id=uid
    try: yield
    finally: db.actor_id=old


def detail(row):
    before=json.loads(row['before_json'] or '{}');after=json.loads(row['after_json'] or '{}')
    lines=[]
    for k in dict.fromkeys([*before,*after]):
        if before.get(k)!=after.get(k): lines.append(f'{FIELD_NAMES.get(k,k)}: {before.get(k,"—")} → {after.get(k,"—")}')
    return '\n'.join(lines)

FIELD_NAMES = {'id':'Номер','user_id':'Участник ID','owner_id':'Владелец счёта ID','name':'Имя / название','active':'Доступ','deleted':'Удалён из списка','role':'Роль','kind':'Тип','occurred_on':'Дата','amount_kop':'Сумма в копейках / центах','currency':'Валюта','comment':'Комментарий','category':'Этап','cost_type':'Тип затрат','source':'Источник','account_id':'Счёт ID','from_account_id':'Откуда, счёт ID','to_account_id':'Куда, счёт ID','status':'Статус','reason':'Пояснение','reviewer_id':'Проверил ID','operation_id':'Операция №','purpose':'Назначение','due_on':'Срок','direction':'Направление','counterparty':'Контрагент','decision':'Решение','stage':'Этап','cancelled':'Отменён','filename':'Файл','request_id':'Заявка №','obligation_id':'Платёж №','created_by':'Создал ID','enabled':'Включено'}
