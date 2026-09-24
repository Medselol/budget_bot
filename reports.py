"""Read-only participant views and calendar-period reports."""
from datetime import timedelta
import bot as ledger

PERIODS = [('За год', 'year'), ('За месяц', 'month'), ('За неделю', 'week'), ('За день', 'day'), ('Весь период', 'all')]


def period_dates(period, today=None):
    today = today or ledger.datetime.now(ledger.TZ).date()
    starts = {'year': today.replace(month=1, day=1), 'month': today.replace(day=1),
              'week': today-timedelta(days=today.weekday()), 'day': today, 'all': None}
    if period not in starts: raise ValueError('Неизвестный период')
    start = starts[period]
    return start.isoformat() if start else None, today.isoformat()


def participant(db, target):
    return db.execute('SELECT id,name,active FROM users WHERE id=? AND deleted=0', (target,)).fetchone()


def pick_participant(api, db, chat, prefix, page=0, archived=False):
    users = db.execute('SELECT id,name,active FROM users WHERE deleted=0 AND active=? ORDER BY name,id', (0 if archived else 1,)).fetchall()
    route = 'archive' if archived else 'page'
    page = max(0, min(page, max(0, (len(users)-1)//12)))
    options = [(r['name']+(' (отключён)' if not r['active'] else ''), f"{prefix}:{r['id']}") for r in users[page*12:(page+1)*12]]
    if prefix.startswith('overview:who:'):
        options.insert(0, ('Все участники', prefix+':all'))
    if page: options.append(('← Назад', f'{prefix}:{route}:{page-1}'))
    if (page+1)*12 < len(users): options.append(('Далее →', f'{prefix}:{route}:{page+1}'))
    options.append(('Действующие участники', f'{prefix}:page:0') if archived else ('Архив участников', f'{prefix}:archive:0'))
    options.append(('Меню', 'menu'))
    api.send(chat, 'Архив участников:' if archived else 'Выбери участника:', options)


def account_lines(db, target):
    return [name+': '+ '; '.join(ledger.money(sums[c],c) for c in ledger.CURRENCIES)
            for name,sums in ledger.balances(db,target).values()]


def callback(api, db, chat, uid, value, owner):
    if value == 'all:menu':
        if ledger.reader(db,uid,owner):
            api.send(chat, 'Общий отчёт. Выбери период (текущий календарный год, месяц, неделя с понедельника или сегодня):', [(label,'overview:period:'+period) for label,period in PERIODS])
        return True
    if not value.startswith(('overview:', 'people:', 'csvuser:')): return False
    if not ledger.reader(db,uid,owner):
        api.send(chat,'Недостаточно прав.'); return True
    parts = value.split(':')
    try:
        if parts[:2] == ['overview','period'] and len(parts)==3:
            period_dates(parts[2])
            pick_participant(api,db,chat,'overview:who:'+parts[2]);return True
        if parts[:2] == ['overview','who']:
            period=parts[2];start,end=period_dates(period)
            if len(parts)==5 and parts[3] in ('page','archive'):
                pick_participant(api,db,chat,'overview:who:'+period,int(parts[4]),archived=parts[3]=='archive');return True
            if len(parts)!=4: return True
            target=None if parts[3]=='all' else int(parts[3])
            if target is not None and not participant(db,target): raise ValueError('Участник не найден')
            ledger.show_report(api,db,chat,start,end,None,target,viewer_id=uid)
            return True
        if value=='people:menu':
            pick_participant(api,db,chat,'people:view');return True
        if parts[:2]==['people','view'] and len(parts)==4 and parts[2] in ('page','archive'):
            pick_participant(api,db,chat,'people:view',int(parts[3]),archived=parts[2]=='archive');return True
        if parts[:2]==['people','view'] and len(parts)==3:
            target=int(parts[2]);u=participant(db,target)
            if not u: raise ValueError('Участник не найден')
            text=u['name']+'\nСчета и остатки на сегодня (с начала учёта):\n'+'\n'.join(account_lines(db,target))
            for i in range(0,len(text),3900): api.send(chat,text[i:i+3900])
            api.send(chat,'Посмотреть операции или сформировать PDF:', [('Приходы, расходы и переводы',f'people:ops:{target}:0')]+[(label,f'overview:who:{period}:{target}') for label,period in PERIODS]+[('Все участники','people:menu')]);return True
        if parts[:2]==['people','ops'] and len(parts)==4:
            target,page=int(parts[2]),int(parts[3]);u=participant(db,target)
            if not u: raise ValueError('Участник не найден')
            rows=list(reversed(ledger.rows_for(db,user_id=target)))
            page=max(0,min(page,max(0,(len(rows)-1)//8)))
            options=[]
            for r in rows[page*8:(page+1)*8]:
                title={'income':'Приход','expense':'Расход','transfer':'Перевод'}[r['kind']]
                options.append((f"{r['occurred_on']} · {title} · {ledger.money(r['amount_kop'],r['currency'])}",f"people:op:{target}:{r['id']}"))
            if page: options.append(('← Назад',f'people:ops:{target}:{page-1}'))
            if (page+1)*8<len(rows): options.append(('Далее →',f'people:ops:{target}:{page+1}'))
            options.append(('Счета и отчёты',f'people:view:{target}'))
            api.send(chat,u['name']+f' · Операции: {len(rows)}',options);return True
        if parts[:2]==['people','op'] and len(parts)==4:
            target,ident=int(parts[2]),int(parts[3])
            row=ledger.operation_row(db,ident,target)
            if row is None: raise ValueError('Операция не найдена')
            options=[('К операциям',f'people:ops:{target}:0')]
            if ledger.manager(db,uid,owner): options.insert(0,('Открыть для редактирования',f'op:view:{ident}'))
            api.send(chat,ledger.operation_text(db,row),options);return True
        if parts[0]=='csvuser' and len(parts)==4:
            target=int(parts[1]);u=participant(db,target)
            if not u: raise ValueError('Участник не найден')
            start=ledger.valid_date(parts[2]) if parts[2]!='0' else None
            end=ledger.valid_date(parts[3]) if parts[3]!='0' else None
            if start and end and end<start: raise ValueError('Неверный период')
            api.document(chat,'stroika_report.csv',ledger.csv_report(ledger.rows_for(db,start,end,user_id=target),start,end,ledger.DEFAULT_PROJECT_NAME+' · '+u['name']))
            return True
    except (ValueError,IndexError,OverflowError):
        api.send(chat,'Кнопка устарела. Открой отчёт заново.',[('Общий отчёт','all:menu')])
    return True
