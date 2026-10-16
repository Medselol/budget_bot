"""Role checks and confirmed transfers between participants' cash accounts."""
import bot as ledger


def permitted(api, db, chat, uid, owner, value, message):
    role = ledger.role_for(db, uid, owner)
    if role is None or chat != uid:
        return False
    common = value in ('menu', 'cancel', '/start', '/menu', '/help', '/cancel')
    if role == 'investor':
        photo_dates = message and (ledger.draft(db, uid) or {}).get('step') == 'site_dates' and not value.startswith('/')
        allowed = photo_dates or common or value in ('all:menu', 'all:month', 'all:all') or value.startswith(('csvall:', '/allreport ', 'overview:', 'people:', 'csvuser:', 'rc:', 'ctl:', 'site:', 'ui:', '/start receipt_')) or value in ('team:balances','team:archive')
        if not allowed:
            api.send(chat, 'У тебя доступ наблюдателя: можно смотреть общие PDF-отчёты, менять записи нельзя.', [('Общий отчёт PDF', 'all:menu')])
        return allowed
    if role == 'foreman' and (value.startswith(('op:', 'ops:', 'report:', 'rp:', 'rproj:', 'csv:', '/report ', '/operations'))):
        api.send(chat, 'В меню прораба доступны приход, расход и свой баланс.', [('Приход','new:income'),('Расход','new:expense')]); return False
    d = ledger.draft(db, uid)
    if d and not common:
        if d.get('ledger_user_id', uid) != uid and not ledger.manager(db, uid, owner):
            ledger.clear_draft(db, uid)
            api.send(chat, 'Права изменились. Открой /start.'); return False
        if role == 'foreman' and d.get('kind') not in (None, 'expense', 'income'):
            ledger.clear_draft(db, uid)
    if role == 'foreman' and (value.startswith(('new:transfer','/project ','/account '))):
        api.send(chat, 'Прораб записывает только приход и расход.'); return False
    if value.startswith(('fund:', 'role:', 'participant:')) and not ledger.manager(db, uid, owner):
        api.send(chat, 'Недостаточно прав.'); return False
    # Existing forms must not allow a demoted user to save income or transfers.
    if role == 'foreman' and value.startswith(('op:edit:', 'op:delete:', 'op:delconfirm:')):
        try:
            r = db.execute('SELECT kind FROM operations WHERE id=?', (int(value.rsplit(':',1)[1]),)).fetchone()
        except ValueError: return False
        if r and r['kind'] not in ('expense','income'):
            api.send(chat, 'Прораб может менять только свои приходы и расходы.'); return False
    return True


def role_picker(api, chat, target, owner):
    if target == owner:
        api.send(chat, 'Это владелец. Его права остаются неизменными.'); return
    api.send(chat, 'Выбери права участника:', [(label, f'role:set:{target}:{role}') for role,label in ledger.ROLE_NAMES.items() if role != 'owner'] + [('К участникам','users:list')])


def show_users(api, db, chat, owner):
    users = db.execute('SELECT * FROM users ORDER BY id').fetchall()
    text = 'Пользователи:\n' + '\n'.join(f"{r['name']} — {ledger.ROLE_NAMES.get(r['role'], r['role'])} · ID {r['id']}" + (' (отключён)' if not r['active'] else '') for r in users)
    text += '\n\nДобавить: /adduser ID Имя\nПосле добавления выбери роль кнопкой.\nПереименовать: /renameuser ID Имя\nОтключить: /removeuser ID'
    api.send(chat, text[:4000], [(r['name']+' · права', f"role:pick:{r['id']}") for r in users if r['id'] != owner] + [('Меню','menu')])


def accounts(db):
    return db.execute("SELECT a.id,a.owner_id,a.name,u.name AS user_name FROM accounts a JOIN users u ON u.id=a.owner_id WHERE u.active=1 AND u.deleted=0 AND u.role!='investor' ORDER BY u.name,a.id").fetchall()


