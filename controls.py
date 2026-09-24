"""Telegram control centre. Every entry point rechecks current role and ownership."""
import json
import sqlite3
import uuid
from datetime import timedelta
import bot as ledger
import control_data as data
import maintenance

PAGE=6
FIELDS={
    'request':[('account_id','own_account','На какой свой счёт получить деньги?'),('currency','currency','Валюта заявки:'),('amount_kop','amount','Какую сумму запросить?'),('purpose','purpose','На что нужны деньги?'),('due_on','date','Когда нужны деньги? Введи ГГГГ-ММ-ДД:')],
    'debt':[('direction','direction','Какой платёж запланировать?'),('counterparty','counterparty','Кому должны или от кого ожидаете деньги?'),('purpose','purpose','Назначение платежа:'),('stage','stage','Этап стройки:'),('classification','classification','Категория оплаты:'),('currency','currency','Валюта:'),('amount_kop','amount','Полная сумма обязательства:'),('due_on','date','Срок оплаты, ГГГГ-ММ-ДД:')],
    'budget':[('stage','stage','Выбери этап:'),('currency','currency','Валюта плана:'),('amount_kop','budget_amount','Новый полный бюджет этапа. 0 означает нулевой план:')],
    'pay':[('account_id','account','С какого счёта платим / куда получаем деньги?'),('amount_kop','amount','Сумма фактического платежа (можно частично):')],
    'fund':[('account_id','account','Из какой кассы выдать деньги?')],
    'reject':[('reason','purpose','Причина отказа:')],
    'due':[('due_on','date','Новая дата оплаты, ГГГГ-ММ-ДД:')],
}
LABELS={'account_id':'Счёт','currency':'Валюта','amount_kop':'Сумма','purpose':'Назначение','due_on':'Срок','direction':'Направление','counterparty':'Контрагент','stage':'Этап','reason':'Причина','classification':'Категория оплаты'}


def send_long(api,chat,text,buttons=None):
    lines=text.splitlines();parts=[];part=''
    for line in lines:
        if len(part)+len(line)>3400: parts.append(part);part=''
        part+=line+'\n'
    parts.append(part.rstrip())
    for i,p in enumerate(parts): api.send(chat,p,buttons if i==len(parts)-1 else None)


def accounts(db,uid=None):
    from access import accounts as all_accounts
    return [r for r in all_accounts(db) if uid is None or r['owner_id']==uid]


def account_name(db,ident):
    from transfer_edit import account_label
    return account_label(db,ident)


def root(api,db,chat,uid,owner):
    role=ledger.role_for(db,uid,owner)
    opts=[]
    opts.append(('Заявки на деньги','ctl:req:list:open:0'))
    if ledger.reader(db,uid,owner):
        opts += [('Бюджет план / факт','ctl:budget:UAH:0'),('Долги и платежи','ctl:debt:list:open:0'),('Недельная сводка','ctl:weekly')]
    if ledger.manager(db,uid,owner): opts.append(('Журнал изменений','ctl:audit:0'))
    if uid==owner: opts.append(('Резервные копии','ctl:backup'))
    api.send(chat,'Контроль стройки. Выбери раздел:',opts+[('Главное меню','menu')])


def can_form(db,uid,owner,flow):
    if flow=='request':
        if ledger.role_for(db,uid,owner) not in ('owner','editor','foreman','member'): raise ValueError('Нельзя подавать заявки с этой ролью.')
    else: data.require_manager(db,uid,owner)


def start_form(api,db,chat,uid,owner,flow,**extra):
    can_form(db,uid,owner,flow)
    d={'step':'ctl_form','flow':flow,'index':0,'values':{},'nonce':uuid.uuid4().hex[:16],**extra}
    ledger.set_draft(db,uid,d);prompt(api,db,chat,uid,d)


