"""Redacting Telegram bot tokens out of the log stream.

Split out of test_main.py rather than folded in: test_main.py's own docstring says
it carries "one test per wiring decision" about the tick loop, and this is a
different kind of regression -- logging hygiene, not tick behaviour -- with its own
end-to-end case that boots `main` for real.
"""

import logging

import httpx
import pytest
import respx

from notifier.__main__ import _redact, _TokenRedactingFilter, main
from notifier.sources import DRAFT_URL, FPL_URL
from notifier.telegram import api_url

# Shaped like a real Telegram token (digits, colon, alphanumerics/underscores/hyphens)
# but obviously not one: long enough to be realistic, `FakeNotARealToken` unmistakable
# if it ever did leak somewhere.
FAKE_TOKEN = "555444333:AAFakeNotARealToken-9x_Qz1uV2w3"


def _formatted(record: logging.LogRecord) -> str:
    """The string that would actually reach a handler's output.

    Asserting against `record.msg` or `record.args` in isolation would pass even if
    the two disagree -- e.g. `msg` rewritten but `args` still holding the token, which
    %-formats right back in. `getMessage()` is what every handler calls, so it is the
    only check that proves the token is gone from what a human reading the log sees.
    """
    return record.getMessage()


class _Explodes:
    """A value whose __str__ raises, to prove the filter survives one."""

    def __str__(self):
        raise RuntimeError("boom")


# --- _redact / _TokenRedactingFilter, in isolation ----------------------------------


def test_a_token_in_an_httpx_shaped_record_is_redacted():
    """The case that matters: httpx logs `logger.info('...%s %s "%s %d %s"', method,
    url, http_version, status, reason)` -- the token lives in `record.args`, not in
    `record.msg`. A filter that only rewrote `msg` would pass a test built around a
    pre-formatted string and redact nothing once httpx actually logs a request."""
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=(
            "GET",
            httpx.URL(f"https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates?timeout=0"),
            "HTTP/1.1",
            404,
            "Not Found",
        ),
        exc_info=None,
    )
    assert _TokenRedactingFilter().filter(record) is True
    formatted = _formatted(record)
    assert FAKE_TOKEN not in formatted
    assert "getUpdates" in formatted, "the method name is the useful part; it must survive"


def test_a_token_baked_directly_into_record_msg_is_also_redacted():
    """Not every logger is httpx -- a future direct `log.info(f"... {url}")` in this
    codebase would put the token straight into `msg` with no `args` involved."""
    record = logging.LogRecord(
        name="notifier",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=f"calling https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage",
        args=(),
        exc_info=None,
    )
    _TokenRedactingFilter().filter(record)
    formatted = _formatted(record)
    assert FAKE_TOKEN not in formatted
    assert "sendMessage" in formatted


@pytest.mark.parametrize("method", ["getUpdates", "sendMessage"])
def test_the_method_name_survives_redaction(method):
    """getUpdates and sendMessage must stay distinguishable in the logs -- redacting
    the whole path would make every request look the same."""
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("GET", f"https://api.telegram.org/bot{FAKE_TOKEN}/{method}", "HTTP/1.1", 200, "OK"),
        exc_info=None,
    )
    _TokenRedactingFilter().filter(record)
    assert method in _formatted(record)


def test_a_record_with_no_token_is_untouched():
    """A filter that rewrites everything it sees, token or not, would be indistinguishable
    from one doing nothing useful -- this pins that the no-op path really is a no-op."""
    record = logging.LogRecord(
        name="notifier",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="refresh failed for %s (%s); holding %d cached moments",
        args=("fpl", "boom", 3),
        exc_info=None,
    )
    original_args = record.args
    _TokenRedactingFilter().filter(record)
    assert record.msg == "refresh failed for %s (%s); holding %d cached moments"
    assert record.args == original_args


