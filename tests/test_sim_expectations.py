from tools import sim


def test_expect_does_not_match_earlier_response(monkeypatch):
    messages = [{"text": "старый ответ"}, {"text": "новый ответ"}]
    monkeypatch.setattr(sim, "_outbox", lambda uid, since=0: messages[since:])
    monkeypatch.setattr(sim, "wait_for", lambda check, timeout: check())
    monkeypatch.setattr(sim, "dump", lambda uid: None)

    assert not sim.check_expectations({"expect": "старый ответ"}, 1, 1, 1)
    assert sim.check_expectations({"expect": "новый ответ"}, 1, 1, 2)


def test_selected_button_label_is_taken_from_the_correct_callback(monkeypatch):
    messages = [{"markup": {"inline_keyboard": [[
        {"text": "Открыть занятие", "callback_data": "lesson:1"},
        {"text": "Отмена", "callback_data": "cancel:1"},
    ]]}}]
    monkeypatch.setattr(sim, "_outbox", lambda uid, since=0: messages[since:])
    assert sim.button_label(1, "lesson:1") == "Открыть занятие"
    assert sim.button_label(1, "missing") is None


def test_absent_button_assertion_only_checks_current_reply(monkeypatch):
    messages = [{"markup": {"inline_keyboard": [[{"text": "Фото", "callback_data": "old"}]]}},
                {"markup": {"inline_keyboard": [[{"text": "Назад", "callback_data": "back"}]]}}]
    monkeypatch.setattr(sim, "_outbox", lambda uid, since=0: messages[since:])
    monkeypatch.setattr(sim.time, "sleep", lambda _: None)
    assert sim.check_expectations({"expect_no_button": "Фото"}, 1, 1, 1)
    assert not sim.check_expectations({"expect_no_button": "Фото"}, 1, 0, 2)
