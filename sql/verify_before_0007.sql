-- Read-only аудит перед миграцией 0007.
-- Требует схему не старше 0006 (есть mentoring_sessions с format и learning_materials).
-- Для старой production-схемы без terms/mentor_roles/format используйте
--   python tools/audit_pg_copy.py --database-url "..."
-- Запускать ТОЛЬКО на копии рабочей базы, не на production:
--   psql "$env:TEST_DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/verify_before_0007.sql
--
-- Скрипт не меняет данные и не выводит имена, вопросы, отзывы или другие
-- персональные тексты. Только схема, версии, количества и распределения.

\pset pager off
\pset footer off
\echo '=== 1. Сервер, размер, расширения ==='
select current_database() as database,
       current_user as db_user,
       version() as server_version,
       pg_size_pretty(pg_database_size(current_database())) as database_size;

select extname, extversion
from pg_extension
order by extname;

\echo '=== 2. Ревизия Alembic (если таблица есть) ==='
select to_regclass('public.alembic_version') is not null as has_alembic \gset
\if :has_alembic
select version_num from alembic_version limit 1;
\else
\echo 'alembic_version отсутствует'
\endif

\echo '=== 3. Ожидаемые таблицы ==='
select expected.table_name,
       (to_regclass('public.' || expected.table_name) is not null) as present
from (values
    ('users'), ('mentors'), ('mentor_subjects'), ('mentor_topics'),
    ('mentoring_sessions'), ('session_bookings'), ('session_feedback'),
    ('learning_materials'), ('mentor_roles'), ('terms'),
    ('mentor_weekly_reports'), ('mentor_final_reports'), ('tickets')
) expected(table_name)
order by expected.table_name;

\echo '=== 4. Колонки 0007: чего не хватает ==='
select expected.table_name, expected.column_name
from (values
    ('mentoring_sessions', 'author_id'),
    ('mentoring_sessions', 'format_confirmed'),
    ('learning_materials', 'cycle_stage'),
    ('learning_materials', 'review_status'),
    ('learning_materials', 'lecturer_name'),
    ('learning_materials', 'reviewed_at'),
    ('learning_materials', 'review_recorded_by')
) expected(table_name, column_name)
left join information_schema.columns c
       on c.table_schema = 'public'
      and c.table_name = expected.table_name
      and c.column_name = expected.column_name
where c.column_name is null
order by expected.table_name, expected.column_name;

\echo '=== 4b. Таблицы 0008: есть или ещё нет ==='
select expected.table_name,
       (to_regclass('public.' || expected.table_name) is not null) as present
from (values ('staff_members'), ('responsibility_assignments')) expected(table_name)
order by expected.table_name;

\echo '=== 5. Размеры ключевых таблиц ==='
select relname as table_name,
       n_live_tup as approx_rows,
       pg_size_pretty(pg_total_relation_size(relid)) as total_size
from pg_stat_user_tables
where relname in ('mentoring_sessions', 'session_bookings', 'session_feedback',
                  'learning_materials', 'users', 'mentors', 'tickets')
order by pg_total_relation_size(relid) desc;

\echo '=== 6. Количество строк ==='
select 'users' as entity, count(*) from users
union all select 'mentors', count(*) from mentors
union all select 'mentoring_sessions', count(*) from mentoring_sessions
union all select 'session_bookings', count(*) from session_bookings
union all select 'session_feedback', count(*) from session_feedback
union all select 'learning_materials', count(*) from learning_materials
union all select 'mentor_roles', count(*) from mentor_roles
union all select 'terms', count(*) from terms
order by entity;

\echo '=== 7. Форматы занятий и подтверждение ==='
select coalesce(format, '[NULL]') as format,
       case when format_confirmed is true then 'confirmed'
            when format_confirmed is false then 'false'
            else '[NULL]' end as confirmed,
       count(*) as sessions
from mentoring_sessions
group by 1, 2
order by 1, 2;

select count(*) filter (where format is null) as format_null,
       count(*) filter (where format_confirmed is null) as confirmed_null,
       count(*) filter (where format_confirmed is true) as confirmed_true,
       count(*) filter (where format_confirmed is false) as confirmed_false
