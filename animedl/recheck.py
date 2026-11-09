"""Owner-triggered source verification. Never sends media or publishes to NAS."""
from __future__ import annotations
import json
import time
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from .models import EpisodeRef
from .observability import operation


def save_case(store, uid, proof, title, episode, voice, quality):
    from .sites.registry import get_parser
    parts = urlsplit(title.url)
    if parts.scheme not in ('http', 'https') or parts.username or parts.password:
        return
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
    get_parser(url)
    identity = store.incidents.identity(uid, proof)[0]
    store.db.execute('CREATE TABLE IF NOT EXISTS audit_recheck_cases '
                     '(id TEXT PRIMARY KEY,created REAL NOT NULL,payload TEXT NOT NULL)')
    payload = dict(uid=uid,url=url,site=title.site,anime_id=title.anime_id,
                   number=episode.number,db_id=episode.db_id,season=episode.season,
                   voice=voice,quality=quality,proof=proof)
    store.db.execute('INSERT OR REPLACE INTO audit_recheck_cases VALUES(?,?,?)',
                     (identity,time.time(),json.dumps(payload,ensure_ascii=False)))
    store.db.execute('DELETE FROM audit_recheck_cases WHERE created<?', (time.time()-30*86400,))
    store.db.execute('DELETE FROM audit_recheck_cases WHERE id IN '
                     '(SELECT id FROM audit_recheck_cases ORDER BY created DESC LIMIT -1 OFFSET 500)')
    store.db.commit()


async def verify_case(store, cfg, identity, *, download=None, cancel_check=None):
    from .sites.registry import get_parser
    from .services import extract_variants
    from .download.downloader import download_variant, Progress
    row = store.db.execute("SELECT user_id,state,operation,opkey,generation FROM audit_incidents WHERE id=?", (identity,)).fetchone()
    if not row or row[1]!='open' or row[2]!='source_download':
        raise ValueError('Нужна открытая ошибка скачивания источника. Отправку в Telegram эта проверка не подтверждает.')
    exists=store.db.execute("SELECT name FROM sqlite_master WHERE name='audit_recheck_cases'").fetchone()
    saved=store.db.execute('SELECT created,payload FROM audit_recheck_cases WHERE id=?',(identity,)).fetchone() if exists else None
    if not saved or saved[0]<time.time()-30*86400:
        raise ValueError('Нет сохранённых параметров: ошибка возникла до обновления или проверка устарела.')
    case=json.loads(saved[1]);generation=row[4]
    parser=get_parser(case['url'])
    title=await parser.fetch_title(case['url'])
    if title.site!=case['site'] or title.anime_id!=case['anime_id']:
        raise ValueError('Источник вернул другое произведение; восстановление не подтверждено.')
    episode=EpisodeRef(case['number'],case['db_id'],season=case['season'])
    buttons=await parser.fetch_episode_players(title,episode)
    buttons=[b for b in buttons if b.voice==case['voice']]
    if not buttons:raise ValueError('Исходная озвучка недоступна.')
    proof=operation('source_download',case['site'],case['url'],case['anime_id'],case['season'],case['number'],case['voice'],case['quality'],
                    **{k:v for k,v in case['proof'].items() if k not in ('operation','operation_key','attempt_id','attempt_started')})
    if proof['operation_key']!=row[3] or case['uid']!=row[0]:
        raise ValueError('Параметры проверки не совпадают с ошибкой.')
    cfg.tmp_dir.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(cfg.tmp_dir).free<1024**3:raise ValueError('Недостаточно места для проверки.')
    started=time.monotonic();available=set()
    for button in buttons:
        if time.monotonic()-started>1200 or cancel_check and cancel_check():
            raise ValueError('Проверка отменена или достигнут лимит времени.')
        try:
            variants=await extract_variants(button)
        except Exception:
            continue
        available.update(v.quality for v in variants)
        for variant in variants:
            if variant.quality!=case['quality']:continue
            progress=Progress()
            with tempfile.TemporaryDirectory(prefix='episode_recheck_',dir=cfg.tmp_dir) as folder:
                def cancelled():
                    return bool(cancel_check and cancel_check()) or time.monotonic()-started>1200 or progress.downloaded>4*1024**3 or shutil.disk_usage(cfg.tmp_dir).free<256*1024**2
                try:
                    result=await (download or download_variant)(variant,Path(folder)/'episode',progress,cancel_check=cancelled)
                    if result.size_bytes<=0 or result.duration<=0 or max(result.height,result.width*9/16)<case['quality']*.94:
                        continue
                except Exception:
                    continue
                current=store.db.execute('SELECT generation,state FROM audit_incidents WHERE id=?',(identity,)).fetchone()
                if current!=(generation,'open'):raise ValueError('Состояние ошибки изменилось во время проверки.')
                # Only this actor/resource, after full media validation; no user request needed.
                store.record('operation_succeeded',case['uid'],**proof,proof='download_validated')
                state=store.db.execute('SELECT state FROM audit_incidents WHERE id=?',(identity,)).fetchone()[0]
                if state!='resolved':raise ValueError('Во время проверки появился более новый сбой; закрытие отложено.')
                return f"Проверка успешна: серия {case['number']}, {case['quality']}p, файл целиком проверен. Уведомление пользователю поставлено в очередь."
    raise ValueError('Исходное качество не подтверждено. Доступно: '+', '.join(f'{q}p' for q in sorted(available,reverse=True)))
