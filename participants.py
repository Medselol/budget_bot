"""Participant cards and username lookup after a private interaction with the bot."""
import re
import bot as ledger


def remember(db, user):
    uid = user.get('id')
    if not isinstance(uid,int) or user.get('is_bot'): return
    username = (user.get('username') or '').lower() or None
    name = ' '.join(filter(None,[user.get('first_name'),user.get('last_name')]))[:80] or f'Участник {uid}'
    with db:
        if username:
            db.execute('UPDATE telegram_contacts SET username=NULL WHERE username=? AND id!=?',(username,uid))
        db.execute('INSERT INTO telegram_contacts(id,username,name) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,name=excluded.name',(uid,username,name))


def lookup(api,db,text):
    text=text.strip()
    if re.fullmatch(r'@[A-Za-z0-9_]{1,32}',text):
        nick=text[1:].lower()
        row=db.execute('SELECT * FROM telegram_contacts WHERE username=?',(nick,)).fetchone()
        if not row: raise ValueError('Ник пока неизвестен боту. Попроси человека нажать /start в этом боте, затем введи @ник ещё раз.')
        try: current=api.call('getChat',{'chat_id':row['id']})
        except Exception:
            raise ValueError('Не удалось проверить ник. Пусть участник нажмёт /start, затем попробуй снова.') from None
        if current.get('id')!=row['id'] or current.get('type')!='private' or (current.get('username') or '').lower()!=nick:
            raise ValueError('Ник изменился или не подтверждён. Попроси участника нажать /start и введи его текущий @ник.')
        return row['id'],row['name'],nick
    if text.isdigit() and 0<int(text)<2**63:
        row=db.execute('SELECT id,name FROM telegram_contacts WHERE id=? UNION SELECT id,name FROM users WHERE id=? LIMIT 1',(int(text),int(text))).fetchone()
        if row: return row['id'],row['name'],None
    raise ValueError('Введи @ник Telegram. Если ника нет — ID участника, который уже нажал /start.')


