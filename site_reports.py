"""Construction photo journal, separate from financial operations and receipts."""
from datetime import date, timedelta
import uuid
import bot as ledger
from control_data import today
import attachments

STATUSES = {'draft':'Черновик', 'submitted':'На проверке', 'accepted':'Принят', 'rework':'На доработке'}
FIELDS = {'stage':'Этап', 'work_date':'Дата работ', 'area':'Место / секция', 'work':'Что выполнено', 'problems':'Проблемы', 'next_work':'Следующий шаг'}
PERIODS = {'all':'Весь период', 'day':'Сегодня', 'week':'Эта неделя', 'month':'Этот месяц', 'year':'Этот год', 'custom':'Свои даты'}


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS site_reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
        stage TEXT NOT NULL DEFAULT '', work_date TEXT NOT NULL, area TEXT NOT NULL DEFAULT '',
        work TEXT NOT NULL DEFAULT '', problems TEXT NOT NULL DEFAULT '', next_work TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','submitted','accepted','rework')),
        review_note TEXT NOT NULL DEFAULT '', reviewer_id INTEGER, version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, submitted_at TEXT, reviewed_at TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS site_one_draft ON site_reports(user_id) WHERE status='draft';
    CREATE INDEX IF NOT EXISTS site_report_dates ON site_reports(work_date,status,user_id);
    CREATE TABLE IF NOT EXISTS site_photos(
        id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER NOT NULL REFERENCES site_reports(id) ON DELETE CASCADE,
        update_id INTEGER NOT NULL UNIQUE, filename TEXT NOT NULL, content BLOB NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    ''')
    db.commit()
    import site_notifications
    site_notifications.init(db)


def role(db, uid, owner):
    value = ledger.role_for(db,uid,owner)
    if not value: raise ValueError('Доступ закрыт.')
    return value


def report(db, ident, uid, owner, edit=False):
    current = role(db,uid,owner)
    r = db.execute('SELECT s.*,u.name AS author FROM site_reports s JOIN users u ON u.id=s.user_id WHERE s.id=?',(ident,)).fetchone()
    if not r or (r['user_id'] != uid and (current not in ('owner','editor','investor') or r['status']=='draft')):
        raise ValueError('Фотоотчёт недоступен.')
    if edit and (current=='investor' or r['user_id']!=uid or r['status'] not in ('draft','rework')):
        raise ValueError('Менять можно только свой черновик или отчёт на доработке.')
    return r


def new(db,uid,owner):
    if role(db,uid,owner)=='investor': raise ValueError('У инвестора доступ только для просмотра.')
    with db:
        db.execute("INSERT OR IGNORE INTO site_reports(user_id,work_date) VALUES (?,?)",(uid,today().isoformat()))
    return db.execute("SELECT id FROM site_reports WHERE user_id=? AND status='draft'",(uid,)).fetchone()[0]


def change(db,uid,owner,ident,version,field,value):
    report(db,ident,uid,owner,True)
    if field not in FIELDS: raise ValueError('Неизвестное поле.')
    value=value.strip()
    if field=='stage' and value not in ledger.STAGES: raise ValueError('Выбери этап кнопкой.')
    if field=='work_date':
        value=ledger.valid_date(value)
        if value>today().isoformat(): raise ValueError('Дата работ не может быть в будущем.')
    if field=='work' and not value: raise ValueError('Напиши, что выполнено.')
    limit=120 if field=='area' else 700
    if len(value)>limit: raise ValueError(f'Не больше {limit} символов.')
    with db:
        cur=db.execute(f'UPDATE site_reports SET {field}=?,version=version+1 WHERE id=? AND version=?',(value,ident,version))
        if not cur.rowcount: raise ValueError('Отчёт изменился. Открой его заново.')


def transition(db,uid,owner,ident,version,action,note=''):
    r=report(db,ident,uid,owner)
    if r['version']!=version: raise ValueError('Отчёт изменился. Открой его заново.')
    if action in ('submit','discard'):
        report(db,ident,uid,owner,True)
        if action=='discard' and r['status']!='draft': raise ValueError('Удалить можно только черновик.')
        if action=='submit':
            count=db.execute('SELECT count(*) FROM site_photos WHERE report_id=?',(ident,)).fetchone()[0]
            if not r['stage'] or not r['work'] or not count: raise ValueError('Укажи этап, выполненные работы и добавь хотя бы одно фото.')
            target='submitted'
    else:
        if not ledger.manager(db,uid,owner): raise ValueError('Проверять отчёты может администратор.')
        if r['status']!='submitted': raise ValueError('Отчёт уже проверен или ещё не отправлен.')
        if action not in ('accept','rework'): raise ValueError('Неизвестное действие.')
        if action=='rework' and not 3<=len(note.strip())<=700: raise ValueError('Напиши замечание: от 3 до 700 символов.')
        target='accepted' if action=='accept' else 'rework'
    with db:
        if action=='discard': db.execute('DELETE FROM site_reports WHERE id=? AND version=?',(ident,version))
        elif action=='submit':
            cur=db.execute("UPDATE site_reports SET status=?,submitted_at=CURRENT_TIMESTAMP,version=version+1 WHERE id=? AND version=?",(target,ident,version))
        else:
            cur=db.execute('UPDATE site_reports SET status=?,review_note=?,reviewer_id=?,reviewed_at=CURRENT_TIMESTAMP,version=version+1 WHERE id=? AND version=?',(target,note.strip(),uid,ident,version))
        if action!='discard':
            if not cur.rowcount: raise ValueError('Отчёт изменился. Открой его заново.')
            from site_notifications import enqueue
            enqueue(db,ident,owner)


def save_photo(db,uid,owner,ident,update_id,content,ext):
    report(db,ident,uid,owner,True)
    if ext not in ('.jpg','.png') or not content or len(content)>attachments.LIMIT: raise ValueError('Нужно фото JPG или PNG до 10 МБ.')
    with db:
        if db.execute('SELECT 1 FROM site_photos WHERE update_id=?',(update_id,)).fetchone(): return
        if db.execute('SELECT count(*) FROM site_photos WHERE report_id=?',(ident,)).fetchone()[0]>=10:
            raise ValueError('В одном отчёте максимум 10 фото. Создай следующий отчёт для остальных.')
        db.execute('INSERT INTO site_photos(report_id,update_id,filename,content) VALUES (?,?,?,?)',(ident,update_id,f'stroyka_{ident}_{update_id}{ext}',content))
        db.execute('UPDATE site_reports SET version=version+1 WHERE id=?',(ident,))



def filters(db,uid):
    return (ledger.draft(db,uid) or {}).get('site_filters',{})


def require_photo_removal(db,ident,uid,owner):
    r=report(db,ident,uid,owner)
    if not ledger.manager(db,uid,owner): report(db,ident,uid,owner,True)
    return r


def remove_photo(db,uid,owner,ident,photo_id,version):
    with db:
        db.execute('BEGIN IMMEDIATE')
        r=require_photo_removal(db,ident,uid,owner)
        if r['version']!=version: raise ValueError('Отчёт изменился. Открой его заново.')
        cur=db.execute('DELETE FROM site_photos WHERE id=? AND report_id=?',(photo_id,ident))
        if not cur.rowcount: raise ValueError('Фото уже удалено.')
        db.execute('UPDATE site_reports SET version=version+1 WHERE id=?',(ident,))
        db.execute("UPDATE site_notifications SET version=version+1 WHERE report_id=? AND version=? AND state='pending'",(ident,version))


def state(db,uid,step,**extra):
    d=dict(step=step,site_filters=filters(db,uid),nonce=uuid.uuid4().hex[:12],**extra)
    ledger.set_draft(db,uid,d)
    return d


def home(api,db,chat,uid,owner):
    current=role(db,uid,owner)
    state(db,uid,'site_home')
    opts=[]
    if current!='investor': opts.append(('＋ Новый фотоотчёт','site:new'))
    opts += [('Журнал фотоотчётов','site:list:0')]
    api.send(chat,'Фотоотчёты стройки\nЭтап, дата работ, фотографии и замечания.\nЧужие черновики скрыты в общем журнале. Для нескольких этапов создай отдельные отчёты.',opts)


def card(api,db,chat,uid,owner,ident):
    r=report(db,ident,uid,owner)
    state(db,uid,'site_card',ident=ident)
    n=db.execute('SELECT count(*) FROM site_photos WHERE report_id=?',(ident,)).fetchone()[0]
    text=f"Фотоотчёт №{ident} · {STATUSES[r['status']]}\n{ledger.DEFAULT_PROJECT_NAME}\nАвтор: {r['author']}\n"+'\n'.join(f"{label}: {r[key] or '—'}" for key,label in FIELDS.items())+f'\nФото: {n}/10'
    if r['review_note']: text+='\nЗамечание администратора: '+r['review_note']
    if r['reviewed_at']: text+='\nПоследняя проверка (UTC): '+r['reviewed_at']
    opts=[('Смотреть фотографии',f'site:photos:{ident}')]
    if r['user_id']==uid and r['status'] in ('draft','rework') and role(db,uid,owner)!='investor':
        opts += [(label,f'site:edit:{ident}:{field}') for field,label in FIELDS.items()]
        opts += [('Добавить фото',f'site:upload:{ident}'),('Отправить на проверку',f'site:submit:{ident}:{r["version"]}')]
        if r['status']=='draft': opts += [('Удалить черновик',f'site:discard:{ident}:{r["version"]}')]
    if ledger.manager(db,uid,owner) and r['status']=='submitted':
        opts += [('Принять',f'site:accept:{ident}:{r["version"]}'),('Вернуть с замечанием',f'site:rework:{ident}:{r["version"]}')]
    opts += [('К журналу','site:list:0')]
    api.send(chat,text,opts)


def edit_prompt(api,db,chat,uid,owner,ident,field,wizard=False):
    r=report(db,ident,uid,owner,True)
    if field not in FIELDS: raise ValueError('Неизвестное поле.')
    d=state(db,uid,'site_edit',ident=ident,version=r['version'],field=field,wizard=wizard)
    opts=[('К отчёту',f'site:view:{ident}')]
    if field=='stage': opts=[(s,f'site:stage:{d["nonce"]}:{i}') for i,s in enumerate(ledger.STAGES)]+opts
    elif field in ('area','problems','next_work'): opts=[('Пропустить / без записи',f'site:empty:{d["nonce"]}')]+opts
    elif field=='work_date': opts=[('Сегодня',f'site:date:{d["nonce"]}:today'),('Вчера',f'site:date:{d["nonce"]}:yesterday')]+opts
    hints={'work_date':'Выбери дату работ или введи её: ГГГГ-ММ-ДД.', 'area':'Где выполнены работы? Например: Дуплекс 2, секция А, первый этаж.', 'work':'Напиши, что выполнено. Если можешь, укажи объём: например, кладка 25 м².', 'problems':'Есть проблемы, задержки или что нужно согласовать?', 'next_work':'Что планируется сделать следующим шагом?', 'stage':'Выбери этап стройки.'}
    api.send(chat,hints[field],opts)


def after_field(api,db,chat,uid,owner,d):
    if not d.get('wizard'): card(api,db,chat,uid,owner,d['ident']);return
    fields=list(FIELDS);index=fields.index(d['field'])+1
    if index<len(fields): edit_prompt(api,db,chat,uid,owner,d['ident'],fields[index],True)
    else:
        state(db,uid,'site_upload',ident=d['ident'])
        api.send(chat,'Теперь пришли фото — по одному или альбомом, до 10 фото по 10 МБ. Затем нажми «Готово» и проверь отчёт перед отправкой.',[('Готово',f'site:view:{d["ident"]}')])


def list_rows(db,uid,owner,f,page=0):
    current=role(db,uid,owner)
    where=['(s.status!=\'draft\' OR s.user_id=?)'];args=[uid]
    if current not in ('owner','editor','investor'): where+=['s.user_id=?'];args+=[uid]
    if f.get('author'): where+=['s.user_id=?'];args+=[int(f['author'])]
    if f.get('stage'): where+=['s.stage=?'];args+=[f['stage']]
    if f.get('status'): where+=['s.status=?'];args+=[f['status']]
    end=today();period=f.get('period','all');start=None
    if period=='day': start=end
    elif period=='week': start=end-timedelta(days=end.weekday())
    elif period=='month': start=end.replace(day=1)
    elif period=='year': start=end.replace(month=1,day=1)
    elif period=='custom': start=date.fromisoformat(f['start']);end=date.fromisoformat(f['end'])
    if start: where+=['s.work_date BETWEEN ? AND ?'];args +=[start.isoformat(),end.isoformat()]
    return db.execute('SELECT s.*,u.name AS author FROM site_reports s JOIN users u ON u.id=s.user_id WHERE '+' AND '.join(where)+' ORDER BY s.work_date DESC,s.id DESC LIMIT 7 OFFSET ?',args+[max(0,page)*6]).fetchall()


def listing(api,db,chat,uid,owner,page=0):
    f=filters(db,uid);rows=list_rows(db,uid,owner,f,page)
    state(db,uid,'site_list')
    labels=[PERIODS[f.get('period','all')],f.get('stage','Все этапы'),STATUSES.get(f.get('status'),'Все статусы')]
    if f.get('period')=='custom': labels += [f['start']+' — '+f['end']]
    if f.get('author'):
        r=db.execute('SELECT name FROM users WHERE id=?',(f['author'],)).fetchone();labels += [r[0] if r else 'Участник']
    opts=[(f"№{r['id']} · {r['work_date']} · {r['stage'] or 'Черновик'} · {r['author']}",f"site:view:{r['id']}") for r in rows[:6]]
    if page: opts += [('← Предыдущие',f'site:list:{page-1}')]
    if len(rows)>6: opts += [('Следующие →',f'site:list:{page+1}')]
    opts += [('Период','site:filter:period:0'),('Этап','site:filter:stage:0'),('Статус','site:filter:status:0')]
    if ledger.reader(db,uid,owner): opts += [('Прораб / автор','site:filter:author:0')]
    opts += [('Сбросить фильтры','site:reset'),('Фотоотчёты','site:home')]
    api.send(chat,'Журнал стройки · '+ ' · '.join(labels)+ ('\nВыбери отчёт.' if rows else '\nОтчётов по этим условиям нет.'),opts)


def callback(api,db,chat,uid,update_id,value,owner):
    if not value.startswith('site:'): return False
    try:
        if chat!=uid: raise ValueError('Открой личный чат с ботом.')
        role(db,uid,owner);p=value.split(':');cmd=p[1]
        if cmd=='home': home(api,db,chat,uid,owner)
        elif cmd=='new':
            ident=new(db,uid,owner);r=report(db,ident,uid,owner)
            if not r['stage']: edit_prompt(api,db,chat,uid,owner,ident,'stage',True)
            else: card(api,db,chat,uid,owner,ident)
        elif cmd=='view': card(api,db,chat,uid,owner,int(p[2]))
        elif cmd=='list': listing(api,db,chat,uid,owner,int(p[2]))
        elif cmd=='reset': ledger.clear_draft(db,uid);listing(api,db,chat,uid,owner)
        elif cmd=='filter':
            field=p[2];page=max(0,int(p[3]));d=state(db,uid,'site_filter',field=field)
            if field=='period': choices=list(PERIODS.items())
            elif field=='stage': choices=[('all','Все этапы')]+[(str(i),s) for i,s in enumerate(ledger.STAGES)]
            elif field=='status': choices=[('all','Все статусы')]+list(STATUSES.items())
            elif field=='author' and ledger.reader(db,uid,owner):
                choices=[('all','Все участники')]+[(str(r['user_id']),r['name']) for r in db.execute("SELECT DISTINCT s.user_id,u.name FROM site_reports s JOIN users u ON u.id=s.user_id WHERE s.status!='draft' OR s.user_id=? ORDER BY u.name",(uid,))]
            else: raise ValueError('Фильтр недоступен.')
            opts=[(label,f'site:pick:{d["nonce"]}:{key}') for key,label in choices[page*8:page*8+8]]
            if page: opts += [('← Предыдущие',f'site:filter:{field}:{page-1}')]
            if len(choices)>(page+1)*8: opts += [('Следующие →',f'site:filter:{field}:{page+1}')]
            api.send(chat,'Выбери фильтр:',opts+[('К журналу','site:list:0')])
        elif cmd=='pick':
            d=ledger.draft(db,uid) or {}
            if d.get('step')!='site_filter' or d.get('nonce')!=p[2]: raise ValueError('Открой фильтр заново.')
            field=d['field'];key=p[3];f=d['site_filters']
            if field=='period' and key=='custom':
                state(db,uid,'site_dates');api.send(chat,'Введи начало и конец: 2026-09-01 2026-09-30',[('К журналу','site:list:0')]);return True
            if key=='all': f.pop(field,None)
            elif field=='period' and key in PERIODS: f[field]=key
            elif field=='status' and key in STATUSES: f[field]=key
            elif field=='stage': f[field]=ledger.STAGES[int(key)] if 0<=int(key)<len(ledger.STAGES) else ''
            elif field=='author' and ledger.reader(db,uid,owner): f[field]=int(key)
            else: raise ValueError('Некорректный фильтр.')
            ledger.set_draft(db,uid,d);listing(api,db,chat,uid,owner)
        elif cmd=='edit':
            edit_prompt(api,db,chat,uid,owner,int(p[2]),p[3])
        elif cmd in ('stage','empty','date'):
            d=ledger.draft(db,uid) or {}
            if d.get('step')!='site_edit' or d.get('nonce')!=p[2]: raise ValueError('Открой поле заново.')
            if cmd=='stage' and d['field']=='stage' and 0<=int(p[3])<len(ledger.STAGES): text=ledger.STAGES[int(p[3])]
            elif cmd=='empty' and d['field'] in ('area','problems','next_work'): text=''
            elif cmd=='date' and d['field']=='work_date' and p[3] in ('today','yesterday'):
                text=(today()-timedelta(days=int(p[3]=='yesterday'))).isoformat()
            else: raise ValueError('Неверная кнопка.')
            change(db,uid,owner,d['ident'],d['version'],d['field'],text);after_field(api,db,chat,uid,owner,d)
        elif cmd=='upload':
            ident=int(p[2]);report(db,ident,uid,owner,True);state(db,uid,'site_upload',ident=ident)
            api.send(chat,'Пришли фотографии по одной или альбомом. До 10 фото в отчёте, каждое до 10 МБ; JPG / PNG. Затем нажми «Готово».',[('Готово',f'site:view:{ident}')])
        elif cmd=='photos':
            ident=int(p[2]);r=report(db,ident,uid,owner)
            photos=db.execute('SELECT id,filename FROM site_photos WHERE report_id=? ORDER BY id',(ident,)).fetchall()
            api.send(chat,f'Фото отчёта №{ident}: {len(photos)}. Выбери фотографию.',[(f'Фото {i+1}',f'site:photo:{r["id"]}:{x["id"]}') for i,x in enumerate(photos)]+[('К отчёту',f'site:view:{ident}')])
        elif cmd=='photo':
            ident=int(p[2]);r=report(db,ident,uid,owner)
            photo=db.execute('SELECT * FROM site_photos WHERE id=? AND report_id=?',(int(p[3]),ident)).fetchone()
            if not photo: raise ValueError('Фото больше нет.')
            api.document(chat,photo['filename'],photo['content'])
            opts=[('Все фото',f'site:photos:{ident}'),('К отчёту',f'site:view:{ident}')]
            if ledger.manager(db,uid,owner) or (r['user_id']==uid and r['status'] in ('draft','rework') and role(db,uid,owner)!='investor'): opts += [('Удалить фото',f'site:remove:{ident}:{photo["id"]}:{r["version"]}')]
            api.send(chat,f'Фото к отчёту №{ident} · {r["work_date"]} · {r["stage"]}',opts)
        elif cmd in ('submit','discard','accept','rework','remove'):
            ident=int(p[2]);r=report(db,ident,uid,owner);version=int(p[4] if cmd=='remove' else p[3])
            if version!=r['version']: raise ValueError('Отчёт изменился. Открой его заново.')
            if cmd=='remove': require_photo_removal(db,ident,uid,owner)
            elif cmd in ('submit','discard'): report(db,ident,uid,owner,True)
            elif not ledger.manager(db,uid,owner): raise ValueError('Недостаточно прав.')
            d=state(db,uid,'site_review' if cmd=='rework' else 'site_confirm',ident=ident,version=version,action=cmd,photo_id=int(p[3]) if cmd=='remove' else None)
            if cmd=='rework': api.send(chat,'Напиши, что прорабу нужно исправить (3–700 символов).',[('К отчёту',f'site:view:{ident}')])
            else: api.send(chat,{'submit':'Отправить отчёт на проверку? После отправки изменения доступны только при возврате на доработку.','discard':'Удалить черновик вместе с фотографиями?','accept':'Принять фотоотчёт?','remove':'Убрать эту фотографию?'}[cmd],[('Подтвердить',f'site:confirm:{d["nonce"]}'),('Отмена',f'site:view:{ident}')])
        elif cmd=='confirm':
            d=ledger.draft(db,uid) or {}
            if d.get('step')!='site_confirm' or d.get('nonce')!=p[2]: raise ValueError('Подтверждение устарело. Открой отчёт.')
            if d['action']=='remove':
                remove_photo(db,uid,owner,d['ident'],d['photo_id'],d['version'])
            else: transition(db,uid,owner,d['ident'],d['version'],d['action'],d.get('note',''))
            if d['action']=='discard': listing(api,db,chat,uid,owner)
            else: card(api,db,chat,uid,owner,d['ident'])
        else: raise ValueError('Открой раздел фотоотчётов заново.')
    except (ValueError,IndexError,KeyError) as exc:
        api.send(chat,str(exc) if isinstance(exc,ValueError) else 'Кнопка устарела. Открой раздел заново.',[('Фотоотчёты','site:home')])
    return True


def message(api,db,chat,uid,text,owner):
    d=ledger.draft(db,uid) or {};step=d.get('step','')
    if step not in ('site_edit','site_dates','site_review'): return False
    try:
        role(db,uid,owner)
        if step=='site_dates':
            parts=text.strip().split()
            if len(parts)!=2: raise ValueError('Введи две даты через пробел: ГГГГ-ММ-ДД ГГГГ-ММ-ДД.')
            start,end=(date.fromisoformat(ledger.valid_date(v)) for v in parts)
            if end<start: raise ValueError('Конец периода раньше начала.')
            d['site_filters'].update(period='custom',start=start.isoformat(),end=end.isoformat());ledger.set_draft(db,uid,d);listing(api,db,chat,uid,owner)
        elif step=='site_edit':
            change(db,uid,owner,d['ident'],d['version'],d['field'],text);after_field(api,db,chat,uid,owner,d)
        else:
            if not ledger.manager(db,uid,owner): raise ValueError('Недостаточно прав.')
            if not 3<=len(text.strip())<=700: raise ValueError('Замечание: от 3 до 700 символов.')
            d.update(step='site_confirm',note=text.strip());ledger.set_draft(db,uid,d)
            api.send(chat,'Вернуть отчёт с замечанием:\n'+d['note'],[('Подтвердить',f'site:confirm:{d["nonce"]}'),('К отчёту',f'site:view:{d["ident"]}')])
    except ValueError as exc: api.send(chat,str(exc),[('Фотоотчёты','site:home')])
    return True


def media(api,db,chat,uid,payload,update_id,owner):
    d=ledger.draft(db,uid) or {}
    if d.get('step')!='site_upload': return False
    try:
        if chat!=uid: raise ValueError('Открой личный чат.')
        report(db,d['ident'],uid,owner,True)
        if not payload.get('photo') and payload.get('document',{}).get('mime_type') not in ('image/jpeg','image/png'):
            raise ValueError('Для фотоотчёта нужны фотографии JPG / PNG, не чек PDF.')
        content,ext=attachments.download(api,payload)
        save_photo(db,uid,owner,d['ident'],update_id,content,ext)
        n=db.execute('SELECT count(*) FROM site_photos WHERE report_id=?',(d['ident'],)).fetchone()[0]
        api.send(chat,f'Сохранено фото: {n}/10. Можно прислать ещё или нажать «Готово».',[('Готово',f'site:view:{d["ident"]}')])
    except ValueError as exc: api.send(chat,str(exc),[('К отчёту',f'site:view:{d["ident"]}')])
    return True
