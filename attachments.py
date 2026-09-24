"""Download only Telegram-issued, bounded PDF/image attachments."""
import re
import urllib.request
LIMIT=10*1024*1024


def download(api,payload):
    if payload.get('photo'): f=payload['photo'][-1];ext='.jpg'
    else:
        f=payload.get('document',{});ext={'application/pdf':'.pdf','image/jpeg':'.jpg','image/png':'.png'}.get(f.get('mime_type'))
    if not ext or f.get('file_size',LIMIT+1)>LIMIT: raise ValueError('Пришли фото, JPG, PNG или PDF до 10 МБ.')
    path=api.call('getFile',{'file_id':f['file_id']})['file_path']
    if not re.fullmatch(r'[A-Za-z0-9_./-]+',path) or '..' in path: raise ValueError('Некорректный путь документа.')
    with urllib.request.urlopen(api.url.replace('/bot','/file/bot',1)+path,timeout=60) as response: content=response.read(LIMIT+1)
    valid=(ext=='.pdf' and content.startswith(b'%PDF-')) or (ext=='.jpg' and content.startswith(b'\xff\xd8')) or (ext=='.png' and content.startswith(b'\x89PNG\r\n\x1a\n'))
    if len(content)>LIMIT or not valid: raise ValueError('Файл не подходит. Нужен PDF или фото до 10 МБ.')
    return content,ext
