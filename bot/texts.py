"""Все тексты бота: русский и английский (раздел 10 спека).

Казахский добавляется третьим ключом "kk" в каждую запись + в LANGS.
"""
from __future__ import annotations

LANGS = ("ru", "en")

CATEGORY_EMOJI = {
    "question": "❓", "complaint": "😠", "suggestion": "💡", "praise": "👍", "serious": "⚠️",
}

CATEGORY_NAMES = {
    "question": {"ru": "Вопрос", "en": "Question"},
    "complaint": {"ru": "Жалоба", "en": "Complaint"},
    "suggestion": {"ru": "Предложение", "en": "Suggestion"},
    "praise": {"ru": "Благодарность", "en": "Praise"},
    "serious": {"ru": "Серьёзное сообщение", "en": "Serious report"},
}

CATEGORY_BUTTONS = {
    "question": {"ru": "❓ Вопрос", "en": "❓ Question"},
    "complaint": {"ru": "😠 Жалоба", "en": "😠 Complaint"},
    "suggestion": {"ru": "💡 Предложение", "en": "💡 Suggestion"},
    "praise": {"ru": "👍 Благодарность", "en": "👍 Praise"},
    "serious": {"ru": "⚠️ Сообщить о серьёзном", "en": "⚠️ Report something serious"},
}

STATUS_NAMES = {
    "new": {"ru": "Новое", "en": "New"},
    "in_review": {"ru": "На рассмотрении", "en": "In review"},
    "in_progress": {"ru": "В работе", "en": "In progress"},
    "resolved": {"ru": "Решено", "en": "Resolved"},
    "closed": {"ru": "Закрыто", "en": "Closed"},
}

MENU = {
    "feedback": {"ru": "✍️ Оставить обращение", "en": "✍️ Send feedback"},
    "status": {"ru": "📋 Мои обращения", "en": "📋 My tickets"},
    "help": {"ru": "ℹ️ Помощь", "en": "ℹ️ Help"},
    "queue": {"ru": "📥 Очередь", "en": "📥 Queue"},
}


def menu_labels(key: str) -> set[str]:
    return set(MENU[key].values())


