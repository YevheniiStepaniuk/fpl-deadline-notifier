import httpx
import pytest
import respx

from notifier.telegram import api_url
from notifier.updates import (
    ChatAdded,
    Command,
    PermanentPollError,
    WebhookConflictError,
    fetch_updates,
    parse_added,
    parse_commands,
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


def test_a_non_string_title_is_dropped_rather_than_rendered_raw():
    """A malformed `"title"` must not sail through to `format_intro`, whose `str |
    None` annotation is a promise this is where it gets kept -- otherwise a message
    could end up saying 'added to "{'a': 1}"'."""
    payload = {"ok": True, "result": [
        _my_chat_member(1, -1, "channel", "left", "member"),
    ]}
    payload["result"][0]["my_chat_member"]["chat"]["title"] = {"a": 1}
    added, _ = parse_added(payload)
    assert added[0].title is None


def test_restricted_with_is_member_true_is_an_add():
    """A supergroup with restrictive defaults can land a freshly-added bot in
    'restricted' rather than 'member'. The bare status string does not say whether the
    bot is actually in the chat -- `is_member` does -- so a status-only present/absent
    check would miss this add entirely."""
    payload = {"ok": True, "result": [{
        "update_id": 1,
        "my_chat_member": {
            "chat": {"id": -1, "type": "supergroup"},
            "old_chat_member": {"status": "left"},
            "new_chat_member": {"status": "restricted", "is_member": True},
        },
    }]}
    added, _ = parse_added(payload)
    assert [c.chat_id for c in added] == [-1]


def test_restricted_with_is_member_false_is_absent_so_a_re_add_still_counts():
    payload = {"ok": True, "result": [{
        "update_id": 1,
        "my_chat_member": {
            "chat": {"id": -1, "type": "supergroup"},
            "old_chat_member": {"status": "restricted", "is_member": False},
            "new_chat_member": {"status": "member"},
        },
    }]}
    added, _ = parse_added(payload)
    assert [c.chat_id for c in added] == [-1]


def test_restricted_to_member_is_not_a_second_add_once_already_present():
    """The chat was already counted present at left -> restricted(is_member=True); the
    following restricted -> member transition must not double-fire, or that chat would
    get a second intro for the same join."""
    payload = {"ok": True, "result": [{
        "update_id": 1,
        "my_chat_member": {
            "chat": {"id": -1, "type": "supergroup"},
            "old_chat_member": {"status": "restricted", "is_member": True},
            "new_chat_member": {"status": "member"},
        },
    }]}
    added, _ = parse_added(payload)
    assert added == []


@respx.mock
def test_fetch_updates_requests_every_update_type_this_service_reads():
    """allowed_updates now covers all three update types this service reads -- adds
    via my_chat_member, commands via message or channel_post -- so getUpdates queues
    nothing else. Privacy mode is what keeps "message" from also handing over
    ordinary group chatter; see the module docstring. "channel_post" is not a
    redundant duplicate of "message": Telegram delivers a channel's posts under this
    separate key even though the payload shape is identical (see ALLOWED_UPDATES'
    own comment) -- without it, /nextdeadline silently never reaches the bot in a
    channel."""
    route = respx.get(UPDATES_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )
    with httpx.Client() as client:
        fetch_updates(client, TOKEN, None)
    request = route.calls[0].request
    assert "my_chat_member" in str(request.url)
    assert "message" in str(request.url)
    assert "channel_post" in str(request.url)
    assert "timeout=0" in str(request.url)
    assert "offset" not in str(request.url)


@respx.mock
def test_fetch_updates_omits_offset_on_the_first_call_and_sends_it_on_the_next():
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
        added1, _commands1, offset1 = fetch_updates(client, TOKEN, None)
        assert offset1 == 10
        added2, _commands2, offset2 = fetch_updates(client, TOKEN, offset1 + 1)
        assert offset2 == 11
    assert "offset" not in str(route.calls[0].request.url)
    assert "offset=11" in str(route.calls[1].request.url)
    assert [c.chat_id for c in added1] == [-1]
    assert [c.chat_id for c in added2] == [-2]


@respx.mock
def test_fetch_updates_returns_both_adds_and_commands_from_one_poll():
    """The whole reason `fetch_updates` parses one payload two ways rather than
    polling twice: a single batch can carry both an add and a command, and both must
    come back from the one GET this makes."""
    payload = {"ok": True, "result": [
        _my_chat_member(1, -100999, "channel", "left", "member", title="News"),
        _message(2, -200, "/nextdeadline"),
    ]}
    respx.get(UPDATES_URL).mock(return_value=httpx.Response(200, json=payload))
    with httpx.Client() as client:
        added, commands, highest = fetch_updates(client, TOKEN, None)
    assert [c.chat_id for c in added] == [-100999]
    assert commands == [Command(chat_id=-200, name="nextdeadline")]
    assert highest == 2


@respx.mock
def test_a_409_names_webhooks_rather_than_leaving_a_bare_status_code():
    """getUpdates returns 409 when a webhook is set on the token. The status code alone
    does not say why -- someone would have to go and look it up."""
    respx.get(UPDATES_URL).mock(return_value=httpx.Response(409, json={"ok": False}))
    with httpx.Client() as client, pytest.raises(WebhookConflictError) as excinfo:
        fetch_updates(client, TOKEN, None)
    assert "webhook" in str(excinfo.value).lower()


def test_webhook_conflict_is_a_permanent_poll_error():
    """`_greet` catches `PermanentPollError` to decide whether to stop polling for the
    rest of the process; 409 has to be one of those or it would be warned about every
    tick forever instead of disabling polling once."""
    assert issubclass(WebhookConflictError, PermanentPollError)


@respx.mock
def test_a_401_disables_further_polling():
    """A bad or revoked token will be just as bad on the next tick. Raising the
    dedicated error is what lets `_greet` stop trying instead of logging the same
    warning every 60 seconds for a service meant to run all season."""
    respx.get(UPDATES_URL).mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    with httpx.Client() as client, pytest.raises(PermanentPollError) as excinfo:
        fetch_updates(client, TOKEN, None)
    assert "401" in str(excinfo.value)


@respx.mock
def test_a_404_disables_further_polling():
    """404 from getUpdates means the token itself does not resolve to a bot -- no
    retry fixes that either."""
    respx.get(UPDATES_URL).mock(
        return_value=httpx.Response(404, json={"ok": False, "description": "Not Found"})
    )
    with httpx.Client() as client, pytest.raises(PermanentPollError):
        fetch_updates(client, TOKEN, None)


@respx.mock
def test_an_http_error_propagates_for_the_caller_to_isolate():
    """A 500 is Telegram's problem, not the token's -- transient, so it must not be
    treated as a PermanentPollError and must not disable polling."""
    respx.get(UPDATES_URL).mock(return_value=httpx.Response(500))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        fetch_updates(client, TOKEN, None)


# --- parse_commands / /nextdeadline matching ------------------------------------------


def _message(update_id, chat_id, text, chat_type="group"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": 1735689600,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": 1, "is_bot": False, "first_name": "Someone"},
            "text": text,
        },
    }


