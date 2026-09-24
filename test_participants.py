import unittest
import bot
import participants
from test_multiuser import FakeBot

class UserBot(FakeBot):
    def __init__(self):super().__init__();self.chats={}
    def call(self,method,payload):
        assert method=='getChat'
        return self.chats[payload['chat_id']]

class ParticipantTests(unittest.TestCase):
    def setUp(self):
        self.db=bot.connect(':memory:',123,(456,789))
        self.api=UserBot()
        self.db.execute("UPDATE users SET role='investor' WHERE id=789");self.db.commit()
        self.contact(555,'Builder555')
    def tearDown(self):self.db.close()
    def contact(self,uid,nick):
        participants.remember(self.db,{'id':uid,'username':nick,'first_name':'Тест'})
        self.api.chats[uid]={'id':uid,'type':'private','username':nick}
    def cb(self,value,uid=123):bot.handle_callback(self.api,self.db,uid,uid,100,value,123)
    def msg(self,text,uid=123):bot.handle_message(self.api,self.db,uid,uid,text,123)
    def test_admission_requires_role_and_deduplicates(self):
        self.cb('participant:add');self.msg('@BUILDER555')
        self.assertIsNone(bot.role_for(self.db,555))
        self.assertEqual(bot.draft(self.db,123)['step'],'participant_admit')
        self.cb('participant:admit:foreman')
        self.assertEqual(bot.role_for(self.db,555),'foreman')
        self.assertEqual(len(bot.names(self.db,'accounts',555)),3)
        self.cb('participant:admit:editor')
        self.assertEqual(bot.role_for(self.db,555),'foreman')
        self.msg('/adduser @builder555')
        self.assertIsNone(bot.draft(self.db,123))
    def test_unknown_and_changed_username_not_granted(self):
        self.cb('participant:add');self.msg('@unknown')
        self.assertEqual(bot.draft(self.db,123)['step'],'participant_lookup')
        self.msg('@builder555')
        self.api.chats[555]['username']='changed555'
        self.cb('participant:admit:editor')
        self.assertIsNone(bot.role_for(self.db,555))
    def test_reassigned_username_cannot_switch_confirmed_id(self):
        self.cb('participant:add');self.msg('@builder555')
        self.contact(666,'builder555')
        self.cb('participant:admit:editor')
        self.assertIsNone(bot.role_for(self.db,555));self.assertIsNone(bot.role_for(self.db,666))
        self.assertIsNone(self.db.execute('SELECT username FROM telegram_contacts WHERE id=555').fetchone()[0])
    def test_removed_username_clears_lookup(self):
        participants.remember(self.db,{'id':555,'first_name':'Тест'})
        with self.assertRaises(ValueError):participants.lookup(self.api,self.db,'@builder555')
    def test_rename_revoke_restore_preserve_operations(self):
        aid=bot.names(self.db,'accounts',456)[0]['id']
        bot.record(self.db,dict(kind='income',account_id=aid,amount_kop=100,occurred_on='2026-09-24',currency='UAH'),1,456)
        self.cb('participant:rename:456');self.msg('Игорь Прораб')
        self.assertEqual(self.db.execute('SELECT name FROM users WHERE id=456').fetchone()[0],'Игорь Прораб')
        self.cb('participant:remove:456');self.assertIsNotNone(bot.role_for(self.db,456))
        self.cb('participant:removeconfirm:456');self.assertIsNone(bot.role_for(self.db,456))
        self.assertEqual(len(bot.rows_for(self.db,user_id=456)),1)
        self.cb('participant:restoreconfirm:456');self.assertIsNotNone(bot.role_for(self.db,456))
        self.assertEqual(bot.balances(self.db,456)[aid][1]['UAH'],100)
    def test_removed_participant_only_in_archive_and_restored_to_active(self):
        self.cb('participant:removeconfirm:456')
        self.assertNotIn('participant:view:456', [v for _,v in self.api.messages[-1][2]])
        self.cb('users:list')
        self.assertNotIn('participant:view:456', [v for _,v in self.api.messages[-1][2]])
        self.cb('participant:archive:0')
        self.assertIn('participant:view:456', [v for _,v in self.api.messages[-1][2]])
        self.assertNotIn('participant:view:123', [v for _,v in self.api.messages[-1][2]])
        self.cb('participant:restoreconfirm:456')
        self.assertIn('participant:view:456', [v for _,v in self.api.messages[-1][2]])

    def test_empty_archived_user_permanent_deletion_requires_confirmation(self):
        self.cb('participant:removeconfirm:456')
        self.cb('participant:purgeconfirm:456')
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM users WHERE id=456').fetchone())
        self.cb('participant:purge:456');self.cb('participant:purgeconfirm:456')
        self.assertIsNone(self.db.execute('SELECT 1 FROM users WHERE id=456').fetchone())
        self.assertFalse(bot.names(self.db,'accounts',456))
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())

    def test_delete_with_history_hides_user_preserves_money_and_blocks_restore(self):
        aid=bot.names(self.db,'accounts',456)[0]['id']
        bot.record(self.db,dict(kind='income',account_id=aid,amount_kop=100,occurred_on='2026-09-24',currency='UAH'),1,456)
        self.cb('participant:removeconfirm:456');self.cb('participant:purge:456');self.cb('participant:purgeconfirm:456')
        self.assertIsNone(bot.role_for(self.db,456))
        self.assertEqual(bot.summary(bot.rows_for(self.db))['UAH']['financing'],100)
        self.cb('participant:restoreconfirm:456');self.assertIsNone(bot.role_for(self.db,456))
        self.cb('participant:archive:0')
        self.assertNotIn('participant:view:456',[v for _,v in self.api.messages[-1][2]])
        self.cb('people:view:archive:0',789)
        self.assertNotIn('people:view:456',[v for _,v in self.api.messages[-1][2]])

    def test_archives_in_balances_and_accounts(self):
        self.cb('participant:rename:456');self.msg('АрхивныйТест')
        self.cb('participant:removeconfirm:456')
        self.cb('team:balances',789);self.assertNotIn('АрхивныйТест',self.api.messages[-2][1])
        self.cb('team:archive',789);self.assertIn('АрхивныйТест',self.api.messages[-2][1])
        self.cb('people:menu',789);self.assertNotIn('people:view:456',[v for _,v in self.api.messages[-1][2]])
        self.cb('people:view:archive:0',789);self.assertIn('people:view:456',[v for _,v in self.api.messages[-1][2]])

    def test_permanent_delete_blocks_owner_active_users_and_investor(self):
        for target in (123,456):
            self.cb(f'participant:purge:{target}');self.cb(f'participant:purgeconfirm:{target}')
            self.assertIsNotNone(bot.role_for(self.db,target))
        self.cb('participant:removeconfirm:456')
        self.cb('participant:purge:456',789);self.cb('participant:purgeconfirm:456',789)
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM users WHERE id=456').fetchone())

    def test_observers_and_members_cannot_manage(self):
        for uid in (456,789):
            for value in ('participant:add','participant:rename:123','participant:removeconfirm:123','participant:removeconfirm:456','participant:admit:editor'):
                self.cb(value,uid)
            self.msg('/adduser @builder555',uid)
        self.assertEqual(bot.role_for(self.db,123),'owner');self.assertIsNotNone(bot.role_for(self.db,456));self.assertIsNone(bot.role_for(self.db,555))
    def test_owner_and_demoted_draft_protected(self):
        self.cb('participant:removeconfirm:123');self.assertEqual(bot.role_for(self.db,123),'owner')
        self.db.execute("UPDATE users SET role='editor' WHERE id=456");self.db.commit()
        self.cb('participant:rename:123',456);self.assertIsNone(bot.draft(self.db,456))
        self.cb('participant:add',456);self.msg('@builder555',456)
        self.cb('role:set:456:investor');self.cb('participant:admit:editor',456)
        self.assertIsNone(bot.role_for(self.db,555))

if __name__=='__main__':unittest.main()
