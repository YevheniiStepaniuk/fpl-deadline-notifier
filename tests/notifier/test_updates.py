import httpx
import pytest
import respx

from notifier.telegram import api_url
from notifier.updates import (
    ChatAdded,
    WebhookConflictError,
    fetch_added,
    parse_added,
)

TOKEN = "123456:AAtoken"
UPDATES_URL = api_url(TOKEN, "getUpdates")


def _my_chat_member(update_id, chat_id, chat_type, old_status, new_status, title=None):
    chat = {"id": chat_id, "type": chat_type}
    if title is not None:
        chat["title"] = title
    return {
        "update_id": update_id,
        "my_chat_member": {
            "chat": chat,
            "date": 1735689600,
            "from": {"id": 1, "is_bot": False, "first_name": "Someone"},
            "old_chat_member": {"user": {"id": 999, "is_bot": True}, "status": old_status},
            "new_chat_member": {"user": {"id": 999, "is_bot": True}, "status": new_status},
        },
    }


def test_being_added_to_a_channel_is_an_add():
    payload = {"ok": True, "result": [
        _my_chat_member(1, -100123, "channel", "left", "administrator", title="News"),
    ]}
    added, highest = parse_added(payload)
    assert added == [ChatAdded(chat_id=-100123, title="News", chat_type="channel")]
    assert highest == 1


def test_being_added_to_a_group_is_an_add():
    payload = {"ok": True, "result": [
        _my_chat_member(1, -200456, "group", "left", "member", title="Mini League"),
    ]}
    added, _ = parse_added(payload)
    assert added == [ChatAdded(chat_id=-200456, title="Mini League", chat_type="group")]


def test_a_kicked_bot_being_re_added_still_counts():
    """`kicked` is as absent as `left`. Telegram uses it when the bot was banned rather
    than removed normally, and the re-add case has to work the same either way."""
    payload = {"ok": True, "result": [
        _my_chat_member(1, -300789, "supergroup", "kicked", "member"),
    ]}
    added, _ = parse_added(payload)
    assert [c.chat_id for c in added] == [-300789]


def test_a_promotion_is_not_an_add():
    """member -> administrator is a my_chat_member update too, but the bot was already
    present, so this must not read as a fresh join."""
    payload = {"ok": True, "result": [
        _my_chat_member(1, -100123, "channel", "member", "administrator"),
    ]}
    added, highest = parse_added(payload)
    assert added == []
    assert highest == 1  # still acknowledged, just not treated as an add


def test_a_demotion_is_not_an_add():
    payload = {"ok": True, "result": [
        _my_chat_member(1, -100123, "channel", "administrator", "member"),
    ]}
    added, _ = parse_added(payload)
    assert added == []


def test_the_bot_being_removed_is_not_an_add():
    payload = {"ok": True, "result": [
        _my_chat_member(1, -100123, "channel", "member", "left"),
    ]}
    added, _ = parse_added(payload)
    assert added == []


def test_an_empty_result_yields_no_adds_and_no_offset():
    added, highest = parse_added({"ok": True, "result": []})
    assert added == []
    assert highest is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"ok": True},
        {"ok": True, "result": "not a list"},
        {"ok": True, "result": [None]},
        {"ok": True, "result": ["not a dict"]},
        {"ok": True, "result": [{"update_id": 1}]},
        {"ok": True, "result": [{"update_id": 1, "my_chat_member": "not a dict"}]},
        {"ok": True, "result": [{"update_id": 1, "my_chat_member": {}}]},
        {"ok": True, "result": [{
            "update_id": 1,
            "my_chat_member": {
                "chat": {"id": -1, "type": "channel"},
                "old_chat_member": {"status": "left"},
                "new_chat_member": "not a dict",
            },
        }]},
        {"ok": True, "result": [{
            "update_id": 1,
            "my_chat_member": {
                "chat": {"type": "channel"},  # missing id
                "old_chat_member": {"status": "left"},
                "new_chat_member": {"status": "member"},
            },
        }]},
    ],
)
def test_a_malformed_update_is_skipped_rather_than_raising(payload):
    """Unlike sources.parse_fpl, an empty or bad result here genuinely means 'nothing
    happened' -- there is no cache for a misread emptiness to corrupt, so skipping
    rather than raising is correct rather than a compromise."""
    added, _ = parse_added(payload)  # must not raise
    assert added == []


def test_a_malformed_update_still_advances_past_its_update_id():
    """A bad entry must still be acknowledged on the next offset, or it would be
    redelivered forever and block every update queued after it."""
    payload = {"ok": True, "result": [{"update_id": 7, "my_chat_member": "garbage"}]}
    _, highest = parse_added(payload)
    assert highest == 7


def test_the_highest_update_id_wins_regardless_of_order():
    payload = {"ok": True, "result": [
        _my_chat_member(5, -1, "channel", "left", "member"),
        _my_chat_member(3, -2, "channel", "left", "member"),
    ]}
    _, highest = parse_added(payload)
    assert highest == 5


def test_a_chat_with_no_title_reports_none():
    payload = {"ok": True, "result": [
        _my_chat_member(1, -1, "group", "left", "member"),
    ]}
    added, _ = parse_added(payload)
    assert added[0].title is None


@respx.mock
def test_fetch_added_requests_only_my_chat_member_updates():
    """allowed_updates limits the backlog Telegram queues to the one type this service
    ever looks at; without it, ordinary posts would accumulate for nothing."""
    route = respx.get(UPDATES_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )
    with httpx.Client() as client:
        fetch_added(client, TOKEN, None)
    request = route.calls[0].request
    assert "my_chat_member" in str(request.url)
    assert "timeout=0" in str(request.url)
    assert "offset" not in str(request.url)


@respx.mock
def test_fetch_added_omits_offset_on_the_first_call_and_sends_it_on_the_next():
    """Telegram expects the next call's offset to be highest_update_id + 1 -- that is
    what confirms and clears the previous batch. Off by one here either replays every
    update forever or silently drops one."""
    first = {"ok": True, "result": [
        _my_chat_member(10, -1, "channel", "left", "member"),
    ]}
    second = {"ok": True, "result": [
        _my_chat_member(11, -2, "channel", "left", "member"),
    ]}
    route = respx.get(UPDATES_URL).mock(side_effect=[
        httpx.Response(200, json=first),
        httpx.Response(200, json=second),
    ])
    with httpx.Client() as client:
        added1, offset1 = fetch_added(client, TOKEN, None)
        assert offset1 == 10
        added2, offset2 = fetch_added(client, TOKEN, offset1 + 1)
        assert offset2 == 11
    assert "offset" not in str(route.calls[0].request.url)
    assert "offset=11" in str(route.calls[1].request.url)
    assert [c.chat_id for c in added1] == [-1]
    assert [c.chat_id for c in added2] == [-2]


@respx.mock
def test_a_409_names_webhooks_rather_than_leaving_a_bare_status_code():
    """getUpdates returns 409 when a webhook is set on the token. The status code alone
    does not say why -- someone would have to go and look it up."""
    respx.get(UPDATES_URL).mock(return_value=httpx.Response(409, json={"ok": False}))
    with httpx.Client() as client, pytest.raises(WebhookConflictError) as excinfo:
        fetch_added(client, TOKEN, None)
    assert "webhook" in str(excinfo.value).lower()


@respx.mock
def test_an_http_error_propagates_for_the_caller_to_isolate():
    respx.get(UPDATES_URL).mock(return_value=httpx.Response(500))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        fetch_added(client, TOKEN, None)