def _channel_post(update_id, chat_id, text):
    # Same `Message` shape as `_message` (confirmed against the Bot API docs: both
    # `message` and `channel_post` are typed as `Message`, with identical `chat` and
    # `text` fields) -- only the top-level key and `chat.type` differ.
    return {
        "update_id": update_id,
        "channel_post": {
            "message_id": 1,
            "date": 1735689600,
            "chat": {"id": chat_id, "type": "channel"},
            "text": text,
        },
    }


def test_the_plain_command_triggers():
    payload = {"ok": True, "result": [_message(1, -200, "/nextdeadline")]}
    commands, highest = parse_commands(payload)
    assert commands == [Command(chat_id=-200, name="nextdeadline")]
    assert highest == 1


def test_a_command_posted_in_a_channel_also_triggers():
    """A command sent in a channel arrives under "channel_post", not "message" --
    without also reading that key, /nextdeadline would work in every chat type
    except the one the intro's "channels are a real case" comments already treat as
    real (see ChatAdded's chat_type comment)."""
    payload = {"ok": True, "result": [_channel_post(1, -100555, "/nextdeadline")]}
    commands, highest = parse_commands(payload)
    assert commands == [Command(chat_id=-100555, name="nextdeadline")]
    assert highest == 1


def test_a_channel_post_lookalike_message_does_not_trigger():
    payload = {"ok": True, "result": [_channel_post(1, -100555, "please run /nextdeadline later")]}
    commands, _ = parse_commands(payload)
    assert commands == []


