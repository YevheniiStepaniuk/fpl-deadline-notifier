import json

import httpx
import pytest
import respx

from notifier.telegram import ATTEMPTS, TelegramError, api_url, send_message

TOKEN = "123456:AAtoken"
CHAT = "987"
URL = api_url(TOKEN)
OK = {"ok": True, "result": {"message_id": 1}}


def _recording_sleep():
    slept = []
    return slept, slept.append


@respx.mock
def test_a_successful_send_posts_the_chat_id_and_text():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=OK))
    with httpx.Client() as client:
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert route.call_count == 1
    # Parsed, not compared as raw bytes: httpx serialises `json=` with compact
    # separators, so a byte-for-byte literal pins httpx's formatting rather than the
    # payload this test is about.
    assert json.loads(route.calls[0].request.read()) == {"chat_id": "987", "text": "hello"}


def test_the_token_is_in_the_url_path_not_a_query_string():
    """A token in a query string lands in every proxy and server log it passes."""
    assert api_url(TOKEN) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"


@respx.mock
def test_a_transient_failure_is_retried_and_can_succeed():
    route = respx.post(URL).mock(side_effect=[
        httpx.Response(502),
        httpx.Response(200, json=OK),
    ])
    with httpx.Client() as client:
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert route.call_count == 2


@respx.mock
def test_a_connection_error_is_retried():
    """The likeliest real failure on a home server is the network, not Telegram."""
    route = respx.post(URL).mock(side_effect=[
        httpx.ConnectError("no route"),
        httpx.Response(200, json=OK),
    ])
    with httpx.Client() as client:
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert route.call_count == 2


@respx.mock
def test_it_gives_up_after_three_attempts():
    route = respx.post(URL).mock(return_value=httpx.Response(500))
    with httpx.Client() as client, pytest.raises(TelegramError):
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert route.call_count == ATTEMPTS == 3


@respx.mock
def test_the_backoff_grows_between_attempts_and_does_not_sleep_after_the_last():
    """Sleeping after the final failure delays the raise for no benefit, and the tick
    loop is the thing that should decide when to try again."""
    respx.post(URL).mock(return_value=httpx.Response(500))
    slept, sleep = _recording_sleep()
    with httpx.Client() as client, pytest.raises(TelegramError):
        send_message(client, TOKEN, CHAT, "hello", sleep=sleep)
    assert slept == [1.0, 4.0]


@respx.mock
def test_a_401_is_not_retried():
    """A bad token will be bad on the third attempt too. Failing fast puts the real
    cause in the log instead of three timeouts."""
    route = respx.post(URL).mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    with httpx.Client() as client, pytest.raises(TelegramError) as excinfo:
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert route.call_count == 1
    assert "Unauthorized" in str(excinfo.value)


@respx.mock
def test_a_400_is_not_retried():
    """Chat not found, or a chat that has never messaged the bot. Also permanent."""
    route = respx.post(URL).mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "chat not found"})
    )
    with httpx.Client() as client, pytest.raises(TelegramError):
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert route.call_count == 1


@respx.mock
def test_a_permanent_status_sets_permanent_true_and_carries_its_status_code():
    """`.permanent` is what `_greet` reads to decide a chat is not worth retrying, and
    `.status_code` is what lets it further tell a chat-level permanent failure
    (400/403) apart from a token-level one (401/404) -- see
    notifier/__main__.py's `_BOT_LEVEL_STATUSES`. Asserted here at the source rather
    than only behaviourally through test_main.py."""
    respx.post(URL).mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    with httpx.Client() as client, pytest.raises(TelegramError) as excinfo:
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert excinfo.value.permanent is True
    assert excinfo.value.status_code == 401


@respx.mock
def test_exhausted_attempts_sets_permanent_false_and_no_status_code():
    """Running out of attempts against a transient failure is a different claim from a
    permanent status, and there is no single status code to name: the three attempts
    need not even fail the same way."""
    respx.post(URL).mock(return_value=httpx.Response(500))
    with httpx.Client() as client, pytest.raises(TelegramError) as excinfo:
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert excinfo.value.permanent is False
    assert excinfo.value.status_code is None


@respx.mock
def test_a_429_is_retried():
    """Rate limiting is transient, unlike the other 4xx cases."""
    route = respx.post(URL).mock(side_effect=[
        httpx.Response(429), httpx.Response(200, json=OK),
    ])
    with httpx.Client() as client:
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert route.call_count == 2


@respx.mock
def test_the_bot_token_is_not_repeated_in_the_error_message():
    """The error is logged, and logs get pasted into issues."""
    respx.post(URL).mock(return_value=httpx.Response(500))
    with httpx.Client() as client, pytest.raises(TelegramError) as excinfo:
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
    assert TOKEN not in str(excinfo.value)


@respx.mock
def test_an_ok_false_body_with_a_200_status_is_still_a_failure():
    """Telegram has answered 200 with ok:false. Treating that as sent would write the
    key and permanently swallow the alert."""
    respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": False, "description": "nope"}))
    with httpx.Client() as client, pytest.raises(TelegramError):
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)


@respx.mock
def test_a_200_with_a_body_that_is_not_json_is_a_failure_not_a_crash():
    """A proxy or a captive portal answering 200 with HTML. response.json() raises
    JSONDecodeError, a ValueError -- and the tick loop catches TelegramError and nothing
    else, so anything else escaping here takes the service down rather than retrying."""
    respx.post(URL).mock(return_value=httpx.Response(200, content=b"<html>gateway</html>"))
    with httpx.Client() as client, pytest.raises(TelegramError):
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)


@respx.mock
def test_a_200_whose_json_is_not_an_object_is_a_failure():
    """Valid JSON, wrong shape. `.get` on a list raises AttributeError, the same leak
    as above by a different route."""
    respx.post(URL).mock(return_value=httpx.Response(200, json=["not", "an", "object"]))
    with httpx.Client() as client, pytest.raises(TelegramError):
        send_message(client, TOKEN, CHAT, "hello", sleep=lambda _: None)
