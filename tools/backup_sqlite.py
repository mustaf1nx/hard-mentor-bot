"""Локальная копия SQLite и проверка восстановления в новый файл.

Примеры:
  python tools/backup_sqlite.py backup --output /путь/backup.db
  python tools/backup_sqlite.py restore --input /путь/backup.db --output /путь/restore-test.db

Для backup адрес источника берётся только из DATABASE_URL. PostgreSQL этим
инструментом не поддерживается. Файлы содержат данные пользователей: выбирайте
защищённое место хранения и не добавляйте их в репозиторий.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass


def source_from_url(url: str) -> Path:
    """Поддерживаем файловые SQLite URL, как в настройке приложения."""
    prefixes = ('sqlite+aiosqlite:///', 'sqlite:///')
    prefix = next((p for p in prefixes if url.startswith(p)), None)
    if prefix is None:
        raise ValueError('Поддерживается только файловая база SQLite; PostgreSQL не поддерживается.')
    name = url[len(prefix):]
    if not name or name == ':memory:' or '?' in name or '#' in name:
        raise ValueError('Нужен обычный путь к файлу SQLite без параметров URL.')
    return Path(name)


def _check(con: sqlite3.Connection) -> None:
    rows = con.execute('PRAGMA integrity_check').fetchall()
    if rows != [('ok',)]:
        raise ValueError('Проверка целостности SQLite не пройдена.')


def copy_and_verify(source: Path, destination: Path) -> None:
    """Копирует снимок через SQLite backup API в ещё не существующий файл."""
    source = source.resolve(strict=True)
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError('Источник должен быть непустым файлом SQLite.')
    # mode=ro исключает случайное создание или изменение исходного файла.
    uri = source.as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as src:
        _check(src)
        # O_EXCL защищает от перезаписи, включая совпадение с источником.
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with sqlite3.connect(destination) as dst:
                src.backup(dst)
                _check(dst)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Локальное копирование и проверка восстановления SQLite.')
    commands = parser.add_subparsers(dest='command', required=True)
    backup = commands.add_parser('backup', help='Источник задаётся DATABASE_URL; выходной файл должен быть новым.')
    backup.add_argument('--output', type=Path, required=True)
    restore = commands.add_parser('restore', help='Восстановление только в новый файл.')
    restore.add_argument('--input', type=Path, required=True)
    restore.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        source = source_from_url(os.environ.get('DATABASE_URL', '').strip()) if args.command == 'backup' else args.input
        copy_and_verify(source, args.output)
    except (OSError, sqlite3.Error, ValueError) as exc:
        # Не выводим URL и содержимое исключения: оно может содержать данные пути.
        print(f'Операция не выполнена ({type(exc).__name__}). Проверьте тип БД, источник и новый путь назначения.', file=sys.stderr)
        return 1
    print('Копия создана и проверена: SQLite integrity_check = ok.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
