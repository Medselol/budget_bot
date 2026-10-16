"""Offline presentation checks: callbacks and authorization stay untouched."""
import unittest
from unittest.mock import patch

import bot
from action_labels import action_label, has_leading_icon
from test_multiuser import FakeBot


class ActionLabelTests(unittest.TestCase):
    def test_semantic_icons_for_main_actions(self):
        for label, callback, icon in (
            ('Расход', 'new:expense', '➖'), ('Приход', 'new:income', '➕'),
            ('Обмен USD ↔ UAH', 'fx:new', '💱'), ('Проверка расходов', 'rc:list', '🔎'),
            ('Отчёты', 'ui:reports', '📊'), ('Фотоотчёты стройки', 'site:home', '📸'),
            ('Главное меню', 'menu', '🏠'), ('Материалы', 'type:0', '🧱'),
            ('Добавить чеки', 'rc:add:42', '➕'), ('Удалить', 'op:delete:42', '🗑️'),
            ('Подтвердить', 'site:confirm:nonce', '✅'),
            ('Гривны (UAH)', 'currency:UAH', '🇺🇦'),
        ):
            with self.subTest(callback=callback):
                self.assertEqual(action_label(label, callback), icon + ' ' + label)

    def test_existing_icons_and_unicode_labels_are_idempotent(self):
        for label in ('✅ Проверено', '🧾 Чеки', '⬅️ Назад', '↔️ Перевод', '🇺🇦 Гривны',
                      '👩🏽‍🔧 Прораб', '♨️ Отопление'):
            self.assertEqual(action_label(label, 'anything:42'), label)
        for label, callback in [('← Назад', 'nav:back'), ('Далее →', 'site:list:1'),
                                ('＋ Новый фотоотчёт', 'site:new'), ('Любое действие', 'future:route')]:
            rendered = action_label(label, callback)
            self.assertTrue(has_leading_icon(rendered))
            self.assertEqual(action_label(rendered, callback), rendered)

    def test_keyboard_keeps_exact_callbacks_and_input_options(self):
        options = [('Изменить права', 'role:pick:123'), ('Да, удалить навсегда', 'participant:purgeconfirm:123'),
                   ('Длинное название операции для чтения', 'future:opaque:abc'),
                   ('Главное меню', 'menu'), ('← Назад', 'nav:back')]
        original = options[:]
        rows = bot.keyboard(options)['inline_keyboard']
        flat = [b for row in rows for b in row]
        self.assertCountEqual([b['callback_data'] for b in flat], [c for _, c in options])
        self.assertEqual(options, original)
        self.assertTrue(all(has_leading_icon(b['text']) for b in flat))
        self.assertEqual([b['callback_data'] for b in rows[-1]], ['nav:back', 'menu'])
        self.assertEqual(next(len(row) for row in rows if row[0]['callback_data'] == 'future:opaque:abc'), 1)

    def test_role_menus_all_render_icons_without_extra_routes(self):
        db = bot.connect(':memory:', 123, (456, 789, 987))
        self.addCleanup(db.close)
        for uid, role in ((456, 'foreman'), (789, 'investor'), (987, 'editor')):
            db.execute('UPDATE users SET role=? WHERE id=?', (role, uid))
        db.commit()
        api = FakeBot()
        for uid in (123, 456, 789, 987):
            for route in ('menu', 'ui:finance', 'ui:reports', 'ui:team', 'site:home', 'ctl:home'):
                bot.handle_callback(api, db, uid, uid, 999, route, 123)
                options = api.messages[-1][2] or []
                buttons = [b for row in bot.keyboard(options)['inline_keyboard'] for b in row]
                self.assertCountEqual([b['callback_data'] for b in buttons], [c for _, c in options])
                self.assertTrue(all(has_leading_icon(b['text']) for b in buttons))
            if uid in (456, 789):
                bot.handle_callback(api, db, uid, uid, 999, 'ui:team', 123)
                self.assertNotIn('users:list', [c for _, c in api.messages[-1][2] or []])

    def test_real_telegram_renderer_decorates_notification_and_form_buttons(self):
        api = bot.Telegram('synthetic-no-network')
        with patch.object(api, 'call') as call:
            api.send(123, 'Test', [('Открыть заявку', 'ctl:req:view:5'),
                                   ('Отправить на проверку', 'site:submit:3:1'),
                                   ('Новый неизвестный шаг', 'future:nonce:abc')])
        method, payload = call.call_args.args
        self.assertEqual(method, 'sendMessage')
        buttons = [b for row in payload['reply_markup']['inline_keyboard'] for b in row]
        self.assertTrue(all(has_leading_icon(b['text']) for b in buttons))
        self.assertEqual([b['callback_data'] for b in buttons],
                         ['ctl:req:view:5', 'site:submit:3:1', 'future:nonce:abc'])