def prompt(api,db,chat,uid,d):
    specs=FIELDS[d['flow']]
    if d['index']>=len(specs):
        lines=['Проверь перед подтверждением:']
        for k,_,_ in specs:
            val=d['values'][k]
            if k=='account_id': val=account_name(db,val)
            elif k=='direction': val='Мы должны' if val=='payable' else 'Нам должны'
            elif k=='amount_kop': val=ledger.money(val,d['values'].get('currency',d.get('currency','UAH')))
            lines.append(f'{LABELS[k]}: {val}')
        if d.get('context'): lines.append(d['context'])
        if d['flow']=='request': lines.append('Сначала сохранится черновик. Можно прикрепить счёт и отправить на согласование.')
        if d['flow'] in ('pay','fund'): lines.append('Подтверди только после фактической передачи денег. Будет создана операция и изменён баланс.')
        api.send(chat,'\n'.join(lines),[('Подтвердить',f"ctl:save:{d['nonce']}"),('Отмена','cancel')]);return
    key,kind,title=specs[d['index']];opts=[]
    prefix=f"ctl:choose:{d['nonce']}:"
    if kind in ('account','own_account'):
        opts=[(r['user_name']+' · '+r['name'],prefix+str(r['id'])) for r in accounts(db,uid if kind=='own_account' else None)]
    elif kind=='currency': opts=[(c,prefix+c) for c in ledger.CURRENCIES]
    elif kind=='stage': opts=[(s,prefix+str(i)) for i,s in enumerate(ledger.STAGES)]
    elif kind=='classification':
        labels=ledger.COST_TYPES if d['values']['direction']=='payable' else ledger.SOURCES
        opts=[(label,prefix+str(i)) for i,label in enumerate(labels)]
    elif kind=='direction': opts=[('Мы должны',prefix+'payable'),('Нам должны',prefix+'receivable')]
    elif kind=='date': opts=[('Сегодня',prefix+'today')]
    api.send(chat,title,opts+[('Отмена','cancel')])


def take_value(api,db,chat,uid,d,raw,choice=False):
    specs=FIELDS[d['flow']]
    if d['index']>=len(specs): prompt(api,db,chat,uid,d);return
    key,kind,_=specs[d['index']]
    if kind in ('account','own_account'):
        val=int(raw)
        if not choice or val not in {r['id'] for r in accounts(db,uid if kind=='own_account' else None)}: raise ValueError('Выбери счёт кнопкой.')
    elif kind=='currency':
        if not choice or raw not in ledger.CURRENCIES: raise ValueError('Выбери валюту кнопкой.')
        val=raw
    elif kind=='stage':
        if not choice or not raw.isdigit() or int(raw)>=len(ledger.STAGES): raise ValueError('Выбери этап кнопкой.')
        val=ledger.STAGES[int(raw)]
    elif kind=='classification':
        labels=ledger.COST_TYPES if d['values']['direction']=='payable' else ledger.SOURCES
        if not choice or not raw.isdigit() or int(raw)>=len(labels): raise ValueError('Выбери категорию кнопкой.')
        val=labels[int(raw)]
    elif kind=='direction':
        if not choice or raw not in ('payable','receivable'): raise ValueError('Выбери направление кнопкой.')
        val=raw
    elif kind=='date': val=data.today().isoformat() if raw=='today' and choice else ledger.valid_date(raw)
    elif kind in ('amount','budget_amount'): val=0 if kind=='budget_amount' and raw.strip()=='0' else ledger.parse_amount(raw)
    else:
        val=raw.strip();maximum=120 if kind=='counterparty' else 500
        if not 3<=len(val)<=maximum: raise ValueError(f'Напиши от 3 до {maximum} символов.')
    d['values'][key]=val;d['index']+=1
    if d['flow']=='budget' and key=='currency':
        old=db.execute('SELECT version FROM budgets WHERE stage=? AND currency=?',(d['values']['stage'],val)).fetchone()
        d['version']=old[0] if old else 0
    ledger.set_draft(db,uid,d);prompt(api,db,chat,uid,d)


