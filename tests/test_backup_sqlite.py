"""Проверки резервной копии на временных базах без реальных данных."""
import sqlite3

import pytest

from tools.backup_sqlite import copy_and_verify, source_from_url


def test_backup_and_restore_include_committed_wal_data(tmp_path):
    source, backup, restored = (tmp_path / name for name in ('source.db', 'backup.db', 'restored.db'))
    with sqlite3.connect(source) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE example (value TEXT)')
        connection.execute('INSERT INTO example VALUES (?)', ('fixture',))
        connection.commit()
        copy_and_verify(source, backup)
        copy_and_verify(backup, restored)
    with sqlite3.connect(restored) as connection:
        assert connection.execute('SELECT value FROM example').fetchall() == [('fixture',)]
        assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)


def test_existing_destination_and_source_are_never_overwritten(tmp_path):
    source, destination = tmp_path / 'source.db', tmp_path / 'existing.db'
    with sqlite3.connect(source) as connection:
        connection.execute('CREATE TABLE example (value TEXT)')
    destination.write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        copy_and_verify(source, destination)
    assert destination.read_bytes() == b'keep'
    original = source.read_bytes()
    with pytest.raises(FileExistsError):
        copy_and_verify(source, source)
    assert source.read_bytes() == original


def test_invalid_source_does_not_create_destination(tmp_path):
    source, destination = tmp_path / 'bad.db', tmp_path / 'new.db'
    source.write_bytes(b'not a database')
    with pytest.raises(sqlite3.DatabaseError):
        copy_and_verify(source, destination)
    assert not destination.exists()


@pytest.mark.parametrize('url', ['', 'postgresql://user:secret@host/db',
                                 'sqlite:///:memory:', 'sqlite:///file.db?mode=ro'])
def test_unsupported_urls_rejected_without_echoing_credentials(url):
    with pytest.raises(ValueError) as error:
        source_from_url(url)
    assert 'secret' not in str(error.value)


def test_sqlite_url_forms():
    assert source_from_url('sqlite+aiosqlite:///./local.db').as_posix() == 'local.db'
    assert source_from_url('sqlite:///./local.db').as_posix() == 'local.db'