def listing(api,db,chat,page=0,archived=False):
    rows=db.execute('SELECT * FROM users WHERE deleted=0 AND active=? ORDER BY name,id',(0 if archived else 1,)).fetchall()
    route = 'archive' if archived else 'page'
    page=max(0,min(page,max(0,(len(rows)-1)//12)))
    options=[('Добавить по @нику','participant:add'),('Найти участника','participant:find')]
    for r in rows[page*12:(page+1)*12]:
        label=f"{r['name']} · {ledger.ROLE_NAMES.get(r['role'],r['role'])}"+(' · доступ закрыт' if not r['active'] else '')
        options.append((label,f"participant:view:{r['id']}"))
    if page:options.append(('← Назад',f'participant:{route}:{page-1}'))
    if (page+1)*12<len(rows):options.append(('Далее →',f'participant:{route}:{page+1}'))
    options.append(('Действующие участники','users:list') if archived else ('Архив участников','participant:archive:0'))
    options.append(('Меню','menu'))
    api.send(chat,'Архив. Доступ закрыт; операции и остатки сохранены.' if archived else 'Действующие участники. Выбери человека для изменения имени, прав или доступа:',options)


def card(api,db,chat,target,uid,owner):
    r=db.execute('SELECT u.*,c.username FROM users u LEFT JOIN telegram_contacts c ON c.id=u.id WHERE u.id=? AND u.deleted=0',(target,)).fetchone()
    if not r:
        api.send(chat,'Участник не найден.');return
    text=f"{r['name']}\n"+('@'+r['username']+'\n' if r['username'] else '')+f"ID: {target}\nРоль: {ledger.ROLE_NAMES.get(r['role'],r['role'])}\nДоступ: "+('открыт' if r['active'] else 'закрыт')
    options=[]
    if target!=owner or uid==owner:options.append(('Переименовать',f'participant:rename:{target}'))
    if target!=owner:
        options += [('Изменить права',f'role:pick:{target}'),('Удалить участника' if r['active'] else 'Восстановить доступ',f"participant:{'remove' if r['active'] else 'restore'}:{target}")]
    if target!=owner and not r['active']:
        options.append(('Удалить из архива навсегда',f'participant:purge:{target}'))
    options.append(('К участникам','users:list'))
    api.send(chat,text,options)


def purge(db, target, owner):
    if target==owner: raise ValueError('Владельца удалить нельзя.')
    with db:
        row=db.execute('SELECT active,deleted FROM users WHERE id=?',(target,)).fetchone()
        if not row or row['deleted']: raise ValueError('Участник уже удалён.')
        if row['active']: raise ValueError('Сначала перенеси участника в архив.')
        linked=db.execute("""SELECT 1 FROM operations WHERE user_id=?
            OR account_id IN (SELECT id FROM accounts WHERE owner_id=?)
            OR from_account_id IN (SELECT id FROM accounts WHERE owner_id=?)
            OR to_account_id IN (SELECT id FROM accounts WHERE owner_id=?)
            OR project_id IN (SELECT id FROM projects WHERE owner_id=?) LIMIT 1""",(target,)*5).fetchone()
        db.execute('DELETE FROM drafts WHERE user_id=?',(target,))
        if linked:
            db.execute('UPDATE users SET deleted=1,active=0 WHERE id=?',(target,))
        else:
            db.execute('DELETE FROM accounts WHERE owner_id=?',(target,))
            db.execute('DELETE FROM projects WHERE owner_id=?',(target,))
            db.execute('DELETE FROM users WHERE id=?',(target,))
        db.execute('DELETE FROM telegram_contacts WHERE id=?',(target,))
        ledger.bump_revision(db)
    return bool(linked)


def callback(api,db,chat,uid,value,owner):
    if value!='users:list' and not value.startswith('participant:'):return False
    if not ledger.manager(db,uid,owner):return True
    p=value.split(':')
    if value=='users:list':
        ledger.clear_draft(db,uid);listing(api,db,chat);return True
    if value in ('participant:add','participant:find'):
        ledger.set_draft(db,uid,{'step':'participant_lookup','mode':p[1]})
        api.send(chat,'Введи @ник Telegram. Человек должен сначала нажать /start в этом боте. Если ника нет — можно ввести его ID.',[('Отмена','cancel')]);return True
    try:
        if len(p)!=3:return True
        if p[1] in ('page','archive'):listing(api,db,chat,int(p[2]),archived=p[1]=='archive');return True
        # New participant gets no access until a role is explicitly chosen.
        if p[1]=='admit':
            d=ledger.draft(db,uid)
            role=p[2]
            if not d or d.get('step')!='participant_admit' or role not in ('foreman','investor','editor','member'):return True
            target=d['target']
            if d.get('nick'):
                verified,_,_=lookup(api,db,'@'+d['nick'])
                if verified!=target:raise ValueError('Ник сменил владельца. Начни добавление заново.')
            if target==owner:raise ValueError('Владелец уже добавлен.')
            if db.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone():
                ledger.clear_draft(db,uid);card(api,db,chat,target,uid,owner);return True
            with db:
                db.execute('INSERT INTO users(id,name,role,active) VALUES (?,?,?,1)',(target,d['name'],role))
                for account in ('Наличные','Карта','Счёт'):
                    db.execute('INSERT OR IGNORE INTO accounts(owner_id,name) VALUES (?,?)',(target,account))
                ledger.bump_revision(db)
            ledger.default_project_id(db,target)
            ledger.clear_draft(db,uid);api.send(chat,'Участник добавлен. Пусть отправит /start для обновления меню.')
            card(api,db,chat,target,uid,owner);return True
        target=int(p[2]);action=p[1]
        if target<=0 or target>=2**63:raise ValueError('Неверный ID.')
        if action=='view':card(api,db,chat,target,uid,owner);return True
        if not db.execute('SELECT 1 FROM users WHERE id=? AND deleted=0',(target,)).fetchone():raise ValueError('Участник не найден.')
        if target==owner and (action!='rename' or uid!=owner):raise ValueError('Доступ владельца изменить нельзя.')
        if action in ('purge','purgeconfirm'):
            row=db.execute('SELECT name,active FROM users WHERE id=?',(target,)).fetchone()
            if row['active']:raise ValueError('Сначала перенеси участника в архив.')
            if action=='purge':
                ledger.set_draft(db,uid,{'step':'participant_purge','target':target})
                api.send(chat,f"Удалить {row['name']} из архива навсегда? Восстановить кнопкой будет нельзя. Пустой аккаунт будет удалён полностью. Если есть операции, они сохранятся в общем учёте, но участник исчезнет из списков.", [('Да, удалить навсегда',f'participant:purgeconfirm:{target}'),('Отмена','users:list')]);return True
            d=ledger.draft(db,uid)
            if not d or d.get('step')!='participant_purge' or d.get('target')!=target:
                raise ValueError('Открой карточку и подтверди удаление заново.')
            kept=purge(db,target,owner)
            ledger.clear_draft(db,uid)
            api.send(chat,'Участник удалён из архива.'+(' Финансовая история сохранена в общем учёте.' if kept else ' Пустой аккаунт удалён полностью.'))
            listing(api,db,chat,archived=True);return True
        if action=='rename':
            ledger.set_draft(db,uid,{'step':'participant_rename','target':target})
            api.send(chat,'Введи новое имя для учёта (до 80 символов). Ник в Telegram не изменится.',[('Отмена','cancel')]);return True
        if action in ('remove','restore'):
            name=db.execute('SELECT name FROM users WHERE id=?',(target,)).fetchone()[0]
            api.send(chat,(f'Удалить {name} из действующих участников? Доступ будет закрыт, участник переместится в архив. Все операции и остатки сохранятся.' if action=='remove' else f'Восстановить доступ для {name}?'),[('Подтвердить',f'participant:{action}confirm:{target}'),('Отмена',f'participant:view:{target}')]);return True
        if action in ('removeconfirm','restoreconfirm'):
            with db:
                db.execute('UPDATE users SET active=? WHERE id=?',(int(action=='restoreconfirm'),target))
                db.execute('DELETE FROM drafts WHERE user_id=?',(target,))
            api.send(chat,'Участник удалён из действующего списка и перенесён в архив. Доступ закрыт.' if action=='removeconfirm' else 'Доступ восстановлен. Участник может нажать /start.')
            if ledger.manager(db,uid,owner):listing(api,db,chat)
            return True
    except (ValueError,OverflowError) as exc:api.send(chat,str(exc))
    return True


def message(api,db,chat,uid,text,owner):
    shortcut=text.startswith('/adduser @')
    d=ledger.draft(db,uid)
    if not shortcut and (text.startswith('/') or not d or not d.get('step','').startswith('participant_')):return False
    if not ledger.manager(db,uid,owner):return True
    if shortcut:
        text=text.split(maxsplit=1)[1]
        d={'step':'participant_lookup','mode':'add'}
    try:
        if d['step']=='participant_lookup':
            target,name,nick=lookup(api,db,text)
            exists=db.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone()
            if exists:
                ledger.clear_draft(db,uid);card(api,db,chat,target,uid,owner);return True
            if d['mode']=='find':raise ValueError('Этот человек ещё не добавлен. Нажми «Добавить по @нику».')
            ledger.set_draft(db,uid,{'step':'participant_admit','target':target,'name':name,'nick':nick})
            api.send(chat,f"Найден: {name}\n"+('@'+nick+'\n' if nick else '')+f'ID: {target}\nПроверь человека и выбери права для добавления:',[(ledger.ROLE_NAMES[r],'participant:admit:'+r) for r in ('foreman','investor','editor','member')]+[('Отмена','cancel')]);return True
        if d['step']=='participant_rename':
            name=text.strip();target=d['target']
            if not name or len(name)>80 or '\n' in name:raise ValueError('Имя должно быть одной строкой от 1 до 80 символов.')
            if target==owner and uid!=owner:raise ValueError('Имя владельца может менять только он сам.')
            with db:
                db.execute('UPDATE users SET name=? WHERE id=?',(name,target));ledger.bump_revision(db)
            ledger.clear_draft(db,uid);card(api,db,chat,target,uid,owner);return True
        api.send(chat,'Выбери роль кнопкой или нажми «Отмена».')
    except ValueError as exc:api.send(chat,str(exc),[('Отмена','cancel')])
    return True