def req_card(api,db,chat,uid,owner,ident):
    r=data.request(db,ident,uid,owner);opts=[]
    n=db.execute('SELECT COUNT(*) FROM request_files WHERE request_id=?',(ident,)).fetchone()[0]
    if n: opts.append((f'Счета и документы ({n})',f'ctl:req:files:{ident}'))
    if ledger.role_for(db,uid,owner)!='investor' and r['user_id']==uid and r['status']=='draft': opts += [('Прикрепить счёт',f'ctl:req:attach:{ident}'),('Отправить на согласование',f'ctl:req:submit:{ident}')]
    if ledger.role_for(db,uid,owner)!='investor' and r['user_id']==uid and r['status'] in ('draft','pending'): opts.append(('Отменить заявку',f'ctl:req:cancel:{ident}'))
    if ledger.manager(db,uid,owner) and r['status']=='pending': opts += [('Одобрить',f'ctl:req:approve:{ident}'),('Отклонить',f'ctl:req:reject:{ident}')]
    if ledger.manager(db,uid,owner) and r['status']=='approved': opts.append(('Подтвердить выдачу денег',f'ctl:req:fund:{ident}'))
    opts.append(('К заявкам','ctl:req:list:open:0'))
    text=f"Заявка №{ident} · {data.REQUEST_STATUS[r['status']]}\nУчастник: {r['user_name'] or r['user_id']}\n{ledger.money(r['amount_kop'],r['currency'])}\nНазначение: {r['purpose']}\nНужны к: {r['due_on']}\nПолучатель: {account_name(db,r['account_id'])}"
    if r['decision']: text+='\nРешение: '+r['decision']
    if r['operation_id']: text+=f"\nПеревод №{r['operation_id']}"
    api.send(chat,text,opts)


def req_list(api,db,chat,uid,owner,mode,page):
    shared=ledger.reader(db,uid,owner);args=[]
    where="status IN ('draft','pending','approved')" if mode=='open' else "status IN ('paid','rejected','cancelled')"
    if shared: where+=" AND (status!='draft' OR user_id=?)";args.append(uid)
    else: where+=' AND user_id=?';args.append(uid)
    rows=db.execute('SELECT * FROM funding_requests WHERE '+where+' ORDER BY id DESC LIMIT ? OFFSET ?',(*args,PAGE+1,page*PAGE)).fetchall()
    opts=[(f"№{r['id']} · {ledger.money(r['amount_kop'],r['currency'])} · {data.REQUEST_STATUS[r['status']]}",f"ctl:req:view:{r['id']}") for r in rows[:PAGE]]
    opts+=pagination(f'ctl:req:list:{mode}',page,len(rows))
    if ledger.role_for(db,uid,owner)!='investor': opts.append(('Новая заявка','ctl:new:request'))
    opts += [('Завершённые' if mode=='open' else 'Открытые',f"ctl:req:list:{'closed' if mode=='open' else 'open'}:0"),('Контроль','ctl:home')]
    api.send(chat,('Открытые заявки' if mode=='open' else 'Завершённые заявки')+('\nЗаявок нет.' if not rows else ''),opts)


def pagination(prefix,page,count):
    opts=[]
    if page: opts.append(('Предыдущие',f'{prefix}:{page-1}'))
    if count>PAGE: opts.append(('Следующие',f'{prefix}:{page+1}'))
    return opts


def debt_state(r):
    if r['cancelled']: return 'Отменён'
    if r['paid']>=r['amount_kop']: return 'Оплачен'
    return 'Просрочен' if r['due_on']<data.today().isoformat() else 'Ожидает оплаты'