from mentoring_sessions;

\echo '=== 8. Статусы занятий, явка, отзывы ==='
select status, count(*) from mentoring_sessions group by 1 order by 1;
select status, attendance, count(*) from session_bookings group by 1, 2 order by 1, 2;

select count(*) as sessions_without_feedback
from mentoring_sessions s
where s.status = 'completed'
  and not exists (select 1 from session_feedback f where f.session_id = s.id);

select count(*) as completed_without_approved_final_material
from mentoring_sessions s
where s.status = 'completed'
  and not exists (
      select 1 from learning_materials m
      where m.session_id = s.id
        and m.archived is false
        and m.cycle_stage = 'final'
        and m.review_status = 'approved'
  );

\echo '=== 9. Материалы: этапы и проверка ==='
select coalesce(cycle_stage, '[NULL]') as cycle_stage,
       coalesce(review_status, '[NULL]') as review_status,
       count(*) as materials
from learning_materials
group by 1, 2
order by 1, 2;

select count(*) filter (where author_id is null) as without_author,
       count(*) filter (where session_id is null) as without_session,
       count(*) filter (where review_recorded_by is null and review_status = 'approved') as approved_without_recorder
from learning_materials;

\echo '=== 10. Дубли и целостность связей ==='
select count(*) as duplicate_creation_keys
from (select creation_key from mentoring_sessions group by 1 having count(*) > 1) d;

select count(*) as duplicate_bookings
from (select session_id, student_id from session_bookings group by 1, 2 having count(*) > 1) d;

select count(*) as sessions_without_topic
from mentoring_sessions s
left join mentor_topics t on t.id = s.topic_id
where t.id is null;

select count(*) as sessions_without_mentor_user
from mentoring_sessions s
left join users u on u.telegram_id = s.mentor_id
where u.telegram_id is null;

select count(*) as sessions_without_author_user
from mentoring_sessions s
left join users u on u.telegram_id = s.author_id
where s.author_id is not null and u.telegram_id is null;

select count(*) as bookings_without_session
from session_bookings b
left join mentoring_sessions s on s.id = b.session_id
where s.id is null;

select count(*) as bookings_without_student
from session_bookings b
left join users u on u.telegram_id = b.student_id
where u.telegram_id is null;

select count(*) as materials_without_topic
from learning_materials m
left join mentor_topics t on t.id = m.topic_id
where t.id is null;

select count(*) as materials_without_session
from learning_materials m
left join mentoring_sessions s on s.id = m.session_id
where m.session_id is not null and s.id is null;

select count(*) as feedback_without_session
from session_feedback f
left join mentoring_sessions s on s.id = f.session_id
where s.id is null;

\echo '=== 11. Риски перед автоотменой и 1-на-1 ==='
select count(*) as unconfirmed_one_active_booking
from mentoring_sessions s
where s.status = 'scheduled'
  and s.format_confirmed is not true
  and (select count(*) from session_bookings b
       where b.session_id = s.id and b.status = 'active') = 1;

select count(*) as confirmed_one_on_one_with_many_active
from mentoring_sessions s
where s.status = 'scheduled'
  and s.format = 'one_on_one'
  and s.format_confirmed is true
  and (select count(*) from session_bookings b
       where b.session_id = s.id and b.status = 'active') > 1;

select count(*) as cancelled_with_active_bookings
from mentoring_sessions s
where s.status = 'cancelled'
  and exists (select 1 from session_bookings b
              where b.session_id = s.id and b.status = 'active');

\echo '=== 12. Ключевые ограничения ==='
select conname, conrelid::regclass as table_name, contype
from pg_constraint
where conname in (
    'uq_session_student', 'ck_session_status', 'ck_booking_status',
    'ck_attendance', 'ck_material_kind', 'ck_material_visibility',
    'uq_mentor_role', 'fk_session_author', 'fk_material_review_recorder'
)
order by conname;

\echo '=== Готово. Это только чтение; данные не изменялись. ==='
