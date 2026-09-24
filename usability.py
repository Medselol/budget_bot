"""Compact role-aware menus; all destinations retain their own access checks."""
import bot as ledger


def home_options(role):
    if role in ('foreman', 'member'):
        options = [('Расход', 'new:expense'), ('Приход', 'new:income'),
                   ('Мой баланс', 'balance'), ('Мои чеки', 'rc:mine'),
                   ('Фотоотчёты стройки', 'site:home'), ('Заявки на деньги', 'ctl:req:list:open:0')]
        if role == 'member': options.append(('Финансы и отчёты', 'ui:finance'))
    elif role == 'investor':
        options = [('Общий отчёт PDF', 'all:menu'), ('Балансы участников', 'team:balances'),
                   ('Участники и счета', 'people:menu'), ('Фотоотчёты стройки', 'site:home'),
                   ('Планирование и контроль', 'ctl:home'), ('Отчёты', 'ui:reports')]
    else:
        options = [('Расход', 'new:expense'), ('Приход', 'new:income'),
                   ('Выдать деньги', 'fund:menu'), ('Финансы', 'ui:finance'),
                   ('Отчёты', 'ui:reports'), ('Команда', 'ui:team'),
                   ('Контроль стройки', 'ctl:home'), ('Фотоотчёты стройки', 'site:home'),
                   ('Проверка расходов', 'rc:list')]
    return options + [('Как пользоваться', 'ui:help')]


def callback(api, db, chat, uid, value, owner):
    if not value.startswith('ui:') and value != '/help': return False
    role = ledger.role_for(db, uid, owner)
    if not role or chat != uid: return True
    section = value.split(':')[-1] if value != '/help' else 'help'
    allowed = {'help'}
    if role in ('owner', 'editor', 'member'): allowed.add('finance')
    if ledger.reader(db, uid, owner): allowed.add('reports')
    if ledger.manager(db, uid, owner): allowed.add('team')
    if section not in allowed:
        api.send(chat, 'Этот раздел недоступен для твоей роли.', [('Главное меню', 'menu')]); return True
    if section == 'help':
        text = 'Как пользоваться · ' + ledger.ROLE_NAMES[role] + '\n\n'
        if role != 'investor':
            text += ('Расход → этап → тип затрат → счёт → валюта → сумма → дата → комментарий → подтверждение. '
                     'После сохранения прикрепи чек или укажи причину его отсутствия. Позже найти запись можно в «Мои чеки».\n\n'
                     'Фотоотчёты стройки → Новый фотоотчёт: этап, дата, выполненные работы и фотографии. '
                     'Отправь готовый отчёт на проверку.\n\n'
                     'Заявки на деньги: укажи сумму, назначение и срок. Одобрение заявки ещё не означает выдачу денег.\n\n')
        if ledger.reader(db, uid, owner):
            text += ('Общий отчёт PDF: выбери период и всех участников или одного человека. '
                     '«Участники и счета» показывает операции, счета и переводы. '
                     'План / факт и долги находятся в разделе контроля.\n\n')
        if ledger.manager(db, uid, owner):
            text += 'Команда → Пользователи: добавление по @нику, права и архив. Финансы → Операции: просмотр и исправления.\n\n'
        text += ('«Назад» возвращает предыдущий экран. «Главное меню» завершает текущий ввод операции; '
                 'сохранённые записи остаются. Черновики фотоотчётов и заявок доступны в своих разделах. '
                 'Суммы в гривнах и долларах учитываются отдельно.')
        api.send(chat, text)
        return True
    ledger.clear_draft(db, uid)
    if section == 'finance':
        options = [('Операции', 'ops:list'), ('Мой баланс', 'balance'), ('Мои чеки', 'rc:mine'),
                   ('Между своими счетами', 'new:transfer'), ('Мой отчёт PDF', 'report:menu')]
        if ledger.manager(db, uid, owner): options += [('Обмен USD ↔ UAH', 'fx:new'), ('Выдать деньги', 'fund:menu'), ('Балансы участников', 'team:balances')]
        text = 'Финансы. Записи, остатки и переводы. Выдача денег участнику учитывается как перевод.'
    elif section == 'reports':
        options = [('Общий отчёт PDF', 'all:menu'), ('Участники и счета', 'people:menu'),
                   ('План / факт', 'ctl:budget:UAH:0'), ('Недельная сводка', 'ctl:weekly')]
        if role != 'investor': options.append(('Мой отчёт PDF', 'report:menu'))
        text = 'Отчёты. Общий отчёт можно сформировать за день, неделю, месяц или год и по выбранному участнику.'
    else:
        options = [('Пользователи', 'users:list'), ('Участники и счета', 'people:menu'),
                   ('Балансы участников', 'team:balances'), ('Проверка расходов', 'rc:list'),
                   ('Заявки на деньги', 'ctl:req:list:open:0')]
        text = 'Команда. Управление доступом, проверка чеков, выдачи и остатки участников.'
    api.send(chat, text, options)
    return True


class FormPrompt:
    def __init__(self, api, d): self.api, self.d = api, d
    def send(self, chat, text, options=None):
        d = self.d
        steps = {'expense': ['stage', 'cost_type', 'account', 'currency', 'amount', 'date', 'comment', 'confirm'],
                 'income': ['source', 'account', 'currency', 'amount', 'date', 'comment', 'confirm'],
                 'transfer': ['from_account', 'to_account', 'currency', 'amount', 'date', 'comment', 'confirm']}.get(d.get('kind'), [])
        step = 'date' if d['step'] == 'date_text' else d['step']
        if step in steps:
            title = {'expense':'Расход', 'income':'Приход', 'transfer':'Перевод'}[d['kind']]
            text = f'{title} · шаг {steps.index(step)+1} из {len(steps)}\n\n' + text
        self.api.send(chat, text, options)
