import io
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import bot
import receipts
from pdf_report import pdf_report
from pypdf import PdfReader

class API:
    url='https://api.telegram.org/botFAKE/'
    def __init__(self): self.messages=[];self.files=[]
    def send(self,chat,text,options=None): self.messages.append((text,options))
    def document(self,chat,name,content): self.files.append(content)
    def call(self,method,payload):
        return {'username':'ReceiptTestBot'} if method=='getMe' else {'file_path':'documents/test.pdf'}

class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.db'
        self.db=bot.connect(self.path,123,(456,789,987))
        self.db.execute("UPDATE users SET role='foreman' WHERE id=456")
        self.db.execute("UPDATE users SET role='investor' WHERE id=789");self.db.commit()
        self.api=API()
        self.ident=bot.record(self.db,dict(kind='expense',occurred_on='2026-09-24',account_id=bot.names(self.db,'accounts',456)[0]['id'],amount_kop=450000,currency='UAH',comment='Цемент'),1,456)
    def tearDown(self): self.db.close();self.tmp.cleanup()
    def cb(self,uid,v): bot.handle_callback(self.api,self.db,uid,uid,500,v,123)
    def msg(self,uid,v): bot.handle_message(self.api,self.db,uid,uid,v,123)
    def upload(self,update=2):
        self.cb(456,f'rc:add:{self.ident}')
        with patch('receipts.urllib.request.urlopen',return_value=io.BytesIO(b'%PDF-test')):
            receipts.media(self.api,self.db,456,456,{'document':{'file_id':'abc','file_size':9,'mime_type':'application/pdf'}},update,123)
    def test_persistent_files_dedup_and_access(self):
        self.upload();self.upload()
        self.assertEqual(receipts.metadata(self.db,self.ident)[2],1)
        self.db.close();self.db=bot.connect(self.path,123)
        self.cb(987,f'rc:files:{self.ident}');self.assertFalse(self.api.files)
        self.cb(789,f'rc:files:{self.ident}');self.assertEqual(self.api.files,[b'%PDF-test'])
        self.cb(123,f'rc:approve:{self.ident}');self.assertEqual(receipts.metadata(self.db,self.ident)[0],'approved')
        self.upload(3);self.assertEqual(receipts.metadata(self.db,self.ident)[0],'pending')
    def test_reason_review_auth_and_edit_reset(self):
        self.cb(456,f'rc:done:{self.ident}');self.assertIn('причину',self.api.messages[-1][0])
        self.cb(456,f'rc:reason:{self.ident}');self.msg(456,'Оплата рабочим')
        self.cb(456,f'rc:approve:{self.ident}');self.cb(789,f'rc:approve:{self.ident}')
        self.assertEqual(receipts.metadata(self.db,self.ident)[0],'pending')
        self.cb(123,f'rc:approve:{self.ident}');self.assertEqual(receipts.metadata(self.db,self.ident)[0],'approved')
        self.msg(987,f'/start receipt_{self.ident}');self.assertIn('недоступна',self.api.messages[-1][0])
    def test_filters_and_deletion(self):
        self.cb(123,'rc:filtermonth');self.msg(123,'2026-08');self.cb(123,'rc:list:missing:0')
        self.assertIn('Записей нет',self.api.messages[-1][0])
        self.cb(123,'rc:reset');self.cb(123,'rc:list:missing:0')
        self.assertIn(f'rc:view:{self.ident}',str(self.api.messages[-1]))
        self.upload()
        self.cb(123,f'op:delete:{self.ident}');self.cb(123,f'op:delconfirm:{self.ident}')
        self.assertEqual(self.db.execute('SELECT count(*) FROM receipts').fetchone()[0],0)
    def test_pdf_links(self):
        rows=receipts.enrich(self.api,self.db,bot.rows_for(self.db))
        data=pdf_report(rows,'2026-09-01','2026-09-24','Вита-Почтовая Дуплексы')
        reader=PdfReader(io.BytesIO(data));links=[]
        for page in reader.pages:
            for annot in page.get('/Annots',[]):
                a=annot.get_object()
                if a.get('/A'): links.append(a['/A'].get('/URI'))
        self.assertIn(f'https://t.me/ReceiptTestBot?start=receipt_{self.ident}',links)
        Path('/tmp/receipt-report.pdf').write_bytes(data)
    def test_expense_confirmation_starts_attachment_flow(self):
        account=bot.names(self.db,'accounts',456)[0]['id']
        bot.set_draft(self.db,456,dict(step='confirm',kind='expense',account_id=account,amount_kop=100,currency='UAH',occurred_on='2026-09-24'))
        self.cb(456,'save')
        self.assertEqual(bot.draft(self.db,456)['step'],'receipt_upload')
        self.assertEqual(bot.balances(self.db,456)[account][1]['UAH'],-450100)

    def test_invalid_and_oversized_files(self):
        self.cb(456,f'rc:add:{self.ident}')
        with patch('receipts.urllib.request.urlopen') as request:
            receipts.media(self.api,self.db,456,456,{'document':{'file_id':'x','file_size':receipts.LIMIT+1,'mime_type':'application/pdf'}},2,123)
            request.assert_not_called()
        with patch('receipts.urllib.request.urlopen',return_value=io.BytesIO(b'<html>bad')):
            receipts.media(self.api,self.db,456,456,{'document':{'file_id':'x','file_size':9,'mime_type':'application/pdf'}},3,123)
        self.assertEqual(receipts.metadata(self.db,self.ident)[2],0)