def debt_card(api,db,chat,uid,owner,ident):
    data.require_reader(db,uid,owner);r=data.obligation(db,ident)
    if not r: raise ValueError('Платёж не найден.')
    opts=[]
    if ledger.manager(db,uid,owner) and not r['cancelled'] and r['paid']<r['amount_kop']:
        opts += [('Внести оплату',f'ctl:debt:pay:{ident}'),('Перенести срок',f'ctl:debt:due:{ident}')]
        if not r['paid']: opts.append(('Отменить обязательство',f'ctl:debt:cancel:{ident}'))
    payments=db.execute('SELECT operation_id FROM obligation_payments WHERE obligation_id=? ORDER BY id DESC',(ident,)).fetchall()
    opts += [('История оплат',f'ctl:debt:payments:{ident}:0')] if payments else []
    opts += [('К платежам','ctl:debt:list:open:0')]
    api.send(chat,f"Платёж №{ident} · {debt_state(r)}\n{'Мы должны' if r['direction']=='payable' else 'Нам должны'}: {r['counterparty']}\n{r['purpose']}\nЭтап: {r['stage']}\nКатегория: {r['classification']}\nСрок: {r['due_on']}\nВсего: {ledger.money(r['amount_kop'],r['currency'])}\nОплачено: {ledger.money(r['paid'],r['currency'])}\nОсталось: {ledger.money(r['amount_kop']-r['paid'],r['currency'])}\nСоздание обязательства не меняет баланс. Оплата создаёт приход или расход. Не записывай её повторно.",opts)


def debt_list(api,db,chat,uid,owner,mode,page):
    data.require_reader(db,uid,owner)
    rows=[data.obligation(db,r[0]) for r in db.execute('SELECT id FROM obligations ORDER BY due_on,id')]
    rows=[r for r in rows if ((not r['cancelled'] and r['paid']<r['amount_kop']) if mode=='open' else (r['cancelled'] or r['paid']>=r['amount_kop']))]
    selected=rows[page*PAGE:(page+1)*PAGE+1]
    opts=[(f"№{r['id']} · {r['counterparty'][:25]} · {r['due_on']} · {debt_state(r)}",f"ctl:debt:view:{r['id']}") for r in selected[:PAGE]]
    opts+=pagination(f'ctl:debt:list:{mode}',page,len(selected))
    if ledger.manager(db,uid,owner): opts.append(('Добавить платёж / долг','ctl:new:debt'))
    opts += [('Завершённые' if mode=='open' else 'Открытые',f"ctl:debt:list:{'closed' if mode=='open' else 'open'}:0"),('Контроль','ctl:home')]
    lines=['Долги и предстоящие платежи' if mode=='open' else 'Завершённые платежи']
    if mode=='open':
        for currency in ledger.CURRENCIES:
            for direction,label in [('payable','Мы должны'),('receivable','Нам должны')]:
                total=sum(r['amount_kop']-r['paid'] for r in rows if r['currency']==currency and r['direction']==direction)
                lines.append(f'{label}: {ledger.money(total,currency)}')
    if not rows: lines.append('Записей нет.')
    api.send(chat,'\n'.join(lines),opts)


def budget_text(db,currency,rows=None):
    rows=data.budget_rows(db,currency) if rows is None else rows
    lines=[f'Бюджет / факт · {currency} · с начала учёта']
    for stage,plan,fact in rows:
        lines.append('\n'+stage)
        lines.append('План: '+('не задан' if plan is None else ledger.money(plan,currency)))
        lines.append('Факт: '+ledger.money(fact,currency))
        if plan is not None: lines.append(('Перерасход: ' if fact>plan else 'Осталось: ')+ledger.money(abs(plan-fact),currency))
    return '\n'.join(lines)


