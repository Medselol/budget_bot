"""Atomic, manager-only exchange between one participant's own currency balances."""
import re
import uuid
from decimal import Decimal, ROUND_HALF_UP
import bot as ledger

DEFAULT_RATE='44.5'
FIELDS=('from_account_id','to_account_id','amount_kop','currency','target_amount_kop','target_currency','exchange_rate','occurred_on','comment')


def init(db):
    cols={r['name'] for r in db.execute('PRAGMA table_info(operations)')}
    added=False
    for name,decl in (('target_amount_kop','INTEGER'),('target_currency','TEXT'),('exchange_rate','TEXT')):
        if name not in cols:
            db.execute(f'ALTER TABLE operations ADD COLUMN {name} {decl}');added=True
    if added:
        for action in ('insert','update','delete'): db.execute('DROP TRIGGER IF EXISTS audit_operations_'+action)
    for action in ('INSERT','UPDATE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS exchange_valid_{action.lower()} BEFORE {action} ON operations
        WHEN (NEW.target_currency IS NOT NULL OR NEW.target_amount_kop IS NOT NULL OR NEW.exchange_rate IS NOT NULL)
        AND (NEW.kind!='transfer' OR NEW.target_currency IS NULL OR NEW.target_currency NOT IN ('UAH','USD')
          OR NEW.target_currency=NEW.currency OR NEW.target_amount_kop IS NULL OR NEW.target_amount_kop<=0
          OR NEW.exchange_rate IS NULL OR CAST(NEW.exchange_rate AS REAL)<=0
          OR NEW.from_account_id IS NULL OR NEW.to_account_id IS NULL
          OR NOT EXISTS(SELECT 1 FROM accounts a JOIN accounts b ON b.id=NEW.to_account_id
            WHERE a.id=NEW.from_account_id AND a.owner_id=b.owner_id AND a.owner_id=NEW.user_id))
        BEGIN SELECT RAISE(ABORT,'Некорректные данные обмена валют.'); END''')
    db.commit()


def rate(text):
    text=text.strip().replace(',','.')
    if not re.fullmatch(r'\d{1,5}(?:\.\d{1,6})?',text) or not Decimal('0.000001')<=Decimal(text)<=Decimal('10000'):
        raise ValueError('Курс: положительное число до 10000, максимум 6 знаков после запятой. Например 41,50.')
    return format(Decimal(text).normalize(),'f')


def converted(amount, currency, exchange_rate):
    r=Decimal(rate(exchange_rate))
    if currency not in ledger.CURRENCIES or type(amount)!=int or not 0<amount<=100_000_000_000:
        raise ValueError('Проверь сумму и валюту.')
    value=Decimal(amount)*r if currency=='USD' else Decimal(amount)/r
    result=int(value.quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    if not 0<result<=100_000_000_000: raise ValueError('Получаемая сумма слишком мала или велика.')
    return result


def snapshot(row): return {k:row[k] for k in FIELDS}


def save(db,uid,owner,d,update_id):
    if not ledger.manager(db,uid,owner): raise ValueError('Обмен доступен только администраторам.')
    v={k:d.get(k) for k in FIELDS};v['comment']=v['comment'] or ''
    v['exchange_rate']=rate(v['exchange_rate']);v['occurred_on']=ledger.valid_date(v['occurred_on'])
    v['target_currency']='UAH' if v['currency']=='USD' else 'USD'
    v['target_amount_kop']=converted(v['amount_kop'],v['currency'],v['exchange_rate'])
    account_owner=d.get('ledger_user_id',uid)
    with db:
        db.execute('BEGIN IMMEDIATE')
        old=None
        if d.get('edit_id'):
            old=db.execute('SELECT * FROM operations WHERE id=?',(d['edit_id'],)).fetchone()
            if not old or not old['target_currency'] or snapshot(old)!=d['original'] or old['user_id']!=account_owner:
                raise ValueError('Операция уже изменена. Открой её заново.')
        elif account_owner!=uid: raise ValueError('Можно обменивать только на своих счетах.')
        elif db.execute('SELECT 1 FROM operations WHERE update_id=?',(update_id,)).fetchone():
            return db.execute('SELECT id FROM operations WHERE update_id=?',(update_id,)).fetchone()[0]
        for key in ('from_account_id','to_account_id'):
            if not db.execute('SELECT 1 FROM accounts a JOIN users u ON u.id=a.owner_id WHERE a.id=? AND a.owner_id=? AND u.active=1 AND u.deleted=0',(v[key],account_owner)).fetchone():
                raise ValueError('Выбери действующие счета одного участника.')
        balance=ledger.balances(db,account_owner)[v['from_account_id']][1][v['currency']]
        if old:
            if old['from_account_id']==v['from_account_id'] and old['currency']==v['currency']: balance+=old['amount_kop']
            if old['to_account_id']==v['from_account_id'] and old['target_currency']==v['currency']: balance-=old['target_amount_kop']
        if balance<v['amount_kop']: raise ValueError('На счёте списания недостаточно денег в выбранной валюте.')
        if old:
            db.execute('UPDATE operations SET '+','.join(k+'=?' for k in FIELDS)+' WHERE id=?',tuple(v[k] for k in FIELDS)+(old['id'],));ident=old['id']
        else:
            cur=db.execute('INSERT INTO operations(update_id,user_id,kind,'+','.join(FIELDS)+') VALUES (?,?,\'transfer\','+','.join('?' for _ in FIELDS)+')',(update_id,uid,*[v[k] for k in FIELDS]));ident=cur.lastrowid
        ledger.bump_revision(db)
    return ident


def prompt(api,db,chat,d):
    uid=d['ledger_user_id'];step=d['step'];prefix='fx:'+d['nonce']+':'
    if step=='fx_direction': api.send(chat,'Обмен валют. Выбери направление:', [('USD → UAH',prefix+'USD'),('UAH → USD',prefix+'UAH')])
    elif step in ('fx_from','fx_to'):
        currency=d['currency'] if step=='fx_from' else d['target_currency']
        balances=ledger.balances(db,uid)
        opts=[(r['name']+' · '+ledger.money(balances[r['id']][1][currency],currency),prefix+str(r['id'])) for r in ledger.names(db,'accounts',uid)]
        api.send(chat,('С какого счёта списать ' if step=='fx_from' else 'На какой счёт зачислить ')+currency+'? Можно обменять валюту внутри одной кассы.',opts)
    elif step=='fx_amount': api.send(chat,'Сколько '+d['currency']+' списать? Введи сумму:')
    elif step=='fx_rate':
        side='покупки доллара обменником' if d['currency']=='USD' else 'продажи доллара обменником'
        api.send(chat,'Курс по умолчанию: 1 USD = 44,50 грн. Нажми кнопку или введи другой курс вручную. Для этого направления используй курс '+side+'.\n\nКурс установлен вручную, автоматического обновления нет.', [('Использовать 44,50',prefix+'default_rate')])
    elif step=='fx_date': api.send(chat,'Дата обмена:', [('Сегодня',prefix+'today'),('Вчера',prefix+'yesterday'),('Другая дата',prefix+'custom')])
    elif step=='fx_date_text': api.send(chat,'Дата обмена: ГГГГ-ММ-ДД.')
    elif step=='fx_comment': api.send(chat,'Комментарий к обмену:', [('Пропустить',prefix+'skip')])
    else:
        from transfer_edit import account_label
        api.send(chat,'Обмен валют\nСписать: '+ledger.money(d['amount_kop'],d['currency'])+'\nОткуда: '+account_label(db,d['from_account_id'])+'\nЗачислить: '+ledger.money(converted(d['amount_kop'],d['currency'],d['exchange_rate']),d['target_currency'])+'\nКуда: '+account_label(db,d['to_account_id'])+'\nКурс: 1 USD = '+d['exchange_rate']+' UAH\nДата: '+d['occurred_on']+'\nКомментарий: '+(d.get('comment') or '—')+'\n\nЭто запись фактического обмена, не банковская операция. Комиссию при наличии запиши отдельным расходом.', [('Подтвердить обмен',prefix+'save'),('Отмена','cancel')])


def callback(api,db,chat,uid,update_id,value,owner):
    editing=value.startswith('op:edit:')
    row=None
    if editing:
        try: row=db.execute('SELECT * FROM operations WHERE id=?',(int(value.rsplit(':',1)[1]),)).fetchone()
        except ValueError: return False
        if not row or not row['target_currency']: return False
    if not editing and not value.startswith('fx:'): return False
    if not ledger.manager(db,uid,owner):
        api.send(chat,'Обмен доступен только администраторам.');return True
    try:
        d=ledger.draft(db,uid) or {}
        if value=='fx:new' or editing:
            d={'step':'fx_direction','nonce':uuid.uuid4().hex[:16],'ledger_user_id':row['user_id'] if row else uid}
            if row: d.update(edit_id=row['id'],original=snapshot(row))
        else:
            parts=value.split(':')
            if len(parts)!=3 or parts[1]!=d.get('nonce') or not d.get('step','').startswith('fx_'): raise ValueError('Кнопка устарела. Начни обмен заново.')
            choice=parts[2];step=d['step']
            if step=='fx_direction' and choice in ledger.CURRENCIES: d.update(currency=choice,target_currency='UAH' if choice=='USD' else 'USD',step='fx_from')
            elif step in ('fx_from','fx_to') and choice.isdigit():
                if not db.execute('SELECT 1 FROM accounts WHERE id=? AND owner_id=?',(int(choice),d['ledger_user_id'])).fetchone(): raise ValueError('Счёт недоступен.')
                d['from_account_id' if step=='fx_from' else 'to_account_id']=int(choice);d['step']='fx_to' if step=='fx_from' else 'fx_amount'
            elif step=='fx_rate' and choice=='default_rate':
                converted(d['amount_kop'],d['currency'],DEFAULT_RATE)
                d.update(exchange_rate=DEFAULT_RATE,step='fx_date')
            elif step=='fx_date' and choice in ('today','yesterday','custom'):
                from datetime import timedelta
                if choice=='custom': d['step']='fx_date_text'
                else: d.update(occurred_on=(ledger.datetime.now(ledger.TZ).date()-timedelta(days=choice=='yesterday')).isoformat(),step='fx_comment')
            elif step=='fx_comment' and choice=='skip': d.update(comment='',step='fx_confirm')
            elif step=='fx_confirm' and choice=='save':
                ident=save(db,uid,owner,d,update_id);ledger.clear_draft(db,uid)
                api.send(chat,f'Обмен №{ident} сохранён. Остатки обеих валют обновлены.', [('Открыть операцию',f'op:view:{ident}'),('Мой баланс','balance'),('Ещё обмен','fx:new')]);return True
            else: raise ValueError('Продолжи текущий шаг обмена.')
        ledger.set_draft(db,uid,d);prompt(api,db,chat,d)
    except (ValueError,KeyError) as exc: api.send(chat,str(exc),[('Новый обмен','fx:new')])
    return True


def message(api,db,chat,uid,text,owner):
    d=ledger.draft(db,uid) or {};step=d.get('step','')
    if not step.startswith('fx_') or text.startswith('/'): return False
    if not ledger.manager(db,uid,owner): ledger.clear_draft(db,uid);return True
    try:
        if step=='fx_amount': d.update(amount_kop=ledger.parse_amount(text),step='fx_rate')
        elif step=='fx_rate':
            r=rate(text);converted(d['amount_kop'],d['currency'],r);d.update(exchange_rate=r,step='fx_date')
        elif step=='fx_date_text': d.update(occurred_on=ledger.valid_date(text),step='fx_comment')
        elif step=='fx_comment':
            if len(text)>500: raise ValueError('Комментарий до 500 символов.')
            d.update(comment=text.strip(),step='fx_confirm')
        else: prompt(api,db,chat,d);return True
        ledger.set_draft(db,uid,d);prompt(api,db,chat,d)
    except ValueError as exc: api.send(chat,str(exc))
    return True