def test_args_as_a_dict_does_not_break_the_filter():
    """logging supports %(name)s-style records whose `args` is a dict rather than a
    tuple -- untested, this branch would AttributeError the first time one showed up
    and take the log record out (or worse, per the filter's own docstring)."""
    record = logging.LogRecord(
        name="notifier",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="calling %(url)s",
        # `LogRecord.__init__` unwraps a single-dict `args` tuple into a bare dict --
        # the same shape `logger.info(msg, {"a": 1})` produces -- so that is what has
        # to be passed in here for `record.args` to actually come out as a dict.
        args=({"url": f"https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates"},),
        exc_info=None,
    )
    assert _TokenRedactingFilter().filter(record) is True
    assert FAKE_TOKEN not in _formatted(record)
    assert "getUpdates" in _formatted(record)


def test_a_non_string_arg_such_as_a_status_code_does_not_break():
    """httpx's format string uses %d for the status code -- if the filter stringified
    every arg unconditionally, `str(404)` would still %-format fine here, but a value
    that only converts *meaningfully* as part of a larger object (an int standing
    alone) must not be mangled into the wrong type for its own placeholder."""
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("GET", "https://api.telegram.org/no-token-here", "HTTP/1.1", 404, "Not Found"),
        exc_info=None,
    )
    assert _TokenRedactingFilter().filter(record) is True
    assert "404" in _formatted(record)


def test_filter_never_raises_even_when_a_value_refuses_to_stringify():
    """The filter's own docstring: a filter that raises takes its record out and,
    depending on the handler, possibly more than that. This runs on every request the
    service makes, so a broken value must degrade to un-redacted, not to a crash."""
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="%s",
        args=(_Explodes(),),
        exc_info=None,
    )
    assert _TokenRedactingFilter().filter(record) is True


def test_redact_leaves_a_plain_value_alone():
    assert _redact("no token here") == "no token here"
    assert _redact(200) == 200


# --- end to end: the wiring in `main`, not just the filter --------------------------


@respx.mock
def test_end_to_end_a_real_tick_never_logs_the_bot_token(monkeypatch, tmp_path, capsys):
    """A unit test of the filter proves the regex works; it proves nothing about
    whether anyone actually attached it. `logging.basicConfig` is called only from
    `main`, and that is where the filter gets attached to the handler -- so this
    drives a real `main(["--once"])` tick against a respx-mocked Telegram, using
    `fetch_updates`/`send_message` exactly as the service does, and inspects the actual
    captured stdout. If a future edit moves or drops the `handler.addFilter(...)`
    call in `main`, this is the test that notices -- a filter-only unit test above
    would keep passing right through that regression.
    """
    # basicConfig is a no-op once the root logger already has a handler, which it will
    # if any earlier test in this process called `main`. Clearing first forces `main`
    # to actually (re)install its own handler -- with its own filter -- against the
    # *current* sys.stdout, which is what capsys is capturing for this test.
    saved_handlers, saved_level = logging.root.handlers[:], logging.root.level
    logging.root.handlers.clear()
    try:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "987")
        monkeypatch.setenv("NOTIFIER_STATE_PATH", str(tmp_path / "state.json"))

        import json
        import pathlib

        fixtures = pathlib.Path(__file__).parent / "fixtures"
        respx.get(FPL_URL).mock(
            return_value=httpx.Response(200, json=json.loads((fixtures / "fpl_bootstrap.json").read_text()))
        )
        respx.get(DRAFT_URL).mock(
            return_value=httpx.Response(200, json=json.loads((fixtures / "draft_bootstrap.json").read_text()))
        )
        respx.get(api_url(FAKE_TOKEN, "getUpdates")).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": []})
        )
        # Mocked unconditionally, whether or not this tick actually has an alert due
        # (that depends on the fixtures' deadlines vs. wall-clock `now`, which this
        # test does not control) -- an unused mock is harmless, an unmocked request
        # respx would refuse to serve is not.
        respx.post(api_url(FAKE_TOKEN)).mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))

        assert main(["--once"]) == 0
    finally:
        logging.root.handlers[:] = saved_handlers
        logging.root.setLevel(saved_level)

    out = capsys.readouterr().out
    assert FAKE_TOKEN not in out
    assert "getUpdates" in out, "httpx's own INFO log line should still be present, just redacted"