def budget_card(api,db,chat,uid,owner,currency,page):
    data.require_reader(db,uid,owner)
    if currency not in ledger.CURRENCIES: return
    rows=data.budget_rows(db,currency)
    text=budget_text(db,currency,rows[page*PAGE:(page+1)*PAGE])
    text+=f"\n\nСумма заданных планов: {ledger.money(sum(p or 0 for _,p,_ in rows),currency)}\nВсего расходов: {ledger.money(sum(f for _,_,f in rows),currency)}"
    unplanned=sum(f for _,p,f in rows if p is None)
    text+=f'\nРасходы без заданного плана: {ledger.money(unplanned,currency)}'
    opts=pagination(f'ctl:budget:{currency}',page,len(rows)-page*PAGE)
    opts += [(c,f'ctl:budget:{c}:0') for c in ledger.CURRENCIES if c!=currency]
    if ledger.manager(db,uid,owner): opts.append(('Задать / изменить план','ctl:new:budget'))
    opts += [('План / факт PDF','ctl:budgetpdf'),('Контроль','ctl:home')]
    api.send(chat,text,opts)


def confirm_action(api,db,chat,uid,action,ident,version,text):
    d={'step':'ctl_action','action':action,'id':ident,'version':version,'nonce':uuid.uuid4().hex[:16]}
    ledger.set_draft(db,uid,d)
    api.send(chat,text,[('Подтвердить',f"ctl:confirm:{d['nonce']}"),('Отмена','cancel')])


def save_form(api,db,chat,uid,owner,update_id,d):
    flow=d['flow'];v=d['values'];can_form(db,uid,owner,flow)
    if d['index']!=len(FIELDS[flow]): raise ValueError('Сначала заполни форму.')
    if flow=='request': ident=data.create_request(db,uid,owner,v,d['nonce'])
    elif flow=='debt': ident=data.create_obligation(db,uid,owner,v,d['nonce'])
    elif flow=='budget': data.set_budget(db,uid,owner,v['stage'],v['currency'],v['amount_kop'],d['version'])
    elif flow=='fund': data.pay_request(db,uid,owner,d['id'],d['version'],v['account_id'],update_id)
    elif flow=='pay': ident=data.settle(db,uid,owner,d['id'],d['version'],v['amount_kop'],v['account_id'],d['nonce'],update_id)
    elif flow=='reject': data.decide_request(db,uid,owner,d['id'],d['version'],'reject',v['reason'])
    elif flow=='due': data.change_obligation(db,uid,owner,d['id'],d['version'],'due',v['due_on'])
    ledger.clear_draft(db,uid)
    if flow=='request': req_card(api,db,chat,uid,owner,ident)
    elif flow in ('fund','reject'): req_card(api,db,chat,uid,owner,d['id'])
    elif flow=='debt': debt_card(api,db,chat,uid,owner,ident)
    elif flow=='due': debt_card(api,db,chat,uid,owner,d['id'])
    elif flow=='pay':
        api.send(chat,f'Оплата сохранена. Операция №{ident}.',[('К платежу',f"ctl:debt:view:{d['id']}")])
        if db.execute('SELECT kind FROM operations WHERE id=?',(ident,)).fetchone()[0]=='expense':
            from receipts import begin
            begin(api,db,chat,uid,ident)
        else: debt_card(api,db,chat,uid,owner,d['id'])
    elif flow=='budget': budget_card(api,db,chat,uid,owner,v['currency'],0)


def callback(api,db,chat,uid,update_id,value,owner):
    if not value.startswith('ctl:'): return False
    if chat!=uid or not ledger.role_for(db,uid,owner): return True
    try: _callback(api,db,chat,uid,update_id,value,owner)
    except (ValueError,sqlite3.IntegrityError) as exc: api.send(chat,str(exc),[('Контроль','ctl:home')])
    except (IndexError,KeyError): api.send(chat,'Кнопка устарела. Открой раздел заново.',[('Контроль','ctl:home')])
    return True


