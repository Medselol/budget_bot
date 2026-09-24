from test_access import AccessTests
import bot

class TransferEditTests(AccessTests):
    def test_edit_conserves_balances_and_identity(self):
        self.seed();ident=self.fund()
        self.cb(987,f'op:edit:{ident}')
        self.cb(987,'tx:field:amount_kop');self.msg(987,'700')
        self.assertEqual(bot.balances(self.db,456)[self.b][1]['UAH'],40000)
        self.cb(987,'tx:save')
        row=self.db.execute('SELECT * FROM operations WHERE id=?',(ident,)).fetchone()
        self.assertEqual((row['amount_kop'],row['user_id'],row['update_id']),(70000,987,2))
        self.assertEqual(bot.balances(self.db,123)[self.a][1]['UAH'],30000)
        self.assertEqual(bot.balances(self.db,456)[self.b][1]['UAH'],70000)
        self.cb(987,'tx:save')
        self.assertEqual(len(bot.rows_for(self.db)),2)
    def test_delete_requires_confirmation_and_reverses_balances(self):
        self.seed();ident=self.fund()
        self.cb(123,f'op:delconfirm:{ident}')
        self.assertEqual(len(bot.rows_for(self.db)),2)
        self.cb(123,f'op:delete:{ident}');self.cb(123,f'op:delconfirm:{ident}')
        self.assertEqual(bot.balances(self.db,123)[self.a][1]['UAH'],100000)
        self.assertEqual(bot.balances(self.db,456)[self.b][1]['UAH'],0)
    def test_stale_edit_and_demoted_editor_blocked(self):
        self.seed();ident=self.fund()
        self.cb(987,f'op:edit:{ident}');self.cb(123,f'op:edit:{ident}')
        self.cb(123,'tx:field:comment');self.msg(123,'Исправлено');self.cb(123,'tx:save')
        self.cb(987,'tx:field:amount_kop');self.msg(987,'900');self.cb(987,'tx:save')
        self.assertEqual(self.db.execute('SELECT amount_kop FROM operations WHERE id=?',(ident,)).fetchone()[0],40000)
        self.cb(123,'role:set:987:member');self.cb(987,'tx:save')
        self.assertEqual(len(bot.rows_for(self.db)),2)
    def test_currency_accounts_date_and_cancel(self):
        self.seed();ident=self.fund();self.cb(123,f'op:edit:{ident}')
        self.cb(123,'tx:field:currency');self.cb(123,'tx:currency:USD')
        self.cb(123,'tx:field:occurred_on');self.msg(123,'2026-09-01')
        self.cb(123,'tx:field:to_account_id');self.cb(123,f'tx:account:{self.account(987)}')
        self.cb(123,'tx:save')
        self.assertEqual(bot.balances(self.db,456)[self.b][1]['UAH'],0)
        self.assertEqual(bot.balances(self.db,987)[self.account(987)][1]['USD'],40000)
        self.cb(123,f'op:delete:{ident}');self.cb(123,'cancel');self.cb(123,f'op:delconfirm:{ident}')
        self.assertEqual(len(bot.rows_for(self.db)),2)
