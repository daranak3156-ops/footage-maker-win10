"""Lumean public API: templates, asynchronous orders and signed downloads."""
import json
import time
from pathlib import Path
import requests

BASE = 'https://api.lumean.app/api/public'

def request(key, method, path, **kwargs):
    r = requests.request(method, BASE + path, headers={'X-API-KEY': key, 'X-Locale': 'ru'}, timeout=60, **kwargs)
    try: body = r.json()
    except ValueError: raise RuntimeError(f'Lumean: HTTP {r.status_code}, некорректный ответ')
    if not r.ok or body.get('success') is False:
        raise RuntimeError(f"Lumean ({r.status_code}): {body.get('message', 'Ошибка запроса')} {body.get('errors') or ''}")
    return body['data']

def templates(key):
    output=[]; page=1
    while True:
        data=request(key,'GET','/templates',params={'page':page,'per_page':30,'type':'tts'})
        output.extend(data.get('items',[]))
        if page>=data.get('last_page',1): return output
        page+=1

def speech(key, template, text, destination, stop, log):
    destination=Path(destination)
    receipt=destination.with_suffix('.lumean-order.json')
    # Keep the order ID so network failures never cause automatic duplicate charges.
    if receipt.exists():
        order=json.loads(receipt.read_text(encoding='utf-8'))
    else:
        try:
            order=request(key,'POST','/orders',json={'template_id':template,'input_text':text})
        except requests.RequestException as exc:
            raise RuntimeError('Связь с Lumean прервалась при создании заказа. Проверьте заказы на сайте перед повтором: заказ мог быть оплачен.') from exc
        receipt.write_text(json.dumps({'id':order['id']},ensure_ascii=False),encoding='utf-8')
    order_id=order['id']; log('Lumean: заказ '+str(order_id))
    deadline=time.monotonic()+1800
    while True:
        if stop.is_set(): raise RuntimeError('Ожидание остановлено. Заказ Lumean продолжает выполняться; его ID сохранён в проекте.')
        order=request(key,'GET','/orders/'+str(order_id))
        status=order.get('status')
        if status=='completed': break
        if status in ('failed','cancelled','canceled','partially_completed'):
            raise RuntimeError(f'Lumean: статус {status}. Проверьте заказ {order_id} на сайте. Автоматический платный повтор не выполняется.')
        if time.monotonic()>deadline: raise RuntimeError('Lumean: истекло время ожидания; заказ сохранён в проекте.')
        stop.wait(4)
    files=(order.get('result') or {}).get('files') or []
    audio=[p for p in files if isinstance(p,str) and p.lower().endswith(('.mp3','.wav','.m4a','.ogg','.flac','.aac'))]
    if len(audio)!=1: raise RuntimeError('Lumean вернул несколько файлов или не вернул готовое аудио. Выберите шаблон с объединением результата в один аудиофайл.')
    url=request(key,'POST','/storage/url',json={'path':audio[0]})['url']
    # Never send the API key to the storage host.
    r=requests.get(url,timeout=180); r.raise_for_status()
    destination.write_bytes(r.content)