def _callback(api,db,chat,uid,update_id,value,owner):
    p=value.split(':');d=ledger.draft(db,uid) or {}
    if value=='ctl:home': ledger.clear_draft(db,uid);root(api,db,chat,uid,owner)
    elif p[1]=='new' and len(p)==3 and p[2] in ('request','debt','budget'): start_form(api,db,chat,uid,owner,p[2])
    elif p[1]=='choose' and len(p)==4:
        if d.get('step')!='ctl_form' or d.get('nonce')!=p[2]: raise ValueError('Эта кнопка устарела. Открой форму заново.')
        can_form(db,uid,owner,d['flow']);take_value(api,db,chat,uid,d,p[3],True)
    elif p[1]=='save' and len(p)==3:
        if d.get('step')!='ctl_form' or d.get('nonce')!=p[2]: raise ValueError('Форма уже сохранена или закрыта.')
        save_form(api,db,chat,uid,owner,update_id,d)
    elif p[1]=='confirm' and len(p)==3:
        if d.get('step')!='ctl_action' or d.get('nonce')!=p[2]: raise ValueError('Подтверждение устарело.')
        if d['action']=='debt_cancel':
            data.change_obligation(db,uid,owner,d['id'],d['version'],'cancel');ledger.clear_draft(db,uid);debt_card(api,db,chat,uid,owner,d['id'])
        else:
            data.decide_request(db,uid,owner,d['id'],d['version'],d['action']);ledger.clear_draft(db,uid);req_card(api,db,chat,uid,owner,d['id'])
    elif p[1]=='req':
        if p[2]=='list' and len(p)==5:
            req_list(api,db,chat,uid,owner,p[3],max(0,int(p[4])));return
        if len(p)!=4: return
        action=p[2];ident=int(p[3]);r=data.request(db,ident,uid,owner)
        if action=='view':
            ledger.clear_draft(db,uid);req_card(api,db,chat,uid,owner,ident)
        elif action=='files':
            for f in db.execute('SELECT filename,content FROM request_files WHERE request_id=?',(ident,)): api.document(chat,f['filename'],f['content'])
            req_card(api,db,chat,uid,owner,ident)
        elif action=='attach':
            if ledger.role_for(db,uid,owner)=='investor' or r['user_id']!=uid or r['status']!='draft': raise ValueError('Документы можно добавить в свою заявку до отправки.')
            ledger.set_draft(db,uid,{'step':'ctl_upload','id':ident})
            api.send(chat,'Отправь фото или PDF до 10 МБ. Можно до 10 файлов.',[('Готово',f'ctl:req:view:{ident}')])
        elif action in ('submit','cancel','approve'):
            if action=='approve': data.require_manager(db,uid,owner)
            elif r['user_id']!=uid: raise ValueError('Это чужая заявка.')
            verb={'submit':'Отправить на согласование','cancel':'Отменить','approve':'Одобрить'}[action]
            confirm_action(api,db,chat,uid,action,ident,r['version'],f"{verb} заявку №{ident} на {ledger.money(r['amount_kop'],r['currency'])}? Балансы пока не меняются.")
        elif action in ('reject','fund'):
            data.require_manager(db,uid,owner)
            start_form(api,db,chat,uid,owner,action,id=ident,version=r['version'],currency=r['currency'],context=f"Заявка №{ident}: {ledger.money(r['amount_kop'],r['currency'])}. Получатель: {account_name(db,r['account_id'])}.")
    elif p[1]=='debt':
        data.require_reader(db,uid,owner)
        if p[2]=='list' and len(p)==5: debt_list(api,db,chat,uid,owner,p[3],max(0,int(p[4])));return
        ident=int(p[3]);r=data.obligation(db,ident)
        if not r: raise ValueError('Платёж не найден.')
        if p[2]=='view': debt_card(api,db,chat,uid,owner,ident)
        elif p[2]=='payments':
            page=max(0,int(p[4]));rows=db.execute('SELECT operation_id FROM obligation_payments WHERE obligation_id=? ORDER BY id DESC LIMIT ? OFFSET ?',(ident,PAGE+1,page*PAGE)).fetchall()
            api.send(chat,f'Оплаты по обязательству №{ident}:',[(f'Операция №{a[0]}',f'ctl:operation:{a[0]}') for a in rows[:PAGE]]+pagination(f'ctl:debt:payments:{ident}',page,len(rows))+[('К платежу',f'ctl:debt:view:{ident}')])
        elif p[2]=='cancel':
            data.require_manager(db,uid,owner)
            confirm_action(api,db,chat,uid,'debt_cancel',ident,r['version'],f'Отменить обязательство №{ident}? Запись останется в завершённых.')
        elif p[2] in ('pay','due'):
            start_form(api,db,chat,uid,owner,p[2],id=ident,version=r['version'],currency=r['currency'],context=f"Платёж №{ident}: {r['counterparty']}. Остаток: {ledger.money(r['amount_kop']-r['paid'],r['currency'])}.")
    elif p[1]=='operation':
        data.require_reader(db,uid,owner);ident=int(p[2]);r=db.execute('SELECT * FROM operations WHERE id=?',(ident,)).fetchone()
        if not r: raise ValueError('Операция удалена.')
        opts=[('Чеки',f'rc:view:{ident}')] if r['kind']=='expense' else []
        if ledger.manager(db,uid,owner): opts += [('Редактировать',f'op:edit:{ident}'),('Удалить',f'op:delete:{ident}')]
        api.send(chat,ledger.operation_text(db,r),opts)
    elif p[1]=='budget' and len(p)==4: budget_card(api,db,chat,uid,owner,p[2],max(0,int(p[3])))
    elif value=='ctl:budgetpdf':
        data.require_reader(db,uid,owner)
        from control_pdf import budget_pdf
        api.document(chat,'budget_plan_fact.pdf',budget_pdf(db));budget_card(api,db,chat,uid,owner,'UAH',0)
    elif p[1]=='audit':
        data.require_manager(db,uid,owner);page=max(0,int(p[2]))
        rows=db.execute('SELECT l.*,u.name AS actor FROM audit_log l LEFT JOIN users u ON u.id=l.actor_id ORDER BY l.id DESC LIMIT ? OFFSET ?',(PAGE+1,page*PAGE)).fetchall()
        opts=[(f"№{r['id']} · {r['actor_name'] or r['actor'] or ('Система' if r['actor_id']==0 else 'Участник')} · {entity_name(r['entity'])} {r['entity_id']}",f"ctl:event:{r['id']}") for r in rows[:PAGE]]
        api.send(chat,'Журнал изменений с момента обновления. Время записей: UTC.',opts+pagination('ctl:audit',page,len(rows)))
    elif p[1]=='event':
        data.require_manager(db,uid,owner)
        r=db.execute('SELECT l.*,u.name AS actor FROM audit_log l LEFT JOIN users u ON u.id=l.actor_id WHERE l.id=?',(int(p[2]),)).fetchone()
        if r:
            from audit_log import detail
            send_long(api,chat,f"Изменение №{r['id']} · {r['happened_at']} UTC\nКто: {r['actor_name'] or r['actor'] or ('Система' if r['actor_id']==0 else 'Участник')} (ID {r['actor_id']})\n{entity_name(r['entity'])} №{r['entity_id']} · "+{'INSERT':'Создание','UPDATE':'Изменение','DELETE':'Удаление'}[r['action']]+'\n\n'+detail(r),[('К журналу','ctl:audit:0')])
    elif p[1]=='weekly':
        data.require_reader(db,uid,owner)
        if len(p)==3 and p[2] in ('on','off'): maintenance.subscribe(db,uid,owner,p[2]=='on')
        s=db.execute('SELECT enabled FROM weekly_subscriptions WHERE user_id=?',(uid,)).fetchone();enabled=bool(s and s[0])
        api.send(chat,'Сводка за завершённую неделю. Автоотправка по понедельникам после 09:00 (Киев).\nТвоя подписка: '+('включена' if enabled else 'выключена'),[('Показать сейчас','ctl:weeklynow'),('PDF за прошлую неделю','ctl:weeklypdf'),('Отключить' if enabled else 'Включить для меня','ctl:weekly:off' if enabled else 'ctl:weekly:on')])
    elif value in ('ctl:weeklynow','ctl:weeklypdf'):
        data.require_reader(db,uid,owner)
        if value.endswith('pdf'):
            end=data.week_start(data.today())-timedelta(days=1);start=end-timedelta(days=6)
            ledger.show_report(api,db,chat,start.isoformat(),end.isoformat(),None,user_id=None,viewer_id=uid)
        else: api.send(chat,data.weekly_text(db),[('Настроить сводку','ctl:weekly')])
    elif p[1]=='backup':
        if uid!=owner: raise ValueError('Полная копия базы доступна только владельцу.')
        if len(p)==3 and p[2]=='create': maintenance.create_backup(db)
        if len(p)==3 and p[2]=='download':
            path=maintenance.latest_backup(db)
            if not path: path=maintenance.create_backup(db)
            if path.stat().st_size>maintenance.MAX_DOWNLOAD: raise ValueError('Копия больше 45 МБ. Скачай её из постоянного диска сервиса; отправка через бота недоступна.')
            api.document(chat,path.name,path.read_bytes())
        last=db.execute("SELECT value FROM settings WHERE key='backup_last'").fetchone()
        error=db.execute("SELECT value FROM settings WHERE key='backup_error'").fetchone()
        api.send(chat,'Резервные копии\nАвтоматически каждый день при работающем боте. Хранятся копии за последние 7 дней с успешным сохранением.\nПоследняя: '+(last[0] if last else 'ещё не создана')+('\n'+error[0] if error else '')+'\nКопия включает операции, пользователей и документы. Хранится на том же диске сервиса: скачивай её отдельно для защиты от потери диска.',[('Создать сейчас','ctl:backup:create'),('Скачать последнюю','ctl:backup:download')])


