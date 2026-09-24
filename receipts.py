"""Private expense attachments persisted in the ledger database."""
import re
import urllib.request
from datetime import date
import bot as ledger

LIMIT=10*1024*1024
STATUS={'pending':'На проверке','approved':'Проверено','missing':'Без чека'}

def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS expense_reviews(operation_id INTEGER PRIMARY KEY REFERENCES operations(id) ON DELETE CASCADE, status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '', reviewer_id INTEGER, reviewed_at TEXT);
    CREATE TABLE IF NOT EXISTS receipts(id INTEGER PRIMARY KEY,operation_id INTEGER NOT NULL REFERENCES operations(id) ON DELETE CASCADE, update_id INTEGER NOT NULL UNIQUE, uploader_id INTEGER NOT NULL, file_id TEXT NOT NULL, filename TEXT NOT NULL, content BLOB NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    ''')

def allowed(db,uid,owner,row,write=False):
    role=ledger.role_for(db,uid,owner)
    return bool(row and role and (ledger.manager(db,uid,owner) or (not write and ledger.reader(db,uid,owner)) or (row['user_id']==uid and role!='investor')))

def row_for(db,ident):
    return db.execute("SELECT * FROM operations WHERE id=? AND kind='expense'",(ident,)).fetchone()

def metadata(db,ident):
    r=db.execute('SELECT * FROM expense_reviews WHERE operation_id=?',(ident,)).fetchone()
    count=db.execute('SELECT count(*) FROM receipts WHERE operation_id=?',(ident,)).fetchone()[0]
    return (r['status'] if r else 'pending'),(r['reason'] if r else ''),count

def card(api,db,chat,uid,owner,ident):
    row=row_for(db,ident)
    if not allowed(db,uid,owner,row): api.send(chat,'Операция недоступна.');return
    status,reason,count=metadata(db,ident)
    api.send(chat,ledger.operation_text(db,row)+f'\nПроверка: {STATUS.get(status,status)}\nЧеков: {count}'+ ('\nБез чека: '+reason if reason else ''),
        [('Посмотреть чеки',f'rc:files:{ident}')]+([('Добавить чеки',f'rc:add:{ident}'),('Нет чека — пояснить',f'rc:reason:{ident}')] if allowed(db,uid,owner,row,True) else [])+([('Проверено',f'rc:approve:{ident}'),('Вернуть на проверку',f'rc:pending:{ident}')] if ledger.manager(db,uid,owner) else [])+[('Меню','menu')])

def begin(api,db,chat,uid,ident):
    ledger.set_draft(db,uid,{'step':'receipt_upload','operation_id':ident})
    api.send(chat,f'Расход №{ident}: отправь фото чеков или PDF (до 10 МБ каждый). Можно несколько файлов.', [('Готово',f'rc:done:{ident}'),('Нет чека — пояснить',f'rc:reason:{ident}'),('Позже','cancel')])

def review_list(api,db,chat,uid,owner,value):
    if not ledger.manager(db,uid,owner): return
    if value=='rc:list':
        api.send(chat,'Проверка расходов:',[(label,'rc:list:'+s+':0') for s,label in STATUS.items()]+[('Меню','menu')]);return
    parts=value.split(':');status=parts[2]
    if status not in STATUS: return
    try: page=max(0,int(parts[3]))
    except (ValueError,IndexError): return
    filt=ledger.draft(db,uid) or {}
    month=filt.get('review_month','');person=filt.get('review_user',0)
    sql="SELECT o.*,u.name FROM operations o LEFT JOIN expense_reviews v ON v.operation_id=o.id LEFT JOIN users u ON u.id=o.user_id WHERE o.kind='expense'"
    args=[]
    if status=='missing': sql+=' AND NOT EXISTS(SELECT 1 FROM receipts r WHERE r.operation_id=o.id)'
    else: sql+=" AND COALESCE(v.status,'pending')=?";args.append(status)
    if month: sql+=' AND substr(o.occurred_on,1,7)=?';args.append(month)
    if person: sql+=' AND o.user_id=?';args.append(person)
    rows=db.execute(sql+' ORDER BY o.occurred_on DESC,o.id DESC LIMIT 9 OFFSET ?',args+[page*8]).fetchall()
    options=[(f"№{r['id']} · {r['name']} · {ledger.money(r['amount_kop'],r['currency'])}",f"rc:view:{r['id']}") for r in rows[:8]]
    if page: options.append(('Назад',f'rc:list:{status}:{page-1}'))
    if len(rows)>8: options.append(('Далее',f'rc:list:{status}:{page+1}'))
    options += [('Месяц','rc:filtermonth'),('Участник','rc:filteruser'),('Сбросить фильтры','rc:reset'),('Статусы','rc:list')]
    api.send(chat,f'{STATUS[status]} · {month or "Все даты"}'+ (' · выбран участник' if person else '')+ ('\nЗаписей нет.' if not rows else ''),options)

def callback(api,db,chat,uid,value,owner):
    if not value.startswith('rc:'): return False
    if chat!=uid or not ledger.role_for(db,uid,owner): return True
    if value=='rc:mine' or value.startswith('rc:mine:'):
        try: page=max(0,int(value.rsplit(':',1)[1])) if value.count(':')==2 else 0
        except ValueError: return True
        rows=db.execute("SELECT id,occurred_on,amount_kop,currency FROM operations WHERE user_id=? AND kind='expense' ORDER BY id DESC LIMIT 9 OFFSET ?",(uid,page*8)).fetchall()
        buttons=[(f"№{r['id']} · {r['occurred_on']} · {ledger.money(r['amount_kop'],r['currency'])}",f"rc:view:{r['id']}") for r in rows[:8]]
        if page: buttons.append(('Назад',f'rc:mine:{page-1}'))
        if len(rows)>8: buttons.append(('Далее',f'rc:mine:{page+1}'))
        api.send(chat,'Твои расходы и чеки:',buttons+[('Меню','menu')]);return True
    if value.startswith('rc:list'): review_list(api,db,chat,uid,owner,value);return True
    if value in ('rc:filtermonth','rc:filteruser','rc:reset') or value.startswith('rc:user:'):
        if not ledger.manager(db,uid,owner): return True
        d=ledger.draft(db,uid) or {}
        if value=='rc:reset': ledger.clear_draft(db,uid)
        elif value=='rc:filtermonth':
            d['step']='receipt_month';ledger.set_draft(db,uid,d);api.send(chat,'Введи месяц ГГГГ-ММ:');return True
        elif value=='rc:filteruser':
            api.send(chat,'Выбери участника:',[(r['name'],f"rc:user:{r['id']}") for r in db.execute('SELECT id,name FROM users WHERE deleted=0')]);return True
        else:
            raw=value.rsplit(':',1)[1]
            if not raw.isdigit(): return True
            d['review_user']=int(raw);ledger.set_draft(db,uid,d)
        review_list(api,db,chat,uid,owner,'rc:list');return True
    try: _,action,raw=value.split(':');ident=int(raw)
    except ValueError: return True
    row=row_for(db,ident)
    if not allowed(db,uid,owner,row): api.send(chat,'Операция недоступна.');return True
    if action=='files':
        files=db.execute('SELECT filename,content FROM receipts WHERE operation_id=? ORDER BY id',(ident,)).fetchall()
        if not files: api.send(chat,'Чеков пока нет.')
        for r in files: api.document(chat,r['filename'],r['content'])
    elif action=='add' and allowed(db,uid,owner,row,True): begin(api,db,chat,uid,ident);return True
    elif action=='reason' and allowed(db,uid,owner,row,True):
        ledger.set_draft(db,uid,{'step':'receipt_reason','operation_id':ident});api.send(chat,'Почему нет чека? Напиши пояснение:');return True
    elif action=='done':
        if metadata(db,ident)[2]==0:
            api.send(chat,'Прикрепи чек или укажи причину его отсутствия.', [('Нет чека — пояснить',f'rc:reason:{ident}')]);return True
        ledger.clear_draft(db,uid)
    elif action in ('approve','pending') and ledger.manager(db,uid,owner):
        with db:
            db.execute("INSERT INTO expense_reviews(operation_id,status,reviewer_id,reviewed_at) VALUES (?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(operation_id) DO UPDATE SET status=excluded.status,reviewer_id=excluded.reviewer_id,reviewed_at=excluded.reviewed_at",(ident,'approved' if action=='approve' else 'pending',uid))
    card(api,db,chat,uid,owner,ident);return True

def message(api,db,chat,uid,text,owner):
    if text.startswith('/start receipt_'):
        raw=text.split('receipt_',1)[1]
        if raw.isdigit(): card(api,db,chat,uid,owner,int(raw))
        return True
    d=ledger.draft(db,uid) or {}
    if text.startswith('/'): return False
    if d.get('step')=='receipt_month' and ledger.manager(db,uid,owner):
        try:
            if not re.fullmatch(r'\d{4}-\d{2}',text): raise ValueError()
            date.fromisoformat(text+'-01')
        except ValueError: api.send(chat,'Нужен месяц в формате ГГГГ-ММ.');return True
        d['review_month']=text;d['step']='receipt_filter';ledger.set_draft(db,uid,d);review_list(api,db,chat,uid,owner,'rc:list');return True
    if d.get('step')!='receipt_reason': return False
    ident=d['operation_id']
    if not allowed(db,uid,owner,row_for(db,ident),True): return True
    if len(text.strip())<3: api.send(chat,'Напиши пояснение подробнее.');return True
    with db:
        db.execute("INSERT INTO expense_reviews(operation_id,status,reason) VALUES (?,'pending',?) ON CONFLICT(operation_id) DO UPDATE SET reason=excluded.reason,status='pending',reviewer_id=NULL,reviewed_at=NULL",(ident,text[:1000]))
    ledger.clear_draft(db,uid);card(api,db,chat,uid,owner,ident);return True

def media(api,db,chat,uid,payload,update_id,owner):
    d=ledger.draft(db,uid) or {};ident=d.get('operation_id')
    if chat!=uid or d.get('step')!='receipt_upload' or not allowed(db,uid,owner,row_for(db,ident),True):
        api.send(chat,'Сначала открой расход и нажми «Добавить чеки».');return
    if db.execute('SELECT 1 FROM receipts WHERE update_id=?',(update_id,)).fetchone(): return
    if payload.get('photo'): f=payload['photo'][-1];ext='.jpg'
    else:
        f=payload.get('document',{});ext={'application/pdf':'.pdf','image/jpeg':'.jpg','image/png':'.png'}.get(f.get('mime_type'))
    if not ext or f.get('file_size',LIMIT+1)>LIMIT:
        api.send(chat,'Пришли фото, JPG, PNG или PDF до 10 МБ.');return
    info=api.call('getFile',{'file_id':f['file_id']})
    path=info['file_path']
    # File path is issued by Telegram; never accept arbitrary attachment URLs.
    if not re.fullmatch(r'[A-Za-z0-9_./-]+',path) or '..' in path: raise ValueError('Invalid file path')
    url=api.url.replace('/bot','/file/bot',1)+path
    with urllib.request.urlopen(url,timeout=60) as response: content=response.read(LIMIT+1)
    valid=(ext=='.pdf' and content.startswith(b'%PDF-')) or (ext=='.jpg' and content.startswith(b'\xff\xd8')) or (ext=='.png' and content.startswith(b'\x89PNG\r\n\x1a\n'))
    if len(content)>LIMIT or not valid: api.send(chat,'Файл не подходит. Пришли фото или PDF до 10 МБ.');return
    with db:
        db.execute('INSERT INTO receipts(operation_id,update_id,uploader_id,file_id,filename,content) VALUES (?,?,?,?,?,?)',(ident,update_id,uid,f['file_id'],f'check_{ident}_{update_id}{ext}',content))
        db.execute("INSERT INTO expense_reviews(operation_id) VALUES (?) ON CONFLICT(operation_id) DO UPDATE SET status='pending',reason='',reviewer_id=NULL,reviewed_at=NULL",(ident,))
    api.send(chat,'Чек сохранён. Можно прислать следующий.', [('Готово',f'rc:done:{ident}')])

def enrich(api,db,rows):
    username=getattr(api,'receipt_username',None)
    if username is None:
        try: username=api.call('getMe',{}).get('username','')
        except Exception: username=''
        if username: api.receipt_username=username
    result=[]
    for row in rows:
        r=dict(row)
        if r['kind']=='expense':
            status,reason,count=metadata(db,r['id'])
            r['receipt_label']=f'Чеки: {count} · {STATUS[status]}' if count else 'Без чека · '+STATUS[status]
            if reason: r['receipt_label']+=' · '+reason
            if username and re.fullmatch(r'[A-Za-z0-9_]+',username): r['receipt_url']=f'https://t.me/{username}?start=receipt_{r["id"]}'
        result.append(r)
    return result
