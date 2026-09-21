from backoffice_agents.adapters.email import extract_fetch_body, parse_uid_search


def test_parse_uid_search_takes_the_most_recent_up_to_limit():
    assert parse_uid_search(b"10 11 12 13 14", limit=3) == [b"12", b"13", b"14"]
    assert parse_uid_search(b"", limit=5) == []
    assert parse_uid_search(None, limit=5) == []


def test_extract_fetch_body_reads_the_payload_tuple():
    parts = [(b"1 (UID 42 BODY[] {4}", b"hola"), b")"]
    assert extract_fetch_body(parts) == b"hola"
