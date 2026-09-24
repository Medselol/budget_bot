#!/usr/bin/env python3
"""Validate and restore a snapshot to a NEW file. Never overwrite a running database."""
import argparse
import shutil
import sqlite3
import zipfile
from pathlib import Path


def restore(archive, destination):
    destination=Path(destination)
    if destination.exists(): raise ValueError('Файл назначения уже существует. Выбери новое имя.')
    created=False
    try:
        with zipfile.ZipFile(archive) as z:
            item=z.getinfo('ledger.sqlite3')
            with z.open(item) as source, destination.open('xb') as target:
                created=True
                shutil.copyfileobj(source,target,length=1024*1024)
        destination.chmod(0o600)
        db=sqlite3.connect(f'file:{destination.resolve()}?mode=ro',uri=True)
        try:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Копия повреждена.')
            if db.execute('PRAGMA foreign_key_check').fetchone(): raise ValueError('В копии нарушены связи.')
            required={'operations','users','accounts','settings'}
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required<=tables: raise ValueError('Это не база бота.')
        finally: db.close()
    except Exception:
        if created: destination.unlink(missing_ok=True)
        raise
    return destination


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Проверка копии и восстановление в новый файл')
    parser.add_argument('archive');parser.add_argument('destination')
    args=parser.parse_args()
    print(restore(args.archive,args.destination))
    print('Проверка завершена. Останови бот перед переключением BOT_DB_PATH на восстановленный файл.')