def entity_name(name):
    return {'operations':'Операция','users':'Участник','accounts':'Счёт','projects':'Объект','expense_reviews':'Проверка чека','receipts':'Чек','budgets':'Бюджет','funding_requests':'Заявка','request_files':'Документ заявки','obligations':'Обязательство','obligation_payments':'Оплата','weekly_subscriptions':'Подписка'}.get(name,name)


def message(api,db,chat,uid,text,owner):
    d=ledger.draft(db,uid) or {}
    if text.startswith('/') or d.get('step')!='ctl_form': return False
    try:
        can_form(db,uid,owner,d['flow']);take_value(api,db,chat,uid,d,text)
    except ValueError as exc: api.send(chat,str(exc))
    return True


def media(api,db,chat,uid,payload,update_id,owner):
    d=ledger.draft(db,uid) or {}
    if d.get('step')!='ctl_upload': return False
    try:
        r=data.request(db,d['id'],uid,owner)
        if ledger.role_for(db,uid,owner)=='investor' or r['user_id']!=uid or r['status']!='draft': raise ValueError('Заявка уже отправлена. Документы изменять нельзя.')
        if db.execute('SELECT 1 FROM request_files WHERE update_id=?',(update_id,)).fetchone(): return True
        if db.execute('SELECT count(*) FROM request_files WHERE request_id=?',(r['id'],)).fetchone()[0]>=10: raise ValueError('В заявке уже 10 файлов.')
        from attachments import download
        content,ext=download(api,payload)
        with db:
            db.execute('INSERT INTO request_files(request_id,update_id,filename,content) VALUES (?,?,?,?)',(r['id'],update_id,f"invoice_{r['id']}_{update_id}{ext}",content))
        api.send(chat,'Документ сохранён.',[('К заявке',f"ctl:req:view:{r['id']}")])
    except ValueError as exc: api.send(chat,str(exc))
    return True
