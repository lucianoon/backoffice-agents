from backoffice_agents.adapters.telegram import parse_updates

RAW = [
    {"update_id": 1, "message": {"chat": {"id": 100}, "from": {"id": 7}, "text": "/status"}},
    {"update_id": 2, "message": {"chat": {"id": 999}, "from": {"id": 7}, "text": "/status"}},   # outro chat
    {"update_id": 3, "callback_query": {"id": "cb", "message": {"chat": {"id": 100}},
                                        "from": {"id": 8}, "data": "approve:1"}},
    {"update_id": 4, "callback_query": {"id": "cb2", "message": {"chat": {"id": 100}},
                                        "from": {"id": 9}, "data": "approve:1"}},              # intruso
    {"update_id": 5, "edited_message": {"chat": {"id": 100}}},                                  # ignorado
]


def test_only_authorized_chat_without_operator_list():
    updates = parse_updates(RAW, chat_id="100", operators=set())
    assert [u.update_id for u in updates] == [1, 3, 4]
    assert updates[1].user_id == "8" and updates[1].callback_data == "approve:1"


def test_operator_allowlist_filters_users():
    updates = parse_updates(RAW, chat_id="100", operators={"7", "8"})
    assert [u.update_id for u in updates] == [1, 3]


def test_bot_adapter_reads_operators_from_settings(settings):
    from backoffice_agents.adapters import build_adapters

    settings.telegram_adapter = "bot"
    settings.telegram_bot_token = "t"
    settings.telegram_chat_id = "100"
    settings.telegram_operators = "7, 8"
    assert build_adapters(settings).telegram._operators == {"7", "8"}