def fund_prompt(api, db, chat, d):
    step = d['step']
    if step in ('fund_from','fund_to','fund_from_account','fund_to_account'):
        side = 'from' if step.startswith('fund_from') else 'to'
        rows = [r for r in accounts(db) if side == 'from' or r['id'] != d.get('from_account_id')]
        if step.endswith('_account'):
            rows = [r for r in rows if r['owner_id'] == d.get('fund_person_id')]
            icons = {'Наличные': '💵', 'Карта': '💳', 'Счёт': '🏦'}
            options = [(icons.get(r['name'], '👛') + ' ' + r['name'],
                        f"fund:box:{side}:{r['owner_id']}:{r['id']}") for r in rows]
            label = ('Из какой кассы выдать?' if side == 'from' else 'В какую кассу передать?')
            label += '\nУчастник: ' + (rows[0]['user_name'] if rows else 'недоступен')
        else:
            people = {r['owner_id']: r['user_name'] for r in rows}
            options = [('👤 ' + name, f'fund:person:{side}:{person}') for person, name in people.items()]
            label = 'Кто выдаёт деньги?' if side == 'from' else 'Кому передать деньги?'
        api.send(chat, label, options+[('Отмена','cancel')])
    elif step == 'fund_currency':
        api.send(chat, 'Валюта выдачи:', [(c,'fund:currency:'+c) for c in ledger.CURRENCIES]+[('Отмена','cancel')])
    elif step == 'fund_amount':
        api.send(chat, 'Введи сумму выдачи. Это запись о передаче денег, не банковский перевод.', [('Отмена','cancel')])
    elif step == 'fund_confirm':
        by_id = {r['id']:r for r in accounts(db)}
        a,b = by_id.get(d['from_account_id']),by_id.get(d['to_account_id'])
        if not a or not b:
            api.send(chat,'Участник отключён. Начни выдачу заново.'); return
        api.send(chat, f"Передать {ledger.money(d['amount_kop'],d['currency'])}\nОт: {a['user_name']} · {a['name']}\nКому: {b['user_name']} · {b['name']}\nПеревод не увеличивает доходы и расходы стройки.", [('Подтвердить выдачу','fund:save'),('Отмена','cancel')])


def save_funding(db, uid, owner, d, update_id):
    if not ledger.manager(db, uid, owner): raise ValueError('Недостаточно прав.')
    old = db.execute('SELECT id FROM operations WHERE update_id=?', (update_id,)).fetchone()
    if old: return old[0]
    accts = {r['id']:r for r in accounts(db)}
    a,b = accts.get(d.get('from_account_id')),accts.get(d.get('to_account_id'))
    amount, currency = d.get('amount_kop'),d.get('currency')
    if not a or not b or a['id']==b['id'] or currency not in ledger.CURRENCIES or not isinstance(amount,int) or amount<=0:
        raise ValueError('Проверь получателя, сумму и валюту.')
    if ledger.balances(db,a['owner_id'])[a['id']][1][currency] < amount:
        raise ValueError('В кассе отправителя недостаточно денег. Сначала запиши фактический приход в эту кассу.')
    comment=f"Выдача: {a['user_name']} → {b['user_name']}. Оформил: " + db.execute('SELECT name FROM users WHERE id=?',(uid,)).fetchone()[0]
    with db:
        cur=db.execute("INSERT INTO operations(update_id,user_id,occurred_on,kind,from_account_id,to_account_id,amount_kop,currency,comment) VALUES (?,?,?,'transfer',?,?,?,?,?)", (update_id,uid,ledger.datetime.now(ledger.TZ).date().isoformat(),a['id'],b['id'],amount,currency,comment))
        ledger.bump_revision(db)
    return cur.lastrowid


