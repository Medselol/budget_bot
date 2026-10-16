"""Presentation-only action icons shared by every Telegram inline keyboard.

Labels may be dynamic or already decorated. Callback data remains the sole
routing/authorization input and is never transformed by this module.
"""

# Specific destinations take precedence over broad action/family fallbacks.
DESTINATIONS = {
    'menu': '🏠', 'nav:back': '⬅️', 'cancel': '❌', 'save': '✅',
    'new:expense': '➖', 'new:income': '➕', 'new:transfer': '↔️',
    'balance': '💰', 'team:balances': '💰', 'team:archive': '🗄️',
    'fund:menu': '💸', 'fund:save': '✅', 'fx:new': '💱',
    'ui:finance': '💰', 'ui:reports': '📊', 'ui:team': '👥', 'ui:help': '❓',
    'ops:list': '📋', 'users:list': '👥', 'people:menu': '👥',
    'participant:add': '➕', 'participant:find': '🔎',
    'report:menu': '📊', 'all:menu': '📊', 'rc:mine': '🧾', 'rc:list': '🔎',
    'rc:next': '➡️', 'rc:reset': '🧹', 'rc:filtermonth': '📅',
    'rc:filteruser': '👤', 'site:home': '📸', 'site:new': '📸',
    'site:reset': '🧹', 'ec:settings': '⚙️', 'ec:reset': '🧹',
    'ctl:home': '🏗', 'ctl:weekly': '📅', 'ctl:weeklynow': '📊',
    'ctl:weeklypdf': '📄', 'ctl:budgetpdf': '📄', 'ctl:backup': '💾',
    'ctl:backup:create': '💾', 'ctl:backup:download': '📥',
    'ctl:new:request': '➕', 'ctl:new:debt': '➕', 'ctl:new:budget': '✏️',
    'currency:UAH': '🇺🇦', 'currency:USD': '💵',
}
ACTIONS = {
    'purge': '🗑️', 'purgeconfirm': '🗑️', 'delete': '🗑️', 'delconfirm': '🗑️',
    'deletealbum': '🗑️', 'remove': '🗑️', 'discard': '🗑️',
    'reject': '❌', 'cancel': '❌', 'rework': '↩️', 'pending': '↩️',
    'approve': '✅', 'accept': '✅', 'confirm': '✅', 'save': '✅', 'done': '✅',
    'edit': '✏️', 'rename': '✏️', 'manage': '✏️', 'reason': '💬',
    'find': '🔎', 'filter': '🔎', 'settings': '⚙️', 'reset': '🧹',
    'archive': '🗄️', 'download': '📥', 'attach': '📎', 'files': '📎',
    'upload': '📷', 'photos': '📸', 'photo': '📷', 'photomanage': '📸',
    'submit': '📤', 'delivery': '📨', 'retry': '🔄',
    'date': '📅', 'due': '📅', 'period': '📅', 'month': '🗓',
    'week': '📅', 'year': '🗓', 'today': '📅', 'yesterday': '📅',
    'account': '👛', 'acct': '👛', 'from': '👛', 'to': '👛', 'currency': '💱',
    'budget': '📊', 'debt': '🧾', 'req': '📝', 'audit': '📋', 'event': '📋',
    'pay': '💸', 'fund': '💸', 'payments': '📋', 'backup': '💾',
    'people': '👥', 'person': '👤', 'author': '👤', 'role': '🔐', 'pick': '🔐',
    'new': '➕', 'add': '➕', 'skip': '⏭️', 'empty': '⏭️',
}
FAMILIES = {
    'op': '📋', 'ops': '📋', 'tx': '↔️', 'rc': '🧾', 'ec': '🔔',
    'site': '📸', 'ctl': '🏗', 'participant': '👤', 'people': '👥',
    'user': '👤', 'users': '👥', 'role': '🔐', 'team': '👥',
    'report': '📊', 'rp': '📅', 'all': '📊', 'csv': '📄', 'csvall': '📄',
    'pdf': '📄', 'fund': '💸', 'fx': '💱', 'proj': '🏗',
    'stage': '🏗', 'type': '🧱', 'source': '💰', 'acct': '👛',
    'currency': '💱', 'date': '📅', 'comment': '💬', 'nav': '⬅️',
}
CHOICES = {
    'Материалы': '🧱', 'Работа': '🛠', 'Техника': '🚜', 'Доставка': '🚚',
    'Услуги': '🤝', 'Прочее': '📌', 'Фундамент': '🧱', 'Стены': '🧱',
    'Земля и оформление': '📐', 'Проектирование': '📐', 'Перекрытия': '🏗',
    'Кровля': '🏠', 'Окна и двери': '🚪', 'Фасад': '🏘', 'Электрика': '💡',
    'Сантехника': '🚿', 'Отопление': '♨️', 'Вентиляция': '💨',
    'Внутренние работы': '🛠', 'Коммуникации': '🔌', 'Благоустройство': '🌳',
    'Личные средства': '👛', 'Инвестор': '🤝', 'Кредит': '🏦',
    'Аванс покупателя': '💵', 'Оплата покупателя': '💵',
    'Возврат поставщика': '↩️', 'Наличные': '💵', 'Безналичные': '💳',
}


def has_leading_icon(label):
    """Recognize existing emoji/symbol icons without adding a duplicate."""
    if not label:
        return False
    point = ord(label[0])
    return (0x1F000 <= point <= 0x1FAFF or 0x2600 <= point <= 0x27BF
            or 0x2B00 <= point <= 0x2BFF or point in (0x21A9, 0x21AA)
            or (point in (0x2190, 0x2191, 0x2192, 0x2193, 0x2194)
                and label[1:2] == '\ufe0f'))


def action_label(label, callback):
    """Return an icon + readable action; never change callback or permissions."""
    text = str(label).strip()
    if has_leading_icon(text):
        return text
    # Replace old text-only arrow/plus decorations with a proper leading icon.
    bare = text.strip('←→＋ ').strip()
    lower = bare.casefold()
    if text.startswith('←') or lower.startswith(('назад', 'предыдущ')):
        icon = '⬅️'
    elif text.endswith('→') or lower.startswith(('далее', 'следующ')):
        icon = '➡️'
    elif lower.startswith(('отмена', 'отменить', 'отклонить')):
        icon = '❌'
    elif lower.startswith(('да, удалить', 'удалить')):
        icon = '🗑️'
    elif lower.startswith(('подтвердить', 'готово', 'принять', 'одобрить')):
        icon = '✅'
    elif lower.startswith(('к списку', 'к журналу', 'к настройкам', 'к проверке', 'к расходу',
                           'к операциям', 'к участникам', 'к отчёту', 'к заявк', 'к платеж')):
        icon = '⬅️'
    elif lower.startswith(('пропустить', 'позже')):
        icon = '⏭️'
    elif callback in DESTINATIONS:
        icon = DESTINATIONS[callback]
    elif bare in CHOICES:
        icon = CHOICES[bare]
    else:
        parts = str(callback).split(':')
        # The innermost action is usually more specific (request -> approve).
        icon = next((ACTIONS[part] for part in reversed(parts) if part in ACTIONS),
                    FAMILIES.get(parts[0], '🔹'))
    return icon + ' ' + (bare or 'Выбрать')
