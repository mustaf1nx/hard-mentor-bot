-- PostgreSQL. Run AFTER deploying the new bot version.
-- Replace the examples below. Only registered users with a unique cached
-- username are added. @ prefix / letter case / duplicate inputs are accepted.
-- This does not change users.role and does not send Telegram notifications.
WITH requested_raw(username) AS (
    VALUES
        ('@mentor_one'),
        ('@mentor_two'),
        ('mentor_three')
), requested AS (
    SELECT DISTINCT lower(ltrim(trim(username), '@')) AS username
    FROM requested_raw
), matched AS (
    SELECT r.username,
           count(u.telegram_id) AS match_count,
           min(u.telegram_id) AS telegram_id
    FROM requested AS r
    LEFT JOIN users AS u ON lower(u.username) = r.username
    GROUP BY r.username
), added AS (
    INSERT INTO mentors (telegram_id)
    SELECT telegram_id
    FROM matched
    WHERE match_count = 1
    ON CONFLICT (telegram_id) DO NOTHING
    RETURNING telegram_id
)
SELECT m.username,
       CASE WHEN m.match_count = 1 THEN m.telegram_id END AS telegram_id,
       CASE
           WHEN m.match_count = 0 THEN 'not_found'
           WHEN m.match_count > 1 THEN 'ambiguous_use_telegram_id'
           WHEN a.telegram_id IS NOT NULL THEN 'added'
           ELSE 'already_mentor'
       END AS result
FROM matched AS m
LEFT JOIN added AS a ON a.telegram_id = m.telegram_id
ORDER BY m.username;
