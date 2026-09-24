"""Upload 2–10 photos as a Telegram media group; one photo uses sendPhoto."""
import json
import uuid
import urllib.request


def send(url, chat, photos, caption):
    if not 1<=len(photos)<=10: raise ValueError('В альбоме должно быть от 1 до 10 фото.')
    boundary='album'+uuid.uuid4().hex
    body=bytearray()
    def field(name,value):
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    field('chat_id',str(chat))
    single=len(photos)==1
    if single: field('caption',caption[:1024])
    else:
        media=[dict(type='photo',media=f'attach://image{i}') for i in range(len(photos))]
        media[0]['caption']=caption[:1024]
        field('media',json.dumps(media,ensure_ascii=False))
    for i,photo in enumerate(photos):
        name,content=photo
        ext='png' if name.lower().endswith('.png') else 'jpg'
        mime='image/png' if ext=='png' else 'image/jpeg'
        key='photo' if single else f'image{i}'
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="photo{i}.{ext}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
        body.extend(content);body.extend(b'\r\n')
    body.extend(f'--{boundary}--\r\n'.encode())
    req=urllib.request.Request(url+('sendPhoto' if single else 'sendMediaGroup'),data=bytes(body),headers={'Content-Type':'multipart/form-data; boundary='+boundary})
    with urllib.request.urlopen(req,timeout=60) as response: result=json.load(response)
    if not result.get('ok'): raise RuntimeError('Telegram не принял альбом. Попробуй ещё раз.')
    return result['result']
