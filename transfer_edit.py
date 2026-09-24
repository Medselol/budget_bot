"""Manager-only correction of recorded transfers, with optimistic confirmation."""
from datetime import date
import bot as ledger

FIELDS = ('from_account_id','to_account_id','amount_kop','currency','occurred_on','comment')

def snapshot(row):
    return {k: row[k] for k in (*FIELDS,'target_amount_kop','target_currency','exchange_rate')}

def account_label(db, aid):
    r=db.execute('SELECT u.name,a.name FROM accounts a JOIN users u ON u.id=a.owner_id WHERE a.id=?',(aid,)).fetchone()
    return ' · '.join(r) if r else 'Недоступен'

def preview(api,db,chat,d):
    v=d['values']
    api.send(chat, f"Перевод №{d['id']}\nОткуда: {account_label(db,v['from_account_id'])}\nКуда: {account_label(db,v['to_account_id'])}\nСумма: {ledger.money(v['amount_kop'],v['currency'])}\nДата: {v['occurred_on']}\nКомментарий: {v['comment'] or '—'}\nИзменения применятся после подтверждения.",
        [(label,'tx:field:'+key) for key,label in [('from_account_id','Откуда'),('to_account_id','Куда'),('amount_kop','Сумма'),('currency','Валюта'),('occurred_on','Дата'),('comment','Комментарий')]]+[('Подтвердить изменения','tx:save'),('Отмена','cancel')])

def callback(api,db,chat,uid,value,owner):
    is_op=value.startswith(('op:view:','op:edit:','op:delete:','op:delconfirm:'))
    if not is_op and not value.startswith('tx:'): return False
    if is_op:
        try: ident=int(value.rsplit(':',1)[1])
        except ValueError: return False
        row=db.execute("SELECT * FROM operations WHERE id=? AND kind='transfer'",(ident,)).fetchone()
        if not row: return False
    if not ledger.manager(db,uid,owner):
        if is_op and value.startswith('op:view:'): return False
        api.send(chat,'Изменять переводы может только владелец или главный администратор.');return True
    d=ledger.draft(db,uid)
    if is_op:
        action=value.split(':')[1]
        if action=='view':
            api.send(chat,ledger.operation_text(db,row), [('Редактировать',f'op:edit:{ident}'),('Удалить',f'op:delete:{ident}'),('К списку','ops:list')]);return True
        if action in ('edit','delete'):
            d={'step':'tx_edit' if action=='edit' else 'tx_delete','id':ident,'original':snapshot(row),'values':snapshot(row)}
            ledger.set_draft(db,uid,d)
            if action=='edit': preview(api,db,chat,d)
            else: api.send(chat,f'Удалить перевод №{ident}? Его сумма будет исключена из балансов обоих счетов и отчётов. Фактического возврата денег это не выполняет.', [('Да, удалить',f'op:delconfirm:{ident}'),('Отмена','cancel')])
            return True
        if not d or d.get('step')!='tx_delete' or d.get('id')!=ident:
            api.send(chat,'Сначала открой перевод и нажми «Удалить».');return True
    elif not d or d.get('step')!='tx_edit':
        api.send(chat,'Открой перевод заново.');return True
    if value.startswith('tx:field:'):
        field=value.rsplit(':',1)[1]
        if field not in FIELDS: return True
        d['field']=field;ledger.set_draft(db,uid,d)
        if field in ('from_account_id','to_account_id'):
            rows=db.execute('SELECT a.id,u.name,a.name FROM accounts a JOIN users u ON u.id=a.owner_id WHERE (u.active=1 AND u.deleted=0) OR a.id IN (?,?)', (d['original']['from_account_id'],d['original']['to_account_id'])).fetchall()
            api.send(chat,'Выбери счёт:',[(f'{r[1]} · {r[2]}',f'tx:account:{r[0]}') for r in rows]+[('Отмена','cancel')])
        elif field=='currency': api.send(chat,'Валюта:',[(c,'tx:currency:'+c) for c in ledger.CURRENCIES])
        else: api.send(chat,{'amount_kop':'Введи новую сумму:','occurred_on':'Введи дату ГГГГ-ММ-ДД:','comment':'Введи комментарий или «-», чтобы убрать его:'}[field])
        return True
    if value.startswith(('tx:account:','tx:currency:')):
        field=d.get('field');raw=value.rsplit(':',1)[1]
        if value.startswith('tx:account:') and field in ('from_account_id','to_account_id') and raw.isdigit():
            aid=int(raw)
            if not db.execute('SELECT a.id FROM accounts a JOIN users u ON u.id=a.owner_id WHERE a.id=? AND ((u.active=1 AND u.deleted=0) OR a.id IN (?,?))',(aid,d['original']['from_account_id'],d['original']['to_account_id'])).fetchone(): return True
            d['values'][field]=aid
        elif value.startswith('tx:currency:') and field=='currency' and raw in ledger.CURRENCIES: d['values'][field]=raw
        else: return True
        d.pop('field',None);ledger.set_draft(db,uid,d);preview(api,db,chat,d);return True
    if value=='tx:save' or value.startswith('op:delconfirm:'):
        deleting=d['step']=='tx_delete'
        v=d['values']
        if not deleting and v['from_account_id']==v['to_account_id']:
            api.send(chat,'Счета отправителя и получателя должны отличаться.');return True
        with db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute("SELECT * FROM operations WHERE id=? AND kind='transfer'",(d['id'],)).fetchone()
            if not current or snapshot(current)!=d['original']:
                api.send(chat,'Запись уже изменена или удалена. Открой её заново.');return True
            if not deleting and current['target_currency']:
                api.send(chat,'Открой форму обмена валют заново.');return True
            if deleting: db.execute('DELETE FROM operations WHERE id=?',(d['id'],))
            else: db.execute('UPDATE operations SET '+','.join(k+'=?' for k in FIELDS)+' WHERE id=?',tuple(v[k] for k in FIELDS)+(d['id'],))
            ledger.bump_revision(db)
        ledger.clear_draft(db,uid)
        api.send(chat,'Перевод удалён.' if deleting else 'Перевод обновлён.')
        ledger.show_operations(api,db,chat,None)
        return True
    return True

def message(api,db,chat,uid,text,owner):
    d=ledger.draft(db,uid)
    if text.startswith('/') or not d or d.get('step')!='tx_edit': return False
    if not ledger.manager(db,uid,owner):
        ledger.clear_draft(db,uid);return True
    field=d.get('field')
    try:
        if field=='amount_kop': val=ledger.parse_amount(text)
        elif field=='occurred_on': val=date.fromisoformat(text.strip()).isoformat()
        elif field=='comment': val='' if text.strip()=='-' else text[:1000]
        else: return True
    except ValueError:
        api.send(chat,'Проверь формат суммы или даты и попробуй ещё раз.');return True
    d['values'][field]=val;d.pop('field',None)
    ledger.set_draft(db,uid,d);preview(api,db,chat,d);return True
