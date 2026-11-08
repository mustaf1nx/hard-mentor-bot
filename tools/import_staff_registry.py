"""Предпросмотр и импорт справочника стаффа в тестовую копию.

    python tools/import_staff_registry.py --json staff_registry_7.11.json \
        --database-url "postgresql://…/test_copy"

По умолчанию только показывает план. `--apply` записывает в `staff_members`.
Семь записей назначений в зоны НЕ импортируются; подтверждённые предметные
Ведущие становятся `mentor_roles` только при явном `--term-id`.
Пользователи не создаются; люди без точного username не добавляются.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

TEAM_MAP = {
    'Hard': 'hard',
    'Руководство Hard': 'hard_leadership',
    'Руководство школы (Soft)': 'school_leadership',
}
STATUS_MAP = {
    'активен': 'active',
    'вышел': 'left',
    # «Мало времени» — личная заметка владельца, человек остаётся стаффом.
    # В боте нет отдельного статуса, поэтому это active, а не paused/left.
    'мало времени': 'active',
}
SUBJECT_ALIASES = {
    # Владелец подтвердил: ICT — это тот же предмет, что в боте.
    'ict': 'information and communication technologies',
}
UNSUPPORTED_FIELDS = ('full_name', 'program', 'roles_reference', 'source', 'notes')


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _username(value: str | None) -> str:
    return (value or '').strip().lstrip('@').lower()


def _norm(value: str | None) -> str:
    return ' '.join((value or '').strip().casefold().split())


def _match(members: list[dict], users: list[asyncpg.Record]) -> tuple[list, list, list]:
    by_username: dict[str, list[asyncpg.Record]] = defaultdict(list)
    for user in users:
        by_username[_username(user['username'])].append(user)
    matched, ambiguous, missing = [], [], []
    for member in members:
        raw_id = str(member.get('telegram_id') or '').strip()
        username = _username(member.get('telegram_username'))
        hits = []
        if raw_id.isdigit():
            hits = [user for user in users if user['telegram_id'] == int(raw_id)]
        if not hits and username:
            hits = by_username.get(username, [])
        if len(hits) == 1:
            matched.append((member, hits[0]))
        elif len(hits) > 1:
            ambiguous.append((member, hits))
        else:
            missing.append(member)
    return matched, ambiguous, missing


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--json', default='staff_registry_7.11.json')
    parser.add_argument('--database-url', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--term-id', type=int, default=None,
                        help='существующий триместр для ролей Ведущих; не создаётся импортёром')
    args = parser.parse_args()

    data = json.loads(Path(args.json).read_text(encoding='utf-8'))
    members = data.get('staff_members', [])
    assignments = data.get('responsibility_assignments', [])
    conn = await asyncpg.connect(args.database_url)
    try:
        await conn.execute('SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY')
        users = await conn.fetch('select telegram_id, username, display_name from users')
        mentors = {row['telegram_id'] for row in await conn.fetch('select telegram_id from mentors')}
        existing = {row['telegram_id']: row for row in await conn.fetch(
            'select telegram_id, status, team, title from staff_members')}
        history_rows = await conn.fetch(
            'select id, display_name, username, status, team, title, source from staff_history')
        history_by_username = {_username(row['username']): row for row in history_rows if row['username']}
        subjects = await conn.fetch('select id, name, name_key from mentor_subjects')
        terms = await conn.fetch('select id, name, starts_on, weeks, active from terms order by id')
        selected_term = next((row for row in terms if row['id'] == args.term_id), None) if args.term_id else None
        existing_roles = await conn.fetch(
            'select telegram_id, subject_id, role from mentor_roles where term_id=$1',
            args.term_id) if selected_term else []
        role_index = {(row['telegram_id'], row['subject_id']): row['role'] for row in existing_roles}

        matched, ambiguous, missing = _match(members, users)
        creates, updates, unchanged, conflicts = [], [], [], []
        unsupported_status = []
        for member, user in matched:
            uid = user['telegram_id']
            status = STATUS_MAP.get(member.get('status'))
            if member.get('status') not in (None, '', 'активен', 'вышел') and status is None:
                unsupported_status.append((member, user))
            team = TEAM_MAP.get(member.get('contour'))
            title = (member.get('position') or '').strip() or None
            if title and len(title) > 120:
                conflicts.append((member, user, 'position длиннее 120 символов'))
                title = None
            row = existing.get(uid)
            if row is None:
                creates.append((member, user, status, team, title))
                continue
            changes = {}
            for column, value in (('status', status), ('team', team), ('title', title)):
                if value is None:
                    continue
                current = row[column]
                if current is None:
                    changes[column] = value
                elif current != value:
                    conflicts.append((member, user, f'{column}: {current!r} != {value!r}'))
            if changes:
                updates.append((member, user, changes))
            else:
                unchanged.append((member, user))

        # Вышедших без учётной записи сохраняем отдельно, без Telegram ID.
        history_creates, history_unchanged, history_conflicts = [], [], []
        for member in missing:
            if member.get('status') != 'вышел':
                continue
            username = _username(member.get('telegram_username'))
            display = (member.get('display_name') or member.get('full_name') or username).strip()
            team = TEAM_MAP.get(member.get('contour'))
            title = (member.get('position') or '').strip() or None
            if title and len(title) > 120:
                title = None
            planned = {'display_name': display, 'username': username or None,
                       'team': team, 'title': title, 'status': 'left',
                       'source': Path(args.json).name}
            current = history_by_username.get(username) if username else None
            if current is None:
                history_creates.append(planned)
            else:
                changed = [key for key in ('display_name', 'team', 'title', 'status', 'source')
                           if current[key] != planned[key] and planned[key] is not None]
                if changed:
                    history_conflicts.append((planned, current, changed))
                else:
                    history_unchanged.append(planned)

        # Подтверждённые Ведущие — отдельные mentor_roles, не зоны ответственности.
        matched_by_name = {_username(user['username']): (member, user) for member, user in matched}
        subject_by_key = {_norm(row['name']): row for row in subjects}
        role_creates, role_unchanged, role_conflicts, role_skips = [], [], [], []
        subject_creates = []
        for assignment in assignments:
            if assignment.get('zone') != 'Предмет':
                continue
            if not str(assignment.get('state', '')).casefold().startswith('подтверждено'):
                continue
            username = _username(assignment.get('telegram_username'))
            pair = matched_by_name.get(username)
            if pair is None:
                role_skips.append((assignment, 'пользователь не найден по username'))
                continue
            member, user = pair
            if user['telegram_id'] not in mentors:
                role_skips.append((assignment, 'сначала /start и добавление в mentors'))
                continue
            subject_name = (assignment.get('subject') or '').strip()
            key = _norm(subject_name)
            key = SUBJECT_ALIASES.get(key, key)
            subject = subject_by_key.get(key)
            new_subject = None
            if subject is None and key.startswith('programming'):
                new_subject = subject_name
                if new_subject not in [x[0] for x in subject_creates]:
                    subject_creates.append((new_subject, key))
            elif subject is None:
                role_skips.append((assignment, 'предмет не найден; автоматически не создаём'))
                continue
            subject_id = subject['id'] if subject else None
            if not selected_term:
                role_creates.append((assignment, user, subject_name, subject_id, new_subject, 'term_missing'))
                continue
            if subject_id is not None:
                existing_role = role_index.get((user['telegram_id'], subject_id))
                if existing_role == 'lead':
                    role_unchanged.append((assignment, user, subject_name))
                    continue
                if existing_role is not None:
                    role_conflicts.append((assignment, user, f'уже роль {existing_role!r}'))
                    continue
            role_creates.append((assignment, user, subject_name, subject_id, new_subject, None))

        print('=== Staff registry import preview ===')
        print('json:', args.json)
        print('members:', len(members), 'assignments:', len(assignments))
        print('matched:', len(matched), 'ambiguous:', len(ambiguous), 'missing:', len(missing))
        print('matched mentors:', sum(1 for _, u in matched if u['telegram_id'] in mentors),
              'matched not mentors:', sum(1 for _, u in matched if u['telegram_id'] not in mentors))
        print('planned creates:', len(creates), 'updates:', len(updates),
              'unchanged:', len(unchanged), 'conflicts:', len(conflicts))
        print('unlinked left for history: creates', len(history_creates),
              'unchanged', len(history_unchanged), 'conflicts', len(history_conflicts))
        print('unsupported status:', len(unsupported_status))
        print('unsupported fields:', ', '.join(UNSUPPORTED_FIELDS))
        print('assignments: NOT imported as zones; only confirmed subject leads become mentor_roles')
        print('term:', (f"#{selected_term['id']} {selected_term['name']} "
                        f"({selected_term['starts_on']}, {selected_term['weeks']} нед., "
                        f"active={selected_term['active']})")
              if selected_term else 'НЕ ВЫБРАН — роли не будут записаны, нужен --term-id')
        print('role creates:', len(role_creates), 'unchanged:', len(role_unchanged),
              'conflicts:', len(role_conflicts), 'skipped:', len(role_skips))
        print('subjects to create:', [name for name, _key in subject_creates] or 'none')

        if ambiguous:
            print('\nambiguous:')
            for member, hits in ambiguous:
                print(' ', member.get('telegram_username'), [(h['telegram_id'], h['display_name']) for h in hits])
        if missing:
            print('\nmissing:')
            for member in missing:
                print(' ', member.get('telegram_username'), member.get('display_name'), member.get('status'))
        if unsupported_status:
            print('\nunsupported status (left empty):')
            for member, user in unsupported_status:
                print(' ', member.get('telegram_username'), repr(member.get('status')))
        if conflicts:
            print('\nconflicts (not overwritten):')
            for member, user, reason in conflicts:
                print(' ', member.get('telegram_username'), reason)

        print('\nplanned changes:')
        for member, user, status, team, title in creates:
            print(' CREATE', member.get('telegram_username'), '->', user['telegram_id'],
                  'status=', status, 'team=', team, 'title=', title)
        for member, user, changes in updates:
            print(' UPDATE', member.get('telegram_username'), '->', user['telegram_id'], changes)
        for member, user in unchanged:
            print(' KEEP ', member.get('telegram_username'), '->', user['telegram_id'])
        if history_creates:
            print('\nhistory without account (planned):')
            for item in history_creates:
                print(' CREATE HISTORY', item['username'], item['display_name'], item['status'])
        for planned, current, changed in history_conflicts:
            print(' CONFLICT HISTORY', planned['username'], changed)

        print('\nassignment proposals (not written):')
        for assignment in assignments:
            subject = assignment.get('subject')
            subject_hits = [s['name'] for s in subjects if subject and
                            str(subject).casefold() in (s['name'].casefold(), s['name_key'].casefold())]
            print(' ', assignment.get('zone'), '|', subject, '|', assignment.get('telegram_username'),
                  '| state:', assignment.get('state'), '| subject matches:', subject_hits)

        print('\nconfirmed subject leads (mentor_roles, role=lead):')
        for assignment, user, subject_name, subject_id, new_subject, pending in role_creates:
            suffix = ' [term missing]' if pending else ''
            print(' CREATE ROLE', assignment.get('telegram_username'), '->', subject_name,
                  f'(user {user["telegram_id"]}, subject {subject_id or "new"}){suffix}')
        for assignment, user, subject_name in role_unchanged:
            print(' KEEP ROLE', assignment.get('telegram_username'), '->', subject_name)
        for assignment, user, reason in role_conflicts:
            print(' CONFLICT ROLE', assignment.get('telegram_username'), reason)
        for assignment, reason in role_skips:
            print(' SKIP ROLE', assignment.get('telegram_username'), reason)

        if not args.apply:
            print('\nDRY RUN: ничего не записано. Для записи в тестовую копию добавьте --apply.')
            return 0

        await conn.execute('SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE')
        async with conn.transaction():
            stamp = now_utc()
            for member, user, status, team, title in creates:
                await conn.execute(
                    'insert into staff_members (telegram_id, status, team, title, created_at, updated_at, updated_by) '
                    'values ($1, $2, $3, $4, $5, $5, null)',
                    user['telegram_id'], status, team, title, stamp)
            for member, user, changes in updates:
                for column, value in changes.items():
                    await conn.execute(
                        f'update staff_members set {column}=$1, updated_at=$2 where telegram_id=$3',
                        value, stamp, user['telegram_id'])
            for item in history_creates:
                await conn.execute(
                    'insert into staff_history (display_name, username, team, title, status, source, '
                    'created_at, updated_at) values ($1, $2, $3, $4, $5, $6, $7, $7)',
                    item['display_name'], item['username'], item['team'], item['title'],
                    item['status'], item['source'], stamp)
            roles_written = 0
            if selected_term:
                for name, key in subject_creates:
                    await conn.execute(
                        'insert into mentor_subjects (name, name_key) values ($1, $2) '
                        'on conflict (name_key) do nothing', name, key)
                for assignment, user, subject_name, subject_id, new_subject, pending in role_creates:
                    if pending:
                        continue
                    if subject_id is None:
                        subject_id = await conn.fetchval(
                            'select id from mentor_subjects where name_key=$1',
                            SUBJECT_ALIASES.get(_norm(subject_name), _norm(subject_name)))
                    if subject_id is None:
                        continue
                    await conn.execute(
                        'insert into mentor_roles (telegram_id, subject_id, term_id, role, created_at) '
                        'values ($1, $2, $3, $4, $5) on conflict do nothing',
                        user['telegram_id'], subject_id, selected_term['id'], 'lead', stamp)
                    roles_written += 1
        print('\nAPPLIED to test copy: creates', len(creates), 'history', len(history_creates),
              'updates', len(updates), 'roles', roles_written,
              'assignments skipped')
        return 0
    finally:
        await conn.close()


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    raise SystemExit(asyncio.run(main()))