def callback(api, db, chat, uid, update_id, value, owner):
    if value.startswith('role:'):
        if not ledger.manager(db, uid, owner): return True
        parts=value.split(':')
        try: target=int(parts[2])
        except (ValueError,IndexError): return True
        if target==owner:
            api.send(chat,'Права владельца изменить нельзя.'); return True
        if len(parts)==3 and parts[1]=='pick':
            role_picker(api,chat,target,owner); return True
        if len(parts)==4 and parts[1]=='set' and parts[3] in ledger.ROLE_NAMES and parts[3]!='owner':
            with db:
                cur=db.execute('UPDATE users SET role=? WHERE id=?',(parts[3],target))
                db.execute('DELETE FROM drafts WHERE user_id=?',(target,))
            api.send(chat, 'Права обновлены: '+ledger.ROLE_NAMES[parts[3]] if cur.rowcount else 'Пользователь не найден.', [('К участникам','users:list')])
        return True
    if value in ('team:balances','team:archive'):
        if not ledger.reader(db,uid,owner): return True
        archived = value=='team:archive'
        lines=['Балансы архивных участников:' if archived else 'Балансы действующих участников (с начала учёта):']
        for u in db.execute('SELECT id,name FROM users WHERE deleted=0 AND active=? ORDER BY name',(0 if archived else 1,)):
            lines.append('\n'+u['name'])
            for name,sums in ledger.balances(db,u['id']).values():
                lines.append(name+': '+ '; '.join(ledger.money(sums[c],c) for c in ledger.CURRENCIES))
        text='\n'.join(lines)
        for i in range(0,len(text),3900): api.send(chat,text[i:i+3900])
        api.send(chat,'Выбери список:', [('Действующие участники','team:balances') if archived else ('Архив участников','team:archive'),('Меню','menu')])
        return True
    if not value.startswith('fund:'): return False
    if not ledger.manager(db,uid,owner): return True
    d=ledger.draft(db,uid)
    if value=='fund:menu':
        d={'step':'fund_from'}
    elif not d or not d.get('step','').startswith('fund_'):
        api.send(chat,'Начни выдачу через меню.'); return True
    elif value.startswith('fund:account:'):
        # Old Telegram keyboards must not choose the wrong side after deployment.
        fund_prompt(api, db, chat, d); return True
    elif value.startswith('fund:person:') and d['step'] in ('fund_from','fund_to'):
        parts = value.split(':')
        side = 'from' if d['step'] == 'fund_from' else 'to'
        if len(parts) != 4 or parts[2] != side: return True
        try: person = int(parts[3])
        except ValueError: return True
        rows = [a for a in accounts(db) if side == 'from' or a['id'] != d.get('from_account_id')]
        if person not in {a['owner_id'] for a in rows}: return True
        d['fund_person_id'] = person
        d['step'] = 'fund_' + side + '_account'
    elif value.startswith('fund:box:') and d['step'] in ('fund_from_account','fund_to_account'):
        parts = value.split(':')
        side = 'from' if d['step'] == 'fund_from_account' else 'to'
        if len(parts) != 5 or parts[2] != side: return True
        try: person, aid = int(parts[3]), int(parts[4])
        except ValueError: return True
        if person != d.get('fund_person_id'): return True
        if aid not in {a['id'] for a in accounts(db) if a['owner_id'] == person}: return True
        if side == 'from':
            d['from_account_id'] = aid; d['step'] = 'fund_to'
        else:
            if aid == d['from_account_id']: return True
            d['to_account_id'] = aid; d['step'] = 'fund_currency'
        d.pop('fund_person_id', None)
    elif value.startswith('fund:currency:') and d['step']=='fund_currency':
        c=value.rsplit(':',1)[1]
        if c not in ledger.CURRENCIES: return True
        d['currency']=c;d['step']='fund_amount'
    elif value=='fund:save' and d['step']=='fund_confirm':
        try: ident=save_funding(db,uid,owner,d,update_id)
        except ValueError as exc:
            api.send(chat,str(exc),[('Начать заново','fund:menu'),('Меню','menu')]); return True
        ledger.clear_draft(db,uid)
        api.send(chat,f'Выдача сохранена. Операция №{ident}. Балансы обновлены.')
        ledger.menu(api,chat,role=ledger.role_for(db,uid,owner)); return True
    else: return True
    ledger.set_draft(db,uid,d);fund_prompt(api,db,chat,d);return True


def message(api,db,chat,uid,text,owner):
    d=ledger.draft(db,uid)
    if text.startswith('/') or not d or not d.get('step','').startswith('fund_'): return False
    if not ledger.manager(db,uid,owner):
        ledger.clear_draft(db,uid); return True
    if d['step']=='fund_amount':
        try: d['amount_kop']=ledger.parse_amount(text)
        except ValueError as exc:
            api.send(chat,str(exc));return True
        d['step']='fund_confirm';ledger.set_draft(db,uid,d)
    fund_prompt(api,db,chat,d);return True