T: dict[str, dict[str, str]] = {
    # ---------- общее ----------
    "start": {
        "ru": (
            "Привет! Это бот обратной связи программы <b>Hard Mentor</b>.\n\n"
            "Здесь можно задать вопрос о программе, пожаловаться, предложить идею, "
            "поблагодарить ментора или сообщить о серьёзной проблеме. Каждое обращение "
            "получает номер и попадает к тому, кто за него отвечает.\n\n"
            "Это <b>не</b> бот записи на менторские сессии — заявки на помощь по предметам "
            "идут через entry-ticket.\n\n"
            "Нажмите «✍️ Оставить обращение» или отправьте /feedback."
        ),
        "en": (
            "Hi! This is the <b>Hard Mentor</b> feedback bot.\n\n"
            "Use it to ask a question about the program, raise a complaint, suggest an idea, "
            "thank a mentor, or report something serious. Every submission gets a ticket ID "
            "and goes to the person responsible for it.\n\n"
            "This is <b>not</b> the bot for booking mentoring sessions — academic help requests "
            "go through the entry-ticket system.\n\n"
            "Tap “✍️ Send feedback” or send /feedback."
        ),
    },
    "help": {
        "ru": (
            "<b>Категории</b>\n"
            "❓ Вопрос — о программе и правилах. Ответ в течение 24 часов.\n"
            "😠 Жалоба — сорванная сессия, неготовый ментор, расписание. Ответ в течение 48 часов.\n"
            "💡 Предложение — подтвердим за 48 часов, разберём на ближайшей встрече волны.\n"
            "👍 Благодарность — передадим ментору.\n"
            "⚠️ Серьёзное сообщение — харассмент, безопасность, давление с нарушением академической "
            "честности. Координатор и руководитель программы получают уведомление сразу, "
            "первый ответ человека — в течение 4 часов.\n\n"
            "<b>Анонимность</b>\n"
            "Выбирается для каждого обращения отдельно. Если обращение анонимное, бот "
            "<b>не сохраняет</b> ваш Telegram ID рядом с ним — раскрыть автора потом невозможно, "
            "даже для серьёзных сообщений. Уведомления по такому обращению не приходят: "
            "статус и ответы можно смотреть по секретному коду, который бот выдаст при отправке.\n\n"
            "<b>Команды</b>\n"
            "/feedback — новое обращение\n"
            "/status — мои обращения\n"
            "/status HM-0001 КОД — анонимное обращение по коду\n"
            "/cancel — отменить заполнение\n"
            "/lang — сменить язык (RU/EN)\n\n"
            "Лимит — {limit} обращений в день."
        ),
        "en": (
            "<b>Categories</b>\n"
            "❓ Question — about the program and its rules. Reply within 24 hours.\n"
            "😠 Complaint — a missed session, an unprepared mentor, scheduling. Reply within 48 hours.\n"
            "💡 Suggestion — acknowledged within 48 hours, reviewed at the next wave meeting.\n"
            "👍 Praise — forwarded to the mentor.\n"
            "⚠️ Serious report — harassment, safety, academic-integrity pressure. The coordinator "
            "and the program lead are notified immediately; first human response within 4 hours.\n\n"
            "<b>Anonymity</b>\n"
            "Chosen per submission. When a ticket is anonymous the bot <b>does not store</b> your "
            "Telegram ID with it — the author cannot be unmasked later, even for serious reports. "
            "You won't get notifications for such a ticket: check its status and replies with "
            "the secret code the bot gives you on submission.\n\n"
            "<b>Commands</b>\n"
            "/feedback — new submission\n"
            "/status — my tickets\n"
            "/status HM-0001 CODE — anonymous ticket by code\n"
            "/cancel — abandon the current submission\n"
            "/lang — switch language (RU/EN)\n\n"
            "Limit: {limit} submissions per day."
        ),
    },
    "help_staff": {
        "ru": (
            "\n\n<b>Для команды программы</b>\n"
            "/queue — очередь обращений (фильтры по категории и статусу)\n"
            "/ticket HM-0001 — открыть обращение\n"
            "/report — сводка за неделю\n"
            "/mentors — список менторов\n"
            "/addmentor ID|@username — добавить ментора (он должен нажать /start)\n"
            "/delmentor ID|@username — убрать ментора\n"
            "/myid — мой Telegram ID"
        ),
        "en": (
            "\n\n<b>For the program team</b>\n"
            "/queue — ticket queue (filter by category and status)\n"
            "/ticket HM-0001 — open a ticket\n"
            "/report — weekly summary\n"
            "/mentors — list mentors\n"
            "/addmentor ID|@username — add a mentor (they must press /start first)\n"
            "/delmentor ID|@username — remove a mentor\n"
            "/myid — my Telegram ID"
        ),
    },
    "lang_set": {"ru": "Язык: русский 🇷🇺", "en": "Language: English 🇬🇧"},
    "myid": {"ru": "Ваш Telegram ID: <code>{id}</code>", "en": "Your Telegram ID: <code>{id}</code>"},
    "cancelled": {"ru": "Отменено. Ничего не сохранено.", "en": "Cancelled. Nothing was saved."},
    "nothing_to_cancel": {"ru": "Сейчас нечего отменять.", "en": "Nothing to cancel right now."},
    "fallback": {
        "ru": "Чтобы оставить обращение, нажмите «✍️ Оставить обращение» или отправьте /feedback.",
        "en": "To send feedback, tap “✍️ Send feedback” or send /feedback.",
    },
    "expired": {
        "ru": "Эта кнопка устарела. Начните заново: /feedback",
        "en": "This button has expired. Start again: /feedback",
    },
    "private_only": {
        "ru": "Я работаю только в личных сообщениях.",
        "en": "I only work in private chats.",
    },
    # ---------- подача обращения ----------
    "rate_limited": {
        "ru": "Сегодня вы уже отправили {limit} обращений — это дневной лимит. Попробуйте завтра.",
        "en": "You have already sent {limit} submissions today — that's the daily limit. Try again tomorrow.",
    },
    "choose_category": {"ru": "Чем хотите поделиться?", "en": "What would you like to share?"},
    "ask_related": {
        "ru": "К какому курсу или ментору это относится? Напишите текстом или выберите ментора. "
              "Необязательно — можно пропустить.",
        "en": "Which course or mentor is this about? Type it or pick a mentor. "
              "Optional — tap Skip if not relevant.",
    },
    "serious_intro": {
        "ru": (
            "⚠️ <b>Серьёзное сообщение</b>\n\n"
            "Что будет дальше: сразу после отправки уведомление получат координатор программы и "
            "руководитель программы — без очереди. Первый ответ человека — в течение 4 часов.\n\n"
            "Если вам угрожает опасность прямо сейчас — звоните <b>112</b>, бот этого не заменит.\n\n"
            "Касается ли сообщение самого координатора?"
        ),
        "en": (
            "⚠️ <b>Serious report</b>\n\n"
            "What happens next: as soon as you submit, the program coordinator and the program lead "
            "are notified immediately — no queue. First human response within 4 hours.\n\n"
            "If you are in immediate danger, call <b>112</b> — the bot is not a substitute.\n\n"
            "Does this report concern the coordinator?"
        ),
    },
    "serious_intro_nolead": {
        "ru": (
            "⚠️ <b>Серьёзное сообщение</b>\n\n"
            "Что будет дальше: сразу после отправки уведомление получит координатор программы — "
            "без очереди. Первый ответ человека — в течение 4 часов.\n\n"
            "Если вам угрожает опасность прямо сейчас — звоните <b>112</b>, бот этого не заменит."
        ),
        "en": (
            "⚠️ <b>Serious report</b>\n\n"
            "What happens next: as soon as you submit, the program coordinator is notified "
            "immediately — no queue. First human response within 4 hours.\n\n"
            "If you are in immediate danger, call <b>112</b> — the bot is not a substitute."
        ),
    },
    "btn_skip": {"ru": "Пропустить", "en": "Skip"},
    "btn_about_coord": {"ru": "👤 Это про координатора", "en": "👤 It's about the coordinator"},
    "btn_coord_yes": {
        "ru": "Да — отправить только руководителю", "en": "Yes — send to the program lead only",
    },
    "btn_coord_no": {"ru": "Нет", "en": "No"},
    "lead_only_note": {
        "ru": "Понял. Это обращение увидит <b>только руководитель программы</b> — координатор его не получит.",
        "en": "Understood. Only the <b>program lead</b> will see this ticket — the coordinator will not receive it.",
    },
    "ask_text": {
        "ru": "Опишите своими словами, что произошло или что вы хотите сказать.",
        "en": "Go ahead and describe what happened, in your own words.",
    },
    "text_needed": {
        "ru": "Нужен текст. Опишите ситуацию сообщением — файл можно будет приложить следующим шагом.",
        "en": "I need text here. Describe it in a message — you can attach a file in the next step.",
    },
    "text_too_short": {
        "ru": "Слишком коротко — нужно хотя бы {min} символов, чтобы было понятно, о чём речь.",
        "en": "Too short — please write at least {min} characters so it's clear what this is about.",
    },
    "text_too_long": {
        "ru": "Слишком длинно ({n} символов). Уложитесь, пожалуйста, в {max}.",
        "en": "Too long ({n} characters). Please keep it under {max}.",
    },
    "text_is_command": {
        "ru": "Сейчас я жду текст обращения. Чтобы выйти, отправьте /cancel.",
        "en": "I'm waiting for your description. Send /cancel to quit.",
    },
    "ask_attachment": {
        "ru": "Хотите приложить скриншот или файл? Отправьте его сюда одним сообщением или нажмите «Пропустить».",
        "en": "Want to attach a screenshot or file? Send it here as one message, or tap Skip.",
    },
    "attachment_needed": {
        "ru": "Пришлите фото или файл — либо нажмите «Пропустить».",
        "en": "Send a photo or a file — or tap Skip.",
    },
    "ask_anon": {
        "ru": (
            "Отправить с вашим именем или анонимно?\n\n"
            "<b>Анонимно</b> значит, что бот не сохранит ваш Telegram ID рядом с обращением. "
            "Раскрыть автора потом невозможно — даже чтобы ответить вам. Уведомления не придут, "
            "но статус и ответы можно будет посмотреть по секретному коду.\n"
            "Если хотите, чтобы с вами связались, можно самому оставить контакт в тексте."
        ),
        "en": (
            "Submit with your name, or anonymously?\n\n"
            "<b>Anonymous</b> means the bot will not store your Telegram ID with the ticket. "
            "The author cannot be identified later — not even to follow up. You won't get "
            "notifications, but you can check status and replies with a secret code.\n"
            "If you want to be contacted, you may leave a contact yourself in the text."
        ),
    },
    "btn_named": {"ru": "С моим именем", "en": "With my name"},
    "btn_anon": {"ru": "Анонимно", "en": "Anonymous"},
    "submitted": {
        "ru": "Принято — обращение <b>{code}</b>. {sla}\nСтатус можно проверить в любой момент: /status",
        "en": "Got it — logged as ticket <b>{code}</b>. {sla}\nYou can check progress anytime with /status.",
    },
    "submitted_anon": {
        "ru": (
            "Принято — обращение <b>{code}</b>. {sla}\n\n"
            "Оно анонимное, поэтому уведомлений не будет. Сохраните секретный код — "
            "восстановить его нельзя:\n<code>/status {code} {secret}</code>\n"
            "Отправьте эту команду, чтобы увидеть статус и ответы."
        ),
        "en": (
            "Got it — logged as ticket <b>{code}</b>. {sla}\n\n"
            "It's anonymous, so you won't get notifications. Save this secret code — "
            "it cannot be recovered:\n<code>/status {code} {secret}</code>\n"
            "Send this command to see status and replies."
        ),
    },
    "sla_question": {
        "ru": "Координатор ответит в течение 24 часов.", "en": "A coordinator will reply within 24 hours.",
    },
    "sla_complaint": {
        "ru": "Координатор посмотрит его в течение 48 часов.",
        "en": "A coordinator will look at this within 48 hours.",
    },
    "sla_suggestion": {
        "ru": "Подтвердим в течение 48 часов и разберём на ближайшей встрече волны.",
        "en": "We'll acknowledge it within 48 hours and review it at the next wave meeting.",
    },
    "sla_praise": {"ru": "Передадим ментору — спасибо!", "en": "We'll pass it on to the mentor — thank you!"},
    "sla_serious": {
        "ru": "Координатор и руководитель программы уже уведомлены. Первый ответ — в течение 4 часов.",
        "en": "The coordinator and the program lead have been notified. First response within 4 hours.",
    },
    "sla_lead_only": {
        "ru": "Его увидит только руководитель программы.", "en": "Only the program lead will see it.",
    },
    # ---------- статус для заявителя ----------
    "no_tickets": {
        "ru": "У вас пока нет именных обращений.\nАнонимное можно проверить так: <code>/status HM-0001 КОД</code>",
        "en": "You have no named tickets yet.\nCheck an anonymous one with: <code>/status HM-0001 CODE</code>",
    },
    "my_tickets": {"ru": "<b>Ваши обращения</b>", "en": "<b>Your tickets</b>"},
    "status_usage": {
        "ru": "Формат: <code>/status HM-0001 КОД</code>", "en": "Usage: <code>/status HM-0001 CODE</code>",
    },
    "ticket_not_found": {
        "ru": "Обращение не найдено или код неверный.", "en": "Ticket not found or the code is wrong.",
    },
    "too_many_attempts": {
        "ru": "Слишком много неверных попыток. Попробуйте завтра.",
        "en": "Too many wrong attempts. Try again tomorrow.",
    },
    "replies_header": {"ru": "<b>Переписка</b>", "en": "<b>Conversation</b>"},
    "no_replies": {"ru": "Ответов пока нет.", "en": "No replies yet."},
    "you": {"ru": "Вы", "en": "You"},
    "team": {"ru": "Команда программы", "en": "Program team"},
    "btn_add_comment": {"ru": "✏️ Дополнить", "en": "✏️ Add a comment"},
    "ask_comment": {
        "ru": "Напишите дополнение к обращению {code} одним сообщением. /cancel — отмена.",
        "en": "Write your comment for ticket {code} in one message. /cancel to abort.",
    },
    "comment_saved": {"ru": "Дополнение к {code} передано.", "en": "Your comment on {code} has been passed on."},
    "status_changed": {
        "ru": "Обращение <b>{code}</b>: статус изменён → <b>{status}</b>.",
        "en": "Ticket <b>{code}</b>: status changed → <b>{status}</b>.",
    },
    "reply_received": {
        "ru": "Ответ по обращению <b>{code}</b>:\n\n{text}",
        "en": "Reply on ticket <b>{code}</b>:\n\n{text}",
    },
    "ask_rating": {
        "ru": "Обращение <b>{code}</b> решено. Хорошо ли с ним разобрались? Оцените от 1 до 5 (необязательно).",
        "en": "Ticket <b>{code}</b> is resolved. Was this handled well? 1–5 (optional).",
    },
    "rating_thanks": {"ru": "Спасибо за оценку!", "en": "Thanks for the rating!"},
    "rating_done": {"ru": "Оценка уже стоит.", "en": "Already rated."},
    # ---------- интерфейс команды ----------
    "staff_only": {"ru": "Команда доступна только команде программы.", "en": "This command is for the program team only."},
    "admin_only": {
        "ru": "Команда доступна координатору и руководителю программы.",
        "en": "This command is for the coordinator and the program lead.",
    },
    "queue_title": {"ru": "📥 <b>Очередь</b> · {cat} · {status} · всего {total}", "en": "📥 <b>Queue</b> · {cat} · {status} · {total} total"},
    "queue_empty": {"ru": "Пусто. 🎉", "en": "Nothing here. 🎉"},
    "filter_all": {"ru": "Все", "en": "All"},
    "filter_open": {"ru": "Открытые", "en": "Open"},
    "btn_refresh": {"ru": "🔄 Обновить", "en": "🔄 Refresh"},
    "btn_back_queue": {"ru": "⬅️ К очереди", "en": "⬅️ Back to queue"},
    "btn_reply": {"ru": "💬 Ответить", "en": "💬 Reply"},
    "btn_note": {"ru": "📝 Заметка", "en": "📝 Internal note"},
    "btn_assign": {"ru": "👤 Назначить", "en": "👤 Assign"},
    "btn_attachment": {"ru": "📎 Вложение", "en": "📎 Attachment"},
    "btn_open": {"ru": "Открыть {code}", "en": "Open {code}"},
    "btn_unassign": {"ru": "Снять назначение", "en": "Unassign"},
    "btn_back": {"ru": "⬅️ Назад", "en": "⬅️ Back"},
    "no_access": {"ru": "Нет доступа к этому обращению.", "en": "You don't have access to this ticket."},
    "ticket_usage": {"ru": "Формат: <code>/ticket HM-0001</code>", "en": "Usage: <code>/ticket HM-0001</code>"},
    "card_status": {"ru": "Статус", "en": "Status"},
    "card_age": {"ru": "Возраст", "en": "Age"},
    "card_from": {"ru": "От", "en": "From"},
    "card_anonymous": {"ru": "анонимно", "en": "anonymous"},
    "card_related": {"ru": "Курс/тема", "en": "Course/topic"},
    "card_mentor": {"ru": "Ментор", "en": "Mentor"},
    "card_assigned": {"ru": "Назначено", "en": "Assigned to"},
    "card_flagged": {"ru": "🚩 Отмечено фильтром", "en": "🚩 Flagged by filter"},
    "card_has_attachment": {"ru": "📎 Есть вложение", "en": "📎 Has attachment"},
    "card_lead_only": {"ru": "🔒 Только для руководителя программы", "en": "🔒 Program lead only"},
    "card_urgent": {"ru": "🔴 СРОЧНО", "en": "🔴 URGENT"},
    "card_overdue": {"ru": "⏰ SLA просрочен", "en": "⏰ SLA breached"},
    "card_rating": {"ru": "Оценка", "en": "Rating"},
    "card_notes": {"ru": "— История —", "en": "— History —"},
    "note_reply": {"ru": "ответ", "en": "reply"},
    "note_internal": {"ru": "заметка", "en": "internal"},
    "note_submitter": {"ru": "заявитель", "en": "submitter"},
    "ask_reply": {
        "ru": "Напишите ответ по {code} — заявитель его увидит. /cancel — отмена.",
        "en": "Write your reply for {code} — the submitter will see it. /cancel to abort.",
    },
    "ask_reply_anon": {
        "ru": "Напишите ответ по {code}. Обращение анонимное: уведомление не придёт, "
              "заявитель увидит ответ, только когда сам проверит статус по коду. /cancel — отмена.",
        "en": "Write your reply for {code}. The ticket is anonymous: no notification will be sent, "
              "the submitter sees the reply only when they check the status with their code. /cancel to abort.",
    },
    "ask_note": {
        "ru": "Напишите внутреннюю заметку по {code} — заявитель её не увидит. /cancel — отмена.",
        "en": "Write an internal note for {code} — the submitter will not see it. /cancel to abort.",
    },
    "reply_saved": {"ru": "Ответ по {code} сохранён.", "en": "Reply on {code} saved."},
    "reply_saved_delivered": {"ru": "Ответ по {code} отправлен заявителю.", "en": "Reply on {code} delivered to the submitter."},
    "reply_not_delivered": {
        "ru": "Ответ по {code} сохранён, но доставить уведомление не удалось (заявитель заблокировал бота?).",
        "en": "Reply on {code} saved, but the notification could not be delivered (bot blocked?).",
    },
    "note_saved": {"ru": "Заметка по {code} сохранена.", "en": "Note on {code} saved."},
    "choose_assignee": {"ru": "Кому назначить {code}?", "en": "Assign {code} to whom?"},
    "assigned_to_you": {"ru": "Вам назначено обращение:", "en": "A ticket has been assigned to you:"},
    "no_attachment": {"ru": "Вложения нет.", "en": "No attachment."},
    "new_ticket": {"ru": "🆕 Новое обращение", "en": "🆕 New ticket"},
    "new_ticket_urgent": {
        "ru": "🚨🚨 <b>СРОЧНО: серьёзное сообщение</b> — первый ответ нужен в течение 4 часов",
        "en": "🚨🚨 <b>URGENT: serious report</b> — first response due within 4 hours",
    },
    "new_ticket_lead_only": {
        "ru": "🔒 <b>Обращение касается координатора</b> — видите его только вы",
        "en": "🔒 <b>This ticket concerns the coordinator</b> — only you can see it",
    },
    "praise_for_you": {"ru": "👍 <b>Вам передали благодарность</b>", "en": "👍 <b>Someone sent you praise</b>"},
    "submitter_comment": {
        "ru": "✏️ Заявитель дополнил обращение <b>{code}</b>:\n\n{text}",
        "en": "✏️ The submitter added a comment to <b>{code}</b>:\n\n{text}",
    },
    "sla_alert": {
        "ru": "⏰ <b>SLA просрочен</b>: по обращению {code} ({cat}) нет первого ответа уже {age}.",
        "en": "⏰ <b>SLA breached</b>: ticket {code} ({cat}) has had no first response for {age}.",
    },
    "mentors_title": {"ru": "<b>Менторы</b>", "en": "<b>Mentors</b>"},
    "mentors_empty": {"ru": "Менторов пока нет. Добавьте: /addmentor ID|@username", "en": "No mentors yet. Add one: /addmentor ID|@username"},
    "mentor_usage": {"ru": "Формат: <code>/{cmd} ID</code> или <code>/{cmd} @username</code>", "en": "Usage: <code>/{cmd} ID</code> or <code>/{cmd} @username</code>"},
    "user_unknown": {
        "ru": "Не знаю такого пользователя. Попросите его сначала нажать /start в этом боте.",
        "en": "I don't know this user. Ask them to press /start in this bot first.",
    },
    "mentor_added": {"ru": "{name} теперь ментор.", "en": "{name} is now a mentor."},
    "mentor_removed": {"ru": "{name} больше не ментор.", "en": "{name} is no longer a mentor."},
    "you_are_mentor": {
        "ru": "Вас добавили как ментора. Назначенные вам обращения — в /queue.",
        "en": "You've been added as a mentor. Tickets assigned to you are in /queue.",
    },
    # ---------- отчёт ----------
    "report_title": {"ru": "📊 <b>Сводка за неделю</b> ({start} — {end})", "en": "📊 <b>Weekly summary</b> ({start} — {end})"},
    "report_scope_coord": {
        "ru": "<i>Без обращений, адресованных только руководителю.</i>",
        "en": "<i>Excludes tickets addressed to the program lead only.</i>",
    },
    "report_volume": {"ru": "<b>Обращения</b> (эта неделя / прошлая)", "en": "<b>Volume</b> (this week / previous)"},
    "report_total": {"ru": "Всего", "en": "Total"},
    "report_median": {"ru": "<b>Медиана времени до первого ответа</b>", "en": "<b>Median time to first response</b>"},
    "report_within_sla": {"ru": "Первый ответ в пределах SLA", "en": "First response within SLA"},
    "report_overdue": {"ru": "<b>Открытые с просроченным SLA</b>", "en": "<b>Open tickets past SLA</b>"},
    "report_anon": {"ru": "Анонимных", "en": "Anonymous share"},
    "report_rating": {"ru": "Средняя оценка", "en": "Average rating"},
    "report_none": {"ru": "нет", "en": "none"},
    "report_nodata": {"ru": "нет данных", "en": "no data"},
}


def t(lang: str, key: str, **kwargs) -> str:
    entry = T[key]
    text = entry.get(lang) or entry["ru"]
    return text.format(**kwargs) if kwargs else text


def cat_name(lang: str, category: str, emoji: bool = True) -> str:
    name = CATEGORY_NAMES[category].get(lang) or CATEGORY_NAMES[category]["ru"]
    return f"{CATEGORY_EMOJI[category]} {name}" if emoji else name


def status_name(lang: str, status: str) -> str:
    return STATUS_NAMES[status].get(lang) or STATUS_NAMES[status]["ru"]


def detect_lang(language_code: str | None) -> str:
    code = (language_code or "").lower()
    if code.startswith("en"):
        return "en"
    return "ru"  # ru, kk и всё остальное — русский по умолчанию (AITU)


def fmt_age(lang: str, seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if lang == "en":
        d, h, m = "d", "h", "m"
    else:
        d, h, m = "д", "ч", "м"
    if days:
        return f"{days}{d} {hours}{h}"
    if hours:
        return f"{hours}{h} {minutes}{m}"
    return f"{minutes}{m}"
