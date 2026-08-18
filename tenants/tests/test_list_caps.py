"""Guards against silent truncation on switcher / picker connections."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_tenant_switcher_max_limit_is_500():
    text = (ROOT / "tenants" / "schema.py").read_text()
    assert text.count("max_limit=500") >= 3
    for chunk in text.split("async def tenants")[1:]:
        assert "max_limit=500" in chunk


def test_chat_messages_cap_is_500():
    text = (ROOT / "chats" / "types.py").read_text()
    assert "min(max(first, 1), 500)" in text


def test_products_picker_max_limit_is_1000():
    text = (ROOT / "events" / "queries.py").read_text()
    assert "max_limit=1000" in text


def test_recap_event_options_max_limit_is_500():
    text = (ROOT / "recaps" / "queries.py").read_text()
    assert text.count("max_limit=500") >= 2