def test_the_bot_suffixed_command_triggers():
    """Telegram commonly suffixes group commands with the bot's own username; both
    forms must resolve to the same command name."""
    payload = {"ok": True, "result": [
        _message(1, -200, "/nextdeadline@fantasyreminderbot"),
    ]}
    commands, _ = parse_commands(payload)
    assert commands == [Command(chat_id=-200, name="nextdeadline")]


def test_matching_is_case_insensitive():
    payload = {"ok": True, "result": [_message(1, -200, "/NextDeadline")]}
    commands, _ = parse_commands(payload)
    assert commands == [Command(chat_id=-200, name="nextdeadline")]


def test_surrounding_whitespace_is_ignored():
    payload = {"ok": True, "result": [_message(1, -200, "  /nextdeadline  \n")]}
    commands, _ = parse_commands(payload)
    assert commands == [Command(chat_id=-200, name="nextdeadline")]


def test_a_lookalike_message_does_not_trigger():
    """The command has to be the message, not a word inside a sentence about it --
    otherwise "please run /nextdeadline later" would fire it too."""
    payload = {"ok": True, "result": [_message(1, -200, "please run /nextdeadline later")]}
    commands, _ = parse_commands(payload)
    assert commands == []


def test_a_word_containing_the_command_name_is_parsed_as_a_different_command():
    """/nextdeadlinee is a syntactically valid command, just not this one -- parsed as
    its own (unrecognised) name rather than folded into "nextdeadline" by a boundary
    check that only looked at the prefix. Recognising *which* command names matter is
    __main__'s job, not parse_commands'; see the module's own docstring on `Command`."""
    payload = {"ok": True, "result": [_message(1, -200, "/nextdeadlinee")]}
    commands, _ = parse_commands(payload)
    assert commands == [Command(chat_id=-200, name="nextdeadlinee")]
    assert commands[0].name != "nextdeadline"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"ok": True},
        {"ok": True, "result": "not a list"},
        {"ok": True, "result": [None]},
        {"ok": True, "result": ["not a dict"]},
        {"ok": True, "result": [{"update_id": 1}]},
        {"ok": True, "result": [{"update_id": 1, "message": "not a dict"}]},
        {"ok": True, "result": [{"update_id": 1, "message": {}}]},
        {"ok": True, "result": [{
            "update_id": 1,
            "message": {"chat": "not a dict", "text": "/nextdeadline"},
        }]},
        {"ok": True, "result": [{
            "update_id": 1,
            "message": {"chat": {"id": -1, "type": "group"}, "text": 12345},
        }]},
        {"ok": True, "result": [{
            "update_id": 1,
            "message": {"chat": {"type": "group"}, "text": "/nextdeadline"},  # no id
        }]},
    ],
)
def test_a_malformed_message_update_is_skipped_rather_than_raising(payload):
    """Mirrors parse_added's own tolerance test: no cache exists here for a misread
    emptiness to corrupt, so skipping a bad entry is correct, not a compromise."""
    commands, _ = parse_commands(payload)  # must not raise
    assert commands == []


def test_a_malformed_message_update_still_advances_past_its_update_id():
    payload = {"ok": True, "result": [{"update_id": 9, "message": "garbage"}]}
    _, highest = parse_commands(payload)
    assert highest == 9


def test_parse_commands_ignores_my_chat_member_updates_but_still_counts_their_id():
    """The two parsers share one payload (see fetch_updates); an add-only entry must
    not be misread as a command, but its update_id still has to be accounted for so
    the offset both parsers compute agrees."""
    payload = {"ok": True, "result": [
        _my_chat_member(5, -1, "channel", "left", "member"),
    ]}
    commands, highest = parse_commands(payload)
    assert commands == []
    assert highest == 5
